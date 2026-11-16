"""Map objects: the bashables, breakables and pickups a world's logic spawns.

Most of a track's props are not placed by the scene file. A breakable (Italy's
ITY_Chandelier_whole, Tokyo's NeonShortWhole, radiator's hay bales) is one
model in the .oct, authored at the world origin, plus its pieces
(MExploder piece_name, e.g. LrgWindowL_1..9, also at the origin). The .mer
has a SpawnAgentNode bound to it (BoundObjectName) in a spawn group; the world
graph fires the group once per locator (a TransformLoop over a list, or a
G2G channel such as 'ITY_WindowLeft' carrying the locator), and the game
spawns a copy of the model at each one. The model at the origin is only the
template. Bashables and pickups work the same way with a model from
assets/objects: the agent's .ap section (matched by MeridianActor_Uuid)
names it, model_name=Objects\\Bashable.

The import replays the logic per track and mode (core/choreoplace.simulate)
and keeps where every agent spawns (scene['c2_agent_places']). This module
shows each spawn: a copy of the model (sharing the template's mesh, or an
instance of the objects/ package) that follows its locator, in the world's
Map Objects collection. The templates and pieces at the origin are hidden.

Editing: a copy follows its locator (position and orientation, no scale, as
the game spawns it), so moving the locator moves the object; the locator is
what gets exported. The copies themselves are never exported. The object's
settings (bashable type, respawn time, the exploder's pieces and velocities)
are its .ap section.

Actors (SpawnActorNode: mi_air's baggage trains and conveyor boxes,
radiator's cows) spawn the same way, but their template is a world .oct node
placed anywhere (its own placement is dropped: the actor stands at its spawn
point). Many ride a .dbl path with a SplineFollower component; those are
spawned again and again on a timer (a driver every 10 s, a kart every 0.5 s
behind it). The import also replays the logic on a clock
(choreoplace.spawn_times -> scene['c2_agent_times']): each such spawn is a copy
that appears at its spawn time on the timeline (frame_start = the race start)
and drives along the path as the game moves it (see follower()), then vanishes
where the logic kills it at the end of the path. Spawners only gameplay events
start (a base blown up in Bomb mode starts the conveyors) are timed from the
event (choreoplace.EVENT_AT seconds in). Spawned characters (turrets, mission
cars, London's trains: Characters\\...) are not map objects and are left out.
"""
import json
import os

import bpy
import numpy as np
from mathutils import Matrix, Vector

import choreo
import octscene
from . import common
from .common import g2b

PLAYER = '<player>'
LIBRARY = 'Cars2 Object Library'


def data(scene):
    try:
        return json.loads(scene.get('c2_agent_places', '{}'))
    except ValueError:
        return {}


def actors_by_uuid(scene):
    """{MeridianActor_Uuid (lower hex): .ap section} of the scene's actors."""
    out = {}
    for a in scene.c2.actors:
        for kv in a.kvs:
            if kv.key == 'MeridianActor_Uuid' and kv.value:
                out[kv.value.lower()] = a
                break
    return out


def kv_of(actor, key, default=None):
    if actor is None:
        return default
    for kv in actor.kvs:
        if kv.key == key:
            return kv.value
    return default


def find_object_package(assets, model_name):
    """'Objects\\Bashable' -> choreo.Package of assets/objects/bashable(.zip), or None."""
    if not assets or not model_name:
        return None
    parts = [p for p in model_name.replace('\\', '/').split('/') if p]
    if not parts:
        return None
    stem = parts[-1].lower()
    base = os.path.join(assets, *[p.lower() for p in parts[:-1]]) if len(parts) > 1 else os.path.join(assets, 'objects')
    z = os.path.join(base, stem + '.zip')
    if os.path.isfile(z):
        return choreo.Package(stem, zpath=z)
    d = os.path.join(base, stem)
    if os.path.isfile(os.path.join(d, stem + '.oct')):
        return choreo.Package(stem, folder=d)
    return None


def _library(name):
    """A collection (kept out of every scene) holding one package's model."""
    lib = bpy.data.collections.get(LIBRARY)
    if lib is None:
        lib = bpy.data.collections.new(LIBRARY)
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        lib.children.link(col)
    return col


def package_model(context, pkg, cache, report):
    """Collection with the static model of an objects/ package (game space,
    reused by every spawn of it), or None."""
    from . import preview
    from .importer import build_geometry_mesh
    name = 'OBJ_' + pkg.stem
    col = bpy.data.collections.get(name)
    if col is not None and col.objects:
        return col
    try:
        sc = pkg.scene()
    except Exception as e:
        report.append('%s: %s' % (pkg.stem, e))
        return None
    col = _library(name)
    mats = preview._materials(sc, os.path.join(cache, 'objects', pkg.stem))
    objs = {}
    for i, node in enumerate(sc.nodes):
        if octscene.is_subgeo(node) or not sc.subgeos(i):
            continue
        me = build_geometry_mesh(sc, i, '%s_%s' % (pkg.stem, node.get('NodeName', i)), mats)
        if me is None:
            continue
        o = bpy.data.objects.new(me.name, me)
        col.objects.link(o)
        o.matrix_basis = g2b(list(sc.world(i).ravel()))
        o['c2_excluded'] = True
        o['c2_preview'] = True          # drawn like a spawned model (casts shadows, reflects)
        if not node.get('Visible', 1):
            o.hide_render = True
            o.display_type = 'WIRE'
        objs[i] = o
    # skinned parts (spy bashable, skate jack): posed at rest, no armature
    preview._rig(context, col, None, sc, objs, name, armature=False)
    return col


