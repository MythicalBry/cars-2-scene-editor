# The Cars 2 Game render engine: how the code works

The README ("Cars 2 Game render engine") describes what the renderer shows and the game behaviour behind each
pass. This page is the code map, for changing it. Each module's docstring names the `sdaemon.exe` functions it
follows.

## Modules

| Module | Role |
|---|---|
| `gamerender.py` | the engine (`C2_RE_game`, id `C2_GAME`), the `Renderer` singleton (`RENDERER`), effects, meshes, draw commands, the frame |
| `core/fxglsl.py` | D3D9 shader bytecode → GLSL, one statement per instruction; `EngineLayout` (the engine uniform block) |
| `core/d3dbc.py` | bytecode decoder (instructions, `def` constants, CTAB) |
| `gameshadow.py` | depth pre-pass, the two shadow casters (dynamic 0x40000 / static 0x4000 RenderCaps bits), cascades, the shadow-mask pass from the game's own effect |
| `gamereflect.py` | the planar reflection: plane from the first `g_AlphaReflection > 0` material, mirror camera, Reflect technique for parts with RenderCaps 0x200000, 5-tap blur |
| `gamepost.py` | auto exposure (the game's log-average luminance), `postprocess.tfx` (exposure, Hejl curve, bloom, motion blur, gamma, LUT), blit into Blender |
| `gameeyes.py` | `renderedEyeTexture` for character eye materials |
| `ambientvol.py` | ambient light volumes into the ambient buffer |
| `particles.py`, `crowd.py` | particle quads and crowd sprites, drawn inside the frame |
| `fxnodes.py` | engine values (`ENGINE_VALUES`: sun, fog, light rig SH ...), material contexts, preview values, undo reset |
| `meshdata.py` | mesh data in the game's terms (per slot: corners, positions, normals, UV / colour sets, tangent frames) |

Engine effects (postprocess, shadow mask, ambient, blur ...) come from the player's own game
(`assets/startup*.zip`), run from their bytecode like any material effect.

## Effects: one shader per effect

`meta_for(assets, effect, technique, local)` reads a `.tfx` (`_read_tfx`: the character package's local
effects first, then the game's), and `effect_for` converts and compiles it (`fxglsl.Converter`,
`_create_shader`). Both are cached per (assets, effect, technique). Compiling takes milliseconds. Materials are
**never** compiled individually: everything a material changes is a uniform block.

## Data the renderer keeps (`Renderer`)

| Field | What |
|---|---|
| `meshes` | `MeshEntry` per object name: GPU batches per part (`_mesh_groups`, `_batch`), bounding sphere. Batches carry the **effect's** vertex attributes, so `effect_changed(mat)` drops the meshes using a material whose effect changed |
| `mesh_users`, `mat_users` | reverse maps (mesh → objects, material → objects) so an update touches only what it affects. Never look objects up by name per frame: that cost ~0.7 s a frame on tokyo_a |
| `obj_draw`, `obj_keys` | per object: its draw commands `(opaque, blended, casters)`, and what they were built from (`_draw_key`: shown, display, world matrix, mesh, material versions) so a soft rebuild can keep them |
| block caches | per draw: the uniform block (`block`); per material: its bindings and values (`mat_cache`, `_material_block`, `_bindings`), worked out once for every object using it; `param_block`, `_defaults_block`. Editing a value bumps the material's version |
| `kept`, `suspect` | meshes kept from before an undo (reused when `_exact` still matches), and meshes an update may have changed (checked against `_exact` on the next draw) |
| `TextureCache` | GPU textures for images, constants and cube maps (cube DDS → equirect panorama, `core/cubemap.py`) |
| `dirty` / `full` / `version` / `cmd_version` | what must be rebuilt next draw |

### Updates (`C2_RE_game.view_update`)

| Depsgraph update | Renderer reaction |
|---|---|
| object geometry | `_changed`: an undeformed mesh outside Edit Mode without modifiers is marked *suspect* (checked, not re-read); otherwise `drop_mesh` (deform-only for armature-bent meshes: `_redeform`) |
| object transform | `mark(name)` plus its descendants |
| mesh data | `_changed` for each object in `mesh_users` (one name map per update, not a lookup each) |
| material | `touch_material` (its blocks and commands) |
| collection | `rebuild(soft=True)` (objects shown, hidden, added or removed: unchanged ones keep their commands) |
| scene | `version += 1` (redraw only) |

`_refresh(depsgraph, ob, name, ctx)` updates an object's existing commands in place, which is cheap during
playback. It **rebuilds instead** when a command's part now has a different effect (or a different Reflect
technique, for mirror-pass commands). Without that check, a shader switch in a still viewport kept drawing with
the old effect. `effect_changed` and `textures_changed` are the entry points for material operators.

