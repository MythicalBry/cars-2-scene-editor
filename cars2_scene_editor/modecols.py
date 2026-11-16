"""Mode collections: what only one game mode uses, gathered in one place.

The world logic sends each race a track message and a mode message
(TrackBRace / TrackBCollect / TrackBTakedown; mission worlds pick Arena,
Bomb or Hunter). The placement replay (core/choreoplace.py) records, per
route and mode, every map object a spawner places. An object every placement
of which is in the same mode belongs to that mode only:

* Battle Race (Race mode): the Race-only weapon pickups. The world graph
  spawns them on TrackXRace without testing for weapons; whether a race has
  weapons is a game setting (`WeaponsEnabledImpulseGate` nodes branch on it,
  e.g. the Battle banner), so plain Race with weapons off shows none.
* Attack (Takedown): the weapon pickups (PickupMG, PickupSJ, Pickup_Missile,
  Pickup_TriMissile, Pickup_SatBlitz), the Takedown bashables, and the
  Takedown restart points (TakedownTrack<X>).
* Survival (Collect): the Collect pickups and bashables, the Collect gates
  (collectgates.py) with their posts, and the Collect restart points.

organize() MOVES those objects (and the locators they spawn at, when no other
mode uses the locator) into '<world> Modes' / '<world> <mode>' / kind
collections, so a mode's eye toggle really hides it and its things are found
in one place. Anything several modes share stays where it was. Collections
never affect export, so a re-export is unchanged. It runs after the import
and after map objects are rebuilt; the Organize by Mode operator re-runs it.
"""
import bpy

from . import common

MODE_LABELS = {
    'Race': 'Battle Race',
    'Takedown': 'Attack',
    'Collect': 'Survival',
    'Arena': 'Arena',
    'Bomb': 'Bomb (Capture the Flag)',
    'Hunter': 'Hunter',
}
ORDER = ('Race', 'Takedown', 'Collect', 'Arena', 'Bomb', 'Hunter')
SPAWN_LISTS = {'Takedown': ('Takedown',), 'Collect': ('Collect',), 'Arena': ('Arena',),
               'Bomb': ('Bomb', 'HiddenBomb'), 'Hunter': ('Hunter',)}


def _modes(o):
    r = o.get('c2_spawn_routes')
    if r is None:
        return set()
    return {x.split('|', 1)[1] for x in r if '|' in x and x.split('|', 1)[1]}


def _move(o, col, kids):
    """o (and the parts hanging under it) into col only. kids: parent -> children
    (Object.children_recursive scans every object per call)."""
    stack, todo = [o], []
    while stack:
        x = stack.pop()
        todo.append(x)
        stack.extend(kids.get(x.as_pointer(), ()))
    for x in todo:
        if col not in x.users_collection:
            col.objects.link(x)
        for c in list(x.users_collection):
            if c is not col:
                c.objects.unlink(x)


def _reparent(child, parent):
    """Collection child under parent only."""
    for p in [p for p in bpy.data.collections if child.name in p.children] + \
             ([bpy.context.scene.collection] if child.name in bpy.context.scene.collection.children else []):
        if p is not parent:
            p.children.unlink(child)
    if child.name not in parent.children:
        parent.children.link(child)


