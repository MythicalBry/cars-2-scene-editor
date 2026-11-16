"""World scene (.oct + _0.vbuf + _0.ibuf) access and rebuilding.

Conventions (game side):
    * Y-up, row-vector 4x4 matrices: v' = v @ M, translation in row 3.
    * LocalToParentMatrix composes as local @ parentWorld.
    * Geometry nodes own SubGeometry(/Lit) children; SubGeometry vertex data
      is in its Geometry node's local space (SubGeometry L2P is identity in
      every shipping world).

The main pools of a world .oct (each a dict of numbered entries):
    SceneTreeNodePool    every node: Scene root, Transform, Geometry,
                         SubGeometry(Lit), Light, Camera ... with parent links
    MaterialPool         materials (effect name, parameters, texture slots)
    TexturePool          textures (each a complete DDS file in 'Data')
    VertexStreamPool     slices of <world>_0.vbuf (see vertexfmt)
    IndexStreamPool      slices of <world>_0.ibuf
    VertexBufferPool / IndexBufferPool   the buffer files themselves
A SubGeometry node is one drawable part: a material + vertex streams + an
index stream. A Geometry node groups the parts of one mesh.
"""
import os
import numpy as np

import c2oct
from c2oct import IntList, FloatList, StrList
import vertexfmt as vf

# 4x4 identity as the flat 16-float list the .oct stores
IDENTITY16 = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def mat_from_list(flat):
    """Flat 16 floats -> 4x4 NumPy matrix (row-vector convention)."""
    return np.array(flat, dtype=np.float64).reshape(4, 4)


def is_subgeo(n):
    """True for SubGeometry / SubGeometryLit nodes (drawable mesh parts)."""
    return n.get('Type', '').startswith('SubGeometry')


class OctScene:
    """A loaded world: the decoded .oct tree plus lazy access to its buffers."""

    def __init__(self, oct_path, raw=None, files=None):
        """oct_path on disk, or raw .oct bytes plus `files` (name -> bytes,
        e.g. read from a choreography/character zip) for the buffers."""
        self.path = oct_path
        self.folder = os.path.dirname(oct_path)
        # world name = file name without .oct ('radiator_a')
        self.name = os.path.basename(oct_path)[:-4] if oct_path.lower().endswith('.oct') else os.path.basename(oct_path)
        self.files = files
        if raw is None:
            with open(oct_path, 'rb') as f:
                raw = f.read()
        self.raw = raw
        self.tree, self.endian = c2oct.decode(self.raw)
        t = self.tree
        # the pools as plain lists (index = the number other entries refer to)
        self.nodes = list(t['SceneTreeNodePool'].values())
        self.vstreams = list(t.get('VertexStreamPool', {}).values())
        self.istreams = list(t.get('IndexStreamPool', {}).values())
        self.materials = list(t.get('MaterialPool', {}).values())
        self.textures = list(t.get('TexturePool', {}).values())
        self._vb = {}           # vertex buffer bytes, read on first use
        self._ib = {}           # index buffer (bytes, width), read on first use
        self._tail = None       # cached index_tail answer
        self._world = {}        # cached world matrices
        # parent index -> child indices
        self.children = {}
        for i, n in enumerate(self.nodes):
            pr = n.get('ParentNodeReferences')
            if pr:
                self.children.setdefault(pr[0], []).append(i)

    # -- buffers -----------------------------------------------------------
    def _read_file(self, fn):
        """Bytes of a side file (vbuf/ibuf) next to the .oct, or from `files`."""
        if not fn:
            return b''
        if self.files is not None:
            for k, v in self.files.items():
                if k.lower() == fn.lower() or k.lower().endswith('/' + fn.lower()):
                    return v
            return b''
        p = os.path.join(self.folder, fn)
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return f.read()
        return b''

    def vbuf(self, ref):
        """Bytes of VertexBuffer #ref (usually the single <world>_0.vbuf)."""
        if ref not in self._vb:
            vb = self.tree['VertexBufferPool'].get('VertexBuffer#%d' % ref, {})
            self._vb[ref] = self._read_file(vb.get('FileName'))
        return self._vb[ref]

    def ibuf(self, ref):
        """(bytes, index width) of IndexBuffer #ref."""
        if ref not in self._ib:
            ib = self.tree['IndexBufferPool'].get('IndexBuffer#%d' % ref, {})
            self._ib[ref] = (self._read_file(ib.get('FileName')), ib.get('Width', 2))
        return self._ib[ref]

    # -- transforms --------------------------------------------------------
    def local(self, i):
        """Node i's LocalToParentMatrix as 4x4."""
        return mat_from_list(self.nodes[i].get('LocalToParentMatrix', IDENTITY16))

    def parent(self, i):
        """Index of node i's parent (None for the root)."""
        pr = self.nodes[i].get('ParentNodeReferences')
        return pr[0] if pr else None

    def world(self, i):
        """Node i's world matrix (local @ parent world, cached)."""
        if i in self._world:
            return self._world[i]
        p = self.parent(i)
        w = self.local(i) if p is None else self.local(i) @ self.world(p)
        self._world[i] = w
        return w

    # -- geometry ----------------------------------------------------------
    def subgeos(self, geo):
        """Indices of the drawable parts of Geometry node `geo`."""
        return [j for j in self.children.get(geo, []) if is_subgeo(self.nodes[j])]

    def stream_layout(self, refs):
        """[(width, [(name, type, offset)])] for vertex streams `refs` - the
        exporter reuses it to write new data in the same layout."""
        return [(self.vstreams[r]['Width'], vf.stream_elements(self.vstreams[r]) if self.vstreams[r]['Length'] else [])
                for r in refs]

    def read_vertices(self, refs):
        """Decode vertex streams `refs` -> ({element name: array}, vertex count)."""
        attrs = {}
        n = 0
        for r in refs:
            s = self.vstreams[r]
            if not s['Length']:
                continue
            n = s['Length']
            for k, v in vf.read_stream(s, self.vbuf(s['VertexBufferReference'])).items():
                attrs.setdefault(k, v)      # first stream wins if a name repeats
        return attrs, n

    @property
    def index_tail(self):
        """Arcade (X360-derived Win32) worlds follow every index stream with a
        second list of the same length that the renderer never reads (it draws
        Length indices from IndexBufferOffset). True when the static index
        buffer is exactly twice the size of its streams."""
        if self._tail is None:
            data, width = self.ibuf(0)
            used = sum(s['Length'] for s in self.istreams if s.get('IndexBufferReference', 0) == 0) * width
            self._tail = bool(used) and len(data) == 2 * used
        return self._tail

    def read_index_stream(self, r):
        """Indices of IndexStream r."""
        s = self.istreams[r]
        data, width = self.ibuf(s['IndexBufferReference'])
        return vf.read_indices(s, data, width)

    def geometry_parts(self, geo):
        """Decoded SubGeometry children of Geometry node `geo`.
        Parts sharing a vertex pool share `pool` (index into the returned pools)."""
        pools, pool_key = [], {}
        parts = []
        for j in self.subgeos(geo):
            n = self.nodes[j]
            refs = tuple(n.get('VertexStreamReferences') or ())
            if not refs:
                continue
            if refs not in pool_key:
                attrs, nv = self.read_vertices(refs)
                if 'Position' not in attrs:
                    continue
                pool_key[refs] = len(pools)
                pools.append({'attrs': attrs, 'count': nv, 'refs': refs})
            idx = self.read_index_stream(n['IndexStreamReference'])
            parts.append({'node': j, 'pool': pool_key[refs], 'indices': idx,
                          'material': n.get('MaterialReference', 0)})
        return pools, parts


