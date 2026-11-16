"""Collect mode (Survival): its gates, their ring targets and the collect line, per lap.

A Collect race sends the player through gates along a collect line. The
world's Meridian graph has one CollectGate node per gate: two gate posts
(TransformFetch from locators such as Tokyo_B_Collect:Gate13 / Gate14), the
tracks it belongs to (TrackAGate / TrackBGate / TrackCGate), the laps it
stands on (LapStartGate, Lap1Gate .. Lap3Gate, AlternateRouteGate) and the
pickup spacing behind it. The line is one long .dbl path per track
(..._CollectLineTrackA): laps 1, 2 and 3 one after the other. Nothing in the
world file draws a gate: the game builds it at run time (CollectGameInf,
sdaemon.exe 0x506300 / 0x50c3a0 / 0x567d70; core/collectline.py has the line
side):

* centre = the middle of the two posts, on the GROUND below it
  (the game's ground-height lookup), direction = post 1 - post 2
* height = gate post 1's Y scale (5 without a post)
* the choreography SurvivalGate_Elements, placed by its anchor
  ANC_SurvivalGate there and scaled (post distance / 30, height / 5, 1): the
  hologram spans the gap; its side scanlines open and close on a 12.9 s loop
* two Objects\\Survival_GateTarget rings where the collect line crosses the
  gate (for each lap the gate is on), 3.5 m above the ground
* the lap gate (LapStartGate) only counts laps: no hologram, no rings
* a gate's order within its lap is its progress along the race spline
  TrackA/B/C - the names ("A 1:4") are labels only

The import shows each gate that way, in the world's Collect Gates collection:
an empty with the hologram's parts under it (sharing the package's meshes; an
instanced collection made the Cars 2 Game renderer rebuild every draw each
frame), the rings, and the collect line cut into one curve per lap (CollectLineALap1,
CollectLineALap2 ...; joined back into the one path on export, drawn over the
viewport in a colour per lap with its name). Animate Gates gives the parts one
shared action per part (off by default: nothing per frame). All of it is tagged
with its track, the Collect mode and its laps, so the Track / Mode / Lap pickers
show what the game shows on that lap. Moving a post or the gate moves and
resizes its gate and its rings; rings follow the collect line (edit the line, in
Edit Mode too, and they move to where it now crosses); scaling post 1 on Y changes
the gate's height.

Gates can be added (two new post locators and a CollectGate node, written on
export), taken off a track (its node stays, on no track - the game skips it)
and relabelled. Their laps and tracks are the node's own flags (also listed in
Gameplay Node Settings).
"""
import json
import math
import os
import re

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import common

PACKAGE = 'SurvivalGate_Elements'
TARGET_MODEL = 'Objects\\Survival_GateTarget'
TEMPLATE = 'C2 Collect Gate (animated)'
TEMPLATE_WIRE = 'C2 Collect Lap Gate (outline)'
WIDTH = 30.0                # the arch's span at scale 1 (the game divides the post distance by 30)
HEIGHT = 5.0                # and the height by 5
TRACK_FLAGS = ('TrackAGate', 'TrackBGate', 'TrackCGate')
LAP_FLAGS = ('LapStartGate', 'Lap1Gate', 'Lap2Gate', 'Lap3Gate', 'AlternateRouteGate')
FLAGS = TRACK_FLAGS + LAP_FLAGS
LAPS = (('LapStartGate', 'Start'), ('Lap1Gate', 'Lap 1'), ('Lap2Gate', 'Lap 2'), ('Lap3Gate', 'Lap 3'),
        ('AlternateRouteGate', 'Alternate route'))
SETTINGS = ('GateToFirstPickupDistance', 'PickupToPickupDistance', 'BonusPickupDistance')


def _cl():
    import collectline
    return collectline


# ----------------------------------------------------------------------
# gate data
# ----------------------------------------------------------------------

def gates(g):
    """[{'name', 'node', 'uuid', 'posts': (post 1, post 2 locator names), 'flags',
    'settings', 'tracks', 'laps'}] of the world graph's CollectGate nodes."""
    import meridian
    out = []
    for i in g.of_type('CollectGate'):
        n = g.nodes[i]
        posts = []
        for field in ('GatePost1', 'GatePost2'):
            port = n.get(field)
            srcs = [g.links[li]['Origin'] for li in g.in_links.get(port, [])] if isinstance(port, int) else []
            loc = g.locator_of_transform_source(srcs[0]) if srcs else None
            posts.append(g.nodes[loc].get('BoundObjectName') if loc is not None else None)
        flags = {k: int(bool(n.get(k, 0))) for k in FLAGS}
        gt = {'name': n.get('AuthoredName', 'Collect Gate %d' % i), 'node': i, 'uuid': meridian.uuid_hex(n.get('Uuid')),
              'posts': posts, 'flags': flags, 'settings': {k: float(n[k]) for k in SETTINGS if k in n}}
        gt['orig_name'] = gt['name']
        out.append(_derive(gt))
    return out


def _derive(gt):
    """tracks / laps lists from the flags (shown in the panel, used by the pickers)."""
    f = gt['flags']
    gt['tracks'] = [t for t, k in zip('ABC', TRACK_FLAGS) if f.get(k)]
    gt['laps'] = [label for key, label in LAPS if f.get(key)]
    return gt


def gate_data(o):
    js = o.get('c2_collect_gate')
    if js is None:
        return None
    gt = json.loads(js)
    if 'flags' not in gt:           # a gate from an older version: flags from its lists
        gt['flags'] = {k: 0 for k in FLAGS}
        for t in gt.get('tracks', ()):
            gt['flags']['Track%sGate' % t] = 1
        for key, label in LAPS:
            if label in gt.get('laps', ()):
                gt['flags'][key] = 1
    return gt


def _settings_item(scene, uuid):
    if not uuid:
        return None
    return next((it for it in scene.c2.node_settings if it.uuid == uuid), None)


def flags_of(scene, o):
    """The gate's flags now: an imported gate's from its Gameplay Node Settings
    entry (where they can also be edited by hand), a new gate's from itself."""
    gt = gate_data(o)
    if gt is None:
        return {}
    f = dict(gt['flags'])
    it = _settings_item(scene, gt.get('uuid'))
    if it is not None:
        for kv in it.kvs:
            if kv.key in FLAGS:
                try:
                    f[kv.key] = int(bool(int(float(kv.value))))
                except ValueError:
                    pass
    return f


def _laps_of(f):
    """1..3 the gate shows on (the lap gate and alternate-route gates: every lap)."""
    laps = [k for k in (1, 2, 3) if f.get('Lap%dGate' % k)]
    if f.get('LapStartGate') or not laps:
        return [1, 2, 3]
    return laps


def _tag(o, f):
    """Picker tags of a gate: its tracks in Collect mode, its laps."""
    o['c2_spawn_routes'] = ['%s|Collect' % t for t, k in zip('ABC', TRACK_FLAGS) if f.get(k)] or ['-|Collect']
    o['c2_collect_laps'] = _laps_of(f)


def set_flags(scene, o, flags):
    """Write a gate's flags: on the gate, in its Gameplay Node Settings entry (exported from there)."""
    gt = gate_data(o)
    gt['flags'].update({k: int(bool(v)) for k, v in flags.items()})
    _derive(gt)
    o['c2_collect_gate'] = json.dumps(gt)
    it = _settings_item(scene, gt.get('uuid'))
    if it is not None:
        for kv in it.kvs:
            if kv.key in flags:
                kv.value = str(int(bool(flags[kv.key])))
    _tag(o, gt['flags'])
    _set_look(o, gt['flags'])


def base_name(o):
    """A gate's label: its object name without Blender's .001."""
    return re.sub(r'\.\d{3}$', '', o.name)


# ----------------------------------------------------------------------
# build
# ----------------------------------------------------------------------

def clear(scene, lines=False):
    objs = [o for o in bpy.data.objects if o.get('c2_collect_gate') or o.get('c2_collect_gate_part') or
            o.get('c2_collect_target')]
    old = [c for c in (bpy.data.collections.get(TEMPLATE), bpy.data.collections.get(TEMPLATE_WIRE)) if c is not None]
    common.remove_ids(objs, old, data=False)       # (old: 0.29.9's instanced hologram)
    _cache.clear()
    _posts['key'] = None
    _line_draw.clear()
    _line_list['key'] = None
    scene['c2_collect_gate_count'] = 0


def _gates_collection(scene):
    from . import gameplay
    return common.ensure_collection('%s Collect Gates' % scene.c2.world_name, gameplay.collections(scene)['MapObjects'])


def _lines_collection(scene):
    from . import gameplay
    return common.ensure_collection('%s Collect Lines' % scene.c2.world_name, gameplay.collections(scene)['Paths'])


def show_lines_collection(context, scene):
    """The Collect Lines collection shown in the viewport. It starts out under Paths, which
    the import hides (dense helpers), and kept that hidden state when moved: the lap lines
    were in the file but never drawn."""
    from . import gameplay
    cols = gameplay.collections(scene, create=False)
    for col in (bpy.data.collections.get('%s Collect Lines' % scene.c2.world_name), cols.get('Paths'),
                cols.get('Bridges')):      # (older imports started the .dbl paths hidden)
        lc = gameplay.layer_collection(context, col) if col is not None else None
        if lc is not None:
            lc.hide_viewport = False
            lc.exclude = False
            col.hide_viewport = False


