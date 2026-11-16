"""Point-to-point races: the race ends at a finish point, not at the start line.

How the game ends a race (sdaemon.exe, Cars 2 Arcade):

* Every car's LapTracker measures its position along the lap spline (the .dbl
  path TrackA / TrackB / TrackC for the current track, else 'lane1') as a
  0..1 fraction. A lap counts when it wraps forward (above 0.95 to below 0.05).
  A car finishes when laps + fraction reaches the lap count: RacerFinished,
  the placement stinger, its finish time. RaceManager ends the race when the
  leading human has finished.
* The lap count is the arcade setting "Number of Laps per Game" (the game's
  track settings), the same for every track.

Two ways to finish elsewhere (scene.c2.finish_mode):

LAP (vanilla finish): Build Point-to-Point Lap reshapes the lap spline (see
    core/p2p.py) so a car crossing the finish point completes the lap. With
    the lap count set to 1 the race starts at the grid and ends at the finish
    exactly like a normal race. Only the .dbl changes.
MESSAGE: on export the world's graph gets a ProximityNode watching players
    1-4 (AI cars never trigger it) around the finish locator, whose
    Inside / Begin sends EndTutorial: RaceManager handles it in every race and
    jumps to the race-over state (results at once: no stinger, finish times
    not exact). Works with any lap count.
"""
import json
import os

import bpy
import numpy as np
from mathutils import Vector


def export(scene, g, obj_ts, report):
    """MESSAGE mode: add the finish logic to graph g (exporter, after the locators are made)."""
    o = scene.c2.finish_point
    if o is None or scene.c2.finish_mode != 'MESSAGE':
        return
    if o.name not in bpy.data.objects or o.c2.meridian != 'LOCATOR':
        report.warn('finish point %s is not a Meridian locator: race finish not written' % o.name)
        return
    ts = obj_ts.get(o.name)
    idx = g.uuid_index.get(ts) if ts is not None else None
    if idx is None:
        report.warn('finish point %s: locator not in the graph, race finish not written' % o.name)
        return
    g.add_race_finish(idx, scene.c2.finish_radius)
    report.info('point-to-point finish at %s (radius %.0f m): the race ends when a player gets there'
                % (o.get('c2_name', o.name), scene.c2.finish_radius))


# ----------------------------------------------------------------------
# LAP mode: the point-to-point lap spline
# ----------------------------------------------------------------------

def _game(scene, obj):
    """Object -> (position, forward) in game space (forward = its local +Z)."""
    m = scene.c2.root.matrix_world.inverted() @ obj.matrix_world
    return np.array(m.translation), np.array(m.col[2][:3])


def _by_name(scene, name):
    return next((o for o in scene.objects if o.get('c2_name', o.name) == name), None)


def grids(scene):
    """[(track letter, grid locator object, depth in metres)] of the world's StartingGrid nodes."""
    from .objplace import _graph
    g = _graph(scene)
    out = []
    if g is None:
        return out
    for i in g.of_type('StartingGrid'):
        n = g.nodes[i]
        t = n.get('Track', 0)
        letter = 'ABC'[t] if isinstance(t, int) and 0 <= t < 3 else 'A'
        srcs = [g.links[li]['Origin'] for li in g.in_links.get(n.get('GridPosition'), [])]
        o = g.locator_of_transform_source(srcs[0]) if srcs else None
        loc = _by_name(scene, g.nodes[o].get('BoundObjectName')) if o is not None else None
        rows = n.get('RowCount', 4)
        depth = float(rows if isinstance(rows, int) else 4) * float(n.get('VerticalSpacing', 14.0)) + \
            float(n.get('RowStagger', 4.0))
        if loc is not None:
            out.append((letter, loc, depth))
    return out


def lap_path(scene, letter):
    """The world's lap path object for a track letter (TrackA/B/C, else lane1)."""
    for name in ('Track' + letter, 'lane1'):
        o = next((x for x in scene.objects if x.c2.kind == 'PATH' and x.get('c2_path_name', x.name) == name), None)
        if o is not None:
            return o
    return None


def source_lap(scene, name):
    """Control points of the lap B-spline in the source .dbl (what the game projects onto)."""
    import dbl
    path = os.path.splitext(bpy.path.abspath(scene.c2.oct_path))[0] + '.dbl'
    if not os.path.isfile(path):
        return None
    d = dbl.Dbl.load(path)
    b = next((x for x in d.bsplines() if x.name == name), None)
    if b is not None:
        c = [tuple(p) for p in b.ctrl]
        return c[:-3] if b.periodic and len(c) > 6 and np.allclose(c[-3:], c[:3]) else c
    p = d.path(name)
    return [tuple(x) for x in p.points] if p is not None else None


def _set_points(obj, pts):
    sp = obj.data.splines[0]
    sp.type = 'POLY'
    n = len(pts)
    if len(sp.points) < n:
        sp.points.add(n - len(sp.points))
    elif len(sp.points) > n:
        # a spline can't lose points: rebuild it
        obj.data.splines.clear()
        sp = obj.data.splines.new('POLY')
        sp.points.add(n - 1)
    for p, q in zip(sp.points, pts):
        p.co = (q[0], q[1], q[2], 1.0)
    sp.use_cyclic_u = True


