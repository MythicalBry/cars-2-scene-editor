"""Cars 2 Bullet collision data (.btm metadata + .btd load-in-place data).

Adapted (bpy-free) from the "Cars 2 BTD_BTM Collision IO" add-on: the BTM/BTD
layout, the BVH block parser and the btOptimizedBvh / btQuantizedBvh rebuild
port are that add-on's work (MIT, used with permission); this module only wraps
them in a file-level API and speeds up the bulk array reads with NumPy.
The rebuild is an altered (Python) port of Bullet Physics' btQuantizedBvh /
btOptimizedBvh, Copyright (c) 2003-2006 Erwin Coumans, zlib licence - see
THIRD_PARTY_NOTICES.md.

One BTD entry == one CollisionShapePool "TriangleList" shape; the shape's
`SerializeIndex` is its entry index in the BTM.  Vertex positions are in the
*shape's* local space - the CollisionInstance Transform places it in the world
(confirmed against Avalanche's ExportBullet filter: Transform = the collision
node's local-to-world matrix with scale removed).

Entry dict:
    positions    (n,3) float32
    indices      (m*3,) int  (uint16 on disk)
    face_attrs   [(surface_type, color), ...]   unique attribute records
    attr_indices (m,) int    per-face index into face_attrs
    bvh          parsed BVH dict (raw node bytes) or None -> rebuild on write
    align, itype BTM per-entry alignment / type

Files:
    .btm   u32 magic 0xC001D00D, u32 count, then per entry (u32 offset into
           the .btd, u16 alignment, u16 type)
    .btd   the entries back to back; each one is a 0x30-byte header of
           offsets/counts, then a BVH block (Bullet's bounding-volume tree
           that makes collision queries fast), positions, indices, face
           attributes and per-face attribute indices.
surface_type in a face attribute is an index into the world .dbl's
surface-type names (asphalt, barrier_Concrete, water ...).
"""
import struct
import hashlib
import numpy as np

BTM_MAGIC = 0xC001D00D
DEFAULT_ALIGNMENT = 16


def _u16_to_i16(v):
    """Reinterpret an unsigned 16-bit value as signed."""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _u8_to_i8(v):
    """Reinterpret an unsigned 8-bit value as signed."""
    v &= 0xFF
    return v - 0x100 if v >= 0x80 else v


# ----------------------------------------------------------------------
# BTM
# ----------------------------------------------------------------------

def read_btm(path):
    """-> ([{'Offset', 'Align', 'Type'} per entry], endian)."""
    with open(path, 'rb') as f:
        data = f.read()
    # the magic tells the byte order (PC little-endian, consoles big-endian)
    magic_le = struct.unpack_from('<I', data, 0)[0]
    if magic_le == BTM_MAGIC:
        e = '<'
    elif struct.unpack_from('>I', data, 0)[0] == BTM_MAGIC:
        e = '>'
    else:
        raise ValueError('%s is not a BTM file (bad magic)' % path)
    (count,) = struct.unpack_from(e + 'I', data, 4)
    out = []
    for i in range(count):
        off, align, itype = struct.unpack_from(e + 'IHH', data, 8 + 8 * i)
        out.append({'Offset': off, 'Align': align, 'Type': itype})
    return out, e


def write_btm(path, instances, endian='<'):
    """Write the .btm entry table."""
    with open(path, 'wb') as f:
        f.write(struct.pack(endian + 'I', BTM_MAGIC))
        f.write(struct.pack(endian + 'I', len(instances)))
        for inst in instances:
            f.write(struct.pack(endian + 'IHH', inst['Offset'], inst['Align'], inst['Type']))


# ----------------------------------------------------------------------
# BVH block (parse / re-emit)
# ----------------------------------------------------------------------

# bytes before the first node: 5 x 16-byte rows, 0x64 unknown bytes,
# subtree count, 8 padding bytes
BVH_HEADER_SIZE = 16 * 5 + 0x64 + 4 + 8


