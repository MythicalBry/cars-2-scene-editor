"""The grandstand crowds: rows of sprite cars on the CrowdMemberPlane nodes.

A world's Meridian graph (a *_Crowd namespace: TKO_B_Crowd ...) has
CrowdMemberPlane nodes: a locator (LOC_Crowd_TKO_091 ...), Row Width,
Row Spacing, Number of Rows, Crowd per Row. They are not choreographies:
sdaemon.exe's CrowdManager draws them (italy, london_a/b, tokyo_a/b/c).

Layout (Meridian::CrowdMemberPlaneNode, FUN_005a3800), in the locator's axes:
  side = rotation x (-1, 0, 0), back = rotation x (0, 0, -1)
  gap  = Row Width / (Crowd per Row - 1)
  row r stands r x Row Spacing back; it is centred on the locator, and odd
  rows have one member fewer, shifted half a gap (staggered stands)
  a member narrower than the gap (geometryWidth x facingPercent) is moved
  sideways at random by up to half the difference
  the kind steps through the types by the primes 631 641 643 647 653
  (mod the type count) from a random start every <type count> members

The types (assets/luatables/luatables.zip crowdmanagerinfo.lua, read by
core/luatable.py): RedCar, BlueCar, GreenVan, YellowCar - sprites of
geometryWidth x geometryHeight metres from a 2048 x 1024 atlas (the world's
crowdanim0_60new16fr texture): 16 frames of 256 x 128 per type, two atlas rows
each, turning from facing the viewer (frame 0) towards side-on; mirrored for
the other side. Each member stands geometryHeight / 2 up (its centre) and
bounces BounceInfo.bounceHeight at bounceSpeed with a random phase.

Drawn by the Cars 2 Game renderer (alpha-tested, after the opaque parts)
and over other engines' viewports, from the locators' current transforms:
move a LOC_Crowd locator and its crowd follows (the locators export as
always). Nothing is added to the scene.
"""
import math
import os
import zipfile

import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

STEPS = (631, 641, 643, 647, 653)
ATLAS_NAME = 'crowdanim0_60new16fr'
END_ANGLE = 60.0                # the atlas turns through 0..60 degrees (90 - endDeadZone)

PLANES = []                     # the crowd locators (objects with c2_crowd.enabled): direct references,
                                # a name lookup costs ~0.13 ms per call even in bpy.data.objects
TYPES = []                      # crowdmanagerinfo.lua CrowdMemberList entries
BOUNCE = {'bounceHeight': 0.1, 'bounceSpeed': 2.0}
_handler = [None]
_tex = {}


def load_types(assets):
    """CrowdMemberList / BounceInfo from the game's crowdmanagerinfo.lua."""
    import luatable
    zp = os.path.join(assets or '', 'luatables', 'luatables.zip')
    TYPES.clear()
    if not os.path.exists(zp):
        return False
    try:
        z = zipfile.ZipFile(zp)
        name = next(n for n in z.namelist() if n.lower().endswith('crowdmanagerinfo.lua'))
        info = luatable.load(z.read(name)).get('CrowdManagerInfo', {})
    except (StopIteration, ValueError, KeyError, zipfile.BadZipFile, OSError) as e:
        print('Cars 2 crowd: crowdmanagerinfo.lua not read (%s)' % e)
        return False
    TYPES.extend(luatable.as_list(info.get('CrowdMemberList')))
    b = luatable.as_list(info.get('BounceInfo'))
    if b:
        BOUNCE.update(b[0])
    return bool(TYPES)


def planes(g):
    """[{...}] of the graph's CrowdMemberPlane nodes: their locator, row
    settings and node UUID."""
    import meridian
    out = []

    def fetched(port, default):
        if not isinstance(port, int):
            return default
        v = g.nodes[port].get('Value', default)
        return v if isinstance(v, (int, float)) else default
    for i in g.of_type('CrowdMemberPlane'):
        n = g.nodes[i]
        loc = None
        port = n.get('Transform')
        if isinstance(port, int):
            for li in g.in_links.get(port, []):
                src = g.links[li]['Origin']
                li_ = g.locator_of_transform_source(src)
                if li_ is not None:
                    loc = g.nodes[li_].get('BoundObjectName')
                    break
        if loc is None:
            continue
        out.append({'name': n.get('AuthoredName', 'Crowd %d' % i), 'locator': loc,
                    'uuid': meridian.uuid_hex(n.get('Uuid')),
                    'width': float(fetched(n.get('Width'), 1.0)), 'spacing': float(fetched(n.get('RowSpacing'), 1.0)),
                    'rows': int(fetched(n.get('NumRows'), 1)), 'cols': int(fetched(n.get('NumColumns'), 1))})
    return out


