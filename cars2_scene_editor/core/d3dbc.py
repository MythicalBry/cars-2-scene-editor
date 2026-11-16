"""Direct3D 9 shader bytecode (vs_3_0 / ps_3_0) decoder.

The Arcade build's effects (.tfx) carry compiled D3D9 shaders. This reads
them without d3dcompiler: instructions, declarations, `def` literals and the
constant table (CTAB: parameter name -> register).

Bytecode basics (all 32-bit little-endian "tokens"):
    token 0          version: 0xFFFF0300 = ps_3_0, 0xFFFE0300 = vs_3_0
    instruction      opcode in the low 16 bits, operand count in bits 24-27,
                     followed by that many operand tokens
    comment 0xFFFE   a block of non-code data; the CTAB block lives here
    0x0000FFFF       end of program
Registers: r# temporaries, v# inputs, c# float constants, s# samplers,
o#/oC# outputs, i#/b# integer/bool constants (see Reg.__repr__).
The result (Program) is consumed by fxgraph.py, which executes it symbolically.
"""
import struct

# register types (the 5-bit type field of a register token)
TEMP, INPUT, CONST, ADDR, RASTOUT, ATTROUT, OUTPUT, CONSTINT, COLOROUT, DEPTHOUT, SAMPLER = range(11)
CONSTBOOL, LOOP, TEMPFLOAT16, MISCTYPE, LABEL, PREDICATE = 14, 15, 16, 17, 18, 19

# opcode number -> mnemonic (D3D9 instruction set)
OPCODES = {
    0: 'nop', 1: 'mov', 2: 'add', 3: 'sub', 4: 'mad', 5: 'mul', 6: 'rcp', 7: 'rsq', 8: 'dp3', 9: 'dp4',
    10: 'min', 11: 'max', 12: 'slt', 13: 'sge', 14: 'exp', 15: 'log', 16: 'lit', 17: 'dst', 18: 'lrp',
    19: 'frc', 20: 'm4x4', 21: 'm4x3', 22: 'm3x4', 23: 'm3x3', 24: 'm3x2', 25: 'call', 26: 'callnz',
    27: 'loop', 28: 'ret', 29: 'endloop', 30: 'label', 31: 'dcl', 32: 'pow', 33: 'crs', 34: 'sgn',
    35: 'abs', 36: 'nrm', 37: 'sincos', 38: 'rep', 39: 'endrep', 40: 'if', 41: 'ifc', 42: 'else',
    43: 'endif', 44: 'break', 45: 'breakc', 46: 'mova', 47: 'defb', 48: 'defi', 64: 'texcoord',
    65: 'texkill', 66: 'texld', 81: 'def', 88: 'cmp', 89: 'bem', 90: 'dp2add', 91: 'dsx', 92: 'dsy', 93: 'texldd',
    94: 'setp', 95: 'texldl', 96: 'breakp',
}
# usage ids of dcl
USAGES = {0: 'POSITION', 1: 'BLENDWEIGHT', 2: 'BLENDINDICES', 3: 'NORMAL', 4: 'PSIZE', 5: 'TEXCOORD',
          6: 'TANGENT', 7: 'BINORMAL', 8: 'TESSFACTOR', 9: 'POSITIONT', 10: 'COLOR', 11: 'FOG', 12: 'DEPTH',
          13: 'SAMPLE'}
SAMPLER_TYPES = {2: '2d', 3: 'cube', 4: 'volume'}
# comparison codes of ifc / breakc / setp
COMPARISONS = {1: 'gt', 2: 'eq', 3: 'ge', 4: 'lt', 5: 'ne', 6: 'le'}
# source modifiers
SRC_MODS = {0: '', 1: 'neg', 2: 'bias', 3: 'biasneg', 4: 'sign', 5: 'signneg', 6: 'comp', 7: 'x2',
            8: 'x2neg', 9: 'dz', 10: 'dw', 11: 'abs', 12: 'absneg', 13: 'not'}
