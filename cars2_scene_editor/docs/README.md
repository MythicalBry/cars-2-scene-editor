# Cars 2 Arcade research notes

These notes record what was learned about Cars 2: The Video Game (Arcade build) while building the Cars 2
Scene Editor. They describe file formats, the Meridian level logic, and engine behaviour taken from
`sdaemon.exe`. They are meant for anyone writing their own tools.

- Excerpts from the game's code are Ghidra decompilations of the Arcade `sdaemon.exe`, trimmed and renamed
  for reading, with their addresses so you can find them yourself.
- Shader excerpts are from a recompiled-to-match HLSL reconstruction of the shipped effects.
- Where a claim was confirmed in game, the text says so. Where a rule is inferred from data rather than
  traced in code, it says that too.

## Files and formats

| Document | Covers |
|---|---|
| [MeridianGraphFormat.md](MeridianGraphFormat.md) | The `.mer` Meridian graph: pools, references, node, port and link layouts, runtime signals, editing checklist, `sdaemon.exe` addresses |
| [GAMEPLAY_DATA.md](GAMEPLAY_DATA.md) | AI track graph, lap splines (GBWIN `.dbl`), starting grids, spawn and restart points, Arena nav meshes, race end |
| [EffectNameGrammar.md](EffectNameGrammar.md) | How the `a_1_…` / `part_…` effect names encode a shader's features, and engine behaviour the names don't show (sRGB textures, Gamma parameters), and which texture slots and parameters a material lists for its effect |
| [Particles.md](Particles.md) | The Pigpen particle `.dbl` (emitters, effects, systems: every field named), particle materials, and how a choreography starts particles (ParticleEffect, ParticleEmitter, tickers, EventLists) |
| [Crowds.md](Crowds.md) | Grandstand crowds: CrowdMemberPlane layout (the engine's algorithm), `crowdmanagerinfo.lua`, the sprite atlas, and reading the game's compiled Lua 5.1 tables |

## Level logic and choreographies

| Document | Covers |
|---|---|
| [MeridianCars2WorldLogic.md](MeridianCars2WorldLogic.md) | How shipped worlds use Meridian: what the game sends at start-up, track and mode variants, where choreographies play (anchors, placement, scale), spawned map objects, which mode spawns what (Battle Race = Race with weapons on), replaying a world's logic without the game, editing recipes |
| [RaceGatesAndBanners.md](RaceGatesAndBanners.md) | What a race gate is made of (HoloGate_Elements, Banner\<Mode\>, HoloGate_Start, LapGate*), the `LapGate` G2G channels, the SpawnGroup + Count loop that places one gate per position, and gate sizing |
| [SharedMaterialSystem.md](SharedMaterialSystem.md) | The material system the Scene Editor shares with the Cars 2 Toolkit: which copy runs, whose textures a material uses, characters in Game Look (the engine's environment scroll map from `sdaemon.exe`, probe reflections, the neutral light, live eyes), the slot survey of every world and character material |
| [AmbientLightVolumes.md](AmbientLightVolumes.md) | `SubGeometryAmbient` light volumes: node fields, the `ambient` pass, the ambient buffer every world shader adds, and which packages use them |

## Developing the editor itself

| Document | Covers |
|---|---|
| [Architecture.md](Architecture.md) | How the add-on is built: the re-read-and-patch idea, module layout, `core/` loading, register order, coordinates, where state lives (`props.py`), the rules the code keeps |
| [WorldFiles.md](WorldFiles.md) | A world's files and every pool of a world `.oct` (tokyo_b counts), the scene tree, and the associations that bind pools to each other |
| [ImportExport.md](ImportExport.md) | The import and export pipelines step by step, tombstones, fingerprints, UUID matching, and the rules for byte-identical round trips |
| [WorldSubNetwork.md](WorldSubNetwork.md) | SubNetworks in worlds (named placements for Meridian, texture flipbooks, spline followers) and in choreography packages, surveyed on every world |
| [GameRendererCode.md](GameRendererCode.md) | The Cars 2 Game render engine's code: effects, renderer caches, depsgraph updates, the frame's passes, final renders, adding passes |
| [Previews.md](Previews.md) | Choreography previews: anchor tracing, logic replay, baking, characters through the Cars 2 Toolkit, two-part rigs |
| [Testing.md](Testing.md) | Which tests to run for which change, and the lessons they encode |

The Cars 2 Toolkit's [docs](../../../cars2-character-toolkit/cars2_toolkit/docs/README.md) cover characters,
clips and the character rig network ([SubNetworkPool.md](../../../cars2-character-toolkit/cars2_toolkit/docs/SubNetworkPool.md)).

## Working on Blender add-ons for these worlds

| Document | Covers |
|---|---|
| [BlenderPerformance.md](BlenderPerformance.md) | What made a 5,000–10,000-object Blender scene slow (name lookups, `Object.children`, per-frame scans, drawing restrictions) and the measured fixes |

## Elsewhere in the editor's README

The add-on's [README](../README.md) also documents, per feature:

- the Cars 2 Game render engine: shadows, planar reflections, bloom, motion blur and the post-process chain
  as `sdaemon.exe` runs them;
- character animation in choreographies: layer clips driving the rig's controls, the rendered eye texture;
- map objects, spawned actors and spline followers;
- Collect-mode gates (CollectGame code);
- point-to-point race finishes;
- the export rules that keep untouched worlds byte-identical.
