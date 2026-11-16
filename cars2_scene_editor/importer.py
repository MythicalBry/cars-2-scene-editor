"""File > Import > Cars 2 Scene (.oct).

Turns a world into Blender objects:
    root empty 'C2_<world>'      rotated so the game's Y-up becomes Z-up
      Geometry nodes  -> mesh objects (one material slot per part)
      Light / Camera  -> lights / cameras
      other nodes     -> empties
      collision       -> wireframe objects (Collision collection)
      probes          -> light probe objects
Everything is tagged with obj.c2.kind / node_index so the exporter can match
it back to the original records. The original files stay untouched; the
exporter re-reads them (scene.c2.oct_path) and applies the changes.
"""
import base64
import json
import math
import os
import re
import time
import zlib

import bpy
import numpy as np
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector

import apfile
import arcade
import btcol
import c2oct
import dds
import meridian
import octscene
from . import common, matio, matnodes
from .common import g2b

SURFACE_COLORS = {}


def _uniq_name(base, existing):
    """Kept for old callers: names are used as they are."""
    return base


# ----------------------------------------------------------------------
# textures / materials
# ----------------------------------------------------------------------

def extract_textures(sc, scene, folder):
    """Every TexturePool texture -> scene.c2.textures (+ files); {index: image}."""
    return matio.fill_texture_table(scene.c2.textures, sc.textures, folder)


def _prop_value(pe):
    """Value of a material PropertyEntry."""
    return pe.get('Value')


def game_context(sc, scene, images, folder, assets):
    """matnodes.GameContext for an Arcade world (None without the game's effects)."""
    from . import fxnodes
    if not arcade.effects_dir(assets):
        return None
    files = {t.index: bpy.path.abspath(t.file) for t in scene.c2.textures if t.file}
    glob = {}
    for m in sc.materials:
        if m.get('Effect') == 'scene_settings':
            glob = {te['Usage']: te['TextureReference'] for te in m.get('TextureEntries', {}).values()}
    return matnodes.GameContext(assets, fxnodes.Textures(images, files, glob, os.path.join(folder, 'equirect'),
                                                         srgb=fxnodes.texture_srgb(scene)))


def import_materials(sc, images, is_arcade=None, game=None):
    """Blender materials for the MaterialPool, as a matnodes.MaterialSet
    (Arcade parts with their own lightmap get per-lightmap variants)."""
    if is_arcade is None:
        is_arcade = arcade.is_arcade(sc)
    anims = arcade.texture_animations(sc.tree) if is_arcade else {}
    mats = [make_material(sc, i, images, is_arcade, game, anims) for i in range(len(sc.materials))]
    return matnodes.MaterialSet(sc, mats, images, is_arcade, game)


def make_material(sc, i, images, is_arcade, game, anims):
    """Blender material for MaterialPool entry i (with its node tree).
    anims: texture_animations result (flipbook slots).
    """
    m = sc.materials[i]
    mat = bpy.data.materials.new(m.get('Name', 'Material%d' % i))
    mat['c2_world'] = sc.name
    mat['c2_platform'] = 'ARCADE' if is_arcade else 'PC'
    # kept even when no mesh uses it (Replace World deletes every user, and
    # Auto-Assign copies these): Blender drops user-less materials on save
    mat.use_fake_user = True
    matio.fill_material_props(mat, m, i)
    if i in anims:
        mat['c2_texanim'] = anims[i]      # flipbook slots (shown in Game Look; exported untouched)
    if game is not None:
        from . import fxnodes
        if bpy.context.scene.c2.eevee_game_shaders:
            plan = fxnodes.plan_for(game.assets, mat.c2.effect)
        else:   # parameters / slots only: no node translation needed
            from . import gamerender
            plan = gamerender.meta_for(game.assets, mat.c2.effect)
        if plan is not None:
            fxnodes.ensure_effect_entries(mat, plan)   # every parameter / slot the effect has
    matnodes.build(mat, images, game)
    return mat


def restore_world_materials(scene):
    """Rebuild the world's game materials that are missing from the .blend
    (lost after Replace World removed every mesh using them) from the source
    .oct, and protect all of them with a fake user. Returns (restored, kept)."""
    src = bpy.path.abspath(scene.c2.oct_path)
    sc = octscene.OctScene(src)
    world = sc.name
    have = {}
    for m in bpy.data.materials:
        if m.get('c2_world') == world and m.c2.variant_of is None and 0 <= m.c2.index < len(sc.materials) \
                and not m.get('c2_auto_from'):
            m.use_fake_user = True
            have.setdefault(m.c2.index, m)
    missing = [i for i in range(len(sc.materials)) if i not in have]
    if not missing:
        return 0, len(have)
    folder = os.path.join(sc.folder, world) + '.oct.textures'
    if len(scene.c2.textures):
        images = {}
        for t in scene.c2.textures:
            p = bpy.path.abspath(t.file) if t.file else ''
            if t.index < len(sc.textures) and t.ttype == '2d' and p and os.path.exists(p):
                try:
                    images[t.index] = bpy.data.images.load(p, check_existing=True)
                except RuntimeError:
                    pass
    else:
        images = extract_textures(sc, scene, folder)
    is_arc = arcade.is_arcade(sc)
    game = None
    if is_arc:
        import choreo
        assets = common.game_assets(scene) or choreo.assets_dir_from(src)
        game = game_context(sc, scene, images, folder, assets)
    anims = arcade.texture_animations(sc.tree) if is_arc else {}
    for i in missing:
        mat = make_material(sc, i, images, is_arc, game, anims)
        if sc.materials[i].get('Effect') == 'scene_settings' and scene.c2.scene_settings is None:
            scene.c2.scene_settings = mat
    return len(missing), len(have)


# ----------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------