def _agents(g):
    """{agent uuid hex: SpawnAgentNode / SpawnActorNode} of the world graph."""
    out = {}
    for i in g.of_type('SpawnAgentNode') + g.of_type('SpawnActorNode'):
        u = g.nodes[i].get('Uuid')
        if isinstance(u, (bytes, bytearray)):
            out[bytes(u).hex()] = g.nodes[i]
    return out


def times(scene):
    """{agent uuid: [(spawn object name, seconds, event message, 'route|mode'), ...]}
    (scene['c2_agent_times'], see choreoplace.spawn_times)."""
    try:
        d = json.loads(scene.get('c2_agent_times', '{}'))
    except ValueError:
        return {}
    moved = {(s_['spot'], s_['track']): s_['object'] for s_ in splits(scene)}
    out = {}
    for v, md in d.items():
        for m, ag in md.items():
            for u, l in ag.items():
                for x, t, ev in l:
                    out.setdefault(u, []).append((moved.get((x, v), x), t, ev, '%s|%s' % (v, m)))
    return out


def follower(g, uuid):
    """The SplineFollower of an actor (its .ap SplineFollowerMeridianComponent_Uuid,
    upper hex): {'splines': [path names it can ride], 'MaxSpeed', 'Acceleration',
    'PctOnPath', 'InitialSpeed', 'kill': the logic kills it at the end of the
    path}, or None. A SplineVariable lists every spline a SetSpline writes
    into it (mi_air's wild karts pick one of two)."""
    import meridian
    if not uuid:
        return None
    grp = next((j for j, n in enumerate(g.nodes) if n.get('Type') == 'NodeGroup' and n.get('Uuid') is not None
                and meridian.uuid_hex(n['Uuid']).upper() == uuid.upper()), None)
    if grp is None:
        return None
    out = {'splines': [], 'kill': False}

    def spline_names(src, depth=0):
        node = next((x for x in g.nodes if x.get('Type') == 'SplineNode' and x.get('Handle') == src), None)
        if node is not None:
            return [node.get('BoundObjectName')] if node.get('BoundObjectName') else []
        n = g.nodes[src]
        if n.get('Type') == 'SplineVariable' and depth < 3:
            names = []
            for li in g.in_links.get(src, []):
                w = g.links[li]['Origin']           # a SetSpline's Write port
                setter = next((k for k, x in enumerate(g.nodes) if x.get('Type') == 'SetSpline'
                               and w in g.owned(k)), None)
                for j in g.owned(setter) if setter is not None else ():
                    if g.nodes[j].get('Type') == 'SplineFetch':
                        for s2 in g.link_sources(j):
                            names += [x for x in spline_names(s2, depth + 1) if x not in names]
            return names
        return []

    for j in g.owned(grp):
        n = g.nodes[j]
        if n.get('Type') == 'FloatFetch':
            out[n.get('LeafName', '')] = float(n.get('Value', 0.0))
        elif n.get('Type') == 'SplineFetch':
            for src in g.link_sources(j):
                out['splines'] += [x for x in spline_names(src) if x not in out['splines']]
        elif n.get('LeafName') == 'Moving.ArrivedEnd':
            out['kill'] = any(g.nodes[g.links[li]['Target']].get('Type') == 'SpawnGroupKiller'
                              for li in g.out_links.get(j, []))
    return out


def _path_object(scene, name):
    return next((o for o in scene.objects if o.type == 'CURVE' and o.c2.kind == 'PATH'
                 and o.get('c2_path_name', o.name) == name), None)


def _path_points(scene, obj):
    """(game-space points (n x 3), closed) of a path object."""
    from .gameplay import _curve_points, game_points
    pts, cyc = _curve_points(obj)
    return np.asarray(game_points(scene, obj, pts), np.float64).reshape(-1, 3), bool(cyc)


def splits(scene):
    """[{'spot': shared locator, 'track': letter, 'object': its copy for that
    track (a locator object name)}]: spawn points split per track."""
    try:
        return list(json.loads(scene.get('c2_spawn_splits', '[]')))
    except ValueError:
        return []


def _save_splits(scene, lst):
    scene['c2_spawn_splits'] = json.dumps(lst)


def placements(scene):
    """{agent uuid: {spawn object name: sorted ['route|mode', ...]}}, with the
    track-specific copies of split spawn points (see split operators)."""
    moved = {(s_['spot'], s_['track']): s_['object'] for s_ in splits(scene)}
    out = {}
    for v, md in data(scene).items():
        for m, d in md.items():
            for u, names in d.items():
                for n in names:
                    if n and n != PLAYER:
                        n = moved.get((n, v), n)
                        out.setdefault(u, {}).setdefault(n, set()).add('%s|%s' % (v, m))
    return {u: {n: sorted(k) for n, k in d.items()} for u, d in out.items()}


def clear(scene):
    scene['c2_follow_count'] = 0
    common.remove_ids([o for o in bpy.data.objects if o.get('c2_map_object')], data=False)
    common.remove_ids(collections=[c for c in bpy.data.collections
                                   if c.name.startswith('OBJ_') and c.users == 1 and not c.objects])
    from . import gameplay
    cols = gameplay.collections(scene, create=False)
    mo = cols.get('MapObjects')
    if mo is not None:
        for c in list(mo.children):
            if not c.objects and not c.children:
                bpy.data.collections.remove(c)
    for o in scene.objects:
        if o.get('c2_spawn_master') or o.get('c2_exploder_piece'):
            saved = o.get('c2_map_saved')
            if saved is not None:
                o.hide_viewport, o.hide_render = bool(saved[0]), bool(saved[1])
                del o['c2_map_saved']
            for k in ('c2_spawn_master', 'c2_exploder_piece'):
                if k in o:
                    del o[k]


