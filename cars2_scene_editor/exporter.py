"""File > Export > Cars 2 Scene.

The original .oct/.mer/.ap/.btm/.btd are re-read from disk and patched with
what is in Blender, so everything this add-on does not model (subnetworks,
tracks, nav meshes, sound sources ...) passes through untouched.

* Untouched meshes keep their original vertex/index bytes (hash check).
* Edited meshes are re-encoded with their SubGeometry's original stream
  layout (same element types the shader expects).
* Deleted objects are tombstoned instead of removed (Geometry hidden and its
  parts made degenerate, lights zeroed) so no node index anywhere shifts.
* Collision is rebuilt from the Blender objects every export.
"""
import copy
import json
import os
import re
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, StringProperty
from mathutils import Matrix, Vector

import apfile
import arcade
import btcol
import c2headers
import c2oct
import dds
import meridian
import octscene
import vertexfmt as vf
from c2oct import FloatList, IntList, StrList
from . import common, gameplay, matio
from .common import b2g, g2b
from .meshdata import Report, extract_groups   # noqa: F401  (shared with the Game Look renderer)
from .matio import image_to_dds, image_to_dds_white   # noqa: F401

NODE_KINDS = ('NODE', 'GEOMETRY', 'LIGHT', 'CAMERA')
MAX_VERTS = 0xFFFF


def export_name(obj):
    """NodeName for an object: the imported name unless the user renamed it
    (Blender's .001 suffixes and 63-char truncation don't count)."""
    orig = obj.get('c2_name')
    if orig:
        base = re.sub(r'\.\d{3}$', '', obj.name)
        if obj.name == orig or orig.startswith(base):
            return orig
    return obj.name


def flist(v):
    """Any sequence -> c2oct FloatList (a float array in the file)."""
    return FloatList(float(x) for x in v)


def close(a, b, tol=1e-5):
    """4x4 (16 floats) comparison, tolerance relative to each row's magnitude."""
    if a is None or len(a) != len(b):
        return False
    for r in range(0, len(a), 4):
        mag = max(1.0, max(abs(v) for v in b[r:r + 4]))
        if any(abs(x - y) > tol * mag for x, y in zip(a[r:r + 4], b[r:r + 4])):
            return False
    return True


# ----------------------------------------------------------------------
# mesh extraction
# ----------------------------------------------------------------------

def bake_group(g, K):
    """Move a group's mesh data into the frame of a node whose matrix got K
    appended (vertices = K^-1 @ v), reversing each triangle when K mirrors."""
    Ki = K.inverted()
    L = np.array(Ki.to_3x3(), np.float64)
    t = np.array(Ki.translation, np.float64)
    nrm_m = np.linalg.inv(L)          # row vectors: n @ L^-1 == (L^-T n)^T

    def unit(a):
        return (a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)).astype(np.float32)

    def rev(a):
        a = np.asarray(a)
        if np.linalg.det(L) >= 0:
            return a
        return a.reshape(-1, 3, *a.shape[1:])[:, ::-1].reshape(a.shape)
    g['corners'] = rev(g['corners'])
    g['co'] = rev((np.asarray(g['co'], np.float64) @ L.T + t).astype(np.float32))
    g['normal'] = rev(unit(np.asarray(g['normal'], np.float64) @ nrm_m))
    g['uvs'] = {k: rev(v) for k, v in g['uvs'].items()}
    g['colors'] = {k: rev(v) for k, v in g['colors'].items()}
    g['frames'] = {k: (rev(unit(np.asarray(a, np.float64) @ L.T)), rev(unit(np.asarray(b, np.float64) @ L.T)))
                   for k, (a, b) in g['frames'].items()}


