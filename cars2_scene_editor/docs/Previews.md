# Choreography previews: how the code works

The README ("Previewing choreographies") covers use. This page follows the code path from list item to baked
timeline.

## Pieces

| Module | Does |
|---|---|
| `core/choreo.py` | finds a package (`find_choreography`: folder or `.zip` under `assets/choreographies/`), loads it (`Choreography`), evaluates its network over time ([WorldSubNetwork.md](WorldSubNetwork.md#choreography-networks-corechoreopy)) |
| `core/clipeval.py` | evaluates ClipDataBlocks at any time |
| `core/anchors.py` | static tracing: from the transform an anchor fetches to a placed object (`resolve` → `('object', name)`, `('player', reason)` or None) |
| `core/choreoplace.py` | dynamic tracing: replays the world's Meridian logic for one route and mode and records the anchor transform every time a Choreography node fires (lap gates placed once per gate locator, the start / finish ...) |
| `core/variants.py` | route / mode replay: Catalyst nodes, then TrackA/B/C and mode messages; toggles show and hide objects |
| `preview.py` | builds the preview scene and bakes keyframes |
| `charrig.py` | animated characters through the Cars 2 Toolkit |
| `particles.py`, `ambientvol.py` | particle bursts and light volumes, drawn live (never baked, never exported) |

## `build_preview(context, item)`

1. **Package**: `choreo.find_choreography(assets, name)` → `choreo.Choreography(pkg)`. Its warnings go into the
   report.
2. **Placement**: the package's anchor (`ANC_xxx_00`) goes where the game puts it, using `_played_at` →
   `choreoplace` / `_graph_anchor` / `_actor_anchor` (a locator, an actor or the spline it follows, a variable
   filled at run time, the racer that fired it). Without one, it plays where it was authored. A holder empty
   (`PREVIEW_<tag>`) carries the placement. Places whose scale an instance can't carry get a second copy
   (`_copy`).
3. **Scene**: the package's meshes (game materials, textures extracted to
   `<world>.oct.textures/choreographies/<pkg>`), cameras and empties, in a `PREVIEW_` collection marked
   `c2_preview` (outside the world root, never exported).
4. **Armatures**: for package bones (`_rig`), and for each referenced character (`_character_model`; a box proxy
   if the model is missing).
5. **Sampling**: every frame up to `max_frames`, keying transforms, bones, visibility, focal length and material
   values (`bake`, `bake_matrices`, `_bake_params`) into F-curves. Playback is then plain Blender animation:
   cheap, scrubbable, and visible in Eevee too.
6. **Characters**: if the Cars 2 Toolkit is installed, `charrig` imports the character with the toolkit's rig
   (controls, constraints, wheel spin, eye drivers), drops the toolkit's meshes, skins the preview's own game
   meshes to the toolkit's bones (same names and rests), plays the LayerBind clip on the controls with the
   toolkit's clip importer, and keys the eye material parameters frame by frame. Without the toolkit,
   `_layer_pose` moves the bones the clip reaches directly.

`clear_preview` removes the preview collections and restores the frame range. **Preview All** (with
Play-on-Start) builds every choreography the current route and mode plays at load (`keep=True`).

## Two-part rigs (`charrig.py`)

The toolkit matches skin bones to the network's joints at one offset, voted over all bones. The Zundappcopter
breaks that: Professor Z (36 bones) stands 3 m from his bind pose on the copter's platform, while the copter's
joints sit on theirs. The fixes are:

- `wire_unmatched` wires bones the toolkit left alone to the control their own joint follows (the network's
  UberConstraint);
- `rebase_followers` puts follower bones where the network has their joint at rest. Z's controls face the door,
  a quarter turn from his bind pose;
- `follow_rig_bones` hangs bones with no control (Z's `Body` → `Body_Rig`).

On ordinary characters these change nothing.

## Performance notes

Preview meshes bent by armatures update through `_redeform` in the renderer (deform-only), and the renderer's
`mesh_users` map avoids name lookups. Choreography playback numbers and fixes are in
[BlenderPerformance.md](BlenderPerformance.md).