def _hide(o, key, label):
    if 'c2_map_saved' not in o:
        o['c2_map_saved'] = [int(o.hide_viewport), int(o.hide_render)]
    o[key] = label
    o.hide_viewport = True
    o.hide_render = True


def build(context, scene, g, report=None):
    """Show every spawned map object at its spawn points (see the module doc).
    Returns the number of copies made."""
    from . import gameplay, trackvariant
    report = report if report is not None else []
    clear(scene)
    place = placements(scene)
    if not place or g is None:
        return 0
    agents = _agents(g)
    by_uuid = actors_by_uuid(scene)
    by_name = trackvariant._objects_by_name(scene, fresh=True)
    assets = common.game_assets(scene)
    cache = os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)),
                         '%s.oct.textures' % scene.c2.world_name)
    multi = len([v for v in data(scene) if v not in ('', trackvariant.MISSION)]) > 1
    mo = gameplay.collections(scene)['MapObjects']
    # choreographies played by a spawned agent's own graph (its breaking
    # animation: crate / fountain / pylon explosions), by spawn group
    group_choreos = {}
    for ci in g.of_type('Choreography'):
        grp = g.nodes[ci].get('Group')
        if isinstance(grp, int):
            group_choreos.setdefault(grp, []).append(ci)
    subs = {}
    made = 0
    missing = set()
    when = times(scene)
    follows = 0
    for u, spots in place.items():
        node = agents.get(u)
        if node is None:
            continue
        actor = by_uuid.get(u)
        bound = node.get('BoundObjectName') or ''
        model = kv_of(actor, 'model_name', '') or ''
        if model.replace('/', '\\').lower().startswith('characters\\'):
            continue                    # a spawned character (turret, mission car), not a map object
        src = next((o for o in by_name.get(bound, ()) if o.type == 'MESH'), None) if bound else None
        lib = None
        if src is None and model and model.lower() != 'none':
            pkg = find_object_package(assets, model)
            lib = package_model(context, pkg, cache, report) if pkg is not None else None
            if lib is None:
                missing.add(model)
                continue
        elif src is None:
            continue                    # a spawner without a model of its own (weapon spawn wrappers)
        label = bound or model.replace('\\', '/').split('/')[-1]
        sub = subs.get(label)
        if sub is None:
            sub = subs[label] = common.ensure_collection('%s %s' % (scene.c2.world_name, label), mo)
        if src is not None:
            # the template at the origin, its children and its exploder pieces
            for x in [src] + list(src.children_recursive):
                _hide(x, 'c2_spawn_master', label)
            piece = kv_of(actor, 'exp/piece_name', '')
            if piece:
                for n, objs in by_name.items():
                    if n.startswith(piece):
                        for x in objs:
                            if x.type == 'MESH' and not x.get('c2_spawn_master'):
                                _hide(x, 'c2_exploder_piece', label)
        # a spline follower: one copy per spawn (time) that drives along its path
        fol = follower(g, kv_of(actor, 'SplineFollowerMeridianComponent_Uuid', '')) \
            if node.get('Type') == 'SpawnActorNode' else None
        if fol is not None and not fol['splines']:
            fol = None
        spawns = {}
        if fol is not None:
            for x, t, ev, route in when.get(u, ()):
                if x in spots:
                    spawns.setdefault((x, t, ev), []).append(route)
        for spot, routes, t0, ev in _spawn_list(spots, spawns):
            loc = next((o for o in by_name.get(spot, ()) if not o.get('c2_spawn_master')), None)
            if loc is None:
                missing.add(spot)
                continue
            name = '%s @ %s' % (label, spot) if t0 is None else '%s @ %s %.2fs' % (label, spot, t0)
            if src is not None:
                o = bpy.data.objects.new(name, src.data)
                o.c2.visible = src.c2.visible
            else:
                # the package's meshes, sharing the library's data, under an
                # empty at the spawn point (the renderer needs scene objects:
                # instances of a collection outside the scene aren't evaluated)
                o = bpy.data.objects.new(name, None)
                o.empty_display_type = 'PLAIN_AXES'
                o.empty_display_size = 0.5
                for lo in lib.objects:
                    m = bpy.data.objects.new('%s %s' % (name, lo.name.split('_', 1)[-1]), lo.data)
                    sub.objects.link(m)
                    m.parent = o
                    m.matrix_parent_inverse.identity()
                    m.matrix_basis = lo.matrix_basis.copy()
                    m.hide_render = lo.hide_render
                    m.display_type = lo.display_type
                    m['c2_excluded'] = True
                    m['c2_preview'] = True
                    m['c2_map_part'] = 1
                    m['c2_map_object'] = 1
            sub.objects.link(o)
            if t0 is None:
                # spawned at the locator's position and orientation (the game drops its scale)
                for ty in ('COPY_LOCATION', 'COPY_ROTATION'):
                    c = o.constraints.new(ty)
                    c.target = loc
            else:
                spline = _nearest_spline(scene, fol['splines'], loc)
                o['c2_follow'] = json.dumps({
                    'path': spline, 't0': t0, 'event': ev, 'kill': fol['kill'],
                    'vmax': fol.get('MaxSpeed', 0.0), 'accel': fol.get('Acceleration', 0.0),
                    'pct': fol.get('PctOnPath', 0.0),
                    # PlaceActorAsInWorld=1: from the path's point nearest the spawn
                    'nearest': str(kv_of(actor, 'PlaceActorAsInWorld', '1')) == '1'})
                follows += 1
            o['c2_map_object'] = 1
            o['c2_excluded'] = True          # never exported: the locator is
            o['c2_agent'] = u
            o['c2_object_label'] = label
            o['c2_spawn_point'] = spot
            o['c2_spawn_routes'] = routes
            o['c2_model'] = model if src is None else ''
            o['c2_break_choreos'] = group_choreos.get(node.get('Group'), [])
            if multi:
                tracks = sorted({r.split('|')[0] for r in routes})
                o['c2_spawn_tracks'] = tracks
            made += 1
    if missing:
        report.append('not found: %s' % ', '.join(sorted(missing)[:12]))
    scene['c2_follow_count'] = follows
    if follows:
        # the timeline covers the replayed race (frame_start = the race start)
        import choreoplace
        fps = scene.render.fps / scene.render.fps_base
        end = scene.frame_start + int(choreoplace.HORIZON * fps)
        if scene.frame_end < end:
            scene.frame_end = end
        report.append('%d spline-following spawns (play the timeline to see them move)' % follows)
        animate(scene)
    from . import modecols
    try:
        modecols.organize(scene)            # single-mode copies into their mode's collection
    except Exception as e:
        print('Cars 2 Scene: mode collections not organized: %s' % e)
    return made


