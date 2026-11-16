"""Choreography preview: load a choreography package into the scene and bake
its animation onto the timeline, placed at its anchor locator.

Preview objects live in their own collection, outside the world root, and are
never exported. Clear Preview removes them and restores the frame range."""
import math
import os

import bpy
import numpy as np
from bpy.props import BoolProperty, IntProperty
from mathutils import Matrix, Quaternion, Vector

import choreo
import octscene
from . import charrig, common
from .common import g2b

PREVIEW_PREFIX = 'C2 Preview: '
CAR_PROXY_SIZE = (4.3, 1.4, 1.9)   # length, height, width (game units ~ metres)


# ----------------------------------------------------------------------
# fcurve baking (works with the legacy and the slotted-action API)
# ----------------------------------------------------------------------

def _fcurves(idblock):
    """The F-curves of an ID's action, whichever animation API this Blender has
    (older: action.fcurves; 4.4+: layered actions with slots/channel bags).
    """
    ad = idblock.animation_data
    if ad is None or ad.action is None:
        return None
    act = ad.action
    if hasattr(act, 'fcurves') and (not hasattr(act, 'layers') or not len(act.layers)):
        return act.fcurves
    try:
        slot = ad.action_slot
        for layer in act.layers:
            for strip in layer.strips:
                cb = strip.channelbag(slot)
                if cb is not None:
                    return cb.fcurves
    except AttributeError:
        pass
    return act.fcurves


def bake(idblock, data_path, index, frames, values, constant=False):
    """Keys `values` at `frames` on idblock.data_path[index] in one go."""
    if len(frames) == 0:
        return
    vals = np.asarray(values, np.float64)
    # a value that never changes needs one key, not thousands
    if np.allclose(vals, vals[0], atol=1e-7):
        frames, vals = frames[:1], vals[:1]
    # insert the first key the normal way (creates the action/F-curve), then
    # add the rest in one bulk foreach_set - much faster than per-key inserts
    path_owner = idblock
    attr = data_path
    if '.' in data_path or '[' in data_path:
        path_owner = idblock.path_resolve(data_path.rsplit('.', 1)[0])
        attr = data_path.rsplit('.', 1)[1]
    cur = getattr(path_owner, attr)
    if index >= 0:
        cur[index] = float(vals[0])
    else:
        setattr(path_owner, attr, type(cur)(vals[0]) if not isinstance(cur, bool) else bool(vals[0] > 0.5))
    idblock.keyframe_insert(data_path, index=index, frame=float(frames[0]))
    fcs = _fcurves(idblock)
    fc = next((f for f in fcs if f.data_path == data_path and (index < 0 or f.array_index == index)), None)
    if fc is None:
        return
    if len(frames) > 1:
        fc.keyframe_points.add(len(frames) - 1)
        co = np.empty(len(frames) * 2, np.float32)
        co[0::2] = frames
        co[1::2] = vals
        fc.keyframe_points.foreach_set('co', co)
    interp = 'CONSTANT' if constant else 'LINEAR'
    for kp in fc.keyframe_points:
        kp.interpolation = interp
    fc.update()


def bake_matrices(obj, frames, mats, pose_bone=None):
    """Key loc / quaternion / scale from a list of Blender matrices."""
    locs, quats, scales = [], [], []
    prev = None
    for m in mats:
        l, q, s = m.decompose()
        # q and -q are the same rotation; keep neighbours on the same side so
        # the interpolation between keys doesn't spin the long way round
        if prev is not None and prev.dot(q) < 0:
            q.negate()
        prev = q
        locs.append(l)
        quats.append(q)
        scales.append(s)
    target = pose_bone if pose_bone is not None else obj
    target.rotation_mode = 'QUATERNION'
    base = 'pose.bones["%s"].' % pose_bone.name if pose_bone is not None else ''
    for k in range(3):
        bake(obj, base + 'location', k, frames, [v[k] for v in locs])
        bake(obj, base + 'scale', k, frames, [v[k] for v in scales])
    for k in range(4):
        bake(obj, base + 'rotation_quaternion', k, frames, [q[k] for q in quats])


def _np2m(a):
    """NumPy 4x4 -> mathutils Matrix."""
    return Matrix([list(r) for r in a])


def _ortho(a):
    """4x4 column matrix with its rotation part orthonormalised (scale dropped)."""
    m = np.array(a, np.float64)
    r = m[:3, :3]
    # nearest pure rotation via SVD (Blender bones can't hold scale/shear)
    u, _, vt = np.linalg.svd(r)
    q = u @ vt
    if np.linalg.det(q) < 0:
        u[:, -1] *= -1
        q = u @ vt
    out = np.eye(4)
    out[:3, :3] = q
    out[:3, 3] = m[:3, 3]
    return out


# ----------------------------------------------------------------------
# building
# ----------------------------------------------------------------------

def _extract_images(sc, folder, files=None):
    """Write a package's textures to `folder` (once) and load the 2D ones.
    -> {texture index: bpy image}; `files` (if given) gets {index: path} for all.
    """
    images = {}
    for i, tx in enumerate(sc.textures):
        data = tx.get('Data', b'')
        if not data:
            continue
        src = tx.get('SourceFilePath', tx.get('Name', 'tex%d' % i)).replace('\\', '/')
        rel = os.path.splitext(src)[0] + ('.dds' if data[:4] == b'DDS ' else os.path.splitext(src)[1])
        path = os.path.join(folder, rel)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
        if files is not None:
            files[i] = path
        if tx.get('Type', '2d') != '2d':
            continue  # cube maps: only the game shaders read them (as panoramas)
        try:
            images[i] = bpy.data.images.load(path, check_existing=True)
        except RuntimeError:
            pass
    return images


def _materials(sc, cache):
    """Materials of a choreography / character package. In an Arcade world
    they get the game shaders too (fog and environment from the world)."""
    from .importer import import_materials
    from . import fxnodes, matnodes
    files = {}
    images = _extract_images(sc, cache, files)
    scene = bpy.context.scene
    world = fxnodes.game_context(scene)
    game = None
    if world is not None:
        # a package may ship its own effects (effects/*.tfx inside its zip):
        # those take precedence over the game's
        local = {}
        for name, data in (sc.files or {}).items():
            n = name.replace('\\', '/').lower()
            if n.endswith('.tfx') and '/effects/' in '/' + n:
                local[os.path.splitext(os.path.basename(n))[0]] = data
        game = matnodes.GameContext(world.assets, fxnodes.Textures(images, files, {}, os.path.join(cache, 'equirect'),
                                                                   parent=world.textures), local)
    fxnodes.SUSPEND[0] += 1
    try:
        ms = import_materials(sc, images, True if scene.c2.platform == 'ARCADE' else None, game)
        if game is not None:   # the Cars 2 Game renderer needs the package's textures and effects
            from . import gamerender
            gamerender.register_package(list(ms), game)
        return ms
    finally:
        fxnodes.SUSPEND[0] -= 1


