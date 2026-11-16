# Import and export pipelines

## Import (`importer.py`, `_import_scene`)

**File > Import > Cars 2 Scene (.oct)**. The original files are never changed; their paths are stored
(`scene.c2.oct_path`) so export can re-read them.

1. **Read** the `.oct` (`octscene`, `c2oct`) and the buffers. Detect the platform: Arcade (lightmaps / VertexBaked)
   or PC (prelight).
2. **Textures**: `extract_textures` writes each DDS to `<world>.oct.textures/` and fills `scene.c2.textures`.
3. **Materials**: `import_materials` / `make_material` give one Blender material per MaterialPool entry
   (`Material.c2` filled by `matio`; node tree from `matnodes` / `fxnodes`). Texture animations
   (MaterialComponents) are noted.
4. **Root**: an empty `C2_<world>` (`kind = 'ROOT'`), rotated so Y-up becomes Z-up.
5. **One object per scene node**:
   - Geometry → a mesh (`build_geometry_mesh`, parts as material slots, streams decoded by `vertexfmt`);
   - Light / Camera → lights / cameras;
   - everything else → empties.

   Each object gets `c2.kind`, `c2.node_index` and `obj['c2_name']`. The game's local matrix becomes the
   object's `matrix_basis` with an identity parent inverse, so local really means local. Matrices with shear are
   remembered.
6. **Fingerprints**: every mesh is hashed (`common.mesh_hash` → `o.c2.mesh_hash`); collision gets a signature
   (`scene.c2['col_sig']`). On export, unchanged means copied byte for byte.
7. **Lighting**: light sets, light rigs (SH), probes (`import_probe`).
8. **Collision** (`import_collision`): `.btm` / `.btd` via `btcol` into wireframe objects in the Collision
   collection (TriangleList meshes from the BVH data, primitives as meshes). Hidden by default.
9. **Meridian** (`import_meridian`): locators and instances are bound to their objects (`c2.meridian`,
   `mer_index`); choreographies are listed (`scene.c2.choreos`) and their places stored (`store_choreo_places`);
   triggers are linked; `.ap` sections are loaded into `scene.c2.actors` (`import_actors`).
10. **Gameplay** (`gameplay.py`): the AI track graph, markers, `.dbl` paths, nav meshes, spawn lists, grids.
11. **Map objects, crowds, Collect gates, mode collections**: `objplace`, `crowd`, `collectgates`, `modecols`.
12. **Tidy**: helpers marked (`mark_helpers`), geometry sorted into collections (`sort_geometry`), the Outliner
    collapsed. Global undo is off while building (faster on 5,000+ objects).

`import_scene` wraps it all, and `IMPORT_OT_cars2_scene` is the operator. Options: collision, Meridian,
textures.

## Export (`exporter.py`, `export_scene`)

**File > Export > Cars 2 Scene** writes a complete world folder. The source world is re-read and edited in
place; everything Blender doesn't model passes through.

1. **Refuse on missing textures.** `missing_texture_lines` lists every texture slot an effect samples that has
   no texture. `MissingTextures` stops the export with the list. Engine-bound slots (`renderedEyeTexture`,
   `environmentScrollMap`) are never required.
2. **Ownership.** Each object under the root claims its original node (`c2.node_index`); the first claimant
   keeps it (sorted so the original name wins). Duplicates and new objects need **new nodes**. Meshes not under
   the root, or without Cars 2 materials when *Only Meshes With Cars 2 Materials* is on, are left out and
   reported. Parents are processed before children.
3. **Existing nodes**: transform (written only when really moved), parent, name (`export_name`), visibility.
   **Deleted objects are tombstoned, not removed**: the Geometry is hidden and its parts made degenerate (one
   degenerate triangle in the old layout); lights are zeroed. No node index anywhere shifts, which keeps
   Meridian, associations, the network and cells valid.
4. **Materials and buffers** (`MaterialWriter`, `octscene` builder):
   - a mesh whose fingerprint still matches keeps its **original vertex and index bytes**;
   - an edited mesh is re-encoded in its SubGeometry's original stream layout (the element types the shader
     expects; `bake_group`, `weld`);
   - new meshes take the layout their material's effect needs;
   - material settings and new or replaced textures are written (`mw.apply`), and instance textures (lightmaps)
     are set per part (`set_instance_textures`).
5. **New nodes** are appended to the scene tree. They are added to the `_REST_OF_WORLD_` cell and the first
   VisHierarchyLink, because a node in no cell is never drawn.
6. **Lighting pools**: lights, light rigs and sets, probes (`write_light_pools`, `export_probe`; probe grids are
   resampled if their density changed).
7. **Collision**: if the signature still matches, the pools, associations and `.btm` / `.btd` are copied
   verbatim. Otherwise collision is rebuilt from the Blender objects (`export_collision`, `btcol.write_collision`;
   triangle meshes keep their BVH when their geometry hash matches).
8. **Meridian**: the `.mer` is re-read, and edits are matched **by UUID, never by index**, because inserting
   nodes shifts the indices of everything after the world's own block.
   - New locators get a `SceneNodeRefsPool` entry, a `Transform` association and a LocatorNode
     (`g.add_locator`).
   - Choreographies and their actors are written (`apply_choreos`, `add_choreo_actors`), along with gameplay
     spawn lists, per-track spawn splits (`objplace.export_splits`), the point-to-point finish
     (`finishpoint.export`) and edited crowd rows (`crowd.export`).
   - `meridian.validate` problems are reported, then `g.apply()`.
9. **Network sync** (`sync_subnetwork`): moved named nodes update their `<Name>/Transform` data node in the
   world's SubNetwork (Meridian reads placements through it); new locators get one, with header paths
   re-allocated ([WorldSubNetwork.md](WorldSubNetwork.md)).
10. **Gameplay pools**: the AI track and nav meshes (`gameplay.export_track`, `export_navmeshes`).
11. **Write** `.oct` (with the source's endianness), `_0.vbuf`, `_0.ibuf`, collision, `.mer`, `.ap` (plus new
    choreography actors' sections), `.dbl` paths. Every other `<world>.*` / `<world>_*` file not written by
    this export is copied, so the folder is a complete world.
12. **Report**: the `Cars2SceneExportReport` text block (meshes kept byte-exact / rebuilt / new nodes, every
    warning).

### Rules for keeping round trips byte-identical

- Re-read and patch; never re-serialise from Blender what wasn't edited.
- Compare against fingerprints taken at import (meshes, collision), not against "the user didn't click anything".
- Write a transform only when it really moved (`close()` with tolerance), and from the local basis.
- Keep pool entry naming (`Node#i`, `Association#i` ...) and list order; append, don't insert.
- Keep float types as floats (`flist`) and int lists as `IntList` (`c2oct` keeps types exactly).
- Match Meridian by UUID; keep names (`export_name`).
- Test every vanilla world (`tests/rt_all.sh`): import → export → compare bytes.

More recipes: [MeridianCars2WorldLogic.md §7.5](MeridianCars2WorldLogic.md).