def weld(group, layout):
    """Per-corner arrays -> unique vertices + indices, keeping only the
    attributes the layout stores. Splits into <=65535-vertex chunks."""
    names = [n for (_, elems) in layout for (n, _, _) in elems]
    cols = [group['co']]
    attr_slices = {}
    off = 3
    attr_src = {'Position': group['co']}
    if 'Normal' in names:
        attr_src['Normal'] = group['normal']
    for n in names:
        if n in ('Position', 'Normal'):
            continue
        if n in group['uvs']:
            attr_src[n] = group['uvs'][n]
        elif n.startswith('Uv') or n.endswith('UV'):
            if group['uvs']:
                attr_src[n] = group['uvs'].get('Uv1', next(iter(group['uvs'].values())))
        elif n in group['colors']:
            attr_src[n] = group['colors'][n]
        elif (n.startswith('Tangent') or n.startswith('Binormal')) and group['frames']:
            uvname = {'2': 'Uv2', 'Mod': 'UvMod'}.get(n.replace('Tangent', '').replace('Binormal', ''), 'Uv1')
            t, b = group['frames'].get(uvname, group['frames']['Uv1'])
            attr_src[n] = t if n.startswith('Tangent') else b
    order = list(attr_src)
    mat = np.hstack([np.asarray(attr_src[n], np.float32).reshape(len(group['co']), -1) for n in order])
    widths = [np.asarray(attr_src[n]).reshape(len(group['co']), -1).shape[1] for n in order]
    chunks = []
    ntri = len(mat) // 3
    tris_per_chunk = ntri
    while True:
        ok = True
        chunks = []
        for s in range(0, ntri, max(1, tris_per_chunk)):
            sub = mat[s * 3:(s + tris_per_chunk) * 3]
            uniq, inv = np.unique(sub, axis=0, return_inverse=True)
            if len(uniq) > MAX_VERTS:
                ok = False
                break
            chunks.append((uniq, np.asarray(inv).reshape(-1)))
        if ok:
            break
        tris_per_chunk = max(1, tris_per_chunk // 2)
    out = []
    for uniq, inv in chunks:
        attrs = {}
        c = 0
        for n, w in zip(order, widths):
            attrs[n] = uniq[:, c:c + w]
            c += w
        out.append((attrs, len(uniq), inv))
    return out


# ----------------------------------------------------------------------
# materials / textures
# ----------------------------------------------------------------------

def library_entry(text, n_textures):
    """MaterialPool entry of a material imported from another world (stored as
    JSON on the Blender material) with the tree's list types back."""
    src = json.loads(text)
    for pe in src.get('PropertyEntries', {}).values():
        v = pe.get('Value')
        if isinstance(v, list):
            pe['Value'] = FloatList(float(x) for x in v)
    src['TextureEntries'] = {k: te for k, te in src.get('TextureEntries', {}).items()
                             if 0 <= te.get('TextureReference', -1) < n_textures}
    # the shipping key order: Name, Uuid, Type, Effect, RenderCaps, TextureEntries, PropertyEntries
    out = {'Name': src.get('Name', ''), 'Uuid': meridian.new_uuid()}
    out.update((k, v) for k, v in src.items() if k not in out)
    return out


class MaterialWriter:
    """Writes materials and textures into the .oct tree.

    Existing materials are updated in place (effect, parameters, texture
    slots). New Blender materials become new MaterialPool entries, cloned from
    their template (or their own copied entry when imported from another world).
    New textures are appended to the TexturePool.
    """
    def __init__(self, tree, report, world):
        """tree: the .oct tree being written; world: the world name."""
        self.world = world
        self.tree = tree
        self.report = report
        self.mats = list(tree.get('MaterialPool', {}).values())
        self.texs = list(tree.get('TexturePool', {}).values())
        self.new_index = {}
        self.tex_by_image = {}
        self.orig_textures = len(self.texs)

    def add_user_textures(self, scene):
        """Textures added in Blender (Material panel > Load Image) take the next
        TexturePool indices, in index order, before anything else is appended."""
        matio.add_new_textures(self.texs, scene.c2.textures, self.report)

    def texture_for_image(self, img):
        """TexturePool index for a Blender image (appended once per image)."""
        if img.name in self.tex_by_image:
            return self.tex_by_image[img.name]
        data = image_to_dds(img)
        src = 'materiallibrary\\custom\\%s.png' % bpy.path.clean_name(os.path.splitext(img.name)[0])
        t = {'Name': os.path.splitext(img.name)[0], 'DataCRC': 0, 'SourceFilePath': src, 'Type': '2d', 'Data': data}
        self.texs.append(t)
        k = len(self.texs) - 1
        self.tex_by_image[img.name] = k
        self.report.info('new texture %d from image %s' % (k, img.name))
        return k

    def neutral_lightmap(self):
        """TexturePool index of a light-1.0 lightmap, for lightmapped parts that
        have not been baked yet (appended once).
        """
        if 'neutral' not in self.tex_by_image:
            name = '%s_neutral_lightmap' % self.world
            self.texs.append({'Name': name, 'DataCRC': 0,
                              'SourceFilePath': 'world_data\\%s\\lightmaps\\%s.hdr' % (self.world, name),
                              'Type': '2d', 'Data': arcade.neutral_lightmap_dds()})
            self.tex_by_image['neutral'] = len(self.texs) - 1
            self.report.warn('parts with a lightmapped effect but no lightmap get a neutral one '
                             '(texture %d) - bake their lightmaps' % (len(self.texs) - 1))
        return self.tex_by_image['neutral']

    def texture_for_file(self, path):
        """New TexturePool entry from a .dds on disk (baked lightmaps)."""
        if path in self.tex_by_image:
            return self.tex_by_image[path]
        with open(path, 'rb') as f:
            data = f.read()
        name = os.path.splitext(os.path.basename(path))[0]
        t = {'Name': name, 'DataCRC': 0,
             'SourceFilePath': 'world_data\\%s\\lightmaps\\%s.hdr' % (self.world, name), 'Type': '2d', 'Data': data}
        self.texs.append(t)
        k = len(self.texs) - 1
        self.tex_by_image[path] = k
        self.report.info('new lightmap texture %d from %s' % (k, path))
        return k

    def update_existing(self, mat):
        """Write an imported material's edits back into its MaterialPool entry."""
        c = mat.c2
        m = self.mats[c.index]
        m['Effect'] = c.effect or m.get('Effect', '')
        if 'RenderCaps' in m:
            m['RenderCaps'] = c.render_caps
        matio.write_props(m, c, len(self.texs))

    def index_for(self, mat, fallback):
        """MaterialPool index for a Blender material (creating one if needed)."""
        if mat is None:
            return fallback
        c = mat.c2
        if c.variant_of is not None and c.variant_of != mat:
            return self.index_for(c.variant_of, fallback)  # lightmap variants export as their game material
        if c.index >= 0 and mat.get('c2_world') == self.world:
            return c.index
        if mat.name in self.new_index:
            return self.new_index[mat.name]
        tmpl = c.template if c.template >= 0 else fallback
        library = mat.get('c2_library_entry')
        if library:
            # imported from another world: its own entry (textures already
            # point at this world's new TexturePool entries)
            src = library_entry(library, len(self.texs))
            tmpl = 'world %s' % mat.get('c2_library', '?')
        else:
            src = copy.deepcopy(self.mats[tmpl]) if 0 <= tmpl < len(self.mats) else None
        if library:
            pass
        elif src is None or src.get('Effect') in ('Error', 'scene_settings', 'SolidColor'):
            src = next((copy.deepcopy(m) for m in self.mats
                        if any(te.get('Usage') == 'diffuseMapA1' for te in m.get('TextureEntries', {}).values())
                        and m.get('Effect', '').startswith('a_1_')), None)
        if src is None:
            self.report.warn('no template material for %s; using %d' % (mat.name, fallback))
            return fallback
        src['Name'] = mat.name
        if c.effect and c.effect != src.get('Effect'):
            src['Effect'] = c.effect  # e.g. switched between _lmh and _vb
            tes = src.setdefault('TextureEntries', {})
            if arcade.baked_mode(c.effect) == 'LIGHTMAP' and \
                    not any(te.get('Usage') == 'lightMap1' for te in tes.values()):
                white = next((k for k, t in enumerate(self.texs) if t.get('Name') == 'white'), 0)
                tes['TextureEntry#%d' % len(tes)] = {'TextureReference': white, 'Usage': 'lightMap1'}
        if 'Uuid' in src:
            src['Uuid'] = meridian.new_uuid()
        if len(c.props):
            matio.write_props(src, c, len(self.texs))
        # diffuse texture from the Blender node tree
        img = None
        if mat.use_nodes and mat.node_tree:
            for nd in mat.node_tree.nodes:
                if nd.type == 'TEX_IMAGE' and nd.image is not None:
                    img = nd.image
                    break
        if img is not None and not library and \
                not any(t.usage == 'diffuseMapA1' and t.texture > 0 for t in c.textures):
            try:
                k = self.texture_for_image(img)
                for te in src.get('TextureEntries', {}).values():
                    if te.get('Usage') == 'diffuseMapA1':
                        te['TextureReference'] = k
            except Exception as e:
                self.report.warn('could not convert image %s: %s' % (img.name, e))
        for pe in src.get('PropertyEntries', {}).values():
            if pe.get('Name') == 'diffuseColorA1' and not len(c.props):
                pe['Value'] = FloatList(float(x) for x in mat.diffuse_color[:3])
        self.mats.append(src)
        k = len(self.mats) - 1
        self.new_index[mat.name] = k
        self.report.info('new material %d "%s" (cloned from %s)' % (k, mat.name, tmpl))
        return k

    def apply(self, scene):
        """Put texture replacements / sRGB changes into the TexturePool and write
        both pools back into the tree.
        """
        matio.apply_replacements(self.texs, scene.c2.textures, self.orig_textures, self.report)
        self.tree['MaterialPool'] = {'Material#%d' % i: m for i, m in enumerate(self.mats)}
        self.tree['TexturePool'] = {'Texture#%d' % i: t for i, t in enumerate(self.texs)}


# ----------------------------------------------------------------------
# collision
# ----------------------------------------------------------------------

def _tri_entry(me, scale, report):
    """Triangle-mesh collision shape -> btcol entry dict.
    scale: baked into the vertices (instances carry no scale); a mirrored
    scale also reverses the triangles. The original BVH is reused when the
    geometry is unchanged.
    """
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    tv = np.empty(nt * 3, np.int32)
    me.loop_triangles.foreach_get('vertices', tv)
    tp = np.empty(nt, np.int32)
    me.loop_triangles.foreach_get('polygon_index', tp)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3) * np.asarray(scale, np.float32)
    tv = tv.reshape(-1, 3)
    if np.prod(np.sign(scale)) < 0:
        tv = tv[:, [0, 2, 1]]

    def face_attr(name):
        """A per-face integer attribute as an array (zeros when missing)."""
        a = me.attributes.get(name)
        if a is None or a.domain != 'FACE':
            return np.zeros(len(me.polygons), np.int64)
        v = np.empty(len(me.polygons), np.int32)
        a.data.foreach_get('value', v)
        return v.astype(np.int64) & 0xFFFFFFFF
    st = face_attr('surface_type')[tp]
    sc_ = face_attr('surface_color')[tp]
    pairs = np.stack([st, sc_], 1)
    up, inv = np.unique(pairs, axis=0, return_inverse=True)
    # keep first-seen order (matches how the shipping exporter lists them)
    first = {}
    order = []
    for k in np.asarray(inv).reshape(-1).tolist():
        if k not in first:
            first[k] = len(order)
            order.append(k)
    face_attrs = [(int(up[k][0]), int(up[k][1])) for k in order]
    attr_indices = np.array([first[k] for k in np.asarray(inv).reshape(-1).tolist()], np.int64)
    entry = {'positions': co, 'indices': tv.reshape(-1).astype(np.int64), 'face_attrs': face_attrs,
             'attr_indices': attr_indices, 'align': me.c2col.btm_align or 16, 'itype': me.c2col.btm_type or 1,
             'bvh': None}
    unscaled = all(abs(s - 1.0) < 1e-6 for s in scale)
    if unscaled and me.get('c2_bvh') and me.c2col.geo_hash and \
            btcol.geometry_hash(entry['positions'], entry['indices']) == me.c2col.geo_hash:
        from .importer import bvh_from_text
        entry['bvh'] = bvh_from_text(me['c2_bvh'])
    if co.shape[0] > MAX_VERTS:
        report.warn('collision mesh %s has %d vertices (max 65535); split it' % (me.name, co.shape[0]))
    return entry


def _bbox(me):
    """(min, max, all vertex positions) of a mesh."""
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3)
    if not len(co):
        return np.zeros(3), np.zeros(3), co
    return co.min(0), co.max(0), co


def _snap(new, olds, tol=1e-4):
    """Return the original matrix when `new` equals it within tolerance
    (Blender's float32 matrix maths adds noise like -6e-15 -> 0.0)."""
    for o in olds:
        if o is not None and len(o) == 16 and all(abs(a - b) <= tol * max(1.0, abs(b)) for a, b in zip(new, o)):
            return FloatList(o)
    return flist(new)