def build_geometry_mesh(sc, gi, name, mats):
    """mats: matnodes.MaterialSet (or a plain list of materials)."""
    if not isinstance(mats, matnodes.MaterialSet):
        mats = matnodes.MaterialSet(sc, list(mats), {}, False)
    pools, parts = sc.geometry_parts(gi)
    if not parts:
        return None
    # all parts become one Blender mesh: vertex pools are stacked, each
    # part's triangles get its pool's offset, its material slot and its
    # SubGeometry node number (face attribute 'c2_subgeo', used on export to
    # put faces back into their original parts)
    offsets, total = [], 0
    for p in pools:
        offsets.append(total)
        total += p['count']
    pos = np.concatenate([p['attrs']['Position'][:, :3] for p in pools]).astype(np.float32)
    tris, tri_mat, tri_sub = [], [], []
    slot_of = {}
    for part in parts:
        idx = part['indices']
        n = len(idx) // 3
        if n == 0:
            continue
        tris.append(idx[:n * 3].reshape(n, 3) + offsets[part['pool']])
        slot = slot_of.setdefault(mats.key(sc.nodes[part['node']]), len(slot_of))
        tri_mat.append(np.full(n, slot, np.int32))
        tri_sub.append(np.full(n, part['node'], np.int32))
    if not tris:
        return None
    tris = np.concatenate(tris).astype(np.int32)
    tri_mat = np.concatenate(tri_mat)
    tri_sub = np.concatenate(tri_sub)
    # drop degenerate triangles (two corners on one vertex: zero area, they draw
    # nothing). Blender can't make an edge from a vertex to itself, and setting
    # custom normals on such a mesh crashes it (mi_italy, mi_london, mi_radiator).
    # An untouched mesh still exports its original index bytes.
    ok = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    if not ok.all():
        tris, tri_mat, tri_sub = tris[ok], tri_mat[ok], tri_sub[ok]
        if not len(tris):
            return None
    ntri = tris.shape[0]
    # build the mesh in bulk (foreach_set) - far faster than bmesh for big worlds
    me = bpy.data.meshes.new(name)
    me.vertices.add(total)
    me.vertices.foreach_set('co', pos.ravel())
    me.loops.add(ntri * 3)
    me.loops.foreach_set('vertex_index', tris.ravel())
    me.polygons.add(ntri)
    me.polygons.foreach_set('loop_start', np.arange(0, ntri * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(ntri, 3, np.int32))
    me.polygons.foreach_set('material_index', tri_mat)
    me.update(calc_edges=True)
    for key in sorted(slot_of, key=lambda k: slot_of[k]):
        me.materials.append(mats.material(key))
    a = me.attributes.new('c2_subgeo', 'INT', 'FACE')
    a.data.foreach_set('value', tri_sub)
    loop_v = tris.ravel()

    def gather(name_, comps, default):
        # one vertex element over all pools (default where a pool lacks it)
        arrs = []
        for p in pools:
            v = p['attrs'].get(name_)
            if v is None:
                v = np.tile(np.asarray(default, np.float32), (p['count'], 1))
            arrs.append(np.asarray(v, np.float32)[:, :comps])
        return np.concatenate(arrs)

    names = []
    for p in pools:
        for k in p['attrs']:
            if k not in names:
                names.append(k)
    for k in names:
        if k in ('Position', 'Normal', 'Tangent', 'Binormal'):
            continue  # tangent frames are rebuilt from Uv1 on export
        if k.lower().startswith('uv') or k.endswith('UV') or k.startswith('Uv'):
            uv = gather(k, 2, (0.0, 0.0))
            uv = uv.copy()
            uv[:, 1] = 1.0 - uv[:, 1]        # Direct3D v runs down, Blender's up
            layer = me.uv_layers.new(name=k)
            layer.data.foreach_set('uv', uv[loop_v].ravel())
        elif all(p['attrs'].get(k) is None or np.asarray(p['attrs'][k]).shape[1] == 4 for p in pools):
            # ColorPrelight / ColorOcclusion / ColorBlend / VertexBaked ...: the raw
            # bytes go in as sRGB-encoded byte colours, so painting edits them 1:1
            col = gather(k, 4, vf_default(k))
            ca = me.color_attributes.new(k, 'BYTE_COLOR', 'POINT')
            ca.data.foreach_set('color_srgb', np.clip(col, 0, 1).ravel())
    if mats.arcade and 'ColorOcclusion' not in me.color_attributes:
        # the viewport shader multiplies baked light by it (the game shaders
        # that have no such stream simply don't); not exported unless a layout has it
        ca = me.color_attributes.new('ColorOcclusion', 'BYTE_COLOR', 'POINT')
        ca.data.foreach_set('color_srgb', np.ones(total * 4, np.float32))
        me['c2_added_occlusion'] = True
    # keep the game's own normals (custom split normals), not Blender's smoothing
    if all('Normal' in p['attrs'] for p in pools):
        nrm = gather('Normal', 3, (0, 1, 0)).astype(np.float64)
        l = np.linalg.norm(nrm, axis=1, keepdims=True)
        l[l < 1e-12] = 1.0
        me.normals_split_custom_set_from_vertices((nrm / l).tolist())
    for k in ('ColorPrelight', 'VertexBaked'):
        if k in me.color_attributes:
            me.color_attributes.active_color = me.color_attributes[k]
            break
    if 'Uv1' in me.uv_layers:
        me.uv_layers.active = me.uv_layers['Uv1']
    return me


def vf_default(name):
    """Neutral default value of a vertex element (see vertexfmt.default_values)."""
    import vertexfmt
    return tuple(vertexfmt.default_values(name, 1)[0])


# ----------------------------------------------------------------------
# lights / cameras / probes
# ----------------------------------------------------------------------

def make_light(n, name):
    """Blender light data for a scene Light node. The game fields go to
    light.c2; unknown StaticKeyValuePairs are kept in extra_kv for export.
    """
    lt = n.get('LightType', 'Directional')
    btype = {'Directional': 'SUN', 'Spot': 'SPOT', 'Ambient': 'POINT', 'Point': 'POINT'}.get(lt, 'POINT')
    ld = bpy.data.lights.new(name, btype)
    ld.color = list(n.get('LightColor', [1, 1, 1]))[:3]
    c = ld.c2
    c.light_type = lt if lt in ('Directional', 'Spot', 'Ambient', 'Point') else 'Directional'
    c.intensity = n.get('LightIntensity', 1.0)
    c.shadow_color = list(n.get('ShadowColor', [0, 0, 0]))[:3]
    c.exclusion_type = n.get('ExclusionType', 0)
    c.ambient_shade = n.get('AmbientShade', 0.45)
    c.decay_rate = n.get('DecayRate', 0)
    c.cone_angle = n.get('ConeAngle', 0.698)
    c.penumbra_angle = n.get('PenumbraAngle', 0.0)
    c.dropoff = n.get('Dropoff', 0.0)
    kv = n.get('StaticKeyValuePairs', {})
    c.falloff_radius = kv.get('FalloffRadius', '10.')
    c.softness = kv.get('Softness', '0.')
    c.is_roaming = kv.get('IsRoaming', 'False') == 'True'
    c.use_tertiary = kv.get('UseTertiary', 'False') == 'True'
    c.is_negative = kv.get('IsNegative', 'False') == 'True'
    c.lm_normal = kv.get('LightUsageLightmapNormal', 'False') == 'True'
    c.lm_specular = kv.get('LightUsageLightmapSpecular', 'False') == 'True'
    known = {'FalloffRadius', 'Softness', 'IsRoaming', 'UseTertiary', 'IsNegative',
             'LightUsageLightmapNormal', 'LightUsageLightmapSpecular'}
    for k, v in kv.items():
        if k not in known:
            e = c.extra_kv.add()
            e.key, e.value = k, v
    from .props import light_preview_energy
    ld.energy = light_preview_energy(ld)
    if btype == 'SPOT':
        ld.spot_size = max(0.02, min(math.pi, c.cone_angle))
        ld.spot_blend = max(0.0, min(1.0, abs(c.penumbra_angle) / max(c.cone_angle, 1e-3)))
    return ld


def make_camera(n, name):
    """Blender camera data for a scene Camera node."""
    cd = bpy.data.cameras.new(name)
    if n.get('FocalLength'):
        cd.lens = n['FocalLength']
    ap = n.get('HorizontalFilmAmperture')
    if ap:
        cd.sensor_width = ap * 25.4
    cd.clip_start = max(1e-3, n.get('NearClipPlane', 0.1))
    cd.clip_end = n.get('FarClipPlane', 1000.0)
    return cd


def import_probe(sc, root, node_objs, col, k, pr):
    """A probe hangs under its node (NodeRef), so it follows it. Volume probes'
    nodes are the invisible box Geometry the volume was authored as:
    VolumeExtents is that box's local half size, Position its world centre."""
    ty = pr.get('TypeName', 'Environment')
    name = pr.get('ProbeName', 'Probe%d' % k)
    lp = bpy.data.lightprobes.new(name, 'VOLUME' if ty == 'Volume' else 'SPHERE')
    obj = bpy.data.objects.new(name, lp)
    col.objects.link(obj)
    pos = Vector(pr.get('Position', [0, 0, 0]))
    ext = pr.get('VolumeExtents', [1, 1, 1])
    node = node_objs.get(pr.get('NodeRef', -1))
    if node is not None and node is not root:
        obj.parent = node
        obj.matrix_parent_inverse = Matrix.Identity(4)
        local = sc.world(pr['NodeRef'])
        p_local = np.append(np.asarray(pos, np.float64), 1.0) @ np.linalg.inv(local)
        obj.matrix_basis = Matrix.Translation(Vector(p_local[:3])) @ (
            Matrix.Diagonal((*ext, 1.0)) if ty == 'Volume' else Matrix.Identity(4))
        if ty != 'Volume':
            obj.matrix_world = root.matrix_world @ Matrix.Translation(pos)
    else:
        obj.parent = root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = Matrix.Translation(pos) @ Matrix.Diagonal((*ext, 1.0))
    c = obj.c2
    c.kind = 'PROBE'
    c.probe_index = k
    c.probe_type = 'Volume' if ty == 'Volume' else 'Environment'
    c.probe_name = pr.get('ProbeName', '')
    c.probe_texture = pr.get('TextureRef', -1)
    c.probe_node = node if node is not root else None
    if 'Density' in pr:
        c.probe_density = list(pr['Density'])[:3]
        lp.resolution_x, lp.resolution_y, lp.resolution_z = [max(1, int(v)) for v in c.probe_density]
    c.probe_fade = pr.get('FadeType', 'Outside')
    if 'OutsideSize' in pr:
        c.probe_outside = list(pr['OutsideSize'])[:3]
    if 'InsideSize' in pr:
        c.probe_has_inside = True
        c.probe_inside = pr['InsideSize']
    c.probe_scene = bool(pr.get('IsSceneProbe', 0))
    if 'AverageLuminance' in pr:
        c.probe_has_luminance = True
        c.probe_luminance = pr['AverageLuminance']
    if 'DirectSH' in pr:
        sh = list(pr['DirectSH'])
        # a Volume probe's SH is its grid, 27 floats per cell (a 1x1x1 volume
        # is 27 floats too); only Environment probes carry one 30-float block
        if ty != 'Volume' and len(sh) <= 30:
            c.probe_has_sh = True
            c.probe_direct_sh = (sh + [0.0] * 30)[:30]
        else:
            obj['c2_direct_sh'] = sh  # Arcade: one 27-float SH cell per grid point
    return obj


# ----------------------------------------------------------------------
# collision
# ----------------------------------------------------------------------

def _surface_material(st):
    """A display material for collision surface type `st` (stable random colour)."""
    name = 'C2_Surface_%d' % st
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        h = (st * 2654435761) & 0xFFFFFF
        m.diffuse_color = (((h >> 16) & 255) / 255.0 * 0.8 + 0.2, ((h >> 8) & 255) / 255.0 * 0.8 + 0.2,
                           (h & 255) / 255.0 * 0.8 + 0.2, 0.5)
    return m


def trilist_mesh(entry, name):
    """Blender mesh for a TriangleList collision entry (btcol entry dict), with
    per-face 'surface_type' / 'surface_color' attributes and one coloured
    material per surface type.
    """
    pos = entry['positions']
    idx = entry['indices'].reshape(-1, 3)
    n = idx.shape[0]
    me = bpy.data.meshes.new(name)
    me.vertices.add(pos.shape[0])
    me.vertices.foreach_set('co', pos.astype(np.float32).ravel())
    me.loops.add(n * 3)
    me.loops.foreach_set('vertex_index', idx.astype(np.int32).ravel())
    me.polygons.add(n)
    me.polygons.foreach_set('loop_start', np.arange(0, n * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(n, 3, np.int32))
    me.update(calc_edges=True)
    fa = entry['face_attrs']
    ai = entry['attr_indices']
    st = np.array([fa[i][0] if i < len(fa) else 0 for i in ai], dtype=np.int64)
    sc_ = np.array([fa[i][1] if i < len(fa) else 0 for i in ai], dtype=np.int64)
    if st.shape[0] != n:
        st = np.resize(st, n)
        sc_ = np.resize(sc_, n)
    to_i32 = lambda a: ((a & 0xFFFFFFFF) ^ 0x80000000).astype(np.int64) - 0x80000000
    me.attributes.new('surface_type', 'INT', 'FACE').data.foreach_set('value', to_i32(st).astype(np.int32))
    me.attributes.new('surface_color', 'INT', 'FACE').data.foreach_set('value', to_i32(sc_).astype(np.int32))
    # one material per surface type for visual identification
    types = sorted(set(st.tolist()))
    for t in types:
        me.materials.append(_surface_material(t))
    lut = {t: k for k, t in enumerate(types)}
    me.polygons.foreach_set('material_index', np.array([lut[t] for t in st.tolist()], np.int32))
    return me


def primitive_mesh(shape, name):
    """Blender mesh shaped like a primitive collision shape (box, sphere,
    cylinder, convex hull, capsule, cone) - game Y-up.
    """
    import bmesh
    st = shape['ShapeType']
    bm = bmesh.new()
    if st in ('Box',):
        e = shape.get('Extent', [1, 1, 1])
        bmesh.ops.create_cube(bm, size=2.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * e[0], v.co.y * e[1], v.co.z * e[2]))
    elif st == 'Sphere':
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=shape.get('Radius', 1.0))
    elif st == 'Cylinder':
        e = shape.get('Extent', [1, 1, 1])
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=1.0, radius2=1.0, depth=2.0)
        for v in bm.verts:  # cone is along Z; game cylinders are along Y
            x, y, z = v.co
            v.co = Vector((x * e[0], z * e[1], y * e[2]))
    elif st == 'ConvexHull':
        pts = shape.get('Points', [])
        for k in range(0, len(pts), 3):
            bm.verts.new(pts[k:k + 3])
        if len(bm.verts) >= 4:
            try:
                bmesh.ops.convex_hull(bm, input=bm.verts[:])
            except Exception:
                pass
    elif st == 'Capsule':
        p1 = Vector(shape.get('Point1', [0, -1, 0]))
        p2 = Vector(shape.get('Point2', [0, 1, 0]))
        r = max(shape.get('Radius1', 0.5), shape.get('Radius2', 0.5))
        d = (p2 - p1).length
        bmesh.ops.create_cone(bm, cap_ends=True, segments=12, radius1=r, radius2=r, depth=max(d, 1e-3))
        rot = (p2 - p1).normalized().to_track_quat('Z', 'Y') if d > 1e-6 else None
        for v in bm.verts:
            co = v.co
            if rot is not None:
                co = rot @ co
            v.co = co + (p1 + p2) * 0.5
    elif st == 'Cone':
        h = shape.get('Height', 1.0)
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=shape.get('Radius', 1.0), radius2=0.0, depth=h)
        for v in bm.verts:
            x, y, z = v.co
            v.co = Vector((x, z, y))
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def import_collision(sc, root, node_objs, col, btm, btd, scene):
    """Create the world's collision objects (Collision collection).

    Each CollisionInstance becomes an object (kind COLLISION) placed by its
    Transform and parented to its scene node; its shape becomes the mesh
    (shared between instances using the same shape). Compound shapes become an
    empty with one child per sub-shape (kind COLSHAPE). Returns the count.
    """
    tree = sc.tree
    shapes = list(tree.get('CollisionShapePool', {}).values())
    insts = list(tree.get('CollisionInstancePool', {}).values())
    if not shapes or not insts:
        return 0
    entries = []
    if btm and os.path.exists(btm) and os.path.exists(btd):
        entries, _ = btcol.read_collision(btm, btd)
    # associations link each instance to its scene node and to its shape
    i2n, i2s = {}, {}
    for a in tree.get('AssociationPool', {}).values():
        if a.get('Type') == 'CollisionInstanceToSceneTreeNode':
            i2n.setdefault(a['CollisionInstanceRef'], a)
        elif a.get('Type') == 'CollisionShapeToCollisionInstance':
            i2s.setdefault(a['CollisionInstanceRef'], a['CollisionShapeRef'])
    mesh_cache = {}

    def shape_mesh(si):
        """Mesh of CollisionShapePool entry si (built once, then shared)."""
        if si in mesh_cache:
            return mesh_cache[si]
        s = shapes[si]
        st = s.get('ShapeType')
        if st == 'TriangleList':
            k = s.get('SerializeIndex', -1)
            if not (0 <= k < len(entries)):
                return None
            e = entries[k]
            me = trilist_mesh(e, 'COLSHAPE_%d' % si)
            me.c2col.serialize_index = k
            me.c2col.btm_type = e.get('itype', 1)
            me.c2col.btm_align = e.get('align', 16)
            me.c2col.geo_hash = btcol.geometry_hash(e['positions'], e['indices'])
            me['c2_bvh'] = _bvh_to_text(e['bvh'])  # original BVH, reused while the shape is unchanged
        else:
            me = primitive_mesh(s, 'COLSHAPE_%d' % si)
            me.c2col.surface_type = s.get('SurfaceType', 0)
            me.materials.append(_surface_material(me.c2col.surface_type))
        me.c2col.shape_type = st
        me.c2col.shape_index = si
        me.c2col.collision_layer = s.get('CollisionLayer', 0)
        extra = {k: v for k, v in s.items() if k not in ('ShapeType', 'SerializeIndex', 'CollisionLayer',
                                                       'SurfaceType', 'Extent', 'Radius', 'Points', 'Shapes')}
        me.c2col.extra_json = json.dumps({k: (list(v) if isinstance(v, list) else v) for k, v in extra.items()})
        mesh_cache[si] = me
        return me

    def style(o):
        """Collision display: orange wireframe."""
        o.display_type = 'WIRE'
        o.show_in_front = False
        o.color = (1.0, 0.4, 0.1, 1.0)

    count = 0
    for ii, inst in enumerate(insts):
        si = i2s.get(ii)
        if si is None or si >= len(shapes):
            continue
        s = shapes[si]
        assoc = i2n.get(ii)
        nm = 'COL_' + (assoc.get('NodeName') if assoc else 'Instance%d' % ii)
        if s.get('ShapeType') == 'Compound':
            obj = bpy.data.objects.new(nm, None)
            obj.empty_display_type = 'CUBE'
            obj.empty_display_size = 0.25
            col.objects.link(obj)
            for ck, child in s.get('Shapes', {}).items():
                me = shape_mesh(child['ShapeRef'])
                if me is None:
                    continue
                co = bpy.data.objects.new('%s_part' % nm, me)
                col.objects.link(co)
                co.parent = obj
                co.matrix_parent_inverse = Matrix.Identity(4)
                co.matrix_basis = g2b(child.get('Transform', octscene.IDENTITY16))
                co.c2.kind = 'COLSHAPE'
                style(co)
            obj['c2_compound_shape'] = si
        else:
            me = shape_mesh(si)
            if me is None:
                continue
            obj = bpy.data.objects.new(nm, me)
            col.objects.link(obj)
            style(obj)
        obj.c2.kind = 'COLLISION'
        obj.c2.col_index = ii
        obj.c2.friction = inst.get('Friction', 0.8)
        obj.c2.restitution = inst.get('Restitution', 0.5)
        parent = node_objs.get(assoc['SceneTreeNodeRef']) if assoc else None
        target = root.matrix_world @ g2b(inst.get('Transform', octscene.IDENTITY16))
        obj.parent = parent or root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_world = target
        count += 1
    return count