def _cache_dir(scene):
    return os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)), '%s.oct.textures' % scene.c2.world_name)


def _library(context, scene, report):
    """(arch model collection or None, choreography package or None)."""
    from . import objplace
    import choreo
    assets = common.game_assets(scene)
    pkg = choreo.find_choreography(assets, PACKAGE) if assets else None
    lib = objplace.package_model(context, pkg, _cache_dir(scene), report) if pkg is not None else None
    if lib is None:
        report.append('collect gates: %s not found in the game assets - gates drawn as boxes' % PACKAGE)
    return lib, pkg


def _is_trigger(lo):
    effects = {s.material.c2.effect for s in lo.material_slots if s.material}
    # the trigger volume (LapFXTrig): never drawn; the lap pass effect (HoloGate_LapFx_*): only
    # while a car drives through
    return lo.hide_render or (effects and effects <= {'SolidColor', 'shadowonly'}) or 'lapfx' in lo.name.lower()


def _arch_parts(scene):
    """The hologram's drawn parts in the package model (not the trigger volume, not the
    pass effect), or []."""
    lib = bpy.data.collections.get(scene.get('c2_collect_gate_lib', ''))
    return [lo for lo in (lib.objects if lib is not None else ()) if lo.type == 'MESH' and not _is_trigger(lo)]


def _actions(scene, lib, pkg, report):
    """{part mesh name: action} - the hologram animated like the game plays it (a cyclic
    12.9 s loop: the side scanlines open and close). One action per part, shared by that
    part of every gate (Animate Gates)."""
    from . import preview
    out = {}
    try:
        import choreo
        ch = choreo.Choreography(pkg)
    except Exception as e:      # still drawn, just not animated
        report.append('collect gates: animation not read (%s)' % e)
        return out
    sc = pkg.scene()
    by_mesh = {('%s_%s' % (pkg.stem, n.get('NodeName', i))).lower(): i for i, n in enumerate(sc.nodes)}
    fps = scene.render.fps / scene.render.fps_base
    nfr = int(round(ch.duration * fps)) if ch.duration > 0 else 0
    if not nfr:
        return out
    frames = np.arange(1, nfr + 2, dtype=np.float32)
    states = None
    for lo in lib.objects:
        if lo.type != 'MESH' or _is_trigger(lo):
            continue
        name = 'C2 Collect Gate %s' % lo.data.name
        act = bpy.data.actions.get(name)
        if act is not None:
            out[lo.data.name] = act
            continue
        i = by_mesh.get(lo.data.name.lower())
        di = ch.bind.get(i) if i is not None else None
        if di is None:
            continue
        if states is None:
            states = [ch.evaluate(ch.duration * k / nfr) for k in range(nfr + 1)]
        rest = np.linalg.inv(np.asarray(ch.chain(di, {}), float))
        mats = [Matrix((np.asarray(ch.chain(di, st), float) @ rest).tolist()) @ lo.matrix_basis for st in states]
        if all(max(abs(x - y) for ra, rb in zip(m, mats[0]) for x, y in zip(ra, rb)) < 1e-6 for m in mats):
            continue
        tmp = bpy.data.objects.new('_c2_gate_anim', None)
        preview.bake_matrices(tmp, frames, mats)
        for fc in preview._fcurves(tmp):
            if not any(m.type == 'CYCLES' for m in fc.modifiers):
                fc.modifiers.new('CYCLES')
        act = tmp.animation_data.action
        act.name = name
        act.use_fake_user = True
        bpy.data.objects.remove(tmp)
        out[lo.data.name] = act
    return out


def _animate_part(c, on):
    """Give a gate part its shared hologram action (or take it off: the rest pose)."""
    act = bpy.data.actions.get('C2 Collect Gate %s' % c.data.name) if on and c.data is not None else None
    if act is None:
        if c.animation_data is not None and c.animation_data.action is not None:
            c.animation_data.action = None
        if c.get('c2_rest') is not None:
            r = list(c['c2_rest'])
            c.matrix_basis = Matrix([r[k * 4:k * 4 + 4] for k in range(4)])
        return
    ad = c.animation_data or c.animation_data_create()
    ad.action = act
    if hasattr(ad, 'action_slot') and ad.action_slot is None and hasattr(act, 'slots') and len(act.slots):
        try:
            ad.action_slot = act.slots[0]
        except (AttributeError, TypeError, RuntimeError):
            pass


def set_animation(scene, on):
    """Animate Gates on / off for every gate part (off: no keyframes at all - nothing
    for playback or the Cars 2 Game renderer to update per frame)."""
    n = 0
    for c in scene.objects:
        if c.get('c2_collect_gate_part') and c.parent is not None:
            _animate_part(c, on)
            n += 1
    return n


def _target_library(context, scene, report):
    from . import objplace
    assets = common.game_assets(scene)
    pkg = objplace.find_object_package(assets, TARGET_MODEL) if assets else None
    lib = objplace.package_model(context, pkg, _cache_dir(scene), report) if pkg is not None else None
    if lib is None:
        report.append('collect gates: %s not found in the game assets - rings not shown' % TARGET_MODEL)
    else:
        scene['c2_collect_ring_lib'] = lib.name
    return lib


def _set_look(o, f):
    """The lap gate draws as an outline only (the game draws nothing there; the Cars 2
    Game renderer skips wire objects)."""
    for c in o.children:
        if c.get('c2_collect_gate_part'):
            c.display_type = 'WIRE' if f.get('LapStartGate') else 'TEXTURED'


def make_gate(scene, gt, col=None):
    """The gate object of gate record `gt` (placed by place()): an empty with the
    hologram's parts under it (sharing the package's meshes)."""
    col = col or _gates_collection(scene)
    o = bpy.data.objects.new(gt['name'], None)
    col.objects.link(o)
    o['c2_collect_gate'] = json.dumps(gt)
    o['c2_excluded'] = True
    parts = _arch_parts(scene)
    o.empty_display_type = 'PLAIN_AXES' if parts else 'CUBE'
    o.empty_display_size = 0.5 if parts else 1.0
    anim = getattr(scene.c2, 'collect_gate_anim', False)
    for lo in parts:
        c = bpy.data.objects.new('%s %s' % (gt['name'], lo.name.split('_', 1)[-1].split('.')[0]), lo.data)
        col.objects.link(c)
        c.parent = o
        c.matrix_parent_inverse.identity()
        c.matrix_basis = lo.matrix_basis.copy()
        c['c2_rest'] = [x for r in lo.matrix_basis for x in r]
        c['c2_excluded'] = True
        c['c2_collect_gate_part'] = 1
        c.hide_select = True            # pick the gate (or its posts), not the hologram's parts
        if anim:
            _animate_part(c, True)
    _tag(o, gt['flags'])
    _set_look(o, gt['flags'])
    return o


def build(context, scene, g, report=None):
    """The gates of the world graph between their posts, their rings, and the
    collect lines cut per lap. Returns how many gates."""
    report = report if report is not None else []
    clear(scene)
    lst = gates(g) if g is not None else []
    if not lst:
        return 0
    lib, pkg = _library(context, scene, report)
    if lib is not None:
        scene['c2_collect_gate_lib'] = lib.name
        _actions(scene, lib, pkg, report)
    col = _gates_collection(scene)
    made = 0
    for gt in lst:
        if not all(gt['posts']):
            continue
        make_gate(scene, gt, col)
        made += 1
    scene['c2_collect_gate_count'] = made
    place(scene, context.evaluated_depsgraph_get())
    n_lines = split_lines(scene)
    show_lines_collection(context, scene)
    _target_library(context, scene, report)
    n_t = refresh_targets(scene, context.evaluated_depsgraph_get())
    report.append('%d collect gates between their posts, %d rings, collect lines in %d laps (Collect mode)'
                  % (made, n_t, n_lines))
    return made


# ----------------------------------------------------------------------
# placing: between the posts, on the ground
# ----------------------------------------------------------------------

_cache = {}


def _post(scene, name):
    if not name:
        return None
    from .objplace import _locator
    o = _locator(scene, name)
    if o is None:
        o = scene.objects.get(name)
    return o


def _game(scene, m):
    return scene.c2.root.matrix_world.inverted() @ m


def _skip_hit(ob):
    ob = getattr(ob, 'original', ob)
    for x in (ob, ob.parent):
        if x is not None and (x.get('c2_excluded') or x.get('c2_preview') or x.get('c2_map_object') or
                              x.get('c2_collect_gate_part') or x.get('c2_map_part')):
            return True
    return False


