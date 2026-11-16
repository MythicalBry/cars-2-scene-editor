# Performance notes: what made the Cars 2 Scene Editor slow, and the fixes

Cars 2 worlds hold 5,000–10,000 objects (tokyo_b 5,417, tokyo_a 6,576). At that size, several ordinary Blender
Python patterns become the bottleneck. These were measured headless (Blender 5.2) with the profiling tools
described at the end. They apply to any add-on working on large scenes.

## Measured (tokyo_b, 1280 × 720, Cars 2 Game renderer)

| | before | after |
|---|---|---|
| Import (no profiler) | ~11 s+ | 7.6 s |
| Orbit redraw, median | 45 ms | 17 ms |
| Timeline playback per frame, median | 53 ms | 19 ms |
| Crowd per frame | 16–18 ms | ~1 ms |
| Light volumes per frame | 11–13 ms | ~0 ms |

tokyo_a (3,938 parts in view, 854 crowd members): orbit 67 → 58 ms, playback 84 → 69 ms. What remains there
is the per-draw-call cost of submitting ~4,000 parts from Python.

## Choreography playback in a real window

Headless (`-b`) timings missed the worst problem: depsgraph handlers and the render engine's `view_update`
don't run there. Measured instead in a real Blender window, playing the timeline in a Rendered viewport with
`sync_mode` NONE (`bl_guiplay.py`):

| Choreography | before | after |
|---|---|---|
| tokyo_a, Survival_Intro_Z_Laugh | 815 ms / frame (1.2 fps) | 56 ms (17.8 fps) |
| tokyo_b, fireWorksA | ~2.3 s / frame | 41.7 ms (the 24 fps cap; draw 8.6 ms) |

Viewport pixels at the same frame are identical before and after, apart from window chrome (the
focus border, the splash screen).

12. **`view_update` looped over every drawn object for each Mesh update.** It matched each object's mesh name
    by looking the object up: ~4,000 name lookups per update, ~0.7 s a frame while armature-bent preview
    meshes played. The renderer now records which objects draw each mesh datablock. Such an update also takes
    the re-deform path instead of a full mesh rebuild.

13. **A keyed material nobody draws forced a full rebuild every frame.** The fireworks' light-volume materials
    are keyed; "no known users" was treated as "unknown users". A material that isn't in the renderer's
    users map isn't drawn, so bumping its version is enough.

14. **Rebuilding one object's commands re-flattened and re-sorted all of them.** That was ~7,000 commands and
    30 ms a frame on tokyo_a. Moved, armature-bent or re-keyed objects now keep their commands, updated in
    place: batch, world matrix, uniform block, states, culling bounds. Anything structural (shown or hidden,
    parked at zero scale, a material swap, opaque ↔ blended) still rebuilds.

15. **Uniform blocks recomputed their texture bindings even when only the world matrix changed.** Bindings
    are now cached per material version (every edit, keyed change or texture replacement bumps it or clears
    the cache), and a move rewrites only the world rows.

16. **Frame-change handlers touched every keyed material every frame**, whether its values changed or not,
    after scanning every material in the file to find them. Now they compare each keyed material's values
    and use a cached list.

17. **`Object.children_recursive` in `view_update`** is another whole-scene scan per moved object. Use a
    parent map built once per object count.

## Freezes fixed

| Action (tokyo_b, 5,459 objects) | Before | After |
|---|---|---|
| First shader search (Pick Effect) | 83 s | 30 ms |
| Delete every collection (Outliner Delete Hierarchy) | 18 s | 0.2 s (Delete World / Delete Hierarchy (Fast)) |
| Clearing the map objects before a rebuild (2,496 copies) | 13.1 s | 0.65 s |

18. **`bpy.data.objects.remove` costs ~4.4 ms per object** in a 5,000-object file (every removal re-checks
    what refers to the object). `bpy.data.batch_remove(ids)` removes them all in one pass: 1,248 objects took
    5.5 s one by one and 0.2 s in a batch. Blender's Outliner *Delete Hierarchy* goes one by one too.
    After removing a world, drop every cache that holds its objects, materials or images, like a file load
    does. A stale image reference crashed the next draw after a re-import.

