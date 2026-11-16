"""DDS cube maps -> equirectangular panoramas for the viewport shaders.

A shader looks up a cube map with an arbitrary direction d (whatever it
computed). The panorama is built so that panorama(equirect_uv(d)) gives what
Direct3D's cube sampling returns for d, so no axis conventions need to agree
with Blender's:

    phi = atan2(d.x, -d.z), theta = atan2(d.y, |d.xz|)
    u = phi / 2pi + 0.5,  v (from the top) = 0.5 - theta / pi

RGBA is kept raw (several cube maps are RGBM encoded), so the panorama is
saved as an 8-bit PNG and loaded as non-colour data.

Why: Blender's shader nodes cannot sample a cube map. Game Look (fxnodes)
turns each cube lookup into a lookup in this panorama instead (node group
C2A_CubeUV computes the u/v above from the direction).
"""
import struct
import zlib

import numpy as np

import arcade


def _level_size(w, h, fmt):
    """Bytes of one mip level: DXT formats store 4x4 blocks (8 or 16 bytes
    each), everything else here is 32-bit."""
    if fmt in ('DXT1', 'DXT3', 'DXT5'):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * (8 if fmt == 'DXT1' else 16)
    return w * h * 4


def cube_faces(data):
    """DDS cube -> list of 6 (size, size, 4) float32 arrays (+X -X +Y -Y +Z -Z, top mip, rows top-down)."""
    if data[:4] != b'DDS ':
        raise ValueError('not a DDS')
    h, w = struct.unpack_from('<II', data, 12)
    mips = max(1, struct.unpack_from('<I', data, 28)[0])
    pf_flags, fourcc = struct.unpack_from('<I4s', data, 80)
    fmt = fourcc.decode('latin1') if pf_flags & 0x4 else 'RGBA32'
    # a cube DDS stores face 0 with all its mips, then face 1 with all its
    # mips, ... - so one face's total size is needed to find the next face
    face_bytes = 0
    mw, mh = w, h
    for _ in range(mips):
        face_bytes += _level_size(mw, mh, fmt)
        mw, mh = max(1, mw // 2), max(1, mh // 2)
    faces = []
    for f in range(6):
        off = 128 + f * face_bytes          # 128 = DDS header size
        # build a tiny single-face, single-mip 2D DDS and let the shared
        # decoder (which knows DXT) turn it into pixels
        top = data[:128] + data[off:off + _level_size(w, h, fmt)]
        hdr = bytearray(top[:128])
        struct.pack_into('<I', hdr, 28, 1)          # one mip
        struct.pack_into('<I', hdr, 112, 0)         # plain 2D
        faces.append(arcade.dds_rgba(bytes(hdr) + top[128:]))
    return faces


def _sample_cube(faces, d):
    """D3D cube sampling (nearest) of directions d (n,3) -> (n,4)."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    size = faces[0].shape[0]
    out = np.zeros((len(d), 4), np.float32)
    # the face is chosen by the largest component ("major axis")
    major = np.where((ax >= ay) & (ax >= az), 0, np.where(ay >= az, 1, 2))
    # Direct3D's table: for each face, which components become the 2D
    # coordinates (sc, tc) and which one is the divisor (ma)
    rules = [  # face index, sc, tc, ma
        (0, (major == 0) & (x >= 0), -z, -y, ax), (1, (major == 0) & (x < 0), z, -y, ax),
        (2, (major == 1) & (y >= 0), x, z, ay), (3, (major == 1) & (y < 0), x, -z, ay),
        (4, (major == 2) & (z >= 0), x, -y, az), (5, (major == 2) & (z < 0), -x, -y, az)]
    for f, sel, sc, tc, ma in rules:
        if not sel.any():
            continue
        m = np.maximum(ma[sel], 1e-12)
        s = (sc[sel] / m + 1.0) * 0.5           # -1..1 -> 0..1
        t = (tc[sel] / m + 1.0) * 0.5
        i = np.clip((t * size).astype(np.int64), 0, size - 1)   # row
        j = np.clip((s * size).astype(np.int64), 0, size - 1)   # column
        out[sel] = faces[f][i, j]
    return out


def equirect(faces, width=None):
    """Six cube faces -> (h, w, 4) panorama (w = 2h) using the mapping in the
    module docstring. Default width: 4x the face size, between 64 and 2048."""
    size = faces[0].shape[0]
    w = width or max(64, min(2048, size * 4))
    h = w // 2
    # pixel centres -> angles -> unit directions
    u = (np.arange(w) + 0.5) / w
    v = (np.arange(h) + 0.5) / h
    phi = (u - 0.5) * 2 * np.pi
    th = (0.5 - v) * np.pi
    ct = np.cos(th)[:, None]
    d = np.stack([np.sin(phi)[None, :] * ct, np.repeat(np.sin(th)[:, None], w, 1),
                  -np.cos(phi)[None, :] * ct], -1).reshape(-1, 3)
    return _sample_cube(faces, d).reshape(h, w, 4)


def write_png(path, rgba):
    """(h, w, 4) floats 0..1, rows top-down -> 8-bit RGBA PNG.

    A minimal PNG writer (no Blender / PIL needed): each row is prefixed with
    filter byte 0, the whole image is zlib-compressed into one IDAT chunk."""
    a = np.clip(np.rint(np.asarray(rgba) * 255.0), 0, 255).astype(np.uint8)
    h, w = a.shape[:2]
    raw = b''.join(b'\0' + a[r].tobytes() for r in range(h))

    def chunk(tag, body):
        # length, tag, data, CRC of tag+data
        c = struct.pack('>I', len(body)) + tag + body
        return c + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF)
    # IHDR: width, height, bit depth 8, colour type 6 (RGBA), no interlace
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)