def organize(scene):
    """Move every single-mode map object, spawn locator, restart list and
    the Collect gates into the world's mode collections. Returns {mode: count}."""
    from . import gameplay
    world = scene.c2.world_name
    cols = gameplay.collections(scene, create=False)
    main = cols.get('Main')
    if main is None:
        return {}
    root = None
    made = {}
    kids = {}
    for x in bpy.data.objects:
        if x.parent is not None:
            kids.setdefault(x.parent.as_pointer(), []).append(x)

    def mode_col(mode, kind=None):
        nonlocal root
        if root is None:
            root = common.ensure_collection('%s Modes' % world, main)
            if root.name not in main.children:
                _reparent(root, main)
        label = MODE_LABELS.get(mode, mode)
        mc = common.ensure_collection('%s %s' % (world, label), root)
        if kind is None:
            return mc
        return common.ensure_collection('%s %s %s' % (world, label, kind), mc)

    # map objects placed in one mode only (not their parts: they move with them)
    by_spot = {}
    single = []
    for o in scene.objects:
        if not o.get('c2_map_object') or o.get('c2_map_part'):
            continue
        ms = _modes(o)
        spot = o.get('c2_spawn_point')
        if spot:
            by_spot.setdefault(spot, set()).update(ms or {'*'})
        if len(ms) == 1:
            single.append((o, next(iter(ms))))
    for o, mode in single:
        label = str(o.get('c2_object_label', ''))
        kind = 'Pickups' if 'pickup' in label.lower() else 'Props & Bashables'
        _move(o, mode_col(mode, kind), kids)
        made[mode] = made.get(mode, 0) + 1

    # the locators they spawn at, when only that mode spawns anything there and
    # the locator is one of the plain "other" locators (not an anchor, grid ...)
    others = cols.get('Locators')
    if others is not None:
        from .trackvariant import _objects_by_name
        names = _objects_by_name(scene, fresh=True)
        for spot, ms in by_spot.items():
            if len(ms) != 1 or '*' in ms:
                continue
            mode = next(iter(ms))
            for o in names.get(spot, ()):
                if o.get('c2_spawn_master') or o.get('c2_map_object'):
                    continue
                if list(o.users_collection) == [others]:
                    _move(o, mode_col(mode, 'Spawn Points'), kids)

    # the per-mode restart / spawn lists (Spawn: TakedownTrackB ...)
    spawns = cols.get('Spawns')
    if spawns is not None:
        for c in list(spawns.children):
            list_name = c.name.split('Spawn: ', 1)[-1]
            for mode, prefixes in SPAWN_LISTS.items():
                if list_name.startswith(prefixes):
                    _reparent(c, mode_col(mode))
                    break
    # Survival: the Collect gates and their posts
    gates = bpy.data.collections.get('%s Collect Gates' % world)
    if gates is not None and gates.all_objects:
        _reparent(gates, mode_col('Collect'))
        if others is not None:
            import json
            from .objplace import _locator
            for o in gates.objects:
                js = o.get('c2_collect_gate')
                if js is None:
                    continue
                for post in json.loads(js)['posts']:
                    p = _locator(scene, post) if post else None
                    if p is not None and list(p.users_collection) == [others]:
                        _move(p, mode_col('Collect', 'Gate Posts'), kids)

    lines = bpy.data.collections.get('%s Collect Lines' % world)
    if lines is not None and lines.all_objects:
        _reparent(lines, mode_col('Collect'))
        from . import collectgates
        collectgates.show_lines_collection(bpy.context, scene)     # (it started out under the hidden Paths)

    # tidy: empty map-object label collections, and mode collections in game order
    mo = cols.get('MapObjects')
    if mo is not None:
        for c in list(mo.children):
            if not c.all_objects and not c.children:
                bpy.data.collections.remove(c)
    root = root or bpy.data.collections.get('%s Modes' % world)
    if root is not None:
        # mode / kind collections left empty (map objects rebuilt or removed)
        for mc in list(root.children):
            for kc in list(mc.children):
                if not kc.all_objects and not kc.children and not kc.name.startswith('%s Spawn: ' % world)                         and not kc.name.endswith((' Collect Gates', ' Collect Lines')):
                    bpy.data.collections.remove(kc)
            if not mc.all_objects and not mc.children:
                bpy.data.collections.remove(mc)
        for mode in ORDER:
            mc = bpy.data.collections.get('%s %s' % (world, MODE_LABELS[mode]))
            if mc is not None and mc.name in root.children:
                root.children.unlink(mc)          # re-link in order: the outliner lists by link order
                root.children.link(mc)
    scene['c2_mode_counts'] = {MODE_LABELS.get(k, k): v for k, v in made.items()}
    return made


def mode_collections(scene):
    """[(label, collection, [kind collections])] of the world's mode collections, in game order."""
    world = scene.c2.world_name
    out = []
    for mode in ORDER:
        mc = bpy.data.collections.get('%s %s' % (world, MODE_LABELS[mode]))
        if mc is not None:
            out.append((MODE_LABELS[mode], mc, sorted(mc.children, key=lambda c: c.name)))
    return out


class C2_OT_organize_modes(bpy.types.Operator):
    """Gather what only one game mode uses (its pickups, bashables, spawn points,
    restart points, Collect gates) into that mode's collection"""
    bl_idname = 'c2.organize_modes'
    bl_label = 'Organize by Mode'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        made = organize(context.scene)
        self.report({'INFO'}, 'moved %d mode-only map objects (%s)' %
                    (sum(made.values()), ', '.join('%s %d' % (MODE_LABELS.get(k, k), v) for k, v in made.items()) or '-'))
        return {'FINISHED'}


_classes = (C2_OT_organize_modes,)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