def _link(col, obj):
    """Put obj in the preview collection and tag it as a preview object
    (the exporter and clear_preview recognise the tag).
    """
    col.objects.link(obj)
    obj['c2_preview'] = True
    return obj


def _car_proxy(name):
    """A car-sized box mesh, shown when a character model can't be found."""
    import bmesh
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    L, H, W = CAR_PROXY_SIZE
    for v in bm.verts:
        v.co = Vector((v.co.x * W, v.co.y * H + H / 2, v.co.z * L))  # game: Y up, Z forward
    bm.to_mesh(me)
    bm.free()
    return me


def _character_model(assets, source, col, parent, cache, report):
    """Meshes of a referenced character under `parent` (rest pose).
    Returns (character scene, {node index: object}) or None."""
    pkg = choreo.find_character(assets, source)
    if pkg is None:
        report.append('character "%s" not found in assets/characters - showing a box' % source)
        return None
    try:
        sc = pkg.scene()
    except Exception as e:
        report.append('character %s: %s' % (source, e))
        return None
    from .importer import build_geometry_mesh
    mats = _materials(sc, os.path.join(cache, 'characters', pkg.stem))
    objs = {}
    for i, node in enumerate(sc.nodes):
        if octscene.is_subgeo(node) or not sc.subgeos(i):
            continue  # meshes hang under Geometry or plain Transform nodes (bodyMesh_...)
        me = build_geometry_mesh(sc, i, '%s_%s' % (pkg.stem, node.get('NodeName', i)), mats)
        if me is None:
            continue
        o = _link(col, bpy.data.objects.new(me.name, me))
        o.parent = parent
        o.matrix_parent_inverse = Matrix.Identity(4)
        o.matrix_basis = g2b(list(sc.world(i).ravel()))  # rest pose, row-vector world
        objs[i] = o
    if not objs:
        report.append('character "%s": no meshes - showing a box' % source)
        return None
    return sc, objs


def _layer_pose(ch, layer, csc, rig, frames_t, report, nm):
    """Bone pose matrices per frame for a layer clip played into a character
    rig: {bone node: [Matrix]} (see choreo.character_layer_targets)."""
    arm, names, rest_bone, bones, world_of, static_local = rig
    targets = choreo.character_layer_targets(csc, layer['template'])
    if not targets:
        report.append('%s: no connection for layer %s' % (nm, layer['name']))
        return {}
    by_name = {n.get('NodeName'): i for i, n in bones.items()}
    # clip slot -> bone: the target path ends in '<Bone>/Transform',
    # 'Control<Bone>/Transform' or '<Bone>_Ctrl/Transform' (a control drives
    # the bone of the same name)
    slot_bone = {}
    for slot, path in targets.items():
        parts = path.split('/')
        if len(parts) >= 2 and parts[-1] == 'Transform':
            b = parts[-2]
            if b.startswith('Control') and b[len('Control'):] in by_name:
                b = b[len('Control'):]
            elif b.endswith('_Ctrl') and b[:-len('_Ctrl')] in by_name:
                b = b[:-len('_Ctrl')]
            if b in by_name:
                slot_bone[slot] = by_name[b]
    clip = layer['clip']
    shown = {s for s in clip.animated_targets() if s in slot_bone}
    skipped = [targets[s] for s in clip.animated_targets() if s not in slot_bone and s in targets]
    if skipped:
        report.append('%s: %d face/eye channels not shown (%s...)' % (nm, len(skipped), skipped[0]))
    if not shown:
        return {}
    out = {bi: [] for bi in bones}
    for t in frames_t:
        r = clip.evaluate(min(t, clip.end))
        delta = {}
        for s in shown:
            o = r.get(s, {})
            loc = [v or 0.0 for v in o.get('loc', [0.0, 0.0, 0.0])]
            R = choreo.euler_mat([v or 0.0 for v in o['euler']], o.get('order', 'ZXY')) if 'euler' in o else np.eye(3)
            delta[slot_bone[s]] = (loc, R)

        def anim_local(i):
            """Rest local of bone i with this frame's layer-clip offset applied."""
            L = static_local(i)
            if i in delta:
                loc, R = delta[i]
                L = L.copy()
                L[:3, :3] = R @ L[:3, :3]
                L[:3, 3] = L[:3, 3] + np.array(loc)
            return L
        memo = {}
        cur = {bi: world_of(bi, anim_local, memo) for bi in bones}
        # Blender pose bones are keyed relative to their rest pose (and their
        # parent's), so convert each animated model-space matrix into that basis
        for bi in bones:
            p = csc.parent(bi)
            if p in bones:
                basis = np.linalg.inv(np.linalg.inv(rest_bone[p]) @ rest_bone[bi]) @ np.linalg.inv(cur[p]) @ cur[bi]
            else:
                basis = np.linalg.inv(rest_bone[bi]) @ cur[bi]
            out[bi].append(_np2m(basis))
    return out


