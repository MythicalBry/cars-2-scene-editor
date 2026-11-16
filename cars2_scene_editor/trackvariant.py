"""Track variant preview: show one route (A / B / C) and mode (Race / Collect /
Takedown) of a world.

A world file holds every route raced in it, so the bridges, detour walls,
barriers and sky domes of all of them overlap after import. The import replays
the world's Meridian logic for each route and mode (core/variants.py) and keeps
the result on the scene ('c2_variants': {letter: {mode: {scene node name:
shown}}}). Picking a route and mode hides what they hide (viewport and
renders): the other routes' walls and skies, the other modes' gates, pickups
and attack spawns. All restores every object's own visibility. Export is unaffected: the game sets
these from its logic, not from the file.
"""
import json

import bpy

_ITEMS = []          # Blender needs the enum items kept alive
_MODE_ITEMS = []
MISSION = 'M'        # the one "route" of a mission world (core/variants.MISSION)
MODE_HELP = {
    'Race': 'Race: the route as raced',
    'Collect': "Collect: the Collect graph's gates, weapon pickups and start points",
    'Takedown': "Takedown (Attack): the Attack graph's enemy spawns and turrets",
    'Arena': 'Arena (team battle): the team start and restart points',
    'Bomb': "Bomb (the game's CTF type): the spy bases' doors, the bomb pedestal, bomb spawns",
    'Hunter': 'Hunter: the hunter start points',
}


def store(scene, mer_tree):
    """Resolve the routes of a world's Meridian tree and keep them on the scene."""
    import variants
    try:
        data = variants.resolve(mer_tree)
    except Exception as e:          # never let a graph oddity stop the import
        print('Cars 2 Scene: track variants not resolved: %s' % e)
        data = {}
    if not any(d for modes in data.values() for d in modes.values()):
        data = {}                   # nothing differs between routes and modes
    scene['c2_variants'] = json.dumps(data)
    return data


def data(scene):
    try:
        return json.loads(scene.get('c2_variants', '{}'))
    except ValueError:
        return {}


def items(self, context):
    """'All' plus one entry per route the world has (mission worlds: one, Mission)."""
    scene = context.scene if context else None
    d = data(scene) if scene is not None else {}
    out = [('ALL', 'All', 'Every route and mode at once (as imported)')]
    for v in sorted(d):
        if v == MISSION:
            out.append((v, 'Mission', 'Show one arena mode (pick it next to this)'))
        else:
            out.append((v, 'Track %s' % v, 'Show route %s (pick the mode next to it)' % v))
    _ITEMS[:] = out
    return _ITEMS


def mode_items(self, context):
    """The modes of the picked route: Race / Collect / Takedown, or a mission
    world's Arena / Bomb / Hunter."""
    scene = context.scene if context else None
    d = data(scene) if scene is not None else {}
    modes = d.get(self.track_variant) or next(iter(d.values()), {}) or {'Race': {}}
    _MODE_ITEMS[:] = [(m, m, MODE_HELP.get(m, m)) for m in modes]
    return _MODE_ITEMS


_by_name = {'key': None, 'map': {}}


def _objects_by_name(scene, fresh=False):
    """{game node name (c2_name): [objects]}. Built once per object count
    (building it walks every object: ~0.3 s on tokyo_b, and the crowd /
    gate / map-object lookups ask for it per locator). fresh: rebuild now."""
    key = (scene.name, len(scene.objects))
    if fresh or _by_name['key'] != key:
        out = {}
        for o in scene.objects:
            n = o.get('c2_name')
            if n is not None:
                out.setdefault(n, []).append(o)
        _by_name.update(key=key, map=out)
    return _by_name['map']


def apply(scene, variant, mode='Race'):
    """Hide what route `variant` in `mode` hides (the object and everything
    under it) and, in Collect mode, what the picked lap doesn't show; 'ALL'
    puts every object back the way it was.

    Works out what should be hidden first and then changes only the objects
    whose state differs: putting every hidden object back and hiding the new set
    again wrote thousands of objects per switch (7-10 s on italy in the UI)."""
    from . import objplace, collectgates
    d = data(scene)
    lap = getattr(scene.c2, 'collect_lap', 'ALL')
    kids = objplace._kids(scene)
    by_variant = {}                         # pointer -> object
    if variant != 'ALL':
        objplace.variant_hidden(scene, variant, mode, kids, by_variant)
        if variant in d:
            by_name = _objects_by_name(scene, fresh=True)
            for name, shown in d[variant].get(mode, {}).items():
                if shown:
                    continue
                for o in by_name.get(name, ()):
                    for x in objplace._tree(o, kids):
                        by_variant[x.as_pointer()] = x
    by_lap = {}
    if lap not in (None, 'ALL'):
        k = int(lap)
        for o in scene.objects:
            laps = o.get('c2_collect_laps')
            if laps is not None and k not in list(laps):
                for x in objplace._tree(o, kids):        # a gate's hologram parts with it
                    if x.as_pointer() not in by_variant:
                        by_lap[x.as_pointer()] = x
    VAR, LAP = 'c2_variant_saved', collectgates.LAP_SAVED
    n = 0
    for o in scene.objects:
        ptr = o.as_pointer()
        want = VAR if ptr in by_variant else (LAP if ptr in by_lap else None)
        sv, sl = o.get(VAR), o.get(LAP)
        have = VAR if sv is not None else (LAP if sl is not None else None)
        if want is not None:
            n += 1
        if have == want and not (sv is not None and sl is not None):
            continue
        own = sv if sv is not None else sl          # the object's own visibility, from before any hiding
        if own is not None:
            if VAR in o:
                del o[VAR]
            if LAP in o:
                del o[LAP]
        if want is None:
            if own is not None:
                o.hide_viewport, o.hide_render = bool(own[0]), bool(own[1])
            continue
        o[want] = list(own) if own is not None else [int(o.hide_viewport), int(o.hide_render)]
        if not o.hide_viewport:
            o.hide_viewport = True
        if not o.hide_render:
            o.hide_render = True
    objplace.animate(scene)
    return n


def update(self, context):
    apply(context.scene, self.track_variant, self.track_mode)
    from . import gamerender
    gamerender.RENDERER.rebuild(soft=True)      # shown / hidden objects (and their shadows)
