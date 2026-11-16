# Scene Editor architecture (for developers)

This page is for people changing the Scene Editor's code. The research notes in this folder describe the game;
the README describes features. Here: how the add-on is put together, where its state lives, and the rules the
code keeps.

## The core idea

The editor **never writes a world from Blender data alone**. Import reads the world's files into Blender objects
and tags each one with the record it came from. Export **re-reads the original files from disk** and patches
them with what changed in Blender. Everything the editor doesn't model passes through untouched: sound sources,
cells, most of the subnetwork, track pools it didn't change, the `.mtc`.

That is why:

- **an untouched world exports byte-identical** (the round-trip test, `tests/bl_rt.py`, on all 21 worlds);
- the `.blend` only has to store what Blender can't (`props.py`);
- a new feature can model one more piece of a world without touching the rest.

See [ImportExport.md](ImportExport.md) for the two pipelines.

## Layout

```
cars2_scene_editor/
  __init__.py      bl_info, core/ on sys.path, register order, the looklink hand-over
  props.py         every property group stored in the .blend
  common.py        coordinate conversion (g2b / b2g), mesh fingerprints, fast lookups, assets_dir_from
  importer.py      File > Import > Cars 2 Scene (.oct)
  exporter.py      File > Export > Cars 2 Scene
  ops.py           scene / collision / lighting / Meridian / actor / check operators
  scenetools.py    custom scenes: Replace World Geometry, auto-assign materials, Delete World, fast Outliner delete
  bakeops.py       lightmap and vertex-light baking (Cycles), PC prelight
  gameplay.py      AI track graph, AI markers, .dbl paths, nav meshes, spawn lists, grids, track preview drawing
  objplace.py      spawned map objects (bashables, breakables, pickups), per-track spawn splits
  finishpoint.py   point-to-point race finish (lap spline)
  collectgates.py  Collect mode: gates (animated, on the ground), rings, the collect line per lap, gate editing
  crowd.py         grandstand crowds (CrowdMemberPlane sprites)
  modecols.py      Battle Race / Attack / Survival collections
  trackvariant.py  show one route (A / B / C) and mode
  preview.py       choreography preview (bakes the animation onto the timeline)
  charrig.py       animated characters in previews, through the Cars 2 Toolkit
  particles.py     choreography particles (Pigpen) in the Game renderer
  ambientvol.py    ambient light volumes (SubGeometryAmbient)
  ui.py            3D View > Sidebar > Cars 2 Scene
  -- the shared material system (also shipped in the Cars 2 Toolkit's look/ folder) --
  looklink.py      which add-on's copy is registered
  props.py (Material.c2, Object.c2tex part), matio.py, meshdata.py, fxnodes.py, matnodes.py, matops.py, lookui.py
  gamerender.py    the Cars 2 Game render engine; gameshadow.py, gamepost.py, gamereflect.py, gameeyes.py
  data/effect_list.json   the Pick Effect list (tests/build_effect_list.py)
  core/            pure Python, no Blender: every file format and the shader translators
  docs/            research notes and these developer pages
```

### `core/` (no Blender)

| Module | Reads / writes |
|---|---|
| `c2oct` | Tupperware (`.oct`, `.mer`, `.tfx`, `.bent`), lossless |
| `c2headers` | SubNetwork header paths ([WorldSubNetwork.md](WorldSubNetwork.md)) |
| `octscene` | the world scene: pools, geometry, buffers, rebuilding |
| `vertexfmt` | vertex and index stream codec (NumPy) |
| `btcol` | collision `.btm` (metadata) + `.btd` (load-in-place Bullet BVH data) |
| `meridian` | `.mer` graph editing ([MeridianGraphFormat.md](MeridianGraphFormat.md)) |
| `apfile` | `.ap` actor properties (INI text) |
| `dbl` | `.dbl` GBWIN container (paths, surface names) |
| `track` | AI track graph and starting grids |
| `navmesh` | Arena nav meshes |
| `p2p`, `spawnsplit`, `variants`, `anchors`, `choreoplace` | finish splines, per-track spawns, route / mode variants, anchor tracing, logic replay |
| `collectline` | Collect mode's line: lap cuts, the game's gate crossings (rings), lap order |
| `choreo`, `clipeval`, `c2clipblock` | choreography packages, clip evaluation |
| `pigpen`, `luatable` | particles; compiled Lua data tables (crowds) |
| `shlight` | order-3 spherical harmonics for light rigs |
| `arcade`, `d3dbc`, `fxglsl`, `fxgraph`, `cubemap`, `dds` | Arcade specifics, shader bytecode → GLSL / node graphs, cube maps, DDS |

