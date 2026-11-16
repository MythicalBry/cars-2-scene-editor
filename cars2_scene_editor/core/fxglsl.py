"""Direct3D 9 shader bytecode (vs_3_0 / ps_3_0) -> GLSL for Blender's gpu module.

The Game Render engine (gamerender.py) draws the scene with the game's own
shaders. Every instruction of the shipped bytecode becomes one GLSL statement
on vec4 "registers", so the result computes exactly what the game computes
(no translation into nodes, no approximations of the maths).

Register mapping:
    r#      vec4 temporaries               (locals)
    v#      vertex attributes (VS) / varyings from the VS (PS), matched by
            D3D usage + index (TEXCOORD3 -> TEXCOORD3) like the hardware does
    c#      `def` literals -> GLSL constants;
            material / instance parameters -> the per-draw uniform block
            (drw.vc[reg] / drw.pc[reg], indexed by the original register, so
            relative addressing c[a0.x + n] keeps working);
            engine globals (camera, sun, fog, time ...) -> the shared engine
            block eng.e[slot], laid out by EngineLayout
    s#      samplers (2D / cube / 3D)
    o#/oC0  outputs -> varyings / the fragment colour

Conventions handled here (the rest is the renderer's job):
  * D3D clip space has z in 0..1: gl_Position.z = 2z - w.
  * D3D textures have v = 0 at the top; Blender's GPU textures at the bottom:
    2D lookups use (u, 1 - v). The renderer uploads UVs in the game's
    convention (v down), so the shader sees exactly the game's numbers.
  * Textures Blender decodes differently from the game (sRGB or not) are
    converted after the lookup by drw.tmode[sampler] (1: sRGB -> linear,
    2: linear -> sRGB).
  * Alpha test (a fixed-function state in D3D9) is done at the end of the
    pixel shader from drw.misc (x: enabled, y: reference 0..1, z: function).
"""
import d3dbc

ENGINE_MAX = 512        # vec4 slots of the engine block
VS_REGS = 256           # vs_3_0 float constants
PS_REGS = 224           # ps_3_0 float constants

# vertex elements whose values are fixed: Blender's armature already deforms
# the mesh, so skinned shaders get all weight on bone 0 and an identity palette
CONST_INPUTS = {'BlendWeights': 'vec4(1.0, 0.0, 0.0, 0.0)', 'BoneIndices': 'vec4(0.0)'}
# float components of each vertex element as uploaded by the renderer
ELEMENT_SIZES = {'Position': 3, 'Normal': 3}

# D3DCMP values of the AlphaFunc render state, by name
ALPHA_FUNCS = {'NEVER': 1, 'LESS': 2, 'EQUAL': 3, 'LESSEQUAL': 4, 'GREATER': 5, 'NOTEQUAL': 6,
               'GREATEREQUAL': 7, 'ALWAYS': 8}

# GLSL shared by both stages: the uniform blocks and helper functions
TYPEDEFS = '''
struct C2Draw {
  vec4 vc[%d];
  vec4 pc[%d];
  vec4 vb[16];
  vec4 pb[16];
  vec4 vi[16];
  vec4 pi[16];
  vec4 tmode[20];
  vec4 misc;
};
struct C2Engine {
  vec4 e[%d];
};
''' % (VS_REGS, PS_REGS, ENGINE_MAX)

