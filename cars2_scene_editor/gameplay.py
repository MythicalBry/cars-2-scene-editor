"""Gameplay data in the editor: collection layout, AI track graph, AI markers,
.dbl paths, arena nav meshes, Meridian spawn lists / starting grids and
gameplay node settings. Formats: gameplay_docs/GAMEPLAY_DATA.md.

Everything here hangs under the scene root, so object-local coordinates are
game coordinates. On export each part is rebuilt only when it changed;
untouched data is written back exactly as it was read."""
import hashlib
import json
import math
import os

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

from . import common
from .props import MARKER_TYPES

# ----------------------------------------------------------------------------
# collections
# ----------------------------------------------------------------------------
# (key, collection label, parent key); the first five keep their old names
LAYOUT = (
    ('Geometry', 'Geometry', None),
    ('Sky', 'Sky', None),
    ('Water', 'Water', None),
    ('Helpers', 'Helper Geometry', None),
    ('ColHelpers', 'Collision Helpers', 'Helpers'),
    ('Shadow', 'Shadow Casters', 'Helpers'),
    ('Hidden', 'Hidden Helpers', 'Helpers'),
    ('Lights', 'Lights', None),
    ('Nodes', 'Nodes', None),
    ('Probes', 'Probes', None),
    ('Collision', 'Collision', None),
    ('Meridian', 'Meridian', None),
    ('Spawns', 'Spawn & Restart Points', 'Meridian'),
    ('Grids', 'Starting Grids', 'Meridian'),
    ('Anchors', 'Choreography Anchors', 'Meridian'),
    ('Triggers', 'Triggers', 'Meridian'),
    ('Locators', 'Other Locators', 'Meridian'),
    ('Race', 'AI & Race', None),
    ('Track', 'AI Track', 'Race'),
    ('Markers', 'AI Markers', 'Race'),
    ('Paths', 'Paths (.dbl)', 'Race'),
    ('NavMeshes', 'Nav Meshes', None),
    ('Bridges', 'Nav Mesh Bridges', 'NavMeshes'),
    ('MapObjects', 'Map Objects', None),
)
LABELS = {k: lab for k, lab, _ in LAYOUT}


def col_name(world, key):
    """Collection name of a LAYOUT key for this world, e.g. 'radiator_a AI Track'."""
    return '%s %s' % (world, LABELS.get(key, key))


def collections(scene, create=True):
    """{LAYOUT key: collection} of the world (created when missing, unless
    create=False); 'Main' is the top 'C2 <world>' collection.
    """
    world = scene.c2.world_name
    main = bpy.data.collections.get('C2 ' + world)
    if main is None:
        if not create:
            return {}
        main = common.ensure_collection('C2 ' + world)
    out = {'Main': main}
    for key, _, parent in LAYOUT:
        name = col_name(world, key)
        c = bpy.data.collections.get(name)
        if c is None and create:
            c = common.ensure_collection(name, out[parent] if parent else main)
        if c is not None:
            out[key] = c
    return out


def spawn_collection(scene, list_name, create=True):
    """The sub-collection holding one spawn list's locators."""
    cols = collections(scene, create)
    name = '%s Spawn: %s' % (scene.c2.world_name, list_name)
    c = bpy.data.collections.get(name)
    if c is None and create and 'Spawns' in cols:
        c = common.ensure_collection(name, cols['Spawns'])
    return c


def move_to(obj, col):
    """Put obj in exactly one collection."""
    if col is None:
        return
    for c in list(obj.users_collection):
        if c != col:
            c.objects.unlink(obj)
    if col not in obj.users_collection:
        col.objects.link(obj)


def link_also(obj, col):
    """Also show obj in `col` (an object can be in several lists)."""
    if col is not None and col not in obj.users_collection:
        col.objects.link(obj)


def _root_child(scene, obj):
    """Parent obj to the world root with identity transforms (game space)."""
    root = scene.c2.root
    obj.parent = root
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = Matrix.Identity(4)


def game_points(scene, obj, pts):
    """Object-local points -> game space (objects hang under the root)."""
    root = scene.c2.root
    m = root.matrix_world.inverted() @ obj.matrix_world
    if m == Matrix.Identity(4):
        return [tuple(p) for p in pts]
    return [tuple(m @ Vector(p)) for p in pts]


