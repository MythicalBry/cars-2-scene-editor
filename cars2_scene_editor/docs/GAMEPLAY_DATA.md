# Cars 2 Arcade: gameplay data in a world

How a world's spawns, AI paths, race progress and arena nav meshes are stored.
This was worked out from the shipping Arcade worlds and `sdaemon.exe` (with Ghidra).
`sdaemon` addresses are given so that each claim can be checked.

Status key: **decoded** means read and write were checked against every shipping world.
**understood** means the meaning is clear but there is no writer yet.
**open** means it is not decoded yet.

| System | Lives in | Status |
|---|---|---|
| AI track graph (nodes, links, sections, racing lines, hints) | `.oct` `Track*` pools | decoded: `core/track.py` rebuilds all 13 race worlds exactly |
| Lap progress / race position | `.dbl` B-spline `TrackA`/`TrackB`/`TrackC` (fallback `lane1`) | decoded: `core/dbl.py` rebuilds all 20 `.dbl` files byte-for-byte |
| Paths (lanes, victory cams, bridges, collect lines, follower splines) | `.dbl` polylines + B-splines | decoded |
| Surface type names | `.dbl` string section | decoded |
| Starting grid | `.mer` `StartingGrid` node + one locator | decoded (slot formula) |
| Grid order / which cars race | `xml/gameprogression.xml` | understood |
| Arena nav meshes | `.oct` `NavMeshPool` blobs | decoded: `core/navmesh.py` rebuilds all 41 meshes byte-for-byte, and can rebuild adjacency and the octree |
| Nav mesh triangle edge flags | nav mesh blob | open (kept as-is) |
| Nav mesh bridges | `.mer` `NavmeshBridge` + `.dbl` `NavMesh_Spline_*` | understood |
| Spawn / restart / hiding locations | `.mer` `TransformMarkup` / `ActorMarkup` lists of locators | understood (uses the existing locator support) |
| Attack waves (arena) | `xml/carshunterwaves.xml` | understood |
| Takedown / collect / AI hint settings | `.mer` nodes | understood (plain node fields) |
| Pickups and spawned props | `.ap` actor templates + `.mer` spawn groups | understood |
| Checkpoints | none in Arcade | see below |

---

## 1. AI track graph (`.oct`)

The pools are registered by `FUN_0040d400` (`AITrack::cTrack`).
The loader reads them in pool order, so custom-data pools must come before the pools that use them.

- **`TrackPool[0]`** holds counts and `Default*` values.
  Any field missing from a node, link or section takes its default, so the exporter writes only values that differ.
  It also holds the constants `ID_COLLECT_TRACK_A/B/C` = 0/1/2 and `TYPE_*`.
- **`TrackNode`** fields:
  - `Position`, and `Rotation` as three rows: across, up, forward.
    Checked: row 2 points along the outgoing link (mean dot product 0.996).
  - `Width` (default 32).
  - `FastLineU` and `EnergyLineU` for tracks A, B and C. These are the two racing lines as a fraction across the road: 0 is the `+across` edge, 1 is the `−across` edge.
  - Optional `CustomDataIndex` pointing to `TrackNodeCustomData`: `EndOfRaceMarker`, `SpeedLimit`, `LookAheadDistance`, `IgnoreOtherPlayers`.
  - The game computes the edges as `pos ± across·width/2` (`FUN_0043d0e0`).
- **`TrackLink`** fields:
  - `Node1Index → Node2Index`, in the direction of travel.
  - `StraightawayLength`.
  - `AvoidanceBitfield`: 9 bits (511 = all). Probably 9 lanes across the road; usage is not traced yet.
  - `TrackLinkCustomData`: `ForceDrifting`, `DriftLeft`, `BashZone`, `CanRespawn`, and `Fast/EnergyLine Hop/TurboV` per track. The `Hop/TurboV` values are signed −1..1 offsets; 100 means unset.
