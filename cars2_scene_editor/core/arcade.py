"""Cars 2 Arcade (the X360-derived "Win32" build) specifics.

Everything here was read from the Arcade build itself: the compiled effects in
assets/effects/effects/*.tfx (D3D9 shaders with debug info, disassembled with
d3dcompiler) and sdaemon.exe (Ghidra).

Baked lighting
    Every lit world part uses exactly one of:
      * a lightmap: effect name ends in "_lmh", a `lightmapUV` stream and a
        per-instance `lightMap1` texture (SubGeometryLit InstanceTextureEntries;
        the material's own lightMap1 is a "white" placeholder).
        Decode (DecodeRGBMTex):  light = (rgb * a * 6) ** 2
      * vertex colours: effect name ends in "_vb", a `VertexBaked` stream.
        Decode (vertex shader):  light = (rgb * a * 6) ** 2.2
    `ColorOcclusion` multiplies that light. Parts with neither are lit at run
    time from the light rig / volume probes (objectSH) and the sun.

Texture blending (m2_/m3_/m4_ effects)
    layer k uses Uv1 * split<k>_scale; layer 2/3/4 weight =
    min(layer alpha, ColorBlend.b / .g / .r); the colours are lerped in order.

Vertex colours are UNORM8X4 in RGBA byte order (the shader reads
ColorBlend.z, which is byte 2).

Volume probes
    DirectSH holds Density.x * .y * .z cells of 27 floats (R, G, B x 9 SH),
    cell (x, y, z) at x + y*Dx + z*Dx*Dy, trilinearly sampled.

Index buffers
    Every index stream is followed by a second list of the same length that
    the renderer never reads (the game's IndexStream loader draws Length indices from
    IndexBufferOffset); octscene.BufferBuilder keeps it.

Effect names
    An effect name is a code describing the shader, e.g. a_1_dmmnsmqdrqcucm_lmh:
    'a_1_' family (one texture layer), letters for its features (d diffuse,
    n normal map, s specular, v vertex colour, ...), and a suffix for the
    baked lighting (_lmh lightmap, _vb vertex baked). Shaders with a 'v' read
    a vertex colour stream - if a mesh is exported without it, the game reads
    black (see effect_streams / layout_for_streams).
"""
import os
import struct

import numpy as np

# RGBM: stored = (light ** (1/power)) / RANGE, split into colour * multiplier
LIGHTMAP_RANGE = 6.0
LIGHTMAP_POWER = 2.0
VERTEX_BAKED_POWER = 2.2

# vertex streams only Arcade worlds have
ARCADE_STREAMS = ('VertexBaked', 'lightmapUV')


def is_arcade(scene):
    """octscene.OctScene -> True for an Arcade world (PC/Steam "Win32_Wii"
    worlds store their baked light as ColorPrelight instead)."""
    for s in scene.vstreams:
        for e in s.get('Elements', {}).values():
            if e.get('Name') in ARCADE_STREAMS:
                return True
    for n in scene.nodes:
        for te in n.get('InstanceTextureEntries', {}).values():
            if te.get('Usage') == 'lightMap1':
                return True
    return False


def baked_mode(effect):
    """'LIGHTMAP', 'VERTEX' or 'NONE' from an effect name's suffix."""
    if effect.endswith('_lmh') or '_lmh_' in effect:
        return 'LIGHTMAP'
    if effect.endswith('_vb') or '_vb_' in effect:
        return 'VERTEX'
    return 'NONE'


# -- RGBM ----------------------------------------------------------------------

def rgbm_decode(rgba, power):
    """rgba (..., 4) floats 0..1 -> linear light (..., 3)."""
    rgba = np.asarray(rgba, np.float64)
    return (rgba[..., :3] * rgba[..., 3:4] * LIGHTMAP_RANGE) ** power


def rgbm_encode(light, power):
    """linear light (..., 3) -> rgba (..., 4) floats 0..1 (alpha = multiplier,
    rounded up to a whole byte so the colour channels stay <= 1)."""
    light = np.maximum(np.asarray(light, np.float64), 0.0)
    v = light ** (1.0 / power) / LIGHTMAP_RANGE
    # the multiplier is the brightest channel, so rgb = v / m stays in 0..1
    m = np.clip(v.max(axis=-1, keepdims=True), 1.0 / 255.0, 1.0)
    m = np.ceil(m * 255.0) / 255.0
    rgb = np.clip(v / m, 0.0, 1.0)
    return np.concatenate([rgb, m], axis=-1)


# -- effects (.tfx) ------------------------------------------------------------

def effects_dir(assets_dir):
    """<assets>/effects/effects, where the compiled .tfx effects live (or None).
    Many features (vertex layouts, Game Look) need this folder."""
    if not assets_dir:
        return None
    p = os.path.join(assets_dir, 'effects', 'effects')
    return p if os.path.isdir(p) else None