def _bvh_to_text(bvh):
    """A parsed BVH block as compressed text (stored on the mesh so an
    unchanged shape is written back with its original tree).
    """
    d = dict(bvh)
    d['unknown2'] = base64.b64encode(d['unknown2']).decode()
    d['nodes_raw'] = [(k, base64.b64encode(b).decode()) for k, b in d['nodes_raw']]
    d['subtrees_raw'] = [base64.b64encode(b).decode() for b in d['subtrees_raw']]
    return base64.b64encode(zlib.compress(json.dumps(d).encode())).decode()


def bvh_from_text(text):
    """Inverse of _bvh_to_text."""
    d = json.loads(zlib.decompress(base64.b64decode(text)).decode())
    d['unknown2'] = base64.b64decode(d['unknown2'])
    d['nodes_raw'] = [(k, base64.b64decode(b)) for k, b in d['nodes_raw']]
    d['subtrees_raw'] = [base64.b64decode(b) for b in d['subtrees_raw']]
    for k in ('aabb_min', 'aabb_max', 'quantization'):
        d[k] = tuple(d[k])
    return d


# ----------------------------------------------------------------------
# meridian / actors
# ----------------------------------------------------------------------

def import_meridian(sc, scene, mer_path, node_objs):
    """Read the .mer: mark locator/instance objects, list the choreographies
    (with anchors, drivers, start catalysts and characters). Returns the graph.
    """
    tree = c2oct.load(mer_path)
    g = meridian.Graph(tree)
    scene.c2.namespace = g.namespace
    refs = []
    snr = sc.tree.get('SceneNodeRefsPool', {})
    for v in snr.values():
        if v.get('Name') == 'MeridianSceneNodeRefs':
            refs = list(v.get('Nodes', []))
    # Locator/Instance nodes point at scene nodes through the
    # MeridianSceneNodeRefs list (SceneNodeIndex = position in that list)
    ts_to_obj = {}
    for i, n in enumerate(g.nodes):
        t = n.get('Type')
        if t in ('LocatorNode', 'InstanceNode') and 0 <= n.get('SceneNodeIndex', -1) < len(refs):
            o = node_objs.get(refs[n['SceneNodeIndex']])
            if o is not None:
                o.c2.meridian = 'LOCATOR' if t == 'LocatorNode' else 'INSTANCE'
                o.c2.mer_index = i
                if 'Transform' in n:
                    ts_to_obj[n['Transform']] = o
    scene.c2.choreos.clear()
    import anchors
    resolver = anchors.Resolver(g)
    for ci in g.of_type('Choreography'):
        n = g.nodes[ci]
        c = scene.c2.choreos.add()
        c.node_index = ci
        c.uuid = meridian.uuid_hex(n.get('Uuid'))
        c.name = c.authored = n.get('AuthoredName', 'Choreography')
        c.choreography_name = n.get('choreography_name', '')
        for f in meridian.CHOREO_FLAGS:
            setattr(c, f, bool(n.get(f, 0)))
        for ref, aname, src in g.choreo_anchors(ci):
            a = c.anchors.add()
            a.name = aname
            a.ref_index = ref
            a.ref_uuid = meridian.uuid_hex(g.nodes[ref].get('Uuid'))
            if src is not None:
                a.locator = ts_to_obj.get(src)
                a.source = g.nodes[src].get('AuthoredName', '')
                if a.locator is None:
                    try:
                        res = resolver.resolve(src)
                    except (RecursionError, KeyError, IndexError):
                        res = None
                    if res is not None:
                        a.resolved = res[1] if res[0] == 'object' else 'a racer (%s)' % res[1]
        drv = []
        for li in g.in_links.get(ci, []):
            l = g.links[li]
            drv.append(g.nodes[l['Origin']].get('AuthoredName', '?'))
        c.drivers = '; '.join(drv)
        c.play_on_start = c.start_exists = bool(g.start_catalysts(ci))
        from . import gameplay
        gameplay.refresh_choreo_actors(scene, c, set(g.choreo_actor_refs(ci)))
    return g