def _hash(*parts):
    """SHA-1 over bytes / reprs: change detection for track, paths, nav meshes."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else repr(p).encode())
    return h.hexdigest()


def _objs(scene, kind):
    """Scene objects of one c2 kind."""
    return [o for o in scene.objects if o.c2.kind == kind]


# ----------------------------------------------------------------------------
# AI track graph: one mesh, vertices = TrackNodes, edges = TrackLinks
# ----------------------------------------------------------------------------
import track as trackmod   # noqa: E402  (core module)

# node / link values stored as mesh attributes on the track object
# (vertex attributes for nodes, edge attributes for links)
NODE_FLOATS = ('Width', 'FastLineUTrackA', 'EnergyLineUTrackA', 'FastLineUTrackB', 'EnergyLineUTrackB',
               'FastLineUTrackC', 'EnergyLineUTrackC', 'SpeedLimit', 'LookAheadDistance')
NODE_INTS = ('EndOfRaceMarker', 'IgnoreOtherPlayers')
LINK_FLOATS = ('StraightawayLength',) + tuple(k for k in trackmod.LINK_CD_KEYS if 'LineHopV' in k or 'LineTurboV' in k)
LINK_INTS = ('AvoidanceBitfield', 'ForceDrifting', 'DriftLeft', 'BashZone', 'CanRespawn')
SECTION_INTS = tuple(k for k in trackmod.SECTION_CD_KEYS if k not in ('SplitChance', 'SplitMinSpeed'))
ROT_ATTRS = ('c2_across', 'c2_up', 'c2_forward')

# defaults of a new track (identical in every shipping world)
DEFAULT_TRACK = {
    'NodeCustomData': 0, 'LinkCustomData': 0, 'SectionCustomData': 0, 'Nodes': 0, 'Links': 0, 'Sections': 0,
    'SplineMarkup': 0, 'DefaultNodeWidth': 32.0, 'DefaultNodeRacingLineU': 0.5,
    'DefaultLinkStraightawayLength': 0.0, 'DefaultLinkAvoidanceBitfield': -1,
    'DefaultNodeIgnoreOtherPlayers': 0.0, 'DefaultNodeEndOfRaceMarker': 0.0, 'DefaultNodeSpeedLimit': 0.0,
    'DefaultNodeLookAheadDistance': 0.0, 'DefaultLinkForceDrifting': 0.0, 'DefaultLinkDriftLeft': 0.0,
    'DefaultLinkBashZone': 0.0, 'DefaultLinkCanRespawn': 1.0, 'DefaultLinkHopV': 100.0, 'DefaultLinkTurboV': 100.0,
    'DefaultSectionSplitChance': 1.0, 'DefaultSectionSplitMinSpeed': 0.0, 'DefaultSectionOnRacingLine': 0,
    'DefaultSectionEnergyPath': 0, 'DefaultSectionWeaponPath': 0, 'DefaultSectionInTheZoneShortcutPath': 0,
    'DefaultSectionHighPerformanceShortcutPath': 0, 'DefaultSectionOnlyIfOnRacingLine': 0,
    'DefaultSectionClosedForOffTrackCheck': 0, 'DefaultSectionValid': 1, 'DefaultSectionLimitToBestAI': 0,
    'DefaultSectionNeedsTurbo': 0, 'ID_COLLECT_TRACK_A': 0, 'ID_COLLECT_TRACK_B': 1, 'ID_COLLECT_TRACK_C': 2,
    'TYPE_BUNNY_HOP': 0, 'TYPE_SIDESTEP_LEFT': 1, 'TYPE_SIDESTEP_RIGHT': 2, 'TYPE_DRIFT_LEFT': 3,
    'TYPE_DRIFT_LEFT_END': 4, 'TYPE_DRIFT_RIGHT': 5, 'TYPE_DRIFT_RIGHT_END': 6,
}


def _set_attr(me, name, kind, domain, values):
    """Create/overwrite a mesh attribute (INT / FLOAT / FLOAT_VECTOR on POINT /
    EDGE / FACE) from a flat list.
    """
    a = me.attributes.get(name) or me.attributes.new(name, kind, domain)
    arr = np.asarray(values, dtype=np.int32 if kind == 'INT' else np.float32).ravel()
    a.data.foreach_set('vector' if kind == 'FLOAT_VECTOR' else 'value', arr)


def _get_attr(me, name, kind, n, default=0):
    """A mesh attribute as an array (filled with `default` when it doesn't exist)."""
    a = me.attributes.get(name)
    width = 3 if kind == 'FLOAT_VECTOR' else 1
    if a is None:
        return np.full((n, width) if width > 1 else n, default, np.int32 if kind == 'INT' else np.float64)
    arr = np.empty(n * width, np.int32 if kind == 'INT' else np.float32)
    a.data.foreach_get('vector' if kind == 'FLOAT_VECTOR' else 'value', arr)
    return arr.reshape(n, 3) if width > 1 else arr


def track_mesh_hash(obj):
    """Fingerprint of the AI track object (geometry, every attribute, sections,
    extra links, placement): unchanged on export = original pools kept.
    """
    me = obj.data
    nv, ne = len(me.vertices), len(me.edges)
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get('co', co)
    ev = np.empty(ne * 2, np.int32)
    me.edges.foreach_get('vertices', ev)
    parts = [co.tobytes(), ev.tobytes(), obj.get('c2_sections', ''), obj.get('c2_extra_links', '')]
    for a in sorted(me.attributes, key=lambda a: a.name):
        if a.name.startswith('.') or a.name == 'position':
            continue
        n = len(a.data)
        if a.data_type == 'FLOAT_VECTOR':
            arr = np.empty(n * 3, np.float32)
            a.data.foreach_get('vector', arr)
        elif a.data_type in ('FLOAT', 'INT'):
            arr = np.empty(n, np.float32 if a.data_type == 'FLOAT' else np.int32)
            a.data.foreach_get('value', arr)
        else:
            continue
        parts += [a.name.encode(), arr.tobytes()]
    m = obj.matrix_basis
    parts.append(repr([round(v, 6) for row in m for v in row]))
    return _hash(*parts)


def import_track(scene, tree, cols):
    """AI track graph -> one mesh object: vertices = TrackNodes, edges =
    TrackLinks, node/link values as attributes. Links a mesh edge can't hold
    (a second link between the same two nodes) are kept as JSON in
    'c2_extra_links'; section records in 'c2_sections'. Also imports the
    markers and draws the preview.
    """
    if not trackmod.Track.has_track(tree):
        return None
    tr = trackmod.Track(tree)
    N = [tr.node_values(i) for i in range(len(tr.nodes))]
    L = [tr.link_values(i) for i in range(len(tr.links))]
    S = [tr.section_values(i) for i in range(len(tr.sections))]
    sec_of = [-1] * len(L)
    for si, run in tr.section_runs():
        for li in run:
            sec_of[li] = si
    edges, kept, extra = [], [], []
    seen = set()
    for li, l in enumerate(L):
        key = frozenset((l['Node1Index'], l['Node2Index']))
        if key in seen or len(key) < 2:
            extra.append(dict(l, _index=li, _section=sec_of[li]))
            continue
        seen.add(key)
        edges.append((l['Node1Index'], l['Node2Index']))
        kept.append(li)
    me = bpy.data.meshes.new('AI Track')
    me.from_pydata([n['Position'] for n in N], edges, [])
    nv, ne = len(N), len(edges)
    for k in NODE_FLOATS:
        _set_attr(me, k, 'FLOAT', 'POINT', [float(n[k] or 0) for n in N])
    for k in NODE_INTS:
        _set_attr(me, k, 'INT', 'POINT', [int(n[k] or 0) for n in N])
    for r, name in enumerate(ROT_ATTRS):
        _set_attr(me, name, 'FLOAT_VECTOR', 'POINT', [n['Rotation'][3 * r:3 * r + 3] for n in N])
    _set_attr(me, 'c2_pos0', 'FLOAT_VECTOR', 'POINT', [n['Position'] for n in N])
    for k in LINK_FLOATS:
        _set_attr(me, k, 'FLOAT', 'EDGE', [float(L[li][k] or 0) for li in kept])
    for k in LINK_INTS:
        _set_attr(me, k, 'INT', 'EDGE', [int(L[li][k] if L[li][k] is not None else 0) for li in kept])
    _set_attr(me, 'c2_section', 'INT', 'EDGE', [sec_of[li] for li in kept])
    obj = bpy.data.objects.new('AI Track Graph', me)
    cols['Track'].objects.link(obj)
    _root_child(scene, obj)
    obj.c2.kind = 'AI_TRACK'
    obj.show_in_front = True
    obj['c2_sections'] = json.dumps(S)
    obj['c2_extra_links'] = json.dumps(extra) if extra else ''
    obj['c2_hash'] = track_mesh_hash(obj)
    # markers
    for k, m in enumerate(tr.markups):
        import_marker(scene, cols, m, k)
    build_track_preview(scene)
    return obj


def import_marker(scene, cols, m, index=-1):
    """An AI marker (TrackSplineMarkup) as a sphere empty. index -1 = new."""
    t = int(m.get('Type', 0))
    o = bpy.data.objects.new('AI %s %d' % (trackmod.MARKUP_TYPES.get(t, str(t)).title().replace('_', ' '),
                                          index if index >= 0 else 0), None)
    o.empty_display_type = 'SPHERE'
    o.empty_display_size = 1.5
    cols['Markers'].objects.link(o)
    _root_child(scene, o)
    p = [float(x) for x in m['Position']]
    o.matrix_basis = Matrix.Translation(p)
    o.c2.kind = 'AI_MARKER'
    o.c2.marker_type = str(t if 0 <= t <= 7 else 7)
    o.c2.marker_track = int(m.get('ID', 0))
    o.c2.marker_distance = float(m.get('Distance', 0.0))
    o['c2_index'] = index
    o['c2_pos0'] = p
    return o


def _lap_polyline(scene, track_id):
    """Game-space points of the TrackA/B/C path (for marker lap distances)."""
    name = 'Track' + 'ABC'[max(0, min(2, track_id))]
    for o in _objs(scene, 'PATH'):
        if o.get('c2_path_name', o.name) in (name, name + '_Markup') and o.type == 'CURVE' and o.data.splines:
            return game_points(scene, o, [p.co[:3] for p in o.data.splines[0].points])
    return None


def _distance_along(points, p):
    """Arc length to the closest point of polyline `points`."""
    P = np.asarray(points, float)
    q = np.asarray(p, float)
    best, acc, at = 1e30, 0.0, 0.0
    for i in range(len(P) - 1):
        a, b = P[i], P[i + 1]
        d = b - a
        L2 = d @ d
        t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, (q - a) @ d / L2))
        dist = np.linalg.norm(a + d * t - q)
        if dist < best:
            best, at = dist, acc + math.sqrt(L2) * t
        acc += math.sqrt(L2)
    return at


def _markers_changed(scene):
    """Were markers added, removed, moved or edited since import?"""
    ms = _objs(scene, 'AI_MARKER')
    if scene.get('c2_marker_count', len(ms)) != len(ms):
        return True
    for o in ms:
        if o.get('c2_index', -1) < 0:
            return True
        p0 = o.get('c2_pos0')
        if p0 is None or (Vector(p0) - o.matrix_basis.translation).length > 1e-4:
            return True
        if o.get('c2_meta0') != [int(o.c2.marker_type), o.c2.marker_track, round(o.c2.marker_distance, 4)]:
            return True
    return False


def _remember_markers(scene):
    """Store the markers' current state (what _markers_changed compares to)."""
    ms = _objs(scene, 'AI_MARKER')
    scene['c2_marker_count'] = len(ms)
    for o in ms:
        o['c2_meta0'] = [int(o.c2.marker_type), o.c2.marker_track, round(o.c2.marker_distance, 4)]


def track_nodes_links(scene, obj):
    """(nodes, links, sections) flat values from the track mesh, frames
    recomputed for moved / new nodes."""
    if obj.mode == 'EDIT':
        obj.update_from_editmode()
    me = obj.data
    nv, ne = len(me.vertices), len(me.edges)
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get('co', co)
    co = co.reshape(nv, 3).astype(np.float64)
    m = obj.matrix_basis
    if m != Matrix.Identity(4):
        M = np.array(m)
        co = co @ M[:3, :3].T + M[:3, 3]
    ev = np.empty(ne * 2, np.int32)
    me.edges.foreach_get('vertices', ev)
    ev = ev.reshape(ne, 2)
    pos0 = _get_attr(me, 'c2_pos0', 'FLOAT_VECTOR', nv, np.nan)
    rots = [_get_attr(me, k, 'FLOAT_VECTOR', nv, 0.0) for k in ROT_ATTRS]
    nf = {k: _get_attr(me, k, 'FLOAT', nv, 0.0) for k in NODE_FLOATS}
    defaults = DEFAULT_TRACK
    for k in NODE_FLOATS:
        if me.attributes.get(k) is None:
            nf[k][:] = defaults['DefaultNodeWidth'] if k == 'Width' else (
                defaults['DefaultNodeRacingLineU'] if 'LineU' in k else 0.0)
    ni = {k: _get_attr(me, k, 'INT', nv, 0) for k in NODE_INTS}
    lf = {k: _get_attr(me, k, 'FLOAT', ne, 0.0) for k in LINK_FLOATS}
    for k in LINK_FLOATS:
        if me.attributes.get(k) is None and k != 'StraightawayLength':
            lf[k][:] = 100.0
    li_ = {k: _get_attr(me, k, 'INT', ne, 0) for k in LINK_INTS}
    if me.attributes.get('AvoidanceBitfield') is None:
        li_['AvoidanceBitfield'][:] = -1
    if me.attributes.get('CanRespawn') is None:
        li_['CanRespawn'][:] = 1
    sec = _get_attr(me, 'c2_section', 'INT', ne, -1)
    nxt, prv = {}, {}
    for a, b in ev:
        nxt.setdefault(int(a), int(b))
        prv.setdefault(int(b), int(a))
    nodes = []
    # a node keeps its stored orientation unless it moved (or is new): then
    # it is re-aimed along the road from its neighbours
    for i in range(nv):
        p = co[i]
        moved = not np.all(np.isfinite(pos0[i])) or np.abs(pos0[i] - p).max() > 1e-3
        rot = np.concatenate([rots[0][i], rots[1][i], rots[2][i]])
        if moved or np.abs(rot).sum() < 1e-6:
            up = rots[1][i] if np.abs(rots[1][i]).sum() > 1e-6 else (0.0, 1.0, 0.0)
            rot = trackmod.node_frame(co[prv[i]] if i in prv else None, p, co[nxt[i]] if i in nxt else None, up)
        n = {'Position': [float(x) for x in p], 'Rotation': [float(x) for x in rot]}
        for k in NODE_FLOATS:
            n[k] = float(nf[k][i])
        for k in NODE_INTS:
            n[k] = int(ni[k][i])
        nodes.append(n)
    links = []
    for e in range(ne):
        l = {'Node1Index': int(ev[e][0]), 'Node2Index': int(ev[e][1]), '_section': int(sec[e])}
        for k in LINK_FLOATS:
            l[k] = float(lf[k][e])
        for k in LINK_INTS:
            l[k] = int(li_[k][e])
        links.append(l)
    extra = json.loads(obj.get('c2_extra_links') or '[]')
    for x in extra:
        if 0 <= x['Node1Index'] < nv and 0 <= x['Node2Index'] < nv:
            links.append(dict(x))
    sections = json.loads(obj.get('c2_sections') or '[]')
    return nodes, links, sections