def export_collision(scene, root, tree, node_index_of, report):
    """Rebuild the collision pools from the Blender objects.

    Shapes whose mesh is untouched reuse their original definition (and, for
    triangle meshes, their original BVH); edited shapes get scale baked in,
    since a collision instance's transform carries no scale."""
    orig_insts = list(tree.get('CollisionInstancePool', {}).values())
    orig_shapes = list(tree.get('CollisionShapePool', {}).values())
    objs = sorted((o for o in common.descendants(root) if o.c2.kind == 'COLLISION'),
                  key=lambda o: (o.c2.col_index < 0, o.c2.col_index, o.name))
    shapes, entries, instances, assocs = [], [], [], []
    cache = {}
    unchanged_cache = {}

    def unchanged(me):
        """Is this collision mesh exactly as imported?"""
        if me.name not in unchanged_cache:
            unchanged_cache[me.name] = (0 <= me.c2col.shape_index < len(orig_shapes) and
                                        me.get('c2_hash') == common.col_mesh_hash(me))
        return unchanged_cache[me.name]

    def unit(scale):
        """Is a scale (1, 1, 1) up to sign, and not mirrored?"""
        return all(abs(abs(x) - 1.0) < 1e-5 for x in scale) and scale[0] * scale[1] * scale[2] > 0

    def shape_for(me, scale):
        """CollisionShapePool index (+ offset) for a mesh at a given scale
        (one shape per mesh/scale pair, shared by instances).
        """
        key = (me.name, tuple(round(float(s), 5) for s in scale))
        if key in cache:
            return cache[key]
        c = me.c2col
        st = c.shape_type
        offset = Vector((0, 0, 0))
        if st == 'TriangleList':
            e = _tri_entry(me, scale, report)
            entries.append(e)
            if unchanged(me) and unit(scale) and orig_shapes[c.shape_index].get('ShapeType') == 'TriangleList':
                d = copy.deepcopy(orig_shapes[c.shape_index])
                d['SerializeIndex'] = len(entries) - 1
            else:
                d = {'ShapeType': 'TriangleList', 'SerializeIndex': len(entries) - 1,
                     'CollisionLayer': c.collision_layer}
        elif unchanged(me) and unit(scale):
            d = copy.deepcopy(orig_shapes[c.shape_index])
        else:
            mn, mx, co = _bbox(me)
            s = np.abs(np.asarray(scale, np.float64))
            half = np.maximum((mx - mn) * 0.5 * s, 0.01)  # flat meshes still get a usable box
            center = (mx + mn) * 0.5 * np.asarray(scale)
            d = {'ShapeType': st, 'SurfaceType': c.surface_type, 'CollisionLayer': c.collision_layer}
            if st == 'Box':
                d['Extent'] = flist(half)
                offset = Vector(center)
            elif st == 'Sphere':
                d['Radius'] = float(half.max())
                offset = Vector(center)
            elif st == 'Cylinder':
                d['Extent'] = flist([max(half[0], half[2]), half[1], max(half[0], half[2])])
                offset = Vector(center)
            elif st == 'ConvexHull':
                d['Points'] = flist((co * np.asarray(scale, np.float32)).ravel())
                extra = json.loads(c.extra_json or '{}')
                d['CollisionMargin'] = float(extra.get('CollisionMargin', 0.04))
            else:
                extra = json.loads(c.extra_json or '{}')
                for k, v in extra.items():
                    d[k] = flist(v) if isinstance(v, list) else v
        shapes.append(d)
        cache[key] = (len(shapes) - 1, offset)
        return cache[key]

    rootinv = root.matrix_world.inverted()
    for obj in objs:
        G = rootinv @ obj.matrix_world
        loc, rot, scl = G.decompose()
        R = rot.to_matrix().to_4x4()
        if obj.type == 'MESH':
            si, off = shape_for(obj.data, scl)
            T = Matrix.Translation(loc) @ R @ Matrix.Translation(off)
        elif obj.type == 'EMPTY':
            parts = [c for c in obj.children if c.type == 'MESH']
            if not parts:
                report.warn('collision %s has no shapes' % obj.name)
                continue
            oc = obj.get('c2_compound_shape', -1)
            olds = [e.get('Transform') for e in orig_shapes[oc].get('Shapes', {}).values()] \
                if 0 <= oc < len(orig_shapes) and orig_shapes[oc].get('ShapeType') == 'Compound' else []
            sub = {}
            for k, ch in enumerate(parts):
                full = Matrix.Diagonal((*scl, 1.0)) @ ch.matrix_local
                if unchanged(ch.data):
                    # keep the child exactly as authored (shipping compounds do carry scale)
                    csi, _ = shape_for(ch.data, (1.0, 1.0, 1.0))
                    CT = full
                else:
                    cl, cr, cs = full.decompose()
                    csi, coff = shape_for(ch.data, cs)
                    CT = Matrix.Translation(cl) @ cr.to_matrix().to_4x4() @ Matrix.Translation(coff)
                sub['Shape#%d' % k] = {'ShapeRef': csi, 'Transform': _snap(b2g(CT), olds)}
            shapes.append({'ShapeType': 'Compound', 'Shapes': sub})
            si = len(shapes) - 1
            T = Matrix.Translation(loc) @ R
        else:
            continue
        ii = len(instances)
        old = [orig_insts[obj.c2.col_index].get('Transform')] if 0 <= obj.c2.col_index < len(orig_insts) else []
        instances.append({'Transform': _snap(b2g(T), old), 'Friction': float(obj.c2.friction),
                          'Restitution': float(obj.c2.restitution)})
        pn = common.node_parent(obj, root)
        nidx = node_index_of(pn) if pn is not None and pn != root else None
        name = export_name(pn) if (pn is not None and pn != root) else re.sub(r'^COL_', '', obj.name)
        if nidx is not None:
            assocs.append({'NodeName': name, 'Type': 'CollisionInstanceToSceneTreeNode',
                           'CollisionInstanceRef': ii, 'SceneTreeNodeRef': nidx})
        assocs.append({'NodeName': name, 'Type': 'CollisionShapeToCollisionInstance',
                       'CollisionInstanceRef': ii, 'CollisionShapeRef': si})
    tree['CollisionShapePool'] = {'Shape#%d' % i: s for i, s in enumerate(shapes)}
    tree['CollisionInstancePool'] = {'Instance#%d' % i: s for i, s in enumerate(instances)}
    report.info('collision rebuilt: %d instances, %d shapes, %d triangle meshes (%d with preserved BVH)'
                % (len(instances), len(shapes), len(entries), sum(1 for e in entries if e['bvh'] is not None)))
    return entries, assocs


# ----------------------------------------------------------------------
# scene subnetwork (named transforms mirrored for Meridian)
# ----------------------------------------------------------------------

def _rotor_from_rows(rows):
    """Game 3x3 row-matrix (rotation x scale) -> stored rotor (conjugate quaternion, x y z w)."""
    m = Matrix([list(r) for r in rows])  # row form
    q = m.transposed().to_quaternion()   # column form rotation
    return [-q.x, -q.y, -q.z, q.w]


def sync_subnetwork(tree, changed, new_locators, report):
    """Keep the scene SubNetwork's named transforms in step with moved nodes
    (Meridian reads node placements through them) and add entries for new
    locators (their header paths are re-allocated so ids stay valid).
    """
    snp = tree.get('SubNetworkPool', {})
    sn = None
    for v in snp.values():
        if v.get('Type') == 'Scene':
            sn = v
            break
    if sn is None:
        return
    paths = c2headers.decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
    by_path = {}
    for k, d in sn['DataNodePool'].items():
        by_path[paths[d['Header'] - 1]] = d
    n_upd = 0
    for name, L in changed.items():
        d = by_path.get(name + '/Transform')
        if d is None or 'Data' not in d:
            continue
        data = d['Data']
        m = np.asarray(L).reshape(4, 4)
        data['Translation'] = flist(m[3, :3])
        rows = m[:3, :3]
        scale = np.linalg.norm(rows, axis=1)
        if 'Orientation#Rotor' in data or np.abs(rows / np.where(scale[:, None] > 1e-9, scale[:, None], 1) - np.eye(3)).max() > 1e-5:
            r = _rotor_from_rows((rows / np.where(scale[:, None] > 1e-9, scale[:, None], 1)).tolist())
            old = data.get('Orientation#Rotor')
            if old is not None and sum(a * b for a, b in zip(old, r)) < 0:
                r = [-x for x in r]
            data['Orientation#Rotor'] = flist(r)
        if 'Scale' in data:
            data['Scale'] = flist(scale)
        n_upd += 1
    if new_locators:
        all_paths = list(paths)
        for name, L in new_locators:
            all_paths.append(name)
            all_paths.append(name + '/Transform')
        ordered = c2headers.allocate(all_paths)
        strings, indices, layout = c2headers.encode_paths(ordered)
        pid = {p: i + 1 for i, p in enumerate(ordered)}
        # remap headers of existing data nodes
        dn = {}
        for k, d in sn['DataNodePool'].items():
            d['Header'] = pid[paths[d['Header'] - 1]]
            dn[k] = d
        for d in sn.get('ProcessorNodePool', {}).values():
            if isinstance(d, dict) and isinstance(d.get('Header'), int):
                d['Header'] = pid[paths[d['Header'] - 1]]
        base = len(dn)
        for j, (name, L) in enumerate(new_locators):
            m = np.asarray(L).reshape(4, 4)
            rows = m[:3, :3]
            scale = np.linalg.norm(rows, axis=1)
            unit = rows / np.where(scale[:, None] > 1e-9, scale[:, None], 1)
            data = {'Translation': flist(m[3, :3])}
            kind = 'Dilation'
            if np.abs(unit - np.eye(3)).max() > 1e-5 or np.abs(scale - 1).max() > 1e-5:
                kind = 'Transform'
                data['Orientation#Rotor'] = flist(_rotor_from_rows(unit.tolist()))
                data['Scale'] = flist(scale)
            dn['DataNode#%d' % (base + j)] = {'Header': pid[name + '/Transform'], 'Type': kind, 'Data': data}
        sn['DataNodePool'] = dn
        sn['HeaderStrings'] = StrList(strings)
        sn['HeaderStringIndices'] = IntList(indices)
        sn['HeaderLayout'] = IntList(layout)
    if n_upd:
        report.info('scene subnetwork: %d named transforms updated' % n_upd)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def _has_c2_materials(o):
    """True when every material slot of the object holds a Cars 2 material."""
    slots = [s.material for s in o.material_slots]
    return bool(slots) and all(m is not None and (m.c2.effect or m.c2.index >= 0) for m in slots)


class MissingTextures(RuntimeError):
    """Export refused: materials have required texture slots left empty."""

    def __init__(self, lines):
        super().__init__('%d texture slot(s) need a texture: %s%s' % (
            len(lines), '; '.join(lines[:4]), ' ...' if len(lines) > 4 else ''))
        self.lines = lines


def missing_texture_lines(scene):
    """['material: slot'] of every required texture slot left empty (a shader
    switch adds the new effect's slots blank). Empty means the game would
    sample nothing there; no shipped material has one."""
    if scene.c2.platform != 'ARCADE':
        return []
    from . import fxnodes
    return ['%s: %s' % (m.name, fxnodes.usage_label(u)) for m, u in fxnodes.missing_textures(scene)]