def ground(scene, depsgraph, p_game, above=1.0, reach=80.0):
    """Game-space Y of the ground under game point p (the first surface the game's
    own world geometry has below a point `above` m over it), or None."""
    if depsgraph is None:
        return None
    rmw = scene.c2.root.matrix_world
    up = (rmw.to_3x3() @ Vector((0.0, 1.0, 0.0))).normalized()
    origin = rmw @ Vector((p_game[0], p_game[1] + above, p_game[2]))
    left = reach
    for _ in range(16):
        try:
            hit, loc, _n, _i, ob, _m = scene.ray_cast(depsgraph, origin, -up, distance=left)
        except Exception:
            return None
        if not hit:
            return None
        if ob is not None and _skip_hit(ob):
            step = (loc - origin).length + 0.01
            left -= step
            origin = loc - up * 0.01
            if left <= 0:
                return None
            continue
        return (rmw.inverted() @ loc)[1]
    return None


def gate_matrix(p1, p2, height, ground_y=None):
    """Game-space matrix of a gate's hologram (as the game places it)."""
    p1, p2 = np.array(p1, float), np.array(p2, float)
    centre = (p1 + p2) / 2.0
    if ground_y is not None:
        centre[1] = ground_y
    d = p1 - p2
    flat = np.array([d[0], 0.0, d[2]])
    span = float(np.linalg.norm(d))
    fl = float(np.linalg.norm(flat))
    x = flat / fl if fl > 1e-6 else np.array([1.0, 0.0, 0.0])
    y = np.array([0.0, 1.0, 0.0])
    z = np.cross(x, y)
    M = np.eye(4)
    M[:3, 0] = x * (span / WIDTH if span > 1e-6 else 1.0)
    M[:3, 1] = y * (height / HEIGHT)
    M[:3, 2] = z
    M[:3, 3] = centre
    return Matrix(M.tolist())


def post_positions(scene, o):
    """(post 1 game matrix, post 2 game matrix) of a gate object, or None."""
    gt = gate_data(o)
    if gt is None:
        return None
    try:
        a, b = (_post(scene, n) for n in gt['posts'])
        return _game(scene, a.matrix_world), _game(scene, b.matrix_world)
    except (AttributeError, ReferenceError, TypeError):
        return None


def place(scene, depsgraph=None, only=None):
    """Put every gate (or the gates named in `only`) between its posts, on the ground."""
    root = scene.c2.root
    if root is None:
        return 0
    rmw = root.matrix_world
    moved = 0
    for o in scene.objects:
        if o.get('c2_collect_gate') is None or (only is not None and o.name not in only):
            continue
        pp = post_positions(scene, o)
        if pp is None:
            continue
        m1, m2 = pp
        key = tuple(round(x, 5) for m in (m1, m2) for r in m for x in r)
        if _cache.get(o.name) == key:
            continue
        _cache[o.name] = key
        p1, p2 = m1.translation, m2.translation
        centre = (p1 + p2) / 2.0
        gy = ground(scene, depsgraph, centre)
        o.matrix_world = rmw @ gate_matrix(p1, p2, abs(m1.to_scale()[1]) or HEIGHT, gy)
        o['c2_placed'] = _flat(o.matrix_world)
        moved += 1
    return moved


def _flat(m):
    return [float(x) for r in m for x in r]


def _user_moved(o):
    """Was o moved since this module last placed it (by the user, not by place())?"""
    pl = o.get('c2_placed')
    if pl is None:
        return False
    return max(abs(a - b) for a, b in zip(_flat(o.matrix_world), pl)) > 1e-4


def move_posts_with_gate(scene, o):
    """The gate itself was moved / turned / stretched: its posts follow, so the gate
    stays where it was put (place() then sets it on the ground between them)."""
    gt = gate_data(o)
    old = Matrix([list(o['c2_placed'])[k * 4:k * 4 + 4] for k in range(4)])
    new = o.matrix_world.copy()
    try:
        inv = old.inverted()
    except ValueError:
        return False
    for n in gt['posts']:
        po = _post(scene, n)
        if po is None:
            continue
        m = po.matrix_world.copy()
        m.translation = new @ (inv @ m.translation)
        po.matrix_world = m
    _cache.pop(o.name, None)
    return True


# ----------------------------------------------------------------------
# collect lines: one curve per lap
# ----------------------------------------------------------------------

def _line_letter(name):
    m = re.search(r'CollectLineTrack([ABC])', name)
    return m.group(1) if m else None


def line_pieces(scene):
    """{line path name: [piece objects in lap order]}."""
    out = {}
    for o in scene.objects:
        n = o.get('c2_collect_line')
        if n and o.type == 'CURVE':
            out.setdefault(n, []).append(o)
    for v in out.values():
        v.sort(key=lambda o: o.get('c2_collect_lap', 0))
    return out


def line_points(scene, pieces):
    """Game-space points of a whole collect line from its lap pieces."""
    from . import gameplay
    return _cl().join([gameplay.game_points(scene, p, gameplay._curve_points(p)[0]) for p in pieces])


def _gate_records(scene, letter=None):
    """[(gate object, flags, p1, p2)] of the gates (of one track) with both posts."""
    out = []
    for o in scene.objects:
        if o.get('c2_collect_gate') is None:
            continue
        try:
            f = flags_of(scene, o)
            if letter is not None and not f.get('Track%sGate' % letter):
                continue
            pp = post_positions(scene, o)
            if pp is None:
                continue
            out.append((o, f, tuple(pp[0].translation), tuple(pp[1].translation)))
        except Exception as e:      # one gate the editor can't read (an older file's?) doesn't stop the rest
            key = '%s: %s' % (o.name, e)
            if key not in _reported:
                _reported.add(key)
                print('Cars 2 Scene: collect gate %s skipped: %s' % (o.name, e))
    return out


def split_lines(scene):
    """Cut every collect line path (a .dbl path named ..._CollectLineTrackA/B/C)
    into one curve per lap at its track's lap gate. Returns the number of lap
    curves. Already cut lines are left alone."""
    from . import gameplay
    cols = gameplay.collections(scene)
    lines = [o for o in scene.objects if o.c2.kind == 'PATH' and o.type == 'CURVE' and not o.get('c2_collect_line')
             and _line_letter(o.get('c2_path_name', o.name))]
    if not lines:
        return sum(len(v) for v in line_pieces(scene).values())
    col = _lines_collection(scene)
    taken = {}
    made = []
    for src in sorted(lines, key=lambda o: (':' not in o.get('c2_path_name', o.name), o.name)):
        name = src.get('c2_path_name', src.name)
        L = _line_letter(name)
        pts, _cyc = gameplay._curve_points(src)
        pts = gameplay.game_points(scene, src, pts)
        lap_gates = [(p1, p2) for _o, f, p1, p2 in _gate_records(scene, L) if f.get('LapStartGate')]
        bounds = _cl().lap_bounds(pts, lap_gates)
        pieces = _cl().split(pts, bounds)
        dup = taken.get(L, 0)
        taken[L] = dup + 1
        suffix = '' if not dup else ' (%s)' % name.split(':')[-1].split('_CollectLine')[0]
        bonly = bool(src.get('c2_bspline_only'))
        for k, piece in enumerate(pieces):
            label = ('CollectLine%sLap%d%s' % (L, k + 1, suffix) if len(pieces) > 1
                     else 'CollectLine%s (all laps)%s' % (L, suffix))
            o = gameplay.make_path_object(scene, cols, label, piece, False, bonly)
            for c in list(o.users_collection):
                c.objects.unlink(o)
            col.objects.link(o)
            o['c2_path_name'] = name
            o['c2_collect_line'] = name
            o['c2_collect_lap'] = k + 1
            o['c2_collect_pieces'] = len(pieces)
            o['c2_spawn_routes'] = ['%s|Collect' % L]
            o['c2_collect_laps'] = [k + 1] if len(pieces) > 1 else [1, 2, 3]
            o['c2_hash'] = gameplay.path_hash(o)
            o.color = LAP_COLOURS[k % len(LAP_COLOURS)]
            made.append(o)
        common.remove_ids([src])
    return len(made)


# green, orange, yellow: none of the colours the AI track and race paths are drawn in
LAP_COLOURS = ((0.15, 0.95, 0.3, 1.0), (1.0, 0.5, 0.05, 1.0), (1.0, 0.92, 0.1, 1.0))


def line_for(scene, letter):
    """The pieces of the collect line the game uses for track `letter` (the
    namespaced <ns>:<world>_collect_CollectLineTrackX when there are several)."""
    best = None
    for name, ps in line_pieces(scene).items():
        if _line_letter(name) != letter:
            continue
        if best is None or (':' in name and ':' not in best[0]):
            best = (name, ps)
    return best[1] if best else []


# ----------------------------------------------------------------------
# the lap lines drawn over the viewport: colour per lap, labelled, a little apart
# ----------------------------------------------------------------------
# The three laps of a collect line mostly run along the same road, so their curves
# lie on top of each other and looked like one unlabelled line. The overlay draws
# each lap in its colour, lifted 0.4 m per lap, with its name along it, in every
# shading mode (the Cars 2 Game renderer draws no curves).

_line_draw = {}                 # piece name -> (batch, label points)
_line_list = {'key': None, 'names': []}
_handles = []
LIFT = 0.6                      # every lap above the road (on it, the road's depth hid the line)
LIFT_STEP = 0.5                 # and the laps apart


_gen = [0]                      # bumped by undo / redo / file load: object references go stale


