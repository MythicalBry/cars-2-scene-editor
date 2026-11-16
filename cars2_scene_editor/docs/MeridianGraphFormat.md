# Meridian graphs in Cars 2: file format, loader rules and runtime

Meridian is the Octane Engine's visual scripting system. Each world's level logic lives in `<world>.mer`, next
to `<world>.oct` / `.ap` / `.dbl` in `assets/worlds/<world>.zip`. That logic covers race start and finish,
track and mode setup, cutscenes, spawned props and pickups, triggers, and HUD and FX cues. Character and object
packages in `assets/characters` and `assets/objects` carry small `.mer` graphs of their own.

Companion file: [MeridianCars2WorldLogic.md](MeridianCars2WorldLogic.md) covers how the shipped worlds use
these pieces, and tested editing recipes.

Sources. These are marked where it matters:

| Mark | Source |
|---|---|
| **[RE]** | Reverse engineering of `sdaemon.exe` (Cars 2 Arcade) in Ghidra, from the supplied reference document. Addresses are given in Appendix A. |
| **[TOOL]** | The Cars 2 Scene Editor's own reader, writer, simulator and exporter. Verified byte-identical on all 19 vanilla Arcade worlds, with logic replays checked against in-game screenshots. |

---

## 1. Container and tooling

- A `.mer` file is an Octane **Tupperware** tree, the same container as `.oct`. Little endian on PC and Arcade.
- `offsetting oct decode <in.mer> <out.json>` and `offsetting oct encode <in.json> <out.mer>` convert it
  without loss. `offsetting zip <folder> <out.zip>` repacks a world. offsetting has **no Meridian schema**: it
  doesn't know which integers are node references or how link counts work. Editors must enforce §3 and §7
  themselves.
- A decode and re-encode is byte-identical [RE][TOOL]. The Scene Editor's `core/c2oct.py` reads and writes the
  tree directly. It keeps the root key (`MeridianOct`) and the typed empty lists, which byte identity needs.
- **`Uuid`** is 16 raw bytes in the binary file. offsetting's JSON shows it as a string.
- **`<world>.mtc`** only pre-sizes frame heaps. When a heap bin fills, the engine falls back to the general
  allocator, so adding or cloning nodes never requires editing `.mtc` [RE].

## 2. Pools

```text
MeridianNodePool              MNode#0..N-1             every node AND every port
MeridianNodeAssociationPool   MNodeAssociation#0..K-1  optional (absent when empty, e.g. frontend.mer)
MeridianLinkPool              MLink#0..M-1             impulses and data between ports
```

- The `#k` suffix only keeps the JSON keys unique. **An element's index is its 0-based position in its pool**
  [RE].
- The node pool must be numbered sequentially, because references are positions.
- The loader creates each element from `"Type"`, using the creator registered for
  `MNode::<Type>`, `MLink::<Type>` or `MNodeAssociation::<Type>`.
- Cars 2 registers **271 MNode types**, **20 MLink types** and **1 MNodeAssociation type** (`Collection`)
  [RE]:

  | Registered by | Types |
  |---|---|
  | Engine core | 49 nodes, 7 links, 1 association, and `NameSpaceBegin`/`End` |
  | GameCommon | 129 nodes, 13 links |
  | Cars 2 | 93 nodes |

### 2.1 Namespace blocks

Each pool is split into blocks bracketed by `{"Type":"NameSpaceBegin", ...}` and `{"Type":"NameSpaceEnd"}`. The
three pools have the same blocks in the same order.

- **Block 0 is the world's own graph.** Its `Name` is the `MeridianNameSpace` that the world's `.ap` sections
  use (e.g. `Italy`) [TOOL].
- **Later blocks are merged reference graphs**, e.g. `Italy_FX`, `TokyoXtreme_BSH`,
  `Radiator_A_Collect:Radiator_A_Collect`.
