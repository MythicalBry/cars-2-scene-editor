# Cars 2 Scene Editor (Blender add-on)

Edit Cars 2: The Video Game worlds (`assets/worlds/<world>/`) in Blender: the scene tree, geometry,
materials and textures, lights, light rigs and probes, baked lighting (lightmaps and vertex colours),
collision, Meridian choreographies, triggers and locators, and actor properties (`.ap`). It reads and
writes the world's files directly, so editing doesn't require offsetting, but repacking the world zips for the game will still require offsetting.

The add-on detects which version of the game a world is from:

* **PC / Steam** ("Win32_Wii"): baked light in `ColorPrelight` vertex colours. **Currently untested.** It is
  unclear whether this version is supported: work on it was dropped very early on in favour of Arcade.
* **Arcade** (the Xbox 360 developer build that runs on Windows, "Win32"): lightmaps, `VertexBaked`
  colours, multi-layer texture blending and volume light probes. See [Arcade worlds](#arcade-worlds).

The Scene panel shows which version was detected.

## Install

Edit > Preferences > Add-ons > Install from Disk > `cars2_scene_editor.zip`, then enable **Cars 2 Scene Editor**.
The tools are in **3D View > Sidebar (N) > Cars 2 Scene**.

**Blender versions.** The earliest version it is tested to work on is **Blender 4.5.9 LTS** (also tested on 5.x).
Blender 4.1 was tested and does **not** work. Other versions older than 4.5 have not been tested.

**Characters in previews.** To preview cutscenes or choreographies that contain skinned characters with the best
results, you may need to install the **Cars 2 Toolkit** add-on alongside the Scene Editor (see
[Previewing choreographies](#previewing-choreographies)).

## Workflow

1. **File > Import > Cars 2 Scene (.oct)**: pick `worlds/<world>/<world>.oct`. The add-on also reads the
   `_0.vbuf`, `_0.ibuf`, `.btm`, `.btd`, `.mer` and `.ap` files next to it. It writes the textures to `<world>.oct.textures/`
   next to the `.oct`, using offsetting's layout, and leaves files that are already there alone.
2. Edit.
3. **Export Scene** (Scene panel, or File > Export > Cars 2 Scene). This writes the complete world to the
   export folder: the seven files it edits plus the unchanged ones (`.dbl`, `.mtc`), copied from the source.
   Copy all of them over the originals. For Arcade, zip the folder's world files (no subfolders needed) as
   `<world>.zip`. The `.dbl` is required: a world without it never finishes loading. The `.mtc` is unused by
   the game but is copied anyway so the zip matches the original.

The export re-reads the original files and patches them. Anything the add-on does not model
(subnetworks, AI tracks, nav meshes, sound sources...) passes through unchanged. If you change nothing,
all seven files come out **byte-identical**. This was verified on air, frontend, holocars_skidpad,
mi_tokyo, radiator_a and tutorial (PC), and on tutorial, mi_tokyo, radiator_a and radiator_b (Arcade).
Save your `.blend` as normal. The source `.oct` must stay where it was imported from.

Arcade worlds ship inside `assets/worlds/<world>.zip`. Unzip one into its own folder first and import
the `.oct` from there.

### Coordinates

Everything hangs under the `C2_<world>` root empty. The root turns the game's Y-up into Blender's Z-up, so
**an object's Location/Rotation/Scale in the N panel are the game's own local values**. Move, rotate,
scale and re-parent objects as normal. Each node's `LocalToParentMatrix` is written back. Nodes you didn't
move keep their exact original floats.

## What you can edit

| Area | How |
|---|---|
| **Nodes** | Transforms, Geometry, Lights and Cameras are objects with the game hierarchy. Moving a Meridian-bound node also updates its mirror in the scene SubNetwork (`<name>/Transform`). |
| **Meshes** | Each Geometry node is one mesh object. Its material slots are its SubGeometry parts. Edit freely. Edited meshes are re-encoded with each part's original vertex layout. Untouched meshes are copied byte-for-byte. Parts with more than 65535 vertices are split automatically. |
| **New objects** | Select meshes, empties, lights or cameras and press **Add Selected**. They become new nodes on export and are linked into the visibility cell. New meshes use the world's most common layout (Position+Uv1 / Normal / ColorPrelight). |
| **Deleting** | Delete objects as normal. Deleted Geometry is hidden and emptied, and deleted lights are set to intensity 0, so no node index anywhere shifts. |
| **Materials** | The Material panel edits the effect, RenderCaps, every shader property and each texture slot. On Arcade worlds every change shows live in Game Look; see [Editing materials](#editing-materials-arcade). A plain Blender material with an Image Texture becomes a new game material, cloned from a world material (**Make Cars 2 Material** picks which one), and the image becomes a new texture. |
| **Textures** | Material panel: **Pick** another world texture for a slot or **Load Image** as a new texture. Or replace a texture everywhere: texture list > **Replace With**. A `.dds` file is copied as-is. Any other image is written as uncompressed DDS with mips. |
| **Scene settings** | Lighting > Scene Settings: sun colour and intensity, light boost, rim light, shadow blob, fog start/end/colour and bloom (the `scene_settings` material). |
| **Lights** | Directional (Sun), Spot, Point (Arcade) and Ambient (shown as a point light) lights, with colour, intensity, shadow colour, cone/penumbra/dropoff/decay, exclusion type and the static key/values (falloff, roaming, lightmap usage...). |
| **Light sets** | The dynamic lights a mesh receives. Build a set from selected lights, then assign it to meshes. |
| **Light rig (SH)** | The 27 spherical-harmonic coefficients (9 per colour channel) of each `LightRigPool` rig, its lights and its position. **Project SH From Rig Lights** projects the rig's directional and ambient lights with the standard real spherical-harmonic formulas (Sloan, *Stupid Spherical Harmonics (SH) Tricks*, GDC 2008). |
| **Light probes** | Environment probes (cube-map reflections) and Volume probes: position, density, fade and cube-map texture. A probe hangs under its scene node and moves with it. A Volume probe is the box of its (invisible) node, so its object scale is the box's half size. **Bake Probe Cube Map** renders the six faces from the probe and writes a DDS cube. Arcade volume probes also hold a lighting grid; see [Arcade worlds](#arcade-worlds). |
| **Baked lighting** | PC: the `ColorPrelight` vertex colours. Arcade: lightmaps or `VertexBaked` colours. Lighting > Baked Lighting has the bake, fill and paint tools for both. |
| **Viewport** | Scene panel > **Viewport**: *Game Look* (Arcade: the game's own shaders, see [Cars 2 Game render engine](#cars-2-game-render-engine-arcade); PC: textures x baked light, unlit), *Baked x Blender Lights* (textures x baked light, Blender lights on top) or *Textures Only*. Use Material Preview or Rendered shading. |
| **Collision** | See below. |
| **Choreographies** | Meridian panel: file, flags (loop, visible before/after, hide gameplay elements...), anchors and the triggers that play each one. |
| **Triggers** | Select a walk-in trigger, then **Play Choreography On Trigger**. Type the choreography name (`ExplosionFireyB`, or the full `choreographies/X/X` path), pick enter or exit, and choose where it plays (new locator at the trigger, or another selected locator). The graph is wired on export. |
| **Locators** | **Add Meridian Locator** creates a Transform node, its `MeridianSceneNodeRefs` entry, its `Transform` association, its scene-SubNetwork entry and the Meridian `LocatorNode`. Use it as a choreography anchor. Duplicating a locator also makes a new one. |
| **Actors (.ap)** | Actor Properties panel: every key/value of the selected object's `.ap` section. This covers bashables/breakables (`MExploder`: piece names, velocities, respawn...), triggers, pickups and spawners. |

### Baked lighting on PC

PC worlds keep their baked lighting in the `ColorPrelight` vertex colours; there are no lightmap textures.
**Bake Vertex Lighting** Cycles-bakes your Blender lighting into it (light x **Prelight Scale**; shipping
prelight averages about 0.5). **Fill Vertex Lighting** sets, multiplies or adds light. You can also paint
`ColorPrelight` in Vertex Paint mode. `ColorBlend`/`ColorOcclusion` layers round-trip too.

### Arcade worlds

Everything here was read from the Arcade build itself: its compiled shaders (`assets/effects/effects/*.tfx`,
which carry debug info) and `sdaemon.exe` (Ghidra). Set **Game Assets Folder** (Meridian or Baked Lighting
panel) if the world wasn't imported from inside the game's `assets` folder; the shader checks and the
lighting-mode switch need the effects folder.

**Baked lighting.** Every lit part of an Arcade world uses exactly one of:

* a **lightmap**: its effect name ends in `_lmh`, the mesh has a `lightmapUV` UV map, and the part has its
  own `lightMap1` texture. The texture is RGBM: light = (rgb x alpha x 6)².
* **vertex colours**: its effect name ends in `_vb`, and the mesh has `VertexBaked` colours. These are RGBM
  too: light = (rgb x alpha x 6)^2.2, so alpha is a brightness multiplier.

`ColorOcclusion` darkens either one. Parts with neither are lit while the game runs (light rig, volume
probes, sun).

The lightmaps are part of the materials. A game material used with several lightmaps becomes one Blender
material per lightmap, named `material|lightmap` (a "lightmap variant"). They all export as the same game
material. Edit the game properties on the base material; the Material panel shows them for a variant.
The variant's **Lightmap** field picks which texture its parts use.

Tools (Lighting > Baked Lighting):

* **Bake Lightmaps**: Cycles-bakes the Blender lighting (direct + indirect, no albedo) of the selected
  meshes into their lightmaps through `lightmapUV`. Shared lightmap atlases are re-baked in place, so only
  the selected meshes' islands change. Lightmapped parts with no lightmap yet get a new one (choose its
  size), and meshes without `lightmapUV` get one from Lightmap Pack. Results are written as uncompressed
  DDS to `<world>.oct.textures/baked_lightmaps/` and go into the `.oct` on export.
* **Bake Vertex Lighting**: the same, into `VertexBaked`. It warns about meshes whose material isn't `_vb`,
  because the game would ignore their colours.
* **Fill Vertex Lighting**: set, multiply or add light (optionally only on vertices selected in Edit Mode).
* **To Lightmaps / To Vertex Colours**: switches the selected meshes' materials between the `_lmh` and
  `_vb` versions of their shader. It only does this when the game has that shader. New game materials are
  made on export. Bake afterwards.

**Texture blending.** `m2_`/`m3_`/`m4_` materials blend up to four diffuse layers. Layer k uses
`Uv1 x split<k>_scale`. Layers 2/3/4 are weighted by the blue/green/red channel of the `ColorBlend` vertex
colour, limited by the layer's alpha. `a_2_` materials put layer 2 on its own `Uv2` map. Paint `ColorBlend`
in Vertex Paint mode to blend.

**Volume light probes.** A volume probe's `DirectSH` is a lighting grid: Density x by y by z cells of 27
floats (order-3 SH of R, G and B, radiance). Cars passing through the box pick it up. **Bake Volume Probe
Lighting** renders a small Cycles panorama of the scene's Game Look at every cell and projects it.
**Fill / Scale Probe Lighting** scales the grid or fills it from the light rig. Changing Density resamples
the grid on export. A grid of 8 x 8 x 8 cells is 512 renders, so try a small panorama width first.

**Meshes.** Arcade meshes have more vertex streams than PC ones: tangent frames, `lightmapUV`, `Uv2`,
`UvMod`, and colour layers. Untouched meshes keep their original bytes. An edited or new part is written
with exactly the streams its material's shader binds, read from the effect file. Tangent frames are
rebuilt from the UV maps; Blender's MikkTSpace frames differ slightly from the original Maya ones on some
skewed UV layouts. A new mesh needs a `lightmapUV` map if its material is lightmapped (Bake Lightmaps
makes one). Until it's baked, it exports with a neutral lightmap and a warning. **Check Scene** lists
materials whose shader the game doesn't have and parts missing their baked lighting.

**Helpers.** Parts drawn with `SolidColor` (it has no world-pass technique) or `shadowonly` (shadow-caster
proxies) are shown as wireframe and left out of renders and bakes, like other invisible geometry.

### Cars 2 Game render engine (Arcade)

**You need the game itself.** No shaders ship with the add-on: each one is built on your PC from your
own copy of the game's effects (`assets/effects/effects/*.tfx`) the first time a world is drawn. Import
worlds from inside the game's `assets/` folder, or set **Game Assets Folder** in the Scene panel (it shows
a warning while the effects can't be found); the world then switches to the Cars 2 Game engine.

Arcade worlds open with **Cars 2 Game** as the render engine (Render Properties > Render Engine). In
**Rendered** viewport shading, and for F12 renders and animations, it draws the world with the game's own
shaders: each effect's shipped Direct3D 9 vertex and pixel shader is converted instruction by instruction
into GLSL (`core/fxglsl.py`) and run on the GPU by `gamerender.py`. Nothing is approximated with nodes, so
what you see is what the game's shaders compute.

* Fast: one small shader per effect (Radiator Springs: 87), compiled in about a second the first time the
  world is drawn. Import no longer translates shaders into nodes (Radiator Springs: about 4 s instead of 24 s).
  Orbiting the view costs a few milliseconds per frame.
* Everything stays live: material values, texture swaps, scene_settings (fog, sun, exposure...), the sun
  light, the light rig and the timeline (scrolling UVs, flipbook textures) update the next frame without
  recompiling anything.
* Blender's overlays (outlines, gizmos, the grid, helper objects) draw on top and sort against the world.
* Material Preview still uses Eevee with the simplified materials; Solid mode is Blender's own.
* F12 renders give the same image as the viewport. Keep the view transform on Standard (Game Look sets it):
  the game's tone curve is already applied.
* **Eevee Game Shaders** (Scene panel, under Viewport) also builds the node translation described below,
  for Material Preview / Eevee renders. It is off by default because it is slow to import and to draw.
* Meshes without a game material are drawn with a simple textured, sun-shaded stand-in.
* **Shadows** (Scene panel: Shadows, Distance, High-Res) work like the game's (as `sdaemon.exe` draws them):
  * two shadow maps of the parts whose SubGeometry `RenderCaps` carry the caster bits: *dynamic* (0x40000,
    1024 px, 2 cascades) and *static* (0x4000, 256 px, out to 150). In the shipped worlds the casters are
    the invisible `shadowonly` proxies plus a few props; previewed cars and characters cast dynamic shadows.
    The rest of the world casts nothing, because its shadows are baked. The `shadowonly` proxies (and the
    other helper meshes) cast whether shown or not, as in the game; anything else casts only while it is
    shown, so map objects the Track / Mode picker hides, hidden templates and hidden collections cast nothing.
    Map-object copies (pickups, bashables, breakables, moving actors) cast like the parts they copy;
  * a playing preview stays light: a material whose values are keyed every frame (the characters' eyes)
    rebuilds only the draws of the objects using it, and a skinned mesh bent by its rig gets only new
    positions and normals, not a full re-read (its tangent frames stay as first built);
  * each cascade draws only the casters whose bounds reach it, and the maps are kept between frames while
    the view and the casters don't change (playing the timeline with a still view redraws no shadow map);
  * the game's own `shadowmask_c2_rot8_nodb_ss_cs` pass (8-tap rotated filter, scrolling cloud shadows from
    scene_settings) runs on a depth pre-pass, and every surface shader reads the result: baked surfaces
    darken towards `g_ShadowBlobColor`, run-time lit ones (cars, characters) lose their sun light;
  * *Distance* is the game's `shadowDistance` (how far the cascades reach; the game takes it from its config,
    60 is a guess); *High-Res* is its `-hiresshadows` mode (2048 px, 3 cascades).
* Previewed characters and cars use their package's own effects and textures.
* The frame ends with the game's own `postprocess` effect (exposure, Hejl tone curve, gamma, colour-grade
  LUT), and missing engine globals take the game's defaults from `shared`. Both are engine effects, read
  from your install's `assets/startup*.zip` like everything else.
* **Bloom** (on by default) is the game's chain (`_submit_bloom` in `sdaemon.exe`): `highpass` at 1/4 size
  with the scene_settings bloom curve (`g_Bloom_Threshold`, `_Intensity_Below/_Above`), Gaussian downsamples
  to 1/16 and 1/64 (`downsamplegauss`), 9-tap separable blurs (`glowgauss`) and accumulation back up
  (`upsampleaccum`), with the engine's own tap offsets and weights. `postprocess` adds it scaled by
  `g_Bloom_Multiplier` and tinted by `g_Bloom_Tint`. Costs well under a millisecond.
* **Reflections** (on by default) are the game's planar reflection (`R_Reflection_Plane`): the first part whose
  material has `g_AlphaReflection` > 0 sets a horizontal plane at its height; a mirror camera (clipped at
  the plane, out to `g_ReflectionFogEnd`) draws the parts whose SubGeometry `RenderCaps` carry 0x200000 with
  their effects' Reflect technique, then `glowgauss` BlurH5/V5 softens it. Glossy floors (frontend,
  tutorial) show the world; water in worlds without reflection casters (Radiator Springs, Italy) shows only
  previewed cars, as in the game.
* Transparent parts draw in the game's order: by the part's draw layer (`RenderCaps & 0x3F`, part of the
  engine's sort key), then back to front. Foam strips are layer 2 and the open sea layer 0, so the foam lies
  over the water as in the game.
* **Refraction**: water reads a copy of the opaque frame (`colorBuffer`) and the view depth, exactly like the
  game, so you see the scene through it, bent, and tinted by depth.
* **Motion Blur** (on by default) is the game's (`_submit_motion_blur`): `motionblurprepare` at half size, then
  `motionblur` blurs every pixel along its screen velocity, from the camera's move since the last frame and,
  for moving previews (cars, characters), the object's own move (its motion id, the game's stencil value),
  so a car the camera follows stays sharp. Strength: `g_MotionBlurScale`, cap: `g_MotionBlurMaxVelocity`.
  It shows when the timeline advances one frame (playback, scrubbing, animation renders); orbiting the view
  or a single F12 still is never blurred.
* **Auto Exposure** (on by default) exposes each view the way the game's auto-exposure settles on it: the
  average log luminance of the frame (`downsample4x4_2t` / `avglum`), clamped to the world's
  `g_HDR_Min/Max_Luminance`. Turn it off to use Scene Brightness; the eyedropper copies the measured value.
* Only what is in view is drawn (bounding-sphere culling); in the viewport, parts smaller than a pixel on screen
  are left out too (F12 renders draw everything). When a preview plays only the moving objects are refreshed. A
  deforming character is re-read every frame (Mater: about 30 ms).
* Undo / redo, the Track / Mode / Lap pickers and showing or hiding objects keep everything that didn't change:
  meshes are only read again when their data really changed. See `docs/BlenderPerformance.md` for the numbers.
* **Film > Transparent** (Render Properties) renders the world with a transparent background where nothing was
  drawn.
* Opaque parts draw in the game's order too: by draw layer, then in the order each material first appears in
  the world's node list (the engine's pass slots). Where depth can't decide, as with sky domes that all sit at
  the far plane, this picks the same winner as the game: oilrig's noise dome (`star_noise1`, from oilrig_b) is
  covered by `outer_dome`, as in the game.
