# The shared material system (Scene Editor and Cars 2 Toolkit)

Worlds and characters use one material system:
* the same material data (`Material.c2`);
* the same Game Look (the Cars 2 Game render engine; Eevee node translations);
* the same Blender material modes;
* the same Material panel;
* the same shader-switch rules: dropped, kept and required texture slots.

This page is for anyone working on either add-on.

## Files

The Scene Editor is the master copy. `tools/sync_look.py` copies these files unchanged into the toolkit
(`cars2_toolkit/look/`, and `cars2_toolkit/core/` for the format readers). Never edit the toolkit's copies.

| File | What it holds |
|---|---|
| `looklink.py` | which add-on's copy runs (below) |
| `props.py` | every property group (the toolkit registers all of them, so a .blend reads the same in either add-on) |
| `matio.py` | MaterialPool / TexturePool <-> Blender: `fill_texture_table`, `fill_material_props`, `write_props`, `add_new_textures`, `apply_replacements`, `image_to_dds` |
| `meshdata.py` | mesh -> the game's vertex groups (`extract_groups`), used by the exporter and the renderer |
| `fxnodes.py`, `matnodes.py` | Game Look as Eevee nodes, the Blender material modes, live editing, the shader-switch rules |
| `gamerender.py`, `gamepost.py`, `gameshadow.py`, `gamereflect.py`, `gameeyes.py` | the Cars 2 Game render engine |
| `matops.py`, `lookui.py` | material operators, the Material panel's contents |
| `core/arcade.py`, `c2oct.py`, `cubemap.py`, `d3dbc.py`, `dds.py`, `fxglsl.py`, `fxgraph.py`, `vertexfmt.py` | format readers |
| `data/effect_list.json` | the Pick Effect search list (world and `_skin` effects) |

World-only modules (crowds, particles, light volumes) are optional to the renderer (`gamerender._optional`).

## One copy runs (`looklink.py`)

Both copies register the same names: `Material.c2`, the `C2_GAME` engine, the `c2.*` operators. Only one may be
registered. `bpy.app.driver_namespace['cars2_look']` records the running copy:

* **Scene Editor enabled:** a toolkit copy registered before it is released first. Then the Scene Editor
  registers its own and claims it.
* **Toolkit enabled:** it registers its copy only when no copy runs. Otherwise it waits
  (`cars2_look_waiting`) and takes over when the Scene Editor is disabled.
* The toolkit reaches the running copy through `look.api()` (`looklink.modules()`). With both add-ons enabled,
  an edit in either panel changes the same data.
* **File loads wipe `bpy.app.driver_namespace`** (File > Open, File > New). `looklink` keeps the owner and
  waiting records a second time in a small module in `sys.modules` (`cars2_looklink_records`), which a file load
  leaves alone, and `owner()` puts them back. Without it, opening a `.blend` with both add-ons enabled lost the
  Scene Editor's record, and the toolkit said "the material system is not running" (Import Character Template,
  the Look panel) although the Scene Editor's copy was still registered. The toolkit's `look._reclaim` also
  restores a missing record of a registered Scene Editor copy in the namespace, and takes over itself if nobody
  runs the system any more.

## Whose textures a material uses

A slot's `texture` is a TexturePool index. Which pool it indexes:

| Material | Texture list | Game context (`fxnodes.material_context`) |
|---|---|---|
| a world's | `scene.c2.textures` | `fxnodes.game_context(scene)` |
| a character's (`mat.c2.owner` = its armature) | `owner.c2tex` | `fxnodes.owner_context(scene, owner)`: the character's own effects folder; fog and reflections from the world when one is loaded |
| a choreography preview's (Scene Editor) | none (read-only) | `gamerender.PACKAGES[name]` |

`fxnodes.texture_table(scene, mat)` gives the list. Pick Texture, Load Image, the Material panel, the export check
(`fxnodes.missing_in`) and the renderer (`gamerender._package`) all go through these.