def import_actors(scene, ap_path, objs_by_name, g):
    """Read the .ap into scene.c2.actors (one entry per section)."""
    sections = apfile.load(ap_path)
    scene.c2.actors.clear()
    for name, kvs in sections:
        a = scene.c2.actors.add()
        a.name = name if name is not None else ''
        for k, v in kvs:
            kv = a.kvs.add()
            kv.key = k
            kv.value = '' if v is None else v
    return sections


def mark_helpers(scene, node_objs):
    """Geometry the game never draws (Visible 0, e.g. render-collision ramps,
    and walk-in trigger volumes with ShowTrigger off) is shown as wireframe and
    left out of renders and prelight/probe bakes. Export is unaffected."""
    triggers = {}
    for a in scene.c2.actors:
        kv = {k.key: k.value for k in a.kvs}
        if kv.get('AddComponent_WalkInTrigger', '').lower() == 'true' or \
                kv.get('AddComponent_WalkInTriggerFiltered', '').lower() == 'true':
            triggers[a.name] = kv.get('ShowTrigger', '0') == '1'
    for o in node_objs.values():
        if o.c2.kind != 'GEOMETRY' or o.type != 'MESH':
            continue
        name = o.get('c2_name', o.name)
        # SolidColor has no Standard technique (never drawn in the world pass);
        # shadowonly parts are shadow-caster proxies
        effects = {s.material.c2.effect for s in o.material_slots if s.material}
        proxy = bool(effects) and effects <= {'SolidColor', 'shadowonly'}
        if not o.c2.visible or (name in triggers and not triggers[name]) or proxy:
            o.display_type = 'WIRE'
            o.hide_render = True
            o['c2_helper'] = 'trigger' if name in triggers else (
                'shadow' if effects == {'shadowonly'} else 'hidden')