# CTAB register sets
CTAB_SETS = {0: 'bool', 1: 'int4', 2: 'float4', 3: 'sampler'}


class Reg:
    """One operand: register type + number, plus
    swizzle (source component order, e.g. .xxyz), mask (destination components
    written), mod (source modifier like neg/abs), sat (clamp result to 0..1),
    rel (relative addressing, e.g. c[a0.x + 5])."""
    __slots__ = ('type', 'num', 'swizzle', 'mask', 'mod', 'sat', 'rel')

    def __init__(self, typ, num, swizzle=(0, 1, 2, 3), mask=(0, 1, 2, 3), mod='', sat=False, rel=None):
        self.type, self.num, self.swizzle, self.mask, self.mod, self.sat, self.rel = typ, num, swizzle, mask, mod, sat, rel

    def __repr__(self):
        n = {TEMP: 'r', INPUT: 'v', CONST: 'c', SAMPLER: 's', OUTPUT: 'o', COLOROUT: 'oC', ADDR: 'a',
             CONSTINT: 'i', CONSTBOOL: 'b', MISCTYPE: 'misc', PREDICATE: 'p', LOOP: 'aL'}.get(self.type, 't%d' % self.type)
        return '%s%s%d' % (self.mod + ' ' if self.mod else '', n, self.num)


class Instr:
    """One instruction: mnemonic, control bits (comparison for ifc etc.),
    destination register (or None) and source registers."""
    __slots__ = ('op', 'control', 'dst', 'src', 'extra')

    def __init__(self, op, control, dst, src, extra=None):
        self.op, self.control, self.dst, self.src, self.extra = op, control, dst, src, extra

    def __repr__(self):
        return '%s%s %s %s' % (self.op, '_sat' if self.dst is not None and self.dst.sat else '', self.dst, self.src)


class Program:
    """A decoded shader."""
    def __init__(self):
        self.kind = None          # 'vs' / 'ps'
        self.version = (3, 0)
        self.instructions = []
        self.defs = {}            # const reg -> 4 floats
        self.defi = {}            # int const reg -> 4 ints
        self.defb = {}            # bool const reg -> bool
        self.inputs = {}          # input reg -> (usage, index)
        self.outputs = {}         # output reg -> (usage, index)  (vs_3_0)
        self.samplers = {}        # sampler reg -> type
        self.constants = []       # CTAB entries: dict(name, set, reg, count, rows, cols)


def _reg_type(tok):
    """The register type is split over two bit fields of the token."""
    return ((tok >> 28) & 7) | ((tok >> 8) & 0x18)


def _dst(tok):
    """Decode a destination-register token (number, write mask, _sat, relative)."""
    return Reg(_reg_type(tok), tok & 0x7FF, mask=tuple(i for i in range(4) if tok & (1 << (16 + i))),
               sat=bool((tok >> 20) & 1), rel=bool(tok & 0x2000))


def _src(tok):
    """Decode a source-register token (number, swizzle, modifier, relative)."""
    sw = (tok >> 16) & 0xFF                  # 4 x 2-bit component selectors
    return Reg(_reg_type(tok), tok & 0x7FF, swizzle=tuple((sw >> (2 * i)) & 3 for i in range(4)),
               mod=SRC_MODS.get((tok >> 24) & 0xF, '?'), rel=bool(tok & 0x2000))


def _ctab(prog, data):
    """Read the constant table: which named parameter (e.g. 'diffuseColorA1',
    'samDiffuseMapA1') lives in which register(s)."""
    if len(data) < 28:
        return
    size, creator, version, count, info_off, flags, target = struct.unpack_from('<7I', data, 0)

    def cstr(off):
        # zero-terminated string at an offset inside the table
        return data[off:data.index(b'\0', off)].decode('latin1')
    for k in range(count):
        # one 20-byte entry per constant, plus a type-info record it points to
        name_off, rset, ridx, rcount, _res, type_off, def_off = struct.unpack_from('<IHHHHII', data, info_off + 20 * k)
        cls, typ, rows, cols, elems, members, member_off = struct.unpack_from('<HHHHHHI', data, type_off)
        prog.constants.append({'name': cstr(name_off), 'set': CTAB_SETS.get(rset, rset), 'reg': ridx,
                               'count': rcount, 'rows': rows, 'cols': cols, 'elements': elems, 'class': cls})


