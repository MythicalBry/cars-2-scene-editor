# Testing the Scene Editor

The test scripts live in the development folder (`cars2-scene-editor/tests/`), not in the released add-on;
they need a local copy of the game's worlds.
This page says which ones to run when, and why.

## Ground rules

- **Never write to the game's assets folder or to `vanilla_worlds/`.** The vanilla worlds are the byte-identical
  reference (`allw/`: all 21 worlds; `vanilla/`: the base set). Outputs go to the work folder
  `C:/Users/<you>/AppData/Local/Temp/c2s`.
- Run `bash tests/sync.sh` first. It copies the add-on to `Temp/c2s/addon` and the scripts to `Temp/c2s/tools`,
  and syntax-checks the add-on.
- Use `blender -b --factory-startup` for background tests. Real-window tests (screenshots, playback timing) need a
  UI.

## What to run

| Change touches | Run |
|---|---|
| import or export, any `core/` format module | `bl_rt.py` on the worlds you touched, then `rt_all.sh` (every world, byte for byte) |
| Meridian editing | `bl_rt.py`, plus the feature test (`bl_var*.py`, `bl_mapobj.py`, `bl_finish.py`, `bl_modecols.py` ...) |
| materials, shader switching, the shared system | `bl_effectswitch.py`, `bl_effectdialog.py`, `bl_undo.py`; then `tools/sync_look.py` and the toolkit's `bl_tk_materials.py` / `bl_tk_shared.py` / `bl_tk_still.py` |
| the Game renderer | `bl_glrender*.py`, `bl_glshadow.py`, `bl_glrefl.py`, `bl_renderanim.py`; screenshots compared by eye |
| speed (UI, playback, import) | `bl_freezes.py`, `bl_perf2.py`, `bl_imptime.py`, `bl_guiplay.py`, `bl_trackdraw.py`; in a real window `gui_perf.py` (navigate, edit, undo / redo, Track / Mode / Lap, preview load, playback; `showprof.py` prints its profiles) ([BlenderPerformance.md](BlenderPerformance.md)) |
| renderer caches (undo, picker, mesh reuse) | `gui_cachecheck.py` (real window): each cached frame against a from-scratch rebuild, must differ by 0 pixels |
| Collect mode | `collectline_vanilla.py` (no Blender: lap cuts and the game's ring crossings on every shipped collect line, 259 / 260 gates), `bl_collect_rt.py` (export with new / relabelled / removed gates and an edited lap, re-import), `bl_collect_panel.py` (the panel drawn into a mock layout that rejects unknown icons; rings after moving a lap), `gui_ringfollow.py` (real window: rings follow an Edit Mode line edit, also from a dropped-timer state) |
| the effect list | `build_effect_list.py` (regenerates `data/effect_list.json`) |
| surveys | `mat_slot_survey.py` (slots per material across worlds), `amb_survey.py` ... |

## Lessons the tests encode

- **Byte identity is the check** for anything touching files. "It loads in the game" is not enough; compare
  bytes, then test in game for behaviour.
- **Don't preset `scene.c2.assets_dir`** in tests of asset detection.
- **Auto exposure adapts over time.** Play about 8 s before Game Look screenshots, or switch
  `game_auto_exposure` off.
- **Still viewports redraw only edited objects.** Test material edits without playback, because playback
  rebuilds everything and hides stale-command bugs.
- **Script undo** needs `ed.undo_push` / `ed.undo` with a full window, screen, area and region override.
- **Avoid wildcard deletes** in scripts; remove exact files.
- **Background runs don't run timers, draw handlers or `view_update`.** Anything that happens "a moment after an
  edit" (ring refresh), overlays and renderer updates must be tested in a real window
  (`blender --python script.py`, steps driven by `bpy.app.timers`).
- **A cache needs a from-scratch comparison**, not just a "looks right": draw, `RENDERER.clear()`, draw again,
  compare pixels.
- **Icon names differ between Blender versions.** An unknown icon raises in `UILayout.label` and stops the panel;
  `bl_collect_panel.py`'s mock layout checks every icon against the running Blender.