def _overlay_lists(scene):
    """(collect lap curves, other .dbl paths, rings to label) as object lists, made once
    per object count (and again after undo). Looking them up by name every redraw was
    the slowest part of drawing: scene.objects.get scans every object (~1.3 ms each on
    italy's 9800, ~56 per redraw)."""
    key = (scene.name, len(scene.objects), _gen[0])
    if _line_list['key'] != key:
        pieces, paths, rings = [], [], []
        for o in scene.objects:
            if o.get('c2_collect_line'):
                pieces.append(o)
            elif o.type == 'CURVE' and o.c2.kind == 'PATH':
                paths.append(o)
            elif o.get('c2_ring_key') and o.get('c2_ring_key', '').split('|')[3] == '0':
                rings.append(o)     # one label per pair
        _line_list.update(key=key, pieces=pieces, paths=paths, rings=rings, names=[o.name for o in pieces])
    return _line_list['pieces'], _line_list['paths'], _line_list['rings']


def _piece_names(scene):
    _overlay_lists(scene)
    return _line_list['names']


def _alive(objs):
    """The objects still valid and shown (a reference freed by undo drops the cache)."""
    out = []
    for o in objs:
        try:
            if o.visible_get():
                out.append(o)
        except ReferenceError:
            _line_list['key'] = None
            return []
    return out


@bpy.app.handlers.persistent
def _stale(*_args):
    _gen[0] += 1
    _line_list['key'] = None
    _posts['key'] = None


def _piece_draw(scene, o):
    hit = _line_draw.get(o.name)
    if hit is not None:
        return hit
    import gpu
    from gpu_extras.batch import batch_for_shader
    if not o.data.splines:
        return None
    mw = o.matrix_world
    up = (scene.c2.root.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))).normalized() if scene.c2.root else Vector((0, 0, 1))
    lift = up * (LIFT + LIFT_STEP * (o.get('c2_collect_lap', 1) - 1)) if o.get('c2_collect_line') else up * PATH_LIFT
    pts = [tuple(mw @ Vector(p.co[:3]) + lift) for p in o.data.splines[0].points]
    if len(pts) < 2:
        return None
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(sh, 'LINE_STRIP', {'pos': pts})
    # labels a little way after the lap gate, each lap further along (the laps start at the same place)
    lap = o.get('c2_collect_lap', 1)
    labels = [pts[min(len(pts) - 1, int(len(pts) * f))] for f in ((0.04 + 0.06 * (lap - 1), 0.5)
                                                                    if o.get('c2_collect_line') else (0.5,))]
    hit = _line_draw[o.name] = (batch, labels)
    return hit


def _overlay_on(scene):
    return scene.get('c2_collect_gate_count') and getattr(scene.c2, 'collect_show_lines', True) and scene.c2.root


def _paths_on(scene):
    return scene.c2.root is not None and getattr(scene.c2, 'path_outlines', True)


PATH_COLOUR = (0.55, 0.85, 1.0)     # light blue: not a collect lap (green / orange / yellow), not the AI paths
PATH_LIFT = 0.25


_draw_calls = [0, 0]           # (calls, pieces drawn) - for tests


def _draw_lines():
    _draw_calls[0] += 1
    scene = bpy.context.scene
    laps, paths = _overlay_on(scene), _paths_on(scene)
    if not (laps or paths):
        return
    pieces, others, _rings = _overlay_lists(scene)
    import gpu
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    vp = gpu.state.viewport_get()
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    sh.bind()
    sh.uniform_float('viewportSize', (vp[2], vp[3]))
    shown = []
    for o in _alive(pieces) if laps else ():
        d = _piece_draw(scene, o)
        if d is not None:
            shown.append((LAP_COLOURS[(o.get('c2_collect_lap', 1) - 1) % len(LAP_COLOURS)], d[0], 4.0))
    _draw_calls[1] = len(shown)
    for o in _alive(others) if paths else ():
        d = _piece_draw(scene, o)
        if d is not None:
            shown.append((PATH_COLOUR, d[0], 3.0))
    # faint through walls, then solid where nothing is in front
    for depth, faint in (('NONE', True), ('LESS_EQUAL', False)):
        gpu.state.depth_test_set(depth)
        for c, batch, width in shown:
            sh.uniform_float('lineWidth', 2.0 if faint else width)
            sh.uniform_float('color', (c[0], c[1], c[2], 0.35 if faint else 1.0))
            batch.draw(sh)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def _draw_labels():
    scene = bpy.context.scene
    laps, paths = _overlay_on(scene), _paths_on(scene)
    region, rv3d = bpy.context.region, bpy.context.region_data
    if not (laps or paths) or rv3d is None:
        return
    import blf
    pieces, others, rings = _overlay_lists(scene)
    blf.size(0, 12)
    for o in _alive(others) if paths else ():
        d = _piece_draw(scene, o)
        if d is not None:
            _label(region, rv3d, d[1], o.get('c2_path_name', o.name), PATH_COLOUR)
    if not laps:
        return
    blf.size(0, 13)
    for o in _alive(pieces):
        d = _piece_draw(scene, o)
        if d is not None:
            _label(region, rv3d, d[1], base_name(o), LAP_COLOURS[(o.get('c2_collect_lap', 1) - 1) % len(LAP_COLOURS)])
    # the rings: which gate and lap each is (several laps' gates often share one pair of posts)
    for r in _alive(rings):
        lap = r.get('c2_collect_lap', 1)
        _label(region, rv3d, [tuple(r.matrix_world.translation)], '%s  lap %d' % (r.get('c2_collect_target', ''), lap),
               LAP_COLOURS[(lap - 1) % len(LAP_COLOURS)])


def _label(region, rv3d, points, text, c):
    """Text at 3D points, in colour c with a dark shadow."""
    import blf
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    for p in points:
        xy = location_3d_to_region_2d(region, rv3d, Vector(p))
        if xy is None:
            continue
        blf.color(0, 0.0, 0.0, 0.0, 0.8)
        blf.position(0, xy[0] + 7, xy[1] + 5, 0)
        blf.draw(0, text)
        blf.color(0, c[0], c[1], c[2], 1.0)
        blf.position(0, xy[0] + 6, xy[1] + 6, 0)
        blf.draw(0, text)


def path_outlines_update(c2, context):
    show_lines_update(c2, context)


def show_lines_update(c2, context):
    for area in context.screen.areas if context.screen else ():
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def anim_update(c2, context):
    from . import gamerender
    set_animation(context.scene, c2.collect_gate_anim)
    gamerender.RENDERER.rebuild(soft=True)


# ----------------------------------------------------------------------
# rings (Objects\Survival_GateTarget) where the line crosses a gate
# ----------------------------------------------------------------------

def _ring_parts(scene):
    lib = bpy.data.collections.get(scene.get('c2_collect_ring_lib', 'OBJ_survival_gatetarget'))
    return [lo for lo in (lib.objects if lib is not None else ()) if lo.type == 'MESH']


def refresh_targets(scene, depsgraph=None):
    """Put the rings of every track where its line crosses its gates (existing ring
    objects are moved, not remade: one being dragged keeps working). Returns how many."""
    parts = _ring_parts(scene)
    rings = [o for o in scene.objects if o.get('c2_collect_target')]
    existing = {o['c2_ring_key']: o for o in rings if o.get('c2_ring_key') and o.type == 'MESH'}
    stale = [o for o in rings if not (o.get('c2_ring_key') and o.type == 'MESH')]     # 0.29.9's instancers
    if not parts or scene.c2.root is None:
        common.remove_ids(rings, data=False)
        return 0
    cl = _cl()
    col = _gates_collection(scene)
    rmw = scene.c2.root.matrix_world
    made = 0
    found = {}                      # gate name -> laps it got rings on
    for L in 'ABC':
        ps = line_for(scene, L)
        recs = _gate_records(scene, L) if ps else []
        if not recs:
            continue
        pts = line_points(scene, ps)
        gd = [{'p1': p1, 'p2': p2, 'start': bool(f.get('LapStartGate')), 'alternate': bool(f.get('AlternateRouteGate'))}
              for _o, f, p1, p2 in recs]
        for gi, lap, x, dv in cl.crossings(pts, gd):
            o, f, p1, p2 = recs[gi]
            if not f.get('Lap%dGate' % (lap + 1)):
                continue
            found.setdefault(o.name, set()).add(lap + 1)
            # the ring is a disc in its local XY plane: turned so it faces across the gate, the way a
            # car drives through it (the game's heading from the post direction, read as the disc's side)
            h = cl.target_heading(p1, p2)
            R = Matrix.Rotation(h + math.pi / 2.0, 4, 'Y')
            S = Matrix.Diagonal(Vector(cl.TARGET_SCALE + (1.0,)))
            gy = ground(scene, depsgraph, x)
            # rings follow the line (the game puts them where the line crosses the gate): they hang
            # under their gate so they move with it at once, and can't be picked or moved by hand.
            # The game's second ring, 0.2 m along the line, hangs under the first.
            first = None
            for k, sgn in enumerate((1.0, -1.0)):
                p = x + dv * cl.TARGET_OFFSET * sgn
                y = (gy if gy is not None else p[1] - cl.TARGET_HEIGHT) + cl.TARGET_HEIGHT
                for pi, lo in enumerate(parts):
                    key = '%s|%s|%d|%d|%d' % (o.name, L, lap + 1, k, pi)
                    t = existing.pop(key, None)
                    fresh = t is None
                    if fresh:
                        t = bpy.data.objects.new('%s ring L%d' % (base_name(o), lap + 1), lo.data)
                        col.objects.link(t)
                        t['c2_ring_key'] = key
                        t['c2_collect_target'] = o.name
                        t['c2_excluded'] = True
                        t['c2_spawn_routes'] = ['%s|Collect' % L]
                        t['c2_collect_laps'] = [lap + 1]
                        t['c2_collect_track'] = L
                        t['c2_collect_lap'] = lap + 1
                        t.rotation_mode = 'QUATERNION'
                    grab = first is None
                    t.hide_select = True
                    t.lock_location = (True, True, True)
                    t.lock_rotation = (True, True, True)
                    t.lock_rotation_w = True
                    t.lock_scale = (True, True, True)
                    parent = o if grab else first
                    if t.parent is not parent:
                        t.parent = parent
                    t.matrix_parent_inverse.identity()
                    t.matrix_world = rmw @ Matrix.Translation(Vector((p[0], y, p[2]))) @ R @ S @ lo.matrix_basis
                    t['c2_placed'] = _flat(t.matrix_basis)
                    t['c2_cross'] = [float(v) for v in x]
                    if fresh:
                        show_like_pickers(scene, t)
                    if grab:
                        first = t
                    made += 1
    common.remove_ids(stale + list(existing.values()), data=False)
    for o in scene.objects:
        if o.get('c2_collect_gate') is not None:
            o['c2_ring_laps'] = sorted(found.get(o.name, ()))
    scene['c2_collect_target_count'] = made
    return made


