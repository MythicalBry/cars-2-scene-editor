# Meridian in Cars 2 worlds: how the level logic is built, and how to edit it

This file covers the patterns the shipped Cars 2 Arcade worlds use, how to replay them without the game, and
editing recipes that were tested: export, re-import and replay, plus byte-identical round trips of untouched
worlds. File format and node layouts: [MeridianGraphFormat.md](MeridianGraphFormat.md).

All of this comes from the Cars 2 Scene Editor's analysis of the 19 vanilla Arcade worlds (`core/variants.py`,
`core/choreoplace.py`, `core/anchors.py`, `core/spawnsplit.py`). Where the game's own behaviour was confirmed
in game, the text says so.

---

## 1. What the game tells a world, and when

### 1.1 Startup order

1. **Level load.** Every `Catalyst` outside spawn groups fires: the world's initial state.
2. **The track.** The game broadcasts `TrackA` / `TrackB` / `TrackC` (the route being raced).
3. **The mode:**

   | Mode | Messages |
   |---|---|
   | Race | `Track<X>Race` |
   | Collect | `CollectMode`, `Track<X>Collect` |
   | Takedown (Attack) | `TakedownMode`, `Track<X>Takedown` |
4. **The race flow:** `EventStart`, `NewQuickStart`, `QuickStartBegin`, `StartCountdown`,
   `QuickStartLightThree`/`Two`/`One`/`Go`, `QuickStartRaceGo`, `CollectIntroStart`, `CollectStartRace`,
   `RacerFinishedLap` (per lap), `RacerFinished`.

These messages are sent by the executable, not by scripts. **Confirmed in `RaceManager` (`0x004f2800`):**
on its first update after the level loads, it sends `sprintf("Track%c")`, then that name plus
`Race`/`Takedown`/`Collect`/`Pursuit`, then `<Mode>Mode`. So load catalysts always fire **before** `Track<X>`.
Logic that must know the track has to hang off a `Track<X>` handler.

### 1.4 How a race ends (`sdaemon.exe`)

- **The lap spline** is the world's `.dbl` path named `TrackA`/`TrackB`/`TrackC` for the current track,
  falling back to `lane1`. An optional grid approach path is `<name>Start` (`0x004f5340`, and the
  `LapTracker` set-up).
- **Each car's `LapTracker`** works like this:
  - `pos = NearestDistance(spline, car) / Length` (0..1);
  - a lap counts when `pos` wraps forward (above 0.95 to below 0.05); wrapping backwards removes one;
  - `lapCount` starts at -1, and the first crossing sends `RacerStartingFirstLap`;
  - every later crossing sends `RacerFinishedLap`;
  - the car finishes when `int(lapCount + pos) >= LapCount`: it sends `RacerFinished` and the car switches to AI.
- **`RaceManager`** ends the race when the leading human's progress passes the lap count. There is also an
  Arcade 180-second timer.
- **`LapCount`** is the Arcade operator setting **Number of Laps per Game** (`save_settings[0x10]`), or the
  linked cabinets' count in multiplayer. It is the same for every track.
- **A graph can end a race with the message `EndTutorial`.** `RaceManager` registers a handler for it in its
  constructor (so in every race), and it calls `SetRaceState(5)`: results and `RM_RaceFinished`. Both tutorials
  end their race with it.
- `RaceManagerNode` (Meridian) only exposes `StartRace` (input), `LapFinished` / `RaceFinished` (events), and
  `CurLap` / `LapsLeft`. It can't end a race.
- **How a car is placed on the lap spline** (`CurveBSpline`, `NearestDistance`, `0x00dcd4c0`):
  - the car stays on its current control-polygon segment while its projection there is between 0 and 1 and
    within 20 m;
  - otherwise it tries the next segment, then searches every segment for the nearest one;
  - its position is the arc length at that point / the total length.

  Both wraps are therefore jumps of this projection, and they can be placed anywhere (core/p2p.py: point-to-point
  laps with the vanilla finish).
- **Point-to-point races with a message instead:** see the Scene Editor's Race Finish (finishpoint.py), End Message mode. It builds a `ProximityNode`
  (players 1 to 4 through `CarsPlayerHandle`, `ComparePos` from a locator via `TransformToVector3d`), and
  `Inside / Begin` → `MessageSender EndTutorial`. Air's luggage-chatter proximity uses the same pieces.

### 1.2 Mission worlds (`mi_*`)