MAX_FOLLOW_COPIES = 40          # spawns shown per actor and spawn point (the replay runs 75 s)


def _spawn_list(spots, spawns):
    """[(spot, routes, t0, event)]: a static copy per spot (t0 None), or a
    timed copy per spawn of a spline follower."""
    if not spawns:
        return [(spot, routes, None, '') for spot, routes in sorted(spots.items())]
    out, per = [], {}
    for (spot, t, ev), routes in sorted(spawns.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if per.get(spot, 0) >= MAX_FOLLOW_COPIES:
            continue
        per[spot] = per.get(spot, 0) + 1
        out.append((spot, sorted(set(routes)), t, ev))
    return out


def _nearest_spline(scene, names, loc):
    """Of the splines an actor can ride, the one nearest its spawn point."""
    if len(names) == 1:
        return names[0]
    root = scene.c2.root
    q = np.array((root.matrix_world.inverted() @ loc.matrix_world).translation)
    best = (1e30, names[0])
    for n in names:
        po = _path_object(scene, n)
        if po is None:
            continue
        pts, _c = _path_points(scene, po)
        if len(pts):
            best = min(best, (float(np.min(np.linalg.norm(pts - q, axis=1))), n))
    return best[1]


# ----------------------------------------------------------------------
# spline followers on the timeline
# ----------------------------------------------------------------------

_motion_cache = {}


def _event_at():
    import choreoplace
    return choreoplace.EVENT_AT


def _motion(o, f, path, spot):
    """choreo.PathMotion of copy o (cached until its path or spawn point moves).
    path: (points, closed, key) of its path; spot: its spawn point (game space)."""
    if path is None or len(path[0]) < 2:
        return None
    pts, closed, pkey = path
    key = (pkey, None if spot is None else tuple(np.round(spot, 3)), o.get('c2_follow'))
    hit = _motion_cache.get(o.name)
    if hit is not None and hit[0] == key:
        return hit[1]
    m = choreo.PathMotion(pts, f.get('pct', 0.0), 0.0, f.get('vmax', 0.0), f.get('accel', 0.0), closed)
    if f.get('nearest') and spot is not None:
        m = choreo.PathMotion(pts, 0.0, 0.0, f.get('vmax', 0.0), f.get('accel', 0.0), closed, start=m.nearest(spot))
    _motion_cache[o.name] = (key, m)
    return m


_anim = {}         # animate()'s per-scene cache: the copies, their settings, paths and spawn points


def _anim_state(scene):
    """(copies [(object, settings)], paths {name: object}, spots {name: object}),
    gathered once and again only when objects are added / removed or the
    Track / Mode picker changes (scanning every object each frame is slow)."""
    key = (len(scene.objects), scene.get('c2_follow_count'), scene.c2.track_variant, scene.c2.track_mode)
    st = _anim.get(scene.name)
    if st is not None and st[0] == key:
        try:
            for o, _f in st[1]:
                o.name                      # raises if the object was deleted
            return st[1], st[2], st[3]
        except ReferenceError:
            pass
    copies = []
    for o in scene.objects:
        js = o.get('c2_follow')
        if js is None or o.get('c2_variant_saved') is not None:
            continue
        try:
            copies.append((o, json.loads(js)))
        except ValueError:
            pass
    used = {f['path'] for _o, f in copies}
    names = {o.get('c2_spawn_point', '') for o, _f in copies}
    paths, spots = {}, {}
    for x in scene.objects:
        if x.type == 'CURVE' and x.c2.kind == 'PATH':
            if x.get('c2_path_name', x.name) in used:
                paths.setdefault(x.get('c2_path_name', x.name), x)
        elif x.get('c2_name', x.name) in names and not x.get('c2_spawn_master') and not x.get('c2_map_object'):
            spots.setdefault(x.get('c2_name', x.name), x)
    _anim[scene.name] = (key, copies, paths, spots)
    return copies, paths, spots


def animate(scene):
    """Put every spline-following copy where the game has it at the current
    frame; before it spawns and after the logic kills it, it is scaled to
    nothing (the game renderer skips it)."""
    root = scene.c2.root
    if root is None:
        return
    fps = scene.render.fps / scene.render.fps_base
    now = (scene.frame_current - scene.frame_start) / fps
    copies, path_objs, spot_objs = _anim_state(scene)
    if not copies:
        return
    # paths and spawn points once per frame (shared by every copy)
    inv = root.matrix_world.inverted()
    paths = {}
    for name, po in path_objs.items():
        pts, closed = _path_points(scene, po)
        paths[name] = (pts, closed, (hash(pts.tobytes()), closed))
    spots = {n: np.array((inv @ x.matrix_world).translation) for n, x in spot_objs.items()}
    rmw = root.matrix_world
    for o, f in copies:
        m = _motion(o, f, paths.get(f['path']), spots.get(o.get('c2_spawn_point', '')))
        t = now - f['t0']
        alive = m is not None and t >= 0.0 and not (f.get('kill') and not m.closed and t > m.travel)
        # not spawned yet / killed: scaled to nothing at its spawn point. Hiding
        # it instead (hide_set, hide_viewport) has Blender re-evaluate the
        # visibility of every object in the file, ~100 ms per spawn
        state = (m.distance(t), id(m)) if alive else 'gone'
        if _last_pos.get(o.name) == state:
            continue                        # unchanged: no update for the renderer
        _last_pos[o.name] = state
        if alive:
            o.matrix_world = rmw @ Matrix(m.matrix(t).tolist())
        else:
            spot = spots.get(o.get('c2_spawn_point', ''))
            at = Vector(spot) if spot is not None else Vector()
            o.matrix_world = rmw @ Matrix.Translation(at) @ Matrix.Diagonal((0.0, 0.0, 0.0, 1.0))
        if o.hide_get() or o.hide_viewport or o.hide_render:
            # hidden by 0.21.0 (which toggled visibility): shown for good
            o.hide_set(False)
            o.hide_viewport = o.hide_render = False


_last_pos = {}


@bpy.app.handlers.persistent
def _frame_change(scene, depsgraph=None):
    if scene.get('c2_follow_count') and scene.c2.root is not None:
        animate(scene)


def _kids(scene):
    """{parent pointer: [children]} of the scene's objects, built once.
    Object.children_recursive scans every object per call: calling it for each
    of italy's ~7000 hidden objects made every Track / Mode / Lap switch take
    ~30 s."""
    kids = {}
    for x in scene.objects:
        if x.parent is not None:
            kids.setdefault(x.parent.as_pointer(), []).append(x)
    return kids


def _tree(o, kids):
    """o and everything parented under it (kids from _kids)."""
    out, stack = [], [o]
    while stack:
        x = stack.pop()
        out.append(x)
        stack.extend(kids.get(x.as_pointer(), ()))
    return out


def variant_hidden(scene, variant, mode, kids, out):
    """Add to `out` (object pointer -> object) the spawned copies route `variant` in
    `mode` doesn't spawn, with what hangs under them (trackvariant.apply hides them)."""
    key = '%s|%s' % (variant, mode)
    for o in scene.objects:
        routes = o.get('c2_spawn_routes')
        if routes is None and o.get('c2_map_part') and o.parent is not None:
            routes = o.parent.get('c2_spawn_routes')
        if routes is None or key in list(routes):
            continue
        # the object and what hangs under it (a collect gate's arch parts):
        # hiding a parent doesn't hide its children in Blender
        for x in _tree(o, kids):
            out[x.as_pointer()] = x


def apply_variant(scene, variant, mode, saved_key='c2_variant_saved'):
    """Track / mode preview (trackvariant.apply): hide the copies that route
    and mode don't spawn. Returns how many were hidden."""
    n = 0
    key = '%s|%s' % (variant, mode)
    kids = _kids(scene)
    for o in scene.objects:
        routes = o.get('c2_spawn_routes')
        if routes is None and o.get('c2_map_part') and o.parent is not None:
            routes = o.parent.get('c2_spawn_routes')
        if routes is None or key in list(routes):
            continue
        # the object and what hangs under it (a collect gate's arch parts):
        # hiding a parent doesn't hide its children in Blender
        for x in _tree(o, kids):
            if saved_key not in x:
                x[saved_key] = [int(x.hide_viewport), int(x.hide_render)]
            x.hide_viewport = True
            x.hide_render = True
        n += 1
    animate(scene)
    return n


# ----------------------------------------------------------------------
# break preview
# ----------------------------------------------------------------------

BREAK_PREFIX = 'C2 Break: '
GRAVITY = 9.81


def _float(actor, key, default):
    try:
        return float(kv_of(actor, key, default))
    except (TypeError, ValueError):
        return default


def clear_break(scene):
    cols = [c for c in bpy.data.collections if c.name.startswith(BREAK_PREFIX)]
    common.remove_ids([o for c in cols for o in c.objects], cols, data=False)
    for o in scene.objects:
        if o.get('c2_break_hidden') is not None:
            o.hide_viewport = o.hide_render = bool(o['c2_break_hidden'])
            del o['c2_break_hidden']


def preview_break(context, obj, speed=15.0, seconds=3.0, seed=0):
    """Animate the exploder of a placed copy: the whole disappears and its
    pieces fly off the way MExploder throws them (rigid bodies in game: an
    approximation, see the README). Returns (pieces, report lines)."""
    scene = context.scene
    clear_break(scene)
    u = obj.get('c2_agent')
    actor = actors_by_uuid(scene).get(u)
    piece = kv_of(actor, 'exp/piece_name', '')
    lines = []
    if not piece:
        return 0, ['%s has no exploder pieces (no MExploder piece_name)' % obj.get('c2_object_label', obj.name)]
    from . import trackvariant
    pieces = [x for n, objs in trackvariant._objects_by_name(scene, fresh=True).items() if n.startswith(piece)
              for x in objs if x.type == 'MESH']
    if not pieces:
        return 0, ['no pieces named %s* in the scene' % piece]
    col = bpy.data.collections.new(BREAK_PREFIX + obj.name)
    scene.collection.children.link(col)
    col['c2_preview'] = True
    fps = scene.render.fps / scene.render.fps_base
    n = max(2, int(seconds * fps))
    f0 = scene.frame_current
    base = obj.matrix_world.copy()
    fade = _float(actor, 'FadeOverTime', 1.0) > 0.5
    up = _float(actor, 'SpawnUp', 0.0) > 0.5
    rng = np.random.default_rng(seed)
    root = scene.c2.root
    world_up = Vector((0, 0, 1))
    center = base.translation
    from .preview import bake
    for k, p in enumerate(pieces):
        o = bpy.data.objects.new('%s_break_%d' % (p.name, k), p.data)
        col.objects.link(o)
        o['c2_excluded'] = True
        # the pieces are authored around the template's origin, like the whole
        start = base @ (root.matrix_world.inverted() @ p.matrix_world if root else p.matrix_world)
        o.matrix_world = start
        o.rotation_mode = 'XYZ'
        pos0 = start.translation.copy()
        d = pos0 - center
        d.z = 0.0
        d = d.normalized() if d.length > 1e-4 else Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), 0)).normalized()
        # the car's push (InheritVelocity) plus the exploder's own velocityRange (cm/s)
        vr = _float(actor, 'velocityRange1', 300.0) / 100.0
        v = d * (speed * rng.uniform(0.4, 1.0) + vr) + world_up * (speed * rng.uniform(0.3, 0.8) + (vr if up else 0.0))
        spin = Vector(rng.uniform(-6, 6, 3))
        frames = np.arange(n + 1, dtype=np.float32) + f0
        for axis in range(3):
            vals = [pos0[axis] + v[axis] * (i / fps) - (0.5 * GRAVITY * (i / fps) ** 2 if axis == 2 else 0.0)
                    for i in range(n + 1)]
            o.location = pos0
            bake(o, 'location', axis, frames, vals)
        r0 = start.to_euler()
        for axis in range(3):
            bake(o, 'rotation_euler', axis, frames, [r0[axis] + spin[axis] * i / fps for i in range(n + 1)])
        if fade:
            s0 = start.to_scale()
            for axis in range(3):
                bake(o, 'scale', axis, frames, [s0[axis] * max(0.0, 1.0 - i / n) for i in range(n + 1)])
    obj['c2_break_hidden'] = int(obj.hide_viewport)
    obj.hide_viewport = True
    obj.hide_render = True
    scene.frame_end = max(scene.frame_end, f0 + n)
    lines.append('%d pieces (%s*), %s' % (len(pieces), piece, 'fading' if fade else 'not fading'))
    return len(pieces), lines