19. **Don't compute a search list's descriptions on first open.** Describing an effect needs its shader
    translated (~30 ms; 1,265 effects). The descriptions of the shipped files never change, so they ship as
    a JSON list keyed by name and file size. Only new or changed files are worked out, once, into a user
    cache.

## Game Look editing in a real window

Measured on italy (9,841 objects) in a real Blender 5.3 window, Rendered viewport, Cars 2 Game renderer
(`gui_perf.py`-style runs: `wm.redraw_timer` around each action, cProfile per section):

| Action | Before | After |
|---|---|---|
| Orbit redraw | 121 ms | 34 ms |
| Undo / redo (each, with the redraw) | 10.4 s | 2.9 s |
| Track / Mode / Lap switch | 7–10 s | 0.5–1 s (to / from All: ~3 s) |
| Timeline playback per frame | 156 ms | 70–80 ms |
| Moving an object (with its undo step) | 156 ms | 150 ms (mostly Blender's own undo push) |

Every cached result was checked against a from-scratch rebuild (`RENDERER.clear()`, then the same view): 0
differing pixels after a mesh edit, undo, redo, a material edit and a Track / Mode / Lap switch.

20. **An overlay looked objects up by name every redraw.** The collect-lap / path outline overlay called
    `scene.objects.get` ~56 times per draw: 1.3 ms each on italy, 70 of the 120 ms of an orbit redraw. It now
    keeps object lists made once per object count (and again after undo / redo / file load: `_stale`).

21. **Undo re-read every mesh.** `reset_after_undo` dropped all of the renderer's meshes (6,300 on italy, ~15 s
    over three undos). Blender's undo leaves the data-blocks it didn't change where they were, so meshes are
    now kept aside and reused when their fingerprint still matches (`_exact`: the mesh data-block's address and
    every position, corner, UV, material and modifier, hashed: ~0.1 ms against ~0.8 ms to read a mesh). Their
    materials are looked up again by name, from one map made per undo.

22. **The material half of a uniform block was worked out once per object.** Italy draws 157 materials over
    ~12,500 parts. `mat_cache` keeps each material's bindings and values once (one entry per material, holding
    its version, so a material keyed every frame replaces its entry instead of piling up new ones). Cleared
    with the block cache (texture swaps).

23. **Showing or hiding an object re-read its mesh.** Blender reports a geometry update (and a mesh data
    update) for an object that was only shown or hidden. Outside Edit Mode, an undeformed mesh without
    modifiers is now only marked *suspect* and checked against its fingerprint on the next draw; it is read
    again only when it really changed.

24. **A full rebuild threw away every object's commands.** The pickers, previews and anything that shows, hides,
    adds or removes objects now ask for a *soft* rebuild (`rebuild(soft=True)`): an object whose visibility,
    world matrix, mesh, material versions and display are unchanged keeps its commands (`_draw_key`). Texture
    swaps, file loads and undo still rebuild everything.

25. **The Track / Mode / Lap pickers put every hidden object back, then hid the new set.** In a real window each
    `hide_viewport` write is expensive (see trap 10). `trackvariant.apply` now works out what should be hidden
    first and writes only the objects whose state changes.

26. **`len(bpy.data.objects)` per updated object.** `_descendant_names` asked for it to validate its parent map:
    ~8,500 calls per switch, 1.4 s. `view_update` takes it once per update.

27. **The auto-exposure read the frame back on every redraw** (a GPU stall, ~5 ms). While the view keeps
    changing it measures at most every 0.12 s; when the view stops, one more redraw (a timer) measures the
    final frame. F12 renders always measure.

28. **Sub-pixel parts.** In the viewport, parts whose bounding sphere is under 0.75 px on screen are left out
    (distant bolts, signs, lamps). F12 renders draw everything.

