"""Vertex / index stream codec for Cars 2 scene buffers.

Element type ids, as found in the shipped worlds' VertexStream elements and
the shaders that read them (names are this add-on's own):

    0  F32X1   1  F32X2   2  F32X3   3  F32X4      32-bit floats
    24 U8X4    raw bytes (bone indices)
    25 UNORM8X4  bytes as 0..1 (colours, RGBA / XYZW byte order)
    31 SNORM10X3      3 x signed 10 bits in 32 bits
    39 SNORM11_11_10  signed 11 + 11 + 10 bits in 32 bits
    44 F16X2   45 F16X4   half floats
    46 FIXED6X3  3 signed bytes, value / 64

Decoding is vectorised (one NumPy op per element), following the approach of
cars2-blender-io's import-model-maps.py (MIT License, Copyright (c) 2025 Team
Offsetting; full text in THIRD_PARTY_NOTICES.md).

How vertex data is stored in a world:
    <world>_0.vbuf   one big byte blob with every vertex of every mesh
    VertexStream     (in the .oct) one slice of it: Length vertices of Width
                     bytes, starting at VertexBufferOffset, with a list of
                     Elements (Name, Type, Offset inside the vertex)
A mesh part usually has two streams, e.g. [Position, Uv1] and
[Tangent, Normal, Binormal, lightmapUV]. Indices live the same way in
<world>_0.ibuf (IndexStream: Length indices at IndexBufferOffset).
"""
import numpy as np

# element type ids (see the table above)
F32X1, F32X2, F32X3, F32X4 = 0, 1, 2, 3
U8X4, UNORM8X4 = 24, 25
SNORM10X3 = 31
SNORM11_11_10 = 39
F16X2, F16X4 = 44, 45
FIXED6X3 = 46

# bytes per element
TYPE_SIZE = {
    F32X1: 4, F32X2: 8, F32X3: 12, F32X4: 16,
    U8X4: 4, UNORM8X4: 4,
    SNORM10X3: 4, SNORM11_11_10: 4,
    F16X2: 4, F16X4: 8, FIXED6X3: 3,
}
# components a decoded element yields
TYPE_COMPONENTS = {
    F32X1: 1, F32X2: 2, F32X3: 3, F32X4: 4,
    U8X4: 4, UNORM8X4: 4,
    SNORM10X3: 3, SNORM11_11_10: 3,
    F16X2: 2, F16X4: 4, FIXED6X3: 3,
}

# index width in bytes -> NumPy little-endian type
INDEX_DTYPES = {1: '<u1', 2: '<u2', 4: '<u4'}


def _sign_extend(vals, bits):
    """Interpret the low `bits` bits of each value as a signed number."""
    sign = 1 << (bits - 1)
    vals = vals.astype(np.int64)
    return (vals & (sign - 1)) - (vals & sign)


def decode_element(buf, off, ty):
    """buf: (n, width) uint8 array. Returns float32 (n, k) or uint8 (n, 4) for U8X4."""
    n = buf.shape[0]
    # plain floats: reinterpret the bytes
    if ty == F32X1:
        return buf[:, off:off + 4].copy().view('<f4').reshape(n, 1)
    if ty == F32X2:
        return buf[:, off:off + 8].copy().view('<f4').reshape(n, 2)
    if ty == F32X3:
        return buf[:, off:off + 12].copy().view('<f4').reshape(n, 3)
    if ty == F32X4:
        return buf[:, off:off + 16].copy().view('<f4').reshape(n, 4)
    # 4 raw bytes (e.g. bone indices)
    if ty == U8X4:
        return buf[:, off:off + 4].copy()
    # 4 bytes as 0..1 (vertex colours)
    if ty == UNORM8X4:
        return buf[:, off:off + 4].astype(np.float32) / 255.0
    # 3 signed 10-bit values packed in 32 bits (normals / tangents)
    if ty == SNORM10X3:
        v = buf[:, off:off + 4].copy().view('<u4').reshape(n).astype(np.int64)
        return np.stack([_sign_extend((v >> 0) & 0x3FF, 10) / 511.0,
                         _sign_extend((v >> 10) & 0x3FF, 10) / 511.0,
                         _sign_extend((v >> 20) & 0x3FF, 10) / 511.0], axis=1).astype(np.float32)
    # 11 + 11 + 10 signed bits packed in 32 bits
    if ty == SNORM11_11_10:
        v = buf[:, off:off + 4].copy().view('<u4').reshape(n).astype(np.int64)
        return np.stack([_sign_extend((v >> 0) & 0x7FF, 11) / 1023.0,
                         _sign_extend((v >> 11) & 0x7FF, 11) / 1023.0,
                         _sign_extend((v >> 22) & 0x3FF, 10) / 511.0], axis=1).astype(np.float32)
    # half floats (UVs, compressed normals)
    if ty == F16X2:
        return buf[:, off:off + 4].copy().view('<f2').reshape(n, 2).astype(np.float32)
    if ty == F16X4:
        return buf[:, off:off + 8].copy().view('<f2').reshape(n, 4).astype(np.float32)
    # three signed bytes in 1.6 fixed point (value / 64)
    if ty == FIXED6X3:
        return buf[:, off:off + 3].copy().view(np.int8).reshape(n, 3).astype(np.float32) / 64.0
    raise ValueError('unsupported vertex element type %d' % ty)