# ----------------------------------------------------------------------
# operators and panel
# ----------------------------------------------------------------------

# the .ap settings shown for a map object (when its section has them)
SETTINGS = (
    ('pickupType', 'Pickup Type'),
    ('respawnDelay', 'Respawn Delay'),
    ('model_name', 'Model'),
    ('exp/piece_name', 'Pieces'),
    ('velocityRange1', 'Piece Velocity 1'),
    ('velocityRange2', 'Piece Velocity 2'),
    ('velocityRange3', 'Piece Velocity 3'),
    ('velocityRange4', 'Piece Velocity 4'),
    ('velocityRange5', 'Piece Velocity 5'),
    ('FadeOverTime', 'Fade Pieces'),
    ('UseRigidBodyPieces', 'Rigid Body Pieces'),
    ('InheritVelocity', 'Inherit Velocity'),
    ('SpawnUp', 'Spawn Up'),
    ('CollideTerrain', 'Collide Terrain'),
    ('surfaceType', 'Surface'),
)


class C2_OT_map_objects_rebuild(bpy.types.Operator):
    """Show every spawned map object (bashables, breakables, pickups) at its spawn points again"""
    bl_idname = 'c2.map_objects_rebuild'
    bl_label = 'Rebuild Map Objects'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        g = _graph(scene)
        if g is None:
            self.report({'ERROR'}, 'No .mer next to the imported .oct')
            return {'CANCELLED'}
        report = []
        n = build(context, scene, g, report)
        from . import trackvariant
        trackvariant.update(scene.c2, context)
        self.report({'INFO'}, '%d map objects placed%s' % (n, ('; ' + report[0]) if report else ''))
        return {'FINISHED'}