def parse_bvh_block(data, endian, offset, size_hint=None):
    """Read a BVH block. Nodes are kept as raw bytes (re-emitted as they
    were), only their kinds are decoded so the block can be walked."""
    p = offset
    block_end = offset + size_hint if size_hint is not None else None

    def take(n):
        # next n bytes, advancing p
        nonlocal p
        b = data[p:p + n]
        if len(b) != n:
            raise ValueError('truncated BVH block')
        p += n
        return b

    unknown = struct.unpack(endian + 'ixxxxxxxxxxxx', take(16))[0]
    aabb_min = struct.unpack(endian + 'fffxxxx', take(16))           # tree bounds
    aabb_max = struct.unpack(endian + 'fffxxxx', take(16))
    quantization = struct.unpack(endian + 'fffxxxx', take(16))       # world -> 16-bit scale
    bullet_version, current_node_index, use_quantization, traversal_mode = \
        struct.unpack(endian + 'iibxxxi', take(16))
    unknown2 = bytes(take(0x64))
    (subtree_header_count,) = struct.unpack(endian + 'I', take(4))
    take(8)

    nodes_raw = []
    i = 0
    while i < current_node_index:
        node_start = p
        # the node kind lives in the high bits of the node's last word
        (raw_trailing,) = struct.unpack_from(endian + 'I', data, node_start + 12)
        hi = (raw_trailing >> 16) & 0xFFFF
        is_leaf = (hi & (1 << 14)) != 0
        if not is_leaf:
            nodes_raw.append(('internal', bytes(take(16))))
            i += 1
            continue
        # a leaf with an extra triangle-index node takes 32 bytes (2 node slots)
        has_tri_index_node = ((((hi & (1 << 14)) << 1) & (hi & (1 << 15))) != 0)
        if has_tri_index_node:
            nodes_raw.append(('leaf2', bytes(take(32))))
            i += 2
        else:
            nodes_raw.append(('leaf', bytes(take(16))))
            i += 1
    subtrees_raw = [bytes(take(32)) for _ in range(subtree_header_count)]
    if block_end is not None and p != block_end:
        raise ValueError('BVH size mismatch (parser at 0x%X, block ends 0x%X)' % (p, block_end))
    return {
        'unknown_int': unknown, 'aabb_min': aabb_min, 'aabb_max': aabb_max,
        'quantization': quantization, 'bullet_version': bullet_version,
        'current_node_index': current_node_index, 'use_quantization': use_quantization,
        'traversal_mode': traversal_mode, 'unknown2': unknown2,
        'subtree_header_count': subtree_header_count,
        'nodes_raw': nodes_raw, 'subtrees_raw': subtrees_raw, '_src_endian': endian,
    }


def _reswap_node_blob(kind, blob, src_endian, dst_endian):
    """Convert a raw node's byte order (PC <-> console)."""
    if src_endian == dst_endian:
        return blob
    if kind == 'internal':
        return struct.pack(dst_endian + 'hhhhhhi', *struct.unpack(src_endian + 'hhhhhhi', blob))
    if kind == 'leaf':
        return struct.pack(dst_endian + 'bbbbbbbbbbbbi', *struct.unpack(src_endian + 'bbbbbbbbbbbbi', blob))
    v1 = struct.unpack(src_endian + 'bbbbbbbbbbbbi', blob[:16])
    v2 = struct.unpack(src_endian + 'hhhhhhhh', blob[16:])
    return struct.pack(dst_endian + 'bbbbbbbbbbbbi', *v1) + struct.pack(dst_endian + 'hhhhhhhh', *v2)


def _reswap_subtree_blob(blob, src_endian, dst_endian):
    # subtree headers are stored big-endian in both file variants
    return blob


def bvh_block_size(bvh):
    """Byte size of a BVH block once written."""
    return BVH_HEADER_SIZE + sum(32 if k == 'leaf2' else 16 for k, _ in bvh['nodes_raw']) \
        + 32 * len(bvh['subtrees_raw'])


def write_bvh_block(endian, bvh):
    """Inverse of parse_bvh_block."""
    out = bytearray()
    out += struct.pack(endian + 'ixxxxxxxxxxxx', bvh['unknown_int'])
    out += struct.pack(endian + 'fffxxxx', *bvh['aabb_min'])
    out += struct.pack(endian + 'fffxxxx', *bvh['aabb_max'])
    out += struct.pack(endian + 'fffxxxx', *bvh['quantization'])
    out += struct.pack(endian + 'iibxxxi', bvh['bullet_version'], bvh['current_node_index'],
                       bvh['use_quantization'], bvh['traversal_mode'])
    out += bvh['unknown2']
    out += struct.pack(endian + 'I', bvh['subtree_header_count'])
    out += b'\x00' * 8
    src = bvh.get('_src_endian', endian)
    for kind, blob in bvh['nodes_raw']:
        out += _reswap_node_blob(kind, blob, src, endian)
    for blob in bvh['subtrees_raw']:
        out += _reswap_subtree_blob(blob, src, endian)
    return bytes(out)


# ----------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------

