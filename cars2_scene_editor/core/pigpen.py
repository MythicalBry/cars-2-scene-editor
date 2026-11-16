"""The game's particle definitions: assets/particles/<world>_d1..4.zip.

Each zip holds a Pigpen data file (<world>_d1.dbl, not the GBWIN .dbl of
worlds) and a .oct with the particle materials (MaterialPool, effects/
part_*.tfx in the zip) and textures (TexturePool, DDS in .tbuf files).
universal.zip has the definitions every world shares.

The .dbl (sdaemon.exe FUN_00d6b410 / dblLoad
0x00752cb0): a 0x20-byte header ("DX" at +8, version 0x100 at +0x10), then
chunks [u16 type, u16 flags, u32 size]; the chunk header is 10 bytes (0x20
with flags & 8), plus a 0x20-byte name with flags & 2. Each chunk's data
starts with a table [u16 count, u16 flags]: flags & 3 == 1 lists u32 record
offsets (from the table), == 2 gives a u32 stride and fixed records from +8
(+0x10 with flags & 8).

  0x64 emitters  (names in 0x84)     0x65 particle systems (names in 0x88)
  0x66 effects   (names in 0x85)

Fields named from the loader and the spawn code (FUN_00d90fc0 particle,
FUN_00d93fd0 velocity, FUN_00d60480 effect, FUN_00d60e40 emitter, the
system fix-up FUN_00755d20). Randomisation (FUN_00d8d140) is left out.
"""
import struct
import zipfile

import numpy as np

EMITTERS, SYSTEMS, EFFECTS = 0x64, 0x65, 0x66
NAMES = {EMITTERS: 0x84, SYSTEMS: 0x88, EFFECTS: 0x85}


def chunks(d):
    """[(type, flags, data offset, size)] of a Pigpen .dbl."""
    if len(d) < 0x20 or struct.unpack_from('<I', d, 0x10)[0] != 0x100:
        raise ValueError('not a Pigpen particle .dbl')
    o = 0x20
    out = []
    while o + 8 <= len(d):
        typ, flags, size = struct.unpack_from('<HHI', d, o)
        hsz = (0x20 if flags & 8 else 10) + (0x20 if flags & 2 else 0)
        out.append((typ, flags, o + hsz, size))
        o += hsz + size
    return out


def table(d, start):
    """Record offsets of the table at `start`."""
    cnt, fl = struct.unpack_from('<HH', d, start)
    if fl & 3 == 1:
        return [start + x for x in struct.unpack_from('<%dI' % cnt, d, start + 4)]
    if fl & 3 == 2:
        stride = struct.unpack_from('<I', d, start + 4)[0]
        base = start + (0x10 if fl & 8 else 8)
        return [base + i * stride for i in range(cnt)]
    return []


def _cstr(d, o):
    return d[o:d.index(b'\0', o)].decode('latin1')


class Curve:
    """A system's value over the particle's life (0..1): 8 floats and a type
    byte (record + 0xfc + k * 0x24). Type 3 holds f[0]; the others are a
    cubic Bezier from (0, f0) through (f1, f2), (f3, f4) to (1, f5) (the
    loader triples f2 and f4 of type 0)."""

    def __init__(self, f, typ):
        self.f, self.typ = f, typ
        if typ != 3:
            u = np.linspace(0.0, 1.0, 65)
            a, b = 1.0 - u, u
            w = (a ** 3, 3 * a * a * b, 3 * a * b * b, b ** 3)
            self.t = w[1] * f[1] + w[2] * f[3] + w[3]
            self.v = w[0] * f[0] + w[1] * f[2] + w[2] * f[4] + w[3] * f[5]
            order = np.argsort(self.t)
            self.t, self.v = self.t[order], self.v[order]

    def __call__(self, x):
        if self.typ == 3:
            return np.full(np.shape(x), self.f[0], np.float32)
        return np.interp(x, self.t, self.v).astype(np.float32)


class System:
    """One particle system (508-byte record)."""

    def __init__(self, d, o, name):
        self.name = name
        self.flags = struct.unpack_from('<I', d, o + 8)[0]
        self.material = struct.unpack_from('<H', d, o + 0xc)[0]
        self.frame_rate = d[o + 0x90]                       # flipbook frames per second
        self.life = d[o + 0x92] / 255.0 * 50.0              # seconds (0: lives on)
        self.size = struct.unpack_from('<H', d, o + 0x98)[0] / 65535.0 * 1000.0 * 0.5   # half width
        self.size_y = struct.unpack_from('<H', d, o + 0x9a)[0] / 65535.0 * 1000.0 * 0.5
        self.direction = np.array(struct.unpack_from('<3b', d, o + 0x94), np.float32) / 127.0
        self.speed = struct.unpack_from('<f', d, o + 0xb8)[0]
        self.cone = (d[o + 0xbd] + d[o + 0xbe] * 0.5) / 255.0 * np.pi   # spread around the direction
        self.spin = struct.unpack_from('<f', d, o + 0xc8)[0]            # radians a second
        self.color0 = np.array(struct.unpack_from('<3f', d, o + 0x1d4), np.float32) / 255.0
        self.color1 = np.array(struct.unpack_from('<3f', d, o + 0x1e0), np.float32) / 255.0
        self.curves = []
        for k in range(6):
            c = o + 0xfc + k * 0x24
            self.curves.append(Curve(struct.unpack_from('<8f', d, c), d[c + 0x20]))
        # curves: 0 size, 1 size Y, 2..4 colour (flags & 0x4000000), 5 alpha

    @property
    def curve_colour(self):
        return bool(self.flags & 0x4000000)