def _rig(context, col, parent, sc, objs, name, bind_pose=False, armature=True):
    """Armature for the Bone nodes of scene `sc` (rest pose in sc's model
    space, child of `parent`) with the skinned meshes in objs ({node index:
    object}) bound to it. Returns (arm or None, bone names, rest matrices,
    bones, world_of, static_local). armature=False: no armature, the skinned
    meshes are only posed at rest (static models)."""
    bones = {i: n for i, n in enumerate(sc.nodes) if n.get('Type') == 'Bone'}
    rest_model, rest_bone = {}, {}

    def world_of(i, local_of, memo):
        """Column-vector matrix of node i relative to the choreography root."""
        if i in memo:
            return memo[i]
        p = sc.parent(i)
        m = local_of(i)
        if p is not None and p != 0:
            m = world_of(p, local_of, memo) @ m
        memo[i] = m
        return m

    def static_local(i):
        """Node i's stored (rest) local matrix, column-vector form."""
        return np.array(sc.nodes[i].get('LocalToParentMatrix', octscene.IDENTITY16)).reshape(4, 4).T

    memo0 = {}
    for bi in bones:
        rest_model[bi] = world_of(bi, static_local, memo0)
        rest_bone[bi] = _ortho(rest_model[bi])
    arm = None
    names = {}
    if bones and not armature:
        names = {bi: sc.nodes[bi].get('NodeName', 'b%d' % bi) for bi in bones}
    elif bones:
        ad = bpy.data.armatures.new(name)
        arm = _link(col, bpy.data.objects.new(ad.name, ad))
        arm.parent = parent
        arm.matrix_parent_inverse = Matrix.Identity(4)
        arm.show_in_front = True
        for o in context.selected_objects:
            o.select_set(False)
        context.view_layer.objects.active = arm
        arm.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        ebs = {}
        for bi in sorted(bones):
            eb = ad.edit_bones.new(sc.nodes[bi].get('NodeName', 'b%d' % bi))
            eb.head = (0, 0, 0)
            eb.tail = (0, 0.25, 0)
            eb.matrix = _np2m(rest_bone[bi])
            ebs[bi] = eb
        for bi in bones:
            p = sc.parent(bi)
            if p in ebs:
                ebs[bi].parent = ebs[p]
        names = {bi: ebs[bi].name for bi in bones}  # edit bones are freed by the mode switch
        bpy.ops.object.mode_set(mode='OBJECT')

    # bind skinned parts: the vertices are stored relative to their bones, so
    # move them to the rest pose (weighted sum over up to 4 bones) and add
    # vertex groups + an Armature modifier. A part's RemapData maps the
    # vertex BoneIndices to bone IDs.
    remap = list(sc.tree.get('SceneComponentPool', {}).values())
    by_boneid = {n.get('BoneID'): i for i, n in bones.items()}
    for i, o in list(objs.items()):
        if o.type != 'MESH':
            continue
        pools, parts = sc.geometry_parts(i)
        if not any('BoneIndices' in p['attrs'] for p in pools):
            continue
        if arm is None and armature:
            continue
        ao = arm
        co = np.concatenate([p['attrs']['Position'][:, :3] for p in pools]).astype(np.float64)
        bidx = np.concatenate([p['attrs'].get('BoneIndices', np.zeros((p['count'], 4))) for p in pools]).astype(int)
        bw = np.concatenate([p['attrs'].get('BlendWeights', np.zeros((p['count'], 4))) for p in pools]).astype(np.float64)
        if bw.max() > 1.5:
            bw = bw / 255.0
        # vertex BoneIndices -> bone nodes: each part (SubGeometry) has its own
        # palette (the RemapData of its scene component), so a vertex uses the
        # palette of the part that draws it
        offs = np.cumsum([0] + [p['count'] for p in pools])
        node_of = np.full(bidx.shape, -1, np.int64)
        for part in parts:
            comp = sc.nodes[part['node']].get('SceneComponentReferences') or []
            rd = remap[comp[0]].get('RemapData', []) if comp and comp[0] < len(remap) else []
            pal = np.array([by_boneid.get(b, -1) if by_boneid.get(b) is not None else -1 for b in rd] or [-1])
            vs = np.unique(np.asarray(part['indices'], np.int64)) + offs[part['pool']]
            vs = vs[vs < len(co)]
            b = bidx[vs]
            node_of[vs] = np.where(b < len(pal), pal[np.clip(b, 0, len(pal) - 1)], -1)
        out = np.zeros_like(co)
        hom = np.hstack([co, np.ones((len(co), 1))])
        groups = {}
        for k in range(4):
            for bn in np.unique(node_of[:, k]).tolist():
                sel = (node_of[:, k] == bn) & (bw[:, k] > 0)
                if not sel.any() or bn < 0:
                    continue
                b2m = np.array(sc.nodes[bn].get('BoneToModelMatrix', octscene.IDENTITY16)).reshape(4, 4).T
                # bind_pose: vertices where the bones' BoneToModel puts them (a rig
                # resting at the bind pose deforms them); else moved to the scene rest
                M = np.eye(4) if bind_pose else rest_bone[bn] @ np.linalg.inv(b2m)
                out[sel] += (hom[sel] @ M.T)[:, :3] * bw[sel, k:k + 1]
                vg = groups.get(bn) or o.vertex_groups.new(name=names[bn])
                groups[bn] = vg
                # one add per distinct weight (weights are bytes / 255: at most 256),
                # not one per vertex (most of a gate's build time)
                vis, ws = np.nonzero(sel)[0], bw[sel, k]
                for w in np.unique(ws).tolist():
                    vg.add(vis[ws == w].tolist(), w, 'ADD')
        o.data.vertices.foreach_set('co', out.astype(np.float32).ravel())
        o.data.update()
        if ao is None:
            continue
        o.parent = ao
        o.matrix_parent_inverse = Matrix.Identity(4)
        o.matrix_basis = Matrix.Identity(4)
        mod = o.modifiers.new('Armature', 'ARMATURE')
        mod.object = ao

    return arm, names, rest_bone, bones, world_of, static_local


def _no_scale(m):
    """m without its scale. A choreography anchor is a Transform DataNode
    (translation + orientation): the game drops the scale of what it is placed
    on. radiator_b's tumbleweeds sit on a 5x locator at the origin but roll by
    the track at their own size; the scaled gate / window / yacht locators
    are only sized for the level editor."""
    loc, rot, _scale = m.decompose()
    return Matrix.LocRotScale(loc, rot, None)


def _anchor(m):
    """Where a choreography plays for anchor matrix m. A uniform scale is the
    level editor's gizmo size and is dropped (_no_scale); a non-uniform one is
    a gate's width and is kept: every race-gate locator is stretched along X
    only (tokyo LOC_Gate_Banner_01 1.99, italy StartGateA1Scale 1.66, oilrig
    LOC_FinishGate_B 2.94), sizing the 30 m HoloGate_Elements to the road."""
    s = m.to_scale()
    if max(s) - min(s) <= 1e-3 * max(1.0, max(s)):
        return _no_scale(m)
    return m.copy()