def _values_typed(d, keys_int):
    """Custom-data values keep the shipping types (flags int, the rest float)."""
    out = {}
    for k, v in d.items():
        if k.startswith('_'):
            continue
        out[k] = int(v) if k in keys_int else v
    return out


def export_track(scene, tree, report):
    """Write the AI track back into the .oct tree when it changed: flat values
    from the mesh, section records matched to the new link runs (each run
    takes the record most of its links had), markers with recomputed lap
    distances for moved ones.
    """
    objs = _objs(scene, 'AI_TRACK')
    if not objs:
        return
    obj = objs[0]
    if len(objs) > 1:
        report.warn('more than one AI Track Graph object - only %s is exported' % obj.name)
    if obj.get('c2_hash') == track_mesh_hash(obj) and not _markers_changed(scene):
        report.info('AI track unchanged - kept as is')
        return
    if not trackmod.Track.has_track(tree):
        tree['TrackPool'] = {'Track#0': dict(DEFAULT_TRACK)}
        report.info('new AI track created')
    tr = trackmod.Track(tree)
    nodes, links, sec_records = track_nodes_links(scene, obj)
    # section records follow the link runs (the game reassigns them by run);
    # each run keeps the record most of its links had, so settings survive
    # edits that don't change the branching
    runs = trackmod.section_runs_of(links)
    used = set()
    sections = []
    for si, run in runs:
        votes = {}
        for li in run:
            s = links[li].get('_section', -1)
            if 0 <= s < len(sec_records):
                votes[s] = votes.get(s, 0) + 1
        pick = max((s for s in votes if s not in used), key=lambda s: votes[s], default=None)
        if pick is None:
            rec = {'BranchName': 'AI_TrackLink%d' % (run[0] + 1)}
            src = max(votes, key=votes.get) if votes else None
            if src is not None:   # a split run: copy the settings of the run it came from
                rec = dict(sec_records[src], BranchName=rec['BranchName'])
        else:
            used.add(pick)
            rec = dict(sec_records[pick])
        sections.append(_values_typed(rec, SECTION_INTS))
    nodes = [_values_typed(n, NODE_INTS) for n in nodes]
    links = [_values_typed(l, LINK_INTS) for l in links]
    # markers
    markups = []
    ms = sorted(_objs(scene, 'AI_MARKER'), key=lambda o: (o.get('c2_index', -1) < 0, o.get('c2_index', 0), o.name))
    for o in ms:
        p = list(game_points(scene, o, [(0, 0, 0)])[0])
        dist = o.c2.marker_distance
        p0 = o.get('c2_pos0')
        moved = p0 is None or (Vector(p0) - Vector(p)).length > 1e-3
        ci = o.get('c2_index', -1)
        orig = tr.markups[ci] if 0 <= ci < len(tr.markups) else None
        if not moved and orig is not None and orig.get('ID') == o.c2.marker_track and                 orig.get('Type') == int(o.c2.marker_type):
            # unmoved: the file's exact values (Blender's float32 round trip
            # would change the last bit)
            markups.append(dict(orig))
            continue
        if moved:
            lap = _lap_polyline(scene, o.c2.marker_track)
            if lap:
                dist = _distance_along(lap, p)
        markups.append({'ID': o.c2.marker_track, 'Type': int(o.c2.marker_type), 'Position': p, 'Distance': dist})
    tr.rebuild(nodes, links, sections, markups)
    for m in tr.check():
        report.warn('AI track: ' + m)
    report.info('AI track rebuilt: %d nodes, %d links, %d sections, %d markers'
                % (len(tr.nodes), len(tr.links), len(tr.sections), len(tr.markups)))


PREVIEW_COLORS = {'edges': (0.9, 0.9, 0.9), 'fast': (1.0, 0.15, 0.05), 'energy': (0.05, 0.55, 1.0)}

# The track preview (road edges, direction arrows, the chosen track's racing
# lines) is drawn by a viewport handler, not by objects: it follows every edit
# as it happens (Edit Mode included), adds nothing to the scene, and leaves
# the nodes visible under thin, see-through lines. `obj` is a direct reference
# to the AI Track Graph (scene.objects lookups scan every object); `lookup`
# asks the next draw to find it again (import, New AI Track, undo, load).
_track_draw = {'obj': None, 'lookup': True, 'version': 0, 'key': None, 'lines': None, 'handler': None}


def _tag_redraw():
    wm = bpy.context.window_manager
    for win in (wm.windows if wm is not None else ()):
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def build_track_preview(scene):
    """Redraw the track preview from the AI Track Graph. Also removes the curve
    previews older versions made (files saved before 0.27.1)."""
    for o in [o for o in _objs(scene, 'PREVIEW') if o.get('c2_preview') == 'track']:
        data = o.data
        bpy.data.objects.remove(o)
        if data is not None and not data.users:
            (bpy.data.curves if isinstance(data, bpy.types.Curve) else bpy.data.meshes).remove(data)
    _track_draw['lookup'] = True
    _track_draw['version'] += 1
    _tag_redraw()
    return []


def _track_arrays(obj, t):
    """(co, across, up, pos0, width, fast U, energy U, links) of the track
    nodes, from the mesh or, in Edit Mode, from the edit mesh. co is in the
    graph's space (the track object's own transform applied)."""
    me = obj.data
    names = ('Width', 'FastLineUTrack' + t, 'EnergyLineUTrack' + t)
    defaults = (DEFAULT_TRACK['DefaultNodeWidth'], DEFAULT_TRACK['DefaultNodeRacingLineU'],
                DEFAULT_TRACK['DefaultNodeRacingLineU'])
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        vs = bm.verts
        vs.index_update()
        n = len(vs)
        co = np.array([v.co for v in vs], float).reshape(n, 3)
        vec = []
        for k, d in (('c2_across', 0.0), ('c2_up', 0.0), ('c2_pos0', np.nan)):
            lay = vs.layers.float_vector.get(k)
            vec.append(np.array([v[lay] for v in vs], float).reshape(n, 3) if lay is not None
                       else np.full((n, 3), d))
        fl = []
        for k, d in zip(names, defaults):
            lay = vs.layers.float.get(k)
            fl.append(np.array([v[lay] for v in vs], float) if lay is not None else np.full(n, d))
        ev = np.array([(e.verts[0].index, e.verts[1].index) for e in bm.edges], np.int32).reshape(-1, 2)
    else:
        n, ne = len(me.vertices), len(me.edges)
        co = np.empty(n * 3, np.float32)
        me.vertices.foreach_get('co', co)
        co = co.reshape(n, 3).astype(float)
        vec = [_get_attr(me, k, 'FLOAT_VECTOR', n, d).astype(float)
               for k, d in (('c2_across', 0.0), ('c2_up', 0.0), ('c2_pos0', np.nan))]
        fl = [_get_attr(me, k, 'FLOAT', n, d).astype(float) for k, d in zip(names, defaults)]
        ev = np.empty(ne * 2, np.int32)
        me.edges.foreach_get('vertices', ev)
        ev = ev.reshape(ne, 2)
    M = np.array(obj.matrix_basis)
    if not np.allclose(M, np.eye(4)):
        co = co @ M[:3, :3].T + M[:3, 3]
    return co, vec[0], vec[1], vec[2], fl[0], fl[1], fl[2], ev


def _track_lines(obj, t):
    """{'edges' | 'fast' | 'energy': float32 (k, 3) line-segment points}. The
    road edges sit half a width either side of each node along its across
    axis; moved and new nodes are re-aimed along the road as export does."""
    co, across, up, pos0, w, fu, eu, ev = _track_arrays(obj, t)
    nxt, prv = {}, {}
    for a, b in ev.tolist():
        nxt.setdefault(a, b)
        prv.setdefault(b, a)
    moved = ~np.all(np.isfinite(pos0), axis=1)
    moved[~moved] = np.abs(pos0[~moved] - co[~moved]).max(axis=1) > 1e-3
    moved |= np.abs(across).sum(axis=1) < 1e-6
    A = across.copy()
    for i in np.nonzero(moved)[0].tolist():
        hint = up[i] if np.abs(up[i]).sum() > 1e-6 else (0.0, 1.0, 0.0)
        A[i] = trackmod.node_frame(co[prv[i]] if i in prv else None, co[i],
                                   co[nxt[i]] if i in nxt else None, hint)[0:3]
    left = co + A * (w[:, None] * 0.5)
    right = co - A * (w[:, None] * 0.5)
    a, b = ev[:, 0], ev[:, 1]

    def segs(P):
        return np.stack([P[a], P[b]], axis=1).reshape(-1, 3)
    # direction arrows at the link midpoints
    d = co[b] - co[a]
    L = np.linalg.norm(d, axis=1)
    ok = L > 1e-6
    d, L, mid = d[ok] / L[ok, None], L[ok], (co[a][ok] + co[b][ok]) * 0.5
    s = np.cross(d, (0.0, 1.0, 0.0))
    s /= np.maximum(np.linalg.norm(s, axis=1), 1e-9)[:, None]
    k = np.minimum(3.0, L * 0.3)[:, None]
    tail = mid - d * k
    arrows = np.stack([mid, tail + s * k * 0.6, mid, tail - s * k * 0.6], axis=1).reshape(-1, 3)
    return {'edges': np.concatenate([segs(left), segs(right), arrows]).astype(np.float32),
            'fast': segs(left + (right - left) * fu[:, None]).astype(np.float32),
            'energy': segs(left + (right - left) * eu[:, None]).astype(np.float32)}