def build_lap(context, report):
    """Reshape the lap spline for a point-to-point race. Returns the path object."""
    import p2p
    scene = context.scene
    B = scene.c2.finish_point
    if B is None:
        raise RuntimeError('Set a finish point first')
    gl = grids(scene)
    if not gl:
        raise RuntimeError('No StartingGrid with a locator in this world\'s .mer')
    want = scene.c2.track_variant if scene.c2.track_variant not in ('ALL', 'M') else None
    letter, grid, depth = next((x for x in gl if x[0] == want), gl[0])
    path = lap_path(scene, letter)
    if path is None:
        raise RuntimeError('No Track%s (or lane1) path in this world' % letter)
    name = path.get('c2_path_name', path.name)
    lap = source_lap(scene, name)
    if not lap:
        raise RuntimeError('%s not found in the source .dbl' % name)
    gpos, gfwd = _game(scene, grid)
    bpos, _ = _game(scene, B)
    R = p2p.Route(lap)
    start_line = None
    if scene.c2.p2p_start_line is not None:
        start_line, _ = _game(scene, scene.c2.p2p_start_line)
    else:
        # a grid off the lap line drives some way before it joins the route
        s = R.project(gpos)
        if float(np.linalg.norm(R.point(s) - gpos)) > 25.0:
            start_line = gpos + p2p._flat(gfwd) * 150.0
    pts, info = p2p.build(lap, gpos, gfwd, depth, bpos, start_line=start_line)
    if 'c2_p2p_original' not in path:
        sp = path.data.splines[0]
        path['c2_p2p_original'] = json.dumps([list(p.co[:3]) for p in sp.points])
    _set_points(path, pts)
    path['c2_p2p'] = json.dumps({k: v for k, v in info.items()})
    report.append('%s: point-to-point lap built from %s (grid) to %s: %.1f km of route, spline %.0f km'
                  % (name, grid.get('c2_name', grid.name), B.get('c2_name', B.name),
                     info['route'] / 1000.0, info['length'] / 1000.0))
    return path


class C2_OT_finish_point_add(bpy.types.Operator):
    """Add a locator at the 3D cursor and make it the race's finish point (point-to-point race)"""
    bl_idname = 'c2.finish_point_add'
    bl_label = 'Add Finish Point'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .ops import make_locator
        if context.scene.c2.root is None:
            self.report({'ERROR'}, 'Import a world first')
            return {'CANCELLED'}
        o = make_locator(context, 'LOC_RaceFinish')
        o['c2_name'] = o.name
        o['c2_finish_point'] = 1      # Lap mode: a reference only, never exported
        context.scene.c2.finish_point = o
        for s in context.selected_objects:
            s.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


class C2_OT_p2p_lap_build(bpy.types.Operator):
    """Reshape the lap spline so the race starts at the grid and the lap ends at the finish point (the game's own finish: set Number of Laps to 1 in the game's settings). Run it again after moving the finish point"""
    bl_idname = 'c2.p2p_lap_build'
    bl_label = 'Build Point-to-Point Lap'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        lines = []
        try:
            build_lap(context, lines)
        except (RuntimeError, ValueError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, lines[0])
        return {'FINISHED'}


class C2_OT_p2p_lap_restore(bpy.types.Operator):
    """Put the world's original lap spline back (undo Build Point-to-Point Lap)"""
    bl_idname = 'c2.p2p_lap_restore'
    bl_label = 'Restore Lap'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for o in context.scene.objects:
            if o.get('c2_p2p_original'):
                _set_points(o, json.loads(o['c2_p2p_original']))
                del o['c2_p2p_original']
                if 'c2_p2p' in o:
                    del o['c2_p2p']
                n += 1
        self.report({'INFO'}, 'restored %d lap path(s)' % n)
        return {'FINISHED'}


def draw_panel(layout, context):
    sc = context.scene.c2
    box = layout.box()
    box.label(text='Race Finish', icon='TRACKING')
    row = box.row(align=True)
    row.prop(sc, 'finish_point', text='')
    row.operator('c2.finish_point_add', text='', icon='ADD')
    if sc.finish_point is None:
        box.label(text='Laps (the game\'s lap-count setting)')
        return
    box.prop(sc, 'finish_mode', expand=True)
    if sc.finish_mode == 'LAP':
        box.prop(sc, 'p2p_start_line')
        row = box.row(align=True)
        row.operator('c2.p2p_lap_build', icon='CURVE_PATH')
        row.operator('c2.p2p_lap_restore', text='', icon='LOOP_BACK')
        built = any(o.get('c2_p2p') for o in context.scene.objects)
        box.label(text='Built: set Number of Laps to 1 in the game' if built else
                  'Build after placing the finish point', icon='INFO')
    else:
        box.prop(sc, 'finish_radius')
        box.label(text='Ends the race at once (no stinger)', icon='INFO')


_classes = (C2_OT_finish_point_add, C2_OT_p2p_lap_build, C2_OT_p2p_lap_restore)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