def export_scene(context, out_dir):
    """Write the edited world to out_dir. Returns the Report.

    Steps: match objects to their scene nodes (new ones get new nodes),
    write transforms/lights, rebuild changed meshes (unchanged ones are
    copied byte for byte), add new nodes and link them to the world's cells,
    lights/rigs/probes, collision, Meridian (locators, choreographies,
    spawns), gameplay data, then write .oct, buffers, .btm/.btd, .mer, .ap,
    .dbl and copy the world's other files.
    """
    t0 = time.time()
    scene = context.scene
    root = scene.c2.root
    report = Report()
    if root is None:
        raise RuntimeError('No Cars 2 scene in this Blender scene (import one first)')
    missing = missing_texture_lines(scene)
    if missing:
        raise MissingTextures(missing)
    # start from the untouched source world: everything not modelled in
    # Blender passes through unchanged; `nodes` is edited in place
    src = bpy.path.abspath(scene.c2.oct_path)
    sc = octscene.OctScene(src)
    tree = sc.tree
    nodes = sc.nodes
    world = sc.name
    os.makedirs(out_dir, exist_ok=True)
    depsgraph = context.evaluated_depsgraph_get()
    all_objs = common.descendants(root)

    # --- which object owns which node -----------------------------------
    # owners: original node index -> object; new_objs: objects that need a
    # new node (added in Blender, or duplicates of an imported object). The
    # first object claiming an index keeps it (sorted so the original name wins).
    owners = {}
    new_objs = []
    skipped = []
    for o in sorted(all_objs, key=lambda o: (o.name != o.get('c2_name', o.name), o.name)):
        if o.get('c2_finish_point') and scene.c2.finish_mode == 'LAP':
            continue    # only the lap spline's reference (finishpoint.py): not part of the world
        k = o.c2.kind
        is_new_mesh = o.type == 'MESH' and (k == 'NONE' or (k == 'GEOMETRY' and o.c2.node_index < 0))
        if scene.c2.export_only_c2 and is_new_mesh and not _has_c2_materials(o):
            skipped.append(o.name)
            continue
        if k == 'NONE':
            if o.type in ('MESH', 'EMPTY', 'LIGHT', 'CAMERA') and o.parent is not None:
                new_objs.append(o)
            continue
        if k not in NODE_KINDS:
            continue
        if o.c2.node_index >= 0 and o.c2.node_index not in owners and o.c2.node_index < len(nodes):
            owners[o.c2.node_index] = o
        else:
            new_objs.append(o)  # new, or a duplicate of an imported object
    # meshes in this Blender scene that are not part of the Cars 2 world at all
    in_world = set(all_objs)
    outside = sorted(o.name for o in scene.objects if o.type == 'MESH' and o not in in_world and
                     (o.c2.kind == 'NONE' or (o.c2.kind == 'GEOMETRY' and o.c2.node_index < 0)) and
                     not o.hide_render and not o.get('c2_excluded'))
    if skipped:
        report.warn('left out %d new mesh(es) without Cars 2 materials (Export Only Cars 2 Materials is on; '
                    'run Auto-Assign Materials on them): %s%s'
                    % (len(skipped), ', '.join(skipped[:10]), ' ...' if len(skipped) > 10 else ''))
    if outside:
        report.warn('left out %d mesh(es) that are not part of the Cars 2 scene (not under %s - unparented, or '
                    'under an older import\'s root; select them and use Add Selected to Scene): %s%s'
                    % (len(outside), root.name, ', '.join(outside[:10]), ' ...' if len(outside) > 10 else ''))
    report.left_out = len(skipped) + len(outside)
    # parents first
    def depth(o):
        """Number of parents (new nodes are created parents first)."""
        d = 0
        while o.parent is not None:
            d += 1
            o = o.parent
        return d
    new_objs.sort(key=depth)
    new_index = {}
    for o in new_objs:
        new_index[o.name] = len(nodes) + len(new_index)

    def node_index_of(o):
        """Scene node index of an object (new or imported), None if it has none."""
        if o is None or o == root:
            return 0
        if o.name in new_index:
            return new_index[o.name]
        for i, own in owners.items():
            if own == o:
                return i
        return None
    owner_index = {o.name: i for i, o in owners.items()}

    def idx_of(o):
        """Scene node index of an object, 0 for the root."""
        if o is None or o == root:
            return 0
        return owner_index.get(o.name, new_index.get(o.name))

    rootinv = root.matrix_world.inverted()

    bakes = {}

    def src_mirrored(o):
        """The object's node had a mirrored world transform in the source file."""
        i = owner_index.get(o.name)
        return i is not None and 0 < i < len(sc.nodes) and np.linalg.det(np.asarray(sc.world(i))[:3, :3]) < 0

    def bake_of(o):
        """Matrix K appended to a mesh node (its vertices get K^-1), or None.
        - new meshes: rotation/scale/mirroring go into the vertices and the node
          sits at the mesh's centre unscaled, so the game's bounding-volume
          culling sees the mesh where it is (imported meshes often carry big
          local coordinates under a small scale, which made them pop in only
          near one spot)
        - kept nodes with a mirrored transform: local X flipped (the game culls
          back faces), unless the node was already mirrored in the source
          world: some shipping meshes are (air's tunnel pipes) and stay as
          they are"""
        if o is None or o == root or o.type != 'MESH':
            return None
        if o.name not in bakes:
            M = _parent_baked(o, _local_matrix(o)[0])
            K = None
            if o.name in new_index:
                c = M @ (sum((Vector(b) for b in o.bound_box), Vector()) / 8.0)
                if abs(M.to_3x3().determinant()) > 1e-12:
                    K = M.inverted() @ Matrix.Translation(c)
            elif (rootinv @ o.matrix_world).to_3x3().determinant() < 0 and not src_mirrored(o):
                K = MIRROR_X
            bakes[o.name] = K
        return bakes[o.name]

    def _parent_baked(o, M):
        """Undo the parent's bake matrix on a child's local matrix (the parent's
        node moved to its mesh centre, the child must stay where it was).
        """
        Kp = bake_of(common.node_parent(o, root))
        return Kp.inverted() @ M if Kp is not None else M

    def local_matrix(o):
        """(local matrix in the parent node's space, parent node index), with
        the object's own bake applied (see bake_of).
        """
        M, pidx = _local_matrix(o)
        M = _parent_baked(o, M)
        K = bake_of(o)
        return (M @ K if K is not None else M), pidx

    def _local_matrix(o):
        """Raw local matrix of o relative to its scene-node parent (or the root)."""
        p = common.node_parent(o, root)
        pidx = 0 if (p is None or p == root) else idx_of(p)
        if o.parent is not None and o.parent == (p or root):
            # not matrix_local: Blender derives that from world matrices and loses precision
            return o.matrix_parent_inverse @ o.matrix_basis, pidx
        if p is None or p == root:
            return rootinv @ o.matrix_world, 0
        return p.matrix_world.inverted() @ o.matrix_world, pidx

    # --- existing nodes ---------------------------------------------------
    # write transforms (only when really moved), parents, names, visibility
    # and light settings back into the original nodes
    changed_named = {}
    for i, o in owners.items():
        n = nodes[i]
        M, pidx = local_matrix(o)
        L = b2g(M)
        orig = list(n.get('LocalToParentMatrix', octscene.IDENTITY16))
        pr0 = n.get('ParentNodeReferences')
        same_parent = (pr0[0] if pr0 else 0) == pidx
        # Blender keeps loc/rot/scale, not the raw matrix: keep the original
        # floats unless the object really moved (or was re-parented)
        # an object whose matrix Blender couldn't hold (shear) and that wasn't
        # touched since import keeps the file's matrix too
        b0 = o.get('c2_basis0')
        untouched = b0 is not None and close([x for row in o.matrix_basis for x in row], list(b0), 1e-6)
        if not (same_parent and (close(orig, L, 1e-4) or untouched)):
            n['LocalToParentMatrix'] = flist(L)
            if n.get('NodeName'):
                changed_named[n['NodeName']] = L
        if not (o.c2.detached and pidx == 0):
            pr = n.get('ParentNodeReferences')
            if pr is None or pr[0] != pidx:
                n['ParentNodeReferences'] = IntList([pidx])
        name = export_name(o)
        if 'NodeName' in n and n['NodeName'] != name:
            report.info('renamed node %d %s -> %s' % (i, n['NodeName'], name))
            n['NodeName'] = name
        if n.get('Type') == 'Geometry':
            n['Visible'] = 1 if o.c2.visible else 0
            if o.type == 'MESH' and 'c2_ambient0' in o:
                from . import ambientvol
                if ambientvol.changed(o):
                    mats_of = subgeo_materials(o)
                    for j, sn in enumerate(nodes):
                        if sn.get('Type') == 'SubGeometryAmbient' and (sn.get('ParentNodeReferences') or [None])[0] == i:
                            ambientvol.write_node(o, sn)
                            if not ambientvol.write_material(o, mats_of.get(j)):
                                report.info('light volume %s: its material has no emissiveColor, the game '
                                            'lights it black' % name)
        elif n.get('Type') == 'Light' and o.type == 'LIGHT':
            write_light(n, o)
        elif n.get('Type') == 'Camera' and o.type == 'CAMERA':
            n['FocalLength'] = float(o.data.lens)

    # deleted -> tombstones
    deleted_geo = set()
    for i, n in enumerate(nodes):
        if i == 0 or i in owners or octscene.is_subgeo(n):
            continue
        t = n.get('Type')
        if t == 'Geometry':
            n['Visible'] = 0
            deleted_geo.add(i)
            report.info('deleted geometry %s (node %d) - hidden + emptied' % (n.get('NodeName'), i))
        elif t == 'Light':
            n['LightIntensity'] = 0.0
            report.info('deleted light %s (node %d) - intensity set to 0' % (n.get('NodeName'), i))

    # --- materials / buffers --------------------------------------------
    # game materials whose effect was switched: their parts need the new
    # effect's vertex streams, so their meshes are re-encoded below
    source_effects = [m.get('Effect', '') for m in sc.materials]   # before the writer updates them
    effect_changed = {m.c2.index for m in bpy.data.materials
                      if m.get('c2_world') == world and m.c2.variant_of is None and 0 <= m.c2.index < len(sc.materials)
                      and m.c2.effect and m.c2.effect != sc.materials[m.c2.index].get('Effect')}
    mw = MaterialWriter(tree, report, world)
    mw.add_user_textures(scene)
    for mat in bpy.data.materials:
        if mat.get('c2_world') == world and 0 <= mat.c2.index < len(mw.mats) and mat.c2.variant_of is None:
            mw.update_existing(mat)
    builder = octscene.BufferBuilder(sc)
    arcade_world = scene.c2.platform == 'ARCADE'
    fallback_mat = next((i for i, m in enumerate(sc.materials) if m.get('Effect', '').startswith('a_1_')), 0)
    common_layout = None
    new_nodes = []

    def degenerate(sub_node, layout):
        """Empty out a deleted/unused part: one degenerate triangle in its old layout
        (the node stays so no index shifts).
        """
        attrs = {'Position': np.zeros((3, 3), np.float32)}
        sub_node['VertexStreamReferences'] = IntList(builder.add_vstreams(layout, attrs, 3))
        sub_node['IndexStreamReference'] = builder.add_indices(np.array([0, 1, 2]))

    def build_geometry(gi, geo_node, obj, is_new):
        """Encode a mesh object into the SubGeometry parts of Geometry node gi.
        Each (material, part) group of faces becomes one part; existing parts keep
        their node and stream layout; new parts get the layout their effect
        needs. Bounds of the Geometry node are recomputed.
        """
        groups = extract_groups(obj, depsgraph, report, -1.0 if arcade_world else 1.0)
        K = bake_of(obj)
        if K is not None:
            for g in groups:
                bake_group(g, K)
        orig_subs = [] if is_new else [j for j in sc.subgeos(gi) if nodes[j].get('VertexStreamReferences')]
        layouts = {j: sc.stream_layout(sc.nodes_orig_refs[j]) for j in orig_subs}
        orig_by_mat = {}
        for j in orig_subs:
            orig_by_mat.setdefault(nodes[j].get('MaterialReference'), j)
        used = set()
        all_pos = []
        # each group of faces (one material + original part) becomes one part:
        # pick its material index, its vertex layout (the original part's, or
        # the one its effect needs), weld and encode it
        for g in groups:
            mi = mw.index_for(g['material'], nodes[orig_subs[0]].get('MaterialReference', fallback_mat)
                              if orig_subs else fallback_mat)
            tmpl = g['subgeo'] if g['subgeo'] in orig_subs else orig_by_mat.get(mi, orig_subs[0] if orig_subs else None)
            if tmpl is not None:
                layout = layouts[tmpl]
                tnode = nodes[tmpl]
            else:
                layout = common_layout
                tnode = None
            if arcade_world:
                # the vertex declaration must match the effect: when the part's effect
                # differs from its template's, build the layout from the effect file
                eff = mw.mats[mi].get('Effect', '') if 0 <= mi < len(mw.mats) else ''
                if tmpl is None or eff != orig_effect.get(tmpl):
                    lay = effect_layout(eff)
                    if lay is not None:
                        layout = lay
                    elif tmpl is not None:
                        report.warn('%s: effect %s not found in assets/effects - kept the old vertex layout'
                                    % (obj.name, eff))
            eff = mw.mats[mi].get('Effect', '') if 0 <= mi < len(mw.mats) else ''
            if eff.startswith('wtr') and 'ColorColor' not in g['colors']:
                # shipping water is white here (alpha fades it out at the shore); the
                # generic mid-grey default would darken it by half
                g['colors']['ColorColor'] = np.ones((len(g['co']), 4), np.float32)
            for attrs, nv, inv in weld(g, layout):
                vs = builder.add_vstreams(layout, attrs, nv)
                isr = builder.add_indices(inv)
                pos = attrs['Position']
                all_pos.append(pos)
                if tnode is not None and tmpl not in used:
                    sub = tnode
                    used.add(tmpl)
                else:
                    sub = copy.deepcopy(tnode) if tnode is not None else {
                        'Type': 'SubGeometryLit' if arcade_world else 'SubGeometry', 'ParentNodeReferences': IntList([gi]),
                        'LocalToParentMatrix': flist(octscene.IDENTITY16), 'BoundingCenter': flist([0, 0, 0]),
                        'NodeName': '', 'RenderCaps': 4096, 'MaterialReference': mi, 'LightSet': 0,
                        'BillboardType': -1, 'VertexStreamReferences': IntList(), 'IndexStreamReference': 0}
                    sub['ParentNodeReferences'] = IntList([gi])
                    sub['NodeName'] = '%s_%s' % (geo_node.get('NodeName', 'Geometry'),
                                                 g['material'].name if g['material'] else 'mat%d' % mi)
                    new_nodes.append(sub)
                sub['VertexStreamReferences'] = IntList(vs)
                sub['IndexStreamReference'] = isr
                sub['MaterialReference'] = mi
                set_instance_textures(sub, g['material'], mw)
                sub['BoundingCenter'] = flist(((pos.min(0) + pos.max(0)) * 0.5).tolist())
                if obj.c2.lightset >= 0:
                    sub['LightSet'] = obj.c2.lightset
        for j in orig_subs:
            if j not in used:
                degenerate(nodes[j], layouts[j])
        if all_pos:
            geo_node.update(octscene.bounds(np.concatenate(all_pos)))
        return len(groups)

    # most common shipping layout, used for brand-new meshes
    from collections import Counter
    lay_count = Counter()
    for n in nodes:
        if octscene.is_subgeo(n) and n.get('VertexStreamReferences'):
            lay_count[tuple(map(lambda x: (x[0], tuple(x[1])), sc.stream_layout(n['VertexStreamReferences'])))] += 1
    common_layout = [(w, list(e)) for w, e in lay_count.most_common(1)[0][0]] if lay_count else octscene.default_layout()

    sc.nodes_orig_refs = {j: list(n.get('VertexStreamReferences', [])) for j, n in enumerate(nodes)
                          if octscene.is_subgeo(n)}
    orig_effect = {j: source_effects[n['MaterialReference']] for j, n in enumerate(nodes)
                   if octscene.is_subgeo(n) and 0 <= n.get('MaterialReference', -1) < len(source_effects)}
    assets = common.game_assets(scene)
    if arcade_world and not arcade.effects_dir(assets):
        report.warn('GAME ASSETS FOLDER NOT FOUND - set it in the Cars 2 Scene panel. Without the game\'s '
                    'effects, new meshes get a generic vertex layout, and shaders that need vertex colours or '
                    'extra UVs (water, shaders with _v_ parts) draw black or not at all')
    elem_types = arcade.world_element_types(sc) if arcade_world else {}
    layout_cache = {}

    def effect_layout(eff):
        """Vertex layout an effect needs (from its .tfx), cached; None when the
        effect file can't be read.
        """
        if eff not in layout_cache:
            tfx = arcade.read_effect(assets, eff) if assets else None
            names = arcade.effect_streams(tfx) if tfx else []
            layout_cache[eff] = arcade.layout_for_streams(names, elem_types) if names else None
        return layout_cache[eff]
    # classify every Geometry node first, so the kept streams can be copied in
    # their original order (an untouched world exports byte-identical)
    #   keep     unchanged mesh: its vertex/index bytes are copied as they are
    #   rebuild  edited mesh (or one whose material effect changed): re-encoded
    #   delete   object deleted in Blender: node hidden, parts emptied
    status = {}
    for gi, n in enumerate(nodes):
        if n.get('Type') != 'Geometry':
            continue
        o = owners.get(gi)
        if gi in deleted_geo:
            status[gi] = 'delete'
        elif o is None or o.type != 'MESH' or bake_of(o) is None and \
                (o.c2.mesh_hash and o.c2.mesh_hash == common.mesh_hash(o) and o.c2.lightset < 0 and
                 not any(nodes[j].get('MaterialReference') in effect_changed for j in sc.subgeos(gi))):
            status[gi] = 'keep'
        else:
            status[gi] = 'rebuild'
    replaced_subs = set()
    for gi, st in status.items():
        if st != 'keep':
            replaced_subs.update(j for j in sc.subgeos(gi) if nodes[j].get('VertexStreamReferences'))
    drop_v, drop_i, need_v, need_i = set(), set(), set(), set()
    for j, n in enumerate(nodes):
        if not octscene.is_subgeo(n):
            continue
        refs, ir = n.get('VertexStreamReferences', []), n.get('IndexStreamReference')
        (drop_v if j in replaced_subs else need_v).update(refs)
        if ir is not None:
            (drop_i if j in replaced_subs else need_i).add(ir)
    for r in range(len(sc.vstreams)):
        if r not in drop_v or r in need_v:
            builder.keep_vstream(r)
    for r in range(len(sc.istreams)):
        if r not in drop_i or r in need_i:
            builder.keep_istream(r)
    for j, n in enumerate(nodes):
        if octscene.is_subgeo(n) and j not in replaced_subs:
            n['VertexStreamReferences'] = IntList(builder.keep_vstream(r) for r in n.get('VertexStreamReferences', []))
            if n.get('IndexStreamReference') is not None:
                n['IndexStreamReference'] = builder.keep_istream(n['IndexStreamReference'])

    for gi, st in status.items():
        o = owners.get(gi)
        if st == 'keep' and o is not None and o.type == 'MESH':
            for j, m in subgeo_materials(o).items():
                if 0 <= j < len(nodes) and octscene.is_subgeo(nodes[j]):
                    set_instance_textures(nodes[j], m, mw)
    n_kept = sum(1 for s in status.values() if s == 'keep')
    n_rebuilt = 0
    for gi, st in status.items():
        n = nodes[gi]
        if st == 'delete':
            for j in sc.subgeos(gi):
                if j in replaced_subs:
                    degenerate(nodes[j], sc.stream_layout(sc.nodes_orig_refs[j]))
        elif st == 'rebuild':
            build_geometry(gi, n, owners[gi], False)
            n_rebuilt += 1
            report.info('rebuilt mesh %s (node %d)' % (n.get('NodeName'), gi))

    # --- new nodes ------------------------------------------------------
    created = []
    for o in new_objs:
        M, pidx = local_matrix(o)
        L = flist(b2g(M))
        name = o.name
        if o.type == 'MESH':
            n = {'NodeName': name, 'Uuid': meridian.new_uuid(), 'DisplayLayer': -1, 'Type': 'Geometry',
                 'Visible': 1, 'DynamicVisPlacement': 1, 'MeshName': name + 'Shape'}
            n.update(octscene.bounds(np.zeros((1, 3))))
            n['BoundingType'] = 'Box'
            n['ParentNodeReferences'] = IntList([pidx])
            n['LocalToParentMatrix'] = L
        elif o.type == 'LIGHT':
            n = new_light_node(o, name, pidx, L)
        elif o.type == 'CAMERA':
            n = {'NodeName': name, 'Uuid': meridian.new_uuid(), 'DisplayLayer': -1, 'Type': 'Camera',
                 'IsOrthographic': 0, 'FocalLength': float(o.data.lens), 'CameraScale': 1.0,
                 'NearClipPlane': float(o.data.clip_start), 'FarClipPlane': float(o.data.clip_end),
                 'HorizontalFilmAmperture': float(o.data.sensor_width / 25.4),
                 'VerticalFilmAperture': float(o.data.sensor_width / 25.4 * 0.75), 'LensSqueezeRatio': 1.0,
                 'ParentNodeReferences': IntList([pidx]), 'LocalToParentMatrix': L}
        else:
            n = {'NodeName': name, 'Uuid': meridian.new_uuid(), 'DisplayLayer': -1, 'Type': 'Transform',
                 'ParentNodeReferences': IntList([pidx]), 'LocalToParentMatrix': L}
        nodes.append(n)
        created.append((o, len(nodes) - 1))
        report.info('new %s node %d "%s"' % (n['Type'], len(nodes) - 1, name))
    for o, gi in created:
        if o.type == 'MESH':
            build_geometry(gi, nodes[gi], o, True)
    for sub in new_nodes:
        nodes.append(sub)

    # --- cells / visibility ----------------------------------------------
    # The game only draws nodes linked to a visibility cell. New nodes go into
    # the first CellLink (the always-drawn _REST_OF_WORLD_ cell in shipping
    # worlds) and new meshes into the first VisHierarchyLink.
    assoc_list = list(tree.get('AssociationPool', {}).values())
    new_refs = [gi for o, gi in created]
    if new_refs:
        for a in assoc_list:
            if a.get('Type') == 'CellLink':
                a['LinkedSceneTreeNodeRefs'] = IntList(list(a.get('LinkedSceneTreeNodeRefs', [])) + new_refs)
                break
        geo_refs = [gi for o, gi in created if o.type == 'MESH']
        for a in assoc_list:
            if a.get('Type') == 'VisHierarchyLink' and geo_refs:
                a['LinkedNodeRefs'] = IntList(list(a.get('LinkedNodeRefs', [])) + geo_refs)
                break

    # --- lighting pools -----------------------------------------------------
    write_light_pools(scene, tree, idx_of, root, report)

    # --- collision ------------------------------------------------------------
    col_entries = None
    col_copy = False
    if scene.c2.has_col or any(o.c2.kind == 'COLLISION' for o in all_objs):
        if scene.c2.get('col_sig') and scene.c2['col_sig'] == common.collision_signature(root) \
                and os.path.exists(os.path.join(sc.folder, world + '.btm')):
            col_copy = True  # untouched: keep pools, associations and .btm/.btd verbatim
            report.info('collision unchanged - copied verbatim')
        else:
            col_entries, col_assocs = export_collision(scene, root, tree, idx_of, report)
            assoc_list = col_assocs + [a for a in assoc_list if a.get('Type') not in
                                       ('CollisionInstanceToSceneTreeNode', 'CollisionShapeToCollisionInstance')]

    # --- meridian ---------------------------------------------------------------
    base_in = os.path.join(sc.folder, world)
    mer_tree = None
    new_locators = []
    extra_ap = []     # .ap sections of new choreography actors
    if scene.c2.has_mer:
        # graph edits are matched by UUID, never by index: inserting nodes
        # shifts the indices of everything after the world's own block
        mer_tree = c2oct.load(base_in + '.mer')
        g = meridian.Graph(mer_tree)
        refs_pool = None
        for v in tree.get('SceneNodeRefsPool', {}).values():
            if v.get('Name') == 'MeridianSceneNodeRefs':
                refs_pool = v
        # locators: new ones (or duplicated ones) get scene refs + graph nodes
        locator_ts = {}
        for i_, n_ in enumerate(g.nodes):
            if n_.get('Type') == 'LocatorNode' and 'Transform' in n_:
                locator_ts[i_] = n_['Transform']
        seen_mer = set()
        obj_ts = {}   # object name -> TransformSource Uuid (indices move when nodes are inserted)
        for o in all_objs:
            if o.c2.meridian == 'NONE':
                continue
            if o.c2.mer_index >= 0 and o.c2.mer_index not in seen_mer and o.c2.mer_index < len(g.nodes):
                seen_mer.add(o.c2.mer_index)
                ts = g.nodes[o.c2.mer_index].get('Transform')
                if ts is not None:
                    obj_ts[o.name] = bytes(g.nodes[ts]['Uuid'])
                continue
            if o.c2.meridian != 'LOCATOR' or refs_pool is None:
                continue
            ni = idx_of(o)
            if ni is None:
                continue
            name = export_name(o) if o.name not in new_index else o.name
            refs = list(refs_pool['Nodes'])
            refs.append(ni)
            refs_pool['Nodes'] = IntList(refs)
            assoc_list.append({'NodeName': name, 'Type': 'Transform', 'SceneTreeNodeRef': ni, 'SubNetworkRef': 0})
            loc, ts = g.add_locator(name, len(refs) - 1)
            obj_ts[o.name] = bytes(g.nodes[ts]['Uuid'])
            new_locators.append((name, nodes[ni]['LocalToParentMatrix']))
            report.info('new Meridian locator "%s"' % name)
        actor_ctx = {'nodes': nodes, 'assoc_list': assoc_list, 'root': root, 'ap': extra_ap}
        apply_choreos(scene, g, obj_ts, report, actor_ctx)
        if scene.c2.has_gameplay:
            gameplay.export_meridian(scene, g, obj_ts, report)
        from . import objplace
        objplace.export_splits(scene, g, obj_ts, report)     # per-track spawn points
        from . import collectgates
        collectgates.export(scene, g, obj_ts, report)        # Collect gates: new ones, labels
        from . import finishpoint
        finishpoint.export(scene, g, obj_ts, report)         # point-to-point finish
        from . import crowd
        crowd.export(scene, g, report)                       # edited crowd rows
        probs = meridian.validate(g)
        for p in probs[:20]:
            report.warn('meridian: ' + p)
        g.apply()

    sync_subnetwork(tree, changed_named, new_locators, report)

    # --- gameplay (AI track, markers, nav meshes) ------------------------------
    if scene.c2.has_gameplay:
        gameplay.export_track(scene, tree, report)
        gameplay.export_navmeshes(scene, tree, report)

    # --- assemble + write -----------------------------------------------------
    # pools are written back as {'Item#i': ...} dicts in list order
    tree['AssociationPool'] = {'Association#%d' % i: a for i, a in enumerate(assoc_list)}
    tree['SceneTreeNodePool'] = {'Node#%d' % i: n for i, n in enumerate(nodes)}
    mw.apply(scene)
    builder.apply(tree, world)
    out_base = os.path.join(out_dir, world)
    with open(out_base + '.oct', 'wb') as f:
        f.write(c2oct.encode(tree, sc.endian))
    with open(out_base + '_0.vbuf', 'wb') as f:
        f.write(builder.vb)
    with open(out_base + '_0.ibuf', 'wb') as f:
        f.write(builder.ib)
    if col_entries is not None:
        btcol.write_collision(out_base + '.btm', out_base + '.btd', col_entries, '<')
    elif col_copy:
        import shutil
        for ext in ('.btm', '.btd'):
            shutil.copyfile(os.path.join(sc.folder, world + ext), out_base + ext)
    if mer_tree is not None:
        with open(out_base + '.mer', 'wb') as f:
            f.write(c2oct.encode(mer_tree, '<'))
    if scene.c2.has_ap:
        sections = [(a.name if a.name else None, [(kv.key, kv.value) for kv in a.kvs]) for a in scene.c2.actors]
        sections += extra_ap
        apfile.save(sections, out_base + '.ap')
    if scene.c2.has_gameplay:
        gameplay.export_paths(scene, base_in + '.dbl', out_base + '.dbl', report)
    # the world's other files (.mtc ...) are not edited: copy them so
    # the export folder is a complete world that can be zipped / dropped in as is
    import shutil
    for fn in sorted(os.listdir(sc.folder)):
        src_f = os.path.join(sc.folder, fn)
        ours = fn.lower().startswith((world.lower() + '.', world.lower() + '_'))
        if ours and os.path.isfile(src_f):
            dst = os.path.join(out_dir, fn)
            if not os.path.exists(dst) or os.path.getmtime(dst) < t0:  # not written by this export
                shutil.copyfile(src_f, dst)
                report.info('copied unchanged %s' % fn)
    report.info('meshes: %d kept byte-exact, %d rebuilt, %d new nodes' % (n_kept, n_rebuilt, len(created)))
    report.info('written to %s in %.1fs' % (out_dir, time.time() - t0))
    txt = bpy.data.texts.get('Cars2SceneExportReport') or bpy.data.texts.new('Cars2SceneExportReport')
    txt.clear()
    txt.write('\n'.join(report.lines))
    return report


