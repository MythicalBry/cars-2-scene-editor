# Cars 2 Arcade effect names: grammar, interpretation and decompilation notes

This document explains how the effect (shader) names of *Cars 2: The Video Game – Arcade* (the
Win32 build of the Xbox 360 game, `sdaemon.exe`) are built, what every fragment means, and what
is known about the compiled shaders. It is written for an agent that will:

1. write HLSL that recompiles to the same bytecode as each shipped shader blob, and
2. recover the Lua-side generator that produced the per-effect `.fx` files from the names.

Everything here was measured on the shipped files. Each claim has a confidence level:

| Level | Meaning |
|---|---|
| **confirmed** | Isolated by minimal pairs: two effects whose names differ only by the fragment differ exactly as described (interface and/or compiled code), or the engine code was read. |
| **likely** | Consistent interface evidence, but no clean minimal pair. |
| **unresolved** | No observable difference found. The hypotheses are listed. |

## Files

| File | Contents |
|---|---|
| `effect_name_fragments.csv` | One row per (grammar position, fragment): meaning, confidence, number of effects using it, examples, an automatically computed **evidence signature** (features present in ≥95% of effects with the fragment and ≤30% of same-family effects without it), and notes. 297 rows: 246 confirmed, 42 likely, 9 unresolved. |
| `effect_shader_tags.csv` | One row per distinct effect blob (1,292): source file, md5, parsed tokens, techniques, shader models, instruction slots, HLSL source path, include files, helper functions, samplers, engine inputs (from the CTAB), parameters, Gamma parameters, vertex streams with semantics, render states, and 63 boolean **feature tags derived from the data, not from the name**. |
| `tools/` | The scripts that produced all of this (see [Reproducing](#reproducing)). |

## 1. The corpus

| Where | Effects |
|---|---|
| `assets/effects/effects/*.tfx` (loose) | 1,263 — the material effects the worlds reference |
| `assets/effects/effects.zip` | same files as the loose folder |
| `assets/startup.zip`, `startup2.zip`, `startup3.zip` (+ `_cafe_mod` copies) | engine effects: `shared`, `error`, `sprite`, `text`, `debugdraw`, `postprocess`, `avglum`, `ambient`, downsample/blur/motion blur… |
| package-local copies (`characters/…/*.zip:effects/`, `choreographies/…`, `particles/…`, `dlc/…`) | duplicates of loose effects, plus a few package-only ones |

1,292 distinct blobs, 1,288 distinct names. Four names exist with two different contents
(`error`, `sprite`, `text`, `debugdraw`). One copy of each was compiled from a **Wii** source tree
(`D:/dev-cars/Main/Cars/Temp/Game-Cars/fx/win32_wii/…`, SM 2.0), the other from the normal tree.
The CSV's `also_in` column lists every location a blob was found in.

## 2. The `.tfx` container

A `.tfx` is a big-endian Tupperware tree (the same container as `.oct`). Top-level keys:

* `Scopes` – scope layout.
* `Parameters` – `Parameter#i` records with these fields:
  * `Name`, `Semantic`, `Type` (`float`/`int`/`bool`), `Rows`, `Cols`, `Elements`, `Bytes`;
  * `Scope` (`PerFrame`, `PerView`, `PerMaterial`, `PerInstance`, `NoRegister`, `Lighting`);
  * `RegisterVS` / `RegisterPS`, `Default`;
  * `Gamma` – see [§8](#8-engine-behaviour-the-names-do-not-show).
* `Samplers` – `Name` (CTAB sampler name, e.g. `samDiffuseMapA1`), `Semantic` (the material texture
  usage, e.g. `diffuseMapA1`), `Scope`, `RegisterPS`, `SamplerState#…` (`MinFilter`, `AddressU`…;
  values in `<…>` are parameter references, e.g. `<MaxAnisotropy>`).
* `Technique#<name>` – the techniques:
  * `Annotation` – `string#<Stream>` = `<semantic>`, which maps vertex-buffer element names to shader
    input semantics (e.g. `Uv1 = TEXCOORD2`, `lightmapUV = TEXCOORD3`). The mapping differs per
    effect; it's listed in `vertex_streams`.
  * `Pass#p0`:
    * `RenderState#<state>` – a value, or `<param>` to take it from a parameter;
    * `VertexShaderReference` / `PixelShaderReference` – indices into the pools.
* `VertexShaderPool` / `PixelShaderPool` – `VertexShader#i` / `PixelShader#i`, with `Data` = a D3D9
  shader token stream.

Material effects have the techniques `Standard` (main pass) and `Reflect` (planar-reflection
pass). Some also have `Depth` / `DepthView` (alpha-tested, blended, skinned), `Silhouette` (`rha`) or
`ProjectedTexture` (`rpt`). Particles use `Shader`, and engine effects use their own technique names.

## 3. The compiled shaders

* **Models:** `vs_3_0` / `ps_3_0`; the four Wii-tree utility effects are `vs_2_0` / `ps_2_0`.
* **Compiler:** the CTAB creator string is `Microsoft (R) D3DX9 Shader Compiler ` (no version
  number) on all 5,442 blobs.
* **Flags:** the CTAB flags are `0x20000101` = `D3DXSHADER_DEBUG` (0x1) | `D3DXSHADER_NO_PRESHADER`
  (0x100) | 0x20000000 (not a public D3DX flag; probably set by the compiler or tool version).
* **Debug info:** 5,414 of 5,442 blobs carry a `DBUG` comment block (`D3DXSHADER_DEBUGINFO`):
  * entry point names (`Standard_VS`, `Standard_PS`, …);
  * the source file list;
  * per-instruction source line and file;
  * variables with their writes (register + component).

  **No source text is embedded:** `SourceCode` offset = 0.

  `D3DDisassemble` (d3dcompiler_47) prints this as `#line N "file"` directives and
  `// name<components>` comments. That gives the HLSL local variable names, including inlined helper
  locals as `Function::local`.
* **File paths stored in the shaders:** `E:\dev-cars2\Main\Cars\Temp\Game-Cars\fx\win32\<effect name>.fx`. There is one
  generated `.fx` per effect name. The `Temp` folder suggests generated, not hand-written, files.
* **Line numbers:**
  * shader code starts around lines 1000–1600, so each generated `.fx` was long, with large shared
    preambles;
  * effects whose names differ only by a fragment with no effect have **identical** `#line` numbers,
    i.e. the generator emitted identical text;
  * use line numbers as a fingerprint of template structure.
* **Include files:**
  * `IncludeFiles/Lighting/SunLight.inc`;
  * `IncludeFiles/Lighting/DotSunLight.inc`;
  * `IncludeFiles/Lighting/SHLight.inc`;
  * `ShadowCascade.inc`;
  * `ShadowTest.inc`;
  * `HelperFunctions.inc` (post-process).

  Everything else was inlined into the generated `.fx`.
* **Helper functions** seen in debug info (count of locals):
  * `CalculateSHColor3`, `NormalMapping_PS`, `Calculate2x2PCFShadowAmount`, `CalculateSlopeBias`,
    `DecodeRGBMTex`, `CalculateShadowCascade2`, `NormalMapping_PS_splitUVs2`,
    `CalculateRot8ShadowAmount`, `CalculateShadowAmount`, `SampleColorBuffer`,
    `Calculate4x4PCFShadowAmount`, `CalculateShadowCascade(3)`, `ModifiedStepScalar`;
  * `NormalMapping2Layers1Look_PS_none`, `NormalMapping2Layers_VS`, `NormalMapping_VS_mod`,
    `SkinPosition`;
  * `CubeToSphere`, `getCubeBlendAmount`, `getSphereBlendAmount`;
  * post-process: `CalcExposedColor`, `ToneMapFunction`, `PixelEncode`/`PixelDecode`,
    `blurColor4…32`, `radialPassPS`, `CalculateDOF`, `Median`, `GetDilatedStencil`.
* **Constant registers:**
  * Engine globals have **fixed registers in every effect**, so the generator emitted explicit
    `register(cN)` bindings.
  * Pixel shader: `g_sunLightPrimary` c2, `g_sunLightTertiary` c3, `g_primaryTertiaryBlend` c4,
    `g_sunLightIntensity` c5, `g_sunLightSoftness` c6, `g_sunLightDirection` c7,
    `g_LightMapSpecularDir` c9, `g_LightMapSpecularColor` c10, `g_LightMapNormalDirection` c11,
    `g_LightmapNormalAmount` c12, `g_LightmapStartThresh` c13, `g_LightmapEndThresh` c14,
    `g_ShadowBlobColor` c21, `g_FogStart` c25, `g_FogEnd` c27, `g_EnvMapExposure` c43, `g_View` c44,
    `g_ViewProjection` c48, `g_ViewInverse` c52 (its `.w` column = camera position),
    `g_CameraParams` c60 (only `.y` = far plane is ever read), `g_TargetDimensions` c61,
    `ShadowMapDimensions` c62, `g_ShadowMatrix` c63, `g_ShadowZScale` c79,
    `g_ReflectionPlaneProj` c80, `g_ReflectionFogStart/End` c88/c89.
  * Vertex shader: `g_Time` c1, `g_View` c16, `g_ViewProjection` c20, `g_ViewInverse` c24,
    `scrollU1/V1` c36/c37, split scales/scrolls c36–c41, `g_WorldMatrix` c40 (usually),
    `g_WorldInverseMatrix` c44, `g_BoneTransforms` c49.
  * Material and instance parameters are allocated by the compiler from **c92** upward (first
    free register after the engine block), in declaration/first-use order. `objectSH` (7 registers)
    usually lands right after the material constants.
* **Engine-bound samplers** are not in `Samplers`; they appear only in the CTAB:
  * `samFogMap` (fog cube) s0;
  * `samAmbientBuffer` (ambient volumes, screen space);
  * `samShadowMask` (dynamic shadow mask, screen space);
  * `samShadowTexture*`;
  * `samDepthViewBuffer` / `samDepthViewHalfBuffer`;
  * `samColorBuffer`;
  * `samReflectionPlane*`.

  The CSV column `engine_inputs` lists the CTAB names of each Standard pass.

## 4. How the engine uses the name

Only two things in `sdaemon.exe` look at an effect name:

1. **The draw-sort key** (material set-up in `sdaemon.exe`, at 0x841488):
   `key = strlen(name) + (name[0] << 8)`, then shifted by family prefix: `wtr` ≪2, `a_` ≪4, `math` ≪6,
   `glass` ≪8, `part` ≪10, `sky` ≪12. It's stored at material+0x14. It's a sort/batching key, not a
   decode of the fragments.
2. **Skinning variants:** the strings `_skin` and `_ptr_skin` are appended at run time to pick the
   skinned effect.

Everything else in the name is generator-side vocabulary. Some fragments (`qc`, a standalone `r`,
`eo`) produce **byte-identical** `.tfx` files, so they only mattered to the authoring tool or exporter.

### 4.1 What a material lists for its effect

A MaterialPool entry lists exactly the texture slots (`TextureEntries`, by `Usage`) and parameters
(`PropertyEntries`, by `Name`) that its effect uses. Checked against each effect's samplers and per-material
parameters, over all 21 Arcade worlds:

| | Materials |
|---|---|
| Materials checked | 3,798 |
| Missing a texture slot the effect samples | 1: the eye shader's `renderedEyeTexture`, rendered by the engine |
| Listing a texture slot the effect doesn't sample | 0 (not counting `lightMap1` / `envMapInstance`, which come with the instance) |
| Listing a parameter the effect doesn't have | 0 |
| Leaving out a parameter the effect has | common (`AlphaBlendEnable` 806, `g_ReflectionPlane` 20): the effect default applies |

So texture slots are effectively required and parameters optional. Materials inside other packages are
looser: the pickup models' `a_1_dmmsmvucemcmlc_ab` materials (`weaponpickup`, `pickup_trimissile`) have no
`specularMapA1` entry although the effect samples one, so the engine tolerates a missing slot.

The scene editor follows the world convention when a material's effect is switched: slots and parameters
the new effect doesn't use are taken out (and kept in Blender for switching back), and export refuses
world materials with an empty required slot (`fxnodes.switch_effect_entries`, `fxnodes.missing_textures`;
check: `tests/mat_slot_survey.py`).

## 5. Name grammar

### 5.1 Family dispatch

Split the name on `_`. The first segment selects the grammar:

| First segment | Family | Grammar |
|---|---|---|
| `a` followed by a digit segment | general material | §5.2 |
| `m2`, `m3`, `m4` | multi-layer terrain blend | §5.3 |
| `glass` | glass | §5.4 |
| `skyd` | skydome | §5.5 |
| `part` | particles | §5.6 |
| `mathadd`, `mathmul`, `mathscrn`, `mathlnrlt` | framebuffer blend FX | §5.7 |
| `distrx`, `distsn` | screen distortion | §5.8 |
| `wtr`, `wtrc`, `wtrw`, `wtrwa`, `wtrwas` | water | §5.9 |
| `car` (`car_eyes`, `car_double_eyes`, `car_cheap_eyes`) | car eyes | §5.10 |
| `shadowmask`, `shadowmask2`, `shadowmaskchar` | shadow mask resolve | §5.11 |
| `shellf4` | shell (fur/grass) | uses the §5.2 slots |
| anything else | hand-written engine/utility effect (`shared`, `postprocess`, `depth_ak_`, `debug_lightmaps_color`, …) | the remaining segments are free-form options (`_skin`, `_ak`, `_mrt_nd`…) |

The reference tokenizer is `tools/fxname.py` (a/mN grammar) and `tools/fxlex.py` (all families). It
tokenizes all 1,288 names with no leftovers.

### 5.2 `a_<N>` — general materials

```
name      := "a_" N "_" layer { "_" layer } [ "_" shared ]   ; N layers; shared present iff N > 1
             [ "_" render ] [ "_" alpha ] [ "_" baked ] [ "_skin" ]
```

`a_1` (882 effects) has one segment that holds the whole descriptor. `a_2` (4 effects) has two
layer segments plus a shared segment.

#### Layer descriptor

The tokens are optional but always in this order (regex in `fxname.py: SLOTS`):

| # | Slot | Regex | Meaning |
|---|---|---|---|
| 1 | D diffuse | `d(t)?(i)?(us\|bc\|dc\|bl\|dl\|m\|u\|l\|k\|s)m` or `dc` | see the diffuse table below |
| 2 | X | `x` | unlit: no SH ambient, no sun, no lighting includes |
| 3 | N | `n` | normal map `normalMapA1` (+ `normalHeightA1`, Tangent/Binormal streams; `NormalMapping_PS`) |
| 4 | S specular | `s(m\|c)` | `sm` = `specularMapA1`, `sc` = `specularColorA1`; both add `specularShineA1`, `specularFresnelA1` (Blinn sun specular) |
| 5 | K | `kc` | subsurface: `subsurfaceColorA1`, `subsurfaceAmountA1`, `subsurfaceAmbientClamp` |
| 6 | R | `r` | rim light: `g_rimLightColor`, `g_rimLightModifier` |
| 7 | Q1 shadow receive | `q(d)?(sc)?r` | `q` + `d` (sample the dynamic screen-space shadow mask, or the cascades directly when blended) + `sc` (`shadowReceiveColor` tint) + `r` (receive). `qr` compiles the receive path with a constant shadow of 1 |
| 8 | Q2 | `qc` | shadow **cast**: no effect on the `.tfx` (byte-identical), so it's consumed outside the shader. *likely* |
| 9 | V vertex colour | `v`, `va`, `a` | `v` = `ColorOcclusion`; `va` = `ColorOcclusion` + `ColorAdditive`; `a` = `ColorAdditive` |
| 10 | U UV block | `u[rlc]*` | `uc` = scroll (`scrollU1/V1` × `g_Time`), `urc` = rotate (`rotationAngleUV`, `rotationOriginU/V`) + scroll, `ul` = slide (`slideU1/V1`) |
| 11 | E environment | `e(m\|i)(s\|r)?(m\|c)` | `e` + cube source (`m` = material cube `environmentMap`, `i` = instance probe `envMapInstance`) + optional `s` (scrolling env map `environmentScrollMap`, `environmentScrollRotation`) or `r` (refraction `environmentRefractionAmountA1/IndexA1`) + reflectivity source (`m` = `environmentReflectivityMapA1`, `c` = `environmentReflectivityColorA1`). Schlick fresnel `environmentFresnelA1` |
| 12 | M | `m` | terminator of the base layer. **unresolved**: present in every a/mN descriptor, absent in glass/skyd/part/math/wtr and the a_2 shared block |
| 13 | MOD modulation layer | `dmumum`, `dasumum`, `dlbumum`, `dcbumum`, `numum`, `umvum`, `dasnmm` | second texture `modMap` on the `UvMod` stream. `d<op>umum` blends it onto diffuse with operator `op` (`m` multiply, `as` add-subtract = linear burn, `lb` linear burn, `cb` colour burn). `numum` = modulation normal map `normalMapMod` (+ `TangentMod`/`BinormalMod`). `umvum` = UV-offset map `modUvMap` |
| 14 | L emissive (glow) | `l(mc\|m\|c)` | `lm` = `emissiveMapA1` × `emissiveMultiplier` (map alpha premultiplies), `lc` = `emissiveColor` × multiplier, `lmc` = map × colour × multiplier |
| 15 | F | `f` | `g_FogBlend` |
| 16 | P | `p` | depth bias: `DepthBiasValue`, `SlopeScaleDepthBias` |
| 17 | Z | `0z` | `ZEnable = 0` |

#### Diffuse slot

`d` + `t` (tertiary `diffuseColorTertiary`) + `i` (intensity map `diffuseIntensityMapA1` ×
`intensityRange`) + blend operator + `m` (map). `dc` means colour only, no map. The operator says how
`diffuseMapA1` combines with `diffuseColorA1`. Every row was confirmed from the compiled maths:

| Fragment | Operator | HLSL |
|---|---|---|
| `dmm` | multiply | `tex * diffuseColorA1` |
| `dum` | multiply | identical code and `#line` layout to `dmm`; the authoring meaning of `u` is unknown |
| `dlm` | lighten | `max(tex, color)` |
| `dkm` | darken | `min(color, tex)` |
| `dsm` | screen | `1 - (1-tex)(1-color)` |
| `dbcm` | colour burn | `1 - saturate((1-tex) / color)` |
| `ddcm` | colour dodge | `saturate(tex / (1-color))` |
| `dblm` | linear burn | `saturate(tex + color - 1)` |
| `ddlm` | linear dodge | `saturate(tex + color)` |
| `dusm` | scale | `tex * diffuseScaleA1` (scalar instead of colour). *likely* |
| `dc` | — | `diffuseColorA1` only |

The operator vocabulary is the Photoshop blend modes (m, s, k, l, bc, dc, bl, dl). The `math*`
family names use the same idea at framebuffer level.

#### Shared block (N > 1)

The layers carry the sources (`s<m|c>`, `e<i|m><m|c>`); the shared block switches features on, in
its **own** order:

```
shared := [n] [s] [e(i|m)c?] [r] [qc] [qdr] [v] [m] [umum | dasnmm]
```

Note that `qc` comes **before** `qdr` here, the reverse of the layer order. Layers are numbered A1,
A2… in the parameter and sampler names (`diffuseMapA2`, `normalHeightA2`, `Uv2`, `Tangent2`…). a_2
layers are blended by the `ColorBlend` vertex stream.

#### Render-extras segment

```
render := "r" [ "r" | "rr" ] [ "ll" | "lwl" ] [ "ha" ] [ "eo" ]
```

| Part | Meaning | Confidence |
|---|---|---|
| `r` alone | nothing: byte-identical to the effect without it | confirmed |
| `rr` | planar reflection receive: `ReflectionPlaneTexture`, `ReflectionPlaneDepthBuffer`, `g_ReflectionPlane`, `g_ReflectionPlaneFresnel`, `g_AlphaReflection` | confirmed |
| `rrr` | as `rr` + `ReflectionPlaneMask` | confirmed |
| `ll` | scanline hologram: `scanline_Color1`, `scanline_Size`, `scanline_Speed` | confirmed |
| `lwl` | scanline + wireframe + distance fade: `scanline_Wireframe*`, `scanline_Fresnel`, `scanline_DistanceFadeIn*` | confirmed |
| `ha` | adds the `Silhouette` technique | confirmed |
| `eo` | nothing | unresolved |

#### Alpha segment

```
alpha := "a" { "r2" | "ae" | "ac" | "ag" | "e" | "d" | "p" | "w" } [ "b" ] ( "b" | "k" )
```

| Part | Meaning |
|---|---|
| terminator `b` | alpha blend (`AlphaBlendEnable`, SrcAlpha/InvSrcAlpha) |
| terminator `k` | alpha test / kill (`AlphaRef`) |
| extra `b` before the terminator | black-point alpha map `alphaBlackPointMap`, `g_AlphaBlackPointScale/Opacity` |
| `r2` | render in the half-resolution translucent buffer: `depthViewHalfBuffer`, premultiplied `One` / `InvSrcAlpha`, ZWrite off |
| `ae` | view-fresnel alpha: `g_AlphaFresnel` + `g_AlphaViewScale` |
| `ac` | fresnel alpha without view scale: `g_AlphaFresnel` |
| `ag` | angle-lookup alpha: `alphaAngleMap`, `g_AlphaLookupScale` |
| `e` | soft edge (soft particle): `g_AlphaEdgeScale` + `depthViewBuffer`; `alpha *= 1-(1-sat((sceneDepth*g_CameraParams.y + viewZ)*g_AlphaEdgeScale))^2` |
| `d` | distance fade-in: `g_AlphaDistanceFadeInStart/End` |
| `p` | depth bias |
| `w` | sine-wave vertex alpha: `alphaSin{Size,Speed,Frequency}{X,Y,Z}` + `ColorConstraint` stream |

All 23 observed codes parse with this grammar: `ab`, `ak`, `abb`, `aeb`, `aebb`, `aaeb`, `aaebb`,
`aaeeb`, `aaeebb`, `aacb`, `aaceb`, `aaedb`, `aaedeb`, `adb`, `adeb`, `aageb`, `apb`, `apk`, `aaepb`,
`awb`, `ar2b`, `ar2bb`, `ar2eb`, `ar2acb`, `ar2aeeb`, `ar2aedeb`, `ar2pb`.

In world materials, `RenderCaps` bit 0x80 accompanies `b` (97%) and bit 0x100 accompanies `k`
(100%); bit 0x40 is the opaque pass.

#### Baked lighting and skinning

| Suffix | Meaning |
|---|---|
| `_lmh` | lightmap `lightMap1` on `lightmapUV`, RGBM-decoded `(rgb·a·6)²` (`DecodeRGBMTex`); replaces SH ambient (`objectSH` disappears) |
| `_lm` | lightmap without RGBM decode |
| `_vb` | `VertexBaked` stream (`bakedColor`); replaces SH ambient |
| `_skin` | `g_BoneTransforms`, `g_BoneCount`, `BlendWeights`/`BoneIndices`; adds `Depth`/`DepthView` techniques on alpha-tested effects |

### 5.3 `m2` / `m3` / `m4` — multi-layer terrain blend

```
name := "m" N { "_" layer }×N "_" shared [ "_" baked ] [ "_skin" ]
```

* **Layers:** each layer is a §5.2 layer descriptor restricted to `d…`, `n`, `s(m|c)`, `e…`.
* **Shared block:** uses the order above and almost always ends in `m`.
* **Blending:** layers mix by the `ColorBlend` vertex stream with height splits `split<k>_scale`
  (`blendAmount = min(height, ColorBlend.z)` style).

### 5.4 `glass`

```
name := "glass_" descriptor "_" alpha [ "_" baked ] [ "_skin" ]
```

* **Descriptor:** the §5.2 slot grammar, but without the `m` terminator, and diffuse may be absent
  (`glass_scqdrucemc`).
* **Alpha:** always blended (`ab`, sometimes `aacb`).
* **Shading:** glass has its own environment and lighting path (`combinedColor`), and no ZWrite
  parameter.

### 5.5 `skyd`

```
name := "skyd_" [ diffuse ] [ "w" ] [ "v" ] "uc" [ "lc" ] [ "f" ] [ "_" alpha ]
```

* **`w`:** world-anchored. Without `w` the VS adds `g_ViewInverse`'s translation so the dome follows
  the camera; with `w` it doesn't (*confirmed*).
* **Lighting:** none.
* **`f`:** `g_FogBlend`, i.e. lerp towards the fog cube by a fixed amount.

### 5.6 `part` — particles

```
name := "part_" descriptor "_" alpha "_r"
```

* **Techniques and interface:** technique `Shader`; texture usage `Base`; always `textureEdgeU/V`.
* **Descriptor lexicon** (greedy):

| Code | Meaning |
|---|---|
| `dmm`, `dum`, `dlm`, `dc` | diffuse (as §5.2) |
| `8` | `screenEffect` (*likely*) |
| `rl` | refraction with blend: `RefractMap`, `colorBuffer`, `refractionScaleX/Y`, `refractionBlend` |
| `rm` | refraction without blend |
| `sm`, `sc` | specular |
| `ii` | `ambientIntensityModifer` (sic) |
| `i` | `lightIntensityModifer` (sic) |
| `s` | `softnessOverride` |
| `o` | constant shadow-receive term (`shadowAmount` local) |
| `r` | after `o`: `modShadowRange` |
| `p` | `sphericalBlend` (spherical normals) |
| `x` | unlit |
| `a` | in `iia` / `xax`. **unresolved**; `xax` replaces output alpha with a constant |
| `uc` | scroll |
| `lm`, `lc`, `lmc` | emissive |

### 5.7 `math<op>` — framebuffer blend effects

```
name := "math" op "_" descriptor "_r" [ "pt" ] "_r" [ "0z" | "eo" ] "_" alpha [ "_skin" ]
```

* **`op`:**
  * `add` = additive (DestBlend `ONE`);
  * `mul` = multiply (SrcBlend `ZERO`, DestBlend `SRCCOLOR`);
  * `scrn` = screen (SrcBlend `INVDESTCOLOR`);
  * `lnrlt` = linear light (*likely*; both shipping effects are half-res).
* **Descriptor lexicon:** `dbcm`, `dblm`, `dusm`, `dmm`, `dc`, `v`, `lmc`, `lm`, `lc`, `l` (emissive
  from vertex colour, *likely*), `urc`, `uc`.
* **`pt`:** projected texture: `projectedFov`, `projectedTextureWidth/Height`, `ProjectedMap`,
  `ProjectedTexture` technique.

### 5.8 `distrx` / `distsn` — screen distortion

```
name := "dist" ("rx" | "sn") "_" ["n"] "uc" "_r" ["v" | "va"] ["qdr"] "_" alpha ["_skin"]
```

* **`rx`:** refraction (`refractionRatio`, `refractionFogMin`, `colorBuffer`).
* **`sn`:** sine wobble (`distortionSinSizeH/V`, `distortionSinSpeed`).
* **`n`:** a normal map drives the offset.

### 5.9 Water

```
name := "wtr" [c | w | wa | was] "_" ["qdr" | "qscr"] ["v"] ("emc" | "eic") "_r" ["r" | "f"] ["_" baked]
```

* **Variants:**
  * `wtr` = refractive (`colorBuffer`, `refractionFog*`, `diffuseLightInfluence`);
  * `wtrc` = cheap: `diffuseColorA1`, no refraction;
  * `wtrw` = + foam (`foamMap`, `foamUV`) and waves (`waveHeight*`);
  * `wtrwa` = + crest colours (*likely*);
  * `wtrwas` = unresolved.
* **Normal maps:** all water uses two scrolling normal maps (`split1/2_scale/scrollU/V`).
* **Render segment:** `rr` = planar reflection; `rf` = `g_FogBlend`.

### 5.10 Car eyes

```
name := "car_" ("eyes" | "double_eyes" | "cheap_eyes") "_" descriptor ["_" baked] ["_skin"]
```

* **Kinds:**
  * `eyes`: one eye pair (`eyeBallOffset/Spacing`, lid curves);
  * `double_eyes`: left/right `eyeBall_*_offset`;
  * `cheap_eyes`: diffuse map + UV slide only.
* **Descriptor lexicon:**
  * `dtmm` / `dtc` = tertiary-colour diffuse with map / with colour;
  * `dm` = map without colour multiply;
  * `dmm`, `dc`, `sc`, `sm`, `qdr`, `qc`;
  * `eisc` / `eism` / `emsm` / `eic` = environment (§5.2 E grammar).
* **Rendered eye texture:** the engine renders eyes into `renderedEyeTexture` with `eyeball`,
  `eyelids` and `eyelidstextured`.

### 5.11 Shadow mask resolve

```
name := "shadowmask" ["2"] "_c" N ["_" kernel] ["_nodb"] ["_ss" | "_ss2"] ["_cs"] ["_sm"]
```

| Part | Meaning |
|---|---|
| `2` | half resolution (`depthViewHalfBuffer`) |
| `cN` | cascade count (*likely*) |
| `2x2` / `4x4` | PCF kernel |
| `rot8` | 8-tap rotated kernel (`rotateMap`) |
| `ss` | second shadow map (`ShadowTexture1`, `ShadowMatrix1`) |
| `cs` | cloud shadows (`CloudTexture1`) |
| `nodb`, `ss2`, `sm` | unresolved: no interface difference, and `nodb` is code-identical in the `Default` technique |

`shadowmaskchar` is the character contact shadow (`GroundPosition`/`GroundNormal`, `ShadowFade`).

## 6. Parsing algorithm (reference)

1. Lower-case the name and split it on `_`.
2. Dispatch on the family (§5.1).
3. For `a_N`:
   1. Read N layer segments and, if N > 1, one shared segment.
   2. Match each layer with the anchored regex
      `^(D)?(X)?(N)?(S)?(K)?(R)?(Q1)?(Q2)?(V)?(U)?(E)?(M)?(MOD)?(L)?(F)?(P)?(Z)?$`, using the slot
      regexes from §5.2, in that order, each optional. The match is unique for every shipped name.
   3. Classify the remaining segments in order: `r…` (render) only before the alpha segment; then
      `a…(b|k)` (alpha); then `lm|lmh|vb`; then `skin`.
4. For `mN`: N layer segments, then the shared segment, then `baked` and `skin`.
5. For the other families, use the greedy lexicons in `fxlex.py` (longest match first).
6. Always also read the `.tfx` interface (§2). A name can be authored with a fragment that compiles
   to nothing, but the interface never lies. The tag CSV comes from the data for this reason.

## 7. Fragments that do not change the shader

These produce byte-identical `.tfx` files, so never infer shader differences from them:

* `qc` (422 effects);
* a standalone render `r`;
* render `eo`;
* math `_r`;
* `dum` versus `dmm` (identical code).

For `qc` and `eo` the effect sorts the same, and no `RenderCaps` bit correlates with `qc` in 3,575
world materials. They were most likely read by the Maya exporter or tools, for example to put the
mesh in the shadow-caster list. When recovering the generator, keep them as accepted-but-ignored
tokens.

## 8. Engine behaviour the names do not show

These are confirmed in `sdaemon.exe` and needed to reproduce the rendered result. They don't affect
bytecode matching.

* **Gamma parameters:** a float3/float4 parameter with `Gamma=1` in the `.tfx` is converted
  sRGB→linear when a material loads (material set-up, 0x841488; exact
  `x <= 0.04045 ? x/12.92 : ((x+0.055)/1.055)^2.4`). Effect defaults are not converted. The column
  `gamma_parameters` lists them per effect (`diffuseColor*`, `specularColor*`, `emissiveColor`,
  `g_rimLightColor`, `refractionFogColor`, `subsurface*`, `scanline_Color1`, `waterCrestColor*`,
  `silhouetteColor`, `g_Color`…). The shared effect (`startup.zip:effects/shared.tfx`) also flags
  `g_sunLightPrimary/Tertiary`, `g_LightMapSpecularColor`, `g_ShadowBlobColor` and `g_Bloom_Tint`.
* **sRGB textures:** sRGB sampling is per texture, not per effect. A DDS whose `dwCaps3` (offset
  0x74) is 1 is sampled with `D3DSAMP_SRGBTEXTURE` (when the material's textures are bound).
* **Post-process:** output is HDR linear. The post-process applies
  `Hejl(c·g_HDR_Exposure/avgLum − 0.004)`; see `startup3.zip:effects/postprocess.tfx`.

## 9. Notes for matching decompilation and generator recovery

* **One template, many switches.** The names are a serialisation of a feature set. Effects that
  share a token set share code, and every slot adds a recognisable block with stable local names:
  * diffuse: `colorDiffuse`;
  * lighting: `lightingDiffuse`, `dotSunLight`, `sunLightBlendAmount`, `colorSunLight`,
    `CalculateSHColor3::*`;
  * shadows: `UvScreen`, `shadowAmountDynamic`, `shadowAmount`;
  * specular: `halfAngleSunLight`, `dotSpecularSunLight`, `fresnelRaised2/4/5`,
    `specularNormalization`, `lightingSpecular`, `combinedSpecular`;
  * environment: `viewReflection`, `colorCube`, `environmentSchlick`, `environmentReflectedAmount`;
  * rim: `dotRim`;
  * fog: `viewDirection`, `viewDistance`, `colorFog`, `fogAmount`;
  * alpha: `viewFresnelAlpha`, `depthModifier`, `alphaModifierDistance`, `finalAlpha`;
  * output: `combinedDiffuse`, `combinedColor`, `OUT`.

  Recover each block by diffing minimal pairs (`tools/fxdiff.py`, `tools/fxps.py`,
  `tools/fxtree.py`), then rebuild the generator as one emission rule per slot.
* **Entry points:** `Standard_VS` / `Standard_PS`; the `Reflect` technique has its own pair.
  Particles use `Shader`.
* **Recompiling:** use the D3DX9-era compiler (the June 2010 DirectX SDK `fxc` / `D3DX9_43`). The
  CTAB creator string has no version, which is typical of D3DX9 `D3DXCompileShader`. Pass flags
  equivalent to `/Zi` and no preshader, plus `register()` bindings for the engine globals at the
  registers in §3. Leave material parameters unbound; they get c92+.
* **Comparing bytecode:** compare instruction tokens with the `DBUG` and CTAB comment blocks stripped
  first, since path strings differ. Then compare CTAB register assignments. `#line` numbers in the
  original `DBUG` tell you the line layout the generator emitted, so a perfect match of a generated
  `.fx` reproduces them too.
* **Order of operations matters for matching.** Observed patterns:
  * fog is applied last as `colour + fogAmount·(colorFog − colour)`, i.e. a single `mad` into
    `oC0.xyz`;
  * `max(lightingDiffuse, 0)` comes before the ambient-buffer add;
  * the RGBM decode is `(rgb·a·6)²`, emitted as `mul`, `mul`, then a self-multiply.

  The `#line` numbers in `DBUG` give the source order of every instruction; follow them.
* **Vertex streams:** the `Annotation` of each technique fixes which `TEXCOORDn` each stream uses, and
  it changes with the feature set (e.g. `Uv1 = TEXCOORD2` once Tangent/Binormal take TEXCOORD0/1).
  The exact assignment rule wasn't derived; `vertex_streams` in the tag CSV lists it for every
  effect, so it can be fitted from the data.

## 10. Open questions

| Fragment | Status |
|---|---|
| `m` (a/mN terminator) | present in all a/mN descriptors; no effect lacks it, so it can't be isolated. It doesn't track `MaxAnisotropy` or the split scales. Possibly "material lighting model". |
| `qc` | no shader effect; most likely the shadow-cast flag for the exporter. |
| render `eo`, math `eo` | no effect. |
| `dum` versus `dmm` | identical code; authoring difference unknown. |
| particle `a` (in `iia`, `xax`) | `xax` forces the output alpha to a constant; `a` alone not isolated. |
| shadowmask `nodb`, `ss2`, `sm` | no interface difference in the `Default` technique. |
| `wtrwas` | single effect; difference from `wtrwa` not isolated. |
| CTAB flag 0x20000000 | not a public D3DX flag. |

## Reproducing

The scripts expect the game at `<your Cars 2 Arcade folder>` and the
add-on's `core/` modules (`c2oct.py` for Tupperware, `d3dbc.py` for SM3 decoding) at the `CORE` path
inside `fxcorpus.py`. Windows' `d3dcompiler_47.dll` provides `D3DDisassemble`.

| Script | Does |
|---|---|
| `fxcorpus.py` | scans every `.tfx` (loose and in zips) → `fx_corpus.json` |
| `fxfeat.py` | feature sets per effect → `fx_features.json`; also prints minimal-pair chunk statistics |
| `fxname.py`, `fxlex.py` | tokenizers (§5) |
| `fxsig.py` | evidence signatures per fragment |
| `build_fxdocs.py <outdir>` | writes both CSVs |
| `fxps.py [vs\|ps] name[:Technique]` | disassembly of one effect |
| `fxdiff.py a:b` | feature diff of two effects |
| `fxtree.py a b` | `.tfx` tree diff ignoring shader bytecode |
| `exe_xref.py <string>` | finds and disassembles code in `sdaemon.exe` that references a string (capstone) |