- `NameSpaceBegin` fields the engine reads [RE]:
  - `Name`;
  - `ReferenceEmbeddedNamespace` (embedded sub-graphs);
  - `IsMerged` (1 = merged into the level's main graph);
  - `GraphVersion` (summed into the master);
  - `FreezeState` (default 0).
- **Links never cross blocks** [TOOL]. Graphs talk to each other with messages and G2G channels (§6.1).

## 3. References and the single-pass order rule

### 3.1 All references are global node-pool indices

Every reference is a **global 0-based index into `MeridianNodePool`**. That covers `Parent`, `Group`, a node's
port fields, a link's `Origin`/`Target`, and an association's `Owner`/`Collector`. All of them resolve through
one lookup in the engine [RE]. Inside a block the engine re-bases indices by the block's first node index [TOOL], but the stored values are always global.

Consequence: **inserting K nodes at position P shifts every stored reference ≥ P by +K**. That includes
references in later blocks, links and associations. The Scene Editor inserts new nodes at the end of block 0
and shifts everything after them [TOOL].

### 3.2 Order rule: what a node references must come before it

Nodes are created and resolved in one forward pass. A reference to a node that hasn't been created yet
resolves to NULL, and serialization fails [RE]. So for every node `u` that references `v` through `Parent`,
`Group` or a port field, `index(v) < index(u)`:

| Relationship | Order |
|---|---|
| A static port claimed by an owner field (`"Open": 101`) | port **before** owner |
| An `EventSurge`'s `Begin`/`End` events | events **before** the surge |
| A dynamic port (`Parent` + `LeafName`, on NodeGroup / Spawn* nodes) | owner **before** port |
| A child sub-object (`Parent`, no `LeafName`), e.g. `ChoreographyTransformReference` | parent **before** child (the child's own ports come before the child) |
| A spawn-group member (`Group` + `IsProxy`) | `SpawnGroupNode` **before** member |

Links and associations are resolved after all nodes exist, so they have no ordering constraint. Shipped files
always write a node's static ports immediately before it, which lets a tool find an owner from a port by looking
a few entries ahead [TOOL].

### 3.3 Unclaimed nodes are destroyed

A node survives loading only if it is [RE]:

- a root (`IsRootObject: 1`);
- a spawn-group member (`Group`);
- a `NodeGroup` or `Spawn*Node`;
- a child (`Parent`);
- or claimed as a port by another node's field.

Anything else is deleted at the end of the load. **New root nodes need `IsRootObject: 1`.**

### 3.4 Telling reference fields from plain integers

JSON has no reference type. These rules hold with zero exceptions across all shipped Cars 2 graphs, about
242,807 nodes [RE][TOOL]:

- **Never references:**

  | Group | Fields |
  |---|---|
  | Counts and flags | `NumInLinks`, `NumOutLinks`, `IsRootObject`, `IsProxy`, `Serialize` |
  | Namespace fields | `GraphVersion`, `FreezeState`, `IsMerged` |
  | Node parameters | `CompType`, `Operator`, `SceneMode`, `SceneType`, `RowCount`, `IsOpen` (Gate), `IsOn` (Switch), `loop` |
  | Scene index | `SceneNodeIndex` (see §5.3) |
  | Per-type values | `IntFetch.Value`, `IntVariable.Value`, `CarsPlayerHandle.Player`, and every `G2G_*_Channel.Index` |
- **Always references:** `Parent`, `Group`, and any other integer field whose value points to an earlier node in
  the same block that is a port type (§4.2). Equivalently, the target's `AuthoredName` starts with
  `"<owner AuthoredName> /"`.
- Inside a later block, a reference always stays inside that block. The only integer value fields that can land
  in that range are the ones listed above [TOOL].

## 4. Nodes, ports, links, associations

### 4.1 Common node fields

| Field | Meaning |
|---|---|
| `Type` | Registered class name (`Gate`, `Choreography`, `Event` ...). |
| `Uuid` | 16-byte id. Unique per node and per link. Other files refer to nodes by it: the `.ap` uses `MeridianActor_Uuid`, `WalkInTrigger_Uuid`, `CarsPickup_Uuid` and similar keys. |
| `AuthoredName` | Display name. Roots look like `Gate - Impulse3`. Ports are `<Root> / <Port>`. Surge sub-events are `<Root> / <Surge> / Begin~`. Not unique. |
| `IsRootObject` | `1` on user-placed top-level nodes. Absent on ports and sub-objects. |
| `Parent` (+`LeafName`) | With `LeafName`: a dynamic port of that owner. Without it: a child sub-object. |
| `LeafName` | The port's name on its owner (`Activate`, `ActorInside`, `Transform`, `Orientation` ...). |
| `NumInLinks` / `NumOutLinks` | The **exact** number of links targeting / leaving this node. Used to pre-size arrays. Written only when non-zero. Keep them right after every edit. |
| `IsProxy` + `Group` | Template member of a `SpawnGroupNode` (§6.4). |

### 4.2 Ports are nodes

Every input and output of a node is its own `MNode`. There are 31 port types in total:

| Kind | Types | Count |
|---|---|---|
| Impulse input | `Handler` | 1 |
| Impulse output | `Event` | 1 |
| Surge output | `EventSurge`, which owns `Begin`/`End` `Event`s | 1 |
| Data inputs | `ActorFetch`, `ActorListFetch`, `EntityFetch`, `FloatFetch`, `InstanceFetch`, `IntFetch`, `MotionSplineFetch`, `SplineFetch`, `TransformFetch`, `TransformListFetch`, `Vector3dFetch` | 11 |
| Data outputs | `ActorSource`, `FloatSource`, `InstanceSource`, `IntSource`, `MotionSplineSource`, `SplineSource`, `TransformSource`, `Vector3dSource` | 8 |
| Write outputs | `ActorListWrite`, `ActorWrite`, `FloatWrite`, `IntWrite`, `SplineWrite`, `TransformListWrite`, `TransformWrite` | 7 |
| Writable targets | `ActorListWritable`, `TransformListWritable` | 2 |

- An unlinked `*Fetch` input uses its own `Value` as a constant default.
- To show a graph the way the editor does, group each port with its owner, then draw owners as cards (Appendix B).

### 4.3 Links

There are 20 link types:

| Kind | Types | Direction |
|---|---|---|
| Execution | `Impulse` | `Event` (or a root's default output) → `Handler` (or a root's default input) |
| Execution | `Surge` | `EventSurge` → `Handler` / root |
| Data fetch (11) | `ActorFetch` ... `Vector3dFetch` | **always Source → Fetch**: the provider is `Origin`, the consumer is `Target`, even though the data is pulled |
| Data write (7) | `ActorListWrite`, `ActorWrite`, `FloatWrite`, `IntWrite`, `SplineWrite`, `TransformListWrite`, `TransformWrite` | `*Write` port → `*Writable` port or a variable node |

- **`IsElementFetch: 1`:** the fetch link targets a **root node** directly. When an impulse or surge reaches
  that node, the engine pulls each element-fetch link's source and passes it as the payload. For example, an
  `ActorSource` feeds a `SetActorReferenceNode` this way [RE].
- The same mechanism drives toggles: an impulse into a `VisibilityToggle` handler acts **once per object fetched
  into that handler** (`ActorFetch` / `InstanceFetch` links) [RE][TOOL].
- Some links carry `IsProxy: 1`. These are links inside spawn-group templates.

### 4.4 Associations

The only association type is `Collection`: `{"Type":"Collection","Owner":<stateful node>,"Collector":<ResetCollection>}`.
When a `ResetCollection` node receives an impulse, it calls `Reset()` on every owner (a `Count`, `Gate`,
`ActorList`, `Once` ...) and then fires its output [RE].

## 5. Common node anatomy (field layouts as shipped)

The ports are listed in file order. Each owner comes right after its ports.

### 5.1 Logic

| Node | Layout |
|---|---|
| `Catalyst` | `{Serialize: 0, IsRootObject: 1}`. Fires once when its graph starts: at level load, or when a spawn group instantiates the graph it belongs to. |
| `MessageHandler` | Port `Sender` (`ActorSource`), and `MessageName`. Fires when that message is broadcast. `MessageSurgeHandler` is the surge version. |
| `MessageSender` | `MessageName`. Broadcasts the message to every graph and to C++ listeners (`CMessageDispatcher`). |
| `Gate` | Ports `Open`, `Close`, `Toggle` (Handlers), `Out`, `Blocked` (Events). **`IsOpen`** sets the initial state. (The reference document's example `"Open": 1` is a port reference, not the state.) |
| `Switch` | Like `Gate`, with ports `Turn On` / `Turn Off` / `Toggle` and initial state `IsOn`. |
| `Count` | Ports `Fewer`, `More`, `Reset`, `HitsNeeded` (`IntFetch`), and the hit count (`IntSource` named `Hits`). Every impulse adds a hit, and the count keeps going past `HitsNeeded`. It fires `Fewer` below the target, its main output at exactly the target, and `More` above it [RE]. |
| `Repeat` | `Count` (`IntFetch`). Fires `Repeated` Count times, then `After`. |
| `BatchImpulse` | Fires its main output, then `Second Out` and any further outputs, in order. |
| `ComparisonImpulse{Int,Float,Actor}`, `ComparisonSurgeFloat` | Ports `A`, `B`, `True`, `False`. `CompType`: 0 `==`, 1 `!=`, 2 `<`, 3 `>`, 4 `<=`, 5 `>=`. |
| `ABArithmetic{Float,Int,Vector3}` | Ports `A`, `B`, `Result`. `Operator`: 0 `+`, 1 `-`, 2 `*`, 3 `/`, 4 `%`. |
| `ConsoleType` | Outputs `Xbox360`, `Wii`, `PS3`, `DX`, `FalseOut`. Routes by platform: **PC and Arcade take `DX`**. |
| `MissionGameType` (Cars 2) | Outputs `ArenaCombatGame`, `CTFGame` (the Bomb mode), `HunterGame`: the mission worlds' mode branch. |
| `ResetCollection` | The collector of `Collection` associations (§4.4). |

### 5.2 Variables, lists and loops (transform family; the actor family mirrors it)

| Node | Layout |
|---|---|
| `TransformVariable` | Ports `PreChange` and `PostChange` (Events). Readers link **from the variable node itself** (`TransformFetch`). A writer links into it (`TransformWrite`). |
| `SetTransform` | Ports `Write` (`TransformWrite`) and `Fetch` (`TransformFetch`, the value read). On an impulse it copies the value into whatever `Write` is linked to, then fires its output. |
| `TransformList` | Written through `TransformListWritable` ports such as `<List> / Add to Top`, `Add to Bottom` and `Remove`. Read with `TransformListFetch`. |
| `SetTransformList` | Ports `Write` (→ a list) and `Fetch` (`TransformListFetch`, **one TransformFetch link per entry**, in link order). An impulse replaces the list's contents. |
| `TransformLoop` | Ports `List`, `EachOut`, `After`, `Empty`, `Value` (`TransformSource`: the current entry). On an impulse it fires `EachOut` once per entry, then `After`, or `Empty` if the list has no entries. |
| `IndexTransformFromList` / `RandomTransformFromList` | Pick one entry of the list port `List`. |
| `ImpulseTransformSplitter` | Ports `In` (Handler) and `Transform` (Source). Exposes the transform that arrived with the impulse. Inside spawned graphs this is the spawn position. |
| `TransformCombiner`, `TransformToFloat` | Combine transforms / extract components (e.g. heading). |
| `TransformMarkup` / `ActorMarkup` | A named list that game code reads by name (`MarkupName`) through `DataMarkupNode` (`GetMarkup`): start grids, restart points, AI spots. `DataList` is the list port feeding it. |

### 5.3 Things in the world

| Node | Layout |
|---|---|
| `LocatorNode` | Ports `Handle` (InstanceSource), `Heading` (FloatSource), `Scale` and `Euler` (Vector3dSource), `Transform` (TransformSource). Fields `SceneNodeIndex`, `BoundObjectName`, `IsRootObject`. A placed point taken from the `.oct`. |
| `InstanceNode`, `SplineNode` | Like `LocatorNode`, for scene instances and `.dbl` splines. Both have `BoundObjectName`. |
| `ActorTransform` | The transform of an actor handle (a racer, a spawned agent). |
| `CarsPlayerHandle` | Port `Handle` (ActorSource). `Player` is **0-based** (0 = player 1). |
| `PlayerCarFinished` | Ports `CarHasFinished`, `ActorV1`, `PlayerId`. **`PlayerId` is 1-based.** |
| `StartingGrid` | `GridPosition` (`TransformFetch`, usually from a `LOC_Start*` locator), `RowCount`, plus spacing and stagger. |
| `SetActorReferenceNode` / `ActorReferenceNode` | Publish / read an actor in a global table by `Namespace` + `Name` (e.g. `quickstart` / `playa`). Choreographies and other graphs read it. Often fed with an element fetch. |
| `VisibilityToggle`, `CollisionToggle` | Ports `Turn On`, `Turn Off`, `Toggle`. The objects they affect are the ones fetched into the handler that was hit (§4.3). A `CollisionToggle` passes its objects on to the toggle it fires. |
| `NodeGroup` (walk-in trigger) | Dynamic ports (`Parent` + `LeafName`): `ActorInside` (EventSurge with Begin/End events), `ActorInside.Who`, `Activate`, `Deactivate`, `Toggle`. The `.ap` links it to a mesh with `AddComponent_WalkInTrigger` and `WalkInTrigger_Uuid`. |
| Cameras (`CoolCamera1`, `EyeCamera`, `FreezeCamera`, `TrackCinemaCamera`, `AnimatedCameraNode`) | `SceneMode`: 0..3 = player 1..4 view, -1 = the view of the actor carried by the impulse, -2 = every view of `SceneType` (0 primary, 1 secondary / picture-in-picture), -3 = shared-screen lock [RE]. |
| Per-player events (`PlayerSuspensionEvent`, `PlayerCollisionEvent`, `ConditionalPlayerEvent`) | Keep fixed 4-entry per-player arrays [RE]. |

**`SceneNodeIndex` is indirect [TOOL].** The value is an index into the `.oct`'s `SceneNodeRefsPool` entry named
`MeridianSceneNodeRefs`, whose `Nodes` list holds the actual scene-tree node indices. A new locator appends its
scene node to that list and uses the new position. The `.oct` also needs a `Transform` association
(`{NodeName, Type:'Transform', SceneTreeNodeRef, SubNetworkRef:0}`).

### 5.4 Choreographies

```text
Event   "<C> / Playing / Begin~"
Event   "<C> / Playing / End~"
EventSurge "<C> / Playing"   {Begin, End}
Handler "<C> / Stop"
Handler "<C> / Restart"
Choreography {Playing, Stop, Restart, choreography_name: "choreographies/X/X",
              loop, visible_before, visible_after, enableAsyncLoad, enableMusicSync,
              hide_gameplay_elements, use_master, fullScreen_CHO, IsRootObject, AuthoredName}
  children (Parent = Choreography):
  TransformFetch "<R> / Transform"
  ChoreographyTransformReference {Parent, In, Name: "ANC_xxx_00"}            anchor slot
  ChoreographyActorReference / ChoreographyMirror / ChoreographyVisibleForActor / TransformChoreography
```

- An impulse into the node itself plays the choreography. `Restart` replays it, and `Stop` stops it.
- `Playing` is a surge, active while the choreography plays.
- `.cho` timeline callbacks can broadcast messages into Meridian (e.g. `QuickStartActorCallback`).
- How anchors are placed: see the companion file, §3.

### 5.5 Spawning

| Node | Layout |
|---|---|
| `SpawnGroupNode` | Ports `KillSpawn`, `KillID`, `KillAll`, `SpawnKilled`, `SpawnID`. An impulse spawns one instance of its member template, at the transform the impulse carries. |
| `SpawnAgentNode` (`IsProxy: 1`, `Group`) | `BaseActorName`, `BoundObjectName`. Dynamic ports after it: `<A> / Position` (`TransformFetch`, `LeafName: Transform`), `<A> / Heading` (`FloatFetch`, `LeafName: Orientation`), `<A> / Orientation` (`TransformFetch`, `LeafName: NewOrientation`). The `.ap` section with `MeridianActor_Uuid = <its Uuid>` gives the agent its components. |
| `SpawnActorNode`, `SpawnInstanceNode` | Same idea, for actors and instances. |
| Template members | Every node with `Group = <SpawnGroupNode>` and `IsProxy: 1`, including its `Catalyst`s and `Choreography`s. Each spawn runs its own copy of the members, so a member `Catalyst` fires once per spawn. |

### 5.6 Cross-graph channels

- **`G2G_Output_Impulse_Channel` → `G2G_Input_Impulse_Channel`:** matched by **`MessageName` + `Index`**, in any
  block [TOOL].
- **Transforms travel with the impulse.** A `TransformFetch` linked into the **output node itself** supplies the
  transform. On the receiving side, an `ImpulseTransformSplitter` exposes it.
- **Several outputs can send on the same channel.**
- Float and spline variants: `G2G_*_Impulse_Float_Channel`, `G2G_*_Impulse_Spline_Channel`. There is also
  `G2G_Output_Impulse_Channel_Targeted`.

## 6. Runtime semantics

### 6.1 Signals

- **Impulses are synchronous and depth-first.** Firing an `Event` immediately runs each linked handler in link
  order, and each of those runs its own outputs before returning [RE][TOOL]. A node with several out-links
  therefore fires them in `MeridianLinkPool` order.
- **Surges** carry a span of time, with `Begin`/`End` events: a choreography playing, a trigger occupied, a
  comparison surge being true.
- **Data is pulled.** A node reads its `*Fetch` ports when it runs: the linked source, or the port's own `Value`.
  A source reports its owner's *current* value, e.g. a `TransformLoop`'s `Value` is the entry being iterated.
- **Messages** (`MessageSender` → every `MessageHandler`/`MessageSurgeHandler` with that `MessageName`, in all
  blocks) connect graphs to each other and to C++. C++ systems register listeners by name, e.g. `CarsAIManager`
  listens for `ShowOff` and `StartFinn`. The game itself broadcasts the track and mode messages
  (companion file, §1).
  `RaceManager` listens for `ForceRaceStart`, `EventStart`, `EndTutorial` (ends the race in any world) and
  `ToggleRainFX`.

### 6.2 Data payloads

An impulse can carry a payload (a `DataElement<T>`): from element-fetch links (§4.3), from the racer that tripped
a trigger, or from a G2G channel's transform. Nodes downstream read it through splitter nodes
(`ImpulseTransformSplitter`, `ImpulseHandleSplitterNode`) or ports named `Who` / `Sender` / `Instigator` /
`Racer` / `Player`.

## 7. Editing checklist (what a tool must do)

1. **Order.** Write ports first and the owner last. Write dynamic ports, children and group members after the
   node they point to (§3.2).
2. **Shift indices.** Inserting nodes shifts every reference at or above the insertion point: node fields,
   link `Origin`/`Target`, association `Owner`/`Collector`, in **all** blocks. While editing, refer to nodes by
   `Uuid` and resolve indices again after each insertion.
3. **Fresh ids.** Every new node and link gets a fresh 16-byte `Uuid`.
4. **Link counts.** Keep `NumInLinks`/`NumOutLinks` exact. Drop the field when the count reaches 0, as shipped
   files do.
5. **Roots.** Give new top-level nodes `IsRootObject: 1` (§3.3) and an `AuthoredName`. Name ports
   `"<Owner> / <Port>"`; other tools find owners by that prefix.
6. **Links stay in their block.** Insert new links inside the target block's link section.
7. **Keep order.** Re-point an existing link in place (change its `Origin`) rather than deleting it and adding a
   new one. List entries and fan-out order follow link order.
8. **Validate.** Check index bounds, the ordering rule, `Uuid` uniqueness, and that every `Collector` is a
   `ResetCollection`. The Scene Editor runs `meridian.validate()` on every export.

---

## Appendix A — `sdaemon.exe` addresses [RE]

| What | Address |
|---|---|
| graph load / descriptor (.mtc) read | `0x00d19670` / `0x00d19f10` |
| `GraphInstance` frame heaps / node allocation | `0x00d1b510` / `0x00d1b790` |
| `GraphInstance` namespace begin | `0x00d1a500` |
| `ObjectLoader` serializer / creator registration | `0x00d1bd60` / `0x00d1bcb0` |
| serializer registration (common / game) | `0x010092b0` / `0x0055ca90` |
| `ReadSerializer` pool read / element creation | `0x008cc4a0` / `0x008c75e0` |
| `OctFile` element fetch / create | `0x008c5100` / `0x008c4880` |
| Node-reference lookup (all 7 call sites) | `0x00d1ea50` |
| node read / pin claim / object read | `0x00d1d1a0` / `0x00d1d420` / `0x00d1e840` |
| link read (Origin / Target) | `0x00d1cb80` / `0x00d1cbb5` |
| Impulse fire with element fetches / surge | `0x00d1dcb0`, `0x00d1dc40` / `0x00d1f600` |
| Unclaimed-object destructor | `0x00d1c410` |
| `Count` / `Gate` / `ResetCollection` | `0x00d23bb0` / `0x00d25f40` / `0x00d30de0` |
| `NodeGroup` after-read | `0x00d20e30` |
| Pool ids (node / association / link) | `0x0186fb88` / `0x0186fb8c` / `0x0186fb90` |

## Appendix B — Grouping ports into cards (reference algorithm)

1. For each `EventSurge`, record its `Begin`/`End` events.
2. Every reference field of a node (§3.4, excluding `Parent`/`Group`) claims the target as a static port of that
   node. Also claim the targeted surge's events.
3. Every node with `Parent` + `LeafName` is a dynamic port of its parent.
4. Every node not claimed above is a **card**: a root or a sub-object.
5. Map each link's endpoints to their cards to get card-to-card edges. A `Collection` adds a Collector → Owner
   edge.
6. Walk the card edges backwards and forwards from a card to get its upstream and downstream logic.

The Scene Editor does the same thing, using `port_owners()` (by `AuthoredName` prefix) and owner-from-port by
position (§3.2) [TOOL].