* The far clip is pushed out to the world's `SceneFarPlane`, so distant sea and skies aren't cut off by the
  viewport's Clip End. Overlays still use Blender's own clip range.
* Cube maps (environment reflections, fog colour) are sampled from their full six faces. Earlier versions
  only had one face on the GPU, so reflections and fog colour could be wrong from some directions.
  Blender doesn't allow loading images during a viewport draw, so each cube map loads just after the first
  Rendered frame: fog and reflections fill in a moment later.
* **Ambient light volumes** fill the ambient buffer (`samAmbientBuffer`), which every world shader adds to
  its diffuse lighting. This is how fireworks, explosions, muzzle flashes, mines, pickups and gates light
  the world around them. See *Ambient light volumes* below.

### Game Look in Eevee: the game's shaders as nodes (Arcade)

With **Eevee Game Shaders** on, *Game Look* also runs the game's actual shaders in Eevee. The add-on reads each material's
effect (`assets/effects/effects/<effect>.tfx`), decodes the compiled Direct3D 9 vertex and pixel shaders, and
translates their instructions into Blender shader nodes. Specular highlights, cube-map reflections and their
masks, glow maps, rim light, texture blending, lightmaps, vertex lighting, fog, alpha blending, additive glow
and cut-outs all use the game's own maths. The colour then goes through the game's tone curve (Hejl filmic
with the world's HDR exposure), so the viewport shows what the game would draw.

Colour space follows the engine (`sdaemon.exe`):
* A texture is decoded from sRGB when its DDS header says so (the `dwCaps3` field = 1). Colour, specular,
  emissive maps and the fog cube have the flag; normal maps, lightmaps and reflection cube maps don't.
  The world texture list shows it as **sRGB Colour**, and export writes it into the DDS. Replacements and
  new textures get the right value automatically: a replacement keeps the original's, and **Load Image**
  sets it from the slot.
* Colour parameters the effect marks `Gamma` (`diffuseColor`, `specularColor`, `emissiveColor`, rim
  light, the scene_settings sun and shadow colours...) are stored sRGB-encoded and converted to linear
  when the world loads. Their colour pickers show the stored value.

* Arcade worlds open in Game Look. Both renderers need the game's effects: set **Game Assets Folder** if the world
  wasn't imported from inside `assets/`, then **Build Eevee Game Shaders** (Eevee only).
* Use Material Preview (or Rendered) shading. The first view of a world compiles every material's shader;
  Blender shows "Compiling shaders" while it does. The very first time on a PC this takes a minute or two
  (Radiator Springs: about 2 minutes); after that the graphics driver has the shaders cached and opening the
  world takes about 15 s.
* Afterwards everything is live and costs under a second: sun, exposure, Inspect, timeline/playback,
  material values, texture swaps, effect swaps and choreography previews. The engine values (sun, fog,
  exposure, time...) are stored in a small float image (`C2A_EngineValues`) that every shader reads, so
  changing them never regenerates a shader. A car or character shown for the first time compiles its own
  shaders once.
* **Scene Brightness** is the exposure. The game adapts it to the average brightness of what's on screen,
  clamped to the world's `g_HDR_Min/Max_Luminance`. The eyedropper (**Auto Exposure**) measures the current
  view the way the game does. Without a measurement it uses the world's minimum, where the game usually
  settles.
* Game Look switches the view transform to Standard (the game's tone curve is already applied) and raises
  the viewport clip end, because sky domes sit thousands of units away.
* The sun (`sunLight`), its colour and strength (scene_settings), fog, the light rig and the lightmap
  settings feed the shaders live. Rotate `sunLight` and the specular highlights move.
* Flipbook textures (`MaterialComponentPool` *TextureAnimation*, e.g. Radiator Springs' flashing arrow
  signs) cycle with the timeline at the rate the world's logic gives them (`FrameSelector`, 10 fps there),
  as the game does: texture = frames[floor(time x rate) mod count]. Press Play to see them.
* Soft particles and other blended effects that fade out near geometry (dust devil funnels, smoke, glows:
  103 effects) are shown as if nothing is behind them.
* **Inspect** colours the world by what each material does: *Shininess* (specular map x colour),
  *Reflections* (how much cube map shows), *Transparency* (blended / additive / multiply / cut-out),
  *Baked Lighting* (lightmap / vertex / run time), *Glow* and *Effect* (one colour per shader).
* Choreography previews use the game shaders too, including skinned and package-only effects.

### Editing materials (Arcade)

Material panel, with a mesh selected (the slot list picks the part):

* **Effect**: **Change** opens a search of every effect in the game. Its tooltip says what each one does
  (normal map, specular map, reflections, glow, transparent...). The descriptions of the game's own effects
  ship with the add-on (`data/effect_list.json`), so the search opens at once. A new or modified effect is
  described the first time and remembered in Blender's user config folder. On export, the meshes using the material are
  re-encoded with the vertex streams the new effect reads. See *Switching shaders* below.
* **Textures**: each slot shows a thumbnail. **Pick** chooses another world texture (search with previews).
  **Load Image** adds an image file as a new texture. A slot the effect needs that has no texture is shown
  in red.
* **Parameters**: every value the effect reads, with readable names and colour pickers, and a reset to the
  effect's default. Values the game material didn't list are only written when you change them.
* **Blending / culling**: back-face culling, alpha blending, alpha test and so on.
* A lightmap variant (`material|lightmap`) shows its own lightmap and follows its game material: edit the
  base, and all variants update.

Edits show in the viewport immediately: values change in place, and texture or effect changes rebuild only
that material.

**Characters.** The Cars 2 Toolkit (2.0 and later) uses this same material system for characters. A toolkit
character in this scene shows its materials in this panel too, with its own texture list. Edits made here or in
the toolkit's Cars 2 Material panel are the same data. In Game Look a character is lit by the world: the sun,
fog, exposure, and the light probe where it stands for its ambient light and reflection. See
`docs/SharedMaterialSystem.md`.

#### Switching shaders: dropped and new texture slots

Every shipped material lists exactly the texture slots and parameters its effect uses: 3,798 materials in 21
worlds, with one exception, the eye shader's `renderedEyeTexture`, which the engine renders. Switching keeps
materials that way:

* **Dropped:** slots and parameters the new effect doesn't use (switching away from a glow shader drops its
  glow map and `emissiveMultiplier`). If any would be dropped, a dialog lists them, with their textures, and
  asks before switching.
* **Kept:** dropped slots and parameters stay with the material, listed under *Kept from earlier shaders*.
  Switching to an effect that uses them brings them back with the same textures and values. They aren't
  exported.
* **New:** slots the new effect needs that nothing provides start empty (red in the panel).
* **Export check:** export refuses to run while any material it writes has an empty required slot. The
  message names the materials and slots, and the console lists them all. This covers the world's own
  materials and new materials on exported meshes. Pickup and choreography package materials aren't written
  by a world export.

Switching back to the original effect restores the material exactly, and the world re-exports
byte-identical.

### Previewing choreographies

Meridian panel: select a choreography, then press **Preview Choreography** and **Play**. The add-on loads the
package from `assets/choreographies/` (a folder or `.zip`) and places it where the game does. Its animation is
baked onto the timeline at the scene's frame rate.

Where a choreography plays is decided by the world's logic at run time, so on import the add-on replays it
for every track and mode, the way the game runs it:
* the load-time catalysts, then the route's and mode's messages;
* the race flow: countdown, quick start, laps, finish;
* transforms carried along the impulses: lists looped over gate locators, the channels between graphs that
  carry the start / lap / finish gate, variables and lists written before a choreography plays, spawned groups
  and where they spawned.

Each time a choreography fires, the add-on records where its anchor points. The preview uses the **Track**
and **Mode** chosen in the Scene panel (with All: track A, Race).

Some examples:
* **Italy:** the start / lap / finish holograms, banners and lap numbers play at the route's start gate
  (`StartGateA`).
* **mi_oil:** the propane-tank explosions play at every locator a tank was spawned at.
* **Oilrig:** `Survival_Intro_Z_Laugh` plays at `HeliBossSpawn`.

When a choreography plays at several places at once (a hologram per gate, an explosion per tank), the preview
shows a copy at each.

The package's own anchor (`ANC_xxx_00`) is replaced by that transform's position and orientation, as in the
game. Scale is dropped: a package anchor is a Transform DataNode, which holds no scale. Many locators are
scaled only for the level editor (gate widths, windows, yachts at 2x / 4x, radiator_b's tumbleweeds on a 5x
locator at the origin), and in game all of them play at their own size and authored place. A
choreography the logic plays at a racer is shown at the route's pole position (`Starting Grid A slot 1`, else
`LOC_AStart`, else `PlayerCar1`). With no anchor at all, the package plays where it was authored, in world
space: Prof Z's helicopter flyby in oilrig flies about 1 km out over the sea, and the track fly-cams play at
their authored places.