def read_effect(assets_dir, effect):
    """Decoded .tfx tree for `effect`, or None. World effects live in
    effects/effects; the engine's own (shared, postprocess, avglum, bloom,
    ambient ...) in the startup packages (see engine_effect_bytes)."""
    d = effects_dir(assets_dir)
    if d is None:
        return None
    p = os.path.join(d, effect.lower() + '.tfx')
    if os.path.exists(p):
        with open(p, 'rb') as f:
            data = f.read()
    else:
        data = engine_effect_bytes(assets_dir, effect)
        if data is None:
            return None
    import c2oct
    try:
        return c2oct.decode(data)[0]
    except ValueError:
        return None


# where the game keeps its engine effects: a loose startup folder, then the
# startup packages the game itself loads (mod copies such as *_cafe_mod.zip
# are left alone)
ENGINE_EFFECT_SOURCES = ('startup', 'startup.zip', 'startup3.zip', 'startup_main.zip')
_engine_effects = {}


def engine_effect_bytes(assets_dir, effect):
    """.tfx bytes of an engine effect from <assets>/startup*/effects, or None."""
    return engine_file_bytes(assets_dir, 'effects/%s.tfx' % effect.lower())


def engine_file_bytes(assets_dir, name):
    """Bytes of a file the engine loads from its startup packages
    (<assets>/startup* folders or zips; name e.g. 'effects/shared.tfx',
    'misc/environmentscroll.dds'), or None."""
    name = name.replace('\\', '/').lower()
    key = (assets_dir, name)
    if key in _engine_effects:
        return _engine_effects[key]
    data = None
    for src in ENGINE_EFFECT_SOURCES:
        p = os.path.join(assets_dir, src)
        if os.path.isdir(p):
            f = os.path.join(p, name)
            if os.path.exists(f):
                with open(f, 'rb') as fh:
                    data = fh.read()
                break
        elif os.path.isfile(p):
            import zipfile
            try:
                with zipfile.ZipFile(p) as z:
                    hit = next((n for n in z.namelist() if n.replace('\\', '/').lower() == name), None)
                    if hit is not None:
                        data = z.read(hit)
                        break
            except (OSError, zipfile.BadZipFile):
                continue
    _engine_effects[key] = data
    return data


def effect_exists(assets_dir, effect):
    """True when the game has a .tfx for this effect name."""
    d = effects_dir(assets_dir)
    return d is not None and os.path.exists(os.path.join(d, effect.lower() + '.tfx'))


def effect_streams(tfx):
    """Vertex element names the effect's Standard technique binds, in order."""
    # the technique's annotation lists one 'string#<ElementName>' per input
    for k, v in tfx.items():
        if k.split('#', 1)[-1] == 'Standard' and isinstance(v, dict):
            ann = v.get('Annotation', {})
            return [k2.split('#', 1)[1] for k2 in ann if k2.startswith('string#')]
    return []


def effect_samplers(tfx):
    """Texture usages (semantics) the effect samples."""
    out = []
    for s in tfx.get('Samplers', {}).values():
        if isinstance(s, dict) and s.get('Semantic'):
            out.append(s['Semantic'])
    return out


def texture_animations(tree):
    """Flipbook textures (MaterialComponentPool, Type TextureAnimation):
    {material index: {usage: {'textures': [TexturePool indices], 'rate': frames per second}}}.

    In the game (sdaemon.exe FUN_00c7e4d0) the component sets
    textures[floor(FrameNumber) % len(textures)] on the material every frame.
    FrameNumber is the output of a FrameSelector processor in the world's
    Meridian network fed by Global.Time; its 20-byte NetworkBuffer block holds
    the rate as a little-endian float at +12 (radiator_a: 10 fps, 6 frames)."""
    comps = tree.get('MaterialComponentPool', {})
    if not comps:
        return {}
    # 1) frame rate of every FrameSelector, by the name of its Output node
    rates = {}
    for sn in tree.get('SubNetworkPool', {}).values():
        if not isinstance(sn, dict):
            continue
        hs = sn.get('HeaderStrings', [])
        dn = sn.get('DataNodePool', {})
        buf = bytes(sn.get('NetworkBuffer', b''))
        for p in sn.get('ProcessorNodePool', {}).values():
            if not isinstance(p, dict) or p.get('Type') != 'FrameSelector':
                continue
            attrs = list(p.get('DataNodeAttributes', []))
            refs = list(p.get('DataNodeRefs', []))
            if 'Output' not in attrs or not p.get('NetworkBufferOffsets'):
                continue
            out = dn.get('DataNode#%d' % refs[attrs.index('Output')], {})
            h = out.get('Header', 0) - 1          # Header is 1-based into HeaderStrings
            off = p['NetworkBufferOffsets'][0]
            if 0 <= h < len(hs) and off + 16 <= len(buf):
                rates[hs[h]] = struct.unpack_from('<f', buf, off + 12)[0]
    # 2) which materials each component is attached to
    links = {}
    for a in tree.get('AssociationPool', {}).values():
        if isinstance(a, dict) and a.get('Type') == 'MaterialComponentRef_MaterialLink':
            links.setdefault(a.get('MaterialComponentRef'), []).append(a.get('Material'))
    # 3) put it together per material
    out = {}
    for k, c in comps.items():
        if not isinstance(c, dict) or c.get('Type') != 'TextureAnimation':
            continue
        ci = int(k.split('#')[1])
        texs = [int(x) for x in c.get('TextureReferences', [])]
        if not texs:
            continue
        rate = rates.get(c.get('AnimationNode', ''), 10.0)
        for mi in links.get(ci, []):
            out.setdefault(mi, {})[c.get('Usage', 'diffuseMapA1')] = {'textures': texs, 'rate': float(rate)}
    return out