class C2_OT_map_object_select_spot(bpy.types.Operator):
    """Select the locator this object spawns at: move it to move the object (the locator is what gets exported)"""
    bl_idname = 'c2.map_object_select_spot'
    bl_label = 'Select Spawn Point'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import trackvariant
        o = context.object
        if o is not None and o.get('c2_map_part') and o.parent is not None:
            o = o.parent
        spot = o.get('c2_spawn_point') if o else None
        loc = next(iter(trackvariant._objects_by_name(context.scene, fresh=True).get(spot, ())), None) if spot else None
        if loc is None:
            self.report({'ERROR'}, 'Spawn point not found')
            return {'CANCELLED'}
        for x in context.selected_objects:
            x.select_set(False)
        loc.hide_viewport = False
        loc.hide_set(False)
        loc.select_set(True)
        context.view_layer.objects.active = loc
        return {'FINISHED'}


class C2_OT_map_object_break(bpy.types.Operator):
    """Preview the active map object breaking: its exploder pieces fly off from the current frame"""
    bl_idname = 'c2.map_object_break'
    bl_label = 'Preview Break'
    bl_options = {'REGISTER', 'UNDO'}
    speed: bpy.props.FloatProperty(name='Car Speed (m/s)', default=15.0, min=0.0,
                                   description='Speed of the car that hits it (the pieces inherit it)')
    seconds: bpy.props.FloatProperty(name='Seconds', default=3.0, min=0.2)

    def execute(self, context):
        o = context.object
        if o is not None and o.get('c2_map_part') and o.parent is not None:
            o = o.parent
        if o is None or not o.get('c2_map_object'):
            self.report({'ERROR'}, 'Select a map object')
            return {'CANCELLED'}
        n, lines = preview_break(context, o, self.speed, self.seconds)
        self.report({'INFO'} if n else {'WARNING'}, '; '.join(lines))
        return {'FINISHED'} if n else {'CANCELLED'}