class BufferBuilder:
    """Rebuilds VertexStreamPool / IndexStreamPool and the Static buffers.

    keep_*() copies an original stream's bytes unchanged (memoised, so shared
    streams stay shared); add_*() appends freshly encoded data.

    On export every stream is written into one new vbuf/ibuf pair: untouched
    meshes are copied byte-for-byte, edited/new meshes are encoded fresh. The
    returned numbers are the new stream indices to put into the nodes."""

    def __init__(self, scene):
        self.scene = scene
        self.vb = bytearray()           # the new _0.vbuf
        self.ib = bytearray()           # the new _0.ibuf
        self.vpool = []                 # the new VertexStreamPool entries
        self.ipool = []                 # the new IndexStreamPool entries
        self.tail = scene.index_tail    # write the unused duplicate index list too (Arcade)
        self._vmap = {}                 # old vertex stream index -> new index
        self._imap = {}                 # old index stream index -> new index

    def keep_vstream(self, old):
        """Copy original vertex stream `old` unchanged -> new stream index."""
        if old in self._vmap:
            return self._vmap[old]
        s = dict(self.scene.vstreams[old])
        size = s['Width'] * s['Length']
        if size:
            data = self.scene.vbuf(s['VertexBufferReference'])
            chunk = data[s['VertexBufferOffset']:s['VertexBufferOffset'] + size]
        else:
            chunk = b''
        s['VertexBufferReference'] = 0
        s['VertexBufferOffset'] = len(self.vb)
        self.vb += chunk
        self._vmap[old] = len(self.vpool)
        self.vpool.append(s)
        return self._vmap[old]

    def keep_istream(self, old):
        """Copy original index stream `old` unchanged -> new stream index
        (re-encoded as 16-bit if it wasn't)."""
        if old in self._imap:
            return self._imap[old]
        s = dict(self.scene.istreams[old])
        data, width = self.scene.ibuf(s['IndexBufferReference'])
        if width != 2:
            idx = vf.read_indices(s, data, width)
            return self.add_indices(idx, old=old)
        # 2 bytes per index, doubled when the world has the duplicate tail list
        chunk = data[s['IndexBufferOffset']:s['IndexBufferOffset'] + 2 * s['Length'] * (2 if self.tail else 1)]
        s['IndexBufferReference'] = 0
        s['IndexBufferOffset'] = len(self.ib)
        self.ib += chunk
        self._imap[old] = len(self.ipool)
        self.ipool.append(s)
        return self._imap[old]

    def add_vstreams(self, layout, attrs, n):
        """layout: [(width, [(name, type, offset)])] -> list of new stream indices."""
        out = []
        for width, elems in layout:
            if not elems:
                # an empty placeholder stream (kept so stream counts match the layout)
                s = {'Length': 0, 'Width': 0, 'ExtraStride': 0, 'VertexBufferReference': 0,
                     'VertexBufferOffset': len(self.vb), 'Elements': {}}
            else:
                data = vf.write_stream(elems, width, attrs, n)
                s = {'Length': n, 'Width': width, 'ExtraStride': 0, 'VertexBufferReference': 0,
                     'VertexBufferOffset': len(self.vb),
                     'Elements': {'Element#%d' % k: {'Type': ty, 'Name': name, 'Offset': off}
                                  for k, (name, ty, off) in enumerate(elems)}}
                self.vb += data
            out.append(len(self.vpool))
            self.vpool.append(s)
        return out

    def add_indices(self, idx, old=None):
        """Append a triangle list as 16-bit indices -> new stream index.
        (16-bit means at most 65,535 vertices per part; the exporter splits
        bigger meshes.)"""
        idx = np.asarray(idx, dtype=np.int64)
        if idx.size and idx.max() > 0xFFFF:
            raise ValueError('index stream needs >16 bits')
        s = {'Length': int(idx.size), 'IndexBufferReference': 0, 'IndexBufferOffset': len(self.ib),
             'IndexStreamPrimitive': 'TRIANGLES', 'IndexStreamElement': ''}
        self.ib += idx.astype('<u2').tobytes()
        if self.tail:
            self.ib += idx.astype('<u2').tobytes()      # the unused duplicate (see index_tail)
        k = len(self.ipool)
        self.ipool.append(s)
        if old is not None:
            self._imap[old] = k
        return k

    def apply(self, tree, name):
        """Put the rebuilt pools into `tree` and point the buffer entries at
        <name>_0.vbuf / <name>_0.ibuf with their new sizes."""
        tree['VertexStreamPool'] = {'VertexStream#%d' % i: s for i, s in enumerate(self.vpool)}
        tree['IndexStreamPool'] = {'IndexStream#%d' % i: s for i, s in enumerate(self.ipool)}
        vbp = tree['VertexBufferPool']
        for k, v in vbp.items():
            if v.get('FileName'):
                v['FileName'] = '%s_0.vbuf' % name
                v['Size'] = len(self.vb)
                break
        ibp = tree['IndexBufferPool']
        for k, v in ibp.items():
            if v.get('FileName'):
                v['FileName'] = '%s_0.ibuf' % name
                v['Size'] = len(self.ib)
                break