def missing_rings(scene, o):
    """Laps a gate is on but gets no ring on: the collect line doesn't pass it near enough
    to its middle for the game to see it (every 10 m, within 0.6 x the post spacing)."""
    f = flags_of(scene, o)
    if f.get('LapStartGate') or f.get('AlternateRouteGate') or not any(f.get(k) for k in TRACK_FLAGS):
        return []
    have = set(o.get('c2_ring_laps', ()))
    return [k for k in (1, 2, 3) if f.get('Lap%dGate' % k) and k not in have]


def _hidden_now(scene, routes, laps):
    """(hidden by the Track / Mode picker, hidden by the Lap picker) of tagged objects."""
    c2 = scene.c2
    v, m, lap = c2.track_variant, c2.track_mode, getattr(c2, 'collect_lap', 'ALL')
    by_v = v != 'ALL' and routes is not None and ('%s|%s' % (v, m)) not in list(routes)
    by_l = lap != 'ALL' and laps is not None and int(lap) not in list(laps)
    return by_v, by_l


def show_like_pickers(scene, o):
    """Give one gate (with its parts), ring or lap line the visibility the Track / Mode /
    Lap pickers give it now - without a whole Track / Mode pass (seconds on big worlds)."""
    objs = [o] + [c for c in o.children if c.get('c2_collect_gate_part')]
    for x in objs:
        for key in (LAP_SAVED, 'c2_variant_saved'):
            sv = x.get(key)
            if sv is not None:
                x.hide_viewport, x.hide_render = bool(sv[0]), bool(sv[1])
                del x[key]
    by_v, by_l = _hidden_now(scene, o.get('c2_spawn_routes'), o.get('c2_collect_laps'))
    if not (by_v or by_l):
        return
    key = 'c2_variant_saved' if by_v else LAP_SAVED
    for x in objs:
        x[key] = [int(x.hide_viewport), int(x.hide_render)]
        x.hide_viewport = True
        x.hide_render = True


# ----------------------------------------------------------------------
# the Lap picker (Track / Mode / Lap)
# ----------------------------------------------------------------------

LAP_SAVED = 'c2_lap_saved'


def restore_lap(scene):
    """Undo apply_lap: every object it hid back the way it was."""
    for o in scene.objects:
        saved = o.get(LAP_SAVED)
        if saved is not None:
            o.hide_viewport, o.hide_render = bool(saved[0]), bool(saved[1])
            del o[LAP_SAVED]


def apply_lap(scene, lap):
    """Hide the collect gates, rings and line laps not on lap `lap` ('ALL' = no
    filter). Kept apart from the Track / Mode picker's hiding, so switching laps
    touches only Collect objects (a full Track / Mode pass is seconds on italy)."""
    restore_lap(scene)
    if lap in (None, 'ALL'):
        return 0
    k = int(lap)
    n = 0
    for o in scene.objects:
        laps = o.get('c2_collect_laps')
        if laps is None or k in list(laps):
            continue
        o[LAP_SAVED] = [int(o.hide_viewport), int(o.hide_render)]
        o.hide_viewport = True
        o.hide_render = True
        n += 1
    return n


def lap_update(c2, context):
    """The Lap picker changed: the Track / Mode / Lap pass (only what changes is touched)."""
    from . import gamerender, trackvariant
    trackvariant.apply(context.scene, c2.track_variant, c2.track_mode)
    gamerender.RENDERER.rebuild(soft=True)


# ----------------------------------------------------------------------
# keeping up with edits
# ----------------------------------------------------------------------

_posts = {'key': None, 'names': {}}


def _post_names(scene):
    """{object name: [gates it moves]}: the posts and the gates themselves."""
    key = (scene.name, len(scene.objects))
    if _posts['key'] != key:
        names = {}
        for o in scene.objects:
            gt = gate_data(o)
            if gt is None:
                continue
            names.setdefault(o.name, set()).add(o.name)
            for p in gt['posts']:
                po = _post(scene, p)
                if po is not None:
                    names.setdefault(po.name, set()).add(o.name)
        _posts.update(key=key, names=names)
    return _posts['names']


_dirty = {'targets': False}


def _refresh_later():
    """Timer: move the rings once edits settle (not on every drag step)."""
    if not _dirty['targets']:
        return None
    _dirty['targets'] = False
    refresh_rings(bpy.context.scene)
    return None


_last_error = [None]            # why the rings last failed to refresh (shown in the Collect Gates panel)


def refresh_rings(scene):
    """Put the rings where the lines cross the gates now; any error is kept for the panel
    (and printed in full to the System Console). True when it worked."""
    for attempt in (0, 1):
        try:
            for o in _overlay_lists(scene)[0]:
                if o.mode == 'EDIT':
                    o.update_from_editmode()     # a lap being edited, read as it is now
            # (the rings moved are redrawn through their own updates: no full renderer rebuild,
            # ~0.7 s on italy after every gate or line edit)
            refresh_targets(scene, bpy.context.evaluated_depsgraph_get())
            _last_error[0] = None
            return True
        except ReferenceError:
            _line_list['key'] = None         # an object list from before an undo: made again, then retried
            _posts['key'] = None
            if attempt:
                _last_error[0] = 'an object was removed while the rings were placed (try again)'
        except Exception as e:      # never break the editor over its rings
            import traceback
            _last_error[0] = '%s: %s' % (type(e).__name__, e)
            print('Cars 2 Scene: collect rings not refreshed:')
            traceback.print_exc()
            break
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return False


def _mark_targets():
    """Have the rings refreshed once the edit settles. The timer is checked every time, not
    only when nothing was pending: a file load (or revert) drops Blender's timers, and a
    refresh pending then left the flag set with no timer to clear it - after that no edit
    ever moved the rings again (only Refresh Rings did)."""
    _dirty['targets'] = True
    if not bpy.app.timers.is_registered(_refresh_later):
        bpy.app.timers.register(_refresh_later, first_interval=0.25, persistent=True)


_busy = [False]


@bpy.app.handlers.persistent
def _depsgraph_update(scene, depsgraph=None):
    """The live update (below): an error is kept for the Collect Gates panel and printed
    once, and never stops the rings from being refreshed later."""
    try:
        _depsgraph_update_body(scene, depsgraph)
    except Exception as e:
        _busy[0] = False
        key = '%s: %s' % (type(e).__name__, e)
        _last_error[0] = key
        if key not in _reported:
            _reported.add(key)
            import traceback
            print('Cars 2 Scene: collect gates update:')
            traceback.print_exc()
        _line_list['key'] = None
        _posts['key'] = None
        _mark_targets()