## Characters in Game Look

* **Skinned effects:** a character's material picks from the `_skin` effects (`matops.is_skinned`). World
  materials pick from the rest.
* **The engine's environment scroll map:** every material that samples `environmentScrollMap` gets
  `misc\environmentScroll.dds` (a 64 x 64 DXT1 cube in `startup3.zip`), bound once on the shared material.
  No shipped material lists it. From `sdaemon.exe`:

  ```c
  // sdaemon.exe, decompiled (variable names ours)
  piVar1 = FUN_00868910(local_14, "misc\\environmentScroll.dds");
  FUN_008d8330(&env->field_0x21fc, piVar1);
  set_shared_texture(shared_material, *(undefined4 *)&env->field_0x21fc, "environmentScrollMap", 0);
  ```
  In the character shaders (e.g. `a_1_dtmmsmqdrqcuceismm_skin`) it multiplies the decoded reflection cube, looked
  up with the `environmentScrollRotation` matrix (identity unless the game sets it):
  `reflection = (env.rgb * env.a * 6)^2 * g_EnvMapExposure * scroll(R * dir)`, blended over the lit colour by
  fresnel x reflectivity map. `fxnodes.ENGINE_FILES` / `engine_texture` bind it. A white stand-in for this map
  (what was bound before) blows a car out to white in a bright map.
* **Reflection cube in a world:** the Cars 2 Toolkit sets `gamerender.ENV_OVERRIDE[armature name]`. That's the
  world texture index of the Volume probe the car is in, else the scene probe, as the game picks it at run
  time. The material keeps its own `envMapInstance` slot, so export doesn't change.
* **No world loaded:** `fxnodes.NEUTRAL_LIGHT` (Tokyo B's sun, exposure and PrimarySceneRig SH averaged to grey,
  no fog). The reflection is the material's own `envMapInstance` cube: characters carry `errorsky` there, the
  stand-in the game uses until a map's probe replaces it (RGBM alpha ~0.09). Only a material with nothing in that
  slot gets `gamerender.NEUTRAL_ENV`. That stand-in is RGBM like a real probe: Tokyo B's `locator3` averages
  rgb 0.74 / 0.71 / 0.78 with a 0.042 multiplier. A plain (0.3, 0.3, 0.3, 1) is about 20 times brighter after
  decoding, and bleaches the paint.
* **Preview values:** `fxnodes.PREVIEW_VALUES` {material: {parameter: value}} override a parameter for drawing
  only (`param_value`, and `matnodes._props` for the Blender materials). Nothing writes them to the material or
  an export. The toolkit's Emissions toggle uses it: 85 of 86 character materials with an `emissiveMapA1` store
  `emissiveMultiplier` 0, because the game lights them at run time.
* **Eyes:** the toolkit puts the rig's eye values in `gamerender.EYE_VALUES[material]`, which only the eye
  texture pass reads. The game evaluates the eye rig every frame and never stores the result. Writing the
  values into the material made an untouched character export with its eyelids moved.

## Texture slots a material lists

Survey of every material against its effect's samplers and parameters (`tests/mat_slot_survey.py`, toolkit
`tests/char_slot_survey.py`):

| | Worlds (21) | Characters (58 zips) |
|---|---|---|
| Materials | 3,798 | 295 (293 `_skin`) |
| Missing a slot the effect samples | 1: `renderedEyeTexture` | `environmentScrollMap` 90, `renderedEyeTexture` 44 |
| Listing a slot the effect doesn't sample | 0 | 0 |
| Listing a parameter the effect doesn't have | 0 | 0 |

So a shader switch drops what the new effect doesn't use. Export requires every sampled slot except
`fxnodes.ENGINE_TEXTURES` (`renderedEyeTexture`, `environmentScrollMap`), which the game fills itself.

## Switching a shader in Game Look

* A mesh's GPU batch carries the vertex inputs of the effect it was built for. A switch to an effect reading
  others (a normal map needs tangents) drew from a batch without them. `gamerender.effect_changed` rebuilds
  the meshes drawing the material.
