"""ClipDataBlock codec (byte-exact on every clip block in the Arcade data:
10,165 animation clips + 236 rig driven keys), plus a driven-key builder.

Block
    f32 h0, f32 h1                 animation clip: flags/duration; driven key: input range
    u16 n_binds, u16 n_cmds, u32 pool_offset, u32 offsets[n_binds + n_cmds]
    bind blocks, commands, then the constant pool (f32s) at pool_offset

Command (one Bezier curve per channel; layout from the Cars 2 Toolkit, opcode 2 added here)
    u32 word: pre(0-2) post(3-5) opcode(6-9) channels(10-19) keys(20-31)
    f32 t_start, f32 t_end
    opcode 0: per channel f32 end_slope, end_value, bias, scale; u16 key times
              (t = t_start + span * u / 65535); per key per channel 4 i16 Bezier deltas
    opcode 2: as opcode 0 but f32 key times           (only used by rig driven keys)
    opcode 4: per channel 2 f32; u16 times (+2 pad if odd); per key per channel 4 f32
    opcode 6: per channel 2 f32; f32 times; per key per channel 4 f32
    padded to 4 bytes.
    Values: v = bias + scale * P, P = running sum of the 4 deltas (P0, P1, P2, P3).

Bind block
    u16 slot_count, u16 n_cmd, u16 cmd_index[n_cmd]
    u16 static_offset, u16 animated_count, animated records,
    u16 static_count (at static_offset), static records
    record: u16 n_subs | 0x20 if the target is a scalar (Float), u16 target
            sub-op: u16 word, then 1 u16 operand (category 0x0000/0x3000) or 3
    sub-op word: category bits 12-15 (0x1000 = Euler, order in bits 6-8),
                 operation bits 0-3, operand k reads the command buffer when
                 ((word >> (4 - 2k)) & 3) == 1, else the constant pool.
    In a ClipProcessor the record target is the index into its Output list.

Two jobs:
    decode()/encode()  round-trip existing clip blocks unchanged
    driven_key()       build a new block that maps one input value to
                       outputs (e.g. a wheel rotation driven by a slider);
                       clipeval.py evaluates blocks for previews
"""
import math
import struct

PAD = b'\xee'       # filler byte used for all padding


# ------------------------------------------------------------------ commands
def _cmd_size(op, n_ch, n_keys):
    """Total byte size of a command (rounded up to 4) - used to find where
    the next record starts and to detect trailing padding."""
    if op in (0, 2):
        n = 12 + n_ch * 16 + n_keys * (2 if op == 0 else 4) + n_keys * n_ch * 8
    elif op == 4:
        n = 12 + n_ch * 8 + n_keys * 2 + (2 if n_keys & 1 else 0) + n_keys * n_ch * 16
    elif op == 6:
        n = 12 + n_ch * 8 + n_keys * 4 + n_keys * n_ch * 16
    else:
        raise ValueError('unsupported opcode %d' % op)
    return (n + 3) & ~3


def decode_command(b, off):
    """Command at `off` -> (dict, end offset). The dict keeps every field
    (including padding bytes) so encode_command reproduces it exactly."""
    w = struct.unpack_from('<I', b, off)[0]
    op, n_ch, n_keys = (w >> 6) & 0xF, (w >> 10) & 0x3FF, w >> 20
    c = {'pre': w & 7, 'post': (w >> 3) & 7, 'opcode': op, 't_start': None, 't_end': None}
    c['t_start'], c['t_end'] = struct.unpack_from('<ff', b, off + 4)
    p = off + 12
    if op in (0, 2):
        # quantised: 4 floats per channel, then times, then 16-bit deltas
        c['params'] = [list(struct.unpack_from('<4f', b, p + 16 * ch)) for ch in range(n_ch)]
        p += 16 * n_ch
        if op == 0:
            c['times'] = list(struct.unpack_from('<%dH' % n_keys, b, p)); p += 2 * n_keys
        else:
            c['times'] = list(struct.unpack_from('<%df' % n_keys, b, p)); p += 4 * n_keys
        c['keys'] = [[list(struct.unpack_from('<4h', b, p + (k * n_ch + ch) * 8)) for ch in range(n_ch)]
                     for k in range(n_keys)]
        p += n_keys * n_ch * 8
    else:
        # float: 2 floats per channel, then times, then float deltas
        c['params'] = [list(struct.unpack_from('<2f', b, p + 8 * ch)) for ch in range(n_ch)]
        p += 8 * n_ch
        if op == 4:
            c['times'] = list(struct.unpack_from('<%dH' % n_keys, b, p)); p += 2 * n_keys
            if n_keys & 1:
                c['mid_pad'] = b[p:p + 2]; p += 2       # keep the keys 4-byte aligned
        else:
            c['times'] = list(struct.unpack_from('<%df' % n_keys, b, p)); p += 4 * n_keys
        c['keys'] = [[list(struct.unpack_from('<4f', b, p + (k * n_ch + ch) * 16)) for ch in range(n_ch)]
                     for k in range(n_keys)]
        p += n_keys * n_ch * 16
    end = off + _cmd_size(op, n_ch, n_keys)
    if end > p:
        c['pad'] = b[p:end]
    return c, end