def _take_anchor(ch):
    """Make the package's anchor node neutral and return its authored
    transform (Blender local to the world root). The preview holder stands for
    the anchor; keeping the anchor's own offset too would place everything
    twice (crane: locator + authored offset)."""
    name = ch.anchor_name
    sc = ch.scene
    ai = next((i for i, n in enumerate(sc.nodes) if name and n.get('NodeName') == name), None)
    if ai is None:      # no PrimaryAnchor association: the first top-level ANC transform
        ai = next((i for i, n in enumerate(sc.nodes) if sc.parent(i) == 0 and n.get('Type') == 'Transform'
                   and n.get('NodeName', '').upper().startswith('ANC')), None)
    if ai is None:
        return Matrix.Identity(4)
    n = sc.nodes[ai]
    authored = g2b(n.get('LocalToParentMatrix', octscene.IDENTITY16))
    n['LocalToParentMatrix'] = list(octscene.IDENTITY16)
    if hasattr(sc, '_world'):
        sc._world.clear()
    di = ch.bind.get(ai)
    if di is None:
        di = ch.path_index.get(name.replace('|', '/') + '/Transform')
    if di is not None and isinstance(ch.dn[di].get('Data'), dict):
        ch.dn[di]['Data'] = {k: v for k, v in ch.dn[di]['Data'].items()
                             if k not in ('Translation', 'Orientation#Rotor', 'Orientation#OrientProxy')}
    return authored


def _player_start(scene, route=''):
    """Where 'a racer' stands for the preview: pole position of the route's
    starting grid, else its start locator, else the first player car / grid
    slot / arena or hunter start."""
    objs = [o for o in scene.objects if not o.get('c2_preview')]
    by = {o.get('c2_name', o.name): o for o in objs}
    names = {o.name: o for o in objs}
    if route and len(route) == 1:
        for n in ('Starting Grid%s slot 1' % route, 'LOC_%sStart' % route, 'LOC_Start%s' % route,
                  'LOC_Starting_Grid_%s' % route):
            if n in names:
                return names[n]
            if n in by:
                return by[n]
    for n in ('PlayerCar1', 'PlayerCar01', 'PlayerCar'):
        if n in by:
            return by[n]
    import re
    for pat in (r'Starting Grid.*slot 1$', r'^LOC_Start', r'^LOC_[A-C]Start$', r'(?i)player.*(arena|hunter|start).*0?1$'):
        o = next((o for o in sorted(objs, key=lambda x: x.name) if re.search(pat, o.name)), None)
        if o is not None:
            return o
    return None


def _route_mode(scene):
    """(route, mode) the preview plays: the Scene panel's Track / Mode, or the
    first route in Race (a mission world: its first mode) when Track is All."""
    import json
    try:
        places = json.loads(scene.get('c2_choreo_places', '{}'))
    except ValueError:
        places = {}
    if not places:
        return '', '', {}
    route = scene.c2.track_variant if scene.c2.track_variant in places else sorted(places)[0]
    modes = places[route]
    mode = scene.c2.track_mode if scene.c2.track_variant in places and scene.c2.track_mode in modes else         ('Race' if 'Race' in modes else next(iter(modes)))
    return route, mode, modes.get(mode, {})


def _played_at(scene, item, report):
    """Objects the game plays this choreography at, for the previewed route and
    mode (core/choreoplace.py): [object, ...], empty when unknown."""
    route, mode, plays = _route_mode(scene)
    names = plays.get(str(item.node_index))
    if not names:
        return []
    out, seen = [], set()
    for nm in names:
        if nm is None or nm in seen:
            continue
        seen.add(nm)
        if nm == '<player>':
            obj = _player_start(scene, route)
            if obj is not None:
                report.append('played at a racer: shown at %s' % obj.name)
        else:
            obj = next((o for o in scene.objects if o.get('c2_name', o.name) == nm and not o.get('c2_preview')), None)
            if obj is None:
                report.append('anchor %s not found in the scene' % nm)
        if obj is not None and obj not in out:
            out.append(obj)
    if out and route:
        report.append('track %s, %s' % (route, mode) if len(route) == 1 else mode)
    return out


_graphs = {}        # world .mer (path, mtime, size) -> meridian.Graph


def _world_graph(scene):
    """The world's Meridian graph, decoded once per file version (decoding
    tokyo_b's .mer takes ~0.3 s; every preview needs it). Read-only."""
    import c2oct
    import meridian
    path = os.path.splitext(bpy.path.abspath(scene.c2.oct_path))[0] + '.mer'
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _graphs:
        try:
            with open(path, 'rb') as f:
                g = meridian.Graph(c2oct.decode(f.read())[0])
        except (OSError, ValueError):
            return None
        _graphs.clear()
        _graphs[key] = g
    return _graphs[key]


def _graph_anchor(scene, item, report):
    """The object an anchor resolves to through the Meridian graph
    (core/anchors.py): a locator / actor, a list or variable's first entry, or
    the player's start for 'a racer'. None when it can't be resolved."""
    import anchors
    import c2oct
    import meridian
    g = _world_graph(scene)
    if g is None:
        return None
    r = anchors.Resolver(g)
    for ref, aname, src in g.choreo_anchors(item.node_index):
        res = r.resolve(src) if src is not None else None
        if res is None:
            continue
        kind, what = res
        if kind == 'object':
            obj = next((o for o in scene.objects if o.get('c2_name', o.name) == what and not o.get('c2_preview')), None)
            if obj is not None:
                return obj
            report.append('anchor %s not found in the scene' % what)
        elif kind == 'player':
            obj = _player_start(scene)
            if obj is not None:
                report.append('anchored to a racer (%s): shown at the player start %s' % (what, obj.name))
                return obj
    return None


