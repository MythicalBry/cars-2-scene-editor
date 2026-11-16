"""Animated characters in choreography previews, through the Cars 2 Toolkit.

A choreography plays a clip into each character it references (a LayerBind,
e.g. 'Yugo:Choreography' on characters/car/car_choreography.ct). The clip
does not move the character's bones: it drives the controls of the
character's own rig network (Body_Ctrl, the wheel and suspension controls,
Jaw, the lips, the Eyes_Ctrl sliders ...), and that network moves the bones
and feeds the eye shader. The Cars 2 Toolkit add-on rebuilds that network as
a Blender control rig and plays clips on it, so the preview uses it:

 1. import the character with the toolkit (SubNetwork: controls, constraints,
    wheel spin, the eye drivers); its meshes are dropped, the preview keeps
    its own game meshes (game materials, Cars 2 Game render engine) and
    skins them to the toolkit's bones, which have the same names and rest
    poses
 2. play the layer clip on the controls (the toolkit's clip importer)
 3. key the eye material parameters (lids, iris position) the rig computes
    from the Eyes_Ctrl sliders and the look control, frame by frame, on the
    game eye material

Without the toolkit the preview falls back to moving the bones the clip
reaches directly (preview._layer_pose).

Rigs of two parts: the toolkit matches skin bones to the network's joints at
one offset, voted over all bones (a car's body height above its rig). The
Zundappcopter is Professor Z (a car, 36 bones) standing on the copter's
platform: in the network his joints sit 3 m from his bind pose, the copter's
(Copter_Body, platform, Gun, Blades, rotor) sit right on theirs. The vote
takes the car's offset, so the toolkit leaves the copter's bones without a
control and the copter hangs off Z, never moving. wire_unmatched() wires every
skin bone the toolkit left alone to the control its own joint follows (the
network's UberConstraint), at whatever offset that joint sits.

The same character also turns Z: his controls rest facing out of the copter's
door, a quarter turn from his bind pose. Skin bones the toolkit makes copy a
control outright (the face) turn with it, but the ones it drives through a
follower bone (Main through Main_Follow, the wheels) keep the bind pose's
orientation, because the toolkit places followers at the skin bone's rest; the
face then faces the door while the body faces ahead, and the mesh tears.
rebase_followers() puts each follower where the network has its joint at rest
(evaluated: an UberConstraint's joint takes its control's transform). On every
ordinary character that is the bind pose itself, and nothing moves. Z's Body
bone also has no control (an ordinary car's copies Body_Final_Proxy): its
joint is the rig bone Body_Rig, 4 mm off and named, which the toolkit's
matching skips; follow_rig_bones() hangs it on Body_Rig.
"""

import os
import sys
import tempfile

import bpy
import numpy as np

from . import common

TOOLKIT_NAME = 'Cars 2 Toolkit'
# core modules both add-ons ship under the same top-level name; ours are a
# superset of the toolkit's, so ours stay registered for everyone
_SHARED_CORE = ('c2oct', 'c2headers', 'c2clipblock')
_OWN_CORE = {}

EYE_EFFECTS = ('car_eyes', 'car_double_eyes')


def remember_core():
    """Called at registration: our copies of the shared core modules."""
    for n in _SHARED_CORE:
        if n in sys.modules:
            _OWN_CORE[n] = sys.modules[n]


def _pin_core():
    """Loading the toolkit puts its own c2oct etc. in sys.modules: put ours back."""
    for n, m in _OWN_CORE.items():
        sys.modules[n] = m


def toolkit():
    """(character, anim, c2_model) modules of the Cars 2 Toolkit add-on, loaded
    for this session if it is installed but not enabled; None when it isn't
    installed."""
    import addon_utils
    mod = None
    for m in addon_utils.modules():
        if getattr(m, 'bl_info', {}).get('name') == TOOLKIT_NAME:
            mod = m
            break
    if mod is None:
        return None
    name = mod.__name__
    loaded = sys.modules.get(name)
    if loaded is None or not hasattr(loaded, 'character'):
        try:
            addon_utils.enable(name, default_set=False, persistent=False)
        except Exception as e:
            print('Cars 2 Scene Editor: could not load %s: %s' % (TOOLKIT_NAME, e))
            return None
        loaded = sys.modules.get(name)
    _pin_core()
    try:
        return loaded.character, loaded.anim, loaded.c2_model
    except AttributeError:
        return None


