"""Shared Blender-side helpers: coordinate conversion, mesh hashing, lookups.

Coordinates: the game is Y-up with row-vector matrices (v' = v @ M);
Blender is Z-up with column-vector matrices (v' = M @ v). An imported world
lives under one empty, the "root" (Object kind ROOT), rotated +90 deg about X.
Every object below the root therefore uses the game's own numbers locally,
and g2b/b2g only need to transpose a matrix - no axis swapping anywhere else.
"""
import hashlib
import math

import bpy
import numpy as np
from mathutils import Matrix

# Root object rotation: game is Y-up, Blender Z-up. Every scene object hangs
# under the root, so object-local matrices are exactly the game's matrices.
ROOT_MATRIX = Matrix.Rotation(math.radians(90.0), 4, 'X')


def g2b(flat16):
    """Game row-vector matrix (16 floats) -> Blender column-vector Matrix."""
    m = np.asarray(flat16, dtype=np.float64).reshape(4, 4)
    return Matrix(m.T.tolist())


def b2g(mat):
    """Blender Matrix -> game row-vector flat list of 16 floats."""
    a = np.array(mat, dtype=np.float64)  # rows of the Blender matrix
    return [float(x) for x in a.T.reshape(16)]


def game_matrix(root, obj):
    """Object's matrix in game world space (Blender column form)."""
    return root.matrix_world.inverted() @ obj.matrix_world


def scene_root(context_or_scene):
    """The active Cars 2 world root of a scene (or a context), or None."""
    sc = getattr(context_or_scene, 'scene', context_or_scene)
    return sc.c2.root if sc and sc.c2.root else None


def descendants(root):
    """Every object below `root` (children, grandchildren ...). Only these
    are part of the Cars 2 world and get exported.

    Object.children scans every object in the file each time it is read, so
    walking it is quadratic (1.7 s for tokyo_b's 5400 objects). One pass
    builds the parent -> children map instead, in the database order
    Object.children uses, so the result (and the export order) is unchanged."""
    import bpy
    kids = {}
    for o in bpy.data.objects:
        p = o.parent
        if p is not None:
            kids.setdefault(p.as_pointer(), []).append(o)
    out = []
    stack = list(kids.get(root.as_pointer(), ()))
    while stack:
        o = stack.pop()
        out.append(o)
        stack.extend(kids.get(o.as_pointer(), ()))
    return out


def node_parent(obj, root):
    """Nearest ancestor that is a scene node (or the root) -> object or None."""
    # helper objects in between (e.g. plain empties) are skipped: the game
    # parent is the nearest object that becomes a scene node itself
    p = obj.parent
    while p is not None:
        if p == root or p.c2.kind in ('NODE', 'GEOMETRY', 'LIGHT', 'CAMERA'):
            return p
        p = p.parent
    return None


def mesh_hash(obj):
    """Hash of everything the exporter writes for a Geometry mesh object.

    Stored on import (obj.c2.mesh_hash). On export, an unchanged hash means
    the mesh's original vertex/index data is copied byte-for-byte instead of
    being re-encoded, so untouched meshes stay exactly as shipped."""
    me = obj.data
    h = hashlib.sha1()
    # vertex positions
    n = len(me.vertices)
    co = np.empty(n * 3, np.float32)
    me.vertices.foreach_get('co', co)
    h.update(co.tobytes())
    # face corners -> vertices, face sizes, material per face
    nl = len(me.loops)
    lv = np.empty(nl, np.int32)
    me.loops.foreach_get('vertex_index', lv)
    h.update(lv.tobytes())
    npoly = len(me.polygons)
    tot = np.empty(npoly, np.int32)
    me.polygons.foreach_get('loop_total', tot)
    h.update(tot.tobytes())
    mi = np.empty(npoly, np.int32)
    me.polygons.foreach_get('material_index', mi)
    h.update(mi.tobytes())
    # UV maps and colour layers (by name and content)
    for uv in me.uv_layers:
        a = np.empty(nl * 2, np.float32)
        uv.data.foreach_get('uv', a)
        h.update(uv.name.encode())
        h.update(a.tobytes())
    for ca in me.color_attributes:
        h.update(ca.name.encode())
        a = np.empty(len(ca.data) * 4, np.float32)
        ca.data.foreach_get('color_srgb' if ca.data_type == 'BYTE_COLOR' else 'color', a)
        h.update(a.tobytes())
    # normals (rounded so tiny float noise doesn't count as an edit)
    try:
        cn = np.empty(nl * 3, np.float32)
        me.corner_normals.foreach_get('vector', cn)
        h.update(np.round(cn, 5).tobytes())
    except AttributeError:
        pass
    for slot in obj.material_slots:
        h.update((slot.material.name if slot.material else '').encode())
    # which SubGeometry part each face came from
    sub = me.attributes.get('c2_subgeo')
    if sub is not None:
        a = np.empty(len(sub.data), np.int32)
        sub.data.foreach_get('value', a)
        h.update(a.tobytes())
    # adding/removing a visible modifier changes the exported mesh too
    h.update(str(len([m for m in obj.modifiers if m.show_viewport])).encode())
    return h.hexdigest()