class C2_OT_map_object_break_clear(bpy.types.Operator):
    """Remove the break preview and show the object again"""
    bl_idname = 'c2.map_object_break_clear'
    bl_label = 'Clear Break Preview'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        clear_break(context.scene)
        return {'FINISHED'}


class C2_OT_map_object_choreo(bpy.types.Operator):
    """Preview a choreography this object's own logic plays (its breaking animation)"""
    bl_idname = 'c2.map_object_choreo'
    bl_label = 'Preview Animation'
    bl_options = {'REGISTER', 'UNDO'}
    node_index: bpy.props.IntProperty()

    def execute(self, context):
        sc = context.scene.c2
        k = next((k for k, c in enumerate(sc.choreos) if c.node_index == self.node_index), None)
        if k is None:
            self.report({'ERROR'}, 'Choreography not in the list')
            return {'CANCELLED'}
        sc.active_choreo = k
        return bpy.ops.c2.choreo_preview('EXEC_DEFAULT', look_through=False)


def draw_panel(layout, context):
    """The Map Objects box of the Scene panel (for the active object)."""
    scene = context.scene
    o = context.object
    box = layout.box()
    n = sum(1 for x in scene.objects if x.get('c2_map_object') and not x.get('c2_map_part'))
    row = box.row()
    row.label(text='Map Objects: %d' % n, icon='OUTLINER_OB_GROUP_INSTANCE')
    row.operator('c2.map_objects_rebuild', text='', icon='FILE_REFRESH')
    if o is None:
        return
    if o.get('c2_map_part') and o.parent is not None:
        o = o.parent
    tmpl = o.get('c2_spawn_master') or o.get('c2_exploder_piece')
    if tmpl:
        box.label(text='Template of %s: the game spawns copies of it' % tmpl, icon='INFO')
        return
    if not o.get('c2_map_object'):
        return
    col = box.column(align=True)
    col.label(text=o.get('c2_object_label', o.name), icon='OBJECT_DATA')
    row = col.row(align=True)
    row.label(text='Spawns at: %s' % o.get('c2_spawn_point', '?'))
    row.operator('c2.map_object_select_spot', text='', icon='RESTRICT_SELECT_OFF')
    if o.get('c2_follow'):
        f = json.loads(o['c2_follow'])
        col.label(text='Rides %s at %.0f m/s from %.2f s' % (f['path'], f.get('vmax', 0.0), f['t0']), icon='CURVE_PATH')
        if f.get('event'):
            col.label(text='After %s (sent %.0f s in)' % (f['event'], _event_at()), icon='TIME')
    tracks = list(o.get('c2_spawn_tracks', []))
    if tracks:
        col.label(text='Tracks: %s' % ', '.join(tracks))
        if len(tracks) > 1:
            col.label(text='Shared spawn point: moving it moves it on every listed track', icon='ERROR')
            row = col.row(align=True)
            row.label(text='Split for track:')
            for t in tracks:
                row.operator('c2.map_object_split', text=t).track = t
    own = next((x for x in splits(scene) if x['object'] == o.get('c2_spawn_point')), None)
    if own is not None:
        row = col.row(align=True)
        row.label(text='Track %s copy of %s' % (own['track'], own['spot']), icon='LINKED')
        row.operator('c2.map_object_merge', text='', icon='X')
    modes = sorted({r.split('|')[1] for r in o.get('c2_spawn_routes', []) if '|' in r} - {''})
    if modes:
        col.label(text='Modes: %s' % ', '.join(modes))
    actor = actors_by_uuid(scene).get(o.get('c2_agent', ''))
    if actor is not None:
        sb = box.column(align=True)
        sb.label(text='Settings (.ap: %s)' % actor.name, icon='PROPERTIES')
        kvs = {kv.key: kv for kv in actor.kvs}
        for key, label in SETTINGS:
            kv = kvs.get(key)
            if kv is not None:
                sb.prop(kv, 'value', text=label)
    row = box.row(align=True)
    row.operator('c2.map_object_break', icon='MOD_EXPLODE')
    row.operator('c2.map_object_break_clear', text='', icon='X')
    for ci in o.get('c2_break_choreos', []):
        c = next((c for c in scene.c2.choreos if c.node_index == ci), None)
        if c is not None:
            r = box.row()
            r.label(text=c.choreography_name.replace(chr(92), '/').split('/')[-1], icon='SEQUENCE')
            r.operator('c2.map_object_choreo', text='', icon='PLAY').node_index = ci


