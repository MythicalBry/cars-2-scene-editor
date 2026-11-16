"""Evaluate Cars 2 ClipDataBlocks (the animation inside choreographies,
characters' clips and rig driven keys) at arbitrary times.

Block layout and command encodings: see c2clipblock (Cars 2 Toolkit). A
ClipProcessor's bind records target indices into its `Output` list; each
record is a short program of sub-ops writing one transform/scalar:

    category 0x0000  op 0 loc xyz, 1-3 loc x/y/z, 4 quaternion xyzw,
                     5 scale xyz, 6-8 scale x/y/z, 9 scalar value
    category 0x1000  Euler angles (radians), rotation order in bits 6-8
                     (Maya rotateOrder: xyz yzx zxy xzy yxz zyx)
    category 0x2000  quaternion xyz from 3 operands (w derived)

An operand reads the command buffer (all channels of the bind's commands,
in order) when its 2-bit source field is 1, else the constant pool.

Overview of the data:
    commands   animation curves (one or more channels each, Bezier keys)
    binds      which commands drive which targets; each target gets a small
               "program" that assembles location / rotation / scale / value
               from the current curve values ("buffer") and constants ("pool")
Used by the choreography preview to bake animation into Blender keyframes.
"""
import math
import struct

import c2clipblock as cb

# Maya's rotateOrder enum -> Blender rotation-mode letters
MAYA_ORDERS = ('XYZ', 'YZX', 'ZXY', 'XZY', 'YXZ', 'ZYX')
# category-0 sub-op number -> (property written, which components it fills)
FIELD_OPS = {
    0: ('loc', (0, 1, 2)), 1: ('loc', (0,)), 2: ('loc', (1,)), 3: ('loc', (2,)),
    4: ('quat', (0, 1, 2, 3)),
    5: ('scale', (0, 1, 2)), 6: ('scale', (0,)), 7: ('scale', (1,)), 8: ('scale', (2,)),
    9: ('value', (0,)),
}