def _depsgraph_update_body(scene, depsgraph=None):
    """Keep gates, posts, rings and the line together while editing:
    * a post moved: its gate is placed between the posts again;
    * the gate itself moved: its posts follow it;
    * the collect line or a gate changed: the rings go where the line now crosses the
      gates (once the edit settles). The line's points are edited in Edit Mode, where the
      update names the curve data, not the object: those count too.
    Only looks at what the update touched: timeline playback updates the scene each
    frame, and re-placing every gate then walked all 5400 objects of tokyo_b."""
    if _line_draw:
        _drop_outlines(scene, depsgraph)
    if _busy[0] or not scene.get('c2_collect_gate_count'):
        return
    names = _post_names(scene)
    if depsgraph is None:
        place(scene)
        return
    moved = set()
    gates_moved = []
    line = False
    curves = None
    for u in depsgraph.updates:
        idb = u.id
        if isinstance(idb, bpy.types.Curve):
            if curves is None:
                curves = _line_curves(scene)
            piece = curves.get(idb.original.name)
            if piece is not None:
                line = True
                _line_draw.pop(piece, None)
            continue
        if not isinstance(idb, bpy.types.Object):
            continue
        orig = idb.original
        if u.is_updated_transform and orig.name in names:
            moved.add(orig.name)
            if orig.get('c2_collect_gate') is not None:
                gates_moved.append(orig)
        if orig.get('c2_collect_line') and (u.is_updated_geometry or u.is_updated_transform):
            line = True
            _line_draw.pop(orig.name, None)
    if not (moved or line):
        return
    _busy[0] = True
    try:
        hit = set()
        for o in gates_moved:
            gt = gate_data(o)
            posts_too = any(p in moved or (_post(scene, p) is not None and _post(scene, p).name in moved)
                            for p in gt['posts'])
            if not posts_too and _user_moved(o):
                move_posts_with_gate(scene, o)
        for n in moved:
            hit |= names.get(n, set())
        if hit and place(scene, depsgraph, hit):
            line = True
    finally:
        _busy[0] = False
    if line:
        _mark_targets()


def _drop_outlines(scene, depsgraph):
    """A path curve (or its object) changed: its outline is drawn again from it."""
    if depsgraph is None:
        _line_draw.clear()
        return
    owners = None
    for u in depsgraph.updates:
        idb = u.id
        if isinstance(idb, bpy.types.Curve):
            if owners is None:
                owners = _curve_owners(scene)
            n = owners.get(idb.original.name)
            if n is not None:
                _line_draw.pop(n, None)
        elif isinstance(idb, bpy.types.Object) and idb.original.name in _line_draw and \
                (u.is_updated_geometry or u.is_updated_transform):
            _line_draw.pop(idb.original.name, None)


def _curve_owners(scene, laps_only=False):
    """{curve data name: object name} of the collect line laps (and the other .dbl paths)."""
    pieces, others, _rings = _overlay_lists(scene)
    out = {}
    for o in list(pieces) + ([] if laps_only else list(others)):
        try:
            if o.data is not None:
                out[o.data.name] = o.name
        except ReferenceError:
            _line_list['key'] = None
    return out


def _line_curves(scene):
    """{curve data name: lap curve object name} of the collect line laps."""
    return _curve_owners(scene, laps_only=True)


def count(scene):
    return sum(1 for o in scene.objects if o.get('c2_collect_gate'))


# ----------------------------------------------------------------------
# gate order and labels
# ----------------------------------------------------------------------

def lap_lists(scene, letter):
    """{lap 1..3: [(gate object, lap progress)] in the game's order (progress
    along the race spline Track<letter>)}, the lap gate first."""
    from . import gameplay
    race = gameplay._lap_polyline(scene, 'ABC'.index(letter))
    cl = _cl()
    out = {1: [], 2: [], 3: []}
    for o, f, p1, p2 in _gate_records(scene, letter):
        mid = [(a + b) / 2 for a, b in zip(p1, p2)]
        prog = -1.0 if f.get('LapStartGate') else (cl.lap_progress(race, mid) if race else 0.0)
        for k in (1, 2, 3):
            if f.get('Lap%dGate' % k) or f.get('LapStartGate'):
                out[k].append((o, prog))
    for k in out:
        out[k].sort(key=lambda x: x[1])
    return out


def off_track(scene):
    """Gates on no track (taken off): kept in the file, skipped by the game."""
    return [o for o in scene.objects if o.get('c2_collect_gate') is not None
            and not any(flags_of(scene, o).get(k) for k in TRACK_FLAGS)]


def renumber(scene, letter):
    """Label every gate of a track 'A 1:1', 'A 1:2' ... in the game's order (the lap
    gate 'A Lap Gate'). Returns how many were renamed."""
    n = 0
    names = {}
    for lap, lst in lap_lists(scene, letter).items():
        k = 0
        for o, _prog in lst:
            if o.name in names:
                continue
            f = flags_of(scene, o)
            if f.get('LapStartGate'):
                new = '%s Lap Gate' % letter
            else:
                laps = [x for x in (1, 2, 3) if f.get('Lap%dGate' % x)]
                if laps and laps[0] != lap:
                    continue        # labelled on its first lap
                k += 1
                new = '%s %d:%d' % (letter, lap, k)
            names[o.name] = new
    # two passes: free every name first, so swapping labels doesn't collide
    objs = [(bpy.data.objects[a], b) for a, b in names.items() if base_name(bpy.data.objects[a]) != b]
    for o, _new in objs:
        o.name = o.name + ' (renaming)'
    for o, new in objs:
        o.name = new
        n += 1
    _posts['key'] = None
    _cache.clear()
    return n


# ----------------------------------------------------------------------
# export
# ----------------------------------------------------------------------

def export(scene, g, obj_ts, report):
    """Write gate edits into the world graph: labels, and the new gates
    (CollectGate + its two TransformFetch ports, fed by the post locators).
    Flags of imported gates go out through Gameplay Node Settings."""
    import meridian
    for o in scene.objects:
        gt = gate_data(o)
        if gt is None:
            continue
        name = base_name(o)
        if gt.get('uuid') and not gt.get('new'):
            gi = g.find_uuid(gt['uuid'])
            if gi is None:
                continue
            n = g.nodes[gi]
            old = n.get('AuthoredName', '')
            if name != old:
                n['AuthoredName'] = name
                for field in ('GatePost1', 'GatePost2'):
                    port = n.get(field)
                    if isinstance(port, int) and str(g.nodes[port].get('AuthoredName', '')).startswith(old + ' / '):
                        g.nodes[port]['AuthoredName'] = name + g.nodes[port]['AuthoredName'][len(old):]
                report.info('collect gate "%s" relabelled "%s"' % (old, name))
            continue
        # a new gate
        ts = []
        for p in gt['posts']:
            po = _post(scene, p)
            u = obj_ts.get(po.name) if po is not None else None
            ts.append(g.find_uuid(u) if u else None)
        if None in ts:
            report.warn('collect gate %s: a post is not a Meridian locator - not added' % name)
            continue
        f = flags_of(scene, o)
        authored = g.unique_authored(name)
        gate = {'Type': 'CollectGate', 'Uuid': meridian.new_uuid(), 'GatePost1': ('@', 0), 'GatePost2': ('@', 1)}
        for k in FLAGS:
            gate[k] = int(bool(f.get(k)))
        for k in SETTINGS:
            gate[k] = float(gt.get('settings', {}).get(k, {'GateToFirstPickupDistance': 160.0}.get(k, 30.0)))
        gate['IsRootObject'] = 1
        gate['AuthoredName'] = authored
        nodes = [{'Type': 'TransformFetch', 'Uuid': meridian.new_uuid(), 'AuthoredName': authored + ' / Gate Post 1'},
                 {'Type': 'TransformFetch', 'Uuid': meridian.new_uuid(), 'AuthoredName': authored + ' / Gate Post 2'},
                 gate]
        f1, f2, _gi = g.insert_nodes(nodes)
        ts = [g.find_uuid(obj_ts[_post(scene, p).name]) for p in gt['posts']]
        g.add_link('TransformFetch', ts[0], f1)
        g.add_link('TransformFetch', ts[1], f2)
        report.info('new collect gate "%s" (%s, %s)' % (authored, ', '.join(_derive(dict(gt, flags=f))['tracks']) or
                                                           'no track', ', '.join(_derive(dict(gt, flags=f))['laps'])))


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------

def _selected_gate(context):
    o = context.object
    if o is None:
        return None
    if o.get('c2_collect_target'):
        return context.scene.objects.get(o['c2_collect_target'])
    if o.get('c2_collect_gate'):
        return o
    name = o.get('c2_name', o.name)
    for x in context.scene.objects:
        gt = gate_data(x)
        if gt is not None and (name in gt['posts'] or o.name in gt['posts']):
            return x
    return None


def draw_gate_box(layout, context, o):
    """One gate: label, posts, tracks and laps as toggles."""
    scene = context.scene
    gt = gate_data(o)
    f = flags_of(scene, o)
    box = layout.box()
    row = box.row(align=True)
    row.prop(o, 'name', text='', icon='MOD_WIREFRAME')
    op = row.operator('c2.collect_gate_remove', text='', icon='X')
    op.gate = o.name
    col = box.column(align=True)
    for k, post in enumerate(gt['posts']):
        row = col.row(align=True)
        row.label(text='Post %d: %s' % (k + 1, post))
        op = row.operator('c2.collect_gate_select_post', text='', icon='RESTRICT_SELECT_OFF')
        op.post = post or ''
    row = box.row(align=True)
    row.label(text='Tracks')
    for t, key in zip('ABC', TRACK_FLAGS):
        op = row.operator('c2.collect_gate_flag', text=t, depress=bool(f.get(key)))
        op.gate, op.flag = o.name, key
    row = box.row(align=True)
    row.label(text='Laps')
    for key, label in (('LapStartGate', 'Lap Gate'), ('Lap1Gate', '1'), ('Lap2Gate', '2'), ('Lap3Gate', '3'),
                       ('AlternateRouteGate', 'Alt')):
        op = row.operator('c2.collect_gate_flag', text=label, depress=bool(f.get(key)))
        op.gate, op.flag = o.name, key
    miss = missing_rings(scene, o)
    if miss:
        col = box.column(align=True)
        col.alert = True
        col.label(text='No ring on lap %s: the line misses this gate' % ', '.join(map(str, miss)), icon='ERROR')
        col.label(text='The game checks every 10 m, within 0.6 x the post spacing')
        col.label(text="of the middle: widen the gate or centre it on the line.")
    if gt.get('new'):
        box.label(text='New gate: added to the world logic on export', icon='ADD')
    box.label(text='Move the posts to move the gate; scale post 1 on Y for its height', icon='INFO')