def effect_defaults(tfx):
    """{parameter name: default value} of the material parameters."""
    out = {}
    for p in tfx.get('Parameters', {}).values():
        if isinstance(p, dict) and 'Default' in p and p.get('Scope') in ('PerMaterial', 'PerInstance', 'NoRegister'):
            out[p['Name']] = p['Default']
    return out


def baked_variant(effect, mode):
    """Effect name with the baked-lighting suffix switched to `mode`
    ('LIGHTMAP' -> _lmh, 'VERTEX' -> _vb). The suffix is the last token."""
    base = effect
    for suf in ('_lmh', '_vb'):
        if base.endswith(suf):
            base = base[:-len(suf)]
    return base + {'LIGHTMAP': '_lmh', 'VERTEX': '_vb'}.get(mode, '')


def element_type_guess(name):
    """Storage type for a vertex element when the world has no example of it
    (matches what shipping Arcade worlds use)."""
    import vertexfmt as vf
    if name == 'Position':
        return vf.F32X3
    if name.startswith('Uv') or name.endswith('UV'):
        return vf.F16X2
    if name.startswith(('Normal', 'Tangent', 'Binormal')):
        return vf.F16X4
    if name == 'BoneIndices':
        return vf.U8X4
    return vf.UNORM8X4          # colours


def world_element_types(scene):
    """{element name: most common type} over a world's vertex streams."""
    from collections import Counter
    c = {}
    for s in scene.vstreams:
        for e in s.get('Elements', {}).values():
            c.setdefault(e['Name'], Counter())[e['Type']] += 1
    return {k: v.most_common(1)[0][0] for k, v in c.items()}


def layout_for_streams(names, types=None):
    """Vertex layout for the streams an effect binds, in the shipping Arcade
    arrangement: [Position + Uv1] then one stream with everything else.
    -> [(width, [(name, type, offset)])]"""
    import vertexfmt as vf
    types = types or {}
    first = [n for n in ('Position', 'Uv1') if n in names]
    rest = [n for n in names if n not in first]
    out = []
    for group in (first, rest):
        if not group:
            continue
        # elements packed back to back; the stream width is the sum
        elems, off = [], 0
        for n in group:
            ty = types.get(n, element_type_guess(n))
            elems.append((n, ty, off))
            off += vf.TYPE_SIZE[ty]
        out.append((off, elems))
    return out


# -- volume probes ---------------------------------------------------------------

def probe_cells(density):
    """Number of cells in a volume probe of Density (x, y, z)."""
    dx, dy, dz = (max(1, int(v)) for v in density)
    return dx * dy * dz


def probe_grid(direct_sh, density):
    """flat DirectSH -> (Dz, Dy, Dx, 3, 9)."""
    dx, dy, dz = (max(1, int(v)) for v in density)
    return np.asarray(direct_sh, np.float64).reshape(dz, dy, dx, 3, 9)


def probe_cell_positions(density):
    """Normalised cell centres in -1..1 box space, in DirectSH order (x fastest)."""
    dx, dy, dz = (max(1, int(v)) for v in density)
    ax = [np.linspace(-1.0, 1.0, d) if d > 1 else np.zeros(1) for d in (dx, dy, dz)]
    z, y, x = np.meshgrid(ax[2], ax[1], ax[0], indexing='ij')
    return np.stack([x.ravel(), y.ravel(), z.ravel()], 1)


# -- textures --------------------------------------------------------------------

def neutral_lightmap_dds(size=4):
    """Lightmap of light 1.0 everywhere (RGBM 255,255,255,43)."""
    import dds
    px = np.empty((size, size, 4), np.float32)
    px[...] = rgbm_encode(np.ones(3), LIGHTMAP_POWER)
    return dds.write_2d(px)