Mission worlds have no track messages. Their mode comes from **`MissionGameType`**:

| Output | Mode |
|---|---|
| `ArenaCombatGame` | Arena |
| `CTFGame` | Bomb (Disruptor) |
| `HunterGame` | Hunter |

Each mode reads its own markup lists (names matching `Arena`, `Bomb|HiddenBomb`, `Hunter`).

### 1.3 Which worlds hold several tracks in one file

| File | Tracks | Plain `Track<X>` handlers |
|---|---|---|
| `italy` | A, B, C | A, B, C |
| `air` | A, B | A, C (the mode handlers cover A, B and C) |
| `oilrig` | A, B | A, B (the mode handlers cover A, B and C) |
| `london_a`/`london_b`, `radiator_a/b/c`, `tokyo_a/b/c` | one track per file | Their graphs still contain `TrackA`/`TrackB` branches, because the logic is shared between the files, but each file is its own world. |

**`ConsoleType`** gates platform logic: PC and Arcade run the `DX` output; the other platform outputs never fire
there.

## 2. Track and mode variants: showing and hiding

A world file holds the objects of every route. Routes differ in bridges, detour walls, barriers, sky domes,
gates and pickups. The route and mode handlers fire chains of `VisibilityToggle` / `CollisionToggle` nodes:

- A toggle acts on the objects fetched into the handler that was hit (`ActorFetch` / `InstanceFetch` links,
  fired once per element).
- Without such links, it acts on the objects carried down the chain: a `CollisionToggle` passes its objects to
  the `VisibilityToggle` it fires.
- `Gate` (`IsOpen`) and `Switch` (`IsOn`) hold state between messages.

**Replaying it.** Fire the catalysts, then the track message, the mode messages and the race flow, depth-first
in link order, and record each object's final visibility. Do that for every track × mode. An object counts as
mode-specific if **any** replay hides it: mission team bases appear only in their own mode.

## 3. Where choreographies play

A `Choreography` plays where its anchor slot points. The slot is `ChoreographyTransformReference`, with `Name`
= the package's anchor node, e.g. `ANC_xxx_00`, and `In` = a `TransformFetch`.

### 3.1 What can feed the anchor

- A locator or instance (`LocatorNode` / `InstanceNode`, by `BoundObjectName`).
- An actor (`ActorTransform`). A racer counts as the player: the editor shows it at the route's pole position
  (`Starting Grid A slot 1`, else `LOC_AStart`, else `PlayerCar1`).
- **A variable or list the logic fills just before playing.** Italy's lap-gate holograms and banners read the
  transform carried by the G2G channel `LapGate` (Gates / Start / Lap / Finish). The world fires that channel
  once per gate locator: a `TransformLoop` over `StartGateA1Scale ...`, per route.
- A spawned agent: inside a spawn group, an `ImpulseTransformSplitter` exposes the spawn position.
  `mi_oil`'s propane-tank explosions play at every locator a tank spawned at.

### 3.2 How the game places the package

- The world transform **replaces** the package's anchor (its `ANC` Transform data node).
- **Scale depends on its shape** (revised November 2026; see [RaceGatesAndBanners.md](RaceGatesAndBanners.md)):
  - A **uniform** scale is the level editor's gizmo size and is dropped. radiator_b's tumbleweeds sit on a 5×
    locator and roll at their own size, confirmed in game. Italy's yachts on 2× and 4× locators are
    characters, so the anchor's scale never reaches them either (next bullet).
  - A **one-axis** stretch is kept. Every race-gate locator is stretched along X only: tokyo
    `LOC_Gate_Banner_01` 1.99, `_02` 1.66, italy `StartGateA1Scale` 1.66, oilrig `LOC_FinishGate_B` 2.94,
    radiator_b `LOC_End` 2.45. That stretch sizes the 30 m `HoloGate_Elements` gate to the road. Confirmed
    against in-game footage of tokyo_b's start gate.
  - This rule is inferred from the data. The engine's transform-reference code
    (`Meridian::TransformReferenceNode`, vftable `01684b4c`) was not traced to the point where the scale is
    applied.
- Characters in the package ride `OffsetKinematicStateUpdate` (KinematicState), so the anchor's scale never
  reaches them either.
- With no anchor, the package plays where it was authored. Oilrig's Professor Z helicopter flyby is about 1 km
  out over the sea.
