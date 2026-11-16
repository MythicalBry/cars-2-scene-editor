"""World .dbl ("GBWIN" database) read/write.

Layout (little endian), verified byte-exact on every shipping Arcade world:
    0x00 u32 0x30, u32 0, char[8] 'GBWIN', u32 0x100, u32 0,
    0x18 u32 section count, u32 size of the largest section
    0x20 sections, each:
        u8 kind, u8 2, u16 0x0e, u32 data size, u16 1, char[54] name ('1000')
        data: u16 count, u16 1, u32 offsets[count] (from data start), records,
              0xEE padding up to data size
    kinds:
        0x03 POLYLINES  name[64], u32 n, u32 closed, n * float3
        0x10 BSPLINES   u32 flags (degree | 0x20 periodic), u32 n ctrl, u32 n knots,
                        u32 zero[5], n * float3, knots * f32, name (zero-terminated)
        0x83 STRINGS    zero-terminated names (surface types, indexed by the
                        collision mesh's per-triangle surface)
Every record is padded to 4 bytes with 0xEE. The polyline and B-spline sections
describe the same paths: PathCollection serves the B-splines by name
(TrackA/TrackB/TrackC = lap progress, lane*, VictoryCam_*, NavMesh_Spline_*
bridges, *_CollectLine*, choreography/actor splines), the polylines are what
Meridian SplineNodes and spline followers sample.

Typical use:
    d = Dbl.load('radiator_a.dbl')
    d.surface_types()                 # ['default', ..., 'asphalt', ...]
    d.set_path('TrackA', points, True)
    d.save('radiator_a.dbl')
"""
import struct

POLYLINES, BSPLINES, STRINGS = 0x03, 0x10, 0x83
PAD = 0xEE      # filler byte used for all padding


def _cstr(b):
    """Bytes up to the first zero, as text."""
    return b.split(b'\0', 1)[0].decode('latin1')


def _pad4(b):
    """Pad to a multiple of 4 bytes with 0xEE."""
    return b + bytes([PAD]) * (-len(b) % 4)


class Polyline:
    """A path as straight segments between points (what Meridian samples)."""

    def __init__(self, name, points, closed=False, raw_name=None):
        self.name, self.points, self.closed = name, [tuple(p) for p in points], bool(closed)
        self.raw_name = raw_name    # 64-byte field incl. leftovers, kept for byte-exact output

    def pack(self):
        # reuse the original 64-byte name field (with whatever garbage the
        # game's tool left after the zero) when the name is unchanged
        nm = self.raw_name if self.raw_name is not None and _cstr(self.raw_name) == self.name \
            else self.name.encode('latin1')[:63].ljust(64, b'\0')
        out = nm + struct.pack('<II', len(self.points), int(self.closed))
        out += b''.join(struct.pack('<3f', *p) for p in self.points)
        return _pad4(out)


class BSpline:
    """A path as a smooth B-spline curve (what PathCollection serves)."""

    def __init__(self, name, ctrl, knots, degree=3, periodic=False):
        self.name, self.ctrl, self.knots = name, [tuple(p) for p in ctrl], list(knots)
        self.degree, self.periodic = degree, bool(periodic)

    def pack(self):
        out = struct.pack('<3I5I', self.degree | (0x20 if self.periodic else 0), len(self.ctrl),
                          len(self.knots), 0, 0, 0, 0, 0)
        out += b''.join(struct.pack('<3f', *p) for p in self.ctrl)
        out += struct.pack('<%df' % len(self.knots), *self.knots)
        return _pad4(out + self.name.encode('latin1') + b'\0')

    @staticmethod
    def uniform(name, points, closed):
        """Cubic B-spline with `points` as control points, laid out like the
        shipping ones. Closed curves are periodic: the n distinct points, then
        the first 3 again (90 of the 97 closed shipping splines), and
        ctrl + 2 uniform knots. A closing point equal to the first (closed
        polylines repeat it) is dropped first. Open curves are clamped
        (0,0, 0..1 in n-3 steps, 1,1). The shipping curves are refits with
        fewer control points than the polyline; using the polyline itself
        gives a slightly smoothed copy of it."""
        n = len(points)
        if closed:
            pts = [tuple(p) for p in points]
            if len(pts) > 3 and all(abs(a - b) < 1e-4 for a, b in zip(pts[0], pts[-1])):
                pts = pts[:-1]
            m = len(pts)
            ctrl = pts + pts[:3]
            return BSpline(name, ctrl, [(i - 2) / m for i in range(len(ctrl) + 2)], 3, True)
        m = max(1, n - 3)
        return BSpline(name, points, [0.0, 0.0] + [i / m for i in range(m + 1)] + [1.0, 1.0], 3, False)