def _package_file(pkg):
    """Path of the character's .oct on disk, with its buffers and .bent
    around it the way the toolkit expects (zipped packages are extracted to
    a temp folder, once per zip version)."""
    if pkg.folder:
        return os.path.join(pkg.folder, pkg.stem + '.oct')
    st = os.stat(pkg.zpath)
    base = os.path.join(tempfile.gettempdir(), 'cars2_scene_editor', 'characters',
                        '%s_%d_%d' % (pkg.stem, st.st_size, int(st.st_mtime)))
    path = os.path.join(base, *pkg.prefix.strip('/').split('/'), pkg.stem + '.oct')
    if not os.path.isfile(path):
        z = pkg._zip
        for n in z.namelist():
            # the package's own files, and the .bent (channel names) at the zip's root
            if n.endswith('/') or not (n.startswith(pkg.prefix) or ('/' not in n and n.lower().endswith('.bent'))):
                continue
            out = os.path.join(base, *n.split('/'))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as f:
                f.write(z.read(n))
    return path


def build(context, pkg, col, parent, report, nm):
    """Toolkit control rig of a character package under `parent` (game model
    space, like the preview's own meshes). Returns (armature, rig plan) or
    None."""
    tk = toolkit()
    if tk is None:
        return None
    character, _anim, c2_model = tk
    try:
        path = _package_file(pkg)
    except (OSError, ValueError) as e:
        report.append('%s: character files not readable (%s)' % (nm, e))
        return None
    before = set(bpy.data.objects)
    before_cols = set(bpy.data.collections)
    before_mats = set(bpy.data.materials)
    quiet = getattr(c2_model, 'LOG_LINES', None)
    # keep what the toolkit plans the rig from (wire_unmatched reads it again)
    seen = {}
    planner = getattr(c2_model, 'plan_control_rig', None)

    def spy(sub_networks, skin_bones):
        seen['net'], seen['skin'] = sub_networks, skin_bones
        return planner(sub_networks, skin_bones)
    if planner is not None:
        c2_model.plan_control_rig = spy
    try:
        arm, msg = character.import_character(context, path, import_textures=False)
    except Exception as e:
        report.append('%s: toolkit import failed (%s)' % (nm, e))
        arm = None
    finally:
        if planner is not None:
            c2_model.plan_control_rig = planner
        _pin_core()
        if isinstance(quiet, list):
            del quiet[:]
    plan = getattr(c2_model, '_LAST_RIG_PLAN', None) or {}
    made = [o for o in bpy.data.objects if o not in before]
    # the preview draws its own game meshes: drop the toolkit's (and their materials)
    common.remove_ids([o for o in made if o is not arm], [c for c in bpy.data.collections if c not in before_cols],
                      data=False)
    for m in [m for m in bpy.data.materials if m not in before_mats]:
        if m.users == 0:
            bpy.data.materials.remove(m)
    if arm is None:
        if msg:
            report.append('%s: %s' % (nm, msg))
        return None
    for c in list(arm.users_collection):
        c.objects.unlink(arm)
    col.objects.link(arm)
    arm.name = 'RIG_' + nm
    arm.parent = parent
    arm.matrix_parent_inverse.identity()
    # The parent is already in game space (no Y-up turn). The toolkit's bones
    # rest at the game bones' bind pose (BoneToModelMatrix); the rig network
    # the game evaluates keeps everything below Main lower by the rig offset
    # (a car's body height above its rig: wheel centres on the ground), and the
    # game skins with the network's result. So the posed rig sits that much
    # lower; the meshes skinned to it keep their bind-pose vertices.
    from mathutils import Matrix, Vector
    off = arm.get('cars2_rig_offset')
    arm.matrix_basis = Matrix.Translation(-Vector(off)) if off is not None and len(off) == 3 else Matrix.Identity(4)
    arm.show_in_front = True
    if seen.get('net') is not None and plan:
        try:
            turned = rebase_followers(context, arm, c2_model, seen['net'], seen['skin'], plan)
            if turned:
                report.append('%s: follower bones placed at the rig network\'s rest: %s' % (nm, ', '.join(turned)))
            hung = follow_rig_bones(context, arm, c2_model, seen['net'], seen['skin'], plan)
            if hung:
                report.append('%s: bones following the rig bone at their joint: %s' % (nm, ', '.join(hung)))
            wired = wire_unmatched(context, arm, c2_model, seen['net'], seen['skin'], plan)
            if wired:
                report.append('%s: bones the toolkit left without a control, wired to the rig network: %s'
                              % (nm, ', '.join(wired)))
        except Exception as e:      # never let this stop the preview
            report.append('%s: extra rig joints not wired (%s)' % (nm, e))
    return arm, plan