def _actor_anchor(scene, item, report):
    """Anchor on an Actor Transform: (actor object, None), or (None, PathMotion)
    when the actor has a spline follower (the dust devils ride CRV curves);
    None when it can't be resolved."""
    import c2oct
    import meridian
    g = _world_graph(scene)
    if g is None:
        return None
    # follow the anchor's TransformFetch back to an ActorTransform, then to
    # the actor's NodeGroup (its name is the .ap section / scene object)
    for ref, aname, src in g.choreo_anchors(item.node_index):
        owner = g.locator_of_transform_source(src) if src is not None else None
        if owner is None or g.nodes[owner].get('Type') != 'ActorTransform':
            continue
        fetch = g.nodes[owner].get('Actor')
        origins = g.link_sources(fetch) if fetch is not None else []
        grp = g.nodes[origins[0]].get('Parent') if origins else None
        actor = g.nodes[grp].get('AuthoredName', '') if grp is not None else ''
        if not actor:
            continue
        obj = next((o for o in bpy.data.objects if o.get('c2_name', o.name) == actor and not o.get('c2_preview')), None)
        a = next((x for x in scene.c2.actors if x.name == actor), None)
        kv = {k.key: k.value for k in a.kvs} if a is not None else {}
        # an actor with a spline follower component moves along a .dbl path:
        # read its start %, speeds and the spline name from the follower's group
        from .objplace import follower as follower_of
        vals = follower_of(g, kv.get('SplineFollowerMeridianComponent_Uuid', ''))
        if vals is not None:
            spline = vals['splines'][0] if vals['splines'] else None
            pts = None
            if spline:
                try:
                    with open(base + '.dbl', 'rb') as f:
                        pts = choreo.dbl_spline(f.read(), spline)
                except OSError:
                    pass
            if pts is not None:
                pts, closed = pts
                motion = choreo.PathMotion(pts, vals.get('PctOnPath', 0.0), vals.get('InitialSpeed', 0.0),
                                           vals.get('MaxSpeed', 0.0), vals.get('Acceleration', 0.0), closed)
                report.append('anchored to actor %s, moving along %sspline %s (%.0f m at %.1f m/s)' % (
                    actor, 'closed ' if closed else '', spline, motion.length, vals.get('MaxSpeed', 0.0)))
                return None, motion
            report.append('actor %s: spline %s not found - placed at the actor' % (actor, spline))
        if obj is not None:
            return obj, None
        report.append('anchor actor %s not found in the world - placed at the origin' % actor)
    return None


