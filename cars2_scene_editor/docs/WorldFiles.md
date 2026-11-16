# A world's files and what is in them

Examples are from `tokyo_b` (`vanilla_worlds/allw/tokyo_b/`). Detailed formats are in the research notes;
this page is the map.

## The package

| File | What | Read / written by |
|---|---|---|
| `<world>.oct` | the scene: tree, geometry, materials, textures, lights, probes, collision pools, track, sound, network | `c2oct`, `octscene`; `importer` / `exporter` |
| `<world>_0.vbuf` / `_0.ibuf` | vertex and index data the `.oct` streams slice | `vertexfmt`, `octscene` |
| `<world>.btm` / `.btd` | collision: metadata + load-in-place Bullet BVH data | `btcol` |
| `<world>.mer` | Meridian level logic: choreographies, triggers, locators, spawns, variants | `meridian` ([MeridianGraphFormat.md](MeridianGraphFormat.md)) |
| `<world>.ap` | actor properties: INI text, one `[section]` per actor (`CollisionBinding=False` ...) | `apfile` |
| `<world>.dbl` | GBWIN database: paths (lanes, lap splines, cameras), surface-type names | `dbl` ([GAMEPLAY_DATA.md](GAMEPLAY_DATA.md)) |
| `<world>.mtc` | not decoded; copied unchanged on export | — |
| `<world>.oct.textures/` | DDS files extracted at import (not a game file) | `importer.extract_textures` |

Related game data outside the world folder: choreography packages (`assets/choreographies/`), particles
(`assets/particles/<world>_d1..4.zip`, [Particles.md](Particles.md)), effects (`assets/effects/effects/*.tfx`),
Lua tables (`assets/luatables/`, [Crowds.md](Crowds.md)), characters (`assets/characters/`).

## Inside a world `.oct`

tokyo_b's pools:

| Pool | tokyo_b | What |
|---|---|---|
| `SceneTreeNodePool` | 4,915 | every node: `Scene` root, `Transform` 1,137, `Geometry` 1,164, `SubGeometryLit` 2,451, `SubGeometry` 95, `Light` 65, `Camera` 2 |
| `MaterialPool` / `TexturePool` | 235 / 362 | materials (effect, parameters, `TextureEntries`); textures (whole DDS files in `Data`) |
| `VertexStreamPool` / `IndexStreamPool` | 4,526 / 1,975 | slices of the buffers |
| `VertexBufferPool` / `IndexBufferPool` | 2 / 6 | the buffer files |
| `MaterialComponentPool` | 2 | texture animation: a list of textures for one slot, stepped by a network frame number |
| `LightRigPool` | 1 | `PrimarySceneRig`: order-3 SH (`LightRigConsts`) from 6 lights |
| `LightSetPool` | 1 | light sets parts reference (`LightSet` on SubGeometryLit) |
| `LightProbePool` | 50 | `Volume` probes (DirectSH grid, `Density`, fade, a cube texture) and environment probes |
| `CollisionShapePool` / `CollisionInstancePool` | 183 / 158 | shapes (TriangleList data in the `.btd`, primitives) and their placements (friction, restitution) |
| `CellPool` | 51 | visibility cells; `_REST_OF_WORLD_` is always drawn |
| `DisplayLayerPool` | 2 | `_cull_default` ... |
| `SoundSourcePool` | 144 | positioned sounds (`cheer_crowd_loop` ...); passed through |
| `ReferenceFilePool` | 4 | other files the world pulls in, each in a namespace (`tokyo_b_collect` → `Tokyo_B_Collect`) |
| `SceneNodeRefsPool` | 1 | `MeridianSceneNodeRefs`: the scene nodes Meridian may name, by position in this list |
| `Track*Pool` | 1 + 216 nodes + 224 links ... | the AI track graph ([GAMEPLAY_DATA.md](GAMEPLAY_DATA.md)) |
| `SubNetworkPool` | 1 | the world's network ([WorldSubNetwork.md](WorldSubNetwork.md)) |
| `AssociationPool` | 1,408 | name / index bindings between the pools (below) |

### Scene tree

Each node has `NodeName`, `Type`, `ParentNodeReferences`, `LocalToParentMatrix` (row-vector, composed as
`local @ parentWorld`) and often a `Uuid`. A `Geometry` node groups the parts of one mesh; each part is a
`SubGeometry(Lit)` child with one `MaterialReference`, vertex streams in the Geometry's local space (the part's
own L2P is identity in every shipping world), an index stream, `RenderCaps` (low 6 bits = draw layer) and a
`LightSet`. The importer makes one Blender mesh per Geometry with one material slot per part.

Arcade baked lighting: a part either uses a **lightmap** (effect `*_lmh`, `lightmapUV` stream, per-instance
`lightMap1` texture) or **vertex colours** (`*_vb`, `VertexBaked` stream). PC uses `ColorPrelight` colours
(`core/arcade.py` docstring, README "Baked lighting").

### Associations: how the pools find each other

| Type (tokyo_b count) | Binds |
|---|---|
| `Transform` (1,091) | a scene node ↔ the network data node `<NodeName>/Transform`; this is how Meridian and choreographies read and move placements |
| `CollisionShapeToCollisionInstance` (158), `CollisionInstanceToSceneTreeNode` (28) | collision shapes, instances and the nodes they follow |
| `CellLink` (51), `VisHierarchyLink` (51) | which nodes each visibility cell draws; **a node in no cell is never drawn**, so the exporter adds new nodes to `_REST_OF_WORLD_` and new meshes to the first VisHierarchyLink |
| `Geometry` (25) | geometry bound to the network |
| `MaterialComponentTextureAnimation`, `MaterialComponentRef_MaterialLink` (2 each) | an animated texture component and the material it drives |

Many bindings are **by name**. Renaming a node in Blender renames it in the file, which can unbind it from its
association, its network transform, or Meridian. `export_name` writes the imported name (`obj['c2_name']`)
unless the user really renamed the object. Blender's `.001` suffixes and its 63-character truncation don't
count as a rename.