def col_mesh_hash(me):
    """Hash of what a collision shape exports from its mesh."""
    h = hashlib.sha1()
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', co)
    h.update(co.tobytes())
    lv = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get('vertex_index', lv)
    h.update(lv.tobytes())
    # per-face surface type / colour
    for nm in ('surface_type', 'surface_color'):
        a = me.attributes.get(nm)
        if a is not None and a.domain == 'FACE':
            v = np.empty(len(a.data), np.int32)
            a.data.foreach_get('value', v)
            h.update(v.tobytes())
    c = me.c2col
    h.update(('%s|%d|%d' % (c.shape_type, c.surface_type, c.collision_layer)).encode())
    return h.hexdigest()


def collision_signature(root):
    """Changes whenever any collision object, its placement or its shape changes."""
    # if it equals the value stored at import, the original .btm/.btd are
    # copied unchanged on export
    items = []
    for o in descendants(root):
        if o.c2.kind not in ('COLLISION', 'COLSHAPE'):
            continue
        m = tuple(round(v, 4) for row in o.matrix_world for v in row)
        mh = (o.data.name, col_mesh_hash(o.data)) if o.type == 'MESH' else None
        items.append(repr((o.name, o.c2.kind, o.c2.col_index, m, mh, round(o.c2.friction, 6),
                           round(o.c2.restitution, 6), o.parent.name if o.parent else '')))
    items.sort()
    return hashlib.sha1('\n'.join(items).encode()).hexdigest()


def ensure_collection(name, parent=None):
    """The collection called `name`, created under `parent` (or the scene) if missing."""
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    return col


def remove_ids(objects=(), collections=(), data=True):
    """Delete objects and collections in one batch (and, with data, the object
    data they leave unused). bpy.data.objects.remove is ~4.4 ms per object in a
    5,000-object world, a batch ~0.16 ms: every removal of more than a handful
    goes through here."""
    objects = [o for o in objects if o is not None]
    datas = {o.data for o in objects if o.data is not None} if data else set()
    ids = list({id(x): x for x in list(objects) + list(collections)}.values())
    if ids:
        bpy.data.batch_remove(ids)
    unused = [d for d in datas if d.users == 0]
    if unused:
        bpy.data.batch_remove(unused)


def objects_by_node(root):
    """{scene node index: Blender object} for every imported node."""
    out = {}
    for o in [root] + descendants(root):
        if o.c2.kind in ('NODE', 'GEOMETRY', 'LIGHT', 'CAMERA', 'ROOT') and o.c2.node_index >= 0:
            out[o.c2.node_index] = o
    return out


def game_assets(scene):
    """The game's assets folder: the Game Assets Folder setting, else found from
    where the source world or the export folder sits (.../assets/worlds/...).
    None when neither is inside the game (e.g. a source copy kept elsewhere)."""
    import os
    if scene.c2.assets_dir:
        p = bpy.path.abspath(scene.c2.assets_dir)
        if os.path.isdir(p):
            return p
    # 'x' stands for a file inside the folder, so the search starts from the
    # folder itself
    for path in (scene.c2.oct_path, scene.c2.export_dir):
        if path:
            p = assets_dir_from(os.path.join(bpy.path.abspath(path), 'x'))
            if p:
                return p
    return None


def assets_dir_from(path):
    """.../assets/worlds/<w>/<w>.oct -> .../assets: the first folder above a
    file named 'assets' or holding choreographies/ (choreo.assets_dir_from,
    here so the Game Look code doesn't need the choreography reader)."""
    import os
    p = os.path.abspath(path)
    for _ in range(6):
        p = os.path.dirname(p)
        if os.path.basename(p).lower() == 'assets' or os.path.isdir(os.path.join(p, 'choreographies')):
            return p
    return None