def draw_panel(layout, context):
    """The selected collect gate (or a post / ring of one)."""
    o = _selected_gate(context)
    if o is not None:
        draw_gate_box(layout, context, o)


def draw_gates_panel(layout, context):
    """Gameplay > Collect Gates: every gate of the picked track, lap by lap, in the game's order."""
    scene = context.scene
    sc = scene.c2
    if not scene.get('c2_collect_gate_count') and not count(scene):
        layout.label(text='No Collect gates in this world')
        layout.operator('c2.collect_gates_rebuild', icon='FILE_REFRESH')
        return
    row = layout.row(align=True)
    row.operator('c2.collect_rings_refresh', icon='FILE_REFRESH')
    row.operator('c2.collect_gates_rebuild', text='Rebuild Gates', icon='FILE_REFRESH')
    if _last_error[0]:
        col = layout.column(align=True)
        col.alert = True
        col.label(text='Rings not refreshed: %s' % _last_error[0][:110], icon='ERROR')
        col.label(text='(full error in Window > Toggle System Console)')
    row = layout.row(align=True)
    row.prop(sc, 'collect_lap', expand=True)
    row = layout.row(align=True)
    row.prop(sc, 'collect_show_lines', toggle=True, icon='CURVE_PATH')
    row.prop(sc, 'collect_gate_anim', toggle=True, icon='PLAY')
    try:
        _draw_tracks(layout, scene, sc)
    except Exception as e:      # never lose the panel (and its Rebuild button) to one bad gate
        _panel_error(layout, e)


def _panel_error(layout, e):
    import traceback
    key = '%s: %s' % (type(e).__name__, e)
    if key not in _reported:
        _reported.add(key)
        print('Cars 2 Scene: Collect Gates panel:')
        traceback.print_exc()
    col = layout.column(align=True)
    col.alert = True
    col.label(text=key[:120], icon='ERROR')
    col.label(text='(details in Window > Toggle System Console; Rebuild Collect Gates may fix it)')


_reported = set()


def _draw_tracks(layout, scene, sc):
    letters = [sc.track_variant] if sc.track_variant in ('A', 'B', 'C') else \
        sorted({t for o in scene.objects if o.get('c2_collect_gate') for t in gate_data(o)['tracks']})
    lap = sc.collect_lap
    for L in letters:
        box = layout.box()
        head = box.row(align=True)
        head.label(text='Track %s' % L, icon='MOD_CURVE')
        op = head.operator('c2.collect_gate_add', text='Add Gate', icon='ADD')
        op.track = L
        op = head.operator('c2.collect_gates_renumber', text='', icon='SORTALPHA')
        op.track = L
        lists = lap_lists(scene, L)
        for k in (1, 2, 3):
            if lap != 'ALL' and int(lap) != k:
                continue
            col = box.column(align=True)
            col.label(text='Lap %d' % k)
            n = 0
            for o, _prog in lists[k]:
                r = col.row(align=True)
                try:
                    f = flags_of(scene, o)
                    if f.get('LapStartGate'):
                        r.label(text='', icon='MARKER_HLT')
                    else:
                        n += 1
                        r.label(text='#%d' % n)
                    if k in missing_rings(scene, o):
                        r.alert = True
                    r.prop(o, 'name', text='')
                    op = r.operator('c2.collect_gate_select', text='', icon='RESTRICT_SELECT_OFF')
                    op.gate = o.name
                    op = r.operator('c2.collect_gate_remove', text='', icon='X')
                    op.gate = o.name
                except Exception as e:
                    _panel_error(r, e)
            pieces = [p for p in line_for(scene, L) if p.get('c2_collect_lap') == k]
            if pieces:
                r = col.row(align=True)
                r.label(text='Line: %s' % pieces[0].name, icon='CURVE_PATH')
                op = r.operator('c2.collect_gate_select', text='', icon='RESTRICT_SELECT_OFF')
                op.gate = pieces[0].name
    off = off_track(scene)
    if off:
        box = layout.box()
        box.label(text='Off every track (skipped by the game): %d' % len(off), icon='HIDE_ON')
        for o in off[:12]:
            r = box.row(align=True)
            r.label(text=base_name(o))
            for t, key in zip('ABC', TRACK_FLAGS):
                op = r.operator('c2.collect_gate_flag', text='+' + t)
                op.gate, op.flag = o.name, key
    col = layout.column(align=True)
    col.label(text='Order = progress along the race line (as the game counts);', icon='INFO')
    col.label(text='names are labels. Renumber relabels them in that order.')
    col.label(text='Drag a gate or its posts to move it. Rings follow the', icon='INFO')
    col.label(text='collect line: edit the line to move them.')


# ----------------------------------------------------------------------
# operators
# ----------------------------------------------------------------------

def _sync_labels(scene):
    """Gameplay Node Settings lists gates by label: keep it in step with the gate names."""
    for o in scene.objects:
        gt = gate_data(o)
        it = _settings_item(scene, gt.get('uuid')) if gt is not None else None
        if it is not None and it.name != base_name(o):
            it.name = base_name(o)


def _after_edit(context, gate=None):
    from . import gamerender
    scene = context.scene
    _sync_labels(scene)
    _posts['key'] = None
    _cache.clear()
    place(scene, context.evaluated_depsgraph_get())
    if gate is not None and gate.name in scene.objects:
        show_like_pickers(scene, gate)
    refresh_targets(scene, context.evaluated_depsgraph_get())
    gamerender.RENDERER.rebuild(soft=True)


class C2_OT_collect_gate_select_post(bpy.types.Operator):
    """Select this gate post (a locator): move it to move and resize the gate"""
    bl_idname = 'c2.collect_gate_select_post'
    bl_label = 'Select Gate Post'
    bl_options = {'REGISTER', 'UNDO'}
    post: bpy.props.StringProperty()

    def execute(self, context):
        p = _post(context.scene, self.post)
        if p is None:
            self.report({'ERROR'}, 'gate post %s not found' % self.post)
            return {'CANCELLED'}
        _select(context, p)
        return {'FINISHED'}


def _select(context, p):
    if p.hide_viewport or p.hide_get():
        p.hide_viewport = False
        p.hide_set(False)
    for s in context.selected_objects:
        s.select_set(False)
    p.select_set(True)
    context.view_layer.objects.active = p


class C2_OT_collect_gate_select(bpy.types.Operator):
    """Select this gate (or lap line)"""
    bl_idname = 'c2.collect_gate_select'
    bl_label = 'Select'
    bl_options = {'REGISTER', 'UNDO'}
    gate: bpy.props.StringProperty()

    def execute(self, context):
        o = context.scene.objects.get(self.gate)
        if o is None:
            return {'CANCELLED'}
        _select(context, o)
        return {'FINISHED'}


class C2_OT_collect_gate_flag(bpy.types.Operator):
    """Turn this track / lap on or off for the gate (the CollectGate node's flag, written on export)"""
    bl_idname = 'c2.collect_gate_flag'
    bl_label = 'Gate Flag'
    bl_options = {'REGISTER', 'UNDO'}
    gate: bpy.props.StringProperty()
    flag: bpy.props.StringProperty()

    def execute(self, context):
        o = context.scene.objects.get(self.gate)
        if o is None or self.flag not in FLAGS:
            return {'CANCELLED'}
        f = flags_of(context.scene, o)
        set_flags(context.scene, o, dict(f, **{self.flag: int(not f.get(self.flag))}))
        _after_edit(context, o)
        return {'FINISHED'}


class C2_OT_collect_gate_remove(bpy.types.Operator):
    """Take this gate out of the race: a new gate is deleted; a gate of the world is
    taken off the picked track (every track when All is picked) - its node stays,
    on no track, and the game skips it. Bring it back from the Off every track list"""
    bl_idname = 'c2.collect_gate_remove'
    bl_label = 'Remove Gate'
    bl_options = {'REGISTER', 'UNDO'}
    gate: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        o = scene.objects.get(self.gate)
        if o is None:
            return {'CANCELLED'}
        gt = gate_data(o)
        if gt.get('new'):
            used = {p for x in scene.objects if x is not o and x.get('c2_collect_gate')
                    for p in gate_data(x)['posts']}
            posts = [_post(scene, p) for p in gt['posts'] if p not in used]
            name = base_name(o)
            if context.view_layer.objects.active in [o] + posts:
                context.view_layer.objects.active = None
            common.remove_ids([o] + list(o.children) + [p for p in posts if p is not None and p.c2.mer_index < 0],
                              data=False)
            scene['c2_collect_gate_count'] = count(scene)
            self.report({'INFO'}, 'new gate %s removed' % name)
            _after_edit(context)
            return {'FINISHED'}
        else:
            v = scene.c2.track_variant
            keys = ['Track%sGate' % v] if v in ('A', 'B', 'C') else list(TRACK_FLAGS)
            set_flags(scene, o, {k: 0 for k in keys})
            self.report({'INFO'}, '%s taken off track %s (kept in the file, skipped by the game)'
                        % (base_name(o), v if v in ('A', 'B', 'C') else 'A/B/C'))
        _after_edit(context, o)
        return {'FINISHED'}