class Curve:
    """One command channel as Bezier segments [(t0, t1, P0..P3)]."""

    def __init__(self, cmd, ch):
        op = cmd['opcode']
        t0, t1 = cmd['t_start'], cmd['t_end']
        span = t1 - t0
        # quantised opcodes (0, 2) store values relative to a per-channel
        # bias and scale; float opcodes store real values
        if op in (0, 2):
            bias, scale = cmd['params'][ch][2], cmd['params'][ch][3]
        else:
            bias, scale = 0.0, 1.0
        times = cmd['times']
        if op in (0, 4):
            # key times are 16-bit fractions of the command's time span
            times = [t0 + span * u / 65535.0 for u in times]
        segs = []
        if op == -1:   # undecoded command: hold its end value
            v = cmd['params'][ch][1]
            segs.append((t0, t1, [v, v, v, v]))
        for k, key in enumerate(cmd['keys']):
            d = key[ch]
            # the four Bezier control points are stored as running deltas:
            # P0 = d0, P1 = d0+d1, P2 = d0+d1+d2, P3 = d0+d1+d2+d3
            p = [d[0], d[0] + d[1], d[0] + d[1] + d[2], d[0] + d[1] + d[2] + d[3]] if op in (0, 2) else list(d)
            if op not in (0, 2):
                # opcodes 4/6 store f32 Bezier deltas too
                p = [d[0], d[0] + d[1], d[0] + d[1] + d[2], d[0] + d[1] + d[2] + d[3]]
            a = times[k]
            b = times[k + 1] if k + 1 < len(times) else t1     # the last segment runs to the end
            segs.append((a, b, [bias + scale * x for x in p]))
        self.segs = segs
        self.t0, self.t1 = t0, t1

    def __call__(self, t):
        """Value at time t (held flat before the first and after the last key)."""
        s = self.segs
        if not s:
            return 0.0
        if t <= s[0][0]:
            return s[0][2][0]
        if t >= s[-1][1]:
            return s[-1][2][3]
        # binary search for the segment containing t
        lo, hi = 0, len(s) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if s[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        a, b, p = s[lo]
        u = 0.0 if b == a else min(1.0, max(0.0, (t - a) / (b - a)))
        v = 1.0 - u
        # cubic Bezier: (1-u)^3 P0 + 3(1-u)^2 u P1 + 3(1-u) u^2 P2 + u^3 P3
        return v * v * v * p[0] + 3 * v * v * u * p[1] + 3 * v * u * u * p[2] + u * u * u * p[3]


class Clip:
    """A decoded ClipDataBlock. evaluate(t) -> {target: {prop: value}}."""

    def __init__(self, block):
        self.warnings = []
        self.blk = self._decode(block)
        # header: two floats - start time and duration
        self.start = struct.unpack('<f', self.blk['header'][0])[0]
        self.duration = struct.unpack('<f', self.blk['header'][1])[0]
        pool = self.blk['pool']
        # the constant pool is a flat array of floats
        self.pool = list(struct.unpack('<%df' % (len(pool) // 4), pool[:len(pool) // 4 * 4]))
        self.binds = []
        for bind in self.blk['binds']:
            # one Curve per channel of each command the bind uses, in order:
            # this list is the "command buffer" that sub-op operands index
            curves = []
            for ci in bind['commands']:
                cmd = self.blk['commands'][ci]
                for ch in range(len(cmd['params'])):
                    curves.append(Curve(cmd, ch))
            self.binds.append((bind, curves))
        ends = [c.t1 for _, cs in self.binds for c in cs]
        self.end = max(ends + [self.duration, self.start])

    def _decode(self, b):
        """Like c2clipblock.decode, but a command with an unknown opcode
        becomes a constant channel at its stored end value instead of
        failing the whole clip (opcode 1: one channel in all shipping data)."""
        # block header: counts of binds and commands, offset of the pool
        nb, nc, pool = struct.unpack_from('<HHI', b, 8)
        offs = list(struct.unpack_from('<%dI' % (nb + nc), b, 16))
        # each record ends where the next one (or the pool) starts
        bounds = sorted(offs + [pool])
        nxt = lambda o: next(x for x in bounds if x > o)
        blk = {'header': [b[0:4], b[4:8]], 'binds': [], 'commands': [], 'pool': b[pool:]}
        for o in offs[:nb]:
            blk['binds'].append(cb.decode_bind(b, o, nxt(o)))
        for o in offs[nb:]:
            try:
                c, _ = cb.decode_command(b, o)
            except ValueError:
                # read just enough of the unknown command (channel count, time
                # span, per-channel end values) to hold it constant
                w = struct.unpack_from('<I', b, o)[0]
                n_ch = (w >> 10) & 0x3FF
                t0, t1 = struct.unpack_from('<ff', b, o + 4)
                params = [[0.0, struct.unpack_from('<f', b, o + 12 + 16 * ch + 4)[0], 0.0, 1.0] for ch in range(n_ch)]
                c = {'opcode': -1, 't_start': t0, 't_end': t1, 'params': params, 'times': [], 'keys': []}
                self.warnings.append('clip command opcode %d held constant' % ((w >> 6) & 0xF))
            blk['commands'].append(c)
        return blk

    def _program(self, rec, read):
        """Run one target's sub-op program -> {'loc'/'quat'/'euler'/'scale'/'value': ...}.
        `read(word, operand_slot, operand, force_buffer)` fetches an operand."""
        out = {}
        for sub in rec['subs']:
            w, ops = sub[0], sub[1:]        # control word, then operands
            cat = w & 0xF000                # category (see module docstring)
            vals = [read(w, k, o) for k, o in enumerate(ops)]
            if cat == 0x1000:
                # Euler rotation + Maya rotation order
                out['euler'] = vals[:3]
                out['order'] = MAYA_ORDERS[(w >> 6) & 7] if ((w >> 6) & 7) < 6 else 'ZXY'
            elif cat == 0x2000:
                # unit quaternion stored as xyz; w = sqrt(1 - x^2 - y^2 - z^2)
                x, y, z = vals[:3]
                out['quat'] = [x, y, z, math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))]
            elif cat in (0x0000, 0x3000):
                f = FIELD_OPS.get(w & 0xF)
                if f is None:
                    continue
                prop, comps = f
                # consecutive components come from consecutive operands
                # starting at the first one, all from the same source
                base = ops[0]
                src_buf = ((w >> 4) & 3) == 1
                cur = out.setdefault(prop, [None] * (4 if prop == 'quat' else 3 if prop != 'value' else 1))
                for i, c in enumerate(comps):
                    cur[c] = read(w, 0, base + i, src_buf)
        return out

    def evaluate(self, t, include_static=True):
        """Every target's values at time t. Static records (constant
        transforms) are included unless include_static is False."""
        result = {}
        for bind, curves in self.binds:
            buf = [c(t) for c in curves]        # current value of every channel

            def read(w, k, operand, force=None):
                # operand slot k has a 2-bit source field in the control word:
                # 1 = command buffer, anything else = constant pool
                from_buf = force if force is not None else ((w >> (4 - 2 * k)) & 3) == 1
                if from_buf:
                    return buf[operand] if operand < len(buf) else 0.0
                return self.pool[operand] if operand < len(self.pool) else 0.0
            recs = bind['animated'] + (bind['static'] if include_static else [])
            for rec in recs:
                r = self._program(rec, read)
                result.setdefault(rec['target'], {}).update(r)
        return result

    def animated_targets(self):
        """Indices of the targets that actually move."""
        return sorted({rec['target'] for bind, _ in self.binds for rec in bind['animated']})