def dds_rgba(data):
    """Decode an uncompressed A8R8G8B8 / X8R8G8B8 or DXT1/3/5 DDS (top mip)
    -> (h, w, 4) float32, rows top-down. Used to seed lightmap re-bakes."""
    if data[:4] != b'DDS ':
        raise ValueError('not a DDS')
    h, w = struct.unpack_from('<II', data, 12)
    pf_flags, fourcc, bits, rm, gm, bm, am = struct.unpack_from('<I4sIIIII', data, 80)
    body = memoryview(data)[128:]
    if pf_flags & 0x4:
        fmt = fourcc.decode('latin1')
        return _decode_dxt(bytes(body), w, h, fmt)
    if bits != 32:
        raise ValueError('unsupported DDS format (%d bit)' % bits)
    a = np.frombuffer(body, np.uint8, w * h * 4).reshape(h, w, 4).astype(np.float32) / 255.0
    out = np.empty_like(a)
    # use the channel masks to find which byte holds R, G and B
    shifts = {0xFF0000: 2, 0xFF00: 1, 0xFF: 0, 0xFF000000: 3}
    for ch, mask in enumerate((rm, gm, bm)):
        out[..., ch] = a[..., shifts.get(mask, 2 - ch)]
    out[..., 3] = a[..., 3] if (pf_flags & 0x1 and am) else 1.0     # X8R8G8B8: opaque
    return out


def _rgb565(c):
    """16-bit 5:6:5 colours -> (..., 3) floats 0..1."""
    r = ((c >> 11) & 31) / 31.0
    g = ((c >> 5) & 63) / 63.0
    b = (c & 31) / 31.0
    return np.stack([r, g, b], -1)


def _decode_dxt(data, w, h, fmt):
    """DXT1/3/5 top mip -> (h, w, 4) floats (vectorised over all 4x4 blocks).

    Each 4x4 block has two 5:6:5 end colours and 2-bit indices picking one
    of 4 palette colours made from them. DXT3 adds explicit 4-bit alpha,
    DXT5 interpolated alpha (two end values + 3-bit indices)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    nb = bw * bh
    bsize = 8 if fmt == 'DXT1' else 16
    blk = np.frombuffer(data, np.uint8, nb * bsize).reshape(nb, bsize)
    cb = blk[:, -8:]                                    # the colour half of each block
    c0 = cb[:, 0].astype(np.uint32) | (cb[:, 1].astype(np.uint32) << 8)
    c1 = cb[:, 2].astype(np.uint32) | (cb[:, 3].astype(np.uint32) << 8)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    # 4-colour mode, or (DXT1 with c0 <= c1) 3 colours + transparent black
    four = (c0 > c1) | (fmt != 'DXT1')
    pal = np.empty((nb, 4, 4), np.float32)
    pal[:, 0, :3], pal[:, 1, :3] = p0, p1
    pal[:, 2, :3] = np.where(four[:, None], (2 * p0 + p1) / 3, (p0 + p1) / 2)
    pal[:, 3, :3] = np.where(four[:, None], (p0 + 2 * p1) / 3, 0.0)
    pal[:, :, 3] = 1.0
    if fmt == 'DXT1':
        pal[:, 3, 3] = np.where(four, 1.0, 0.0)
    # 16 2-bit palette indices per block
    bits = cb[:, 4:8].astype(np.uint32)
    idx = bits[:, 0] | (bits[:, 1] << 8) | (bits[:, 2] << 16) | (bits[:, 3] << 24)
    sel = (idx[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    px = np.take_along_axis(pal, sel[:, :, None].astype(np.int64).repeat(4, 2), 1)  # (nb,16,4)
    if fmt == 'DXT5':
        ab = blk[:, :8]
        a0, a1 = ab[:, 0].astype(np.float32) / 255.0, ab[:, 1].astype(np.float32) / 255.0
        apal = np.empty((nb, 8), np.float32)
        apal[:, 0], apal[:, 1] = a0, a1
        big = a0 > a1
        for k in range(2, 8):    # 8-alpha block: six interpolated values
            seven = ((8 - k) * a0 + (k - 1) * a1) / 7.0
            five = ((6 - k) * a0 + (k - 1) * a1) / 5.0 if k < 6 else (0.0 if k == 6 else 1.0)
            apal[:, k] = np.where(big, seven, five)
        # 16 3-bit alpha indices packed into 6 bytes
        abits = np.zeros(nb, np.uint64)
        for k in range(6):
            abits |= ab[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
        asel = (abits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)
        px[:, :, 3] = np.take_along_axis(apal, asel.astype(np.int64), 1)
    elif fmt == 'DXT3':
        ab = blk[:, :8]
        nib = np.stack([ab & 15, ab >> 4], -1).reshape(nb, 16)
        px[:, :, 3] = nib / 15.0
    # blocks -> image rows (block row, pixel row, block column, pixel column)
    img = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return img[:h, :w]