def encode_command(c):
    """Inverse of decode_command."""
    op = c['opcode']
    n_ch, n_keys = len(c['params']), len(c['times'])
    w = (c['pre'] & 7) | (c['post'] & 7) << 3 | op << 6 | n_ch << 10 | n_keys << 20
    out = bytearray(struct.pack('<Iff', w, c['t_start'], c['t_end']))
    if op in (0, 2):
        for prm in c['params']:
            out += struct.pack('<4f', *prm)
        out += struct.pack('<%d%s' % (n_keys, 'H' if op == 0 else 'f'), *c['times'])
        for k in range(n_keys):
            for ch in range(n_ch):
                out += struct.pack('<4h', *c['keys'][k][ch])
    else:
        for prm in c['params']:
            out += struct.pack('<2f', *prm)
        if op == 4:
            out += struct.pack('<%dH' % n_keys, *c['times'])
            if n_keys & 1:
                out += c.get('mid_pad', PAD * 2)
        else:
            out += struct.pack('<%df' % n_keys, *c['times'])
        for k in range(n_keys):
            for ch in range(n_ch):
                out += struct.pack('<4f', *c['keys'][k][ch])
    pad = -len(out) % 4
    out += c.get('pad', PAD * pad)[:pad].ljust(pad, PAD)
    return bytes(out)


# --------------------------------------------------------------------- binds
def _n_operands(word):
    """Number of operands a sub-op has: 1 for categories 0x0000/0x3000, else 3."""
    return 1 if (word & 0xF000) in (0x0000, 0x3000) else 3


def decode_bind(b, off, end):
    """Bind block at `off` (ending at `end`) -> dict (layout in the module docstring)."""
    u16 = lambda o: struct.unpack_from('<H', b, off + o)[0]
    slot_count, n_cmd = u16(0), u16(2)
    idx = [u16(4 + 2 * i) for i in range(n_cmd)]
    base = 4 + 2 * n_cmd
    static_off, anim_count = u16(base), u16(base + 2)

    def walk(p, count):
        # read `count` records starting at relative offset p
        recs = []
        for _ in range(count):
            word, target = u16(p), u16(p + 2); p += 4
            subs = []
            for _ in range(word & 0xF):             # low 4 bits = number of sub-ops
                sw = u16(p); n = _n_operands(sw)
                subs.append([sw] + [u16(p + 2 + 2 * k) for k in range(n)])
                p += 2 + 2 * n if n == 3 else 4
            recs.append({'target': target, 'flags': word & ~0xF, 'subs': subs})
        return recs, p
    anim, p = walk(base + 4, anim_count)
    bind = {'slot_count': slot_count, 'commands': idx, 'animated': anim, 'static': []}
    if static_off:
        bind['static'], p = walk(static_off + 2, u16(static_off))
        p = max(p, static_off + 2)
    else:
        bind['no_static'] = True
    tail = b[off + p:end]
    if tail.rstrip(PAD) != b'' or len(tail) > 3:
        bind['tail'] = tail           # never seen in shipping data
    return bind


def _records(recs):
    """Encode a list of bind records."""
    out = bytearray()
    for r in recs:
        out += struct.pack('<HH', len(r['subs']) | r.get('flags', 0), r['target'])
        for s in r['subs']:
            out += struct.pack('<%dH' % len(s), *s)
            if len(s) == 4:
                pass
    return bytes(out)


def encode_bind(bind):
    """Inverse of decode_bind."""
    head = struct.pack('<HH', bind['slot_count'], len(bind['commands']))
    head += struct.pack('<%dH' % len(bind['commands']), *bind['commands'])
    anim = _records(bind['animated'])
    if bind.get('no_static'):
        blob = head + struct.pack('<HH', 0, len(bind['animated'])) + anim
    else:
        # the static list follows the animated records; its offset is stored first
        static_off = len(head) + 4 + len(anim)
        blob = head + struct.pack('<HH', static_off, len(bind['animated'])) + anim
        blob += struct.pack('<H', len(bind['static'])) + _records(bind['static'])
    return blob + bind.get('tail', b'')      # alignment padding is added by encode()