HELPERS = '''
float c2_pow(float a, float b) { return exp2(b * log2(abs(a))); }
vec4 c2_lit(vec4 s) {
  return vec4(1.0, max(s.x, 0.0), s.x > 0.0 ? exp2(clamp(s.w, -128.0, 128.0) * log2(max(s.y, 0.0))) : 0.0, 1.0);
}
vec3 c2_s2l(vec3 c) {
  return mix(c / 12.92, pow(max((c + 0.055) / 1.055, vec3(0.0)), vec3(2.4)), step(vec3(0.04045), c));
}
vec3 c2_l2s(vec3 c) {
  return mix(c * 12.92, 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}
vec2 c2_equirect(vec3 d) {
  // cube-map direction -> the equirectangular image made from the cube DDS
  // (core/cubemap.py equirect; v from the bottom like every Blender image)
  return vec2(atan(d.x, -d.z) * 0.15915494 + 0.5, atan(d.y, length(d.xz)) * 0.31830989 + 0.5);
}
vec4 c2_conv(vec4 t, float mode) {
  if (mode == 1.0) return vec4(c2_s2l(t.rgb), t.a);
  if (mode == 2.0) return vec4(c2_l2s(t.rgb), t.a);
  return t;
}
'''


# pixel shaders only (screen derivatives): a seamless lookup across the u wrap
HELPERS_PS = '''
vec4 c2_cube(sampler2D s, vec3 d) {
  vec2 uv = c2_equirect(d);
  vec2 dx = dFdx(uv), dy = dFdy(uv);
  if (abs(dx.x) > 0.5) dx.x -= sign(dx.x);
  if (abs(dy.x) > 0.5) dy.x -= sign(dy.x);
  return textureGrad(s, uv, dx, dy);
}
'''


class Unsupported(Exception):
    """The shader uses something this converter can't express (the renderer
    then draws the material with its fallback shader)."""
    pass


class EngineLayout:
    """Where each engine global lives in the shared engine block. One layout
    serves every shader of a session; a name keeps its slots once given (a
    later shader declaring it with more registers gets a second, larger region
    and both are filled)."""

    def __init__(self):
        self.entries = {}      # name -> [(base, count, matrix class)]
        self.used = 0

    def slot(self, name, count, cls):
        """Base slot of an engine global spanning `count` registers."""
        for base, n, _c in self.entries.get(name, ()):
            if n >= count:
                return base
        if self.used + count > ENGINE_MAX:
            raise Unsupported('engine block full')
        self.entries.setdefault(name, []).append((self.used, count, cls))
        self.used += count
        return self.used - count

    def regions(self):
        """[(name, (base, count, class))] of every allocated region."""
        return [(n, r) for n, rs in self.entries.items() for r in rs]


def _swz(r):
    """GLSL swizzle of a source operand ('' for .xyzw)."""
    s = ''.join('xyzw'[i] for i in r.swizzle)
    return '' if s == 'xyzw' else '.' + s


def _mask(r):
    """GLSL write mask of a destination operand."""
    return ''.join('xyzw'[i] for i in r.mask)


def _cmp(code, a, b):
    """GLSL comparison of ifc / breakc (control code)."""
    op = {'gt': '>', 'eq': '==', 'ge': '>=', 'lt': '<', 'ne': '!=', 'le': '<='}[d3dbc.COMPARISONS.get(code, 'ne')]
    return '(%s %s %s)' % (a, op, b)


def _flt(x):
    """A float literal GLSL accepts (inf / nan written as expressions)."""
    if x != x:
        return '(0.0 / 0.0)'
    if x in (float('inf'), float('-inf')):
        return '(%s1.0 / 0.0)' % ('-' if x < 0 else '')
    s = repr(float(x))
    return s if ('.' in s or 'e' in s) else s + '.0'


