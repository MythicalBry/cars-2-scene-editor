"""Minimal DDS support: header inspection and uncompressed A8R8G8B8 writing
(2D and cube maps, with box-filtered mip chains).

Texture `Data` blobs in a Cars 2 .oct TexturePool are complete DDS files, so a
texture is replaced by swapping that blob.

DDS header cheat-sheet (byte offsets into the file):
    0    'DDS '            magic
    12   height, 16 width
    28   mip count
    80   pixel-format flags, 84 FourCC ('DXT1', 'DXT5', or 0 for raw RGB)
    88   bits per pixel, 92..107 R/G/B/A bit masks
    108  caps, 112 caps2 (cube map flags)
    116  caps3 - the Arcade engine's sRGB flag (see SRGB_OFFSET)
    128  pixel data starts
New textures are always written uncompressed 32-bit (no DXT encoder here):
bigger files, but no quality loss and no extra dependency.
"""
import struct
import numpy as np

# header flag bits (from the DirectX documentation)
DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PITCH = 0x1, 0x2, 0x4, 0x8
DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT = 0x1000, 0x20000
DDPF_ALPHAPIXELS, DDPF_FOURCC, DDPF_RGB = 0x1, 0x4, 0x40
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
DDSCAPS2_CUBEMAP = 0x200
DDSCAPS2_CUBEMAP_ALLFACES = 0xFC00


def info(data):
    """-> dict(width, height, mips, format, cube) or None if not a DDS."""
    if len(data) < 128 or data[:4] != b'DDS ':
        return None
    (size, flags, h, w, pitch, depth, mips) = struct.unpack_from('<7I', data, 4)
    pf_flags, fourcc = struct.unpack_from('<I4s', data, 80)
    bits = struct.unpack_from('<I', data, 88)[0]
    caps2 = struct.unpack_from('<I', data, 112)[0]
    # compressed: the FourCC names the format; raw: 'RGB32' etc.
    fmt = fourcc.decode('latin1') if pf_flags & DDPF_FOURCC else 'RGB%d' % bits
    return {'width': w, 'height': h, 'mips': max(1, mips), 'format': fmt,
            'cube': bool(caps2 & DDSCAPS2_CUBEMAP)}


SRGB_OFFSET = 0x74   # dwCaps3. The Arcade engine samples a texture as sRGB when it is 1
                     # (the game's texture loader sets D3DSAMP_SRGBTEXTURE)


def is_srgb(data):
    """True when the game will decode this texture as sRGB (colour maps);
    False for data maps (normal maps, lightmaps, masks)."""
    return len(data) >= 128 and data[:4] == b'DDS ' and struct.unpack_from('<I', data, SRGB_OFFSET)[0] == 1


def with_srgb(data, srgb):
    """DDS bytes with the sRGB flag set or cleared (unchanged when it already matches)."""
    if len(data) < 128 or data[:4] != b'DDS ' or is_srgb(data) == bool(srgb):
        return data
    b = bytearray(data)
    struct.pack_into('<I', b, SRGB_OFFSET, 1 if srgb else 0)
    return bytes(b)


def _mips(rgba):
    """rgba: (h, w, 4) float 0..1 -> list of uint8 BGRA levels down to 1x1."""
    levels = []
    img = rgba.astype(np.float32)
    while True:
        # DDS A8R8G8B8 stores bytes as B, G, R, A
        bgra = np.clip(np.rint(img[..., [2, 1, 0, 3]] * 255.0), 0, 255).astype(np.uint8)
        levels.append(bgra)
        h, w = img.shape[:2]
        if h == 1 and w == 1:
            break
        # next level: average 2x2 pixel blocks (a "box filter"); an odd last
        # row/column is dropped, and a 1-pixel side stays 1
        h2, w2 = max(1, h // 2), max(1, w // 2)
        img = img[:h2 * 2 if h > 1 else 1, :w2 * 2 if w > 1 else 1]
        if h > 1:
            img = 0.5 * (img[0::2] + img[1::2])
        if w > 1:
            img = 0.5 * (img[:, 0::2] + img[:, 1::2])
    return levels


def _header(w, h, mips, cube):
    """128-byte header for an uncompressed 32-bit A8R8G8B8 texture."""
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PITCH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT
    caps = DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX
    caps2 = (DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_ALLFACES) if cube else 0
    # magic, header size 124, flags, height, width, pitch (bytes per row), depth, mips
    hdr = struct.pack('<4s7I', b'DDS ', 124, flags, h, w, w * 4, 0, mips)
    hdr += b'\x00' * 44                                  # reserved
    # pixel format: size 32, RGB + alpha, no FourCC, 32 bits, channel masks
    hdr += struct.pack('<2I4s5I', 32, DDPF_RGB | DDPF_ALPHAPIXELS, b'\x00' * 4, 32,
                       0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    # caps, caps2, caps3 (sRGB flag - 0 here, set with with_srgb), caps4, reserved
    hdr += struct.pack('<4I', caps, caps2, 0, 0) + b'\x00' * 4
    assert len(hdr) == 128
    return hdr


def write_2d(rgba):
    """rgba: (h, w, 4) float array, row 0 = top. Returns DDS bytes."""
    levels = _mips(rgba)
    h, w = rgba.shape[:2]
    return _header(w, h, len(levels), False) + b''.join(l.tobytes() for l in levels)


def write_cube(faces):
    """faces: 6 (s, s, 4) float arrays in DDS order +X -X +Y -Y +Z -Z."""
    assert len(faces) == 6
    s = faces[0].shape[0]
    out = []
    n = 0
    for f in faces:
        # each face is followed by its own mip chain
        levels = _mips(f)
        n = len(levels)
        out.append(b''.join(l.tobytes() for l in levels))
    return _header(s, s, n, True) + b''.join(out)


def decode_bgra8_top(data):
    """Top mip of an uncompressed 32-bit DDS as (h, w, 4) float RGBA, else None."""
    i = info(data)
    if not i or i['format'] != 'RGB32':
        return None
    w, h = i['width'], i['height']
    px = np.frombuffer(data, np.uint8, count=w * h * 4, offset=128).reshape(h, w, 4)
    return px[..., [2, 1, 0, 3]].astype(np.float32) / 255.0    # BGRA -> RGBA