def build_preview(context, item, look_through=True, max_frames=4000, keep=False, _copy=None):
    """Load the choreography of list item `item` and bake it onto the timeline.

    Steps:
     1. find the package, evaluate its network (choreo.Choreography)
     2. build its scene (meshes, cameras, empties) under a holder empty placed
        at the anchor locator (or the anchoring actor / its spline)
     3. build armatures for bones and for animated characters
     4. sample every frame and key transforms, bones, visibility, focal
        length and material values
    Returns (choreography, report lines). max_frames limits the key count.
    keep: leave the previews already built (Preview All Play-on-Start).
    _copy: (anchor object, number) - build one more copy of the preview there
    (a place the logic also plays it at whose scale an instance can't carry).
    """
    scene = context.scene
    root = scene.c2.root
    report = []
    assets = common.game_assets(scene)
    if not assets or not os.path.isdir(os.path.join(assets, 'choreographies')):
        raise RuntimeError('Set the Game Assets Folder (Meridian panel): the folder that contains choreographies/')
    pkg = choreo.find_choreography(assets, item.choreography_name)
    if pkg is None:
        raise RuntimeError('Choreography "%s" not found in %s (folder or .zip)'
                           % (item.choreography_name, os.path.join(assets, 'choreographies')))
    ch = choreo.Choreography(pkg)
    report += ch.warnings
    authored = _take_anchor(ch)
    if not keep:
        clear_preview(context)
    tag = item.authored if _copy is None else '%s_%d' % (item.authored, _copy[1])
    col = bpy.data.collections.new(PREVIEW_PREFIX + tag)
    scene.collection.children.link(col)
    col['c2_preview'] = True
    cache = os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)),
                         '%s.oct.textures' % scene.c2.world_name, 'choreographies', pkg.stem)

    # placement: the first anchor's locator, else the world origin
    holder = _link(col, bpy.data.objects.new('PREVIEW_' + tag, None))
    holder.empty_display_type = 'ARROWS'
    holder.empty_display_size = 2.0
    # The game puts the package's anchor (ANC_xxx_00) at the world transform its
    # ChoreographyTransformReference fetches: a locator, an actor (or the spline
    # it follows), a variable / list filled at run time, the racer that fired it.
    # Without one it plays where the package was authored (its anchor's own
    # transform, in world space). The anchor node itself is neutral (_take_anchor).
    # The run-time placement comes first (core/choreoplace.py: the world's logic
    # replayed for the previewed route and mode); an actor riding a spline
    # moves the anchor along it; then static tracing; else as authored.
    anchor = None
    motion = None
    if _copy is None and not any(a.locator is not None for a in item.anchors):
        res = _actor_anchor(scene, item, report)
        if res is not None:
            anchor, motion = res
    extra = []
    if _copy is not None:
        anchor = _copy[0]
    elif motion is None:
        played = _played_at(scene, item, report)
        if played:
            anchor, extra = played[0], played[1:]
    if anchor is None and motion is None:
        anchor = next((a.locator for a in item.anchors if a.locator is not None), None)
    if anchor is None and motion is None:
        anchor = _graph_anchor(scene, item, report)
    if motion is not None:
        holder.matrix_world = root.matrix_world @ _np2m(motion.matrix(0.0))
    elif anchor is not None:
        holder.matrix_world = _anchor(anchor.matrix_world)
        report.append('placed at %s' % anchor.get('c2_name', anchor.name))
    else:
        holder.matrix_world = root.matrix_world @ _no_scale(authored)
        if item.anchors:
            report.append('anchor not found in the world: played where the package was authored')
    holder['c2_preview_frames'] = (scene.frame_start, scene.frame_end)
    holder['c2_preview_camera'] = scene.camera.name if scene.camera else ''

    # build the choreography's own scene: meshes, cameras, plain transforms
    from .importer import build_geometry_mesh, make_camera
    sc = ch.scene
    mats = _materials(sc, cache)
    objs = {0: holder}
    bones = {}
    amb_by_mat = {}         # MaterialPool index -> light volume objects (their clip keys)
    for i, n in enumerate(sc.nodes):
        t = n.get('Type', '')
        if i == 0 or octscene.is_subgeo(n):
            continue
        name = n.get('NodeName', '%s%d' % (t, i))
        if t == 'Bone':
            bones[i] = n
            continue
        if t == 'Camera':
            o = bpy.data.objects.new(name, make_camera(n, name))
        elif sc.subgeos(i):
            me = build_geometry_mesh(sc, i, name, mats)
            o = bpy.data.objects.new(name, me)
            subs = sc.subgeos(i)
            effects = {sc.materials[sc.nodes[j]['MaterialReference']].get('Effect')
                       for j in subs if 'MaterialReference' in sc.nodes[j]
                       and sc.nodes[j]['MaterialReference'] < len(sc.materials)}
            if me is not None and (all(sc.nodes[j].get('Type') == 'SubGeometryAmbient' for j in subs) or
                                   (effects and effects <= {'SolidColor', 'shadowonly'}) or
                                   not n.get('Visible', 1)):
                # additive light volumes, trigger / collision volumes (SolidColor:
                # no Standard technique) and hidden parts: the game never draws them
                o.display_type = 'WIRE'
                o.hide_render = True
                amb = [j for j in subs if sc.nodes[j].get('Type') == 'SubGeometryAmbient']
                if amb:
                    # a light volume (ambientvol.py): it lights the world, its mesh isn't drawn
                    from . import ambientvol
                    mref = sc.nodes[amb[0]].get('MaterialReference', 0)
                    ambientvol.fill(o, sc.nodes[amb[0]], sc.materials[mref] if mref < len(sc.materials) else {})
                    amb_by_mat.setdefault(mref, []).append(o)
                if not (all(sc.nodes[j].get('Type') == 'SubGeometryAmbient' for j in subs) or
                        (effects and effects <= {'SolidColor', 'shadowonly'})):
                    o['c2_hidden_node'] = 1     # drawn after all when the clip keys it visible
        else:
            o = bpy.data.objects.new(name, None)
            o.empty_display_type = 'PLAIN_AXES'
            o.empty_display_size = 0.5
        objs[i] = _link(col, o)
        if o.type == 'MESH' and o.c2_ambient.enabled:
            o.hide_set(True)        # a light volume's sphere is huge: unhide it to see / edit its reach
    for i, o in objs.items():
        if i == 0:
            continue
        p = sc.parent(i)
        o.parent = objs.get(p, holder) if p is not None else holder
        o.matrix_parent_inverse = Matrix.Identity(4)
        o.matrix_basis = g2b(sc.nodes[i].get('LocalToParentMatrix', octscene.IDENTITY16))

    arm, names, rest_bone, bones, world_of, static_local = _rig(context, col, holder, sc, objs, 'RIG_' + item.authored)
    # particle effects started by the package's logic, at its LocPrtcl_* locators
    from . import particles
    try:
        particles.attach(context, ch, pkg, objs, item, report)
    except Exception as e:      # a preview without its particles beats no preview
        report.append('particles skipped: %s' % e)

    # characters riding their anchors
    char_objs = []
    char_rigs = []
    tk_rigs = []
    no_toolkit = []
    for ref, di in ch.character_anchors():
        if di is None:
            continue  # e.g. a camera-blocking reference to the world itself, not a placed character
        nm = ref.get('Namespace') or ref.get('SourceFile', 'Character')
        co_ = _link(col, bpy.data.objects.new('CHAR_' + nm, None))
        co_.empty_display_type = 'SINGLE_ARROW'
        co_.empty_display_size = 2.0
        co_.parent = holder
        # characters ride a KinematicState (OffsetKinematicStateUpdate): position
        # and orientation only, so the anchor's scale never reaches them (Italy's
        # yachts sit on 2x / 4x scaled locators but sail at their own size)
        hs = holder.matrix_world.to_scale()
        co_.matrix_parent_inverse = Matrix.Diagonal(
            [1.0 / v if abs(v) > 1e-9 else 1.0 for v in hs] + [1.0])
        model = _character_model(assets, ref.get('SourceFile', ''), col, co_, cache, report)
        rig = None
        if model is None:
            px = _link(col, bpy.data.objects.new('CAR_' + nm, _car_proxy('CAR_' + nm)))
            px.parent = co_
            px.display_type = 'WIRE'
        elif ch.layer_for(ref.get('Namespace', '')) is not None:
            # the clip drives the character's control rig (body, suspension,
            # wheels, face, eyes): the Cars 2 Toolkit's rebuild of it, when installed
            tk = charrig.build(context, choreo.find_character(assets, ref.get('SourceFile', '')), col, co_,
                               report, nm)
            # the choreography animates this character's rig: skin it to an armature.
            # The toolkit's bones rest at the bind pose (BoneToModelMatrix), which for
            # cars sits higher than the scene's rest (LocalToParentMatrix: wheel
            # centres at the ground), so its meshes keep their bind-pose vertices
            rig = _rig(context, col, co_, model[0], model[1], 'RIG_' + nm, bind_pose=tk is not None)
            if tk is not None and rig[0] is None:
                bpy.data.objects.remove(tk[0])
                tk = None
            if tk is not None:
                charrig.skin(model[1], tk[0], rig[0])
                tk_rigs.append((nm, ch.layer_for(ref.get('Namespace', '')), tk[0], tk[1], model[1]))
                rig = None
            else:
                no_toolkit.append(nm)
        char_objs.append((co_, di))
        if rig is not None and rig[0] is not None:
            char_rigs.append((nm, ch.layer_for(ref.get('Namespace', '')), model[0], rig))
    if no_toolkit and charrig.toolkit() is None:
        report.append("install the Cars 2 Toolkit add-on to see characters' faces, eyes, suspension and wheels "
                      'animate (now only bones the clip reaches directly move)')

    # ------------------------------------------------------------- bake
    # sample times: one per frame (every `step` frames for very long ones);
    # a looping choreography wraps its clip time
    fps = scene.render.fps / scene.render.fps_base
    total = ch.duration
    if motion is not None and motion.travel > total:
        total = min(motion.travel, 600.0)    # play until the actor reaches the end of its path
    n = max(1, int(math.ceil(total * fps)))
    step = max(1, int(math.ceil(n / max_frames)))
    fidx = list(range(0, n + 1, step))
    frames = np.array([1 + f for f in fidx], np.float32)
    times = [f / fps for f in fidx]
    clip_times = [t % ch.duration if (item.loop and ch.duration > 0 and t > ch.duration) else t for t in times]
    states = [ch.evaluate(t) for t in clip_times]
    if motion is not None:
        bake_matrices(holder, frames, [root.matrix_world @ _np2m(motion.matrix(t)) for t in times])

    # key only the objects whose DataNode (or one of its parents) is animated
    bound = {i: di for i, di in ch.bind.items() if i in objs}
    animated = set()
    for st in states:
        animated.update(st.keys())
    for i, di in bound.items():
        dep = set()
        p = di
        while p is not None:
            dep.add(p)
            p = ch.dn[p].get('BasisDataNodeRef')
        if not (dep & animated):
            continue
        bake_matrices(objs[i], frames, [_np2m(ch.local(di, st)) for st in states])

    if arm is not None:
        def anim_local(i, st):
            """Animated local of node i at state st (rest local when it isn't bound)."""
            return ch.local(ch.bind[i], st) if i in ch.bind else static_local(i)
        cur_by_frame = []
        for st in states:
            memo = {}
            cur_by_frame.append({bi: world_of(bi, lambda i: anim_local(i, st), memo) for bi in bones})
        for bi in bones:
            pb = arm.pose.bones[names[bi]]
            p = sc.parent(bi)
            mats_ = []
            for cur in cur_by_frame:
                if p in bones:
                    basis = np.linalg.inv(np.linalg.inv(rest_bone[p]) @ rest_bone[bi]) @ np.linalg.inv(cur[p]) @ cur[bi]
                else:
                    basis = np.linalg.inv(rest_bone[bi]) @ cur[bi]
                mats_.append(_np2m(basis))
            bake_matrices(arm, frames, mats_, pose_bone=pb)

    for co_, di in char_objs:
        if di is None:
            continue
        bake_matrices(co_, frames, [_np2m(ch.chain(di, st)) for st in states])
    for nm, layer, tk_arm, plan, cobjs in tk_rigs:
        action = charrig.play(context, tk_arm, layer, float(frames[0]), item.loop, report, nm)
        if action is None:
            continue
        eyes = charrig.eye_materials(cobjs.values())
        if eyes:
            vals = charrig.eye_values(tk_arm, action, plan, frames)
            for m in eyes:
                _bake_params(m, vals, frames)
    for nm, layer, csc, rig in char_rigs:
        poses = _layer_pose(ch, layer, csc, rig, clip_times, report, nm)
        arm_, names_ = rig[0], rig[1]
        for bi, mats_ in poses.items():
            if mats_:
                bake_matrices(arm_, frames, mats_, pose_bone=arm_.pose.bones[names_[bi]])

    # scalar channels: visibility, focal length, material values
    path_obj = {}
    for i, o in objs.items():
        p = ch.node_path(i)
        if p:
            path_obj[p] = o
    for di, path in ch.float_outputs():
        head, leaf = path.rsplit('/', 1)
        vals = [ch.value(path, st, 0.0) for st in states]
        if leaf == 'Visibility' and head in path_obj and path_obj[head].c2_ambient.enabled:
            continue        # light volumes: keyed 0 throughout; their multiplier turns them on
        if leaf == 'Visibility' and head in path_obj:
            o = path_obj[head]
            hidden = [1.0 if v < 0.5 else 0.0 for v in vals]
            if o.get('c2_hidden_node') and min(hidden) < 0.5:
                # hidden in the file, shown by the clip (the fireworks' burst lights)
                o.display_type = 'TEXTURED'
            bake(o, 'hide_viewport', -1, frames, hidden, constant=True)
            bake(o, 'hide_render', -1, frames, hidden, constant=True)
        elif leaf == 'FocalLength' and head in path_obj and path_obj[head].type == 'CAMERA':
            bake(path_obj[head].data, 'lens', -1, frames, vals)
        elif head in ch.material_assoc:
            mi = ch.material_assoc[head]
            amb_key = {'emissiveColorR': ('c2_ambient.color', 0), 'emissiveColorG': ('c2_ambient.color', 1),
                       'emissiveColorB': ('c2_ambient.color', 2),
                       'emissiveMultiplier': ('c2_ambient.multiplier', -1)}.get(leaf)
            for o in amb_by_mat.get(mi, ()) if amb_key else ():
                bake(o, amb_key[0], amb_key[1], frames, vals)
            if mi < len(mats) and mats[mi] is not None and mats[mi].node_tree:
                _bake_material(mats[mi], leaf, frames, vals)

    cams = [o for o in objs.values() if o.type == 'CAMERA']
    if look_through and cams:
        scene.camera = cams[0]
    scene.frame_start = 1
    scene.frame_end = int(frames[-1])
    scene.frame_set(1)
    holder['c2_preview_choreo'] = item.authored
    # played at several places at once (a hologram per gate ...): the same
    # preview again at each, as collection instances
    stretched = any(_anchor(o.matrix_world) != _no_scale(o.matrix_world) for o in [anchor] + list(extra)
                    if o is not None)
    if extra and stretched:
        # a gate stretched to its road (_anchor): an instance's matrix can't carry
        # one placement's stretch relative to another's (it would need a shear),
        # so each place gets a copy of its own
        saved = (holder['c2_preview_frames'], holder['c2_preview_camera'])
        for k, obj in enumerate(extra):
            build_preview(context, item, False, max_frames, True, (obj, k + 2))
            h = scene.objects.get('PREVIEW_%s_%d' % (item.authored, k + 2))
            if h is not None:
                h['c2_preview_frames'], h['c2_preview_camera'] = saved
        if look_through and cams:
            scene.camera = cams[0]
        report.append('also played at %s' % ', '.join(o.name for o in extra))
    elif extra:
        icol = bpy.data.collections.new(PREVIEW_PREFIX + item.authored + ' (copies)')
        scene.collection.children.link(icol)
        icol['c2_preview'] = True
        inv = holder.matrix_world.inverted()
        copies = []
        for k, obj in enumerate(extra):
            e = bpy.data.objects.new('PREVIEW_%s_%d' % (item.authored, k + 2), None)
            e.instance_type = 'COLLECTION'
            e.instance_collection = col
            e.matrix_world = _anchor(obj.matrix_world) @ inv
            icol.objects.link(e)
            copies.append(e.matrix_world.copy())
        particles.add_instances(copies, holder.name)
        report.append('also played at %s' % ', '.join(o.name for o in extra))
    return ch, report