def _values(c):
    return [float(c.width), float(c.spacing), int(c.rows), int(c.cols)]


def build(scene, g, report=None):
    """Read the world's crowd planes (on import) onto their locators
    (obj.c2_crowd). Returns the member count."""
    from . import common
    PLANES.clear()
    _members.clear()
    if g is None:
        return 0
    n = 0
    for p in planes(g):
        o = _locator(scene, p['locator'])
        if o is None or o.c2_crowd.enabled:
            continue            # one plane per locator (the shipped worlds have no shared ones)
        c = o.c2_crowd
        c.enabled = True
        c.plane, c.uuid = p['name'], p['uuid']
        c.width, c.spacing, c.rows, c.cols = p['width'], p['spacing'], p['rows'], p['cols']
        o['c2_crowd0'] = _values(c)
        n += 1
    scene['c2_crowd_planes'] = n
    if not n:
        return 0
    scene['c2_show_crowd'] = scene.get('c2_show_crowd', 1)
    if not refresh(scene) and report is not None:
        report.append('crowd: crowdmanagerinfo.lua not found in assets/luatables - crowds not shown')
    if report is not None:
        report.append('%d crowd planes (sprites, drawn by the renderer)' % n)
    return n


def refresh(scene):
    """The crowd locators, car types and atlas of the scene's world (after an
    import, or when a saved .blend opens)."""
    from . import common
    PLANES.clear()
    _members.clear()
    PLANES.extend(o for o in scene.objects if o.c2_crowd.enabled)
    if not PLANES:
        return False
    ok = load_types(common.game_assets(scene))
    load_atlas(scene)
    ensure_handler()
    return ok


@bpy.app.handlers.persistent
def _undo(_scene=None, _dummy=None):
    """Undo / redo re-create the objects: the held locator references are stale."""
    scene = bpy.context.scene
    if scene is not None and scene.get('c2_crowd_planes'):
        PLANES.clear()
        _members.clear()
        PLANES.extend(o for o in scene.objects if o.c2_crowd.enabled)


@bpy.app.handlers.persistent
def _load_post(_dummy=None):
    for scene in bpy.data.scenes:
        if scene.get('c2_crowd_planes'):
            refresh(scene)
            break


def export(scene, g, report):
    """Edited crowd row settings back into their CrowdMemberPlane nodes."""
    for o in scene.objects:
        c = o.c2_crowd
        if not c.enabled or list(o.get('c2_crowd0', [])) == _values(c):
            continue
        i = g.find_uuid(c.uuid)
        if i is None:
            report.warn('crowd %s: its CrowdMemberPlane node is gone' % c.plane)
            continue
        n = g.nodes[i]
        for field, v in (('Width', float(c.width)), ('RowSpacing', float(c.spacing)),
                         ('NumRows', int(c.rows)), ('NumColumns', int(c.cols))):
            port = n.get(field)
            if isinstance(port, int) and 0 <= port < len(g.nodes):
                g.nodes[port]['Value'] = v
        report.info('crowd %s: %d row(s) of %d, %.1f m wide, rows %.1f m apart'
                    % (c.plane, c.rows, c.cols, c.width, c.spacing))


_members = {}       # (uuid, settings, locator matrix) -> layout arrays (game space)