Trigger and collision volumes inside packages (SolidColor) are shown as wireframe and never rendered, as in
the game.

The Meridian panel shows, under each anchor that isn't a plain locator, where it ends up in game.
sit in their own collection outside the world and are never exported. If the world wasn't imported from inside
the game's `assets/` folder, set **Game Assets Folder** in the panel.

**Preview All Play-on-Start** loads every choreography the world starts on load, all together on one
timeline. In the game these run as soon as the world loads. In oilrig, for example, there is one per
trimaran, which animates that boat's eyes, so previewing a single one leaves the other boats without eyes.

The preview includes:

* the choreography's meshes, materials and textures;
* skinned parts, as an armature (fire, smoke and debris sprites);
* cameras, with animated focal length. **Use Its Camera** makes it the scene camera; press Numpad 0 to look through it;
* visibility switching;
* animated material tint, opacity and glow;
* character cars from `assets/characters/`, riding their animated anchors (a box if the model isn't found).
  When the choreography plays a clip into a character (`Yugo:Choreography` on `car_choreography.ct`: the race
  intros like Italy's `M6`, the mission cutscenes like `M5`, the background characters), the clip drives the
  character's control rig: body, suspension, wheel spin, mouth, eyelids and where the eyes look, as in game.
  A reference picks the character the game spawns: `pacer` and `gremlin` (Acer and Grem) also exist as
  cut-down copies without a face rig in `characters/misc/`, and the main `characters/<name>` package wins.
  This uses the **Cars 2 Toolkit** add-on, which rebuilds the character's rig network (SubNetwork) in
  Blender. It only has to be installed; the preview loads it when needed. The toolkit's rig (`RIG_<name>`,
  with its controls) moves the preview's own game meshes. Without the toolkit only the bones the clip reaches
  directly move, and the report says so.
  Rigs made of two parts are wired by the add-on too. The Zundappcopter is Professor Z standing on the copter's
  platform. In its rig network Z's joints sit about 3 m from his bind pose, while the copter's (`Copter_Body`,
  `platform`, `Gun`, `Blades`, `rotor`) sit on their own. The toolkit matches bones at one offset, the car's, so
  it left the copter's bones without controls: the copter hung rigidly off Z. The add-on now gives every
  connected group of bones the toolkit left alone the control its own joint follows in the network
  (`<bone>_NetFollow` follower bones), and the report lists them. Single stray bones (a `Lid` on a few cars)
  keep the toolkit's behaviour.
  Z is also turned: his controls rest facing out of the door, a quarter turn from his bind pose. The face bones
  copy their controls outright and turned with them. The toolkit drives `Main` and the wheels through follower
  bones placed at the bind pose, so they did not turn, the front and back wheels took each other's controls,
  and `Body` had no control at all: the mesh tore. Followers are now placed where the network has the joint
  at rest, with the turn included. Each wheel goes to the joint the turn puts on it, and `Body` follows the
  rig's `Body_Rig`. On all 48 shipped characters this changes only the Zundappcopter;
* character eyes as the game draws them. Each frame the game renders the eyes into a small texture
  (`renderedEyeTexture`): the sclera, the eyeballs (`eyeBallTexture`), a grey outline under the top lid, then
  the lids (plain, or textured with `diffuseMapA1`, like Carla's lashes). The eye material then samples that
  texture. The Cars 2 Game render engine does the same from the material's eye parameters, which the preview
  keys every frame from the rig. The sclera and outline greys are set by the game at run time and not stored
  in the files, so they use the toolkit's calibrated values;
* the placement the game uses. An anchor on a locator or object sits there. An anchor on an *Actor
  Transform* follows the actor: if the actor has a spline follower (the dust devils, the tractor cows) the
  preview moves it along that spline (read from the world's `.dbl`) at the follower's speed, looping the
  choreography while it travels; closed splines go round once.

The add-on evaluates the network itself. It decodes the `ClipDataBlock` clips (codec from the Cars 2 Toolkit),
the Hermite `AnimCurve` paths, `Connection`s and the kinematic anchors of characters. Its transform maths
reproduces the static pose of 97% of the animated nodes in all 538 PC packages. Every PC choreography
previews except two packages that contain no scene. In the Arcade build, 528 of 531 packages load and
evaluate; the other three contain no scene.

It doesn't simulate:

* constraints (`UberConstraint`, `ParentTo`), radial-basis, `Relationship` and `DynamicChain` processors;
* character face and eye clips without the Cars 2 Toolkit installed (only the body moves);
* the eyes in Material Preview / Eevee: they animate in the Cars 2 Game render engine only;
* sprite billboarding;
* sounds;
* particle randomness exactly as the game rolls it (see below);
* one clip-command format (opcode 1, used by a single channel in one fly-cam), which is held at its end value.

The **Cars2ChoreoPreview** text lists what was skipped.

**Particle effects.** A package's logic (its .mer) can start particles: a **ParticleEffect** node
(`effect_name`) bursts each time a ticker's impulse fires it (the times are an EventList in the package),
and a **ParticleEmitter** (`emitter_name`) keeps starting its effect while its ticker's surge is keyed on.
Both sit at a `LocPrtcl_*` locator. The names come from the world's particle library,
`assets/particles/<world>_d1.zip`, then `universal.zip`. That library holds a Pigpen .dbl of emitters,
effects and particle systems, plus a .oct with the particle materials and textures (format in
`core/pigpen.py`). The preview simulates these as camera-facing quads. Each particle uses its system's
texture, start and end colour (or colour curves), size and alpha curves, lifetime, speed and direction.
tokyo_b's fireworks (`fireWorksA` at `FxLoc_fireworks`) show their red, gold, green and blue bursts and
white pop flashes over the clip, plus the random bursts of its `EmitFrwrksA` emitter.

* The Cars 2 Game renderer draws them in its HDR target, after the blended parts. The other engines show
  them over the viewport only, not in their F12 renders.
* The game randomises counts, sizes and directions with its own random stream, which isn't reproduced
  here. Bursts appear at the right place and time but not on the same pixels.
* Particles are nothing in the scene: they can't be selected or edited, and nothing is exported.

### Gate holograms and mode banners

A race gate in the game is several choreographies played together at the same spot:

* `Banner<Mode>` (BannerRace, BannerAttack, BannerBattle, BannerSurvival): only the floating mode icon. One
  plays per mode.
* `HoloGate_Elements`: the gate itself, with the blue hologram pillars, side flares, scanlines and lights.
* `HoloGate_Start`: the 3-2-1 countdown. `LapGate*` / `SecondLap`: the lap counters.

The world logic sends the gate locators (tokyo: `LOC_Gate_Banner_01..04`; 01 and 03 are the same spot, as
are 02 and 04) through a `LapGate` message. HoloGate_Elements is spawned once per gate: a Count keeps
spawning it until every listed gate has one. Preview HoloGate_Elements together with the banner (or use
Preview All) to see the gate as the game shows it.

Gate locators are stretched along their X axis only (tokyo `LOC_Gate_Banner_01` 1.99, `02` 1.66, italy
`StartGateA1Scale` 1.66, oilrig `LOC_FinishGate_B` 2.94): that stretch sizes the 30 m gate to the road, so
previews keep a non-uniform anchor scale. A uniform one (radiator_b's tumbleweeds on a 5× locator, italy's
yachts) is only the level editor's gizmo size and is still dropped. When a choreography plays at several
differently stretched gates, each gets its own copy of the preview. The placement replay now runs these Counts the way the
game does, so the hologram stands at every gate, start *and* finish, in every race world.

### Grandstand crowds

The crowds in italy, london_a/b and tokyo_a/b/c are not choreographies. They come from `CrowdMemberPlane`
nodes in the world logic (a `*_Crowd` namespace): each has a locator (`LOC_Crowd_TKO_091` ...), a row width,
row spacing, number of rows and cars per row. The game's CrowdManager (`sdaemon.exe`) lays them out and
draws them as sprites:

* **Layout** (FUN_005a3800): cars are spread across the row width, centred on the locator. Each further row
  stands Row Spacing further back, odd rows have one car fewer and are shifted half a gap. A car narrower
  than its gap is moved sideways at random.
* **Cars** (`assets/luatables/luatables.zip` → `crowdmanagerinfo.lua`, compiled Lua read by
  `core/luatable.py`): RedCar, BlueCar, GreenVan and a pale-blue mini, 7 × 3.5 m sprites (8 × 4 for the
  van). They come from the world's `crowdanim0_60new16fr` atlas: 16 pre-rendered turning angles per car,
  picked by the angle you view them from and mirrored on the other side. Each bounces 0.1 m with its own
  phase.

The Cars 2 Game renderer draws them (and other engines show them over the viewport). Select a `LOC_Crowd`
locator to edit its crowd in the **Crowds** box:

* **Crowd per Row**, **Number of Rows**, **Row Width** and **Row Spacing** are the plane's own settings.
  They redraw at once and export into the world logic's CrowdMemberPlane node (only edited planes are
  written; **Reset Rows** puts one back as imported).
* Move or turn the locator to move the crowd; the locator exports as always.
* **Show** hides every crowd in the viewport (nothing is removed).

The settings are saved with the .blend file, and the crowds come back when it is opened again.

### Ambient light volumes

A `SubGeometryAmbient` part is a light volume, not something drawn. It appears in about 40 choreography
packages, including fireworks, explosions, fires, muzzle flashes, mines, pickup respawns, lap and
survival gates, energy pickups and the oil-rig cinematics. In worlds, only mi_oil and the tutorials have
them.

**How the game uses them** (`sdaemon.exe` FUN_00c95dc0, `ambient.hlsl`):
* Each frame, the game draws every volume into the ambient buffer.
* For each pixel, it rebuilds the surface position from the depth buffer and measures it in the volume's
  axes, divided by the extents.
* It adds colour × multiplier × (1 − r)³. For a sphere, r is the distance from the centre; for a box, the
  box is mapped onto a sphere.
* The world shaders add this buffer to their lighting. That is the red, green, gold and blue light the
  tokyo_b fireworks throw on the track and buildings.
* The colour and multiplier are the volume material's `emissiveColor` and `emissiveMultiplier`. The colour
  is gamma-corrected, the way the game does it, and the clips animate both. The fireworks pulse their
  multiplier from 0 to about 3.6 at each burst; their keyed Visibility stays 0.

The Cars 2 Game renderer does the same before its opaque pass. Select a volume to edit it in the
**Ambient Light Volume** box in the sidebar:

| Setting | What it does |
|---|---|
| Shape | Sphere or Box. Plane volumes only write the buffer's alpha in the game, so they add no light. |
| Color, Multiplier | The light. Both are keyframeable. |
| Extents | Half size, where the light reaches zero. |
| Scale Bias | How much the game enlarges the drawn mesh. |
| Fade Dist | The camera distance at which plane volumes fade. |
| Falloff Dist | Kept on export, but the game resets it to 0. |
| Lights the World | Turns the volume on or off. |

**In previews:**
* Volumes come from the package, with the clip's colour and multiplier baked onto these settings.
* Volumes are eye-hidden by default, because the fireworks' spheres are 600 m wide. Unhide one to see how
  far it reaches. Hiding a volume with the eye keeps its light; Disable in Viewports turns the light off.
* Preview edits are not exported.

**In worlds:** edited volumes export to their node: shape, extents, scale bias and the two distances.
Colour and multiplier go to the material's game parameters, if its effect has `emissiveColor`. The shipped
SolidColor volumes don't, so the game lights them black.

### Helper geometry

Geometry the game never draws is shown as wireframe and left out of renders and bakes. Export is unaffected.
This covers nodes with `Visible = 0` (such as render-collision ramps), walk-in trigger volumes with
`ShowTrigger` off, and parts drawn only with `SolidColor` or `shadowonly`. Collision is never rendered
either.

### Collision

Imported collision instances are attached to the scene node they belong to. In the game,
`CollisionInstance.Transform` is that node's world matrix with scale removed (Avalanche's ExportBullet
filter). Collision therefore moves with its node, and **the export computes the transform for you**.

* **Collision From Mesh**: makes a triangle-mesh collision instance from the selected Geometry, attached to it.
* **Box / Sphere / Cylinder**: adds a primitive fitted to the active object.
* **Attach Collision To Active**: re-parents collision to another node and keeps its place.
* Shared shapes are shared mesh data. Compound shapes are an empty with child shape meshes. Scale is baked
  into the shape on export.
* Surface paint: in Edit Mode, select faces and press **Assign Surface To Faces**. This sets the per-face
  `surface_type`/`surface_color`, the same attributes the BTD/BTM add-on uses.
* Friction and restitution are set per instance.
* Untouched collision is copied verbatim. Edited triangle meshes get a rebuilt BVH (the BTD/BTM add-on's
  source-verified Bullet port). Unedited ones keep their original BVH bytes.
* Collision is hidden after import. Use **Show/Hide Collision** to toggle it.

### Custom scenes (replacing a world's map)

These are in the Scene panel, under **Custom Scene**:

1. **Replace World Geometry** deletes the world's imported meshes and all collision.
   * It keeps walk-in trigger volumes and actor roots, which the world's logic needs to load. **Keep Sky** is optional.
   * Deleted meshes export hidden and empty. Ctrl+Z undoes it.
2. Append or paste your scene, and select its meshes.
3. **Add + Auto-Assign Materials** adds the meshes to the Cars 2 scene and gives each material the closest
   Cars 2 material of the world.
   * Matching uses your object, material and image names, plus the kind of surface they describe: sky,
     water, glass, foliage, road, ground, building, metal, wood or light.
   * Solid materials never get cut-out, blended or glowing shaders, and terrain blends and particle shaders
     are never used.
   * Each match is a copy of the Cars 2 material: the real game shader with its settings, which shows in
     Game Look. Your colour image goes into it, and so does your normal map if you have one. Otherwise it
     gets a flat normal map and low shine instead of the template's.
   * The matches are listed in the **Cars2AutoMaterials** text (the **Material Matches** button). To pick a
     different one, use the effect picker or **Make Cars 2 Material** on that material.

   * **Your own baked lightmaps.** If a material has a lightmap you baked yourself, it gets a lightmapped
     Cars 2 shader and your lighting. A lightmap is an image read through a second UV map, or named like
     `lightmap`, `_lm` or `bake`. Your lightmap's UV map becomes the mesh's `lightmapUV`, and the image is
     converted to the game's RGBM format. Bake **light only**: a Diffuse bake with Direct and Indirect on
     and Color off. A combined bake applies the texture colour twice. Light 1.0 shows the texture at full
     brightness, and the game clips at 2.0.

   * **Materials from other worlds.** When this world has no shader for a surface (radiator_a has no
     water), use **Import Materials from World...**. Pick another world's `.zip` (from `assets/worlds`) or
     `.oct`, choose the kind (Water, Sky, Glass or All) or type a name filter. The matching materials are
     copied in with their textures and, if **Assign to Selected Meshes** is on, put on the selected meshes.
     They are Auto-Assign candidates from then on. Water: `italy`, `mi_italy`, `london_a`, `oilrig`,
     `tokyo_a`. Texture animations (material components) are not copied.
   * **Restore World Materials** brings back world materials missing from the file. Older versions let
     Blender drop the ones no mesh used when saving, which happened after Replace World.

Then add collision, bake lighting (unless you brought your own), and move the gameplay pieces (AI track, TrackA path, grid, spawn points)
to the new map.

### Layers (what each collection holds)

The **Layers** panel lists every collection of the world with an eye toggle. Shift-click a toggle to
show only that layer.

* **Geometry**: what the game draws.
* **Sky** (sky domes, `skyd_*` effects) and **Water** (`wtr*` effects).
* **Helper Geometry**: meshes the game never draws:
  * **Collision Helpers**: invisible collision stand-ins (`Col_*`, `COLLS*`, barrier and ramp blocks).
  * **Shadow Casters**: `shadowonly` proxies.
  * **Hidden Helpers**: other placeholders (dialog-actor stand-ins, probe and sound markers ...).
* **Lights**, **Nodes**, **Probes**: the rest of the scene tree.
* **Collision**: collision instances and shapes. It starts hidden.
* **Meridian**:
  * **Spawn & Restart Points**: one sub-collection per list the game reads (`SpawnRam`, `TakedownTrackA`,
    `HunterRestartLocations` …). A locator in several lists appears in each of them.
  * **Starting Grids**: grid locators and the slot previews.
  * **Choreography Anchors**.
  * **Triggers**: walk-in volumes.
  * **Other Locators**.
* **AI & Race**: the **AI Track** graph and its preview, the **AI Markers**, and **Paths (.dbl)**. Paths start hidden.
* **Nav Meshes** (arenas), with the **Nav Mesh Bridges** splines.
* **Modes**: what only one game mode uses (see Mode collections below).

Collections only organise the scene. Export doesn't depend on them, and hidden objects are still exported.

**Deleting a world or collections.** Blender's own *Outliner > Delete Hierarchy* removes objects one at a
time: about 18 s for tokyo_b's 5,459 objects. Two faster ways do the same in one batch (about 0.2 s):
* **Delete World** (bottom of the Scene panel) removes the imported world. That includes its collections,
  objects, choreography previews and map-object models, and the materials, textures and shader groups only it
  used, and resets the editor for it. Undo brings it back. Re-importing afterwards gives the same names as a
  fresh import.
* **Delete Hierarchy (Fast)** in the Outliner's right-click menu, on collections or objects: deletes them
  with everything inside or parented under them.
After an import the Outliner shows the world's collections folded.

### Mode collections (Battle Race, Attack, Survival)

Everything only one game mode uses is gathered under **`<world> Modes`** (in Main), one collection per mode:

* **Battle Race**: the Race-only map objects, which are the Battle Race weapon pickups (the game spawns
  them in Race mode and shows them when weapons are on).
* **Attack**: the Takedown weapon pickups and bashables, and the Takedown restart lists (`Spawn: TakedownTrackB` ...).
* **Survival**: the Collect pickups and props, the **Collect Gates** with their **Gate Posts**, and the Collect
  restart list.
* Mission worlds get **Arena**, **Bomb (Capture the Flag)** and **Hunter** instead.

Each mode has **Pickups**, **Props & Bashables** and **Spawn Points** sub-collections. Spawn Points holds the
locators those objects spawn at, but only when no other mode spawns anything there. The objects are moved, not
linked, so a mode's eye toggle really hides all of it. Objects that several modes share stay in **Map Objects**
and their usual locator collections.

This runs after every import and after map objects are rebuilt. **Organize by Mode** in the Layers panel
re-runs it, for example after you add map objects or locators. The panel's **Game modes** rows show each mode's
collections. Collections don't affect export: tokyo_b, italy and mi_air re-export byte-identical.

### Map objects (bashables, breakables, pickups)

Most of a track's props are not placed by the scene file but spawned by the world's logic. This covers
bashables (energy), negative bashables (spin-out), breakables (Italy's casino windows, chandeliers and tables,
Tokyo's neon signs and lanterns, radiator's hay bales and cacti) and weapon pickups. The game spawns them in
two ways:

* **Breakables in the world file.** The `.oct` holds one model, authored at the world origin, plus its pieces
  (MExploder `piece_name`, e.g. `LrgWindowL_1..9`). That model is only a template. A Meridian
  `SpawnAgentNode` bound to it is spawned once per locator (a loop over a list, or a channel such as
  `ITY_WindowLeft` that carries the locator), and each spawn is a copy at that locator.
* **Models from `assets/objects/`.** The agent's `.ap` section (matched by `MeridianActor_Uuid`) names the
  model, e.g. `model_name=Objects\Bashable` or `Objects\Bashable_Neg`, and the logic spawns it at its
  locators the same way.

On import the add-on replays the logic for every track and mode, records where each agent spawns, and shows a
copy of its model at each spawn point. The copies go in the world's **Map Objects** collection, one
sub-collection per object. The templates and pieces at the origin are hidden. Objects the logic never spawns
in this world stay hidden too: London's cone and topiary graphs are shared between `london_a` and `london_b`,
and each file only spawns its own. The **Track** and **Mode** pickers in the Scene panel show only the objects
that track and mode spawn.

Select a copy to see its **Map Object** box in the Scene panel:

* **Spawns at**: the locator it spawns at. The copy follows the locator's position and orientation (the game
  drops the locator's scale). Press the select button and move the locator to move the object. The locator is
  what gets exported; the copies never are.
* **Tracks / Modes**: where the logic spawns it.
* **Settings**: the object's `.ap` values, exported with the `.ap`. These include the pickup type (2: bashable,
  3: negative bashable), respawn delay, model, and the exploder's pieces, piece velocities (`velocityRange1..5`),
  fade and physics flags.