def _track_object(scene):
    """The AI Track Graph, kept as a direct reference between draws."""
    o = _track_draw['obj']
    if o is not None:
        try:
            o.name
        except ReferenceError:
            o = _track_draw['obj'] = None
            _track_draw['lookup'] = True
    if _track_draw['lookup']:
        _track_draw['lookup'] = False
        objs = _objs(scene, 'AI_TRACK')
        o = _track_draw['obj'] = objs[0] if objs else None
        _track_draw['version'] += 1
    return o


def _draw_track():
    """POST_VIEW: the track preview, over every viewport shading."""
    ctx = bpy.context
    scene, sp, rv3d = ctx.scene, ctx.space_data, ctx.region_data
    if scene is None or rv3d is None or sp is None or not sp.overlay.show_overlays:
        return
    sc = scene.c2
    if not sc.track_preview_show:
        return
    try:
        obj = _track_object(scene)
        if obj is None or not obj.visible_get():
            return
        t = sc.track_preview
        key = (obj.as_pointer(), t, obj.mode, _track_draw['version'])
        if key != _track_draw['key']:
            _track_draw['lines'] = _track_lines(obj, t)
            _track_draw['key'] = key
        lines = _track_draw['lines']
        import gpu
        from gpu_extras.batch import batch_for_shader
        sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        st = gpu.state
        st.blend_set('ALPHA')
        st.depth_test_set('NONE')       # in front of the scene, like the track itself
        sh.bind()
        sh.uniform_float('viewportSize', st.viewport_get()[2:])
        alpha = sc.track_preview_opacity
        M = obj.matrix_world @ obj.matrix_basis.inverted()      # graph space -> world
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(M)
            for name in ('edges', 'energy', 'fast'):
                pts = lines[name]
                if not len(pts):
                    continue
                sh.uniform_float('lineWidth', sc.track_preview_width * (1.25 if name == 'edges' else 1.0))
                sh.uniform_float('color', (*PREVIEW_COLORS[name], alpha))
                batch_for_shader(sh, 'LINES', {'pos': pts}).draw(sh)
        st.blend_set('NONE')
    except Exception as e:
        print('Cars 2 track preview: not drawn (%s)' % e)


@bpy.app.handlers.persistent
def _track_depsgraph(scene, depsgraph):
    """Redraw the preview when the track object or its mesh changed (moved
    nodes, edits, panel values); nothing else is looked at."""
    o = _track_draw['obj']
    if o is None:
        return
    try:
        ptrs = (o.as_pointer(), o.data.as_pointer())
    except ReferenceError:
        _track_draw['obj'] = None
        _track_draw['lookup'] = True
        return
    for u in depsgraph.updates:
        if u.id.original.as_pointer() in ptrs:
            _track_draw['version'] += 1
            return


@bpy.app.handlers.persistent
def _track_reload(*_args):
    """Undo, redo and file loads re-create every object."""
    _track_draw['obj'] = None
    _track_draw['lookup'] = True
    _track_draw['version'] += 1


@bpy.app.handlers.persistent
def _track_load(*_args):
    _track_reload()
    for scene in bpy.data.scenes:
        if any(o.get('c2_preview') == 'track' for o in _objs(scene, 'PREVIEW')):
            build_track_preview(scene)


def _track_preview_changed(_self, _context):
    _tag_redraw()


# ----------------------------------------------------------------------------
# .dbl paths: curve objects (POLY splines, game space)
# ----------------------------------------------------------------------------
import dbl as dblmod   # noqa: E402


def _curve_points(obj):
    """(points, closed) of a path curve's first spline."""
    sp = obj.data.splines[0] if obj.data.splines else None
    if sp is None:
        return [], False
    if sp.type == 'BEZIER':
        return [tuple(p.co) for p in sp.bezier_points], sp.use_cyclic_u
    return [tuple(p.co[:3]) for p in sp.points], sp.use_cyclic_u


def path_hash(obj):
    """Fingerprint of a path object (points, closed, name, placement)."""
    pts, cyc = _curve_points(obj)
    return _hash(np.asarray(pts, np.float32).tobytes(), cyc, obj.get('c2_path_name', obj.name),
                 [round(v, 6) for row in obj.matrix_basis for v in row])


def make_path_object(scene, cols, name, points, closed, bspline_only=False):
    """A path as a POLY curve object (NavMesh_Spline_* go to Bridges).
    bspline_only: the path exists only as a B-spline in the .dbl.
    """
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    sp = cu.splines.new('POLY')
    sp.points.add(max(0, len(points) - 1))
    for p, q in zip(sp.points, points):
        p.co = (q[0], q[1], q[2], 1.0)
    sp.use_cyclic_u = bool(closed)
    o = bpy.data.objects.new(name, cu)
    col = cols['Bridges'] if name.startswith('NavMesh_Spline') and 'Bridges' in cols else cols['Paths']
    col.objects.link(o)
    _root_child(scene, o)
    o.c2.kind = 'PATH'
    o['c2_path_name'] = name
    if bspline_only:
        o['c2_bspline_only'] = 1
    return o


def import_paths(scene, dbl_path, cols):
    """Every .dbl path as a curve object; their names are remembered so deleted
    paths can be removed on export. -> count.
    """
    if not os.path.exists(dbl_path):
        return 0
    d = dblmod.Dbl.load(dbl_path)
    names = set()
    n = 0
    for pl in d.polylines():
        if pl.name in names:
            continue
        names.add(pl.name)
        o = make_path_object(scene, cols, pl.name, pl.points, pl.closed)
        o['c2_hash'] = path_hash(o)
        n += 1
    for bs in d.bsplines():
        if bs.name in names:
            continue
        names.add(bs.name)
        o = make_path_object(scene, cols, bs.name, bs.ctrl, bs.periodic, True)
        o['c2_hash'] = path_hash(o)
        n += 1
    scene['c2_dbl_names'] = sorted(names)
    return n


def export_paths(scene, src_dbl, out_dbl, report):
    """Write changed/new paths into a copy of the source .dbl and drop deleted
    ones. Returns False when nothing changed (the original is then copied).
    """
    if not os.path.exists(src_dbl) or 'c2_dbl_names' not in scene:
        return False
    d = dblmod.Dbl.load(src_dbl)
    objs = _objs(scene, 'PATH')
    changed = 0
    have = set()
    # Collect lines shown one curve per lap (collectgates.split_lines): joined back into the one path
    laps = {}
    for o in objs:
        if o.type == 'CURVE' and o.get('c2_collect_line'):
            laps.setdefault(o['c2_collect_line'], []).append(o)
    for name, ps in laps.items():
        have.add(name)
        ps.sort(key=lambda o: o.get('c2_collect_lap', 0))
        if all(o.get('c2_hash') == path_hash(o) for o in ps) and len(ps) == ps[0].get('c2_collect_pieces', len(ps)):
            continue
        import collectline
        pts = collectline.join([game_points(scene, o, _curve_points(o)[0]) for o in ps])
        if len(pts) < 2:
            report.warn('collect line %s has fewer than 2 points - skipped' % name)
            continue
        if len(ps) != ps[0].get('c2_collect_pieces', len(ps)):
            report.warn('collect line %s: %d of its %d lap curves left - the others are missing from it'
                        % (name, len(ps), ps[0].get('c2_collect_pieces', len(ps))))
        if ps[0].get('c2_bspline_only'):
            sec = d.section(dblmod.BSPLINES) or d._add(dblmod.BSPLINES)
            bs = dblmod.BSpline.uniform(name, pts, False)
            sec.items = [bs if b.name == name else b for b in sec.items]
            if not any(b.name == name for b in sec.items):
                sec.items.append(bs)
            sec.size = None
        else:
            d.set_path(name, pts, False)
        changed += 1
        report.info('collect line %s written from its %d lap curves (%d points)' % (name, len(ps), len(pts)))
    for o in objs:
        if o.type != 'CURVE' or o.get('c2_collect_line'):
            continue
        name = o.get('c2_path_name') or o.name
        have.add(name)
        if o.get('c2_hash') == path_hash(o):
            continue
        pts, cyc = _curve_points(o)
        if len(pts) < 2:
            report.warn('path %s has fewer than 2 points - skipped' % name)
            continue
        pts = game_points(scene, o, pts)
        if o.get('c2_bspline_only'):
            sec = d.section(dblmod.BSPLINES) or d._add(dblmod.BSPLINES)
            bs = dblmod.BSpline.uniform(name, pts, cyc)
            sec.items = [bs if b.name == name else b for b in sec.items]
            if not any(b.name == name for b in sec.items):
                sec.items.append(bs)
            sec.size = None
        else:
            d.set_path(name, pts, cyc)
        changed += 1
        report.info('path %s written (%d points%s)' % (name, len(pts), ', closed' if cyc else ''))
    for name in scene['c2_dbl_names']:
        if name not in have:
            d.remove_path(name)
            changed += 1
            report.info('path %s removed' % name)
    if not changed:
        return False
    names = {p.name for p in d.polylines()} | {b.name for b in d.bsplines()}
    if _objs(scene, 'AI_TRACK') and not names & {'TrackA', 'TrackB', 'TrackC', 'lane1'}:
        report.warn('no TrackA/TrackB/TrackC/lane1 path: races in this world have no lap progress')
    d.save(out_dbl)
    return True