def write_light(n, o):
    """Write a Blender light's game fields into an existing Light node."""
    ld = o.data
    c = ld.c2
    n['LightColor'] = flist(ld.color[:3])
    n['LightIntensity'] = float(c.intensity)
    n['ShadowColor'] = flist(c.shadow_color[:3])
    if 'AmbientShade' in n:
        n['AmbientShade'] = float(c.ambient_shade)
    if 'DecayRate' in n:
        n['DecayRate'] = int(c.decay_rate)
    if 'ConeAngle' in n:
        n['ConeAngle'] = float(c.cone_angle)
        n['PenumbraAngle'] = float(c.penumbra_angle)
        n['Dropoff'] = float(c.dropoff)
    n['ExclusionType'] = int(c.exclusion_type)
    kv = n.get('StaticKeyValuePairs')
    if kv is not None:
        kv['FalloffRadius'] = c.falloff_radius
        kv['Softness'] = c.softness
        kv['IsRoaming'] = str(bool(c.is_roaming))
        kv['UseTertiary'] = str(bool(c.use_tertiary))
        kv['IsNegative'] = str(bool(c.is_negative))
        kv['LightUsageLightmapNormal'] = str(bool(c.lm_normal))
        kv['LightUsageLightmapSpecular'] = str(bool(c.lm_specular))
        for e in c.extra_kv:
            kv[e.key] = e.value