def _layout(c, M):
    """Members of crowd plane c (a locator's c2_crowd) for the locator's
    game-space matrix M (engine rules): arrays pos (n, 3), facing (n, 3),
    kind (n,), phase (n,). Cached until the plane or its locator changes."""
    p = {'width': float(c.width), 'spacing': float(c.spacing), 'rows': int(c.rows), 'cols': int(c.cols)}
    key = (c.uuid, tuple(p.values()), tuple(round(x, 4) for r in M for x in r))
    hit = _members.get(key)
    if hit is not None:
        return hit
    rng = np.random.default_rng(int(c.uuid[:8] or '0', 16))
    rot = M.to_3x3().normalized()
    side = rot @ Vector((-1.0, 0.0, 0.0))
    back = rot @ Vector((0.0, 0.0, -1.0))
    pos = M.translation
    cols, rows = max(1, p['cols']), max(1, p['rows'])
    gap = p['width'] / (cols - 1) if cols > 1 else 0.0
    ntypes = len(TYPES)
    at_l, kind_l, phase_l = [], [], []
    kind, step, run = 0, 0, ntypes      # run == ntypes: pick a new start first
    for r in range(rows):
        base = pos + back * (r * p['spacing'])
        half, count = p['width'] * 0.5, cols
        if r % 2:
            count -= 1
            half -= gap * 0.5
        start = base - side * half
        for col in range(count):
            at = start + side * (col * gap)
            if run == ntypes:
                run = 0
                step = (step + 1) % len(STEPS)
                kind = int(rng.integers(0, ntypes))
            run += 1
            kind = (kind + STEPS[step]) % ntypes
            t = TYPES[kind]
            foot = float(t.get('geometryWidth', 7.0)) * float(t.get('facingPercent', 1.0))
            if foot < gap:
                at = at + side * float(rng.uniform(-(gap - foot) * 0.5, (gap - foot) * 0.5))
            at_l.append(tuple(at))
            kind_l.append(kind)
            phase_l.append(float(rng.random()))
    n = len(at_l)
    face = -np.array(tuple(back), np.float32)
    out = (np.array(at_l, np.float32).reshape(n, 3), np.tile(face, (n, 1)),
           np.array(kind_l, np.int32), np.array(phase_l, np.float32))
    if len(_members) > 4096:
        _members.clear()
    _members[key] = out
    return out


def _locator(scene, name):
    from .objplace import _locator as loc
    return loc(scene, name)


def _type_table():
    """Per type: width, height, startColumn, startRow, frames, mirrored (arrays)."""
    t = _members.get('types')
    if t is None or len(t[0]) != len(TYPES):
        t = (np.array([float(x.get('geometryWidth', 7.0)) for x in TYPES], np.float32),
             np.array([float(x.get('geometryHeight', 3.5)) for x in TYPES], np.float32),
             np.array([int(x.get('startColumn', 0)) for x in TYPES], np.int32),
             np.array([int(x.get('startRow', 0)) for x in TYPES], np.int32),
             np.array([int(x.get('numFrames', 16)) for x in TYPES], np.int32),
             np.array([bool(x.get('isMirrored')) for x in TYPES], bool))
        _members['types'] = t
    return t