# ----------------------------------------------------------------------------
# nav meshes
# ----------------------------------------------------------------------------
import navmesh as navmod   # noqa: E402


def navmesh_material():
    """See-through green display material for nav meshes."""
    m = bpy.data.materials.get('C2 Nav Mesh')
    if m is None:
        m = bpy.data.materials.new('C2 Nav Mesh')
        m.diffuse_color = (0.1, 0.9, 0.3, 0.45)
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get('Principled BSDF')
        if bsdf is not None:
            bsdf.inputs['Base Color'].default_value = (0.1, 0.9, 0.3, 1)
            bsdf.inputs['Alpha'].default_value = 0.45
        try:
            m.blend_method = 'BLEND'
        except (AttributeError, TypeError):
            pass
        m.use_fake_user = True
    return m


def navmesh_hash(obj):
    """Fingerprint of a nav mesh object."""
    me = obj.data
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', co)
    lv = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get('vertex_index', lv)
    fl = _get_attr(me, 'c2_flags', 'INT', len(me.polygons), 0)
    return _hash(co.tobytes(), lv.tobytes(), fl.tobytes(), obj.get('c2_navmesh_name', obj.name),
                 [round(v, 6) for row in obj.matrix_basis for v in row])


def import_navmeshes(scene, tree, cols):
    """Every NavMeshPool entry as a mesh object (triangle flags in 'c2_flags')."""
    pool = tree.get('NavMeshPool')
    if not pool:
        return 0
    mat = navmesh_material()
    for k, (key, v) in enumerate(pool.items()):
        nm = navmod.NavMesh.parse(bytes(v['Data']))
        me = bpy.data.meshes.new(nm.name)
        me.from_pydata(nm.verts, [], [t[4:7] for t in nm.tris])
        _set_attr(me, 'c2_flags', 'INT', 'FACE', [t[0] for t in nm.tris])
        me.materials.append(mat)
        o = bpy.data.objects.new(nm.name, me)
        cols['NavMeshes'].objects.link(o)
        _root_child(scene, o)
        o.c2.kind = 'NAVMESH'
        o['c2_navmesh_name'] = nm.name
        o['c2_index'] = k
        o['c2_hash'] = navmesh_hash(o)
        o.show_wire = True
        o.hide_render = True
    return len(pool)


def navmesh_from_object(scene, obj, flags_by_face=None):
    """Mesh object -> navmesh.NavMesh (triangulated, game space, neighbours
    and octree rebuilt).
    """
    if obj.mode == 'EDIT':
        obj.update_from_editmode()
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    fl_layer = bm.faces.layers.int.get('c2_flags')
    bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3])
    bm.verts.index_update()
    pts = game_points(scene, obj, [v.co[:] for v in bm.verts])
    tris = []
    for f in bm.faces:
        fl = f[fl_layer] if fl_layer is not None else 0
        tris.append([fl, 0xFFFF, 0xFFFF, 0xFFFF] + [v.index for v in f.verts])
    bm.free()
    nm = navmod.NavMesh(obj.get('c2_navmesh_name') or obj.name, pts, tris, (0, 0, 0), 1.0)
    nm.rebuild_adjacency()
    # edge flags belong to edges: a re-wound/split face keeps the face's value,
    # flags of open edges are dropped on rebuilt meshes only if they no longer exist
    nm.rebuild_tree()
    return nm


def export_navmeshes(scene, tree, report):
    """Rebuild NavMeshPool: unchanged meshes keep their original data, changed
    or new ones are packed fresh, deleted ones dropped.
    """
    objs = sorted(_objs(scene, 'NAVMESH'), key=lambda o: (o.get('c2_index', -1) < 0, o.get('c2_index', 0), o.name))
    pool = tree.get('NavMeshPool')
    if pool is None and not objs:
        return
    orig = list(pool.values()) if pool else []
    if len(objs) == len(orig) and all(o.get('c2_index', -1) == k and o.get('c2_hash') == navmesh_hash(o)
                                      for k, o in enumerate(objs)):
        report.info('nav meshes unchanged - kept as is')
        return
    new = {}
    for k, o in enumerate(objs):
        oi = o.get('c2_index', -1)
        if 0 <= oi < len(orig) and o.get('c2_hash') == navmesh_hash(o):
            data = bytes(orig[oi]['Data'])
        else:
            nm = navmesh_from_object(scene, o)
            if len(nm.tris) > 0xFFFE or len(nm.verts) > 0xFFFF:
                report.warn('nav mesh %s is too big (%d triangles) - skipped' % (o.name, len(nm.tris)))
                continue
            data = nm.pack()
            report.info('nav mesh %s rebuilt: %d triangles, %d vertices' % (nm.name, len(nm.tris), len(nm.verts)))
        new['NavMesh#%d' % len(new)] = {'Data': data}
    removed = len(orig) - sum(1 for o in objs if 0 <= o.get('c2_index', -1) < len(orig))
    if removed > 0:
        report.info('%d nav mesh(es) removed' % removed)
    if pool is None:
        items = list(tree.items())
        tree.clear()
        tree['NavMeshPool'] = new
        tree.update(items)
    else:
        tree['NavMeshPool'] = new


# ----------------------------------------------------------------------------
# Meridian: spawn lists, starting grids, triggers, anchors, node settings
# ----------------------------------------------------------------------------
import meridian   # noqa: E402

SETTINGS_TYPES = ('TakedownSpawnSettings', 'CollectSettings', 'CollectGate', 'AIHint', 'CarsAIHandle',
                  'HunterGame', 'HunterBossGame', 'ArenaCombatGame', 'CTFGame', 'NavmeshBridge',
                  'TrackResetOverride', 'PowerTrainOverride', 'SpawnInstanceNode', 'MissionGameType',
                  'RaceManager')
SKIP_KEYS = {'Type', 'Uuid', 'IsRootObject', 'NumInLinks', 'NumOutLinks', 'AuthoredName', 'IsProxy',
             'Parent', 'Group', 'SceneNodeIndex'}


def _obj_for_source(g, ts, by_mer):
    """Scene object of the locator that owns source port ts."""
    owner = g.owner_of_source(ts)
    return by_mer.get(owner) if owner is not None else None


def organize_meridian(scene, g, node_objs):
    """Move Meridian objects into the Meridian sub-collections and read the
    spawn lists, starting grids and gameplay node settings."""
    cols = collections(scene)
    by_mer = {o.c2.mer_index: o for o in node_objs.values() if o.c2.meridian != 'NONE' and o.c2.mer_index >= 0}
    placed = set()
    # spawn / restart lists
    scene.c2.spawn_lists.clear()
    for m in g.markup_lists():
        it = scene.c2.spawn_lists.add()
        it.name = m['name']
        it.kind = m['kind']
        it.has_entry = bool(m['entries'])
        members = [_obj_for_source(g, s, by_mer) for s in m['sources']]
        members = [o for o in members if o is not None]
        others = len(m['sources']) - len(members)
        it.info = '%d locator(s)%s' % (len(members), ', %d from actors' % others if others else '')
        if not members:
            continue
        col = spawn_collection(scene, m['name'])
        for o in members:
            lists = json.loads(o.get('c2_lists', '[]'))
            if m['name'] not in lists:
                lists.append(m['name'])
            o['c2_lists'] = json.dumps(lists)
            o['c2_lists0'] = o['c2_lists']
            if o.name in placed:
                link_also(o, col)
            else:
                move_to(o, col)
                placed.add(o.name)
    # starting grids
    scene.c2.grids.clear()
    for gi in g.of_type('StartingGrid'):
        n = g.nodes[gi]
        it = scene.c2.grids.add()
        it.name = n.get('AuthoredName', 'Starting Grid')
        it.uuid = meridian.uuid_hex(n.get('Uuid'))
        it.track = int(n.get('Track', 0))
        it.row_count = int(n.get('RowCount', 6))
        it.h_spacing = float(n.get('HorizontalSpacing', 6.0))
        it.v_spacing = float(n.get('VerticalSpacing', 8.0))
        it.stagger = float(n.get('RowStagger', 0.0))
        it.custom23 = bool(n.get('CustomGrid2-3', 0))
        it.custom32 = bool(n.get('CustomGrid3-2', 0))
        for port, attr in (('GridPosition', 'locator'), ('CollectGridPosition', 'collect_locator')):
            src = g.link_sources(n.get(port, -1), 'TransformFetch') if port in n else []
            o = _obj_for_source(g, src[0], by_mer) if src else None
            if o is not None:
                setattr(it, attr, o)
                if o.name not in placed:
                    move_to(o, cols['Grids'])
                    placed.add(o.name)
                else:
                    link_also(o, cols['Grids'])
    # choreography anchors
    for c in scene.c2.choreos:
        for a in c.anchors:
            o = a.locator
            if o is not None and o.name not in placed:
                move_to(o, cols['Anchors'])
                placed.add(o.name)
    # triggers (walk-in volumes) and the remaining Meridian-bound objects
    from . import ops
    for o in node_objs.values():
        if o.name in placed or o.c2.kind == 'ROOT':
            continue
        if o.get('c2_helper') == 'trigger' or ops.is_trigger(scene, o):
            move_to(o, cols['Triggers'])
            placed.add(o.name)
        elif o.c2.meridian == 'LOCATOR' and o.c2.kind == 'NODE':   # instances stay with their geometry
            move_to(o, cols['Locators'])
            placed.add(o.name)
    # gameplay node settings
    scene.c2.node_settings.clear()
    for i, n in enumerate(g.nodes):
        if n.get('Type') not in SETTINGS_TYPES:
            continue
        ports = set(g.ports_of(i))
        kvs = [(k, v) for k, v in n.items() if k not in SKIP_KEYS and not isinstance(v, (bytes, bytearray, list))
               and not (isinstance(v, int) and v in ports)]
        if not kvs:
            continue
        it = scene.c2.node_settings.add()
        it.name = n.get('AuthoredName', n['Type'])
        it.uuid = meridian.uuid_hex(n.get('Uuid'))
        it.node_type = n['Type']
        for k, v in kvs:
            kv = it.kvs.add()
            kv.key = k
            kv.value = repr(v) if isinstance(v, float) else str(v)
    for it in scene.c2.grids:
        refresh_grid_preview(scene, it)