SKY_EFFECTS = ('skyd',)
WATER_EFFECTS = ('wtr', 'wtrc', 'wtrw')
_COLLISION_NAME = re.compile(r'(^|[_:])(col|coll+s?|collision|barrier|block|blocker|wall|ramp|ground|floor|'
                             r'kill|death|reset)', re.I)


def sort_geometry(scene):
    """Split the world's Geometry collection so it holds only what the game
    draws: sky domes -> Sky, water -> Water, and the meshes it never draws
    (mark_helpers) -> Helper Geometry: collision stand-ins, shadow-caster
    proxies and other hidden helpers (dialog / probe / sound markers ...).
    Visible-0 meshes with real materials stay: they are world geometry the
    game shows through its logic. Collections never affect export."""
    from . import gameplay
    cols = gameplay.collections(scene)
    geo = cols.get('Geometry')
    if geo is None:
        return
    for o in list(geo.objects):
        if o.type != 'MESH' or len(o.users_collection) != 1:
            continue
        effects = {s.material.c2.effect for s in o.material_slots if s.material}
        prefixes = {e.split('_')[0] for e in effects}
        helper = o.get('c2_helper')
        key = None
        if helper == 'shadow':
            key = 'Shadow'
        elif helper == 'hidden' and effects and effects <= {'SolidColor', 'shadowonly'}:
            key = 'ColHelpers' if _COLLISION_NAME.search(o.get('c2_name', o.name)) else 'Hidden'
        elif effects and prefixes <= set(SKY_EFFECTS):
            key = 'Sky'
        elif effects and prefixes <= set(WATER_EFFECTS):
            key = 'Water'
        if key is not None:
            gameplay.move_to(o, cols[key])