class _Network:
    """The rig network the toolkit planned from: stored rest matrices
    (world), the UberConstraint links, and the rest as evaluated (an
    UberConstraint's output joint takes its input control's transform)."""

    def __init__(self, c2_model, sub_networks):
        pool = c2_model.pool_list
        net = next((sn for sn in pool(sub_networks) if sn.get('DataNodePool') and sn.get('ProcessorNodePool')), None)
        self.ok = net is not None
        self.dn = pool(net.get('DataNodePool')) if net is not None else []
        self.c2 = c2_model
        self.uber = []
        for p in pool(net.get('ProcessorNodePool')) if net is not None else ():
            if p.get('Type') == 'UberConstraintProcessor':
                self.uber += [(o, i) for o, i in zip(p.get('Output') or [], p.get('Input') or [])
                              if 0 <= o < len(self.dn) and 0 <= i < len(self.dn)]
        self.driver = dict(self.uber)
        self._world, self._eval = {}, {}

    def _parent(self, i):
        par = self.dn[i].get('BasisDataNodeRef')
        return par if isinstance(par, int) and 0 <= par < len(self.dn) else None

    def world(self, i, guard=0):
        """Stored rest (parent chain of local matrices)."""
        if i not in self._world:
            par = self._parent(i)
            loc = self.c2._data_node_local(self.dn[i])
            self._world[i] = self.world(par, guard + 1) @ loc if par is not None and guard < 256 else loc
        return self._world[i]

    def evaluated(self, i, guard=0):
        """Rest as the network evaluates it."""
        if i not in self._eval:
            if guard > 256:
                return self.world(i)
            if i in self.driver:
                m = self.evaluated(self.driver[i], guard + 1)
            else:
                par = self._parent(i)
                loc = self.c2._data_node_local(self.dn[i])
                m = self.evaluated(par, guard + 1) @ loc if par is not None else loc
            self._eval[i] = m
        return self._eval[i]