`view_draw` catches `ReferenceError` (a freed data-block after undo), calls `fxnodes.reset_after_undo()` and
redraws, so Game Look never stays dead.

After undo / redo, `forget_blender_data` drops every command and block (they hold materials and images undo may
have re-made) but keeps the meshes aside in `kept`: a mesh whose `_exact` fingerprint still matches is reused with
its materials looked up again. `rebuild()` (hard) is for scene-wide changes the keys can't see (texture swaps,
render settings); `rebuild(soft=True)` for visibility changes.

## A frame (`Renderer.draw` → `_draw_scene`)

1. **Commands** (`_commands`): per visible object, `_object_draw` makes a `_Cmd` per part (shader, batch,
   blocks, textures, render states from `_states`, cutout from `_cutout`). Casters for the shadow maps are
   chosen by RenderCaps (`_object_cast`). This happens **outside** any framebuffer binding, because creating a
   GPU texture can rebind framebuffers. Only dirty objects are rebuilt unless `full` is set.
2. **Engine block** (`engine_block`): camera matrices, target size, `g_Time`, and every engine value, laid out by
   `fxglsl.EngineLayout`. One uniform buffer serves every draw.
3. **Culling**: bounding spheres against the frustum planes (`_frustum_planes`, `_in_frustum`); in the viewport
   also parts under `SUBPIXEL` (0.75 px) on screen (`_subpixel`). The shadow mask only matters within the
   static map's range (150 m).
4. **Planar reflection** (`gamereflect`): mirror view of the reflection casters; far plane = fog end.
5. **Depth pre-pass, shadow maps, mask** (`gameshadow`, `_shadow_passes`). The view depth is kept for
   refraction and soft particles.
6. **Ambient buffer** (`ambientvol`): light volumes over the view depth (black without any).
7. **Opaque parts** in the game's order (`_game_order`, the `FUN_0087ac90` sort key: draw layer `RenderCaps &
   0x3F`, e.g. foam strips over the ocean). Then a copy of the opaque frame (`samColorBuffer`, what water
   refracts). Then **blended parts back to front**, crowds (alpha-tested sprites) and particles last.
   `_run_cmds` binds and draws.
8. **Post** (`gamepost`): exposure, tone map, bloom, motion blur (`_motion_state` keeps last frame's matrices),
   grade. The output goes to Blender as scene-linear, with depth so overlays sort against the world
   (`blit`; with Film > Transparent, alpha 0 where nothing was drawn). The auto-exposure measure (a read-back)
   runs at most every 0.12 s while the view changes, then once more when it stops (`_redraw_views`). On Blender
   4.1, which lacks `GPUShaderCreateInfo.depth_write()`, the post shaders use the older constructor
   (`gamepost.legacy_quad_shader`) so the depth write survives.

Textures that only exist per frame (`FRAME_TEXTURES`: shadow mask and maps, depth view buffer, colour buffer,
reflection, ambient buffer) are bound by name at draw time. Engine textures the editor doesn't produce
(`ENGINE_TEXTURES`) get constant stand-ins, except `environmentScrollMap`, which is the engine's own file.

## Final renders (`render()`)

F12 and Render Animation draw offscreen at the output size, then copy the pixels with `rect.foreach_set` (a
Python loop took 1.48 s a frame; `foreach_set` 64 ms). During Render Animation the commands are kept between
frames and refreshed incrementally, like playback. `_RENDER_JOB` plus the `render_init` / `render_complete` /
`render_cancel` handlers track the job (tokyo_b: 481 → 139 ms per frame).

## Characters and packages

Materials owned by a character (`mat.c2.owner`, Cars 2 Toolkit) and choreography packages resolve through
`_package` / `PACKAGES`. Without a world, a character uses neutral light (`NEUTRAL_LIGHT`, `NEUTRAL_SH`), its own
reflection cube, or `NEUTRAL_ENV` if the slot is empty. Preview-only overrides (`EYE_VALUES`, `ENV_OVERRIDE`,
`fxnodes.PREVIEW_VALUES`) only affect drawing ([SharedMaterialSystem.md](SharedMaterialSystem.md)).

## Adding a pass or a binding

- New per-frame textures go in `FRAME_TEXTURES` and are bound in `_run_cmds`.
- New engine values go in `fxnodes.ENGINE_VALUES`, and their slot comes from `EngineLayout`, which is built from
  the converted shaders' globals. Convert effects before filling the block (`_draw_scene` does this first).
- Run the game's own effect from bytecode when one exists (as the shadow mask and postprocess are), rather than
  writing GLSL by hand: then it matches the game by construction.
- Measure with `tests/bl_perf2.py` / `bl_guiplay.py` on tokyo_a and tokyo_b ([BlenderPerformance.md](BlenderPerformance.md)).