def collapse_outliner():
    """Fold every Outliner tree so a fresh import shows the world's
    collections closed (Blender opens new ones): everything folded, then two
    levels opened back up: Scene Collection > C2 <world> > its collections,
    each closed."""
    def outliners():
        wm = bpy.context.window_manager
        for win in wm.windows if wm else ():
            for area in win.screen.areas:
                if area.type == 'OUTLINER':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                    if region is not None:
                        yield win, area, region

    def fold():
        for win, area, region in outliners():
            try:
                with bpy.context.temp_override(window=win, area=area, region=region):
                    for _ in range(8):
                        bpy.ops.outliner.show_one_level(open=False)
                    for _ in range(2):
                        bpy.ops.outliner.show_one_level(open=True)
            except RuntimeError as e:
                print('Cars 2 Scene: outliner: %s' % e)
            area.tag_redraw()
        return None

    # fold once an Outliner has drawn its rebuilt tree: the first draw after
    # the import still shows the old one, so wait for the second. Operators
    # can't run inside a draw callback, so it hands over to a timer
    handle = []
    draws = [0]

    def drawn():
        draws[0] += 1
        if draws[0] == 1:
            bpy.app.timers.register(retag, first_interval=0.2)
        elif handle:
            bpy.types.SpaceOutliner.draw_handler_remove(handle.pop(), 'WINDOW')
            bpy.app.timers.register(fold, first_interval=0.0)

    def retag():
        for _win, area, _region in outliners():
            area.tag_redraw()
        return None
    if not bpy.app.background:
        handle.append(bpy.types.SpaceOutliner.draw_handler_add(drawn, (), 'WINDOW', 'POST_PIXEL'))
        for _win, area, _region in outliners():
            area.tag_redraw()


def _same16(a, b, tol=1e-4):
    return len(a) == len(b) and all(abs(x - y) <= tol * max(1.0, abs(y)) for x, y in zip(a, b))


def store_choreo_places(scene, mer_tree):
    """Replay the world's logic per route and mode (core/choreoplace.py) and
    keep where each choreography is played: scene['c2_choreo_places'] =
    {route: {mode: {choreography node index: [anchor object name, ...]}}}."""
    import choreoplace
    import meridian
    agents = {}
    try:
        data, spawned = choreoplace.simulate(mer_tree)
        data = {v: {m: {str(ci): pl for ci, pl in d.items()} for m, d in md.items()} for v, md in data.items()}
        # spawned map objects (objplace.py), by agent UUID: node indices change when the graph is edited
        g = meridian.Graph(mer_tree)
        uid = {i: bytes(n['Uuid']).hex() for i, n in enumerate(g.nodes) if isinstance(n.get('Uuid'), (bytes, bytearray))}
        agents = {v: {m: {uid[a]: pl for a, pl in d.items() if a in uid} for m, d in md.items()}
                  for v, md in spawned.items()}
        # when actors spawn (spline followers come again and again: objplace.py)
        timed = {v: {m: {uid[a]: l for a, l in d.items() if a in uid and l and g.nodes[a].get('Type') == 'SpawnActorNode'}
                     for m, d in md.items()} for v, md in choreoplace.spawn_times(mer_tree).items()}
        # spawners only the clock reaches (a Pulse turned on by a Switch) spawn there too
        for v, md in timed.items():
            for m, d in md.items():
                for u, l in d.items():
                    have = agents.setdefault(v, {}).setdefault(m, {}).setdefault(u, [])
                    have += [x for x in dict.fromkeys(e[0] for e in l) if x not in have]
    except Exception as e:          # never let a graph oddity stop the import
        print('Cars 2 Scene: choreography placement not resolved: %s' % e)
        data, timed = {}, {}
    scene['c2_choreo_places'] = json.dumps(data)
    scene['c2_agent_places'] = json.dumps(agents)
    scene['c2_agent_times'] = json.dumps(timed)