* **Preview Break**: from the current frame, hides the object and throws its pieces off, fading them if
  `FadeOverTime` is on. In the game the pieces are rigid bodies pushed by the car, so the flight is an
  approximation. The piece models, which pieces belong to it (`Pieces`) and the settings above are the real
  data. Objects whose own logic plays a choreography when they break (crate, fountain and pylon explosions)
  list it under the button, with a play button that previews it.

Editing a template's mesh (select it in the Outliner) changes every copy, as in the game.

**Moving actors (spline followers).** Some map objects are actors (Meridian `SpawnActorNode`) rather than
agents: mi_air's baggage trains and the boxes, luggage and barrels on its conveyor belts, and radiator B / C's
cows. Their template is a world `.oct` node placed anywhere, not at the origin. Its own placement is dropped,
because the actor stands at its spawn point. Most of them ride a `.dbl` path with a SplineFollower component,
and the logic spawns them again and again on a timer: an arena driver every 10 s, then a kart every 0.5 s
behind it, until a Count switches the timer off. The import also replays the logic on a clock (Delay, Pulse,
Count and Chance are timed) and shows each spawn as its own copy:

* The copy appears at its spawn time on the timeline. The first frame is the start of the race, and the
  timeline is lengthened to cover the 75 s replay. It drives along its path as the game moves it: from the
  point nearest the spawn point (`PlaceActorAsInWorld`), forward, at `Max Speed` (or speeding up by
  `Acceleration` from 0). It vanishes at the end of the path when the logic kills it there.