def rebase_followers(context, arm, c2_model, sub_networks, skin_bones, plan):
    """Follower bones (a skin bone's loc_via / rot_via target, e.g.
    Main_Follow, the wheels' *_centerJoint_Follow) placed where the network
    has the skin bone's joint at rest when that differs from the bind pose
    (see the module doc). A turned rig also moves its wheels' joints off the
    wheels' bind positions, where the toolkit's nearest-joint fallback picks
    the wrong wheel (Z's front wheels took the back wheels' controls): once
    the root's turn is known, each follower goes to the joint that turn puts
    on its bone, under the control driving that joint. Returns the skin
    bones whose followers moved."""
    import math
    from mathutils import Matrix, Vector
    net = _Network(c2_model, sub_networks)
    if not net.ok:
        return []
    offset = Vector(plan.get('offset') or (0.0, 0.0, 0.0))
    T = Matrix.Translation(offset)
    rest_of = {n: r for n, r, _p in skin_bones}
    parent_of = {n: p for n, _r, p in skin_bones}
    controls = {i for _o, i in net.uber}
    node_of = plan.get('node_of_bone') or {}
    bone_of = {i: n for n, i in node_of.items()}
    labelled = set(node_of.values())
    xform = [i for i in range(len(net.dn)) if net.dn[i].get('Type') in c2_model._XFORM_TYPES]
    joints = [i for i in xform if i not in controls and i not in labelled]
    bones = arm.data.bones
    jobs = []
    for owner, _target, ops in plan.get('constraints') or []:
        fol = sorted({op[1] for op in ops if op[0] in ('loc_via', 'rot_via', 'scale_via') and len(op) > 1})
        if fol and owner in rest_of and all(f in bones for f in fol):
            jobs.append((owner, fol))
    # roots first: their turn places the rest
    jobs.sort(key=lambda j: parent_of.get(j[0]) is not None)
    turn_of_root = None
    todo = []
    for owner, fol in jobs:
        rest = rest_of[owner]
        p = rest.to_translation()
        joint = None
        if turn_of_root is not None:
            # where the root's turn puts this bone: the joint there
            want = (turn_of_root @ rest).to_translation()
            cands = [i for i in joints if ((T @ net.evaluated(i)).to_translation() - want).length < 2e-3]
            joint = cands[0] if cands else None
        if joint is None:
            cands = [i for i in joints if ((net.world(i).to_translation() + offset) - p).length < 2e-3]
            joint = cands[0] if cands else None
        if joint is None:
            continue
        ev = T @ net.evaluated(joint)
        delta = ev @ rest.inverted()
        if parent_of.get(owner) is None and turn_of_root is None:
            turn_of_root = delta
        turn = math.degrees(rest.to_quaternion().rotation_difference(ev.to_quaternion()).angle)
        if turn < 1.0 and (ev.to_translation() - p).length < 1e-3:
            continue                        # an ordinary character: the bind pose
        # the control driving the joint (or the nearest driven ancestor's)
        k, ctrl = joint, None
        for _g in range(256):
            if k in net.driver:
                ctrl = bone_of.get(net.driver[k])
                break
            k = net._parent(k)
            if k is None:
                break
        todo.append((owner, fol, ev, ctrl if ctrl is not None and ctrl in bones else None))
    if not todo:
        return []
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        ebs = arm.data.edit_bones
        for _owner, fol, ev, ctrl in todo:
            loc, rot, _sc = ev.decompose()
            for f in fol:
                eb = ebs[f]
                length = eb.length
                eb.head = (0.0, 0.0, 0.0)
                eb.tail = (0.0, length, 0.0)
                eb.matrix = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
                if ctrl is not None and eb.parent is not None and eb.parent.name != ctrl:
                    eb.parent = ebs[ctrl]
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = prev
    return [o for o, _f, _e, _c in todo]


def follow_rig_bones(context, arm, c2_model, sub_networks, skin_bones, plan):
    """Skin bones left without a control (not roots, not driven: the wheel
    spin joints have drivers) whose own joint is a bone of the toolkit's rig
    that the network drives (Z's Body: Body_Rig, which follows rig_16_Proxy
    under Body_Ctrl): a follower under that rig bone at the skin bone's rest,
    copied in pose space, so the skin bone takes the rig bone's motion from
    its rest. Returns the skin bones hung."""
    from mathutils import Vector
    net = _Network(c2_model, sub_networks)
    if not net.ok:
        return []
    offset = Vector(plan.get('offset') or (0.0, 0.0, 0.0))
    owned = {o for o, _t, _ops in plan.get('constraints') or []}
    node_of = plan.get('node_of_bone') or {}
    driven_bones = [(n, i) for n, i in node_of.items() if i in net.driver and n in arm.data.bones]
    ad = arm.animation_data
    with_driver = set()
    for d in (ad.drivers if ad is not None else ()):
        if d.data_path.startswith('pose.bones["'):
            with_driver.add(d.data_path.split('"')[1])
    bones = arm.data.bones
    todo = []
    for name, rest, parent in skin_bones:
        if parent is None or name in owned or name in with_driver or name not in bones \
                or arm.pose.bones[name].constraints:
            continue
        p = rest.to_translation()
        best = None
        for rb, i in driven_bones:
            d = ((net.world(i).to_translation() + offset) - p).length
            if d < 0.01 and (best is None or d < best[0]):
                best = (d, rb)
        if best is not None:
            todo.append((name, best[1]))
    if not todo:
        return []
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        ebs = arm.data.edit_bones
        for name, rb in todo:
            src = ebs[name]
            f = ebs.new(name + '_RigFollow')
            f.head, f.tail, f.roll = src.head.copy(), src.tail.copy(), src.roll
            f.parent = ebs[rb]
            f.use_deform = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = prev
    for name, _rb in todo:
        c = arm.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        c.name = 'Rig follow'
        c.target = arm
        c.subtarget = name + '_RigFollow'
        c.owner_space = c.target_space = 'POSE'
    return [n for n, _r in todo]


