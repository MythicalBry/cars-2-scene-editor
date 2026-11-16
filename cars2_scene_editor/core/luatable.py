"""Read the game's data tables: compiled Lua 5.1 chunks (assets/luatables/
luatables.zip, e.g. crowdmanagerinfo.lua) that only build tables of
constants and assign them to globals.

Not a Lua VM: the main function's instructions are replayed for the opcodes
such a chunk uses (LOADK, LOADNIL, LOADBOOL, MOVE, NEWTABLE, SETTABLE,
SETLIST, SETGLOBAL, GETGLOBAL, RETURN). Either byte order (the header says).
"""
import struct

# Lua 5.1 opcodes (lopcodes.h)
MOVE, LOADK, LOADBOOL, LOADNIL, GETUPVAL, GETGLOBAL, GETTABLE, SETGLOBAL, SETUPVAL, SETTABLE, NEWTABLE = range(11)
SETLIST, RETURN = 34, 30
FIELDS_PER_FLUSH = 50


class _Reader:
    def __init__(self, d):
        if d[:4] != b'\x1bLua' or d[4] != 0x51:
            raise ValueError('not a Lua 5.1 chunk')
        self.d = d
        self.e = '<' if d[6] == 1 else '>'
        self.int_size, self.size_t, self.instr, self.num = d[7], d[8], d[9], d[10]
        self.integral = d[11]
        self.o = 12

    def _u(self, size):
        v = int.from_bytes(self.d[self.o:self.o + size], 'little' if self.e == '<' else 'big')
        self.o += size
        return v

    def int_(self):
        return self._u(self.int_size)

    def string(self):
        n = self._u(self.size_t)
        s = self.d[self.o:self.o + n]
        self.o += n
        return s[:-1].decode('latin1') if n else None

    def number(self):
        if self.integral:
            v = self._u(self.num)
            return v
        v = struct.unpack_from(self.e + ('d' if self.num == 8 else 'f'), self.d, self.o)[0]
        self.o += self.num
        return v

    def function(self):
        self.string()                       # source
        self.int_(); self.int_()            # line defined, last line
        self.o += 4                         # nups, numparams, is_vararg, maxstacksize
        code = [self._u(self.instr) for _ in range(self.int_())]
        consts = []
        for _ in range(self.int_()):
            t = self.d[self.o]
            self.o += 1
            if t == 0:
                consts.append(None)
            elif t == 1:
                consts.append(bool(self.d[self.o]))
                self.o += 1
            elif t == 3:
                consts.append(self.number())
            elif t == 4:
                consts.append(self.string())
            else:
                raise ValueError('constant type %d' % t)
        protos = [self.function() for _ in range(self.int_())]
        # debug info: line info, locals, upvalue names
        self.o += self.int_() * self.int_size
        for _ in range(self.int_()):
            self.string(); self.int_(); self.int_()
        for _ in range(self.int_()):
            self.string()
        return code, consts, protos


def load(data):
    """{global name: value} the chunk's main function assigns. Lua tables
    become dicts (array parts keyed 1, 2, ...)."""
    code, k, _protos = _Reader(data).function()
    reg = {}
    out = {}

    def rk(x):
        return k[x - 256] if x >= 256 else reg.get(x)
    for ins in code:
        op = ins & 0x3F
        a = (ins >> 6) & 0xFF
        c = (ins >> 14) & 0x1FF
        b = (ins >> 23) & 0x1FF
        bx = ins >> 14
        if op == LOADK:
            reg[a] = k[bx]
        elif op == LOADNIL:
            for r in range(a, b + 1):
                reg[r] = None
        elif op == LOADBOOL:
            reg[a] = bool(b)
        elif op == MOVE:
            reg[a] = reg.get(b)
        elif op == NEWTABLE:
            reg[a] = {}
        elif op == SETTABLE:
            if isinstance(reg.get(a), dict):
                reg[a][rk(b)] = rk(c)
        elif op == SETLIST:
            t = reg.get(a)
            base = ((c - 1) * FIELDS_PER_FLUSH) if c else 0
            for i in range(1, b + 1):
                t[base + i] = reg.get(a + i)
        elif op == SETGLOBAL:
            out[k[bx]] = reg.get(a)
        elif op == GETGLOBAL:
            reg[a] = out.get(k[bx])
        elif op == RETURN:
            break
    return out


def as_list(t):
    """A Lua array table (keys 1..n) as a list."""
    if not isinstance(t, dict):
        return []
    return [t[i] for i in range(1, len(t) + 1) if i in t]
