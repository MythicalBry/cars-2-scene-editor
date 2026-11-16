# Race gates, mode banners and lap counters

A race gate in Cars 2 Arcade is not one object. It is several choreographies the world logic plays at the
same gate locator, at different moments. This file lists the parts, how the logic places them, how the gate
is sized to the road, and the Count loop that spawns one gate per position. Based on tokyo_b's graph, and
checked on air, italy, london_a/b, oilrig, radiator_b and tokyo_c.

---

## 1. The parts

| Choreography | What it shows | When |
|---|---|---|
| `HoloGate_Elements` (`ANC_HoloGate30_00`) | the gate: blue hologram pillars, side flares, scanlines, purple hologram and lights. Skinned to bones `Flare1..4` and others; 30 m wide as authored | at every gate, looping |
| `Banner<Mode>`: `BannerRace`, `BannerAttack`, `BannerBattle`, `BannerSurvival` | only the floating mode icon (a 2-quad mesh on bones `<Mode>ModeB1/B2`) | at the start gate, one per mode, looping |
| `HoloGate_Start` (`ANC_HoloGateIcons`) | the 3-2-1 countdown and the GO flag | at the start gate, once |
| `LapGate1` … `LapGate10`, `SecondLap`, `BannerFinish` | lap counters and the final-lap banner | at the lap gate, chosen by lap comparisons |
| `HoloGateFinish_Fireworks` | the finish fireworks (particles and light volumes, as in `fireWorksA`) | at the finish |
| `HoloGate_LapFx` | the scan effect when a car passes | spawned by `HoloGate_Elements`' own logic, per player |

The pillars people see at the start line come from `HoloGate_Elements`, not from the banner. Preview the gate
and the banner together to see the start line as the game shows it.

## 2. How the world sends the gate positions (tokyo_b, Track B)

The world graph's `TrackB` message handler (`Message Handler - Impulse43`) drives a G2G channel named
`LapGate`, carrying a transform with each impulse:

| Channel | Sender | Carries |
|---|---|---|
| 1 `Gates` | `Transform Loop16` over a list | `LOC_Gate_Banner_02`, then `LOC_Gate_Banner_01` |
| 2 `Start` | the handler | `LOC_Gate_Banner_04` |
| 3 `Lap`, 4 `Finish` | the handler | `LOC_Gate_Banner_03` |
| 5 `LoopDone` | the loop's `After` | (none) |

In tokyo, `LOC_Gate_Banner_02` and `_04` stand at the same spot, as do `_01` and `_03`.

The receivers sit in the merged `VictoryCam` namespace (`G2G_Input_Impulse_Channel`, MessageName `LapGate`,
Index 1–6). Each goes through a `ConsoleType` node: the Arcade build takes the `False` output. Then:

- **Channel 2** → `ImpulseTransformSplitter` → `SetTransform` into a `TransformVariable` → the anchors of
  `HoloGate_Start` and, after `Delay - Impulse8` and a `Gate` per mode, `BannerAttack`, `BannerBattle`,
  `BannerRace` and `BannerSurvival`.
- **Channel 3** → a variable feeding the anchors of `LapGate1..10`, `SecondLap` and `BannerFinish`. Each
  plays when a `Comparison Float Impulse` on the lap count passes.
- **Channels 1 and 5** → the gate spawn loop below.

## 3. One gate per position: the SpawnGroup + Count loop

```
channel 1 (per gate) ─► Transform Write4 ─► TransformList "Add to Top"     (list of gate positions)
                                     └──► Count A ("Count - Impulse"): just counts the gates
channel 5 (loop done) ─► Count B ("Count - Impulse3"), Hits Needed ◄── AB Arithmetic Int: A.Hits + 1
Count B "Fewer" ─► Spawn Group2 ─► (spawned graph) Catalyst - Impulse3 ─┬► Transform Write5 (proxy):
                                                                         │   Random Transform From List → variable
                                                                         │   → HoloGate_Elements' anchor,
                                                                         │   and removes that entry from the list
                                                                         └► Count B again
```