def parse_entry(data, endian, base):
    """One .btd entry starting at `base` -> entry dict (see module docstring)."""
    # header: offsets (relative to base) and counts of every array
    (bvh_off, bvh_size, pos_off, pos_count, idx_off, face_count,
     fattr_off, fattr_count, aidx_off, aidx_count,
     vtx_stride, tri_vtx_stride, index_size, flags) = struct.unpack_from(endian + 'IIIIIIIIIIHHHH', data, base)
    bvh = parse_bvh_block(data, endian, base + bvh_off, bvh_size)
    fdt = np.dtype(endian + 'f4')
    positions = np.frombuffer(data, dtype=fdt, count=pos_count * 3, offset=base + pos_off).reshape(pos_count, 3).astype(np.float32)
    idt = np.dtype(endian + ('u2' if index_size != 4 else 'u4'))
    indices = np.frombuffer(data, dtype=idt, count=face_count * 3, offset=base + idx_off).astype(np.int64)
    # (surface type, colour) pairs, shared by faces via attr_indices
    fa = np.frombuffer(data, dtype=np.dtype(endian + 'u4'), count=fattr_count * 2, offset=base + fattr_off).reshape(fattr_count, 2)
    face_attrs = [(int(a), int(b)) for a, b in fa]
    attr_indices = np.frombuffer(data, dtype=np.dtype(endian + 'u4'), count=aidx_count, offset=base + aidx_off).astype(np.int64)
    return {
        'header': dict(vtx_stride=vtx_stride, tri_vtx_stride=tri_vtx_stride, index_size=index_size, flags=flags),
        'bvh': bvh, 'positions': positions, 'indices': indices,
        'face_attrs': face_attrs, 'attr_indices': attr_indices,
    }


def read_collision(btm_path, btd_path):
    """Read a .btm/.btd pair -> ([entry dicts], endian)."""
    meta, endian = read_btm(btm_path)
    with open(btd_path, 'rb') as f:
        data = f.read()
    entries = []
    for i, m in enumerate(meta):
        if m['Offset'] >= len(data):
            raise ValueError('BTM entry %d points past the end of the BTD (mismatched pair?)' % i)
        e = parse_entry(data, endian, m['Offset'])
        e['align'] = m['Align']
        e['itype'] = m['Type']
        entries.append(e)
    return entries, endian


def geometry_hash(positions, indices):
    """Fingerprint of a collision mesh's geometry. If it is unchanged on
    export, the original BVH is kept instead of being rebuilt."""
    h = hashlib.sha1()
    h.update(np.ascontiguousarray(np.asarray(positions, dtype='<f4')).tobytes())
    h.update(np.ascontiguousarray(np.asarray(indices, dtype='<u2')).tobytes())
    return h.hexdigest()


def _entry_bytes(endian, e):
    """Entry dict -> its .btd bytes (rebuilds the BVH when e['bvh'] is None)."""
    bvh = e['bvh']
    if bvh is None:
        bvh = _rebuild_quantized_bvh([tuple(p) for p in np.asarray(e['positions']).tolist()],
                                     [int(i) for i in np.asarray(e['indices']).tolist()])
    bvh_bytes = write_bvh_block(endian, bvh)
    positions = np.asarray(e['positions'], dtype=np.float32)
    indices = np.asarray(e['indices'], dtype=np.int64)
    if positions.shape[0] > 0xFFFF:
        raise ValueError('collision mesh has %d vertices; the format stores 16-bit indices (max 65535)' % positions.shape[0])
    face_attrs = e['face_attrs']
    attr_indices = np.asarray(e['attr_indices'], dtype=np.int64)

    # lay out the arrays after the 0x30-byte header
    bvh_off = 0x30
    pos_off = bvh_off + len(bvh_bytes)
    pos_count = positions.shape[0]
    idx_off = pos_off + 12 * pos_count
    face_count = indices.shape[0] // 3
    idx_bytes = 2 * indices.shape[0]
    pad = 2 if (idx_off + idx_bytes) % 4 else 0          # keep the next array 4-byte aligned
    fattr_off = idx_off + idx_bytes + pad
    aidx_off = fattr_off + 8 * len(face_attrs)

    # header: offsets/counts, vertex stride 12, triangle stride 6, index size 2, flags 0
    out = bytearray(struct.pack(endian + 'IIIIIIIIIIHHHH', bvh_off, len(bvh_bytes), pos_off, pos_count,
                                idx_off, face_count, fattr_off, len(face_attrs), aidx_off,
                                attr_indices.shape[0], 12, 6, 2, 0))
    out += bvh_bytes
    out += positions.astype(endian + 'f4').tobytes()
    out += indices.astype(endian + 'u2').tobytes()
    out += b'\x00' * pad
    for st, col in face_attrs:
        out += struct.pack(endian + 'II', st & 0xFFFFFFFF, col & 0xFFFFFFFF)
    out += attr_indices.astype(endian + 'u4').tobytes()
    return bytes(out)