def bounds(positions):
    """Geometry-node bounding fields for local-space positions (n,3).

    The game's culling (FUN_00858c20 in sdaemon.exe) builds a sphere from
    BoundingSphereRadius and a box from BoundingBox (min xyz, max xyz) and
    keeps the smaller one - nearly always the box. Both are in the node's
    local space."""
    p = np.asarray(positions, dtype=np.float64)
    if p.size == 0:
        p = np.zeros((1, 3))
    mn, mx = p.min(0), p.max(0)
    c = (mn + mx) * 0.5
    ext = (mx - mn) * 0.5
    r = float(np.sqrt(((p - c) ** 2).sum(1)).max())
    return {
        'BoundingSphereCenter': FloatList(c.tolist()),
        'BoundingSphereRadius': r,
        'BoundingOBBOrientation': FloatList([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
        'BoundingOBBCenter': FloatList(c.tolist()),
        'BoundingOBBExtents': FloatList(ext.tolist()),
        'BoundingBox': FloatList(mn.tolist() + mx.tolist()),
    }


def default_layout(has_color=True):
    """Most common shipping world layout: [Position f3 + Uv1 f2], [Normal f4], [ColorPrelight u8n]."""
    lay = [(20, [('Position', vf.F32X3, 0), ('Uv1', vf.F32X2, 12)]),
           (16, [('Normal', vf.F32X4, 0)])]
    if has_color:
        lay.append((4, [('ColorPrelight', vf.UNORM8X4, 0)]))
    return lay
