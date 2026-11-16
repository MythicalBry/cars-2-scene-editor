"""Arena nav meshes: the `Data` blob of each .oct NavMeshPool entry.

Read by Navigate::NavMeshLoader (sdaemon 0x00da7f00 fixes the offsets up,
0x00daa070 walks the octree). Little endian:

    0x00 u32 triangle table offset, u32 vertex table offset, u32 octree offset
    0x0c u16 triangle count, u16 vertex count
    0x10 float3 centre, float half size     (root octree cube; also a bound)
    0x20 char[64] name ('NavMesh23')
    triangles, 16 bytes: u32 flags, u16 neighbour[3] (0xFFFF = open edge),
                         u16 vertex[3]; edge e runs vertex[e] -> vertex[e+1]
    vertices: float3
    octree, depth-first preorder, 36 bytes per node:
        internal: u32 0, u32 child[8]           (0xFFFFFFFF = empty)
        leaf:     u32 0x80000000, u32 n tris, u32 n open edges,
                  u32 tri list (u16 * n), u32 edge tri list (u8 * m, index
                  into this leaf's tri list), u32 edge list (u8 * m, 4 + edge),
                  u32 0 * 3                      (-1 offsets when m == 0)
        child i centre = centre + OFFSET[i] * half / 2, half /= 2
    then every leaf's tri list, then every edge-tri list, then every edge list.

Triangle flags are stored as a big-endian-authored bitfield that the loader
(0x00db77e0) reorders; in file order: bits 31-23, 22-15, 14, 13-11, 10-8,
7-5, 4-0. Only the three 3-bit fields 13-11 / 10-8 / 7-5 are ever set in
shipping data (0 or 1, one per edge, edges 0/1/2). Their meaning is not
decoded yet, so they are kept as-is (0 on new triangles).

What it is for: arena (battle / mission) AI drives on these triangle meshes.
Neighbour links let the path finder walk from triangle to triangle; the octree
lets it find which triangle a car is on quickly. Open edges (no neighbour)
are the walls the AI must not cross.
"""
import struct

# child cube directions, in the order the game expects children
OFFSET = ((1, 1, 1), (-1, 1, 1), (1, -1, 1), (-1, -1, 1),
          (1, 1, -1), (-1, 1, -1), (1, -1, -1), (-1, -1, -1))
LEAF = 0x80000000            # first word of a leaf node
NONE32 = 0xFFFFFFFF          # "no child" / "no list"
OPEN = 0xFFFF                # "no neighbour across this edge"
MAX_LEAF_TRIS = 16           # shipping meshes never have more per leaf
MAX_DEPTH = 8                # ... nor deeper trees


def edge_flags(flags):
    """The three per-edge 3-bit fields of a triangle's flags (edges 0, 1, 2)."""
    return ((flags >> 11) & 7, (flags >> 8) & 7, (flags >> 5) & 7)


def with_edge_flags(flags, e):
    """flags with the three per-edge fields replaced by e[0..2]."""
    flags &= ~((7 << 11) | (7 << 8) | (7 << 5))
    return flags | (e[0] & 7) << 11 | (e[1] & 7) << 8 | (e[2] & 7) << 5


class Leaf:
    """Octree leaf: the triangles touching its cube and their open edges."""
    __slots__ = ('tris', 'edges', 'extra')

    def __init__(self, tris, edges, extra=(0, 0, 0)):
        self.tris = list(tris)          # triangle indices
        self.edges = list(edges)        # (index into self.tris, edge 0..2)
        self.extra = extra              # three trailing words (always 0 in shipping data)


class Node:
    """Octree internal node: 8 children."""
    __slots__ = ('children',)

    def __init__(self, children):
        self.children = list(children)  # 8 entries, Node / Leaf / None