- **`TrackSection`**: a run of links between branch or merge nodes.
  - `BranchName`, plus `TrackSectionCustomData`: `Valid`, `OnFastLine` and `OnEnergyLine` per track; `SplitChance`, `SplitMinSpeed`, `EnergyPath`, `WeaponPath`, `InTheZone/HighPerformanceShortcutPath`, `OnlyIfOnRacingLine`, `LimitToBestAI`, `NeedsTurbo`, `ClosedForOffTrackCheck`.
  - **The game rebuilds sections itself** (`FUN_00433b50`, `FUN_0043e690`):
    1. It walks the links in pool order.
    2. Each link that has no section yet gets the next section record.
    3. That section grows back and forward to the nearest branch or merge.
  - `FrontIndex` (the run's first link) is written by the exporter but never read.
  - `Track.section_runs()` reproduces `FrontIndex` exactly in every world.
- **`TrackSplineMarkup`** fields:
  - `ID`: which collect track, 0/1/2.
  - `Type`: 0 bunny hop, 1/2 sidestep left/right, 3/4 drift-left start/end, 5/6 drift-right start/end. Type 7 appears 8 times and is open.
  - `Position`, and `Distance` along the lap.

Editing rules (`core/track.py`):
- After adding or removing links, call `commit()`. It rewrites counts, key order, pool order and `FrontIndex`.
- Keep `sections` in run order.
- `set_custom()` shares identical custom-data records, as the exporter did.

## 2. Race progress and resets

- **Lap progress:** the game (0x004f5340) takes the `.dbl` B-spline named `TrackA`, `TrackB` or `TrackC` for the chosen track, falling back to `lane1`. Progress is `NearestDistance / Length` (0x004f5420).
  **A new track needs that spline.** Without it there is no lap or position logic.
- **Checkpoints:** Arcade has none.
  - `ArcadeManager` registers an `ArcadeCheckpointEntered` handler (0x00450670), but the handler is an empty function (0x004559f0).
  - The only sender of `Arcade_CheckPoint_Passed` is the debug menu's "Pass Checkpoint" button (0x00443df0).
  - No world, script or XML file mentions either name.
  - So in Arcade, "checkpoints" are lap progress along the spline, plus the end-of-race marker on the AI graph (`EndOfRaceMarker`, used in air and oilrig).
  - The Cars 2 console `CheckpointSystem` / `CheckPointCollection` nodes still exist in the exe but no Arcade world uses them.
- **Resets:** the game (0x004fb480) picks a locator from its reset list by lap fraction (+20 m ahead, up to a 250 m window). The worlds feed it with `TransformMarkup` lists such as `ResetTrackB`. `TrackResetOverride` nodes adjust the reset angle test.

## 3. `.dbl` (GBWIN container), `core/dbl.py`

The `.dbl` holds three sections:

- **0x03:** polylines. Each is a 64-byte name, a point count, a closed flag and float3 points.
  Meridian `SplineNode` and spline followers sample these.
- **0x10:** cubic B-splines with the same names. Each record holds:
  - flags (`degree | 0x20` when periodic),
  - control-point and knot counts, then 5 zero words,
  - the control points, the knots, and the name.

  `PathCollection` serves these. The shipping curves are refits that have fewer control points than the polylines.
  `Dbl.set_path()` writes both representations, using the polyline as control points. That gives a slightly smoothed copy, which is fine for lap progress.
- **0x83:** surface type names. Collision triangles index this list.

## 4. Starting grid

- The `StartingGrid` node holds `Track`, `RowCount` (default 6), `HorizontalSpacing` (6), `VerticalSpacing` (8), `RowStagger` (0) and `CustomGrid2-3`/`CustomGrid3-2`.
- It has ports `GridPosition` and `CollectGridPosition`, each fed by one locator (`LOC_StartA`, `LOC_StartB`).
- Slot positions come from `FUN_0056c9a0` and are mirrored in `track.grid_slot()`:
  - The column count is `12 / RowCount`, clamped to at least 1 and at most the number of racers.
  - A slot's row is `slot / columns` and its column is the remainder.
  - The back offset is `−row × VerticalSpacing`.
  - The side offset is `(columns − 1) × H / 2 ± stagger / 2 − column × H`. The stagger is added on even rows and subtracted on odd ones.
  - The custom 2-3 and 3-2 options use alternating 2- and 3-car rows.
- **Who sits where:** `GameProgressionManager` reads the grid-position columns of the event's row in `xml/gameprogression.xml` (sheet `Cars`). That row also holds the AI car count, the AI skill ranges and the per-car overrides.
  In Race mode the player is placed in slot 0, 4 or 9 for Easy, Medium or Hard. In other modes the player is in slot 0.

## 5. Spawn points (player, NPC, pickups, props)

Every spawn location is a scene locator collected into a named list.

1. A locator in the `.oct` scene tree (`locator_B01` …) is wrapped by a `.mer` `LocatorNode` (`SceneNodeIndex` → `SceneNodeRefs`).
2. Its `Transform` port links to the Read port of a `SetTransformList`.
3. On an impulse, the list is written into a `TransformList`.
4. A `TransformMarkup` with `MarkupName = X` publishes it.
5. Game code reads it with `DataMarkupNode` (`GetMarkup`, name `X`, index `i`).

| Markup | Used by |
|---|---|
| `SpawnRabbit`, `SpawnRam`, `SpawnSpotter`, `SpawnHenchmen` | MinionManager (arena attack NPCs). Spawns are triggered by `HUNTER_Spawn_*` messages, built from wave `spawnCode`s |
| `RabbitHidingPlaces`, `HelicopterPoints`, `BossSpawnLoc` | arena AI behaviour |
| `HunterRestartLocations` | player start/restart in Hunter (indexed by player number, 0x00519c80) |
| `Arena{Red,Blue,Yellow,Green}RestartLocations`, `Bomb{Red,Blue}RestartLocations` | team restarts |
| `BombSpawnLocators`, `HiddenBombLocations` (`ActorMarkup`) | bomb mode |
| `TakedownTrackA`, `CollectTrackA/B`, `ResetTrackB` | race-world takedown spawns, collect mode, resets |

**To add an attack-NPC spawn point:**
1. Add a locator.
2. Add a `LocatorNode` for it.
3. Link its Transform port to the Read port of the `SetTransformList` that feeds the markup (`SpawnRam`, …).

The add-on can already create locators and LocatorNodes (`meridian.Graph`, `exporter.sync_subnetwork`). Only the link-to-markup step is new.

**Pickups and props:**
- A `SpawnAgentNode` or `SpawnActorNode` is a proxy for an `.ap` template of the same name (`Spawn Agent30` is a `CarsPickup` weapon box; `Spawn Actor16` is a prop with `model_name`).
- A `TransformLoop` over a locator list fires the owning `SpawnGroupNode` once per entry, passing the transform to the proxy's `Position` port.
- `SpawnGroupKiller` / `KillAll` remove the spawned actors. `SpawnInstanceNode` spawns scene instances (`useCompleteTransform`).

**Waves** (`xml/carshunterwaves.xml`, one sheet per arena, chosen by `HunterGame.waveSetName`):
- Each row is `wave, spawnCode, MaxEnemies, 1P-4P threshold, WaveStartTime, TimerStartSecs`.
- `spawnCode` is a list of codes: `rbt` Rabbit, `rm` Ram, `sp` Spotter, `hnch` Henchmen, `prz` Prof Z, `exrm` exploding ram; `T:n` waits n seconds and `D:n` delays.
- Sheet `AIspecs` maps each code to its spawn message and its score.

**Race-world NPC settings:**
- `TakedownSpawnSettings`: per-level kills and weapon mix, for Mission and Custom events.
- `CollectSettings` / `CollectGate`: gate posts, lap flags and pickup spacing.
- `AIHint`: `AIHintType` (BunnyHop, …), speeds, chances, per-track flags and `SectionName`.
- `CarsAIHandle`: `Mode` such as "Race (1st highest skill)".

## 6. Arena nav meshes, `core/navmesh.py`

There is one `NavMeshPool` entry per walkable island (17 in mi_london). Each `Data` blob holds:

1. **Header:** offsets, triangle and vertex counts, the root cube (centre + half size), and a 64-byte name.
2. **Triangles**, 16 bytes each: a flags word, `neighbour[3]` (`0xFFFF` = open edge), and `vertex[3]`. Edge e runs from `vertex[e]` to `vertex[e+1]`.
3. **Vertices**, as float3.
4. **Octree**, depth-first, 36 bytes per node:
   - An internal node is 0 followed by 8 child offsets.
   - A leaf (`0x80000000`) holds a triangle list and its open-edge list, given as (index into the leaf's list, 4 + edge).
   - Child i's centre is `centre + OFFSET[i] × half / 2`, where bit 0 = −x, bit 1 = −y and bit 2 = −z (table filled in by `FUN_01581c70`).
   - Shipping leaves have at most 16 triangles, and the tree is at most 8 levels deep.
5. All the leaves' triangle lists, then all their edge-triangle lists, then all their edge lists.

Checked:
- Re-packing is byte-exact on all 41 meshes in the 6 arenas.
- `rebuild_adjacency()` reproduces the shipping neighbour tables.
- `rebuild_tree()` makes a valid tree. It is not byte-identical, because it uses a box overlap test, which lists a few extra triangles per leaf.

Still open:
- The flags word is a bitfield that the loader reorders (`FUN_00db77e0`).
- Only three 1-bit-per-edge fields are ever set (file bits 13-11, 10-8 and 7-5, on 3% of edges).
- They are not open-edge or bridge markers: I tested both.
- They are kept unchanged on edit, and new triangles get 0.

**Bridges:** a `NavmeshBridge` node (`Path`, `MinSpeed`, `MaxSpeed`, `MoveType`) links two islands through a `.dbl` spline (`NavMesh_Spline_CtrBrdge01_InOut`). AI uses `CarsNavMeshDriver` / `NavMeshFollower`.

## 7. What is still open

- What nav mesh edge flags mean (search the exe for the reordered bits 18-26 once Ghidra's code index finishes).
- `TrackSplineMarkup` type 7.
- How `AvoidanceBitfield` is used.
- `HunterGame` / `ArenaCombatGame` scoring internals (only the node fields are known).
- The Lua in `ai/ai.zip` (`goldstardata.lua`, `meridianmissionnodedata.lua`) is not reviewed.