def new_light_node(o, name, pidx, L):
    """A new Light node for a Blender light."""
    ld = o.data
    c = ld.c2
    lt = c.light_type
    if ld.type == 'SPOT':
        lt = 'Spot'
    elif ld.type == 'SUN' and lt == 'Spot':
        lt = 'Directional'
    n = {'NodeName': name, 'Uuid': meridian.new_uuid(), 'DisplayLayer': -1, 'Type': 'Light', 'LightType': lt,
         'LightColor': flist(ld.color[:3]), 'LightIntensity': float(c.intensity)}
    if lt == 'Ambient':
        n['AmbientShade'] = float(c.ambient_shade)
    n['ShadowColor'] = flist(c.shadow_color[:3])
    if lt == 'Point':
        n['DecayRate'] = int(c.decay_rate)
    if lt == 'Spot':
        n['DecayRate'] = int(c.decay_rate)
        n['ConeAngle'] = float(ld.spot_size if c.cone_angle == 0.698 else c.cone_angle)
        n['PenumbraAngle'] = float(c.penumbra_angle)
        n['Dropoff'] = float(c.dropoff)
    n['StaticKeyValuePairs'] = {}
    n['ExclusionType'] = int(c.exclusion_type)
    n['ParentNodeReferences'] = IntList([pidx])
    n['LocalToParentMatrix'] = L
    write_light(n, o)
    return n