With two gates, A = 2, so B needs 3 hits. Hit 1 (loop done) spawns gate 1; its catalyst is hit 2, which
spawns gate 2; its catalyst is hit 3, which reaches the target and stops. One `HoloGate_Elements` stands at
every listed position.

A replay must let **Count B be hit again while it is still running** (from what it set off), or it spawns
only one gate. The editor's replay queues such re-entries until the current chain finishes; see
[MeridianCars2WorldLogic.md §6](MeridianCars2WorldLogic.md). With that, the hologram stands at every gate, start
and finish, in air, italy, london_a/b, oilrig, radiator_b/c and tokyo_b/c.

## 4. Sizing the gate to the road

Gate locators are stretched along their X axis only:

| World | Locator | X scale |
|---|---|---|
| tokyo_b | `LOC_Gate_Banner_01` / `_02` | 1.99 / 1.66 |
| italy | `StartGateA1Scale`, `StartGateB1Scale`, `StartGateC1Scale` | 1.66 |
| london_a | `TrackB_FinishGate_LOC` | 1.66 |
| oilrig | `LOC_Start_Gate_B` / `LOC_FinishGate_A` / `LOC_FinishGate_B` | 1.99 / 1.66 / 2.94 |
| radiator_b | `LOC_End` | 2.45 |

`HoloGate_Elements` is authored 30 m wide (its anchor is `ANC_HoloGate30_00`, and its mesh bounding box is
32 m × 8.4 m). At 1.66 it spans about 50 m, the width of tokyo's start straight. That matches in-game footage,
with the pillars at the road edges past the barriers.

**Uniform** scales are different. radiator_b's tumbleweeds (`RAD_TWeedsA`) sit on a 5 × 5 × 5 locator, but
their skinned path spans 378 × 510 m and must match the track; in game they roll at their own size. So the
rule the editor uses is: **keep a non-uniform anchor scale, drop a uniform one.** It is inferred from the
data. Where `Meridian::TransformReferenceNode` (vftable `01684b4c`) applies the scale was not traced.

Practical consequence for tools: when one choreography plays at several gates with different stretches, a
copy can't be expressed as one placement relative to another. The relative transform would need a shear,
which object transforms can't hold. Build each placement separately.

## 5. Collect-mode gates are something else

The Survival gates of Collect mode are `CollectGate` nodes sized between two post locators by game code
(`CollectGameInf`, `FUN_00567d70` / `FUN_0050c3a0`), using the `SurvivalGate_Elements` choreography. They belong
only to the Collect mode of their tracks (`TrackAGate` / `TrackBGate` / `TrackCGate` flags).

What the game does with them (traced in `sdaemon.exe`; the editor's copy is `core/collectline.py`):

| Step | Code | Detail |
|---|---|---|
| read a gate | `FUN_00567d70` | flags (`Lap1/2/3`, `LapStart`, `Alternate`), pickup spacing, the posts' middle and `post1 - post2`, height = post 1's Y scale (5 without) |
| set it up | `CollectGameInf::Update` 0x506300 | lap progress = the game's lap progress (0x004f5420) at the middle on TrackA/B/C (the order within a lap); crossing radius² = (0.5 × 1.2 × post distance)²; plane through the middle, normal `cross(post1 - post2, up)` |
| walk the line | same, the loop over the collect spline | every 10 m; a gate is crossed between two samples on opposite sides of its plane when the nearer is within the radius (height counted ×2). The lap gate sets the lap of what follows: under 1/6 of the line lap 1, under 1/2 lap 2, else 3. Other crossings store two ring positions ±0.1 m along the line and the pickup start |
| draw | `FUN_0050c3a0` | per player and lap: `SurvivalGate_Elements` at the middle on `GetGroundHeight`, `ANC_SurvivalGate`, scale (distance / 30, height / 5, 1); two `Objects\Survival_GateTarget` at ground + 3.5, scale (2, 2, 1), turned by `atan2` of the post direction. The lap gate gets neither |

Laps of the collect line are cut where it passes its track's lap gate. On all 16 shipping collect lines that
gives three laps, and 259 of 260 gates get rings on exactly their flagged laps. The one exception is on
radiator_a's leftover `radiatorsprings_CollectLineTrackA`, which the game doesn't use.
