"""Mesh data in the game's terms, shared by the exporter and the Game Look
renderer (Cars 2 Scene Editor and the Cars 2 Toolkit's copy of its material
system): per material slot and sub-geometry, triangle corners with their
positions, normals, UV sets, colour sets and tangent frames.
"""
import bpy
import numpy as np
from mathutils import Matrix


class Report:
    """Collects the export log (shown as the Cars2SceneExportReport text).
    warnings counts problems; left_out counts meshes that were not exported.
    """
    def __init__(self):
        """Empty report."""
        self.lines = []
        self.warnings = 0
        self.left_out = 0

    def info(self, msg):
        """Log a line."""
        self.lines.append(msg)

    def warn(self, msg):
        """Log a problem (counted, prefixed WARNING)."""
        self.warnings += 1
        self.lines.append('WARNING: ' + msg)


def extract_groups(obj, depsgraph, report, binormal_sign=1.0, force_eval=False):
    """Triangulated per-corner data grouped by (material slot, c2_subgeo).
    Tangents come from Uv1 (Arcade stores Binormal = -(N x T) * sign, so it
    passes binormal_sign=-1). force_eval: read the evaluated mesh even
    without modifiers (the Game render engine: shows Edit Mode changes)."""
    use_eval = force_eval or any(m.show_viewport for m in obj.modifiers)
    if use_eval:
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
    else:
        me = obj.data
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    if nt == 0:
        if use_eval:
            obj.evaluated_get(depsgraph).to_mesh_clear()
        return []
    tri_loops = np.empty(nt * 3, np.int32)
    me.loop_triangles.foreach_get('loops', tri_loops)
    tri_poly = np.empty(nt, np.int32)
    me.loop_triangles.foreach_get('polygon_index', tri_poly)
    nl = len(me.loops)
    loop_v = np.empty(nl, np.int32)
    me.loops.foreach_get('vertex_index', loop_v)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3)
    pm = np.empty(len(me.polygons), np.int32)
    me.polygons.foreach_get('material_index', pm)
    sub_attr = me.attributes.get('c2_subgeo')
    if sub_attr is not None and sub_attr.domain == 'FACE':
        ps = np.empty(len(me.polygons), np.int32)
        sub_attr.data.foreach_get('value', ps)
    else:
        ps = np.full(len(me.polygons), -1, np.int32)
    try:
        cn = np.empty(nl * 3, np.float32)
        me.corner_normals.foreach_get('vector', cn)
        cn = cn.reshape(-1, 3)
    except AttributeError:
        me.calc_normals_split()
        cn = np.empty(nl * 3, np.float32)
        me.loops.foreach_get('normal', cn)
        cn = cn.reshape(-1, 3)
    uvs = {}
    for uv in me.uv_layers:
        a = np.empty(nl * 2, np.float32)
        uv.data.foreach_get('uv', a)
        a = a.reshape(-1, 2).copy()
        a[:, 1] = 1.0 - a[:, 1]
        uvs[uv.name] = a
    cols = {}
    for ca in me.color_attributes:
        a = np.empty(len(ca.data) * 4, np.float32)
        ca.data.foreach_get('color_srgb' if ca.data_type == 'BYTE_COLOR' else 'color', a)
        a = a.reshape(-1, 4)
        if ca.domain == 'POINT':
            a = a[loop_v]
        elif ca.domain != 'CORNER':
            continue
        cols[ca.name] = a
    # tangent frames per UV set: Tangent/Binormal(1) from Uv1, Tangent2/Binormal2
    # from Uv2, TangentMod/BinormalMod from UvMod (Arcade a_2_ / UvMod effects)
    frames = {}
    for uvname in [n for n in ('Uv1', 'Uv2', 'UvMod') if n in uvs] or list(uvs)[:1]:
        try:
            me.calc_tangents(uvmap=uvname)
        except RuntimeError:
            continue
        t = np.empty(nl * 3, np.float32)
        me.loops.foreach_get('tangent', t)
        sgn = np.empty(nl, np.float32)
        me.loops.foreach_get('bitangent_sign', sgn)
        t = t.reshape(-1, 3)
        frames[uvname] = (t, np.cross(cn, t) * (sgn * binormal_sign)[:, None])
    if frames and 'Uv1' not in frames:
        frames['Uv1'] = next(iter(frames.values()))
    slots = [s.material for s in obj.material_slots]
    corners = tri_loops.reshape(nt, 3)
    key_mat = pm[tri_poly]
    key_sub = ps[tri_poly]
    keys = np.stack([key_mat, key_sub], 1)
    ukeys, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    out = []
    for gi, (mslot, sub) in enumerate(ukeys.tolist()):
        tri_sel = corners[inv == gi]
        lsel = tri_sel.ravel()
        out.append({
            'material': slots[mslot] if 0 <= mslot < len(slots) else None,
            'subgeo': int(sub),
            'corners': lsel,
            'vidx': loop_v[lsel],           # corner -> vertex (gamerender's deform-only refresh)
            'co': co[loop_v[lsel]],
            'normal': cn[lsel],
            'uvs': {k: v[lsel] for k, v in uvs.items()},
            'colors': {k: v[lsel] for k, v in cols.items()},
            'frames': {k: (t[lsel], b[lsel]) for k, (t, b) in frames.items()},
        })
    if use_eval:
        obj.evaluated_get(depsgraph).to_mesh_clear()
    return out


MIRROR_X = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