# --------------------------------------------------------------------- block
def decode(b):
    """Whole ClipDataBlock -> dict(header, binds, commands, pool[, order])."""
    h0, h1 = b[0:4], b[4:8]
    nb, nc, pool = struct.unpack_from('<HHI', b, 8)
    offs = list(struct.unpack_from('<%dI' % (nb + nc), b, 16))
    # each record ends where the next one (or the pool) starts
    bounds = sorted(offs + [pool])
    nxt = lambda o: next(x for x in bounds if x > o)
    blk = {'header': [h0, h1], 'binds': [], 'commands': [], 'pool': b[pool:]}
    for o in offs[:nb]:
        blk['binds'].append(decode_bind(b, o, nxt(o)))
    for o in offs[nb:]:
        c, end = decode_command(b, o)
        if end != nxt(o):
            c['gap'] = b[end:nxt(o)]        # unexpected bytes between records: keep them
        blk['commands'].append(c)
    # the records are usually stored in table order; remember it when not
    order = sorted(range(len(offs)), key=lambda i: offs[i])
    if order != list(range(len(offs))):
        blk['order'] = order
    return blk


def encode(blk):
    """Inverse of decode: lay the records out again and rebuild the offset table."""
    parts = [encode_bind(x) for x in blk['binds']] + \
            [encode_command(c) + c.get('gap', b'') for c in blk['commands']]
    n = len(parts)
    order = blk.get('order', list(range(n)))
    cursor = 16 + 4 * n                     # after the header and offset table
    offs = [0] * n
    body = bytearray()
    nb = len(blk['binds'])
    for i in order:
        if i >= nb and cursor % 4:          # commands are 4-byte aligned; binds are packed
            pad = -cursor % 4; body += PAD * pad; cursor += pad
        offs[i] = cursor; body += parts[i]; cursor += len(parts[i])
    if cursor % 4:                          # the pool is 4-byte aligned
        pad = -cursor % 4; body += PAD * pad; cursor += pad
    out = bytearray(blk['header'][0] + blk['header'][1])
    out += struct.pack('<HHI', len(blk['binds']), len(blk['commands']), cursor)
    out += struct.pack('<%dI' % n, *offs)
    return bytes(out + body + blk['pool'])


# ----------------------------------------------------------- driven keys
OUT_OPS = {   # property -> (category/op word without source bits, operand slot)
    'loc_x': 0x0001, 'loc_y': 0x0002, 'loc_z': 0x0003,
    'scale_x': 0x0006, 'scale_y': 0x0007, 'scale_z': 0x0008, 'value': 0x0009,
}
EULER = {'rot_x': 0, 'rot_y': 1, 'rot_z': 2}           # rotation property -> axis
MAYA_ORDERS = ('xyz', 'yzx', 'zxy', 'xzy', 'yxz', 'zyx')