def _parse_like(old, text):
    """Text -> a value of the same type as `old` (node settings edited as text)."""
    if isinstance(old, bool):
        return bool(int(text))
    if isinstance(old, int):
        return int(float(text))
    if isinstance(old, float):
        return float(text)
    return text


def export_meridian(scene, g, obj_ts, report):
    """Spawn-list membership, starting-grid settings and node settings."""
    lists = {m['name']: m for m in g.markup_lists()}
    # spawn lists: 'c2_lists' = the lists a locator should be in now,
    # 'c2_lists0' = the lists it was in when imported. Joining = a
    # TransformFetch link from the locator to the list's entry port.
    for o in scene.objects:
        if 'c2_lists' not in o and 'c2_lists0' not in o:
            continue
        want = set(json.loads(o.get('c2_lists', '[]')))
        had = set(json.loads(o.get('c2_lists0', '[]')))
        if want == had:
            continue
        u = obj_ts.get(o.name)
        ts = g.find_uuid(u) if u else None
        if ts is None:
            report.warn('%s: not a Meridian locator yet - spawn lists not changed' % o.name)
            continue
        for name in sorted(want - had):
            m = lists.get(name)
            if m is None:
                report.warn('%s: spawn list %s not in this world' % (o.name, name))
                continue
            entries = list(m['entries'].values())
            if not entries:
                report.warn('%s: spawn list %s has no locator entry to join' % (o.name, name))
                continue
            entry = max(set(entries), key=entries.count)
            g.add_link('TransformFetch', ts, entry)
            report.info('%s joins spawn list %s' % (o.name, name))
        for name in sorted(had - want):
            m = lists.get(name)
            if m is None:
                continue
            ent = set(m['entries'].values())
            for li in list(g.out_links.get(ts, [])):
                l = g.links[li]
                if l['Type'] == 'TransformFetch' and l['Target'] in ent:
                    g.remove_link(li)
                    break
            report.info('%s leaves spawn list %s' % (o.name, name))
    for it in scene.c2.grids:
        gi = g.find_uuid(it.uuid)
        if gi is None:
            continue
        n = g.nodes[gi]
        vals = {'Track': it.track, 'RowCount': it.row_count, 'CustomGrid2-3': int(it.custom23),
                'CustomGrid3-2': int(it.custom32), 'HorizontalSpacing': it.h_spacing,
                'VerticalSpacing': it.v_spacing, 'RowStagger': it.stagger}
        for k, v in vals.items():
            old = n.get(k)
            if isinstance(v, float) and isinstance(old, float) and abs(old - v) < 1e-6:
                continue
            if old != v:
                n[k] = type(old)(v) if old is not None else v
                report.info('%s: %s = %s' % (it.name, k, v))
        for port, attr in (('GridPosition', 'locator'), ('CollectGridPosition', 'collect_locator')):
            o = getattr(it, attr)
            if o is None or port not in n:
                continue
            u = obj_ts.get(o.name)
            ts = g.find_uuid(u) if u else None
            cur = g.link_sources(n[port], 'TransformFetch')
            if ts is not None and cur != [ts]:
                for li in list(g.in_links.get(n[port], [])):
                    if g.links[li]['Type'] == 'TransformFetch':
                        g.remove_link(li)
                        break
                g.add_link('TransformFetch', ts, n[port])
                report.info('%s: %s now %s' % (it.name, port, o.name))
    for it in scene.c2.node_settings:
        gi = g.find_uuid(it.uuid)
        if gi is None:
            continue
        n = g.nodes[gi]
        for kv in it.kvs:
            if kv.key not in n:
                continue
            old = n[kv.key]
            try:
                new = _parse_like(old, kv.value)
            except ValueError:
                report.warn('%s: %s = %r is not a valid value' % (it.name, kv.key, kv.value))
                continue
            if (isinstance(old, float) and abs(old - new) > 1e-7) or (not isinstance(old, float) and old != new):
                n[kv.key] = new
                report.info('%s: %s = %s' % (it.name, kv.key, new))


# ----------------------------------------------------------------------------
# characters of choreographies (each needs a world actor)
# ----------------------------------------------------------------------------

def assets_dir(scene):
    """The game's assets folder (see common.game_assets)."""
    from . import common
    return common.game_assets(scene)


def refresh_choreo_actors(scene, c, existing=None):
    """Fill c.actors from the choreography file's character slots; `existing`
    = slot names already bound in the source graph."""
    import choreo
    assets = assets_dir(scene)
    if not assets or not c.choreography_name:
        return
    old = {a.name: (a.spawn, a.exists) for a in c.actors}
    c.actors.clear()
    for slot, source in choreo.character_slots(assets, c.choreography_name):
        a = c.actors.add()
        a.name = slot
        a.source = source
        a.model = choreo.character_model_name(assets, source)
        # new choreographies get their actors; ones read from the game are left
        # as they ship unless the user ticks Spawn Actor
        spawn, exists = old.get(slot, (not c.uuid, False))
        a.exists = slot in existing if existing is not None else exists
        a.spawn = spawn


def actor_ap_section(name, namespace, uuid_hex, model):
    """An .ap actor that shows a character model (copied from radiator_a's [mater])."""
    return (name, [('CollisionBinding', 'False'), ('RigidBodyBinding', 'False'), ('DisableContribCulling', 'False'),
                   ('DistanceAlphaFadeStart', '0'), ('DistanceAlphaFadeEnd', '0'), ('DisableVisCulling', 'False'),
                   ('VizCullBinding', 'False'), ('cpName', 'neverReset'), ('shutdownDistance', '100'),
                   ('ActorInteractionSettings', 'True'), ('useAsSendPoint', 'False'), ('class', 'compactor'),
                   ('name', 'MiscActors'), ('AddComponent_MeridianActor', 'True'), ('MeridianNameSpace', namespace),
                   ('MeridianActor_Uuid', uuid_hex), ('enableShutdown', '0'), ('destroyWhenDead', 'False'),
                   ('model_name', model)])