def write_collision(btm_path, btd_path, entries, endian='<'):
    """Write entries in order (entry i == SerializeIndex i)."""
    blobs = [_entry_bytes(endian, e) for e in entries]
    meta = []
    out = bytearray()
    for i, (e, b) in enumerate(zip(entries, blobs)):
        meta.append({'Offset': len(out), 'Align': e.get('align', DEFAULT_ALIGNMENT) or DEFAULT_ALIGNMENT,
                     'Type': e.get('itype', 1)})
        out += b
        # pad so the next entry starts on its alignment (not after the last one)
        if i < len(blobs) - 1:
            a = meta[-1]['Align']
            if a and len(out) % a:
                out += b'\x00' * (a - len(out) % a)
    with open(btd_path, 'wb') as f:
        f.write(out)
    write_btm(btm_path, meta, endian)


# ----------------------------------------------------------------------
# REBUILD-mode BVH construction - verbatim port from the
# "Cars 2 BTD_BTM Collision IO" add-on (see its notes on validation).
#
# Used for new or edited collision meshes. It reproduces Bullet's
# btOptimizedBvh::build step by step (quantise each triangle's box to 16
# bits, split the triangles recursively along the axis of largest spread,
# merge small subtrees into compact "bounding leaf" nodes, and add subtree
# headers), so the game's Bullet code reads the result as its own.
# ----------------------------------------------------------------------

_MIN_AABB_DIMENSION = 0.002
_MIN_AABB_HALF_DIMENSION = 0.001
_TRI_INDEX_CNT = 2                 # btQuantizedBoundingLeafNode::TRI_INDEX_CNT
_TRIINDEXNODE_CNT = 8               # btTriIndexNode::TRI_INDEX_CNT
_INVALID_TRIANGLE_INDEX_14BIT = 0x3FFF
_MAX_SUBTREE_SIZE_IN_BYTES = 2048
_BOUNDING_LEAF_NODE_MASK = 1 << 30
_HAS_TRI_INDEX_NODE_MASK = 1 << 31
_ESCAPE_INDEX_MASK = 1 << 31


class _Quantizer:
    """Mirrors btQuantizedBvh::setQuantizationValues()/quantize()/unQuantize()."""

    def __init__(self, bvh_min, bvh_max, margin=1.0):
        self.bvh_min = tuple(bvh_min[i] - margin for i in range(3))
        self.bvh_max = tuple(bvh_max[i] + margin for i in range(3))
        size = tuple(self.bvh_max[i] - self.bvh_min[i] for i in range(3))
        self.quant = tuple(65533.0 / size[i] for i in range(3))

    def quantize(self, point, is_max):
        # boxes are rounded outwards: min down to even, max up to odd
        v = [(point[i] - self.bvh_min[i]) * self.quant[i] for i in range(3)]
        out = []
        for i in range(3):
            if is_max:
                q = (int(v[i] + 1.0) & 0xFFFF) | 1
            else:
                q = int(v[i]) & 0xFFFE
            out.append(q & 0xFFFF)
        return tuple(out)

    def unquantize(self, q):
        return tuple(q[i] / self.quant[i] + self.bvh_min[i] for i in range(3))


def _calc_quant8(aabb_min, aabb_max, margin=0.01):
    """Mirrors btCalculateQuantizationValues8bit()."""
    amin = [aabb_min[i] - margin for i in range(3)]
    amax = [aabb_max[i] + margin for i in range(3)]
    size = [amax[i] - amin[i] for i in range(3)]
    quant = [253.0 / size[i] for i in range(3)]
    return amin, quant


def _quantize8(point, is_max, amin, quant):
    """Mirrors btQuantize8bit()."""
    v = [(point[i] - amin[i]) * quant[i] for i in range(3)]
    out = []
    for i in range(3):
        if is_max:
            q = (int(v[i] + 1.0) & 0xFF) | 1
        else:
            q = int(v[i]) & 0xFE
        out.append(q & 0xFF)
    return tuple(out)


def _build_leaf_nodes(positions, indices, quantizer):
    """Mirrors btOptimizedBvh::build()'s QuantizedNodeTriangleCallback."""
    leafs = []
    tri_count = len(indices) // 3
    for t in range(tri_count):
        a, b, c = (positions[indices[3 * t + k]] for k in range(3))
        amin = [min(a[i], b[i], c[i]) for i in range(3)]
        amax = [max(a[i], b[i], c[i]) for i in range(3)]
        # give flat triangles a tiny thickness
        for i in range(3):
            if amax[i] - amin[i] < _MIN_AABB_DIMENSION:
                amax[i] += _MIN_AABB_HALF_DIMENSION
                amin[i] -= _MIN_AABB_HALF_DIMENSION
        qmin = quantizer.quantize(amin, False)
        qmax = quantizer.quantize(amax, True)
        wmin = quantizer.unquantize(qmin)
        wmax = quantizer.unquantize(qmax)
        leafs.append(dict(qmin=qmin, qmax=qmax, wmin=wmin, wmax=wmax, tri=t, part=0))
    return leafs