def _bezier_from_points(points):
    """[(x, y)] -> per-segment [(x0, x1, P0..P3)] with Catmull-Rom style
    tangents (linear when 2 points), i.e. a smooth curve through every point."""
    segs = []
    n = len(points)
    for i in range(n - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        # slope at each end from the neighbouring points (one-sided at the ends)
        m0 = (y1 - y0) if i == 0 else (y1 - points[i - 1][1]) * (x1 - x0) / (x1 - points[i - 1][0])
        m1 = (y1 - y0) if i == n - 2 else (points[i + 2][1] - y0) * (x1 - x0) / (points[i + 2][0] - x0)
        segs.append((x0, x1, [y0, y0 + m0 / 3.0, y1 - m1 / 3.0, y1]))
    return segs


def make_curve(points, linear=None):
    """Driven-key curve through [(input, output), ...] (sorted by input).
    linear=True (default for 2 points) joins the points with straight lines."""
    points = sorted(points)
    if len(points) < 2:
        raise ValueError('need at least two points')
    if linear is None:
        linear = len(points) == 2
    if linear:
        # a straight Bezier: control points at 1/3 and 2/3 along the line
        return [(a[0], b[0], [a[1], a[1] + (b[1] - a[1]) / 3.0, a[1] + 2 * (b[1] - a[1]) / 3.0, b[1]])
                for a, b in zip(points, points[1:])]
    return _bezier_from_points(points)


def _quantize(segs):
    """Bezier segments -> (bias, scale, 16-bit delta keys) as opcodes 0/2 store them."""
    vals = [v for s in segs for v in s[2]]
    lo, hi = min(vals), max(vals)
    bias = (hi + lo) / 2.0                          # centre of the value range
    scale = (hi - lo) / 2.0 / 32767.0 or 1.0        # half range maps to the int16 limit
    keys = []
    for _, _, P in segs:
        # exporter convention: P0 rounded, each delta truncated toward zero
        d = [int(round((P[0] - bias) / scale))]
        d += [int((P[k] - P[k - 1]) / scale) for k in (1, 2, 3)]
        d[0] = max(-32767, min(32767, d[0]))
        if any(abs(x) > 32767 for x in d[1:]):
            raise ValueError('curve too steep for 16-bit keys: a Bezier handle spans more than half the '
                             'value range in one segment - add keys or use make_curve(..., linear=True)')
        keys.append([d])
    return bias, scale, keys


def driven_key(curve, outputs, order='zxy'):
    """Build a ClipDataBlock for a ClipProcessor.

    curve   : make_curve(...) result - output value as a function of the input
              Float (inputs outside the range clamp to the end values).
    outputs : list, one per ClipProcessor Output (same order):
              a property name or list of names for that output -
              'rot_x'/'rot_y'/'rot_z' (radians), 'loc_x'..'loc_z', 'scale_x'..'scale_z',
              or 'value' for a Float output.
    Mirrors the shipping blocks: opcode 2 (f32 key times) for transform
    outputs, opcode 0 for Float outputs, one command with one channel.
    """
    t0, t1 = curve[0][0], curve[-1][1]
    bias, scale, keys = _quantize(curve)
    scalar_only = all((o if isinstance(o, list) else [o]) == ['value'] for o in outputs)
    op = 0 if scalar_only else 2
    if op == 0:
        # 16-bit fractions of the input range
        span = t1 - t0
        times = [int(round((s[0] - t0) / span * 65535.0)) for s in curve]
    else:
        times = [s[0] for s in curve]
    end_value = curve[-1][2][3]
    cmd = {'pre': 0, 'post': 0, 'opcode': op, 't_start': t0, 't_end': t1,
           'params': [[0.0, end_value, bias, scale]], 'times': times, 'keys': keys}
    pool = []
    def const(v):
        # constant-pool slot holding v (shared when repeated)
        if v not in pool:
            pool.append(v)
        return pool.index(v)
    recs = []
    for target, props in enumerate(outputs):
        props = props if isinstance(props, list) else [props]
        subs = []
        rot = [p for p in props if p in EULER]
        if rot:
            # one Euler sub-op: driven axes read the curve (buffer slot 0),
            # the others read a constant 0
            word = 0x1000 | MAYA_ORDERS.index(order) << 6
            operands = []
            for axis in range(3):
                if any(EULER[p] == axis for p in rot):
                    word |= 1 << (4 - 2 * axis); operands.append(0)
                else:
                    operands.append(const(0.0))
            subs.append([word] + operands)
        for p in props:
            if p in OUT_OPS:
                subs.append([OUT_OPS[p] | 0x10, 0])     # 0x10: operand from the buffer
            elif p not in EULER:
                raise KeyError('unknown output property %r' % p)
        recs.append({'target': target, 'flags': 0x20 if props == ['value'] else 0, 'subs': subs})
    bind = {'slot_count': 1, 'commands': [0], 'animated': recs, 'static': []}
    return encode({'header': [struct.pack('<f', t0), struct.pack('<f', t1)],
                   'binds': [bind], 'commands': [cmd],
                   'pool': struct.pack('<%df' % len(pool), *pool)})


def evaluate(block, x):
    """Value of a single-channel driven key at input x (clamped), for checking."""
    c = decode(block)['commands'][0]
    bias, scale = c['params'][0][2], c['params'][0][3]
    span = c['t_end'] - c['t_start']
    ts = [c['t_start'] + span * u / 65535.0 for u in c['times']] if c['opcode'] == 0 else list(c['times'])
    segs = []
    for k, key in enumerate(c['keys']):
        d = key[0]
        P = [d[0], d[0] + d[1], d[0] + d[1] + d[2], d[0] + d[1] + d[2] + d[3]]
        segs.append((ts[k], ts[k + 1] if k + 1 < len(ts) else c['t_end'], [bias + scale * p for p in P]))
    x = min(max(x, segs[0][0]), segs[-1][1])
    for a, b, P in segs:
        if x <= b or (a, b, P) == segs[-1]:
            u = 0.0 if b == a else (x - a) / (b - a)
            return ((1 - u) ** 3 * P[0] + 3 * (1 - u) ** 2 * u * P[1]
                    + 3 * (1 - u) * u * u * P[2] + u ** 3 * P[3])