def sprites(scene, cam_pos, t):
    """(centres (n, 3), half sizes (n, 2), uv rects (n, 4), mirrored (n,)) of
    every member, Blender world space; None without a crowd. Vectorised:
    ~0.5 ms for tokyo_a's 104 planes (the per-member loop took ~20 ms)."""
    root = scene.c2.root
    if root is None or not PLANES or not TYPES:
        return None
    rmw = root.matrix_world
    inv = rmw.inverted()
    parts = []
    try:
        for o in PLANES:
            if o.hide_viewport or not o.c2_crowd.enabled:
                continue
            parts.append(_layout(o.c2_crowd, inv @ o.matrix_world))
    except ReferenceError:              # a crowd locator was deleted: look again
        refresh(scene)
        return None
    if not parts:
        return None
    pos = np.concatenate([x[0] for x in parts])
    face = np.concatenate([x[1] for x in parts])
    kind = np.concatenate([x[2] for x in parts])
    phase = np.concatenate([x[3] for x in parts])
    tw, th, sc, sr, nf, mirrored = _type_table()
    # view angle against each member's facing, on the ground plane (game Y up)
    cam = np.array(tuple(inv @ cam_pos), np.float32)
    v = cam[None] - pos
    v[:, 1] = 0.0
    f = face.copy()
    f[:, 1] = 0.0
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-6)
    f /= np.maximum(np.linalg.norm(f, axis=1, keepdims=True), 1e-6)
    ang = np.degrees(np.arccos(np.clip((v * f).sum(1), -1.0, 1.0)))
    mirror = mirrored[kind] & ((f[:, 2] * v[:, 0] - f[:, 0] * v[:, 2]) < 0.0)     # (f x v).y < 0
    n = nf[kind]
    frame = np.minimum(n - 1, np.rint(np.minimum(ang, END_ANGLE) / END_ANGLE * (n - 1)).astype(np.int32))
    fw, fh = 256.0 / 2048.0, 128.0 / 1024.0
    u0 = (sc[kind] + frame % 8) * fw
    v0 = (sr[kind] + frame // 8) * fh
    bh, bs = float(BOUNCE.get('bounceHeight', 0.1)), float(BOUNCE.get('bounceSpeed', 2.0))
    h = th[kind]
    centre = pos.copy()
    centre[:, 1] += h * 0.5 + bh * np.abs(np.sin(np.pi * (bs * t + phase)))
    R = np.array(rmw, np.float32)
    world = centre @ R[:3, :3].T + R[:3, 3]
    half = np.stack([tw[kind] * 0.5, h * 0.5], 1)
    uv = np.stack([u0, v0, u0 + fw, v0 + fh], 1).astype(np.float32)
    return world, half, uv, mirror


def load_atlas(scene):
    """The world's crowd atlas as a Blender image (at import or file load: a
    viewport draw may not load images). Blender's own DDS loader takes
    0.06 s; the path must be absolute (the texture list may hold a relative one)."""
    item = next((t for t in scene.c2.textures if t.name.lower() == ATLAS_NAME), None)
    _tex.pop('atlas', None)
    if item is None or not item.file:
        return None
    path = os.path.abspath(bpy.path.abspath(item.file))
    if not os.path.exists(path):
        return None
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as e:
        print('Cars 2 crowd: atlas not loaded (%s)' % e)
        return None
    scene['c2_crowd_atlas'] = img.name
    return img


def _texture():
    """GPU texture of the world's crowd atlas (sRGB: sampled as linear)."""
    if _tex.get('atlas') is not None:
        return _tex['atlas']
    scene = bpy.context.scene
    img = bpy.data.images.get(scene.get('c2_crowd_atlas', '')) if scene is not None else None
    if img is None:
        return None
    try:
        _tex['atlas'] = gpu.texture.from_image(img)
    except Exception as e:
        print('Cars 2 crowd: atlas not made (%s)' % e)
        return None
    return _tex['atlas']


def draw(scene, vp, view, scale=1.0):
    """Draw the crowd (alpha-tested, depth-writing): vp = projection @ view."""
    if not PLANES or not scene.get('c2_show_crowd', 1):
        return 0
    tex = _texture()
    if tex is None:
        return 0
    from . import particles
    from gpu_extras.batch import batch_for_shader
    vinv = view.inverted_safe()
    cam = vinv.translation
    fps = scene.render.fps / scene.render.fps_base
    data = sprites(scene, cam, (scene.frame_current - 1) / fps)
    if data is None or not len(data[0]):
        return 0
    pos, half, uvr, mir = data
    up_b = np.array(scene.c2.root.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0)), np.float32)
    up_b /= max(np.linalg.norm(up_b), 1e-6)
    # cylindrical billboards: upright, turned to the camera
    to_cam = np.array(cam, np.float32)[None] - pos
    to_cam -= (to_cam @ up_b)[:, None] * up_b[None]
    right = np.cross(up_b[None], to_cam)
    right /= np.maximum(np.linalg.norm(right, axis=1, keepdims=True), 1e-6)
    corners = np.array([(-1, -1), (1, -1), (1, 1), (-1, -1), (1, 1), (-1, 1)], np.float32)
    n = len(pos)
    p = (pos[:, None, :] + corners[None, :, 0:1] * (half[:, None, 0:1] * right[:, None, :]) +
         corners[None, :, 1:2] * (half[:, None, 1:2] * up_b[None, None, :])).reshape(-1, 3)
    # atlas uv (v top-down; the shader flips): u flips for mirrored members
    us = np.where(mir[:, None], uvr[:, [2, 0, 0, 2, 0, 2]], uvr[:, [0, 2, 2, 0, 2, 0]])
    vs = uvr[:, [3, 3, 1, 3, 1, 1]]
    uv = np.stack([us, vs], -1).reshape(-1, 2)
    sh = particles._get_shader()
    batch = batch_for_shader(sh, 'TRIS', {'pos': p, 'uv': uv, 'col': np.ones((n * 6, 4), np.float32)})
    st = gpu.state
    sh.bind()
    sh.uniform_float('vp', vp)
    sh.uniform_float('scale', scale)
    sh.uniform_int('mode', 1)
    sh.uniform_sampler('tex', tex)
    st.depth_test_set('LESS_EQUAL')
    st.depth_mask_set(True)
    st.face_culling_set('NONE')
    st.blend_set('NONE')
    batch.draw(sh)
    st.depth_mask_set(False)
    st.depth_test_set('NONE')
    return n