def write_light_pools(scene, tree, idx_of, root, report):
    """Write light sets, light rigs and light probes."""
    if 'LightSetPool' in tree:
        out = {}
        for k, ls in enumerate(scene.c2.light_sets):
            d = {}
            for key, lt in (('SpotLights', 'Spot'), ('AmbientLights', 'Ambient'), ('DirectionalLights', 'Directional')):
                refs = [idx_of(r.obj) for r in ls.lights
                        if r.obj is not None and r.obj.type == 'LIGHT' and r.obj.data.c2.light_type == lt
                        and idx_of(r.obj) is not None]
                if refs:
                    d[key] = IntList(refs)
            out['LightSet#%d' % k] = d
        if out:
            tree['LightSetPool'] = out
    if 'LightRigPool' in tree:
        rigs = list(tree['LightRigPool'].values())
        for it in scene.c2.light_rigs:
            if 0 <= it.index < len(rigs):
                r = rigs[it.index]
                refs = [idx_of(x.obj) for x in it.lights if x.obj is not None and idx_of(x.obj) is not None]
                if refs or 'LightRefs' in r:
                    r['LightRefs'] = IntList(refs)
                r['Position'] = flist(it.position)
                r['LightRigConsts'] = flist(list(it.sh)[:len(r.get('LightRigConsts', [])) or 27])
                r['RigName'] = it.rig_name
                if it.type_name or 'TypeName' in r:
                    r['TypeName'] = it.type_name
    probes = [o for o in common.descendants(root) if o.c2.kind == 'PROBE']
    if probes or 'LightProbePool' in tree:
        orig = list(tree.get('LightProbePool', {}).values())
        out = []
        for o in sorted(probes, key=lambda o: (o.c2.probe_index < 0, o.c2.probe_index)):
            out.append(export_probe(o, orig, root, idx_of, report))
        tree['LightProbePool'] = {'LightProbe#%d' % i: d for i, d in enumerate(out)}


def export_probe(o, orig, root, idx_of, report):
    """LightProbePool entry for a probe object (original fields kept, placement,
    node, texture, density and SH grid updated).
    """
    c = o.c2
    d = copy.deepcopy(orig[c.probe_index]) if 0 <= c.probe_index < len(orig) else {}
    G = root.matrix_world.inverted() @ o.matrix_world
    loc, rot, scl = G.decompose()
    if o.parent is not None and o.parent == c.probe_node:
        scl = o.matrix_basis.to_scale()  # VolumeExtents are in the box node's own units
    d['TypeName'] = c.probe_type
    # the game reads every probe's node (null pointer crash otherwise): when the
    # node's object was deleted, the node itself is still in the file as a hidden,
    # empty tombstone, so the probe keeps it with its own placement
    orphan = c.probe_node is None and isinstance(d.get('NodeRef'), int) and d['NodeRef'] >= 0
    if orphan:
        report.info('probe %s: its node was deleted - kept on the hidden node %d' % (o.name, d['NodeRef']))
    else:
        d['NodeRef'] = idx_of(c.probe_node) if c.probe_node is not None and idx_of(c.probe_node) else -1
        if d['NodeRef'] < 0 and c.probe_type == 'Volume':
            report.warn('probe %s has no node: the game may not load it' % o.name)
    d['ProbeName'] = c.probe_name or o.name
    if c.probe_texture >= 0:
        d['TextureRef'] = c.probe_texture
    if c.probe_luminance and c.probe_has_luminance:
        d['AverageLuminance'] = float(c.probe_luminance)
    if c.probe_type == 'Volume':
        old_dens = list(d.get('Density', c.probe_density))
        dens = [max(1, int(v)) for v in c.probe_density]
        d['Density'] = IntList(dens)
        d['FadeType'] = c.probe_fade
        # only probes that had one or fade outside: Inside-fading probes never
        # carry it and don't use it (round trip)
        if 'OutsideSize' in d or c.probe_fade == 'Outside':
            d['OutsideSize'] = flist(c.probe_outside)
        if c.probe_has_inside:
            d['InsideSize'] = float(c.probe_inside)
        if not (orphan and 'VolumeExtents' in d):   # extents are in the node's units
            d['VolumeExtents'] = _snap_vec([abs(v) for v in scl], d.get('VolumeExtents'))
        grid = o.get('c2_direct_sh')
        if grid is None and c.probe_has_sh:
            # scenes imported before 0.14.1 kept a 1x1x1 volume's one cell here
            grid = list(c.probe_direct_sh)[:27]
        if grid is not None:
            grid = [float(x) for x in grid]
            if len(grid) != arcade.probe_cells(dens) * 27:
                grid = resample_probe_grid(grid, old_dens, dens, report, o.name)
            d['DirectSH'] = FloatList(grid)
    if not (orphan and 'Position' in d):
        d['Position'] = _snap_vec(loc, d.get('Position'))
    if c.probe_has_sh and c.probe_type != 'Volume':
        d['DirectSH'] = flist(c.probe_direct_sh)
    if c.probe_scene:
        d['IsSceneProbe'] = d.get('IsSceneProbe') or 1
    elif 'IsSceneProbe' in d:
        del d['IsSceneProbe']
    return d


def _snap_vec(new, old, tol=1e-4):
    """Like _snap, for vectors: keep the original values when within tolerance."""
    new = [float(x) for x in new]
    if old is not None and len(old) == len(new) and \
            all(abs(a - b) <= tol * max(1.0, abs(b)) for a, b in zip(new, old)):
        return FloatList(old)
    return FloatList(new)


def resample_probe_grid(grid, old_dens, new_dens, report, name):
    """Density changed: trilinearly resample the SH cells onto the new grid."""
    old_dens = [max(1, int(v)) for v in old_dens]
    if len(grid) != arcade.probe_cells(old_dens) * 27:
        report.warn('probe %s: DirectSH does not match its density - filled with zeros' % name)
        return [0.0] * (arcade.probe_cells(new_dens) * 27)
    g = arcade.probe_grid(grid, old_dens)
    out = np.empty((arcade.probe_cells(new_dens), 3, 9))
    for k, pt in enumerate(arcade.probe_cell_positions(new_dens)):
        f = [(v + 1.0) * 0.5 * (n - 1) for v, n in zip(pt, old_dens)]
        i0 = [int(np.floor(v)) for v in f]
        i1 = [min(a + 1, n - 1) for a, n in zip(i0, old_dens)]
        t = [v - a for v, a in zip(f, i0)]
        acc = np.zeros((3, 9))
        for cz in (0, 1):
            for cy in (0, 1):
                for cx in (0, 1):
                    w = (t[0] if cx else 1 - t[0]) * (t[1] if cy else 1 - t[1]) * (t[2] if cz else 1 - t[2])
                    acc += w * g[(i1 if cz else i0)[2], (i1 if cy else i0)[1], (i1 if cx else i0)[0]]
        out[k] = acc
    report.info('probe %s: SH grid resampled %s -> %s' % (name, old_dens, new_dens))
    return out.ravel().tolist()


def subgeo_materials(o):
    """{SubGeometry node index: Blender material} from the mesh's c2_subgeo face attribute."""
    me = o.data
    a = me.attributes.get('c2_subgeo')
    if a is None or a.domain != 'FACE' or not len(me.polygons):
        return {}
    sub = np.empty(len(me.polygons), np.int32)
    a.data.foreach_get('value', sub)
    mi = np.empty(len(me.polygons), np.int32)
    me.polygons.foreach_get('material_index', mi)
    slots = o.material_slots
    out = {}
    for j, k in np.unique(np.stack([sub, mi], 1), axis=0).tolist():
        if j >= 0 and 0 <= k < len(slots):
            out.setdefault(j, slots[k].material)
    return out