def _graph(scene):
    import c2oct
    import meridian
    path = os.path.splitext(bpy.path.abspath(scene.c2.oct_path))[0] + '.mer'
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        return meridian.Graph(c2oct.decode(f.read())[0])


def _locator(scene, name):
    from . import trackvariant
    for fresh in (False, True):
        try:
            return next((o for o in trackvariant._objects_by_name(scene, fresh).get(name, ())
                         if not o.get('c2_spawn_master')), None)
        except ReferenceError:      # an object of the cached map was deleted
            continue
    return None


def _rebuild(context):
    from . import trackvariant
    scene = context.scene
    n = build(context, scene, _graph(scene))
    trackvariant.update(scene.c2, context)
    return n


class C2_OT_map_object_split(bpy.types.Operator):
    """Give this track its own copy of the shared spawn point: a new locator (start at the same place) that
    only this track spawns the object at, so moving it leaves the other tracks alone. Written into the
    logic on export"""
    bl_idname = 'c2.map_object_split'
    bl_label = 'Split for Track'
    bl_options = {'REGISTER', 'UNDO'}
    track: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        o = context.object
        if o is not None and o.get('c2_map_part') and o.parent is not None:
            o = o.parent
        if o is None or not o.get('c2_map_object'):
            self.report({'ERROR'}, 'Select a map object')
            return {'CANCELLED'}
        spot = o.get('c2_spawn_point')
        tracks = list(o.get('c2_spawn_tracks', []))
        if self.track not in tracks or len(tracks) < 2:
            self.report({'ERROR'}, 'This spawn point is not shared with track %s' % self.track)
            return {'CANCELLED'}
        lst = splits(scene)
        if any(x['spot'] == spot and x['track'] == self.track for x in lst):
            self.report({'ERROR'}, '%s is already split for track %s' % (spot, self.track))
            return {'CANCELLED'}
        loc = _locator(scene, spot)
        if loc is None or loc.c2.meridian != 'LOCATOR':
            self.report({'ERROR'}, 'Spawn point %s is not a Meridian locator' % spot)
            return {'CANCELLED'}
        name = '%s_%s' % (spot, self.track)
        new = loc.copy()
        for c in loc.users_collection:
            c.objects.link(new)
        new.name = name
        new['c2_name'] = name
        # a new scene node and a new Meridian locator on export
        new.c2.node_index = -1
        new.c2.mer_index = -1
        for k in ('c2_variant_saved', 'c2_map_saved'):
            if k in new:
                del new[k]
        new.hide_viewport = False
        lst.append({'spot': spot, 'track': self.track, 'object': name})
        _save_splits(scene, lst)
        _rebuild(context)
        for x in context.selected_objects:
            x.select_set(False)
        new.select_set(True)
        context.view_layer.objects.active = new
        self.report({'INFO'}, 'Track %s now spawns at %s (selected: move it)' % (self.track, name))
        return {'FINISHED'}


class C2_OT_map_object_merge(bpy.types.Operator):
    """Undo a split: this track spawns at the shared spawn point again (the track's own locator is deleted)"""
    bl_idname = 'c2.map_object_merge'
    bl_label = 'Merge Back'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        o = context.object
        if o is not None and o.get('c2_map_part') and o.parent is not None:
            o = o.parent
        spot = o.get('c2_spawn_point') if o is not None else None
        lst = splits(scene)
        hit = [x for x in lst if x['object'] == spot]
        if not hit:
            self.report({'ERROR'}, 'Not a track-specific spawn point')
            return {'CANCELLED'}
        loc = _locator(scene, spot)
        if loc is not None:
            bpy.data.objects.remove(loc)
        _save_splits(scene, [x for x in lst if x['object'] != spot])
        _rebuild(context)
        self.report({'INFO'}, 'Track %s spawns at %s again' % (hit[0]['track'], hit[0]['spot']))
        return {'FINISHED'}


def export_splits(scene, g, obj_ts, report):
    """Write the per-track spawn points into the graph (core/spawnsplit.py).
    obj_ts: {object name: TransformSource UUID} of the export's locators."""
    import spawnsplit
    import variants
    lst = splits(scene)
    if not lst:
        return
    by_c2 = {}
    for o in scene.objects:
        if o.get('c2_name') and o.name in obj_ts:
            by_c2.setdefault(o['c2_name'], o.name)
    routes = variants.variant_names(g)
    groups = {}
    for x in lst:
        groups.setdefault(x['spot'], {})[x['track']] = x['object']
    for spot, per in sorted(groups.items()):
        ts = obj_ts.get(by_c2.get(spot, spot))
        values = {t: obj_ts.get(by_c2.get(n, n)) for t, n in per.items()}
        if ts is None or not all(values.values()):
            report.warn('spawn point %s: split not written (locator missing)' % spot)
            continue
        msgs = []
        n = spawnsplit.split(g, ts, values, routes, msgs, spot)
        for m in msgs:
            report.info(m)
        if n:
            report.info('spawn point %s: own copy for track %s' % (spot, ', '.join(sorted(per))))


_classes = (C2_OT_map_objects_rebuild, C2_OT_map_object_select_spot, C2_OT_map_object_break,
            C2_OT_map_object_break_clear, C2_OT_map_object_choreo, C2_OT_map_object_split,
            C2_OT_map_object_merge)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if _frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_frame_change)


def unregister():
    if _frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