def encode_element(values, ty):
    """values: (n, k) float array (k may exceed the type's components; extra
    columns are dropped, missing ones padded). Returns (n, size) uint8."""
    values = np.asarray(values)
    n = values.shape[0]
    comps = TYPE_COMPONENTS[ty]
    if values.ndim == 1:
        values = values.reshape(n, 1)
    if values.shape[1] < comps:
        pad = np.zeros((n, comps - values.shape[1]), dtype=values.dtype)
        if ty in (UNORM8X4,) and comps == 4:
            pad[:, -1] = 1.0  # opaque alpha
        values = np.hstack([values, pad])
    values = values[:, :comps]
    # each branch is the exact inverse of the matching decode_element branch
    if ty in (F32X1, F32X2, F32X3, F32X4):
        return np.ascontiguousarray(values.astype('<f4')).view(np.uint8).reshape(n, 4 * comps)
    if ty == U8X4:
        return np.clip(np.rint(values), 0, 255).astype(np.uint8).reshape(n, 4)
    if ty == UNORM8X4:
        return np.clip(np.rint(values * 255.0), 0, 255).astype(np.uint8).reshape(n, 4)
    if ty == SNORM10X3:
        q = np.clip(np.rint(values * 511.0), -511, 511).astype(np.int64) & 0x3FF
        u = (q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20)).astype('<u4')
        return u.view(np.uint8).reshape(n, 4)
    if ty == SNORM11_11_10:
        a = np.clip(np.rint(values[:, 0] * 1023.0), -1023, 1023).astype(np.int64) & 0x7FF
        b = np.clip(np.rint(values[:, 1] * 1023.0), -1023, 1023).astype(np.int64) & 0x7FF
        c = np.clip(np.rint(values[:, 2] * 511.0), -511, 511).astype(np.int64) & 0x3FF
        u = (a | (b << 11) | (c << 22)).astype('<u4')
        return u.view(np.uint8).reshape(n, 4)
    if ty == F16X2:
        return np.ascontiguousarray(values.astype('<f2')).view(np.uint8).reshape(n, 4)
    if ty == F16X4:
        return np.ascontiguousarray(values.astype('<f2')).view(np.uint8).reshape(n, 8)
    if ty == FIXED6X3:
        return np.clip(np.rint(values * 64.0), -127, 127).astype(np.int8).view(np.uint8).reshape(n, 3)
    raise ValueError('unsupported vertex element type %d' % ty)


def stream_elements(stream):
    """[(name, type, offset)] of a VertexStream dict, in file order."""
    return [(e['Name'], e['Type'], e['Offset']) for e in stream.get('Elements', {}).values()]


def read_stream(stream, vbuf_bytes):
    """Decode one VertexStream from the raw vbuf bytes -> {name: array}."""
    length, width = stream['Length'], stream['Width']
    if length == 0 or width == 0:
        return {}
    off = stream['VertexBufferOffset']
    # view the stream's bytes as a (vertices, bytes per vertex) table
    raw = np.frombuffer(vbuf_bytes, dtype=np.uint8, count=length * width, offset=off)
    buf = raw.reshape(length, width)
    return {name: decode_element(buf, eoff, ty) for name, ty, eoff in stream_elements(stream)}


def write_stream(layout, width, attrs, n):
    """Pack a stream. layout: [(name, type, offset)], attrs: {name: (n,k)}.
    Returns bytes.

    Elements the mesh doesn't have (attrs lacks the name) get
    default_values() - e.g. white occlusion - so the shader always finds
    every input its effect declares."""
    out = np.zeros((n, width), dtype=np.uint8)
    for name, ty, off in layout:
        vals = attrs.get(name)
        if vals is None:
            vals = default_values(name, n)
        enc = encode_element(vals, ty)
        out[:, off:off + enc.shape[1]] = enc
    return out.tobytes()


# neutral pre-lighting (PC build: 0.5 = unlit texture colour)
DEFAULT_PRELIGHT = (128 / 255.0, 128 / 255.0, 128 / 255.0, 1.0)
# per-name defaults for vertex colour streams a mesh doesn't have
DEFAULT_COLORS = {
    'ColorOcclusion': (1.0, 1.0, 1.0, 1.0),     # no occlusion: lighting shows at full strength
    'ColorConstraint': (1.0, 1.0, 1.0, 1.0),
    'ColorBlend': (0.0, 0.0, 0.0, 1.0),         # texture blend weights: first layer only
    'ColorAdditive': (0.0, 0.0, 0.0, 1.0),      # nothing added
    'VertexBaked': (1.0, 1.0, 1.0, 43 / 255.0),  # Arcade RGBM: light 1.0
}


def default_values(name, n):
    """n copies of the neutral value for an element the mesh doesn't provide."""
    if name in DEFAULT_COLORS:
        return np.tile(np.array(DEFAULT_COLORS[name], np.float32), (n, 1))
    if name.startswith('Color'):
        return np.tile(np.array(DEFAULT_PRELIGHT, np.float32), (n, 1))
    if name == 'Normal':
        return np.tile(np.array([0, 1, 0, 0], np.float32), (n, 1))     # straight up (game space is Y-up)
    return np.zeros((n, 4), np.float32)


def read_indices(istream, ibuf_bytes, width=2):
    """Indices of one IndexStream as int64 (width = bytes per index)."""
    dt = INDEX_DTYPES.get(width, '<u2')
    return np.frombuffer(ibuf_bytes, dtype=dt, count=istream['Length'],
                         offset=istream['IndexBufferOffset']).astype(np.int64)