The modules are imported as **top-level** names (`import c2oct`), with `core/` first on `sys.path`. Blender
does not reload them when the add-on is re-enabled or updated. `__init__.py` therefore pops every name in
`_CORE_MODULES` (and every `cars2_scene_editor.*` submodule) before importing. **Add every new core module to
`_CORE_MODULES`**, or an update keeps running its old copy until Blender restarts.

## Register order and the shared material system

`register()` first asks `looklink.owner()` who runs the material system. If the Cars 2 Toolkit's copy is
registered, it is released (the toolkit waits). Then every module registers in `_modules` order (props first,
because everything else uses the property groups). Finally `looklink.claim('scene_editor', ...)` runs, and
`charrig.remember_core()` notes where the toolkit's core is. On unregister, a waiting toolkit takes over again.
The full story is in [SharedMaterialSystem.md](SharedMaterialSystem.md).

The Scene Editor is the **master copy** of the shared files. After changing any of them, run
`python tools/sync_look.py` (the toolkit's copy is then rewritten) and check with `--check`.

## Coordinates (`common.py`)

The game is Y-up with row-vector matrices (`v' = v @ M`, translation in row 3); Blender is Z-up with column
vectors. An imported world hangs under one empty, the **root** (`kind = 'ROOT'`, `scene.c2.root`), rotated +90°
about X. Every object below it uses the game's own numbers locally, so `g2b` / `b2g` only transpose a matrix and
no axis swapping happens anywhere else. Keep it that way: code that needs world positions multiplies by the
root's matrix once.

Matrices are written back from the object's **basis** (local), not `matrix_local`, which Blender derives from
world matrices and loses precision. A matrix with shear (which loc/rot/scale cannot hold) is remembered at import
so an untouched object keeps the file's matrix.

## Where the state lives (`props.py`)

| Group | On | Holds |
|---|---|---|
| `Object.c2` | every imported object | `kind` (ROOT, NODE, GEOMETRY, LIGHT, CAMERA, COLLISION, COLSHAPE, PROBE, AI_TRACK, AI_MARKER, PATH, NAVMESH, PREVIEW), `node_index` / other index fields back to the source record (**-1 = new, create it on export**), `meridian` (NONE / LOCATOR / INSTANCE) and `mer_index`, marker and probe settings |
| `Mesh.c2col` | collision meshes | shape type (TriangleList, Box, Sphere, Cylinder, ConvexHull, Capsule, Cone), surface |
| `Light.c2` | lights | the game's light fields |
| `Material.c2` | materials | effect, parameters, texture slots, owner (shared system) |
| `Scene.c2` | the scene | `oct_path`, `world_name`, `root`, `platform` (PC / ARCADE), `assets_dir`, `export_dir`, `namespace`; `has_mer` / `has_ap` / `has_col` / `has_gameplay`; textures, light rigs, light sets, choreographies, actors (`.ap`), grids, spawn lists, node settings; viewport and Game-renderer settings; track variant / mode; finish settings; track preview |

`kind = 'PREVIEW'` objects (grid slots, track edges, choreography previews) are display-only and never exported.
Preview objects live in their own collections outside the root.

Functions named `_xxx_update` are Blender property update callbacks (live viewport updates). Keep them cheap:
they run on every slider drag.

## Collections

The importer sorts a world into collections ([README](../README.md#layers-what-each-collection-holds)): geometry
by kind, Collision, Lights, Locators, Gameplay, Map Objects, mode collections. Collections are for the user;
**the exporter never decides anything from collection membership**. It uses `Object.c2` and parenting under the
root, so users can reorganise freely.

## Rules the code follows

- **Byte identity is the regression test.** Any change to import or export must keep every vanilla world's
  round trip identical (`tests/rt_all.sh`). Vanilla worlds in `vanilla_worlds/` are read-only references.
- **No game patches.** Everything goes through data the stock game reads.
- **Never shift an index other files use.** A deleted node is tombstoned, not removed; Meridian edits match
  nodes by UUID, never by index ([ImportExport.md](ImportExport.md)).
- **Speed on low-end machines.** Worlds have 5,000–10,000 objects. No per-frame scans of `bpy.data.objects`;
  `scene.objects.get` is O(N); `Object.children` is slow; `bpy.data.objects.remove` costs ~4.4 ms per call, so
  use `batch_remove`. Measured traps and fixes: [BlenderPerformance.md](BlenderPerformance.md).
- **Undo recreates data-blocks.** Anything that caches Blender data registers `undo_post` and `redo_post`
  handlers and copes with `ReferenceError`.
- **Preview-only values are never exported** (eye values, emission previews, probe overrides, choreography
  previews).