def _calc_splitting_axis(leafs, start, end):
    """Axis (0/1/2) along which the leaf centres spread the most."""
    n = end - start
    means = [0.0, 0.0, 0.0]
    for i in range(start, end):
        for k in range(3):
            means[k] += 0.5 * (leafs[i]["wmax"][k] + leafs[i]["wmin"][k])
    means = [m / n for m in means]
    variance = [0.0, 0.0, 0.0]
    for i in range(start, end):
        for k in range(3):
            c = 0.5 * (leafs[i]["wmax"][k] + leafs[i]["wmin"][k])
            d = c - means[k]
            variance[k] += d * d
    if n > 1:
        variance = [v / (n - 1) for v in variance]
    return variance.index(max(variance))


def _sort_and_calc_splitting_index(leafs, start, end, axis):
    """Partition the leaves around the mean on `axis`; returns the split point
    (falls back to the middle when the split would be too unbalanced)."""
    n = end - start
    mean = 0.0
    for i in range(start, end):
        mean += 0.5 * (leafs[i]["wmax"][axis] + leafs[i]["wmin"][axis])
    mean /= n
    split = start
    for i in range(start, end):
        c = 0.5 * (leafs[i]["wmax"][axis] + leafs[i]["wmin"][axis])
        if c > mean:
            leafs[i], leafs[split] = leafs[split], leafs[i]
            split += 1
    range_balanced = n // 3
    unbalanced = (split <= start + range_balanced) or (split >= end - 1 - range_balanced)
    if unbalanced:
        split = start + (n >> 1)
    if split == end or split == end - 1:
        split = end - 2
        if split <= start:
            split = start + 1
    return split


def _build_tree(leafs, start, end, nodes, quantizer):
    """Mirrors btQuantizedBvh::buildTree()."""
    n = end - start
    if n == 1:
        leaf = leafs[start]
        nodes.append(dict(kind="leaf", qmin=leaf["qmin"], qmax=leaf["qmax"],
                           tri=leaf["tri"], part=leaf["part"]))
        return

    axis = _calc_splitting_axis(leafs, start, end)
    split = _sort_and_calc_splitting_index(leafs, start, end, axis)

    # internal node: box grown to contain every leaf below it
    internal_index = len(nodes)
    qmin0 = quantizer.quantize(quantizer.bvh_max, False)
    qmax0 = quantizer.quantize(quantizer.bvh_min, True)
    nodes.append(dict(kind="internal", qmin=list(qmin0), qmax=list(qmax0), escape=None))

    for i in range(start, end):
        qnewmin = quantizer.quantize(leafs[i]["wmin"], False)
        qnewmax = quantizer.quantize(leafs[i]["wmax"], True)
        node = nodes[internal_index]
        for k in range(3):
            if node["qmin"][k] > qnewmin[k]:
                node["qmin"][k] = qnewmin[k]
            if node["qmax"][k] < qnewmax[k]:
                node["qmax"][k] = qnewmax[k]

    _build_tree(leafs, start, split, nodes, quantizer)
    _build_tree(leafs, split, end, nodes, quantizer)
    # "escape index": how many nodes to skip to jump past this subtree
    nodes[internal_index]["escape"] = len(nodes) - internal_index


def _node_tree_size(nodes, idx):
    """Number of node slots the subtree at idx takes."""
    n = nodes[idx]
    if n["kind"] in ("boundingLeaf", "boundingLeaf2"):
        return 2 if n["kind"] == "boundingLeaf2" else 1
    return 1 if n["kind"] == "leaf" else n["escape"]


def _remove_node_range(nodes, start, end):
    """Mirrors btQuantizedBvh::removeNodeRange()."""
    if start > end or end >= len(nodes):
        return
    deleted = end + 1 - start
    cur = 0
    # fix the escape indices of the nodes before the removed range
    while cur < start:
        n = nodes[cur]
        if n["kind"] not in ("boundingLeaf", "boundingLeaf2"):
            if n["kind"] == "internal":
                esc = n["escape"]
                if cur + esc > end:
                    n["escape"] = esc - deleted
                elif cur + esc >= start:
                    n["escape"] = start - cur
        else:
            if n["kind"] == "boundingLeaf2":
                cur += 1
        cur += 1
    del nodes[start:end + 1]