def _viewport_draw():
    ctx = bpy.context
    scene = ctx.scene
    if scene is None or not PLANES:
        return
    from . import gamerender
    if scene.render.engine == gamerender.ENGINE_ID:
        sp = ctx.space_data
        if sp is not None and getattr(sp.shading, 'type', '') == 'RENDERED':
            return              # drawn by the game renderer itself
    rv3d = ctx.region_data
    if rv3d is None:
        return
    try:
        draw(scene, rv3d.perspective_matrix, rv3d.view_matrix, 1.0)
    except Exception as e:
        print('Cars 2 crowd: not drawn (%s)' % e)


def ensure_handler():
    if _handler[0] is None:
        _handler[0] = bpy.types.SpaceView3D.draw_handler_add(_viewport_draw, (), 'WINDOW', 'POST_VIEW')


def draw_panel(layout, context):
    scene = context.scene
    if not scene.get('c2_crowd_planes'):
        return
    box = layout.box()
    row = box.row()
    row.label(text='Crowds: %d planes' % scene['c2_crowd_planes'], icon='COMMUNITY')
    row.prop(scene, '["c2_show_crowd"]', text='Show')
    o = context.object
    if o is None or not o.c2_crowd.enabled:
        return
    c = o.c2_crowd
    box.label(text=c.plane, icon='OUTLINER_OB_POINTCLOUD')
    col = box.column(align=True)
    col.prop(c, 'cols')
    col.prop(c, 'rows')
    col.prop(c, 'width')
    col.prop(c, 'spacing')
    if list(o.get('c2_crowd0', [])) != _values(c):
        box.operator('c2.crowd_reset', icon='LOOP_BACK')
    box.label(text='Move or turn this locator to move the crowd', icon='INFO')


def _update(self, context):
    _members.clear()
    from . import gamerender
    gamerender.RENDERER.version += 1          # redraw with the new rows


class C2CrowdPlane(bpy.types.PropertyGroup):
    """A CrowdMemberPlane node's settings, kept on its locator (exported back to the node)."""
    enabled: bpy.props.BoolProperty(default=False)
    plane: bpy.props.StringProperty(name='Plane')
    uuid: bpy.props.StringProperty()
    width: bpy.props.FloatProperty(name='Row Width', min=0.0, soft_max=200.0, unit='LENGTH', update=_update,
                                   description='Width the cars of a row spread over (centred on the locator)')
    spacing: bpy.props.FloatProperty(name='Row Spacing', min=0.0, soft_max=50.0, unit='LENGTH', update=_update,
                                     description='Distance between rows (each row further back)')
    rows: bpy.props.IntProperty(name='Number of Rows', min=1, soft_max=20, update=_update,
                                description='Rows of cars; odd rows have one car fewer and are staggered')
    cols: bpy.props.IntProperty(name='Crowd per Row', min=1, soft_max=40, update=_update,
                                description='Cars in the front row')


class C2_OT_crowd_reset(bpy.types.Operator):
    """Put this crowd's rows back as imported"""
    bl_idname = 'c2.crowd_reset'
    bl_label = 'Reset Rows'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        o = context.object
        v = list(o.get('c2_crowd0', [])) if o is not None else []
        if len(v) != 4:
            return {'CANCELLED'}
        c = o.c2_crowd
        c.width, c.spacing, c.rows, c.cols = float(v[0]), float(v[1]), int(v[2]), int(v[3])
        return {'FINISHED'}


def register():
    bpy.utils.register_class(C2CrowdPlane)
    bpy.utils.register_class(C2_OT_crowd_reset)
    bpy.types.Object.c2_crowd = bpy.props.PointerProperty(type=C2CrowdPlane)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undo not in h:
            h.append(_undo)


def unregister():
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undo in h:
            h.remove(_undo)
    if _handler[0] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler[0], 'WINDOW')
        _handler[0] = None
    PLANES.clear()
    _tex.clear()
    del bpy.types.Object.c2_crowd
    bpy.utils.unregister_class(C2_OT_crowd_reset)
    bpy.utils.unregister_class(C2CrowdPlane)