MAT_CHANNELS = {
    'diffuseColorA1R': ('C2_Tint', 7, 0), 'diffuseColorA1G': ('C2_Tint', 7, 1), 'diffuseColorA1B': ('C2_Tint', 7, 2),
    'g_AlphaModifier': ('C2_BSDF', 'Alpha', -1), 'Opacity': ('C2_BSDF', 'Alpha', -1),
    'emissiveColorR': ('C2_BSDF', 'Emission Color', 0), 'emissiveColorG': ('C2_BSDF', 'Emission Color', 1),
    'emissiveColorB': ('C2_BSDF', 'Emission Color', 2),
    'glowAdditiveOpacity': ('C2_BSDF', 'Emission Strength', -1), 'emissiveMultiplier': ('C2_BSDF', 'Emission Strength', -1),
}


def _bake_material(mat, leaf, frames, vals):
    """Key one animated material value (see MAT_CHANNELS) on the material's
    simplified node tree.
    """
    ch = MAT_CHANNELS.get(leaf)
    if ch is None:
        return
    node = mat.node_tree.nodes.get(ch[0])
    if node is None:
        return
    try:
        sock = node.inputs[ch[1]]
    except (KeyError, IndexError):
        return
    if ch[1] == 'Alpha':
        try:
            mat.blend_method = 'BLEND'
        except (AttributeError, TypeError):
            pass
        try:
            mat.surface_render_method = 'BLENDED'
        except AttributeError:
            pass
    path = 'nodes["%s"].inputs[%s].default_value' % (ch[0], ch[1] if isinstance(ch[1], int) else '"%s"' % ch[1])
    bake(mat.node_tree, path, ch[2], frames, vals)


