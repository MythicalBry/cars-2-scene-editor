# Ambient light volumes: the light fireworks, explosions and pickups throw on the world

When a firework bursts over tokyo_b, the buildings and track below turn red, gold, green or blue. That light
comes from **ambient light volumes**: `SubGeometryAmbient` parts that are never drawn as geometry. The engine
renders them into a screen-space **ambient buffer**, which every world shader adds to its lighting. The Cars
2 Scene Editor implements this in `ambientvol.py`, and the Cars 2 Game renderer draws the buffer before its
opaque pass.

---

## 1. Where they are

About 40 choreography packages have them. All are spheres using material effect `a_1_dcxvucmlc_ab`:

- fireworks: `fireWorksA`, `HoloGateFinish_Fireworks`
- explosions and fires: `explosionLargeA`, `explosionfireyb`, `fireburnout`, `mi_gianttankexplosion`
- weapons: `cin_machinegun`, `cin_muzzleflash`, the leech and impact mines, `satbeam_arena`
- pickups: `pkup_respncgenerica/b`, `pkup_respncspya`, `energygain_b`, `energygain_boila/b`
- gates: `lapgate30`, `race_survivalgate`
- level props: `bridgemain`, `bridgesmall`, `lon_xingmech(b)`
- cinematics: the oil rig parts 1–3, `h4c`, `disruptor`

In worlds, only `mi_oil` and the tutorials have them. Those use a `SolidColor` material with no
`emissiveColor`, so they add nothing.

## 2. Node fields (`FUN_00c95dc0`, the `SubGeometryAmbient` loader)

```c
// FUN_00c95dc0 (decompiled, trimmed)
read_base_node_fields(...);                          // the SubGeometry fields
type = read_int("AmbientVolumeType");                // 1 box, 2 sphere, 3 plane
if (type) {
    falloff = read_float(0.0,  "AmbientVolumeFalloffDist");
    falloff = 0.0;                                   // read, then overwritten: never used
    fade    = read_float(150.0, "AmbientVolumeFadeDist");
    bias    = read_float(1.05,  "AmbientVolumeScaleBias");
    ext     = (10, 10, 10); read_vec3(&ext, "AmbientVolumeExtents");
    ext     = max(ext, 1.0);                         // per axis
    mul     = 1.0; col = (0, 0, 0);
    get_material_param(mat, "emissiveColor")      -> col;
    get_material_param(mat, "emissiveMultiplier") -> mul;
    node->renderCaps = 0x2000;
}
```

`FUN_00c95860` chooses the engine effect `ambient` (in the startup zips) with technique `box`/default (type 1),
`sphere` (type 2) or `plane` (type 3).

The light is **the part material's `emissiveColor` × `emissiveMultiplier`**. `emissiveColor` has `Gamma 1`
in the effect, so it is converted from sRGB to linear when the material loads. Clips animate both. The
fireworks pulse `emissiveMultiplier` from 0 to about 3.6 at each burst and decay. They key only some colour
channels: the red light keys R = 1 and keeps the material's G 0.33 and B 0.45, which is why the city glows
pink.

The fireworks' lights are also keyed `Visibility` 0 for the whole clip. The volume still lights: the
multiplier is what switches it.

## 3. The ambient pass (`ambient.hlsl`, engine effect `ambient`)

```hlsl
// sphereVS: the volume mesh, enlarged by scaleBias
vPosition *= scaleBias;
...
// spherePS (cubePS is the same with CubeToSphere(clamp(d, -1, 1)))
float viewDepth = tex2D(samDepthViewBuffer, UvScreen).x;     // linear depth: -z / far
float depth = -IN.ViewPosition.z / g_CameraParams.y;
if (depth < viewDepth) return 0;
float3 viewDepthPos = IN.ViewPosition / depth * viewDepth;   // the surface behind this pixel
viewDepthPos = mul(float4(viewDepthPos, 1), g_ViewInverse).xyz;
float3 d = mul(viewDepthPos - IN.VolumePosition, (float3x3)g_WorldInverseMatrix) / volumeSize;
float r = length(d);
float b = 1 - saturate((r - fadeParams.x) / (1 - fadeParams.x));
return float4(emissiveColor, 1) * b * b * b * emissiveMultiplier;
```

- Blend `One / One` (additive), no depth test, back faces (`CullBackFaces 2`).
- `volumeSize` = Extents, `fadeParams` = (falloff, FadeDist), so `fadeParams.x` is always 0.
- **Plane** volumes write only the alpha channel (`ColorWriteEnable 8`, `BlendOpAlpha MAX`) and add no light.
- Every world shader then does, for example in `a_1_dbcmqdrvucm`:

  ```hlsl
  lightingDiffuse *= IN.ColorOcclusion.rgb;
  lightingDiffuse += tex2D(samAmbientBuffer, UvScreen).rgb;
  float3 combinedDiffuse = colorDiffuse.rgb * lightingDiffuse;
  ```

So a volume adds `colour × multiplier × (1 − r)³` of light, where r is the surface's distance from the
volume centre over its extents. The fireworks' spheres have extents of 419 m and a scale bias of 1.5.

## 4. How the Cars 2 Game renderer does it

1. Collect the volumes that give light now: enabled, not disabled in viewports, colour × multiplier > 0.
2. If there are any, the depth pre-pass takes **every** opaque part, not just those near the camera, because
   the volumes reach hundreds of metres.
3. For each volume, a full-screen pass rebuilds the surface position from the linear depth and applies the
   formula above, additively into an RGBA16F target. Volumes are culled by their bounding sphere.
4. That target is bound as `samAmbientBuffer` for the opaque and blended passes.

The editor gives each volume keyframeable settings (`obj.c2_ambient`). World volumes export back to their
node (type, extents, scale bias, falloff, fade) and material (colour, multiplier); only edited ones are
written.