class Effect:
    """A list of systems started together: [(system index, delay, spread,
    count, count variance, chance)]."""

    def __init__(self, d, o, name):
        self.name = name
        n = struct.unpack_from('<I', d, o + 4)[0]
        self.entries = []
        e = o + 8
        for _ in range(n):
            si = struct.unpack_from('<I', d, e)[0]
            delay, spread = struct.unpack_from('<2f', d, e + 4)
            count, var, chance = d[e + 0xc], d[e + 0xe], d[e + 0x11]
            self.entries.append((si, delay, spread, count, var, (chance or 100) / 100.0))
            e += 0x14


class Emitter:
    """Starts an effect over and over while it is on."""

    def __init__(self, d, o, name):
        self.name = name
        self.effect = struct.unpack_from('<H', d, o + 4)[0]
        self.kind = d[o + 6]
        self.rate = d[o + 7] / 255.0 * 100.0               # effects a second
        self.radius = struct.unpack_from('<H', d, o + 0x10)[0] / 65535.0 * 1000.0
        self.life = d[o + 0x21] / 255.0 * 50.0             # 0: as long as it is on


class Library:
    """Definitions, materials and textures of one particle zip."""

    def __init__(self, zpath):
        self.zpath = zpath
        self.zip = zipfile.ZipFile(zpath)
        self.lower = {n.lower(): n for n in self.zip.namelist()}
        stem = zpath.replace('\\', '/').rsplit('/', 1)[-1][:-4]
        d = self.read(stem + '.dbl')
        recs, names = {}, {}
        for typ, _fl, start, _size in chunks(d):
            recs[typ] = table(d, start)
        for typ, ntyp in NAMES.items():
            names[typ] = [_cstr(d, r) for r in recs.get(ntyp, [])]
        self.systems = [System(d, r, n) for r, n in zip(recs.get(SYSTEMS, []), names[SYSTEMS])]
        self.effects = [Effect(d, r, n) for r, n in zip(recs.get(EFFECTS, []), names[EFFECTS])]
        self.emitters = [Emitter(d, r, n) for r, n in zip(recs.get(EMITTERS, []), names[EMITTERS])]
        self.effect_by_name = {e.name.strip().lower(): e for e in self.effects}
        self.emitter_by_name = {e.name.strip().lower(): e for e in self.emitters}
        self.oct = None
        oct_name = stem + '.oct'
        if oct_name.lower() in self.lower:
            import c2oct
            self.oct = c2oct.decode(self.read(oct_name))[0]
        self.materials = list((self.oct or {}).get('MaterialPool', {}).values())
        self.textures = list((self.oct or {}).get('TexturePool', {}).values())
        self._blend = {}

    def read(self, name):
        return self.zip.read(self.lower[name.lower()])

    def has(self, name):
        return name.lower() in self.lower

    def material(self, i):
        """(texture index or None, tint RGB, blend) of MaterialPool #i.
        blend: 'ALPHA_PREMULT' (the ar2b effects: One / InvSrcAlpha),
        'ALPHA' (alpha-tested ak effects), 'ADDITIVE_PREMULT'."""
        if not 0 <= i < len(self.materials):
            return None, (1.0, 1.0, 1.0), 'ALPHA_PREMULT'
        m = self.materials[i]
        tex = None
        for te in m.get('TextureEntries', {}).values():
            if te.get('Usage') == 'Base':
                tex = te.get('TextureReference')
                break
        tint = (1.0, 1.0, 1.0)
        for pe in m.get('PropertyEntries', {}).values():
            if pe.get('Name') == 'diffuseColorA1' and len(pe.get('Value') or ()) >= 3:
                tint = tuple(pe['Value'][:3])
        return tex, tint, self.blend(m.get('Effect', ''))

    def blend(self, effect):
        key = effect.lower()
        if key not in self._blend:
            mode = 'ALPHA_PREMULT'
            name = 'effects/%s.tfx' % key
            if self.has(name):
                try:
                    import c2oct
                    t = c2oct.decode(self.read(name))[0]
                    tech = next(v for k, v in t.items() if k.startswith('Technique'))
                    p = next(v for k, v in tech.items() if k.startswith('Pass'))
                    st = {k.split('#')[1]: v for k, v in p.items() if k.startswith('RenderState')}
                    if not st.get('AlphaBlendEnable'):
                        mode = 'ALPHA'
                    else:
                        src, dst = str(st.get('SrcBlend', '')).upper(), str(st.get('DestBlend', '')).upper()
                        mode = {('ONE', 'ONE'): 'ADDITIVE_PREMULT', ('SRCALPHA', 'ONE'): 'ADDITIVE',
                                ('SRCALPHA', 'INVSRCALPHA'): 'ALPHA'}.get((src, dst), 'ALPHA_PREMULT')
                except (ValueError, KeyError, StopIteration):
                    pass
            self._blend[key] = mode
        return self._blend[key]

    def texture_bytes(self, i):
        """(name, DDS bytes, frame count) of TexturePool #i."""
        tx = self.textures[i]
        return tx.get('Name', 'tex%d' % i), self.read(tx['FileName']), int(tx.get('FrameCount', 1) or 1)


def event_list(nb, off):
    """Times of an EventList in a choreography's NetworkBuffer: [start, end,
    u32 flags, u32 count, then count (time, value) pairs]."""
    n = struct.unpack_from('<I', nb, off + 12)[0]
    return [struct.unpack_from('<f', nb, off + 16 + 8 * k)[0] for k in range(n)]