class Section:
    """One section of the file: a list of records of one kind.
    `size` is the original data size (kept so unchanged files stay
    byte-identical; None = compute a fresh size)."""

    def __init__(self, kind, items, name='1000', flag=2, unk=0x0e, version=1, size=None):
        self.kind, self.items, self.name = kind, items, name
        self.flag, self.unk, self.version, self.size = flag, unk, version, size

    def pack_data(self):
        """Records + offset table + 0xEE padding -> section data bytes."""
        recs = []
        for it in self.items:
            recs.append(_pad4(it.encode('latin1') + b'\0') if self.kind == STRINGS else it.pack())
        head = 4 + 4 * len(recs)         # count, 1, then one offset per record
        offs, o = [], head
        for r in recs:
            offs.append(o)
            o += len(r)
        data = struct.pack('<HH', len(recs), 1) + struct.pack('<%dI' % len(recs), *offs) + b''.join(recs)
        # keep the original size when it still fits, else round up to 32 bytes
        size = self.size if self.size is not None and self.size >= len(data) else len(data) + (-len(data) % 32)
        return data + bytes([PAD]) * (size - len(data))


class Dbl:
    """A whole .dbl file: a list of sections."""

    def __init__(self, sections=None):
        self.sections = sections or []

    def section(self, kind):
        """First section of a kind (or None)."""
        for s in self.sections:
            if s.kind == kind:
                return s
        return None

    # -- convenience -----------------------------------------------------
    def polylines(self):
        s = self.section(POLYLINES)
        return s.items if s else []

    def bsplines(self):
        s = self.section(BSPLINES)
        return s.items if s else []

    def surface_types(self):
        """Collision surface names; a collision triangle's surface_type is an index into this list."""
        s = self.section(STRINGS)
        return s.items if s else []

    def path(self, name):
        """The polyline called `name` (or None)."""
        for p in self.polylines():
            if p.name == name:
                return p
        return None

    def set_path(self, name, points, closed):
        """Add or replace a path in both representations."""
        pl = self.path(name)
        if pl is None:
            sec = self.section(POLYLINES) or self._add(POLYLINES)
            sec.items.append(Polyline(name, points, closed))
        else:
            pl.points, pl.closed, pl.raw_name = [tuple(p) for p in points], bool(closed), None
        sec = self.section(BSPLINES) or self._add(BSPLINES)
        bs = BSpline.uniform(name, points, closed)
        for i, b in enumerate(sec.items):
            if b.name == name:
                sec.items[i] = bs
                break
        else:
            sec.items.append(bs)
        # sizes changed: let every section compute a fresh size
        for s in self.sections:
            s.size = None

    def remove_path(self, name):
        """Delete a path from both representations."""
        for kind in (POLYLINES, BSPLINES):
            s = self.section(kind)
            if s:
                s.items = [x for x in s.items if x.name != name]
                s.size = None

    def _add(self, kind):
        """Create an empty section, keeping the shipping order
        (polylines, B-splines, strings)."""
        s = Section(kind, [])
        order = {POLYLINES: 0, BSPLINES: 1, STRINGS: 2}
        self.sections.append(s)
        self.sections.sort(key=lambda x: order.get(x.kind, 9))
        return s

    # -- io ----------------------------------------------------------------
    @staticmethod
    def parse(d):
        """File bytes -> Dbl (layout in the module docstring)."""
        if d[8:13] != b'GBWIN':
            raise ValueError('not a GBWIN .dbl')
        count, = struct.unpack_from('<I', d, 0x18)
        off, secs = 0x20, []
        for _ in range(count):
            # 64-byte section header
            kind, flag, unk, size, ver = struct.unpack_from('<BBHIH', d, off)
            name = _cstr(d[off + 10:off + 0x40])
            base = off + 0x40                       # section data starts here
            n, _one = struct.unpack_from('<HH', d, base)
            offs = struct.unpack_from('<%dI' % n, d, base + 4)
            items = []
            for k, o in enumerate(offs):
                r = base + o
                if kind == POLYLINES:
                    raw = d[r:r + 64]
                    cnt, closed = struct.unpack_from('<II', d, r + 64)
                    pts = [struct.unpack_from('<3f', d, r + 72 + 12 * i) for i in range(cnt)]
                    items.append(Polyline(_cstr(raw), pts, closed, raw))
                elif kind == BSPLINES:
                    fl, nc, nk = struct.unpack_from('<3I', d, r)
                    p = r + 32                      # after flags, counts and 5 zero words
                    ctrl = [struct.unpack_from('<3f', d, p + 12 * i) for i in range(nc)]
                    p += 12 * nc
                    knots = list(struct.unpack_from('<%df' % nk, d, p))
                    p += 4 * nk
                    items.append(BSpline(_cstr(d[p:p + 256]), ctrl, knots, fl & 0x1f, fl & 0x20))
                else:
                    items.append(_cstr(d[r:r + 256]))
            secs.append(Section(kind, items, name, flag, unk, ver, size))
            off = base + size
        return Dbl(secs)

    def pack(self):
        """Dbl -> file bytes."""
        body = b''
        sizes = []
        for s in self.sections:
            data = s.pack_data()
            sizes.append(len(data))
            hdr = struct.pack('<BBHIH', s.kind, s.flag, s.unk, len(data), s.version)
            hdr += s.name.encode('latin1').ljust(0x40 - 10, b'\0')
            body += hdr + data
        head = struct.pack('<II', 0x30, 0) + b'GBWIN\0\0\0' + struct.pack('<IIII', 0x100, 0, len(self.sections),
                                                                         max(sizes) if sizes else 0)
        return head + body

    @staticmethod
    def load(path):
        with open(path, 'rb') as f:
            return Dbl.parse(f.read())

    def save(self, path):
        with open(path, 'wb') as f:
            f.write(self.pack())