* Before it spawns and after it is killed, a copy is scaled to nothing at its spawn point instead of being
  hidden. Showing and hiding objects makes Blender re-evaluate every object in the file, which made playback
  stutter each time something spawned.
* Moving the path's points or the spawn locator updates the copies on the next frame. The Map Object box shows
  the path, the speed and the spawn time.
* Some spawners start only from a gameplay event. In mi_air's Bomb mode the conveyors and the wild karts start
  when a bomb destroys a base (the game sends `BlueBase_D0_EXPLODE_3Bomb` and the like). The replay sends that
  event 15 s into the race, so those copies appear from about 17–26 s; the box names the event. Events that
  name another mode (Bomb, CTF, Arena, Hunter) count only in that mode.
* Mission worlds start their content with `RestartRace`, which the game's mission classes handle. The CTF
  baggage train in mi_air (`ACT_baggageDriver9`) comes from it.
* Actors whose model is a character (`Characters\...`: mission turrets, London's trains and yachts, mission
  cars) are characters, not map objects, and are left out.

**Tracks in one file.** Italy (A, B, C), air (A, B) and oilrig (A, B) keep several tracks in one world file;
every other world has one file per track, so edits there never reach another track. In those three worlds,
and in custom worlds built the same way, some spawn points are used by several tracks, because one spawn list
runs on all of them (for example Italy's harbour crates). The Map Object box warns about these and offers
**Split for track: A / B / C**. Splitting gives that track its own copy of the locator (`<spot>_<track>`,
selected so you can move it right away). Only that track spawns the object there; the other tracks keep the
original. **Merge Back** (on the copy's box) undoes it. A split can be repeated for every track, and works
for any object on the spot (all of them follow the track's copy).

How the split is written into the logic on export (core/spawnsplit.py): the list entry that read the locator
reads a new Transform Variable instead, and before the spawn list runs, each track sets that variable to its
own locator. Lists started by a track's own messages (`TrackARace`, `TrackBCollect` ...) get the set step
added right after those handlers. Lists started at level load (a Catalyst) can't know the track yet. Their
start moves to new `TrackA` / `TrackB` / `TrackC` handlers, so those objects spawn when the game sends the
track message instead of at load. The export report lists every spot this happened to. The new locators are
ordinary scene nodes and Meridian locators, and re-importing the exported world shows the split.

Not in this version: the idle animations of animated pickups (spinning batteries and weapon pickups show at
rest), and adding brand-new spawn points from the panel (add a locator and wire it in the Meridian graph instead).

### Collect mode (Survival): gates, rings and the collect line

A Collect race sends the player through holographic gates along a collect line. The world's Meridian graph has
one `CollectGate` node per gate. Each holds:

* two gate posts, which are locators such as `Tokyo_B_Collect:Gate13` and `Gate14`;
* the tracks it belongs to (`TrackAGate`, `TrackBGate`, `TrackCGate`);
* the laps it stands on (`LapStartGate`, `Lap1Gate` to `Lap3Gate`, `AlternateRouteGate`).

The collect line is one long `.dbl` path per track (`..._CollectLineTrackA`): laps 1, 2 and 3 one after the other.

Nothing in the world file draws a gate: the game builds it while the race runs (`CollectGameInf` in
`sdaemon.exe`, 0x506300 / 0x50c3a0 / 0x567d70):

* the gate stands in the middle of its two posts, **on the ground below it** (`GetGroundHeight`), facing along
  the posts; its height is gate post 1's Y scale (5 when unset);
* it plays the `SurvivalGate_Elements` hologram scaled (post distance / 30, height / 5, 1). Its side scanlines
  open and close on a 12.9 s loop. The `HoloGate_LapFx` lightning only plays while a car drives through;
* it walks the collect line in 10 m steps. Where the line passes a gate (between the posts, within 0.6 x their
  spacing) it puts two `Survival_GateTarget` rings 3.5 m above the ground: the circles that show where the line
  goes;
* the **lap gate** (`LapStartGate`) only counts laps: no hologram, no rings. Each pass of it starts the next lap;
* a gate's place in its lap is its progress along the race spline `TrackA/B/C`. Names like `A 1:4` are
  labels the game never reads.

The import shows all of that:

* **Gates** in the world's **Collect Gates** collection: an empty with the hologram's parts under it (the parts
  share the package's meshes). Lap gates show as an outline only. **Animate Gates** (Collect Gates panel, off by
  default) plays the hologram's loop on the timeline; off, the gates are still and nothing about them updates
  per frame, so playback and editing stay fast.
* **Rings** where the collect line crosses each gate, for the laps that gate is on.
* **The collect line cut per lap**: `CollectLineALap1`, `CollectLineALap2`, `CollectLineALap3`
  (`CollectLineBLap1` ... for other tracks, with the path's prefix added when a world has two lines for one
  track; `CollectLineA (all laps)` when no lap gate was found) in the **Collect Lines** collection. Laps are cut
  where the line passes its track's lap gate. Edit them like any curve, in Edit Mode or by moving them; export
  joins them back into the one path (unchanged laps leave the `.dbl` byte-identical).
* The laps mostly run along the same road, so the viewport draws each lap in its own colour (lap 1 green,
  lap 2 orange, lap 3 yellow: none of the AI paths' colours), raised above the road (0.6 m, then 0.5 m more per
  lap) and labelled with its name, in every shading mode. Each ring is labelled with its gate and lap too.
  **Lap Lines** (Collect Gates panel) turns both off.

Pick **Track**, **Mode: Collect** and a **Lap** in the Scene panel to see what the game shows on that lap: its
gates, their rings and that lap's part of the line.

To move a gate, grab it (or one of its posts) and move or turn it: the posts follow the gate, the gate stays
between its posts and on the ground, and its rings follow. Move one post to make the gate wider, narrower or
turned. Scale post 1 on Y to change its height. The posts are the world's own locators and export as they always
do. The gate objects, rings and outline are never exported.

A ring is where the game finds the collect line crossing the gate, so it is not stored anywhere: rings **follow
the collect line**. Edit a lap's line (in Edit Mode or by moving the curve) and its rings move to where the line
now crosses each gate a moment after the edit settles; move a gate and its rings move with it. Rings can't be
picked or moved by hand. **Refresh Rings** (top of the Collect Gates panel) places them on demand; if they can't
be placed, the panel says why in red (the full error is in Window > Toggle System Console).
Several laps' gates often stand on one pair of posts (oilrig's `A 2:2` and `A 3:2`): with **All Laps** their
rings show together, so pick a lap to see that lap's ring only.

A gate gets no ring when the game can't see the line pass it: it checks every 10 m along the line, within
0.6 x the post spacing of the gate's middle, so a gate narrower than about 8.5 m, or one the line passes far
from the middle, never gets one (and no pickups behind it) in the game either. Such gates are listed in red
and their box says which lap has no ring. **Add Gate** puts a new gate across the line, centred on it, at least
20 m wide.

**Gameplay > Collect Gates (Survival)** lists each track's gates lap by lap, in the order the game meets them
(`#1`, `#2` ..., the lap gate marked first), with the lap's line. **Rebuild Gates** builds the gates, rings and
lap lines again from the world logic (new gates and edits not yet exported are lost; it also shows the Paths
collection again if it was hidden). A row the editor can't read shows its error instead of stopping the panel.

* **Add Gate**: a gate for that track at the 3D cursor, across the collect line there, on the picked lap (lap 1
  with All). It gets two new post locators sized like the track's nearest gate. Export adds them and a
  `CollectGate` node (with its two ports) to the world logic. A gate taken off every track whose posts no other
  gate shares is reused first, so the file doesn't grow;
* **X**: a new gate is deleted. A gate of the world is taken off the picked track (every track with All): its
  node stays in the file on no track, and the game skips it. It shows under **Off every track**, with **+A /
  +B / +C** to bring it back;
* the gate box's **Tracks** and **Laps** toggles are the node's own flags (the same values as in Gameplay Node
  Settings);
* **Renumber** (sort icon) relabels a track's gates `A 1:1`, `A 1:2` ... per lap in the game's order, and
  `A Lap Gate`. Labels are the gates' names: rename a gate there or in the outliner. Export writes them as the
  nodes' `AuthoredName`. To change a gate's real order, move it along the race.

### Point-to-point races (Race Finish)

How the game counts laps:
- Every car's progress is its position along the world's lap spline: the `.dbl` B-spline
  `TrackA`/`TrackB`/`TrackC`.
- A lap counts when a car's position jumps from the last 5% of the spline to the first 5%. A car
  finishes when its completed laps reach the lap count.
- The lap count is the setting **Number of Laps per Game** in the game's track settings.

**Race Finish** in the Scene panel ends the race somewhere other than the start line. Press **+** to add a finish
locator at the 3D cursor and move it where the race should end. Then pick a mode:

- **Lap Spline** (vanilla finish, recommended). **Build Point-to-Point Lap** rebuilds the lap spline so that:
  - the stretch under the starting grid sits in the last 5%;
  - the road from the front row onward sits in the first 5% and continues along the route;
  - the last metres before the finish sit in the last 5%;
  - the road just past the finish sits in the first 5%.

  These pieces are joined by legs high in the sky, plus a long dummy loop up there that makes the spline long
  enough. Crossing the finish then completes the lap exactly like a normal finish line: the placement stinger,
  real finish times, and AI cars finishing too.
  - Set **Number of Laps to 1** in the game's settings before racing it.
  - Only the `.dbl` changes. The finish locator is a reference and is not exported.
  - Run it again after moving the finish point. **Restore Lap** puts the original lap back.
  - If the grid sits off the lap line, the cars' path to the route is taken 150 m ahead of the grid. Set **Start
    Line** to an object to give it exactly.
  - Shortcoming: AI or weapon logic that looks "N metres further along the lap" can, within about 100 m of the
    finish, look into the sky legs.
- **End Message**. On export, the world's logic ends the race when a human player (players 1 to 4) comes within
  the radius. It uses the message `EndTutorial`, which the race manager handles in every race. The race ends at
  once: no placement stinger, finish times not exact, and the lap counter still shows. This mode works with any
  lap count.

Either way, keep the finish point well along the route, with no other part of the route nearby.

### Track variants (A / B / C) and modes (Race / Collect / Takedown)

One world file holds every route raced in it (oilrig has OILRIG_A and OILRIG_B, Italy and Air have A, B
and C routes). Every race world also holds the content of all three modes. So after import, the routes'
bridges, detour walls, barriers and skies overlap, and so do the modes' objects.

**Track** in the Scene panel shows one route at a time (All, Track A, Track B ...). **Mode** next to it
picks the game mode:
* **Race**: the route as raced (two-wheel gates ...).
* **Collect**: the Collect graph's gates, weapon pickups, start point and results cameras.
* **Takedown**: the Attack graph's enemy spawns and turrets.

When a race starts, the game sends the messages `TrackA` / `TrackB` / `TrackC`, then the mode's
(`TrackARace`; `CollectMode` and `TrackACollect`; `TakedownMode` and `TrackATakedown`). The world's Meridian
logic then shows and hides that route's and that mode's objects. On import, the add-on replays that logic:
* the `Catalyst` nodes first (the state when the world loads);
* then the route's messages;
* impulses run through gates, switches, message senders and the channels between graphs;
* each Visibility Toggle acts on the objects fetched into it;
* objects of the world's Collect and Attack sub-graphs (`OilRig_Collect`, `Radiator_B_ATTACK` ...) count as
  that mode's;
* locators in a route's lists (`CollectTrackB`, `TakedownTrackA` ...) count as that route's.

In oilrig, A gets the starry sky, the raised bridge and the left detour wall. B gets the cloudy dome, the
lowered bridge, the right detour wall, the container wall and the reverse-block barriers.

Whatever the logic hides when the world loads is hidden in every preview too: the spy bases' damaged states
(D1 to D4), spawn templates (crate stacks, pylons) and spawned placeholders, until a mode spawns them.

Picking a route and mode hides everything else's objects (and everything parented under them) in the
viewport and in renders. **All** puts every object back. Export ignores it.

London switches routes with props the game spawns at run time (statues, boxes). Those props aren't in the
world file, so its Race differs little between A and B, but its Collect and Takedown content still separates.

**Mission worlds** (`mi_*`, the arenas) have one route, **Mission**, and their arena modes:
* **Arena**: team battle; the coloured team start and restart points.
* **Bomb**: the game's CTF type; the red and blue spy bases (intact state), the bomb pedestal and bomb
  spawns. In Arena and Hunter the bases are gone, and the props they replace (Italy's fountain ...) show.
* **Hunter**: the hunter start points.

The game picks the mode with `MissionGameType` nodes, which branch on Arena Combat / CTF / Hunter when the
world loads. The add-on runs only the chosen branch, and adds the restart and spawn lists named for each
mode (`ArenaRedRestartLocations`, `BombSpawnLocators`, `HunterRestartLocations` ...).

### Gameplay (AI track, spawns, grids, paths, nav meshes)

These are in the **Gameplay** panel. The formats are documented in `gameplay_docs/GAMEPLAY_DATA.md`.

**AI Track.** One mesh: vertices are track nodes and edges are links.
* Edges point in the direction of travel. Arrows on the **AI Track Edges** preview show it, and
  **Flip Link Direction** reverses it.
* In Edit Mode, extrude to lay out road and press F to join nodes.
* The panel shows the active node's or link's values. Click a value to set it on every selected node or link.
* Node values: width, the fast and energy racing lines (U across the road, 0 to 1) per track A/B/C, speed
  limit, look-ahead and end-of-race marker.
* Link values: avoidance lanes, force drift / drift left, bash zone, can-respawn, and hop/turbo per line.
* Sections (the runs between branches) are rebuilt the way the game does it, and keep their settings.
* Moved and new nodes are re-oriented along the road on export. **Recompute Node Frames** does it on demand.
* The **track preview** draws the road edges (white, with direction arrows), the fast racing line (red) and the
  energy line (blue) of the track picked next to **Preview**. It follows every edit as you make it, in Edit Mode
  too, and is drawn in front of the scene. **Width** and **Opacity** set how thin and see-through its lines are,
  so the nodes stay easy to pick. It hides with the AI Track layer or the viewport overlays, and is never
  exported.

**AI Markers.** Empties for hop, sidestep and drift points, with a type and track. Their lap distance is
recomputed when they move.

**Spawn & Restart Points.**
* Pick a list, then use **Add Spawn Point** (at the 3D cursor), or **Add/Remove Selected** for existing locators.
* Arena attack NPCs use `SpawnRabbit`, `SpawnRam`, `SpawnSpotter` and `SpawnHenchmen`. Waves are in
  `xml/carshunterwaves.xml`.
* Lists marked with a lock are filled by game code, not by locators.

**Starting Grids.**
* Row count, spacing, stagger, the 2-3 / 3-2 layouts, and the grid locators.
* The slot previews are children of the grid locator, so they follow it as you move it. Slot positions use
  the game's own formula.
* Which cars race, and in which slots, is set in `xml/gameprogression.xml`.

**Paths (.dbl).**
* Each named path is a curve, shown on import (the Paths and Bridges collections). Edit its points in Edit Mode.
  **New Path** adds one.
* Every path that isn't a collect line lap is outlined in **light blue** with its name, so it isn't taken for a
  collect lap (green / orange / yellow) or the AI track preview (white / red / blue). **Path Outlines** (Paths
  panel) turns it off.
* A track needs `TrackA` (or `TrackB`/`TrackC`, falling back to `lane1`) for lap progress and race
  positions. Arcade has no checkpoints.
* Edited paths are written to both the polyline and the B-spline the game reads.

**Nav Meshes.**
* One mesh per walkable island. **Make Nav Mesh** turns selected meshes into nav meshes.
* Edited meshes are triangulated, and their neighbours and lookup tree are rebuilt. Untouched ones are kept
  byte-exact.

**Gameplay Node Settings.** Plain fields of the gameplay graph nodes: takedown and collect settings, collect
gates, AI hints, AI car handles, hunter/arena games, nav mesh bridges and reset overrides.

Worlds imported with an older version: use **Load Gameplay Data**.

## Research notes (for other tools)

Developer documentation for the editor itself (architecture, world files, import / export, the renderer's code, previews, tests) is listed in [docs/README.md](docs/README.md#developing-the-editor-itself).

The [docs](docs/README.md) folder collects everything learned about the game's files, level logic and engine
while building this add-on: the Meridian graph format and how worlds use it, gameplay data, particles, race
gates, grandstand crowds, ambient light volumes and the game's Lua tables, with excerpts of the game's code
and their `sdaemon.exe` addresses. It also covers the performance pitfalls of large Blender scenes.

## Format notes (for other tools)

* `.oct`/`.mer` are Tupperware trees. `core/c2oct.py` (from the Cars 2 Toolkit) is byte-exact on every world.
  The fixes here: the `.mer` root key is `MeridianOct`, and the game's encoder always interns `""` first.
* Meridian references (`Parent`, `Begin`, `In`, link `Origin`/`Target`...) are **global** indices, and each
  namespace block is self-contained. To add nodes to the world block, every reference in the merged blocks
  after it is shifted. `core/meridian.py` does this, using the reference fields measured on every shipping world.
* A choreography is placed by `LocatorNode → TransformSource -(TransformFetch link)→ ChoreographyTransformReference(Name=ANC_xx)
  → Choreography`. It is played by an `Impulse` link from any `Event` (a walk-in trigger's `Inside / Begin` is its enter event).
* Walk-in triggers are actor-root Geometry nodes (no scene parent) with an `.ap` section that has
  `AddComponent_WalkInTrigger` and `WalkInTrigger_Uuid` (a NodeGroup in the `.mer`).
* Vertex element types (ids in the files): 1/2/3 = float2/3/4, 25 = u8x4 normalised (RGBA), 44/45 = half2/4.
* `LightRigConsts` is `float[3][9]` (R, G, B order-3 SH).
* Arcade only (details in `core/arcade.py`):
  * Every index stream in `_0.ibuf` is followed by a second list of the same length. The renderer never
    reads it (the game's `IndexStream` loader draws `Length` indices from `IndexBufferOffset`), but the add-on keeps
    it so files stay byte-identical.
  * The stored `Binormal` is −(N × T)·sign of Blender's tangent space.
  * Volume probe cell (x, y, z) is at `DirectSH[(x + y·Dx + z·Dx·Dy)·27]`, sampled trilinearly.
    `AverageLuminance` = mean luminance of the cells' DC term × 0.2821.

## Limitations / not in this version

* **Mostly untested in-game.** Confirmed in-game: an exported Arcade world loads; rotating `sunLight`
  moves the sun and the car shadows; AI track path edits; Collect mode gate placement, rings and lap order.
  Everything else was verified structurally (round trips, re-import,
  graph and shader-stream validation). Test each kind of change in the game before relying on it.
  New geometry, new Meridian locators/choreographies and baked cube maps are the riskiest.
* SH projection uses the runtime formula. Cars 2's shipped rigs were baked with a different normalisation,
  which **Keep Overall Brightness** compensates for.
* Arcade:
  * Both renderers run the game's shaders, but these engine run-time buffers are replaced by constants:
    * no dynamic shadows in Eevee (the Cars 2 Game engine has them);
    * the world's own particle systems don't appear. Rain (Tokyo), splashes, drips, steam, smoke and
      sparks are emitters the game spawns at run time from the world's particle package. Only
      choreography previews simulate particles (see Previewing choreographies);
    * ambient light volumes light the world in the Cars 2 Game renderer only, not in Eevee;
    * no planar reflections or refraction in Eevee (the Cars 2 Game engine has them);
    * no bloom or motion blur in Eevee (the Cars 2 Game engine has both);
    * unbaked parts take their SH ambient from the world's light rig, not from the volume probe they sit in.
  * The sky dome stays where it is; in the game it follows the camera.
  * Exposure comes from Scene Brightness (or Auto Exposure), not from frame-by-frame adaptation.
  * With **Eevee Game Shaders** on, importing takes longer (10-30 s) because every effect is translated
    into node groups, and the first Material Preview view compiles every material's shader (see above).
  * The *Baked x Blender Lights* and *Textures Only* modes (used by the bakes) are simplified materials.
  * Where the probe grid cells sit inside the box (spread evenly across it) is inferred from the sampler, not
    seen in the code.
  * Baked lightmaps are written uncompressed (4x the size of the shipped DXT5).
  * 111 shaders exist in both `_lmh` and `_vb` versions; switching only works for those (the rest are
    left unchanged with a warning).
* The cube-map face orientation follows D3D conventions in game space. It has not been checked against the game.
* You can't create new walk-in triggers or breakable types from scratch, or delete choreographies that came
  from the game.
* Gameplay editing: AI track path edits and Collect mode gates are tested in the game. The rest (markers,
  other paths, spawn lists, grids, nav meshes) is verified structurally (untouched exports are identical and
  edits re-read correctly), not in the game yet.
  * Two links between the same nodes (one place, in tutorial) can't be one Blender edge. They are kept, but
    after vertices are deleted they may point at the wrong nodes.
  * Nav mesh per-edge flags (3% of edges, meaning unknown) are kept per face. Rebuilt meshes get a looser
    lookup tree than the shipping one (still valid).
  * New B-spline paths use the polyline points as control points (a slightly smoothed copy).
  * Arena waves and the event table (`xml/`) are not edited here.

## Credits

* `core/c2oct.py`, `core/c2headers.py`, `core/c2clipblock.py`: the Cars 2 Toolkit (MIT, used with permission).
* `core/btcol.py` BTM/BTD layout and BVH rebuild: the "Cars 2 BTD_BTM Collision IO" add-on (MIT, used with
  permission). Its BVH rebuild is a port of [Bullet Physics](https://github.com/bulletphysics/bullet3)'
  `btQuantizedBvh` / `btOptimizedBvh` (zlib licence).
* The vectorised vertex decoding is adapted from
  [cars-2-blender-io](https://github.com/offsetting/cars-2-blender-io) (MIT License, Team Offsetting).
* Light-rig spherical harmonics follow Ramamoorthi & Hanrahan (SIGGRAPH 2001) and Sloan, *Stupid Spherical
  Harmonics (SH) Tricks* (GDC 2008).

The full licence texts are in `THIRD_PARTY_NOTICES.md`.

Engine behaviour was worked out from the shipped game: the Arcade build's shaders (disassembled with Windows'
d3dcompiler) and `sdaemon.exe` in Ghidra. The shader translators (`core/d3dbc.py`, `core/fxgraph.py` +
`fxnodes.py` for Eevee, `core/fxglsl.py` + `gamerender.py` for the Cars 2 Game engine) decode the shipped shader
bytecode itself; its decoder was checked instruction by instruction against d3dcompiler on all 1,162 shaders
the Arcade worlds use.

## License

GNU General Public License v3.0 or later (see `LICENSE`). Cars 2 and its game files belong to their owners; the add-on ships no game files.