def wire_unmatched(context, arm, c2_model, sub_networks, skin_bones, plan):
    """Skin bones the toolkit left without a control (see the module doc):
    find the network joint sitting on the bone (at any offset) that an
    UberConstraint drives from a control bone of the rig, and make the bone
    follow that control: a follower bone under the control at the bone's
    rest moved by the joint's offset, which the skin bone copies (pose
    space). Returns the names of the bones wired."""
    from mathutils import Matrix, Vector
    pool = c2_model.pool_list
    net = next((sn for sn in pool(sub_networks) if sn.get('DataNodePool') and sn.get('ProcessorNodePool')), None)
    if net is None:
        return []
    dn = pool(net.get('DataNodePool'))
    world = {}

    def get_world(i, guard=0):
        if i in world:
            return world[i]
        par = dn[i].get('BasisDataNodeRef')
        loc = c2_model._data_node_local(dn[i])
        m = get_world(par, guard + 1) @ loc if par is not None and 0 <= par < len(dn) and guard < 256 else loc
        world[i] = m
        return m
    uber = []
    for p in pool(net.get('ProcessorNodePool')):
        if p.get('Type') == 'UberConstraintProcessor':
            uber += [(o, i) for o, i in zip(p.get('Output') or [], p.get('Input') or [])
                     if 0 <= o < len(dn) and 0 <= i < len(dn)]
    bone_of = {i: nm for nm, i in (plan.get('node_of_bone') or {}).items()}
    owned = {o for o, _t, _ops in plan.get('constraints') or []}
    offset = Vector(plan.get('offset') or (0.0, 0.0, 0.0))
    bones = arm.data.bones
    todo = []
    for name, rest, parent in skin_bones:
        # roots (Main) stay as the toolkit leaves them
        if parent is None or name in owned or name not in bones or arm.pose.bones[name].constraints:
            continue
        p = rest.to_translation()
        best = None
        for out, inp in uber:
            ctrl = bone_of.get(inp)
            if ctrl is None or ctrl not in bones:
                continue
            d = (p - get_world(out).to_translation()).length
            if d < 2e-3 and (best is None or d < best[0]):
                best = (d, ctrl, out)
        if best is not None:
            todo.append((name, best[1], best[2]))
    # only whole parts (a bone wired with its skin parent or child): a single
    # stray joint at offset 0 (Guido's, Sarge's and Fillmore's Lid, Axelrod's
    # SwappDummy) may well be a coincidence, and on a car the rest of the rig
    # is lowered by the offset - those keep the toolkit's own behaviour
    names = {t[0] for t in todo}
    parent_of = {n: p for n, _r, p in skin_bones}
    linked = {n for n in names if parent_of.get(n) in names} | \
        {parent_of.get(n) for n in names if parent_of.get(n) in names}
    todo = [t for t in todo if t[0] in linked]
    if not todo:
        return []
    # the rig's bones are in its own space: the network's joints sit at
    # their rest + offset there; a joint on the bone itself (offset 0) has its
    # control `offset` away, so the follower is placed that far off the bone
    prev = context.view_layer.objects.active
    mode = arm.mode
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        ebs = arm.data.edit_bones
        for name, ctrl, _out in todo:
            src = ebs[name]
            f = ebs.new(name + '_NetFollow')
            f.head, f.tail, f.roll = src.head + offset, src.tail + offset, src.roll
            f.parent = ebs[ctrl]
            f.use_deform = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = prev
    for name, _ctrl, _out in todo:
        c = arm.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        c.name = 'Net follow'
        c.target = arm
        c.subtarget = name + '_NetFollow'
        c.owner_space = c.target_space = 'POSE'
    return [name for name, _c, _o in todo]


def skin(objs, arm, old):
    """Bind the preview's skinned meshes ({node: object}) to the toolkit rig
    in place of the plain game-bone armature `old`, which goes."""
    for o in objs.values():
        mod = next((m for m in o.modifiers if m.type == 'ARMATURE'), None)
        if mod is None:
            continue
        mod.object = arm
        o.parent = arm
        o.matrix_parent_inverse.identity()
        o.matrix_basis.identity()
    if old is not None:
        ad = old.data
        bpy.data.objects.remove(old)
        if ad.users == 0:
            bpy.data.armatures.remove(ad)