class NavMesh:
    """One nav mesh: vertices, triangles and the octree over them."""

    def __init__(self, name, verts, tris, centre, half, tree=None):
        self.name = name
        self.verts = [tuple(v) for v in verts]
        self.tris = [list(t) for t in tris]   # [flags, n0, n1, n2, v0, v1, v2]
        self.centre, self.half = tuple(centre), half
        self.tree = tree

    # -- parse -------------------------------------------------------------
    @staticmethod
    def parse(d):
        """NavMeshPool `Data` bytes -> NavMesh (layout in the module docstring)."""
        ot, ov, oo = struct.unpack_from('<3I', d, 0)
        nt, nv = struct.unpack_from('<HH', d, 12)
        cx, cy, cz, half = struct.unpack_from('<4f', d, 16)
        name = d[0x20:0x60].split(b'\0', 1)[0].decode('latin1')
        tris = [list(struct.unpack_from('<I6H', d, ot + 16 * i)) for i in range(nt)]
        verts = [struct.unpack_from('<3f', d, ov + 12 * i) for i in range(nv)]

        def node(o):
            # read the octree node at offset o (recursively)
            fl, = struct.unpack_from('<I', d, o)
            if fl & LEAF:
                n, m, a, b, c, x0, x1, x2 = struct.unpack_from('<8I', d, o + 4)
                tl = struct.unpack_from('<%dH' % n, d, a) if n else ()
                ei = d[b:b + m] if m else b''
                ee = d[c:c + m] if m else b''
                # edge numbers are stored as 4 + edge
                return Leaf(tl, [(i, e - 4) for i, e in zip(ei, ee)], (x0, x1, x2))
            ch = struct.unpack_from('<8I', d, o + 4)
            return Node([None if x == NONE32 else node(x) for x in ch])
        return NavMesh(name, verts, tris, (cx, cy, cz), half, node(oo))

    # -- build -------------------------------------------------------------
    def bounds(self):
        """Centre and half size of a cube around all vertices."""
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        c = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
        h = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2
        return c, h

    def rebuild_adjacency(self):
        """Neighbours from shared edges (open edges -> 0xFFFF)."""
        # map each undirected edge (low vertex, high vertex) to the triangles using it
        owner = {}
        for ti, t in enumerate(self.tris):
            for e in range(3):
                a, b = t[4 + e], t[4 + (e + 1) % 3]
                owner.setdefault((min(a, b), max(a, b)), []).append((ti, e))
        for t in self.tris:
            t[1:4] = [OPEN, OPEN, OPEN]
        # an edge shared by exactly two triangles links them; edges used by
        # one (border) or 3+ (non-manifold) stay open
        for lst in owner.values():
            if len(lst) == 2:
                (t0, e0), (t1, e1) = lst
                self.tris[t0][1 + e0] = t1
                self.tris[t1][1 + e1] = t0

    def rebuild_tree(self):
        """Octree from the triangles: root cube around the vertices, a
        triangle goes into every cell its bounding box touches, cells split
        until <= 16 triangles or depth 8 (shipping meshes: max 16, depth 8)."""
        self.centre, self.half = self.bounds()
        self.half = max(self.half, 1e-3) * 1.001      # tiny margin so border vertices are inside
        # bounding box of every triangle
        boxes = []
        for t in self.tris:
            p = [self.verts[i] for i in t[4:7]]
            boxes.append((tuple(min(q[k] for q in p) for k in range(3)), tuple(max(q[k] for q in p) for k in range(3))))

        def touches(i, c, h):
            # does triangle i's box overlap the cube (centre c, half size h)?
            lo, hi = boxes[i]
            return all(hi[k] >= c[k] - h and lo[k] <= c[k] + h for k in range(3))

        def build(ids, c, h, depth):
            if len(ids) <= MAX_LEAF_TRIS or depth >= MAX_DEPTH:
                # leaf: also list the open edges of its triangles
                edges = [(li, e) for li, ti in enumerate(ids) for e in range(3) if self.tris[ti][1 + e] == OPEN]
                return Leaf(ids, edges)
            kids = []
            for off in OFFSET:
                cc = tuple(c[k] + off[k] * h * 0.5 for k in range(3))
                sub = [i for i in ids if touches(i, cc, h * 0.5)]
                kids.append(build(sub, cc, h * 0.5, depth + 1) if sub else None)
            return Node(kids)
        self.tree = build(list(range(len(self.tris))), self.centre, self.half, 0)

    # -- pack --------------------------------------------------------------
    def pack(self):
        """NavMesh -> NavMeshPool `Data` bytes (builds the octree if needed)."""
        if self.tree is None:
            self.rebuild_tree()
        # every node in depth-first pre-order (the file order)
        nodes = []

        def collect(n):
            nodes.append(n)
            if isinstance(n, Node):
                for c in n.children:
                    if c is not None:
                        collect(c)
        collect(self.tree)
        # work out where everything goes before writing
        ot = 0x60                                   # triangles right after the 96-byte header
        ov = ot + 16 * len(self.tris)
        oo = ov + 12 * len(self.verts)
        pos = {id(n): oo + 36 * i for i, n in enumerate(nodes)}
        leaves = [n for n in nodes if isinstance(n, Leaf)]
        p = oo + 36 * len(nodes)
        tl_off, ei_off, ee_off = {}, {}, {}
        for lf in leaves:
            tl_off[id(lf)] = p if lf.tris else NONE32
            p += 2 * len(lf.tris)
        for lf in leaves:
            ei_off[id(lf)] = p if lf.edges else NONE32
            p += len(lf.edges)
        for lf in leaves:
            ee_off[id(lf)] = p if lf.edges else NONE32
            p += len(lf.edges)
        # header, name, triangles, vertices
        out = bytearray(struct.pack('<3IHH4f', ot, ov, oo, len(self.tris), len(self.verts), *self.centre, self.half))
        out += self.name.encode('latin1')[:63].ljust(64, b'\0')
        for t in self.tris:
            out += struct.pack('<I6H', *t)
        for v in self.verts:
            out += struct.pack('<3f', *v)
        # octree nodes
        for n in nodes:
            if isinstance(n, Leaf):
                out += struct.pack('<9I', LEAF, len(n.tris), len(n.edges), tl_off[id(n)], ei_off[id(n)],
                                   ee_off[id(n)], *n.extra)
            else:
                out += struct.pack('<9I', 0, *[NONE32 if c is None else pos[id(c)] for c in n.children])
        # the per-leaf lists, in the same order the offsets were assigned
        for lf in leaves:
            out += struct.pack('<%dH' % len(lf.tris), *lf.tris)
        for lf in leaves:
            out += bytes(i for i, _ in lf.edges)
        for lf in leaves:
            out += bytes(4 + e for _, e in lf.edges)
        out += b'\0' * (len(out) & 1)               # pad to an even size
        return bytes(out)