class _Stage:
    """GLSL for one decoded program (vs or ps)."""

    def __init__(self, prog, conv, stage):
        self.p = prog
        self.conv = conv
        self.stage = stage                   # 'vs' / 'ps'
        self.cmap = d3dbc.constant_map(prog)
        self.entries = {}                    # CTAB name -> entry dict
        for c in prog.constants:
            self.entries.setdefault(c['name'], c)
        self.temps = set()
        self.outs = set()
        self.loops = 0                       # open loop depth (aL)
        self.max_loops = 0
        self.uses_a0 = False
        self.lines = []
        self.indent = 1
        self.samplers = {}                   # reg -> GLSL name
        self.relative = set()

    # -- operands ---------------------------------------------------------------------
    def const(self, num, rel=None):
        """GLSL expression of float constant register c<num> (relative: rel is
        the address operand)."""
        p = self.p
        idx = ''
        if rel is not None and rel is not False:
            if rel.type == d3dbc.ADDR:
                self.uses_a0 = True
                idx = ' + a0.%s' % 'xyzw'[rel.swizzle[0]]
            elif rel.type == d3dbc.LOOP:
                if not self.loops:
                    raise Unsupported('aL outside a loop')
                idx = ' + aL%d' % (self.loops - 1)
            else:
                raise Unsupported('relative register type %d' % rel.type)
        if not idx and num in p.defs:
            return 'c%d' % num
        ent = self.cmap.get(('float4', num))
        if ent is None:
            if idx:
                raise Unsupported('relative addressing outside a constant array')
            return 'vec4(0.0)'
        name, el = ent
        c = self.entries[name]
        if idx:
            self.relative.add(name)
        if self.conv.is_draw(name):
            self.conv.draw_entries.add((self.stage, name, c['reg'], c['count'], c['class'], 'float4'))
            arr = 'vc' if self.stage == 'vs' else 'pc'
            return 'drw.%s[%d%s]' % (arr, num, idx)
        base = self.conv.layout.slot(name, c['count'], c['class'])
        self.conv.engine_names.add(name)
        return 'eng.e[%d%s]' % (base + num - c['reg'], idx)

    def src(self, r):
        """vec4 GLSL expression of a source operand (swizzle + modifier)."""
        t = r.type
        if t == d3dbc.TEMP:
            self.temps.add(r.num)
            e = 'r%d' % r.num
        elif t == d3dbc.INPUT:
            if r.rel:
                raise Unsupported('relative input')
            e = 'v%d' % r.num
        elif t == d3dbc.CONST:
            e = self.const(r.num, r.rel)
        elif t == d3dbc.CONSTBOOL:
            e = self.boolean(r.num)
            e = 'vec4(%s ? 1.0 : 0.0)' % e
        elif t == d3dbc.CONSTINT:
            e = 'vec4(%s)' % self.integer(r.num)
        elif t == d3dbc.MISCTYPE:
            e = 'misc%d' % r.num
        elif t == d3dbc.LOOP:
            e = 'vec4(float(aL%d))' % (self.loops - 1)
        else:
            raise Unsupported('source register type %d' % t)
        e += _swz(r)
        m = r.mod
        if m == 'neg':
            e = '(-%s)' % e
        elif m == 'abs':
            e = 'abs(%s)' % e
        elif m == 'absneg':
            e = '(-abs(%s))' % e
        elif m:
            raise Unsupported('source modifier %s' % m)
        return e

    def boolean(self, num):
        """GLSL bool expression of b<num>."""
        if num in self.p.defb:
            return 'true' if self.p.defb[num] else 'false'
        ent = self.cmap.get(('bool', num))
        if ent is None:
            return 'false'
        name = ent[0]
        c = self.entries[name]
        self.conv.draw_entries.add((self.stage, name, c['reg'], c['count'], c['class'], 'bool'))
        return '(drw.%s[%d].x != 0.0)' % ('vb' if self.stage == 'vs' else 'pb', num)

    def integer(self, num):
        """GLSL ivec4 expression of i<num>."""
        if num in self.p.defi:
            return 'ivec4(%s)' % ', '.join(str(int(x)) for x in self.p.defi[num])
        ent = self.cmap.get(('int4', num))
        if ent is None:
            return 'ivec4(0)'
        name = ent[0]
        c = self.entries[name]
        self.conv.draw_entries.add((self.stage, name, c['reg'], c['count'], c['class'], 'int4'))
        return 'ivec4(drw.%s[%d])' % ('vi' if self.stage == 'vs' else 'pi', num)

    def dst(self, d):
        """GLSL lvalue of a destination register."""
        t = d.type
        if t == d3dbc.TEMP:
            self.temps.add(d.num)
            return 'r%d' % d.num
        if t == d3dbc.OUTPUT:
            self.outs.add(d.num)
            return 'o%d' % d.num
        if t == d3dbc.COLOROUT:
            if d.num != 0:
                return None                  # extra render targets are not drawn
            return 'oC0'
        if t == d3dbc.RASTOUT:
            return 'oPos' if d.num == 0 else None
        if t == d3dbc.DEPTHOUT:
            return None                      # depth output: not used here
        raise Unsupported('destination register type %d' % t)

    # -- emission ---------------------------------------------------------------------
    def emit(self, s):
        self.lines.append('  ' * self.indent + s)

    def write(self, d, expr):
        """dst.mask = expr.mask (expr is a vec4 expression), with _sat."""
        lv = self.dst(d)
        if lv is None:
            return
        if d.sat:
            expr = 'clamp(%s, 0.0, 1.0)' % expr
        m = _mask(d)
        if m == 'xyzw':
            self.emit('%s = %s;' % (lv, expr))
        else:
            self.emit('%s.%s = (%s).%s;' % (lv, m, expr, m))

    def sampler(self, r):
        """GLSL name of sampler register s<num> (declared for the shader)."""
        name = ('%ss%d' % ('v' if self.stage == 'vs' else '', r.num))
        self.samplers[r.num] = name
        return name

    def texture(self, ins):
        """GLSL expression of a texld / texldl / texldd / texldp / texldb."""
        coord = self.src(ins.src[0])
        sr = ins.src[1]
        name = self.sampler(sr)
        typ = self.p.samplers.get(sr.num, '2d')
        op = ins.op
        if typ == 'cube':
            # Blender's Python GPU API uploads only one cube face, so cube maps
            # are bound as their equirectangular image
            if op == 'texldl':
                look = 'textureLod(%s, c2_equirect(%s.xyz), %s.w)' % (name, coord, coord)
            elif self.stage == 'vs' or op == 'texldd':
                look = 'textureLod(%s, c2_equirect(%s.xyz), 0.0)' % (name, coord)
            else:
                look = 'c2_cube(%s, %s.xyz)' % (name, coord)
            idx = sr.num if self.stage == 'ps' else 16 + sr.num
            return 'c2_conv(%s, drw.tmode[%d].x)%s' % (look, idx, _swz(sr))
        if typ == '2d':
            c = 'vec2(%s.x, 1.0 - %s.y)'
            if op == 'texld' and ins.control == 1:          # texldp: projected
                c = 'vec2(%s.x / %s.w, 1.0 - %s.y / %s.w)' % (coord, coord, coord, coord)
            else:
                c = c % (coord, coord)
        else:
            c = '%s.xyz' % coord
        if op == 'texldl':
            look = 'textureLod(%s, %s, %s.w)' % (name, c, coord)
        elif op == 'texldd':
            dx, dy = self.src(ins.src[2]), self.src(ins.src[3])
            if typ == '2d':
                look = 'textureGrad(%s, %s, vec2(%s.x, -%s.y), vec2(%s.x, -%s.y))' % (name, c, dx, dx, dy, dy)
            else:
                look = 'textureGrad(%s, %s, %s.xyz, %s.xyz)' % (name, c, dx, dy)
        elif op == 'texld' and ins.control == 2:            # texldb: lod bias in w
            look = ('texture(%s, %s, %s.w)' % (name, c, coord)) if self.stage == 'ps' else \
                'textureLod(%s, %s, 0.0)' % (name, c)
        elif self.stage == 'vs':
            look = 'textureLod(%s, %s, 0.0)' % (name, c)
        else:
            look = 'texture(%s, %s)' % (name, c)
        idx = sr.num if self.stage == 'ps' else 16 + sr.num
        return 'c2_conv(%s, drw.tmode[%d].x)%s' % (look, idx, _swz(sr))

    def run(self):
        """Emit every instruction."""
        p = self.p
        for ins in p.instructions:
            op = ins.op
            s = ins.src
            if op in ('nop',):
                continue
            if op == 'mov':
                self.write(ins.dst, self.src(s[0]))
            elif op in ('add', 'sub', 'mul'):
                o = {'add': '+', 'sub': '-', 'mul': '*'}[op]
                self.write(ins.dst, '(%s %s %s)' % (self.src(s[0]), o, self.src(s[1])))
            elif op == 'mad':
                self.write(ins.dst, '(%s * %s + %s)' % (self.src(s[0]), self.src(s[1]), self.src(s[2])))
            elif op == 'dp3':
                self.write(ins.dst, 'vec4(dot(%s.xyz, %s.xyz))' % (self.src(s[0]), self.src(s[1])))
            elif op == 'dp4':
                self.write(ins.dst, 'vec4(dot(%s, %s))' % (self.src(s[0]), self.src(s[1])))
            elif op == 'dp2add':
                self.write(ins.dst, 'vec4(dot(%s.xy, %s.xy) + %s.x)' % (self.src(s[0]), self.src(s[1]), self.src(s[2])))
            elif op == 'rcp':
                self.write(ins.dst, 'vec4(1.0 / %s.x)' % self.src(s[0]))
            elif op == 'rsq':
                self.write(ins.dst, 'vec4(inversesqrt(abs(%s.x)))' % self.src(s[0]))
            elif op == 'exp':
                self.write(ins.dst, 'vec4(exp2(%s.x))' % self.src(s[0]))
            elif op == 'log':
                self.write(ins.dst, 'vec4(log2(abs(%s.x)))' % self.src(s[0]))
            elif op == 'pow':
                self.write(ins.dst, 'vec4(c2_pow(%s.x, %s.x))' % (self.src(s[0]), self.src(s[1])))
            elif op in ('min', 'max'):
                self.write(ins.dst, '%s(%s, %s)' % (op, self.src(s[0]), self.src(s[1])))
            elif op == 'slt':
                self.write(ins.dst, 'vec4(lessThan(%s, %s))' % (self.src(s[0]), self.src(s[1])))
            elif op == 'sge':
                self.write(ins.dst, 'vec4(greaterThanEqual(%s, %s))' % (self.src(s[0]), self.src(s[1])))
            elif op == 'frc':
                self.write(ins.dst, 'fract(%s)' % self.src(s[0]))
            elif op == 'abs':
                self.write(ins.dst, 'abs(%s)' % self.src(s[0]))
            elif op == 'sgn':
                self.write(ins.dst, 'sign(%s)' % self.src(s[0]))
            elif op == 'lrp':
                # lerp: s0 * (s1 - s2) + s2
                self.write(ins.dst, 'mix(%s, %s, %s)' % (self.src(s[2]), self.src(s[1]), self.src(s[0])))
            elif op == 'nrm':
                a = self.src(s[0])
                self.write(ins.dst, '(%s * inversesqrt(dot(%s.xyz, %s.xyz)))' % (a, a, a))
            elif op == 'cmp':
                # s0 >= 0 ? s1 : s2, per component
                self.write(ins.dst, 'mix(%s, %s, greaterThanEqual(%s, vec4(0.0)))' %
                           (self.src(s[2]), self.src(s[1]), self.src(s[0])))
            elif op == 'sincos':
                a = self.src(s[0])
                self.write(ins.dst, 'vec4(cos(%s.x), sin(%s.x), 0.0, 0.0)' % (a, a))
            elif op == 'crs':
                self.write(ins.dst, 'vec4(cross(%s.xyz, %s.xyz), 0.0)' % (self.src(s[0]), self.src(s[1])))
            elif op == 'lit':
                self.write(ins.dst, 'c2_lit(%s)' % self.src(s[0]))
            elif op == 'dst':
                a, b = self.src(s[0]), self.src(s[1])
                self.write(ins.dst, 'vec4(1.0, %s.y * %s.y, %s.z, %s.w)' % (a, b, a, b))
            elif op in ('m4x4', 'm4x3', 'm3x4', 'm3x3', 'm3x2'):
                # dst.i = dot(src0, src1 register + i)
                n_in = 4 if op[1] == '4' else 3
                n_out = int(op[3])
                a = self.src(s[0])
                comps = []
                for i in range(n_out):
                    r = d3dbc.Reg(s[1].type, s[1].num + i, s[1].swizzle, mod=s[1].mod, rel=s[1].rel)
                    b = self.src(r)
                    comps.append('dot(%s%s, %s%s)' % (a, '' if n_in == 4 else '.xyz', b, '' if n_in == 4 else '.xyz'))
                comps += ['0.0'] * (4 - n_out)
                self.write(ins.dst, 'vec4(%s)' % ', '.join(comps))
            elif op == 'mova':
                self.uses_a0 = True
                m = _mask(ins.dst)
                self.emit('a0.%s = ivec4(floor(%s + 0.5)).%s;' % (m, self.src(s[0]), m))
            elif op in ('texld', 'texldl', 'texldd'):
                self.write(ins.dst, self.texture(ins))
            elif op == 'texkill':
                d = ins.dst
                lv = self.dst(d)
                m = _mask(d)
                if len(m) == 1:
                    self.emit('if (%s.%s < 0.0) discard;' % (lv, m))
                else:
                    self.emit('if (any(lessThan(%s.%s, vec%d(0.0)))) discard;' % (lv, m, len(m)))
            elif op == 'if':
                if s[0].type == d3dbc.CONSTBOOL:
                    self.emit('if (%s) {' % self.boolean(s[0].num))
                else:
                    raise Unsupported('if on register type %d' % s[0].type)
                self.indent += 1
            elif op == 'ifc':
                self.emit('if %s {' % _cmp(ins.control, self.src(s[0]) + '.x', self.src(s[1]) + '.x'))
                self.indent += 1
            elif op == 'else':
                self.indent -= 1
                self.emit('} else {')
                self.indent += 1
            elif op in ('endif', 'endrep'):
                self.indent -= 1
                self.emit('}')
            elif op == 'rep':
                k = len(self.lines)
                self.emit('for (int rep%d = 0; rep%d < %s.x; rep%d++) {' % (k, k, self.integer(s[0].num), k))
                self.indent += 1
            elif op == 'loop':
                # loop aL, i#: count i.x, start i.y, step i.z
                iv = self.integer(s[1].num)
                d = self.loops
                self.loops += 1
                self.max_loops = max(self.max_loops, self.loops)
                self.emit('aL%d = %s.y;' % (d, iv))
                self.emit('for (int lc%d = 0; lc%d < %s.x; lc%d++, aL%d += %s.z) {' % (d, d, iv, d, d, iv))
                self.indent += 1
            elif op == 'endloop':
                self.loops -= 1
                self.indent -= 1
                self.emit('}')
            elif op == 'break':
                self.emit('break;')
            elif op == 'breakc':
                self.emit('if %s break;' % _cmp(ins.control, self.src(s[0]) + '.x', self.src(s[1]) + '.x'))
            elif op in ('dsx', 'dsy'):
                # D3D's screen y points down, GL's up
                self.write(ins.dst, ('dFdx(%s)' if op == 'dsx' else '(-dFdy(%s))') % self.src(s[0]))
            else:
                raise Unsupported('instruction %s' % op)
        if self.indent != 1:
            raise Unsupported('unbalanced flow control')

    def declarations(self):
        """Local declarations at the top of main()."""
        out = []
        for n in sorted(self.temps):
            out.append('  vec4 r%d = vec4(0.0);' % n)
        for n in sorted(self.outs):
            out.append('  vec4 o%d = vec4(0.0);' % n)
        if self.uses_a0:
            out.append('  ivec4 a0 = ivec4(0);')
        for d in range(self.max_loops):
            out.append('  int aL%d = 0;' % d)
        return out

    def constants(self):
        """GLSL constants for the `def` literals (outside main)."""
        return ['const vec4 c%d = vec4(%s);' % (n, ', '.join(_flt(x) for x in v)) for n, v in sorted(self.p.defs.items())]