def play(context, arm, layer, first_frame, loop, report, nm):
    """Play a layer clip on the toolkit rig: an action from first_frame.
    Returns the action or None."""
    tk = toolkit()
    if tk is None:
        return None
    _character, anim, _c2_model = tk
    template = os.path.splitext(os.path.basename(layer['template'].replace('\\', '/')))[0]
    if template not in anim.BUILTIN_CHANNEL_MAPS:
        report.append('%s: no channel names for %s' % (nm, layer['template']))
        return None
    # the toolkit reads clips from files: hand it the block and its template
    read, templates = anim.read_block, anim.templates_in_file
    anim.read_block = lambda _p, _t: layer['block']
    anim.templates_in_file = lambda _p: [template]
    fps = context.scene.render.fps / context.scene.render.fps_base
    try:
        anim.reset_pose(arm)
        action, _n, unmatched = anim.import_clip(context, layer['name'].replace(':', '_') + '.oct', None, fps,
                                                 target=arm)
    except Exception as e:
        report.append('%s: clip not played (%s)' % (nm, e))
        return None
    finally:
        anim.read_block, anim.templates_in_file = read, templates
        _pin_core()
    action.name = 'CLIP_' + nm
    if unmatched:
        report.append('%s: %d clip channels without a control (%s ...)' % (nm, len(unmatched), unmatched[0]))
    for fc in anim.action_curves(action):
        kps = fc.keyframe_points
        if len(kps):
            co = np.empty(len(kps) * 2, np.float32)
            for attr in ('co', 'handle_left', 'handle_right'):
                kps.foreach_get(attr, co)
                co[0::2] += first_frame
                kps.foreach_set(attr, co)
        if loop:
            fc.modifiers.new('CYCLES')
        fc.update()
    anim.assign_action(arm, action)
    anim.settle_eyelids(arm, action)
    return action


def _curve(action, anim, path, index=0):
    return next((fc for fc in anim.action_curves(action) if fc.data_path == path and fc.array_index == index), None)


def eye_values(arm, action, plan, frames):
    """{eye material parameter: [value per frame]}: the Eyes_Ctrl sliders it
    is linked to, and the look interpolator over the Eyes control position
    (the same network the toolkit's eye shader evaluates)."""
    tk = toolkit()
    if tk is None:
        return {}
    _character, anim, c2_model = tk
    out = {}
    for label, param, rest in plan.get('eye_links', []):
        fc = _curve(action, anim, '["%s"]' % label)
        if fc is not None:
            out[param] = [fc.evaluate(f) for f in frames]
        else:
            out[param] = [float(arm.get(label, rest))] * len(frames)
    rbf = plan.get('eye_rbf')
    if rbf and rbf.get('bone') in arm.pose.bones:
        pb = arm.pose.bones[rbf['bone']]
        path = 'pose.bones["%s"].location' % pb.name
        scale = getattr(c2_model, 'EYE_LOOK_SCALE', None) or rbf.get('frame_scale') or (1.0, 1.0)
        env = {}
        for i, axis in enumerate(('look_x', 'look_y')):
            fc = _curve(action, anim, path, i)
            vals = [fc.evaluate(f) for f in frames] if fc is not None else [pb.location[i]] * len(frames)
            env[axis] = np.asarray(vals, np.float64) * scale[i]
        roots = c2_model.build_look_graph(rbf['keys'], rbf['sds'], rbf['outputs'])
        for name, node in roots.items():
            v = c2_model.eval_numpy(node, env)
            out[name] = list(np.broadcast_to(np.asarray(v, np.float64), (len(frames),)))
    return out


def eye_materials(objs):
    """Game eye materials (car_eyes_* / car_double_eyes_*) on these objects."""
    out = []
    for o in objs:
        for m in getattr(o.data, 'materials', []) or []:
            if m is not None and m not in out and (m.c2.effect or '').lower().startswith(EYE_EFFECTS):
                out.append(m)
    return out