29. **A timer can be dropped under you.** Blender removes non-persistent timers on file load / revert. A "refresh
    pending" flag guarding the timer then stayed set with no timer to clear it, and live updates stopped for
    good. Register such timers `persistent=True` and check `is_registered` every time instead of trusting a flag.

## The traps

1. **`scene.objects.get(name)` and `scene.objects[name]` scan every object.** `scene.objects` isn't a hashed
   list. Even `bpy.data.objects.get` cost about 0.13 ms per call here. **Keep direct object references** in
   per-frame code. Refresh them on `ReferenceError`, and in `undo_post`, `redo_post` and `load_post` handlers,
   because undo re-creates every object.

2. **`Object.children` scans every object each time it is read**, so walking a hierarchy with it is quadratic:
   1.7 s per call for 5,400 objects. **Build a parent → children map in one pass** over `bpy.data.objects`. In
   database order it gives the same order `children` does, which matters when the order feeds an export.

3. **Don't iterate all objects or `depsgraph.object_instances` per frame** to find a few special ones (light
   volumes, gate parts). Cache the list and rebuild it when the object count changes, when a relevant setting
   changes, or after undo.

4. **`depsgraph_update_post` handlers run on every update**, including every frame of playback. Look at
   `depsgraph.updates` first and only act when one of *your* objects changed (for example
   `u.is_updated_transform and u.id.original.name in names`).

5. **A viewport draw (`RenderEngine.view_draw`, draw handlers) may not create or modify IDs.** Loading images
   fails there, which silently broke particle textures. Load images when the data is built and only call
   `gpu.texture.from_image` during the draw.

6. **Use Blender's own loaders with absolute paths.** `bpy.data.images.load` read the 2048 × 1024 DXT5 crowd
   atlas in 0.06 s. A relative path made it fail, and the numpy DXT decoder written as a workaround cost 11 s
   per import.

7. **Vectorise per-element work with numpy.** The crowd sprites (view angle, frame, mirror, bounce for ~850
   cars) went from a 20 ms Python loop to well under 1 ms.

8. **One `VertexGroup.add` per vertex is slow.** Weights stored as bytes / 255 have at most 256 values, so
   group vertices by weight and add each group in one call.

9. **Resolve per-frame constants once per pass**, not per draw call. Looking up each part's frame textures by
   name built 2,000 key tuples a frame on tokyo_a. Also skip `uniform_sampler` when the same texture is already
   bound to that sampler of the bound shader. This was verified pixel-identical.

10. **Visibility toggles are expensive** (about 95 ms each in a 10,000-object file): `hide_viewport`, and even
    `hide_set`, re-evaluate every object's visibility. To hide something per frame, scale it to zero and skip
    zero-scale objects in your renderer.

11. **Profilers can mislead** when Blender calls back into Python (property `update` callbacks during an import):
    cProfile attributed 9 s of an import to a function that took 0.2 s. Confirm with a plain stopwatch, with
    the suspect step switched off.

## Tools (in the editor's test folder)

- `bl_perf2.py`: import, orbit redraws, choreography previews and playback, each profiled through the
  Cars 2 Game renderer's viewport path into an offscreen framebuffer.
- `bl_imptime.py`: import time with and without one step.
- `bl_playperf.py`: playback with view_update emulation.
- `bl_fwview.py`: a real Blender window, Rendered viewport, screenshot. Needed for view_draw-only problems,
  which background (`-b`) runs can't show.
- `bl_guiplay.py`: a real Blender window playing a choreography. It reports real ms per frame and times
  `view_update` and `view_draw`, with a profile and a screenshot at a fixed frame for comparing versions.
- `bl_chofb.py`: the viewport draw path with emulated `view_update` marks, read back for pixel comparison.
- Real-window profiling of navigation, edits, undo / redo, Track / Mode / Lap switches, playback and preview
  loading, and the cached-versus-from-scratch pixel check, are described in section *Game Look editing in a
  real window* above; run them with `blender --python <script>` (no `-b`) and a timer-driven step list.