def _swap_child_trees(nodes, parent_idx):
    """Mirrors btQuantizedBvh::swapChildTrees()."""
    left_idx = parent_idx + 1
    left_size = _node_tree_size(nodes, left_idx)
    right_idx = left_idx + left_size
    right_size = _node_tree_size(nodes, right_idx)
    left_block = nodes[left_idx:left_idx + left_size]
    right_block = nodes[right_idx:right_idx + right_size]
    nodes[left_idx:right_idx + right_size] = right_block + left_block


def _merge_leaf_nodes_subtree(nodes, root_idx, parent_idx, allow_multi, quantizer):
    """Mirrors btQuantizedBvh::mergeLeafNodes(rootNodeIndex, parentNodeIndex, ...).

    Replaces a small subtree (a few triangles) with one compact bounding-leaf
    node holding two 8-bit boxes and the triangle indices. Returns how many
    nodes were removed (0 = not merged)."""
    root = nodes[root_idx]
    if root["kind"] in ("boundingLeaf", "boundingLeaf2", "leaf"):
        return 0
    subtree_size = root["escape"]
    limit = (2 * (_TRI_INDEX_CNT + _TRIINDEXNODE_CNT) - 1) if allow_multi else (2 * _TRI_INDEX_CNT - 1)
    if subtree_size > limit:
        return 0

    # collect the subtree's leaves
    tri_count = 0
    leaf_indices = []
    first14 = -1            # first leaf whose triangle index fits in 14 bits
    cur = root_idx + 1
    end = root_idx + subtree_size
    while cur < end:
        n = nodes[cur]
        if n["kind"] in ("boundingLeaf", "boundingLeaf2"):
            return 0
        if n["kind"] == "leaf":
            if n["tri"] >= 0xFFFF or n["part"] != 0:
                return 0
            if n["tri"] < _INVALID_TRIANGLE_INDEX_14BIT and first14 < 0:
                first14 = cur
            leaf_indices.append(cur)
            tri_count += 1
        cur += 1

    if allow_multi:
        mod = -1 if first14 < 0 else 0
        if tri_count > _TRI_INDEX_CNT + _TRIINDEXNODE_CNT + mod:
            return 0
    else:
        if tri_count > _TRI_INDEX_CNT or first14 < 0:
            return 0
    if tri_count < 2:
        return 0

    # split the leaves into a left and a right half, each gets one box
    left_start = leaf_indices[0]
    right_start = leaf_indices[tri_count // 2]
    left_end = right_start
    right_end = end
    if first14 >= right_start:
        left_end, right_end = right_end, left_end
        left_start, right_start = right_start, left_start

    left_extra, right_extra = [], []
    left_min = left_max = right_min = right_max = None
    cur = left_start
    left_tri_count = 0
    while cur < left_end:
        n = nodes[cur]
        if n["kind"] == "leaf":
            wmin, wmax = quantizer.unquantize(n["qmin"]), quantizer.unquantize(n["qmax"])
            if left_tri_count == 0:
                left_min, left_max = list(wmin), list(wmax)
            else:
                left_min = [min(left_min[i], wmin[i]) for i in range(3)]
                left_max = [max(left_max[i], wmax[i]) for i in range(3)]
            if cur != first14:
                left_extra.append(n["tri"])
            left_tri_count += 1
        cur += 1
    cur = right_start
    right_tri_count = 0
    while cur < right_end:
        n = nodes[cur]
        if n["kind"] == "leaf":
            wmin, wmax = quantizer.unquantize(n["qmin"]), quantizer.unquantize(n["qmax"])
            if right_tri_count == 0:
                right_min, right_max = list(wmin), list(wmax)
            else:
                right_min = [min(right_min[i], wmin[i]) for i in range(3)]
                right_max = [max(right_max[i], wmax[i]) for i in range(3)]
            if right_tri_count > 0:
                right_extra.append(n["tri"])
            right_tri_count += 1
        cur += 1

    if first14 != -1:
        tri0 = nodes[first14]["tri"]
        has_tri_index = tri_count > _TRI_INDEX_CNT
    else:
        tri0 = _INVALID_TRIANGLE_INDEX_14BIT
        has_tri_index = tri_count > _TRI_INDEX_CNT - 1
    tri1 = nodes[right_start]["tri"]

    # the 8-bit boxes are relative to the parent's box
    if parent_idx != root_idx:
        p_node = nodes[parent_idx]
        p_min = quantizer.unquantize(p_node["qmin"])
        p_max = quantizer.unquantize(p_node["qmax"])
    else:
        p_min, p_max = quantizer.bvh_min, quantizer.bvh_max

    amin8, quant8 = _calc_quant8(p_min, p_max)
    min1 = _quantize8(left_min, False, amin8, quant8)
    max1 = _quantize8(left_max, True, amin8, quant8)
    min2 = _quantize8(right_min, False, amin8, quant8)
    max2 = _quantize8(right_max, True, amin8, quant8)

    node0 = dict(kind="boundingLeaf2" if has_tri_index else "boundingLeaf",
                 min1=min1, max1=max1, min2=min2, max2=max2, tri0=tri0, tri1=tri1)

    nodes[root_idx] = node0
    if has_tri_index:
        # more than 2 triangles: an extra node lists the rest (4 per side)
        shorts = (left_extra + [-1] * 4)[:4] + (right_extra + [-1] * 4)[:4]
        nodes[root_idx + 1] = dict(kind="triIndexNode", shorts=shorts)
        removed = subtree_size - 2
        _remove_node_range(nodes, root_idx + 2, root_idx + subtree_size - 1)
    else:
        removed = subtree_size - 1
        _remove_node_range(nodes, root_idx + 1, root_idx + subtree_size - 1)
    return removed


def _merge_leaf_nodes_pass(nodes, allow_multi, quantizer):
    """Mirrors btQuantizedBvh::mergeLeafNodes(allowMultipleTrianglesPerAabb)."""
    cur_index = 0
    end_index = len(nodes)
    parent_stack = [0]
    while cur_index < end_index:
        # pop parents whose subtree we have left
        while len(parent_stack) > 1 and cur_index >= parent_stack[-1] + nodes[parent_stack[-1]]["escape"]:
            parent_stack.pop()
        cur_parent = parent_stack[-1]
        removed = _merge_leaf_nodes_subtree(nodes, cur_index, cur_parent, allow_multi, quantizer)
        if removed > 0:
            end_index -= removed
            # Bullet keeps a merged leaf as the right child when the left one is internal
            if cur_index != cur_parent:
                parent_node = nodes[cur_parent]
                if parent_node["kind"] == "internal":
                    left_child = nodes[cur_parent + 1]
                    if left_child["kind"] == "internal":
                        cur_node = nodes[cur_index]
                        if cur_node["kind"] in ("boundingLeaf", "boundingLeaf2"):
                            _swap_child_trees(nodes, cur_parent)
        cur_node = nodes[cur_index]
        if cur_node["kind"] == "internal":
            parent_stack.append(cur_index)
        increment = 2 if cur_node["kind"] == "boundingLeaf2" else 1
        cur_index += increment
    del nodes[end_index:]


def _update_subtree_headers(nodes, headers, left_idx, right_idx, parent_qmin, parent_qmax):
    """Add a subtree header for each child small enough to fit in 2 KB."""
    for idx in (left_idx, right_idx):
        n = nodes[idx]
        if n["kind"] not in ("boundingLeaf", "boundingLeaf2"):
            size = 1 if n["kind"] == "leaf" else n["escape"]
        else:
            size = 2 if n["kind"] == "boundingLeaf2" else 1
        if size * 16 <= _MAX_SUBTREE_SIZE_IN_BYTES:
            if n["kind"] not in ("boundingLeaf", "boundingLeaf2"):
                qmin, qmax = n["qmin"], n["qmax"]
            else:
                qmin, qmax = parent_qmin, parent_qmax
            headers.append(dict(root=idx, size=size, qmin=qmin, qmax=qmax))


def _build_subtree_headers(nodes):
    """Mirrors btQuantizedBvh::buildSubtreeHeaders()."""
    headers = []
    cur = 0
    end = len(nodes)
    while cur < end:
        n = nodes[cur]
        if n["kind"] == "internal":
            escape = n["escape"]
            if escape * 16 > _MAX_SUBTREE_SIZE_IN_BYTES:
                left_idx = cur + 1
                left = nodes[left_idx]
                if left["kind"] not in ("boundingLeaf", "boundingLeaf2"):
                    right_idx = left_idx + 1 if left["kind"] == "leaf" else left_idx + left["escape"]
                else:
                    right_idx = left_idx + (2 if left["kind"] == "boundingLeaf2" else 1)
                _update_subtree_headers(nodes, headers, left_idx, right_idx, n["qmin"], n["qmax"])
        cur += 2 if n["kind"] == "boundingLeaf2" else 1
    return headers


def _rebuild_quantized_bvh(positions, indices, allow_multi=True):
    """Full rebuild pipeline, mirroring btOptimizedBvh::build(). Returns the
    same field layout as parse_bvh_block()/_deserialize_bvh_raw() so it can
    be dropped straight into export_btm_btd()'s blobs dict."""
    if positions:
        mesh_min = [min(p[i] for p in positions) for i in range(3)]
        mesh_max = [max(p[i] for p in positions) for i in range(3)]
    else:
        mesh_min = mesh_max = [0.0, 0.0, 0.0]

    quantizer = _Quantizer(mesh_min, mesh_max, margin=1.0)
    leafs = _build_leaf_nodes(positions, indices, quantizer)

    nodes = []
    if leafs:
        _build_tree(leafs, 0, len(leafs), nodes, quantizer)
        if allow_multi:
            _merge_leaf_nodes_pass(nodes, allow_multi, quantizer)

    headers = _build_subtree_headers(nodes)
    if not headers and nodes:
        # small trees: one header covering the whole tree
        qmin_fb = quantizer.quantize(quantizer.bvh_min, False)
        qmax_fb = quantizer.quantize(quantizer.bvh_max, True)
        n0 = nodes[0]
        if n0["kind"] not in ("boundingLeaf", "boundingLeaf2"):
            size = 1 if n0["kind"] == "leaf" else n0["escape"]
        else:
            size = 2 if n0["kind"] == "boundingLeaf2" else 1
        headers.append(dict(root=0, size=size, qmin=qmin_fb, qmax=qmax_fb))

    nodes_raw = _pack_rebuilt_nodes(nodes, endian_for_pack := "<")
    subtrees_raw = [_pack_subtree_header(h) for h in headers]

    return dict(
        unknown_int=0,
        aabb_min=quantizer.bvh_min, aabb_max=quantizer.bvh_max, quantization=quantizer.quant,
        bullet_version=276, current_node_index=len(nodes), use_quantization=1, traversal_mode=0,
        unknown2=b"\x00" * 0x64, subtree_header_count=len(headers),
        nodes_raw=nodes_raw, subtrees_raw=subtrees_raw,
        _src_endian=endian_for_pack,
    )


def _pack_rebuilt_nodes(nodes, endian):
    """Packs the logical node list into the file's physical 16/32-byte
    records, using the SAME bit layout confirmed against real oilrig.btd
    node headers (BOUNDING_LEAF_NODE_MASK=bit30, HAS_TRI_INDEX_NODE_MASK/
    ESCAPE_INDEX_MASK=bit31 of the trailing uint32)."""
    out = []
    i = 0
    while i < len(nodes):
        n = nodes[i]
        if n["kind"] == "internal":
            qmin = [_u16_to_i16(v) for v in n["qmin"]]
            qmax = [_u16_to_i16(v) for v in n["qmax"]]
            trailing = (n["escape"] | _ESCAPE_INDEX_MASK) & 0xFFFFFFFF
            blob = struct.pack(endian + "hhhhhhI", *qmin, *qmax, trailing)
            out.append(("internal", blob))
            i += 1
        elif n["kind"] == "leaf":
            # Genuinely unmerged single-triangle leaf: same 16-byte layout
            # as an internal node (6x int16 AABB + trailing uint32), but
            # the trailing field is a bare triangleIndex with NO reserved
            # bits set (setTriangleIndexAndPartId with partId=0).
            qmin = [_u16_to_i16(v) for v in n["qmin"]]
            qmax = [_u16_to_i16(v) for v in n["qmax"]]
            blob = struct.pack(endian + "hhhhhhI", *qmin, *qmax, n["tri"])
            out.append(("internal", blob))
            i += 1
        elif n["kind"] in ("boundingLeaf", "boundingLeaf2"):
            min1 = [_u8_to_i8(v) for v in n["min1"]]
            max1 = [_u8_to_i8(v) for v in n["max1"]]
            min2 = [_u8_to_i8(v) for v in n["min2"]]
            max2 = [_u8_to_i8(v) for v in n["max2"]]
            trailing = ((n["tri0"] << 16) | n["tri1"] | _BOUNDING_LEAF_NODE_MASK) & 0xFFFFFFFF
            if n["kind"] == "boundingLeaf2":
                trailing |= _HAS_TRI_INDEX_NODE_MASK
            blob1 = struct.pack(endian + "bbbbbbbbbbbbI", *min1, *max1, *min2, *max2, trailing)
            if n["kind"] == "boundingLeaf":
                out.append(("leaf", blob1))
                i += 1
            else:
                tin = nodes[i + 1]
                shorts = [v if v == -1 else (v & 0xFFFF) for v in tin["shorts"]]
                blob2 = struct.pack(endian + "8h", *shorts)
                out.append(("leaf2", blob1 + blob2))
                i += 2
        else:
            raise ValueError(f"unexpected rebuilt node kind {n['kind']!r}")
    return out


def _pack_subtree_header(h):
    """32-byte btBvhSubtreeInfo record, always big-endian regardless of the
    target file's endianness (see _reswap_subtree_blob's confirmed note)."""
    return struct.pack(">HHHHHHii", *h["qmin"], *h["qmax"], h["root"], h["size"]) + b"\x00" * 12