class Converter:
    """Converts one effect technique (VS + PS bytecode).

    streams: {(usage, index): element name} from the technique annotation
    (fxgraph.Translator.streams). is_draw(name): True for per-draw values
    (material and instance parameters), False for engine globals.
    """

    def __init__(self, streams, is_draw, layout):
        self.streams = streams
        self.is_draw = is_draw
        self.layout = layout
        self.draw_entries = set()
        self.engine_names = set()

    def element(self, usage, idx):
        """Vertex element name of a vs input (usage, index)."""
        if usage == 'POSITION' and idx == 0:
            return 'Position'
        name = self.streams.get((usage, idx))
        if name is None:
            name = {'NORMAL': 'Normal', 'TANGENT': 'Tangent', 'BINORMAL': 'Binormal',
                    'BLENDWEIGHT': 'BlendWeights', 'BLENDINDICES': 'BoneIndices'}.get(usage, '%s%d' % (usage, idx))
        return name

    def convert(self, vs_code, ps_code):
        """-> Shader (sources and binding info)."""
        vs = d3dbc.decode(vs_code)
        ps = d3dbc.decode(ps_code)
        if vs.kind != 'vs' or ps.kind != 'ps' or vs.version[0] < 3 or ps.version[0] < 3:
            raise Unsupported('not vs_3_0 / ps_3_0')
        sh = Shader()
        # vertex shader
        v = _Stage(vs, self, 'vs')
        v.run()
        pre = []
        for reg, (usage, idx, mask) in sorted((k, x) for k, x in vs.inputs.items() if isinstance(k, int)):
            el = self.element(usage, idx)
            if el in CONST_INPUTS:
                pre.append('  vec4 v%d = %s;' % (reg, CONST_INPUTS[el]))
                continue
            n = ELEMENT_SIZES.get(el, 2 if (el.startswith('Uv') or el.endswith('UV')) else
                                  3 if el.startswith(('Tangent', 'Binormal')) else 4)
            a = 'a_' + ''.join(ch if ch.isalnum() else '_' for ch in el)
            if a not in [x[0] for x in sh.attrs]:
                sh.attrs.append((a, el, n))
            # D3D fills missing components with (0, 0, 0, 1)
            fill = {1: '%s, 0.0, 0.0, 1.0', 2: '%s, 0.0, 1.0', 3: '%s, 1.0', 4: '%s'}[n] % a
            pre.append('  vec4 v%d = vec4(%s);' % (reg, fill))
        post = []
        pos_reg = None
        vary = {}
        for reg, (usage, idx, mask) in sorted(vs.outputs.items()):
            if usage == 'POSITION' and idx == 0:
                pos_reg = reg
                continue
            if usage in ('PSIZE', 'FOG'):
                continue
            name = 'c2_%s%d' % (usage, idx)
            vary[(usage, idx)] = name
            post.append('  %s = o%d;' % (name, reg))
            v.outs.add(reg)
        if pos_reg is None:
            raise Unsupported('no position output')
        if not vary:
            # an empty interface block doesn't compile
            vary[('#', 0)] = 'c2_unused'
            post.append('  c2_unused = vec4(0.0);')
        v.outs.add(pos_reg)
        post.append('  gl_Position = vec4(o%d.xy, 2.0 * o%d.z - o%d.w, o%d.w);' % ((pos_reg,) * 4))
        sh.varyings = sorted(vary.values())
        sh.vs_src = '\n'.join(v.constants() + [HELPERS, 'void main()', '{'] + v.declarations() + pre +
                              v.lines + post + ['}'])
        # pixel shader
        f = _Stage(ps, self, 'ps')
        f.run()
        pre = []
        for reg, (usage, idx, mask) in sorted(((k, x) for k, x in ps.inputs.items() if isinstance(k, int))):
            name = vary.get((usage, idx))
            pre.append('  vec4 v%d = %s;' % (reg, name if name else 'vec4(0.0)'))
        if ('misc', 0) in ps.inputs:
            # VPOS: pixel position, y down (eng '#target' holds the target size)
            t = self.layout.slot('#target', 1, 0)
            self.engine_names.add('#target')
            pre.append('  vec4 misc0 = vec4(gl_FragCoord.x - 0.5, eng.e[%d].y - gl_FragCoord.y - 0.5, 0.0, 0.0);' % t)
        if ('misc', 1) in ps.inputs:
            pre.append('  vec4 misc1 = vec4(gl_FrontFacing ? 1.0 : -1.0);')
        post = ['  // alpha test (fixed-function in D3D9)',
                '  if (drw.misc.x > 0.5) {',
                '    float a = oC0.a, ref = drw.misc.y; int fn = int(drw.misc.z);',
                '    bool pass = fn == 1 ? false : fn == 2 ? a < ref : fn == 3 ? a == ref : fn == 4 ? a <= ref :',
                '                fn == 5 ? a > ref : fn == 6 ? a != ref : fn == 7 ? a >= ref : true;',
                '    if (!pass) discard;',
                '  }',
                '  fragColor = oC0;']
        sh.fs_src = '\n'.join(f.constants() + [HELPERS, HELPERS_PS, 'void main()', '{', '  vec4 oC0 = vec4(0.0);'] +
                              f.declarations() + pre + f.lines + post + ['}'])
        sh.ps_samplers = [(name, reg, ps.samplers.get(reg, '2d'), self._sampler_name(ps, reg))
                          for reg, name in sorted(f.samplers.items())]
        sh.vs_samplers = [(name, reg, vs.samplers.get(reg, '2d'), self._sampler_name(vs, reg))
                          for reg, name in sorted(v.samplers.items())]
        sh.draw_entries = sorted(self.draw_entries)
        sh.engine_names = sorted(self.engine_names)
        sh.relative = v.relative | f.relative
        sh.kill = any(i.op == 'texkill' for i in ps.instructions)
        return sh

    @staticmethod
    def _sampler_name(prog, reg):
        """CTAB name of sampler register reg (e.g. 'samDiffuseMapA1')."""
        for c in prog.constants:
            if c['set'] == 'sampler' and c['reg'] <= reg < c['reg'] + c['count']:
                return c['name']
        return 's%d' % reg


class Shader:
    """A converted technique.

    attrs         [(GLSL attribute, vertex element, float count)]
    varyings      GLSL names of the vec4 values passed VS -> PS
    ps_samplers   [(GLSL name, register, '2d'/'cube'/'volume', CTAB name)]
    vs_samplers   the same for vertex texture fetch (tmode index 16 + reg)
    draw_entries  [(stage, name, register, count, class, set)] per-draw values
    engine_names  engine globals read (their slots are in the EngineLayout)
    relative      CTAB names read with relative addressing (bone palettes)
    vs_src/fs_src GLSL of the two stages (main + helpers)
    """

    def __init__(self):
        self.attrs = []
        self.varyings = []
        self.ps_samplers = []
        self.vs_samplers = []
        self.draw_entries = []
        self.engine_names = []
        self.relative = set()
        self.vs_src = ''
        self.fs_src = ''
        self.kill = False