def actor_scene_node(name, matrix16):
    """Actor root: a parentless Geometry node with no parts (DummyMesh), like radiator_a's 'mater'."""
    from c2oct import FloatList
    return {'NodeName': name, 'Uuid': meridian.new_uuid(), 'DisplayLayer': -1, 'Type': 'Geometry', 'Visible': 1,
            'DynamicVisPlacement': 1, 'MeshName': 'DummyMesh',
            'BoundingSphereCenter': FloatList([0.0, 0.0, 0.0]), 'BoundingSphereRadius': 0.8660253882408142,
            'BoundingOBBOrientation': FloatList([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
            'BoundingOBBCenter': FloatList([0.0, 0.0, 0.0]), 'BoundingOBBExtents': FloatList([0.5, 0.5, 0.5]),
            'BoundingBox': FloatList([-0.5, -0.5, -0.5, 0.5, 0.5, 0.5]), 'BoundingType': 'Box',
            'LocalToParentMatrix': FloatList(matrix16)}


# ----------------------------------------------------------------------------
# starting grid preview (children of the grid locator: they follow it live)
# ----------------------------------------------------------------------------

def refresh_grid_preview(scene, it):
    """Arrow empties for every slot of a starting grid, parented to its
    locator so they follow it live.
    """
    common.remove_ids([o for o in _objs(scene, 'PREVIEW') if o.get('c2_preview') == 'grid' and
                       o.get('c2_grid') == it.uuid], data=False)
    loc = it.locator
    if loc is None:
        return
    cols = collections(scene)
    for s in range(scene.c2.grid_racers):
        x, z = trackmod.grid_slot(s, scene.c2.grid_racers, it.row_count, it.h_spacing, it.v_spacing, it.stagger,
                                  it.custom23, it.custom32)
        o = bpy.data.objects.new('%s slot %d' % (it.name, s + 1), None)
        o.empty_display_type = 'SINGLE_ARROW'
        o.empty_display_size = 2.5
        cols['Grids'].objects.link(o)
        o.parent = loc
        o.matrix_parent_inverse = Matrix.Identity(4)
        # the locator's local frame is the game's: across = +X, forward = +Z
        # (single arrows point along +Z)
        sc = loc.matrix_world.to_scale()
        o.matrix_basis = Matrix.Translation((x / (sc.x or 1), 0.0, z / (sc.z or 1)))
        o.c2.kind = 'PREVIEW'
        o['c2_preview'] = 'grid'
        o['c2_grid'] = it.uuid
        o.hide_select = True
        o.hide_render = True
        o.show_name = s == 0


# ----------------------------------------------------------------------------
# import entry point (also for worlds imported by older versions)
# ----------------------------------------------------------------------------

def import_all(context, scene, sc, base, node_objs, g):
    """All gameplay data of a world (called by the importer and by Load
    Gameplay Data for older files).
    """
    cols = collections(scene)
    t = import_track(scene, sc.tree, cols)
    n_paths = import_paths(scene, base + '.dbl', cols)
    n_nav = import_navmeshes(scene, sc.tree, cols)
    if g is not None:
        organize_meridian(scene, g, node_objs)
    _remember_markers(scene)
    scene.c2.has_gameplay = True
    # the .dbl paths start shown (0.29.13): hidden, the collect line laps drawn over them looked
    # editable but couldn't be picked. Their outline (collectgates: light blue) tells them apart
    for key in ('Paths', 'Bridges'):
        lc = layer_collection(context, cols.get(key))
        if lc is not None:
            lc.hide_viewport = False
    print('Cars 2 Scene: gameplay - track %s, %d paths, %d nav meshes, %d spawn lists, %d grids'
          % ('yes' if t else 'no', n_paths, n_nav, len(scene.c2.spawn_lists), len(scene.c2.grids)))


def layer_collection(context, col, lc=None):
    """The view-layer entry of collection `col` (where viewport visibility lives)."""
    if col is None:
        return None
    lc = lc or context.view_layer.layer_collection
    if lc.collection == col:
        return lc
    for c in lc.children:
        r = layer_collection(context, col, c)
        if r is not None:
            return r
    return None


# ----------------------------------------------------------------------------
# operators
# ----------------------------------------------------------------------------

def _need(op, context):
    """Report an error and return False when no world is imported."""
    if context.scene.c2.root is None:
        op.report({'ERROR'}, 'Import a Cars 2 world first')
        return False
    return True


class C2_OT_load_gameplay(bpy.types.Operator):
    """Read gameplay data into a scene imported by an older version."""
    bl_idname = 'c2.load_gameplay'
    bl_label = 'Load Gameplay Data'
    bl_description = ('Read the AI track, paths, nav meshes, spawn lists and grids of this world '
                      '(for worlds imported before this editor version)')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _need(self, context):
            return {'CANCELLED'}
        scene = context.scene
        if scene.c2.has_gameplay:
            self.report({'WARNING'}, 'Gameplay data is already loaded')
            return {'CANCELLED'}
        import octscene
        src = bpy.path.abspath(scene.c2.oct_path)
        sc = octscene.OctScene(src)
        base = os.path.join(sc.folder, sc.name)
        node_objs = dict(enumerate([scene.c2.root] + common.descendants(scene.c2.root)))
        g = None
        if scene.c2.has_mer:
            import c2oct
            g = meridian.Graph(c2oct.load(base + '.mer'))
        import_all(context, scene, sc, base, node_objs, g)
        return {'FINISHED'}


class C2_OT_toggle_layer(bpy.types.Operator):
    """Show/hide a collection (Shift: solo)."""
    bl_idname = 'c2.toggle_layer'
    bl_label = 'Show/Hide'
    bl_description = 'Show or hide this collection in the viewport (Shift: show only this one)'
    bl_options = {'REGISTER', 'UNDO'}
    collection: StringProperty()
    solo: BoolProperty(default=False)

    def invoke(self, context, event):
        self.solo = event.shift
        return self.execute(context)

    def execute(self, context):
        col = bpy.data.collections.get(self.collection)
        lc = layer_collection(context, col)
        if lc is None:
            return {'CANCELLED'}
        if self.solo:
            cols = collections(context.scene, False)
            for k, c in cols.items():
                if k == 'Main':
                    continue
                l2 = layer_collection(context, c)
                if l2 is not None:
                    l2.hide_viewport = c != col and not _is_ancestor(c, col)
            lc.hide_viewport = False
        else:
            lc.hide_viewport = not lc.hide_viewport
        return {'FINISHED'}


def _is_ancestor(a, b):
    """Is collection b inside collection a (at any depth)?"""
    return any(ch == b or _is_ancestor(ch, b) for ch in a.children)


class C2_OT_track_preview(bpy.types.Operator):
    """Redraw the AI track preview."""
    bl_idname = 'c2.track_preview'
    bl_label = 'Refresh Track Preview'
    bl_description = 'Redraw the track edges, racing lines and link arrows from the AI Track Graph'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _objs(context.scene, 'AI_TRACK'):
            self.report({'WARNING'}, 'This world has no AI Track Graph')
            return {'CANCELLED'}
        build_track_preview(context.scene)
        return {'FINISHED'}


class C2_OT_track_new(bpy.types.Operator):
    """Start a new AI track."""
    bl_idname = 'c2.track_new'
    bl_label = 'New AI Track'
    bl_description = 'Start an AI track graph from the 3D cursor (extrude vertices in Edit Mode to lay it out)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not _need(self, context):
            return {'CANCELLED'}
        if _objs(scene, 'AI_TRACK'):
            self.report({'ERROR'}, 'This world already has an AI Track Graph')
            return {'CANCELLED'}
        cols = collections(scene)
        root = scene.c2.root
        p = root.matrix_world.inverted() @ scene.cursor.location
        me = bpy.data.meshes.new('AI Track')
        me.from_pydata([tuple(p), tuple(p + Vector((0, 0, 20)))], [(0, 1)], [])
        for k in NODE_FLOATS:
            _set_attr(me, k, 'FLOAT', 'POINT', [32.0 if k == 'Width' else (0.5 if 'LineU' in k else 0.0)] * 2)
        for k in NODE_INTS:
            _set_attr(me, k, 'INT', 'POINT', [0, 0])
        for k in LINK_FLOATS:
            _set_attr(me, k, 'FLOAT', 'EDGE', [0.0 if k == 'StraightawayLength' else 100.0])
        for k in LINK_INTS:
            _set_attr(me, k, 'INT', 'EDGE', [-1 if k == 'AvoidanceBitfield' else (1 if k == 'CanRespawn' else 0)])
        _set_attr(me, 'c2_section', 'INT', 'EDGE', [-1])
        obj = bpy.data.objects.new('AI Track Graph', me)
        cols['Track'].objects.link(obj)
        _root_child(scene, obj)
        obj.c2.kind = 'AI_TRACK'
        obj.show_in_front = True
        obj['c2_sections'] = '[]'
        obj['c2_hash'] = ''
        build_track_preview(scene)
        return {'FINISHED'}


def _track_bm(context):
    """(object, bmesh) of the AI track in Edit Mode, else (None, None)."""
    o = context.active_object
    if o is None or o.c2.kind != 'AI_TRACK' or o.mode != 'EDIT':
        return None, None
    return o, bmesh.from_edit_mesh(o.data)


class C2_OT_track_set(bpy.types.Operator):
    """Set a node/link value on the selection."""
    bl_idname = 'c2.track_set'
    bl_label = 'Set On Selected'
    bl_description = 'Set this value on the selected track nodes (vertices) or links (edges)'
    bl_options = {'REGISTER', 'UNDO'}
    attr: StringProperty()
    value: StringProperty(name='Value')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, title='Set %s on selected' % self.attr)

    def execute(self, context):
        o, bm = _track_bm(context)
        if bm is None:
            self.report({'ERROR'}, 'Edit the AI Track Graph in Edit Mode')
            return {'CANCELLED'}
        edge = self.attr in LINK_FLOATS + LINK_INTS
        seq = bm.edges if edge else bm.verts
        is_int = self.attr in LINK_INTS + NODE_INTS
        layers = seq.layers.int if is_int else seq.layers.float
        lay = layers.get(self.attr) or layers.new(self.attr)
        try:
            v = int(float(self.value)) if is_int else float(self.value)
        except ValueError:
            self.report({'ERROR'}, 'Not a number: %s' % self.value)
            return {'CANCELLED'}
        n = 0
        for e in seq:
            if e.select:
                e[lay] = v
                n += 1
        bmesh.update_edit_mesh(o.data)
        self.report({'INFO'}, '%s = %s on %d %s' % (self.attr, v, n, 'links' if edge else 'nodes'))
        return {'FINISHED'}


class C2_OT_track_flip(bpy.types.Operator):
    """Reverse link directions."""
    bl_idname = 'c2.track_flip'
    bl_label = 'Flip Link Direction'
    bl_description = 'Reverse the direction of travel of the selected links (edges)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        o, bm = _track_bm(context)
        if bm is None:
            self.report({'ERROR'}, 'Edit the AI Track Graph in Edit Mode')
            return {'CANCELLED'}
        sel = [e for e in bm.edges if e.select]
        lay_i = {k: bm.edges.layers.int.get(k) for k in LINK_INTS + ('c2_section',)}
        lay_f = {k: bm.edges.layers.float.get(k) for k in LINK_FLOATS}
        for e in sel:
            a, b = e.verts
            vals_i = {k: e[l] for k, l in lay_i.items() if l is not None}
            vals_f = {k: e[l] for k, l in lay_f.items() if l is not None}
            bm.edges.remove(e)
            ne = bm.edges.new((b, a))
            for k, l in lay_i.items():
                if l is not None:
                    ne[l] = vals_i[k]
            for k, l in lay_f.items():
                if l is not None:
                    ne[l] = vals_f[k]
            ne.select = True
        bmesh.update_edit_mesh(o.data)
        self.report({'INFO'}, 'Flipped %d links (they move to the end of the link list)' % len(sel))
        return {'FINISHED'}


class C2_OT_track_frames(bpy.types.Operator):
    """Re-orient track nodes."""
    bl_idname = 'c2.track_frames'
    bl_label = 'Recompute Node Frames'
    bl_description = ('Re-orient the selected nodes along the road (moved and new nodes are re-oriented on '
                      'export anyway)')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        o, bm = _track_bm(context)
        if bm is None:
            self.report({'ERROR'}, 'Edit the AI Track Graph in Edit Mode')
            return {'CANCELLED'}
        lays = [bm.verts.layers.float_vector.get(k) or bm.verts.layers.float_vector.new(k) for k in ROT_ATTRS]
        n = 0
        for v in bm.verts:
            if not v.select:
                continue
            nxt = next((e.other_vert(v).co for e in v.link_edges if e.verts[0] == v), None)
            prv = next((e.other_vert(v).co for e in v.link_edges if e.verts[1] == v), None)
            rot = trackmod.node_frame(tuple(prv) if prv else None, tuple(v.co), tuple(nxt) if nxt else None,
                                      tuple(v[lays[1]]) if v[lays[1]].length > 1e-6 else (0, 1, 0))
            for r in range(3):
                v[lays[r]] = rot[3 * r:3 * r + 3]
            n += 1
        bmesh.update_edit_mesh(o.data)
        self.report({'INFO'}, 'Re-oriented %d nodes' % n)
        return {'FINISHED'}


class C2_OT_marker_add(bpy.types.Operator):
    """Add an AI marker."""
    bl_idname = 'c2.marker_add'
    bl_label = 'Add AI Marker'
    bl_description = 'New AI marker (hop / sidestep / drift) at the 3D cursor'
    bl_options = {'REGISTER', 'UNDO'}
    marker_type: EnumProperty(name='Type', items=MARKER_TYPES[:7])
    track: IntProperty(name='Track', default=0, min=0, max=2)

    def execute(self, context):
        if not _need(self, context):
            return {'CANCELLED'}
        scene = context.scene
        root = scene.c2.root
        p = root.matrix_world.inverted() @ scene.cursor.location
        o = import_marker(scene, collections(scene), {'Type': int(self.marker_type), 'ID': self.track,
                                                      'Position': tuple(p), 'Distance': 0.0}, -1)
        del o['c2_pos0']
        for s in context.selected_objects:
            s.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


class C2_OT_path_new(bpy.types.Operator):
    """Add a .dbl path."""
    bl_idname = 'c2.path_new'
    bl_label = 'New Path'
    bl_description = ('New .dbl path starting at the 3D cursor (name it TrackA/TrackB/TrackC for lap progress, '
                      'lane1 as fallback); edit its points in Edit Mode')
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty(name='Name', default='TrackA')
    closed: BoolProperty(name='Closed Loop', default=True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        if not _need(self, context):
            return {'CANCELLED'}
        scene = context.scene
        p = scene.c2.root.matrix_world.inverted() @ scene.cursor.location
        pts = [tuple(p + Vector((math.cos(a) * 50, 0, math.sin(a) * 50))) for a in
               np.linspace(0, 2 * math.pi, 9)[:-1]] if self.closed else [tuple(p), tuple(p + Vector((0, 0, 50)))]
        o = make_path_object(scene, collections(scene), self.name, pts, self.closed)
        o['c2_hash'] = ''
        if 'c2_dbl_names' not in scene:
            scene['c2_dbl_names'] = []
        for s in context.selected_objects:
            s.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


class C2_OT_navmesh_from_mesh(bpy.types.Operator):
    """Turn meshes into nav meshes."""
    bl_idname = 'c2.navmesh_from_mesh'
    bl_label = 'Make Nav Mesh'
    bl_description = ('Turn the selected mesh objects into arena nav meshes (one walkable island each; '
                      'triangulated on export, neighbours and lookup tree rebuilt)')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _need(self, context):
            return {'CANCELLED'}
        scene = context.scene
        cols = collections(scene)
        mat = navmesh_material()
        n = 0
        for o in context.selected_objects:
            if o.type != 'MESH' or o.c2.kind not in ('NONE', 'NAVMESH'):
                continue
            mw = o.matrix_world.copy()
            move_to(o, cols['NavMeshes'])
            o.parent = scene.c2.root
            o.matrix_parent_inverse = Matrix.Identity(4)
            o.matrix_world = mw
            o.c2.kind = 'NAVMESH'
            o['c2_navmesh_name'] = o.get('c2_navmesh_name') or 'NavMesh%s' % o.name.replace(' ', '')
            o['c2_index'] = o.get('c2_index', -1)
            if not o.data.materials:
                o.data.materials.append(mat)
            o.show_wire = True
            o.hide_render = True
            n += 1
        self.report({'INFO'}, '%d nav mesh(es)' % n)
        return {'FINISHED'}


class C2_OT_spawn_add(bpy.types.Operator):
    """Add a spawn point to the active list."""
    bl_idname = 'c2.spawn_add'
    bl_label = 'Add Spawn Point'
    bl_description = 'New locator at the 3D cursor that joins the active spawn / restart list'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _need(self, context):
            return {'CANCELLED'}
        scene = context.scene
        sl = scene.c2.spawn_lists
        if not (0 <= scene.c2.active_spawn_list < len(sl)):
            return {'CANCELLED'}
        it = sl[scene.c2.active_spawn_list]
        if not it.has_entry:
            self.report({'ERROR'}, '%s is not fed by locators in this world' % it.name)
            return {'CANCELLED'}
        from . import ops
        base = 'LOC_%s_' % it.name
        k = 1
        while bpy.data.objects.get('%s%02d' % (base, k)):
            k += 1
        o = ops.make_locator(context, '%s%02d' % (base, k), None)
        o['c2_lists'] = json.dumps([it.name])
        o['c2_lists0'] = '[]'
        move_to(o, spawn_collection(scene, it.name))
        for s in context.selected_objects:
            s.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


class C2_OT_spawn_membership(bpy.types.Operator):
    """Add/remove locators to/from the active spawn list."""
    bl_idname = 'c2.spawn_membership'
    bl_label = 'Add/Remove Selected'
    bl_description = 'Add the selected locators to (or remove them from) the active spawn / restart list'
    bl_options = {'REGISTER', 'UNDO'}
    remove: BoolProperty(default=False)

    def execute(self, context):
        scene = context.scene
        sl = scene.c2.spawn_lists
        if not (0 <= scene.c2.active_spawn_list < len(sl)):
            return {'CANCELLED'}
        it = sl[scene.c2.active_spawn_list]
        col = spawn_collection(scene, it.name, not self.remove)
        n = 0
        for o in context.selected_objects:
            if o.c2.meridian != 'LOCATOR':
                continue
            lists = json.loads(o.get('c2_lists', '[]'))
            if self.remove and it.name in lists:
                lists.remove(it.name)
                if col is not None and col in o.users_collection and len(o.users_collection) > 1:
                    col.objects.unlink(o)
                elif col is not None and col in o.users_collection:
                    move_to(o, collections(scene)['Locators'])
            elif not self.remove and it.name not in lists:
                lists.append(it.name)
                link_also(o, col)
            else:
                continue
            o['c2_lists'] = json.dumps(lists)
            if 'c2_lists0' not in o:
                o['c2_lists0'] = '[]'
            n += 1
        self.report({'INFO'}, '%d locator(s) %s %s' % (n, 'removed from' if self.remove else 'added to', it.name))
        return {'FINISHED'}


class C2_OT_grid_preview(bpy.types.Operator):
    """Redraw every starting grid's slot arrows"""
    bl_idname = 'c2.grid_preview'
    bl_label = 'Refresh Grid Slots'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for it in context.scene.c2.grids:
            refresh_grid_preview(context.scene, it)
        return {'FINISHED'}


class C2_OT_select_by_kind(bpy.types.Operator):
    """Select every visible object of this kind"""
    bl_idname = 'c2.select_by_kind'
    bl_label = 'Select'
    bl_options = {'REGISTER', 'UNDO'}
    kind: StringProperty()

    def execute(self, context):
        objs = _objs(context.scene, self.kind)
        for s in context.selected_objects:
            s.select_set(False)
        for o in objs:
            if o.visible_get():
                o.select_set(True)
        if objs:
            context.view_layer.objects.active = objs[0]
        return {'FINISHED'}


_classes = (C2_OT_load_gameplay, C2_OT_toggle_layer, C2_OT_track_preview, C2_OT_track_new, C2_OT_track_set,
            C2_OT_track_flip, C2_OT_track_frames, C2_OT_marker_add, C2_OT_path_new, C2_OT_navmesh_from_mesh,
            C2_OT_spawn_add, C2_OT_spawn_membership, C2_OT_grid_preview, C2_OT_select_by_kind)


_TRACK_HANDLERS = (('depsgraph_update_post', _track_depsgraph), ('undo_post', _track_reload),
                   ('redo_post', _track_reload), ('load_post', _track_load))


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    for h, f in _TRACK_HANDLERS:
        getattr(bpy.app.handlers, h).append(f)
    _track_draw['handler'] = bpy.types.SpaceView3D.draw_handler_add(_draw_track, (), 'WINDOW', 'POST_VIEW')


def unregister():
    if _track_draw['handler'] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_track_draw['handler'], 'WINDOW')
        _track_draw['handler'] = None
    for h, f in _TRACK_HANDLERS:
        lst = getattr(bpy.app.handlers, h)
        if f in lst:
            lst.remove(f)
    _track_reload()
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