def set_instance_textures(sub, mat, mw):
    """Arcade SubGeometryLit: lightMap1 / envMapInstance follow the Blender
    material's (variant's) textures. Values that already match are untouched."""
    if mat is None:
        return
    c = mat.c2
    want = {}
    lm = c.lightmap
    if mat.get('c2_new_lightmap'):
        lm = mw.texture_for_file(bpy.path.abspath(mat['c2_new_lightmap']))
    if lm >= 0:
        want['lightMap1'] = lm
    if c.envmap_inst >= 0:
        want['envMapInstance'] = c.envmap_inst
    tes = sub.get('InstanceTextureEntries')
    if tes is None:
        tes = {}
    have = {te.get('Usage'): te for te in tes.values()}
    effect = (c.variant_of.c2.effect if c.variant_of else c.effect) or ''
    if 'lightMap1' not in want and 'lightMap1' not in have and arcade.baked_mode(effect) == 'LIGHTMAP':
        want['lightMap1'] = mw.neutral_lightmap()  # lightmapped effect, nothing baked yet
    if not want:
        return
    for usage, ref in want.items():
        if usage in have:
            if have[usage].get('TextureReference') != ref:
                have[usage]['TextureReference'] = ref
        else:
            tes['TextureEntry#%d' % len(tes)] = {'TextureReference': ref, 'Usage': usage}
    sub['InstanceTextureEntries'] = tes


def apply_choreos(scene, g, obj_ts, report, actor_ctx=None):
    """Edits are resolved through UUIDs: inserting nodes moves the indices of
    every node in the merged namespaces that follow the world's own block."""
    sections = {a.name: a for a in scene.c2.actors}

    def ts_index(obj):
        """Graph index of the TransformSource of an object's locator (or None)."""
        u = obj_ts.get(obj.name) if obj is not None else None
        return g.find_uuid(u) if u else None

    def trigger_event(obj, event):
        """The Begin (enter) or End (exit) event node of a walk-in trigger object."""
        if obj is None:
            return None
        sec = sections.get(obj.get('c2_name', obj.name)) or sections.get(obj.name)
        if sec is None:
            return None
        u = next((kv.value for kv in sec.kvs if kv.key in ('WalkInTrigger_Uuid', 'WalkInTriggerFiltered_Uuid')), None)
        gi = g.find_uuid(u) if u else None
        if gi is None:
            return None
        ports = g.trigger_ports(gi)
        return ports.get('begin' if event == 'ENTER' else 'end')

    # field edits first (no index changes)
    for c in scene.c2.choreos:
        if not c.uuid:
            continue
        ci = g.find_uuid(c.uuid)
        if ci is None or g.nodes[ci].get('Type') != 'Choreography':
            report.warn('choreography %s not found in the source .mer' % c.authored)
            continue
        n = g.nodes[ci]
        new = meridian.choreo_path(c.choreography_name)
        # same path written with '\' in the file (tokyo_c, tutorialadvanced): keep it
        if meridian.choreo_path(n.get('choreography_name', '')) != new:
            n['choreography_name'] = new
        for f in meridian.CHOREO_FLAGS:
            n[f] = int(bool(getattr(c, f)))
    # structural edits
    for c in scene.c2.choreos:
        if c.uuid:
            if g.find_uuid(c.uuid) is None:
                continue
            cu = c.uuid
        else:
            flags = {f: getattr(c, f) for f in meridian.CHOREO_FLAGS}
            ci = g.add_choreography(c.choreography_name, c.authored or 'Choreography', flags)
            cu = meridian.uuid_hex(g.nodes[ci]['Uuid'])
            report.info('new choreography "%s" -> %s' % (c.authored, meridian.choreo_path(c.choreography_name)))
        for a in c.anchors:
            ts = ts_index(a.locator)
            if a.locator is not None and ts is None:
                report.warn('choreography %s anchor %s: %s is not a Meridian locator' % (c.authored, a.name, a.locator.name))
            ref = g.find_uuid(a.ref_uuid) if a.ref_uuid else None
            if ref is not None:
                cur = g.link_sources(g.nodes[ref].get('In', -1), 'TransformFetch')
                if ts is not None and (not cur or cur[0] != ts):
                    g.set_anchor_source(ref, ts)
                    report.info('choreography %s anchor %s -> %s' % (c.authored, a.name, a.locator.name))
                g.nodes[ref]['Name'] = a.name
            else:
                g.add_choreo_anchor(g.find_uuid(cu), a.name, ts)
        for t in c.triggers:
            if t.exists:
                continue
            ev = trigger_event(t.obj, t.event)
            if ev is None:
                report.warn('choreography %s: %s is not a walk-in trigger (no .ap WalkInTrigger)' %
                            (c.authored, t.obj.name if t.obj else '?'))
                continue
            ci = g.find_uuid(cu)
            if ci not in g.link_targets(ev, 'Impulse'):
                g.connect_impulse(ev, ci)
                report.info('trigger %s (%s) plays %s' % (t.obj.name, t.event.lower(), c.authored))
        if c.play_on_start and not c.start_exists:
            cat = g.add_catalyst()
            g.connect_impulse(cat, g.find_uuid(cu))
            report.info('%s plays when the level loads (new Catalyst)' % c.authored)
        elif not c.play_on_start and c.start_exists:
            ci = g.find_uuid(cu)
            for cat in g.start_catalysts(ci):
                for li in list(g.out_links.get(cat, [])):
                    if g.links[li]['Target'] == ci:
                        g.remove_link(li)
                        break
            report.info('%s no longer plays on level load' % c.authored)
        if actor_ctx is not None:
            add_choreo_actors(scene, g, c, cu, actor_ctx, report)
        ci = g.find_uuid(cu)
        restart = g.nodes[ci].get('Restart')
        started = g.link_sources(ci, 'Impulse') or (restart is not None and g.link_sources(restart))
        if not c.uuid and not started:
            report.warn('choreography %s: nothing starts it (no trigger, no level start, no graph input)'
                        % c.authored)


def add_choreo_actors(scene, g, c, cu, ctx, report):
    """World actors for the choreography's characters (scene node + .ap actor +
    graph group, bound by a ChoreographyActorReference), as radiator_a does
    for its background characters."""
    ci = g.find_uuid(cu)
    bound = set(g.choreo_actor_refs(ci))
    nodes, root = ctx['nodes'], ctx['root']
    taken = {n.get('NodeName') for n in nodes} | {a.name for a in scene.c2.actors} | {s[0] for s in ctx['ap']}
    loc = next((a.locator for a in c.anchors if a.locator is not None), None)
    m = b2g(common.game_matrix(root, loc)) if loc is not None else list(octscene.IDENTITY16)
    for r in range(3):   # the choreography places and scales the character; the actor root stays unscaled
        s = float(np.linalg.norm(m[4 * r:4 * r + 3])) or 1.0
        m[4 * r:4 * r + 3] = [v / s for v in m[4 * r:4 * r + 3]]
    for a in c.actors:
        if not a.spawn or a.name in bound:
            continue
        base = 'C2_%s' % a.name
        name, k = base, 2
        while name in taken:
            name, k = '%s_%d' % (base, k), k + 1
        taken.add(name)
        nodes.append(gameplay.actor_scene_node(name, m))
        ni = len(nodes) - 1
        for asc in ctx['assoc_list']:
            if asc.get('Type') == 'CellLink':
                asc['LinkedSceneTreeNodeRefs'] = IntList(list(asc.get('LinkedSceneTreeNodeRefs', [])) + [ni])
                break
        grp, handle = g.add_actor_group(name)
        ctx['ap'].append(gameplay.actor_ap_section(name, g.namespace, meridian.uuid_hex(g.nodes[grp]['Uuid']),
                                                   a.model))
        g.add_choreo_actor(g.find_uuid(cu), a.name, handle)
        report.info('%s: new actor %s (%s) plays %s' % (c.authored, name, a.model, a.name))


class EXPORT_OT_cars2_scene(bpy.types.Operator):
    """Export the world (.oct and side files) to the export folder"""
    bl_idname = 'export_scene.cars2_scene'
    bl_label = 'Export Cars 2 Scene'
    bl_description = 'Write the edited world (.oct, _0.vbuf, _0.ibuf, .btm, .btd, .mer, .ap) to the export folder'
    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        if context.scene.c2.root is None:
            self.report({'ERROR'}, 'Import a Cars 2 scene first')
            return {'CANCELLED'}
        missing = missing_texture_lines(context.scene)
        if missing:
            self._refuse(MissingTextures(missing))
            return {'CANCELLED'}
        self.directory = bpy.path.abspath(context.scene.c2.export_dir) if context.scene.c2.export_dir else ''
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        out = bpy.path.abspath(self.directory or context.scene.c2.export_dir)
        src_dir = os.path.dirname(bpy.path.abspath(context.scene.c2.oct_path))
        if os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(src_dir)):
            self.report({'ERROR'}, 'Export folder is the source folder - pick another folder (the exporter re-reads the originals)')
            return {'CANCELLED'}
        try:
            rep = export_scene(context, out)
        except MissingTextures as e:
            self._refuse(e)
            return {'CANCELLED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, 'Export failed: %s' % e)
            return {'CANCELLED'}
        context.scene.c2.export_dir = out
        self.report({'WARNING'} if rep.warnings else {'INFO'},
                    'Exported to %s (%s%d warnings - see Export Report)'
                    % (out, '%d mesh(es) LEFT OUT, ' % rep.left_out if rep.left_out else '', rep.warnings))
        return {'FINISHED'}


    def _refuse(self, e):
        """Report the empty slots (all of them in the console)."""
        print('Cars 2 export refused, texture slots need a texture:')
        for line in e.lines:
            print('   ', line)
        self.report({'ERROR'}, 'Not exported: %s (pick textures in the Material panel; full list in the '
                               'console)' % e)


def menu_export(self, context):
    """File > Export menu entry."""
    self.layout.operator(EXPORT_OT_cars2_scene.bl_idname, text='Cars 2 Scene (.oct)')


def register():
    bpy.utils.register_class(EXPORT_OT_cars2_scene)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.utils.unregister_class(EXPORT_OT_cars2_scene)