def link_existing_triggers(scene, g, node_objs):
    """Record walk-in triggers whose enter/exit event already plays a
    choreography directly (shown in the trigger and choreography panels)."""
    by_name = {}
    for o in node_objs.values():
        by_name.setdefault(o.get('c2_name', o.name), o)
    choreo_by_index = {c.node_index: c for c in scene.c2.choreos}
    for a in scene.c2.actors:
        u = next((kv.value for kv in a.kvs if kv.key in ('WalkInTrigger_Uuid', 'WalkInTriggerFiltered_Uuid')), None)
        obj = by_name.get(a.name)
        gi = g.find_uuid(u) if u else None
        if obj is None or gi is None:
            continue
        ports = g.trigger_ports(gi)
        for ev, key in (('ENTER', 'begin'), ('EXIT', 'end')):
            src = ports.get(key)
            if src is None:
                continue
            for tgt in g.link_targets(src, 'Impulse'):
                c = choreo_by_index.get(tgt)
                if c is not None:
                    t = c.triggers.add()
                    t.obj = obj
                    t.event = ev
                    t.exists = True


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def import_scene(context, filepath, do_collision=True, do_meridian=True, do_textures=True):
    """Import a world .oct (+ side files) into the current scene. Returns the root."""
    from . import fxnodes
    fxnodes.SUSPEND[0] += 1   # no live-edit callbacks while the scene is built
    try:
        return _import_scene(context, filepath, do_collision, do_meridian, do_textures)
    finally:
        fxnodes.SUSPEND[0] -= 1


def _import_scene(context, filepath, do_collision=True, do_meridian=True, do_textures=True):
    """The import itself (import_scene wraps it with live updates suspended).

    Order: root + collections, textures, materials, one object per scene
    node (then parent them), light sets / rigs / probes, collision, Meridian,
    actors, gameplay data (track, paths, nav meshes, spawns), Game Look.
    """
    t0 = time.time()
    scene = context.scene
    sc = octscene.OctScene(filepath)
    world = sc.name
    base = os.path.join(sc.folder, world)
    main = common.ensure_collection('C2 ' + world)
    cols = {k: common.ensure_collection('%s %s' % (world, k), main)
            for k in ('Geometry', 'Lights', 'Nodes', 'Collision', 'Probes')}

    scene.c2.oct_path = filepath
    scene.c2.world_name = world
    is_arc = arcade.is_arcade(sc)
    scene.c2.platform = 'ARCADE' if is_arc else 'PC'
    matnodes.lighting_group()
    root = bpy.data.objects.new('C2_' + world, None)
    root.empty_display_type = 'ARROWS'
    root.empty_display_size = 5.0
    main.objects.link(root)
    root.matrix_world = common.ROOT_MATRIX
    root.c2.kind = 'ROOT'
    root.c2.node_index = 0
    scene.c2.root = root

    images = extract_textures(sc, scene, base + '.oct.textures') if do_textures else {}
    game = None
    if is_arc and do_textures:
        import choreo
        assets = bpy.path.abspath(scene.c2.assets_dir) if scene.c2.assets_dir else choreo.assets_dir_from(filepath)
        game = game_context(sc, scene, images, base + '.oct.textures', assets)
        if game is None:
            print('Cars 2 Scene: game effects not found - Game Look uses simplified materials '
                  '(set the Game Assets Folder and re-import)')
    mats = import_materials(sc, images, is_arc, game)
    for m, src in zip(mats, sc.materials):
        if src.get('Effect') == 'scene_settings':
            scene.c2.scene_settings = m
            m.use_fake_user = True
            break

    # one object per scene node (SubGeometry parts are inside their Geometry
    # node's mesh). node_objs: scene node index -> object
    node_objs = {0: root}
    for i, n in enumerate(sc.nodes):
        t = n.get('Type', '')
        if i == 0 or octscene.is_subgeo(n):
            continue
        name = n.get('NodeName', '%s%d' % (t, i))
        obj = None
        if t == 'Geometry':
            me = build_geometry_mesh(sc, i, name, mats)
            obj = bpy.data.objects.new(name, me)
            cols['Geometry'].objects.link(obj)
            obj.c2.kind = 'GEOMETRY'
            obj.c2.visible = bool(n.get('Visible', 1))
            if me is None:
                obj.empty_display_type = 'CUBE'
                obj.empty_display_size = 0.5
        elif t == 'Light':
            obj = bpy.data.objects.new(name, make_light(n, name))
            cols['Lights'].objects.link(obj)
            obj.c2.kind = 'LIGHT'
        elif t == 'Camera':
            obj = bpy.data.objects.new(name, make_camera(n, name))
            cols['Nodes'].objects.link(obj)
            obj.c2.kind = 'CAMERA'
            obj['c2_camera'] = json.dumps({k: v for k, v in n.items() if isinstance(v, (int, float, str))})
        else:
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_type = 'PLAIN_AXES'
            obj.empty_display_size = 1.0
            cols['Nodes'].objects.link(obj)
            obj.c2.kind = 'NODE'
            obj['c2_node_type'] = t
        obj.c2.node_index = i
        obj['c2_name'] = name
        if t == 'Geometry' and me is not None:
            amb = [j for j in sc.subgeos(i) if sc.nodes[j].get('Type') == 'SubGeometryAmbient']
            if amb:     # a light volume (ambientvol.py)
                from . import ambientvol
                mref = sc.nodes[amb[0]].get('MaterialReference', 0)
                ambientvol.fill(obj, sc.nodes[amb[0]], sc.materials[mref] if mref < len(sc.materials) else {})
        obj.c2.display_layer = n.get('DisplayLayer', -1)
        obj.c2.detached = not n.get('ParentNodeReferences')
        node_objs[i] = obj

    # parent and place them: the game's local matrix becomes matrix_basis
    # (matrix_parent_inverse = identity, so local really means local)
    for i, obj in node_objs.items():
        if i == 0:
            continue
        n = sc.nodes[i]
        p = sc.parent(i)
        obj.parent = node_objs.get(p, root) if p is not None else root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = g2b(n.get('LocalToParentMatrix', octscene.IDENTITY16))
    context.view_layer.update()
    # fingerprint every mesh now: on export, unchanged meshes are copied as is
    for i, obj in node_objs.items():
        if obj.type == 'MESH' and obj.c2.kind == 'GEOMETRY':
            obj.c2.mesh_hash = common.mesh_hash(obj)
        # a matrix with shear can't be held as loc/rot/scale: remember what
        # Blender made of it, so an untouched object keeps the file's matrix
        if i:
            src = sc.nodes[i].get('LocalToParentMatrix')
            if src is not None and not _same16(common.b2g(obj.matrix_basis), src):
                obj['c2_basis0'] = [x for row in obj.matrix_basis for x in row]

    # light sets / rigs / probes
    scene.c2.light_sets.clear()
    for k, ls in enumerate(sc.tree.get('LightSetPool', {}).values()):
        it = scene.c2.light_sets.add()
        it.index = k
        it.name = 'LightSet %d' % k
        for key in ('SpotLights', 'AmbientLights', 'DirectionalLights'):
            for r in ls.get(key, []):
                if r in node_objs:
                    it.lights.add().obj = node_objs[r]
    scene.c2.light_rigs.clear()
    for k, rig in enumerate(sc.tree.get('LightRigPool', {}).values()):
        it = scene.c2.light_rigs.add()
        it.index = k
        it.name = it.rig_name = rig.get('RigName', 'Rig%d' % k)
        it.type_name = rig.get('TypeName', '')
        it.position = list(rig.get('Position', [0, 0, 0]))[:3]
        sh = list(rig.get('LightRigConsts', []))
        it.sh = (sh + [0.0] * 27)[:27]
        it.sh_order = rig.get('SH Order', 3)
        it.scene_rig = rig.get('SceneRig', 1)
        for r in rig.get('LightRefs', []):
            if r in node_objs:
                it.lights.add().obj = node_objs[r]
    for k, pr in enumerate(sc.tree.get('LightProbePool', {}).values()):
        import_probe(sc, root, node_objs, cols['Probes'], k, pr)
    context.view_layer.update()

    ncol = 0
    if do_collision:
        ncol = import_collision(sc, root, node_objs, cols['Collision'], base + '.btm', base + '.btd', scene)
        scene.c2.has_col = os.path.exists(base + '.btm')
        context.view_layer.update()
        for o in cols['Collision'].objects:
            if o.type == 'MESH' and 'c2_hash' not in o.data:
                o.data['c2_hash'] = common.col_mesh_hash(o.data)
        # collision fingerprint: unchanged on export = original .btm/.btd copied
        scene.c2['col_sig'] = common.collision_signature(root)
        # dense wireframes hide the world; Collision panel > Show/Hide Collision brings them back
        from .ops import _find_layer_collection
        lc = _find_layer_collection(context.view_layer.layer_collection, cols['Collision'].name)
        if lc is not None and ncol:
            lc.hide_viewport = True
        cols['Collision'].hide_render = True  # never part of renders / bakes

    g = None
    scene.c2.has_mer = do_meridian and os.path.exists(base + '.mer') and os.path.getsize(base + '.mer') > 64
    from . import trackvariant
    for key in ('c2_variants', 'c2_choreo_places', 'c2_agent_places', 'c2_agent_times', 'c2_spawn_splits'):
        if key in scene:
            del scene[key]
    scene.c2['track_variant'] = 0           # 'All' (raw: no update while importing)
    scene.c2['track_mode'] = 0
    if scene.c2.has_mer:
        g = import_meridian(sc, scene, base + '.mer', node_objs)
        trackvariant.store(scene, g.tree)     # routes A / B / C (Track Variant in the Scene panel)
        store_choreo_places(scene, g.tree)    # where each choreography plays, per route and mode
    scene.c2.has_ap = os.path.exists(base + '.ap')
    if scene.c2.has_ap:
        import_actors(scene, base + '.ap', None, g)
        if g is not None:
            link_existing_triggers(scene, g, node_objs)
    mark_helpers(scene, node_objs)
    from . import gameplay
    gameplay.import_all(context, scene, sc, base, node_objs, g)
    sort_geometry(scene)
    if g is not None:
        # bashables, breakables and pickups the logic spawns, at their spawn points
        from . import objplace
        try:
            objplace.build(context, scene, g)
        except Exception as e:      # never let this stop the import
            import traceback
            traceback.print_exc()
            print('Cars 2 Scene: map objects not placed: %s' % e)
        # the grandstand crowds (CrowdMemberPlane nodes: sprite cars)
        from . import crowd
        try:
            crowd.build(scene, g)
        except Exception:
            import traceback
            traceback.print_exc()
        # Collect mode's gates between their posts
        from . import collectgates
        try:
            collectgates.build(context, scene, g)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('Cars 2 Scene: collect gates not shown: %s' % e)
        # Battle Race / Attack / Survival collections: what only one mode uses
        from . import modecols
        try:
            modecols.organize(scene)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('Cars 2 Scene: mode collections not organized: %s' % e)
    if game is not None:
        from . import fxnodes
        scene.render.engine = 'C2_GAME'       # Rendered view / F12: the game's shaders (gamerender)
        fxnodes.sync_engine(scene)
        scene.c2.viewport_lighting = 'GAME'   # open Arcade worlds in the game's own shading
        from . import gamerender
        gamerender.RENDERER.clear()

    if not scene.c2.export_dir:
        scene.c2.export_dir = os.path.join(sc.folder, 'c2_export') + os.sep
    print('Cars 2 Scene: imported %s (%s) - %d nodes, %d materials, %d collision instances in %.1fs'
          % (world, scene.c2.platform, len(node_objs), len(mats), ncol, time.time() - t0))
    collapse_outliner()
    return root