* A switch adds the new effect's slots empty. An empty cube slot was bound to white, which decodes as RGBM at
  full multiplier: about 36 times a real probe. The car blew out white, and auto exposure then darkened
  everything. A character's empty cube slot now gets `NEUTRAL_ENV`. World materials are unchanged.

* With a still viewport only edited objects are redrawn, and their commands are refreshed in place
  (`Renderer._refresh`). That refresh kept each command's effect, so after a switch the part drew with the old
  shader. Textures picked for the new slots changed nothing visible, and glass drew black until something
  rebuilt everything. A command whose part now has another effect (or Reflect technique, for mirror-pass
  commands) is rebuilt. Tests that play the timeline hid this: playback rebuilds. `cars2-character-toolkit/
  tests/bl_tk_still.py` edits with a still view and checks each command's effect.
* An empty glow map (`emissiveMapA1`) is black (`fxnodes.MISSING`). White made the whole part glow at the new
  effect's `emissiveMultiplier` (often 1) as soon as a switch added the slot.
* The texture picker lists the whole table. Textures of the kind the slot samples come first, the others are
  marked "(cube map)" / "(2D)". The kind comes from the effect's pixel shader (`fxnodes.slot_is_cube`), not from
  the texture in the slot.
* The toolkit's Emissions preview follows material edits (a Material depsgraph update re-applies it), not only
  the toggle.

## Final renders (Render Animation)

* `render()` handed its pixels over with `passes['Combined'].rect = array`. Blender converts that value by
  value: ~1.4 s a frame at 1280 x 720. `rect.foreach_set` takes ~1 ms.
* No depsgraph updates reach a final render, so every frame rebuilt every command: ~370 ms in tokyo_b. Now
  the first frame of a render job (`render_init`) builds everything. Later frames rebuild only the objects
  whose evaluated matrix or `hide_render` changed, plus armature / shape-key meshes (re-deformed). Keyed
  material values arrive through the frame-change handler. `render_complete` / `render_cancel` rebuild for
  the viewport.
* Measured (`tests/bl_renderanim.py`, 24 frames, 1280 x 720, an animated McQueen):

  | | Before | Pixel hand-over fixed | Both fixes |
  |---|---|---|---|
  | McQueen alone | 1,483 ms a frame | 62 ms | 64 ms |
  | McQueen in tokyo_b | not measured | 481 ms | 139 ms |

## Undo and redo

Undo and redo re-create the data-blocks they change: an edited material, its images. A Python reference kept from
before points at freed memory. The renderer's draw commands held materials (`c.mat`), the cached contexts held
images, and the node-slot cache held nodes. So after undoing a material edit, every later draw failed, and Game
Look stayed blank until Blender was restarted. Now:

* `fxnodes.reset_after_undo` (`undo_post` / `redo_post`) drops the contexts, slots, keyed-material and variant
  lists. `gamerender.reset_after_undo` drops every command, block and object list
  (`Renderer.forget_blender_data`). Preview packages' textures look their images up again by name
  (`Textures.revalidate`).
* `view_draw` catches a `ReferenceError`, resets the same way and redraws, instead of failing on every frame.
* The toolkit drops its character, mesh, eye-rig and probe lists too.

Test: `tests/bl_undo.py` (a material edit, its undo step, `ed.undo`, then drawing). Script-made undo steps need
`bpy.ops.ed.undo_push` with a full window / screen / area context. Pushed from a timer without one, they are
silently skipped, and an undo then goes back past the world's import.

## Testing in a real window

The renderer's auto exposure adapts frame by frame (`g_HDR_Adaptive_Luminance_Rate`). A screenshot taken a moment
after switching to Rendered view shows an over-exposed frame (everything white in a dark map). Play the timeline
for a few seconds first (`cars2-character-toolkit/tests/bl_tk_look.py` does).