class C2_OT_collect_gate_add(bpy.types.Operator):
    """Add a gate for this track at the 3D cursor, across the collect line there, on the
    picked lap (lap 1 when All is shown): two new post locators and a CollectGate node,
    written into the world logic on export. A gate taken off every track whose posts no
    other gate uses is reused first"""
    bl_idname = 'c2.collect_gate_add'
    bl_label = 'Add Collect Gate'
    bl_options = {'REGISTER', 'UNDO'}
    track: bpy.props.StringProperty(default='A')

    def execute(self, context):
        from .ops import make_locator
        scene = context.scene
        root = scene.c2.root
        if root is None:
            return {'CANCELLED'}
        L = self.track
        lap = int(scene.c2.collect_lap) if scene.c2.collect_lap != 'ALL' else 1
        cur = _game(scene, Matrix.Translation(scene.cursor.location)).translation
        # across the line: its direction at the point nearest the cursor
        ps = [p for p in line_for(scene, L) if p.get('c2_collect_lap') == lap] or line_for(scene, L)
        P = np.asarray(line_points(scene, ps), float) if ps else None
        d = np.array([1.0, 0.0, 0.0])
        on_line = None
        if P is not None and len(P) > 1:
            k = int(np.argmin(np.linalg.norm(P - np.asarray(cur), axis=1)))
            a, b = P[max(0, k - 1)], P[min(len(P) - 1, k + 1)]
            v = b - a
            v[1] = 0.0
            if np.linalg.norm(v) > 1e-6:
                d = v / np.linalg.norm(v)
            on_line = P[k]          # the gate goes across the line there, centred on it
        across = np.array([d[2], 0.0, -d[0]])
        # size, height and pickup spacing from the track's nearest gate
        recs = [r for r in _gate_records(scene, L) if not r[1].get('LapStartGate')]
        span, height, settings = 30.0, HEIGHT, {}
        if recs:
            near = min(recs, key=lambda r: np.linalg.norm((np.add(r[2], r[3]) / 2.0) - np.asarray(cur)))
            span = float(np.linalg.norm(np.subtract(near[2], near[3]))) or 30.0
            pp = post_positions(scene, near[0])
            height = abs(pp[0].to_scale()[1]) or HEIGHT
            settings = gate_data(near[0]).get('settings', {})
        span = max(span, 20.0)      # narrower, the game's 10 m steps can miss the gate (no ring, no pickups)
        c = np.asarray(on_line if on_line is not None else cur, float)
        c[1] += 2.0
        p1, p2 = c + across * span / 2.0, c - across * span / 2.0
        # reuse a gate that is on no track and owns its posts
        used = {}
        for x in scene.objects:
            gx = gate_data(x)
            if gx is not None:
                for p in gx['posts']:
                    used[p] = used.get(p, 0) + 1
        reuse = next((x for x in off_track(scene) if all(used.get(p, 0) == 1 for p in gate_data(x)['posts'])), None)
        flags = {k: 0 for k in FLAGS}
        flags['Track%sGate' % L] = 1
        flags['Lap%dGate' % lap] = 1
        rmw = root.matrix_world
        name = self._label(scene, L, lap)
        if reuse is not None:
            gt = gate_data(reuse)
            posts = [_post(scene, p) for p in gt['posts']]
            for po, p in zip(posts, (p1, p2)):
                sc_ = po.scale.copy()
                po.matrix_world = rmw @ Matrix.Translation(Vector(p))
                po.scale = sc_
            set_flags(scene, reuse, flags)
            reuse.name = name
            o = reuse
            self.report({'INFO'}, 'gate %s reused for track %s lap %d' % (gt['orig_name'], L, lap))
        else:
            posts = []
            for k, p in enumerate((p1, p2)):
                po = make_locator(context, '%s_Post%d' % (name.replace(' ', '_').replace(':', '_'), k + 1))
                po.matrix_world = rmw @ Matrix.Translation(Vector(p))
                if k == 0:
                    po.scale = (1.0, height, 1.0)          # post 1's Y scale = the gate's height
                posts.append(po)
            gt = {'name': name, 'node': -1, 'uuid': '', 'posts': [po.name for po in posts], 'flags': flags,
                  'settings': settings, 'orig_name': name, 'new': True}
            o = make_gate(scene, _derive(gt))
            self.report({'INFO'}, 'new gate %s at the 3D cursor (track %s, lap %d)' % (name, L, lap))
        scene['c2_collect_gate_count'] = count(scene)
        _after_edit(context, o)
        _select(context, o)
        return {'FINISHED'}

    @staticmethod
    def _label(scene, L, lap):
        names = {base_name(o) for o in scene.objects if o.get('c2_collect_gate')}
        k = 1
        while '%s %d:%d' % (L, lap, k) in names:
            k += 1
        return '%s %d:%d' % (L, lap, k)


class C2_OT_collect_gates_renumber(bpy.types.Operator):
    """Relabel this track's gates by their order in the race ('A 1:1', 'A 1:2' ... per lap,
    'A Lap Gate'): the order the game meets them (progress along the race line). Labels
    only - written as the gates' names on export"""
    bl_idname = 'c2.collect_gates_renumber'
    bl_label = 'Renumber Gates'
    bl_options = {'REGISTER', 'UNDO'}
    track: bpy.props.StringProperty(default='A')

    def execute(self, context):
        n = renumber(context.scene, self.track)
        _sync_labels(context.scene)
        self.report({'INFO'}, '%d gate(s) of track %s relabelled' % (n, self.track))
        return {'FINISHED'}


class C2_OT_collect_gates_rebuild(bpy.types.Operator):
    """Build the Collect gates, rings and per-lap collect lines again from the world logic
    (new gates and label / flag edits made here are kept only in Gameplay Node Settings)"""
    bl_idname = 'c2.collect_gates_rebuild'
    bl_label = 'Rebuild Collect Gates'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .objplace import _graph
        from . import trackvariant
        scene = context.scene
        g = _graph(scene)
        if g is None:
            self.report({'ERROR'}, 'no world logic (.mer) loaded')
            return {'CANCELLED'}
        rep = []
        n = build(context, scene, g, rep)
        trackvariant.apply(scene, scene.c2.track_variant, scene.c2.track_mode)
        self.report({'INFO'}, '; '.join(rep) or '%d gates' % n)
        return {'FINISHED'}


class C2_OT_collect_rings_refresh(bpy.types.Operator):
    """Put every ring where the collect line crosses its gate now (they follow line and gate edits by
    themselves; this does it on demand, without rebuilding the gates)"""
    bl_idname = 'c2.collect_rings_refresh'
    bl_label = 'Refresh Rings'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        scene['c2_collect_gate_count'] = count(scene)       # (a lost count switched the live updates off)
        _dirty['targets'] = False
        _line_list['key'] = None
        _posts['key'] = None
        _cache.clear()
        place(scene, context.evaluated_depsgraph_get())
        if not refresh_rings(scene):
            self.report({'ERROR'}, 'Rings not refreshed: %s' % _last_error[0])
            return {'CANCELLED'}
        self.report({'INFO'}, '%d rings placed' % scene.get('c2_collect_target_count', 0))
        return {'FINISHED'}


@bpy.app.handlers.persistent
def _load_count(*_args):
    """After a file loads: the gate count the live updates check (an older file may lack it)."""
    for scene in bpy.data.scenes:
        if scene.get('c2_collect_gate_count') is None and any(o.get('c2_collect_gate') for o in scene.objects):
            scene['c2_collect_gate_count'] = count(scene)


_classes = (C2_OT_collect_gate_select_post, C2_OT_collect_gate_select, C2_OT_collect_gate_flag,
            C2_OT_collect_gate_remove, C2_OT_collect_gate_add, C2_OT_collect_gates_renumber,
            C2_OT_collect_gates_rebuild, C2_OT_collect_rings_refresh)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if _depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _stale not in h:
            h.append(_stale)
    if _load_count not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_count)
    if not bpy.app.background:
        _handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_lines, (), 'WINDOW', 'POST_VIEW'))
        _handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL'))


def unregister():
    for h in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    _handles.clear()
    _line_draw.clear()
    if _depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _stale in h:
            h.remove(_stale)
    if _load_count in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_count)
    if bpy.app.timers.is_registered(_refresh_later):
        bpy.app.timers.unregister(_refresh_later)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