- **Static tracing is not enough.** Replay the logic (§6) and record the anchor value each time a
  `Choreography` fires. A choreography fired several times plays at each place: one hologram per gate.

### 3.3 Characters in choreographies (package side, for context)

- The package's `ReferenceFilePool` lists characters: `{SourceFile:'pacer', Namespace:'Pacer',
  BindingName:'characters/car/pacer', ActorName:'pacer_00', Flags:22}`. The bound actor is the car the game
  spawns.
- When a name exists twice, the main `characters/<name>` package wins: `pacer` and `gremlin` (Acer and Grem)
  also exist as cut-down copies in `characters/misc`.
- Animation comes from a `LayerBind` clip (`'Pacer:Choreography'` on `characters/car/car_choreography.ct`). The
  clip drives the character rig's **controls** (`<Bone>_Ctrl`, `Eyes_Ctrl/*` sliders), not its bones.

## 4. Spawned map objects (bashables, breakables, pickups)

Most track props are **spawned by the logic**, not placed in the `.oct`.

### 4.1 The two sources

**1. A template in the world `.oct`.**
- The model sits at the world origin. Examples: `ITY_Chandelier_whole`, `NeonShortWhole`, the hay bales.
- Its break pieces are also at the origin. They're named `<exp/piece_name>*` (e.g. `LrgWindowL_1..9`).
- The template is a `SpawnAgentNode` with `BoundObjectName` set to it.
- Its `.ap` section (matched by `MeridianActor_Uuid`) adds:
  - `MExploder` (break pieces, `velocityRange1..5` in cm/s, `FadeOverTime`, `UseRigidBodyPieces`,
    `InheritVelocity`, `SpawnUp`);
  - `CarsPickup` (`pickupType` 2 = bashable / energy, 3 = negative bashable / spin-out, `respawnDelay`,
    `surfaceType`);
  - `model_name=None` (it uses its own scene model).

**2. A model from `assets/objects`.**
- The agent's `.ap` section names it: `model_name=Objects\Bashable`, `Objects\Bashable_Neg`, `Objects\PickupMG`,
  `Objects\WeaponPickup` ...
- Each package (`objects/<name>.zip`) is a small scene: `.oct`, buffers, its own `.mer`, `.dbl`, and effects.

Rules that apply to both:
- **`.ap` section names aren't unique.** Two agents can both be `[Spawn Agent7]`. Always match by
  `MeridianActor_Uuid`; `ParameterSetName` holds the same UUID.
- **The game spawns a copy at each spawn transform.** The template at the origin is never shown.
- A graph with no sender spawns nothing. London's cone and topiary graphs exist in both `london_a` and
  `london_b`, but each file only fires its own channels.

### 4.2 The spawn chain (every shipped case has this shape)

```text
start ─Impulse─> [SetTransformList (Fetch ← locator, locator, ...) ─Write→ TransformList] ─Impulse─> TransformLoop(List)
TransformLoop.EachOut ─Impulse─> sink ;  TransformLoop.Value ─TransformFetch─> sink
```

What starts the loop:

| Start | Example | When it runs |
|---|---|---|
| A load-time `Catalyst`, often through `ConsoleType.DX` | Italy's harbour crates | Every track |
| Track handlers (`TrackARace`, `TrackBRace` → the same loop) | Italy's weapon spawns | The listed tracks |

What the loop feeds (the sink):

| Sink | What happens |
|---|---|
| A `SpawnGroupNode` | `EachOut` → the group, and `Value` → the agent's `Position` port. |
| A **`G2G_Output_Impulse_Channel`** (e.g. `DockCrates`, `ITY_WindowLeft`) | `Value` is fetched into the output node. On the other side: `G2G_Input` → `ImpulseTransformSplitter` → `SpawnGroupNode`, with the agent's `Position` fetched from the splitter's `Transform`. |
| An `Or` | Merges two chains. |

Some loops also feed a `TransformToFloat`, for the heading.

The locator usually enters through a `SetTransformList`'s `Fetch`. Some go straight into a loop's `List` port.

### 4.3 Placing them (as the editor does)

- Replay the logic and, whenever a `SpawnGroupNode` is hit, record for each of its agents the value of the
  agent's `Position` port, falling back to the impulse's transform.
- Show a copy of the model there, following the locator's position and rotation (no scale).
- Coverage, as distinct agent and spot pairs: Italy 1,345 (2,246 spawns over all track and mode replays),
  tokyo_b 898, radiator_b 561. These counts include spawner agents that have no model of their own.
- **Long lists:** weapon lists have 100+ entries. A replay that caps how often a node may run must allow at least
  a few hundred visits, or it drops entries. The editor's cap is 256 visits per node per replay.

### 4.4 Which mode a spawned object belongs to

Replaying each route with each mode message (`TrackBRace`, `TrackBCollect`, `TrackBTakedown`; Arena / Bomb /
Hunter in mission worlds) gives, per object, the set of modes that spawn it. Most objects are spawned in one mode
only:

| Mode message | In-game mode | Spawned only there (tokyo_b) |
|---|---|---|
| `TrackXRace` | Race and **Battle Race** | 174 weapon pickups, 6 props, at 32 spots |
| `TrackXTakedown` | **Attack** | 1,062 weapon pickups (PickupMG, PickupSJ, Pickup_Missile, Pickup_TriMissile, Pickup_SatBlitz), 330 bashables, at 342 spots |
| `TrackXCollect` | **Survival** | 456 pickups, 12 props, at 82 spots; plus the Collect gates and their 36 posts |

456 objects are spawned by more than one mode. In Italy 216 are shared; in mi_air (Arena 62, Bomb 252,
Hunter 6) 700 are.

**Battle Race is Race with weapons on.** No separate mode message exists. The world graph spawns the Race weapon
pickups on `TrackXRace` without a test. Nodes that differ between plain Race and Battle Race branch on
`WeaponsEnabledImpulseGate`, which reads the game setting (the Battle banner, for example). So the Race-only
weapon pickups are Battle Race's weapons.

The restart lists also name their mode: `TakedownTrackB`, `CollectTrackB`, `ArenaRedRestartLocations`,
`BombSpawnLocators`, `HiddenBomb...`, `HunterRestartLocations`.

## 5. Other `.ap` ↔ Meridian links

- Every `.ap` section with `AddComponent_MeridianAgent/Actor` names its graph in `MeridianNameSpace`, and its
  node by `MeridianAgent_Uuid` / `MeridianActor_Uuid`.
- **Walk-in triggers:** `AddComponent_WalkInTrigger` with `WalkInTrigger_Uuid` → a `NodeGroup` whose
  `ActorInside` surge fires on enter and exit. `ShowTrigger=0` means the trigger mesh is never drawn, and
  `Active` sets its initial state.
- **Markup lists** (`TransformMarkup`/`ActorMarkup`, read by name by game code) hold start grids, spawn and
  restart points, and AI spots. A locator joins a list through a `TransformFetch` into an entry port (a
  `SetTransformList`'s `Fetch`). Trace backwards from the markup's `DataList` to find the members.

## 6. Replaying a world's logic (the editor's simulator)

The model is good enough to reproduce visibility, choreography placement and spawn positions on every shipped
world:

- **Order:** catalysts → the track message → the mode messages → the race flow. Impulses run depth-first, in link
  order.
- **Transforms travel with impulses.** Sources: a `TransformLoop`'s `EachOut` (the current entry); a G2G
  output's own `TransformFetch`, which replaces whatever came in; a racer event (the player).
- **`SetTransform`** writes its `Fetch` value into what `Write` links to: a variable, or a list port (`Add to Top`
  inserts at the head; `Add to Bottom`/default appends; `Remove` deletes). `SetTransformList` copies a whole list.
- **Index/RandomTransformFromList** → the first entry.
- **`Gate`/`Switch`** hold state. **`ConsoleType`** takes `DX`. **`MissionGameType`** takes the run's mode.
- **G2G** matches `MessageName` + `Index`. A spawn group starts its members' catalysts per spawn.
- **`Count`** counts in every replay (added November 2026). Below `Hits Needed` it fires `Fewer`, on it the
  main output, past it `More`. `Hits Needed` is read through `IntFetch` links: a `Count`'s `Hits`
  (`CurHits` port) or an `ABArithmeticInt` `Result` (operators 0 +, 1 −, 2 ×, 3 ÷).
  - **The gate loop depends on this.** The VictoryCam graph spawns `HoloGate_Elements` once per gate: count A
    counts the gates pushed onto a list, count B needs `A + 1` hits, and each spawned gate's own catalyst hits
    B again. So B spawns until every gate stands. See [RaceGatesAndBanners.md](RaceGatesAndBanners.md).
  - **A `Count` hit again from what it set off** (re-entered while running) is queued and taken once the
    current chain finishes. Blocking it, as the old guard did, stopped the gate loop after one gate.
  - **A `Count` a short replay never completes** (mi_air's wild karts wait for several bomb hits) fires its
    main output once at the end of an untimed replay (`finish_counts`).
- **A `Switch` turning on fires the `Pulse` nodes on its `Out` surge once** in an untimed replay (the timed
  replay runs them on a clock). mi_air's Arena kart trains and luggage driver start this way.
- **Limits:** a node can't re-enter itself while it is running (except the queued `Count` case above); each
  node runs at most 256 times per replay; a replay stops after 300,000 steps.
- **Not modelled:**
  - timers and delays (fired immediately);
  - comparisons on game state (every branch passes);
  - live-race data such as lap counters and proximity. For those, the anchor is taken as the logic leaves it.

## 7. Editing recipes (tested)

The editor keeps graph edits as scene data and applies them at export to a **fresh copy of the source `.mer`**.
So an export with no edits is byte-identical, and undoing an edit restores identity.

### 7.1 Add a choreography

- Insert, in this order: `Event` Begin~, `Event` End~, `EventSurge` Playing, `Handler` Stop, `Handler` Restart,
  and the `Choreography` (all eight flags; `choreography_name` as `choreographies/<X>/<X>`).
- Add an `Impulse` from a start (a `Catalyst` or a trigger's `Begin`) to the node.

### 7.2 Add an anchor slot

- Insert a `TransformFetch "<R> / Transform"`.
- Then insert `ChoreographyTransformReference {Parent: ci, In, Name}`.
- Then add a `TransformFetch` link from a locator's `TransformSource` to the fetch.

### 7.3 Add a locator

- **`.oct`:**
  - a new `Transform` scene node;
  - append it to `SceneNodeRefsPool['MeridianSceneNodeRefs'].Nodes`;
  - add the association `{NodeName, Type:'Transform', SceneTreeNodeRef, SubNetworkRef:0}`.
- **`.mer`:** insert `InstanceSource "N / Handle"`, `FloatSource "N / Heading"`, `Vector3dSource "N / Scale"`,
  `Vector3dSource "N / Euler"`, `TransformSource "N / Transform"`, then `LocatorNode {SceneNodeIndex: position
  in that list, Handle, Heading, Scale, Euler, Transform, IsRootObject:1, AuthoredName:N, BoundObjectName:N}`.

### 7.4 Give one track its own copy of a shared spawn point

Use this in multi-track files (Italy, air, oilrig, and custom worlds built the same way).

1. Add a locator for the track (§7.3), e.g. `ITA_Boat_Loc_1_B`.
2. Insert a `TransformVariable` (`PreChange`/`PostChange` events, then the variable).
3. **Re-point in place** every link that carried the shared locator into the chain: change the
   `TransformSource → Fetch/List` link's `Origin` to the variable, and adjust both nodes' `NumOutLinks`. List
   order is unchanged.
4. Find the starts of the chain: walk impulse links backwards from the consumer to `MessageHandler`/`Catalyst`
   nodes.
   - **Handler of one track** (`Track<R>`, `Track<R>Race`, `Track<R>Collect`, `Track<R>Takedown`): splice in a
     `SetTransform` (`Write`/`Fetch` ports, `TransformFetch` from track R's locator, `TransformWrite` → the
     variable). The chain becomes handler → SetTransform → the original target.
   - **Track-independent start** (a load `Catalyst`): remove its link into the chain. For **every** track R of
     the world, add `MessageHandler Track<R>` → `SetTransform(R's locator)` → the original target. Tracks without
     their own copy write the original locator. Those objects now spawn when the track message arrives instead
     of at load.
5. Do it all by `Uuid`: each insertion shifts indices.

Result, verified by export and replay: the chosen track spawns at its locator in every mode, the other tracks
keep the original, and `meridian.validate()` reports no problems. Only `.oct` (the new node) and `.mer` change;
`.ap`, buffers and collision stay identical.

### 7.5 Rules of thumb for byte-identical round trips

- Match graph edits by `Uuid`, never by index.
- Re-point links instead of deleting and re-adding them.
- Insert at the end of block 0.
- Rebuild from the source file at every export, and keep everything you didn't touch in its original order.
- Floats and matrices must be the original values, not recomputed ones.