def decode(code):
    """bytes -> Program"""
    toks = struct.unpack('<%dI' % (len(code) // 4), code[:len(code) // 4 * 4])
    prog = Program()
    ver = toks[0]
    prog.kind = 'ps' if (ver >> 16) == 0xFFFF else 'vs'
    prog.version = ((ver >> 8) & 0xFF, ver & 0xFF)
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == 0x0000FFFF:
            break                                     # end token
        op = t & 0xFFFF
        if op == 0xFFFE:      # comment
            n = (t >> 16) & 0x7FFF                    # comment length in tokens
            blob = struct.pack('<%dI' % n, *toks[i + 1:i + 1 + n])
            if blob[:4] == b'CTAB':
                _ctab(prog, blob[4:])
            i += 1 + n
            continue
        length = (t >> 24) & 0xF                      # number of operand tokens
        control = (t >> 16) & 0xFF
        args = toks[i + 1:i + 1 + length]
        i += 1 + length
        name = OPCODES.get(op, 'op%d' % op)
        if name == 'dcl':
            # declaration: what an input/output/sampler register means
            usage_tok, reg_tok = args[0], args[1]
            r = _dst(reg_tok)
            if r.type == SAMPLER:
                prog.samplers[r.num] = SAMPLER_TYPES.get((usage_tok >> 27) & 0xF, '2d')
            elif r.type in (INPUT, OUTPUT):
                usage = USAGES.get(usage_tok & 0x1F, 'U%d' % (usage_tok & 0x1F))
                (prog.inputs if r.type == INPUT else prog.outputs)[r.num] = (usage, (usage_tok >> 16) & 0xF, r.mask)
            elif r.type == MISCTYPE:
                # misc 0 = screen position (VPOS), misc 1 = front/back face (VFACE)
                prog.inputs[('misc', r.num)] = ('VFACE' if r.num == 1 else 'VPOS', 0, r.mask)
            continue
        if name == 'def':
            # literal float constant baked into the shader
            r = _dst(args[0])
            prog.defs[r.num] = struct.unpack('<4f', struct.pack('<4I', *args[1:5]))
            continue
        if name == 'defi':
            prog.defi[_dst(args[0]).num] = struct.unpack('<4i', struct.pack('<4I', *args[1:5]))
            continue
        if name == 'defb':
            prog.defb[_dst(args[0]).num] = bool(args[1])
            continue
        # flow-control instructions have only source operands
        no_dst = name in ('if', 'ifc', 'else', 'endif', 'rep', 'endrep', 'loop', 'endloop', 'break', 'breakc',
                          'call', 'callnz', 'ret', 'label', 'texkill', 'breakp')
        dst = None
        srcs = []
        k = 0
        if name == 'texkill':
            dst = _dst(args[0])
            k = 1
        elif not no_dst and args:
            dst = _dst(args[0])
            k = 1
            if dst.rel:
                k += 1                                 # skip the relative-address token
        while k < len(args):
            s = _src(args[k])
            k += 1
            if s.rel and prog.version >= (2, 0):
                # shader model 2+: the address register follows as its own token
                s.rel = _src(args[k])
                k += 1
            srcs.append(s)
        prog.instructions.append(Instr(name, control, dst, srcs))
    return prog


def constant_map(prog):
    """{(set, register): (name, element offset)} for every register a CTAB entry covers."""
    out = {}
    for c in prog.constants:
        # a matrix or array spans several consecutive registers
        for k in range(c['count']):
            out[(c['set'], c['reg'] + k)] = (c['name'], k)
    return out