class IMPORT_OT_cars2_scene(bpy.types.Operator, ImportHelper):
    """Import a Cars 2 world (.oct)"""
    bl_idname = 'import_scene.cars2_scene'
    bl_label = 'Import Cars 2 Scene'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.oct'
    filter_glob: StringProperty(default='*.oct', options={'HIDDEN'})
    collision: BoolProperty(name='Collision (.btm/.btd)', default=True)
    meridian_: BoolProperty(name='Meridian + Actors (.mer/.ap)', default=True)
    textures: BoolProperty(name='Extract Textures', default=True,
                           description='Write the .oct\'s textures to <world>.oct.textures next to it and load them')

    def execute(self, context):
        if context.scene.c2.root is not None:
            self.report({'ERROR'}, 'This Blender scene already holds a Cars 2 world; use a new scene')
            return {'CANCELLED'}
        try:
            import_scene(context, self.filepath, self.collision, self.meridian_, self.textures)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, 'Import failed: %s' % e)
            return {'CANCELLED'}
        return {'FINISHED'}


def menu_import(self, context):
    """File > Import menu entry."""
    self.layout.operator(IMPORT_OT_cars2_scene.bl_idname, text='Cars 2 Scene (.oct)')


def register():
    bpy.utils.register_class(IMPORT_OT_cars2_scene)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.utils.unregister_class(IMPORT_OT_cars2_scene)


fill_material_props = matio.fill_material_props     # older callers