def _bake_params(mat, values, frames):
    """Key game material parameters ({name: [value per frame]}, first
    component) for the Cars 2 Game renderer."""
    from . import fxnodes
    have = {p.name: i for i, p in enumerate(mat.c2.props)}
    fxnodes.SUSPEND[0] += 1
    try:
        for name, vals in values.items():
            i = have.get(name)
            if i is None:
                p = mat.c2.props.add()
                p.name, p.ptype, p.datatype, p.size, p.extra = name, 'Float', 0, 1, True
                p.fdefault = p.fvalue = (float(vals[0]), 0.0, 0.0, 0.0)
                i = have[name] = len(mat.c2.props) - 1
            bake(mat, 'c2.props[%d].fvalue' % i, 0, frames, vals)
    finally:
        fxnodes.SUSPEND[0] -= 1
    mat['c2_animated'] = True


def clear_preview(context, authored=None):
    """Remove every preview collection and restore frame range / camera."""
    scene = context.scene
    from . import particles
    particles.clear()
    for col in [c for c in bpy.data.collections if c.get('c2_preview')]:
        holder = next((o for o in col.objects if 'c2_preview_frames' in o), None)
        if holder is not None:
            fs, fe = holder['c2_preview_frames']
            scene.frame_start, scene.frame_end = int(fs), int(fe)
            cam = bpy.data.objects.get(holder.get('c2_preview_camera', ''))
            scene.camera = cam
        common.remove_ids(list(col.objects), [col])
    for m in [m for m in bpy.data.materials if m.users == 0 and m.get('c2_world') and
              m.get('c2_world') != scene.c2.world_name]:
        bpy.data.materials.remove(m)


class C2_OT_choreo_preview(bpy.types.Operator):
    """Preview the active choreography in the viewport."""
    bl_idname = 'c2.choreo_preview'
    bl_label = 'Preview Choreography'
    bl_description = ('Load the active choreography (from assets/choreographies, folder or .zip) at its anchor and '
                      'bake its animation onto the timeline. Press Play to watch it. Preview objects are not exported')
    bl_options = {'REGISTER', 'UNDO'}
    look_through: BoolProperty(name='Use Its Camera', default=True,
                               description='Make the choreography\'s camera the scene camera (Numpad 0 to look through it)')

    def execute(self, context):
        sc = context.scene.c2
        if not (0 <= sc.active_choreo < len(sc.choreos)):
            self.report({'ERROR'}, 'Select a choreography in the list')
            return {'CANCELLED'}
        item = sc.choreos[sc.active_choreo]
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            ch, report = build_preview(context, item, self.look_through)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        txt = bpy.data.texts.get('Cars2ChoreoPreview') or bpy.data.texts.new('Cars2ChoreoPreview')
        txt.clear()
        txt.write('%s (%s)\nduration %.2fs, %d clips, %d curves\n' % (
            item.authored, ch.pkg, ch.duration, len(ch.clips), len(ch.curves)) + '\n'.join(report))
        self.report({'INFO'}, 'Previewing %s: %.2fs - press Play%s' % (
            item.choreography_name.split('/')[-1], ch.duration,
            ' (some parts not simulated, see Cars2ChoreoPreview text)' if report else ''))
        return {'FINISHED'}


class C2_OT_choreo_preview_clear(bpy.types.Operator):
    """Remove the choreography preview."""
    bl_idname = 'c2.choreo_preview_clear'
    bl_label = 'Clear Preview'
    bl_description = 'Remove all choreography preview objects and restore the frame range and camera'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        clear_preview(context)
        return {'FINISHED'}


class C2_OT_choreo_preview_all(bpy.types.Operator):
    """Preview every choreography the world starts on load."""
    bl_idname = 'c2.choreo_preview_all'
    bl_label = 'Preview All Play-on-Start'
    bl_description = ('Load every choreography set to play on start (the game runs them all as soon as the '
                      'world loads: e.g. the oil rig trimarans\' eyes, one choreography per boat) together on '
                      'the timeline. Preview objects are not exported')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        items = [c for c in scene.c2.choreos if c.play_on_start]
        if not items:
            self.report({'INFO'}, 'No choreography plays on start in this world')
            return {'CANCELLED'}
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        clear_preview(context)
        original = (scene.frame_start, scene.frame_end)
        cam = scene.camera
        built, failed, end = 0, [], 1
        for item in items:
            try:
                ch, _report = build_preview(context, item, False, keep=True)
                built += 1
                end = max(end, scene.frame_end)
            except Exception as e:
                failed.append('%s: %s' % (item.choreography_name.split('/')[-1], e))
        # one timeline for all of them; Clear Preview restores what was there before
        for col in [c for c in bpy.data.collections if c.get('c2_preview')]:
            for o in col.objects:
                if 'c2_preview_frames' in o:
                    o['c2_preview_frames'] = original
                    o['c2_preview_camera'] = cam.name if cam else ''
        scene.frame_start, scene.frame_end = 1, end
        scene.camera = cam
        scene.frame_set(1)
        self.report({'WARNING'} if failed else {'INFO'}, 'Previewing %d choreographies%s' % (
            built, (' - not loaded: ' + '; '.join(failed)) if failed else ''))
        return {'FINISHED'}


_classes = (C2_OT_choreo_preview, C2_OT_choreo_preview_all, C2_OT_choreo_preview_clear)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
