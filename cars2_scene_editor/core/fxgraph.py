"""Symbolic execution of an effect's Standard vertex + pixel shader.

The two shaders become one scalar expression graph (hash-consed, constant
folded) from mesh inputs, material parameters, engine globals and texture
samples to the pixel colour. matnodes/fxnodes turns it into Blender nodes, so
the viewport runs the game's own shading maths.

World-space convention: mesh inputs (position, normal, tangent ...) are given
in game world space, so the world matrices are identity and the projection
only feeds screen-space engine buffers, which are replaced by constants.

Expression nodes are tuples:
    ('k', value)                          constant
    ('in', element, comp)                 vertex stream / built-in input
    ('par', name, element, comp)          material or instance parameter
    ('glob', name, element, comp)         engine global (sun, fog, SH, time ...)
    ('tex', sampler, stage, u, v, w, comp)  texture read (u v w are expressions)
    (op, a[, b[, c]])                     add mul min max pow exp2 log2 rcp rsq
                                          frc sin cos abs neg sat lt sel ...

How "symbolic execution" works here: every shader register holds 4
*expressions* instead of 4 numbers. Running `mad r0, v1, c3, r2` builds
add(mul(v1, c3), r2) for each component. At the end, the pixel shader's
colour output register holds the whole computation as one expression tree.
Branches (if/else) become select(condition, then, else) per component.
"Hash-consed" means identical sub-expressions are stored once (Graph.n), so
the tree stays small and Blender gets one node per distinct operation.
"""
import d3dbc

ENGINE_SAMPLERS = {
    # sampler name -> constant result (r, g, b, a) used instead of the run-time buffer
    'samShadowMask': (1.0, 1.0, 1.0, 1.0),      # fully lit (no dynamic shadow)
    # ambient volumes: black is exact - every shipped volume's material lacks emissiveColor
    'samAmbientBuffer': (0.0, 0.0, 0.0, 0.0),
    'samShadowTexture': (1.0, 1.0, 1.0, 1.0),
    'samDepthViewBuffer': (1.0, 1.0, 1.0, 1.0),
    'samDepthViewHalfBuffer': (1.0, 1.0, 1.0, 1.0),
    'samReflectionPlaneDepthBuffer': (1.0, 1.0, 1.0, 1.0),
    'samReflectionPlaneTexture': (0.0, 0.0, 0.0, 0.0),
    'samColorBuffer': (0.0, 0.0, 0.0, 0.0),
}
# engine parameters that become identity / camera / zero
IDENTITY_MATRICES = ('g_WorldMatrix', 'g_WorldInverseMatrix', 'g_WorldInverseTransposeMatrix', 'g_World',
                     'g_WorldInverse')
ZERO_GLOBALS = ('g_ViewProjection', 'g_View', 'g_Projection', 'g_TargetDimensions', 'g_ShadowMatrix',
                'g_ReflectionPlaneProj', 'ShadowMapDimensions')
# engine parameters with a fixed stand-in value. g_CameraParams.y is the far
# plane: 103 soft-particle effects (dust, smoke, glows) fade alpha by
# sat((sceneDepth * far + particleDepth) * g_AlphaEdgeScale); with the depth
# buffer stubbed to 1 (nothing behind the particle) that must come out 1.
CONST_GLOBALS = {'g_CameraParams': (0.1, 10000.0, 0.0, 0.0)}
# bump when translations change, so node groups cached in .blend files are rebuilt
TRANSLATOR_VERSION = 2


class Unsupported(Exception):
    """The shader uses something the translator can't express (Game Look then
    falls back to the simplified material)."""
    pass


class Graph:
    """Expression builder with constant folding and sharing."""

    def __init__(self):
        self.memo = {}

    def n(self, *t):
        """The canonical (shared) copy of expression tuple t."""
        return self.memo.setdefault(t, t)

    def k(self, v):
        """Constant expression."""
        return self.n('k', float(v))

    # -- folding helpers -----------------------------------------------------------
    # Each helper simplifies when it can (constants, x+0, x*1, x*0 ...) so the
    # Blender node tree doesn't fill up with useless maths.
    @staticmethod
    def isk(a, v=None):
        """Is a a constant (equal to v, when given)?"""
        return a[0] == 'k' and (v is None or a[1] == v)

    def add(self, a, b):
        if self.isk(a) and self.isk(b):
            return self.k(a[1] + b[1])
        if self.isk(a, 0.0):
            return b
        if self.isk(b, 0.0):
            return a
        if a > b:
            a, b = b, a             # canonical order: a+b and b+a become the same node
        return self.n('add', a, b)

    def mul(self, a, b):
        if self.isk(a) and self.isk(b):
            return self.k(a[1] * b[1])
        if self.isk(a, 0.0) or self.isk(b, 0.0):
            return self.k(0.0)
        if self.isk(a, 1.0):
            return b
        if self.isk(b, 1.0):
            return a
        if self.isk(a, -1.0):
            return self.neg(b)
        if self.isk(b, -1.0):
            return self.neg(a)
        if a > b:
            a, b = b, a
        return self.n('mul', a, b)

    def neg(self, a):
        if self.isk(a):
            return self.k(-a[1])
        if a[0] == 'neg':
            return a[1]             # --x = x
        return self.n('neg', a)

    def sub(self, a, b):
        return self.add(a, self.neg(b))

    def un(self, op, a):
        """Unary operation (rcp rsq abs frc exp2 log2 sin cos sat)."""
        import math
        if self.isk(a):
            x = a[1]
            try:
                return self.k({'rcp': lambda: 1.0 / x if x else float('inf'),
                               'rsq': lambda: 1.0 / math.sqrt(abs(x)) if x else float('inf'),
                               'abs': lambda: abs(x), 'frc': lambda: x - math.floor(x),
                               'exp2': lambda: 2.0 ** x, 'log2': lambda: math.log2(abs(x)) if x else -float('inf'),
                               'sin': lambda: math.sin(x), 'cos': lambda: math.cos(x),
                               'sat': lambda: min(1.0, max(0.0, x))}[op]())
            except (KeyError, OverflowError, ValueError):
                pass
        if op == 'sat' and a[0] == 'sat':
            return a                # sat(sat(x)) = sat(x)
        return self.n(op, a)

    def bin(self, op, a, b):
        """Binary operation (min max pow lt)."""
        if self.isk(a) and self.isk(b):
            x, y = a[1], b[1]
            if op == 'min':
                return self.k(min(x, y))
            if op == 'max':
                return self.k(max(x, y))
            if op == 'pow':
                try:
                    return self.k(abs(x) ** y)
                except (OverflowError, ZeroDivisionError, ValueError):
                    pass
            if op == 'lt':
                return self.k(1.0 if x < y else 0.0)
        if op in ('min', 'max') and a == b:
            return a
        if op == 'pow' and self.isk(b, 1.0):
            return self.un('abs', a)
        return self.n(op, a, b)

    def sel(self, c, a, b):
        """c is 0/1: c ? a : b"""
        if self.isk(c):
            return a if c[1] else b
        if a == b:
            return a
        return self.n('sel', c, a, b)

    def sat(self, a):
        """Clamp to 0..1 (D3D's _sat modifier)."""
        return self.un('sat', a)


def _swz(vals, reg):
    """Apply a source register's swizzle (e.g. .yyzw) to 4 values."""
    return [vals[reg.swizzle[i]] for i in range(4)]


class Translator:
    """Turns one effect (a decoded .tfx tree) into an expression graph."""

    def __init__(self, tfx, technique='Standard'):
        self.g = Graph()
        self.tfx = tfx
        tech = tfx.get('Technique#' + technique)
        if tech is None:
            raise Unsupported('no %s technique' % technique)
        # the first pass ('Pass#p0'; engine effects such as shadowmask_* leave it unnamed)
        self.pass0 = tech.get('Pass#p0') or next(v for k, v in tech.items() if k.startswith('Pass#'))
        ann = tech.get('Annotation', {})
        # stream semantic -> element name, e.g. ('TEXCOORD', 2) -> 'Uv1'
        self.streams = {}
        for k, v in ann.items():
            if not k.startswith('string#'):
                continue
            sem = str(v)
            base = sem.rstrip('0123456789')
            idx = int(sem[len(base):] or 0)
            self.streams[(base, idx)] = k.split('#', 1)[1]
        self.samplers = {}   # CTAB sampler name -> semantic (texture usage)
        self.sampler_states = {}
        for s in tfx.get('Samplers', {}).values():
            if isinstance(s, dict):
                self.samplers[s['Name']] = s.get('Semantic', s['Name'])
                self.sampler_states[s['Name']] = {k.split('#')[1]: v for k, v in s.items() if k.startswith('SamplerState')}
        self.params = {}     # parameter name -> its .tfx description (scope, default ...)
        for p in tfx.get('Parameters', {}).values():
            if isinstance(p, dict):
                self.params[p['Name']] = p
        self.sampler_types = {}   # CTAB sampler name -> '2d' / 'cube' / 'volume'
        self.kill = []            # texkill conditions (alpha test)
        self.used_inputs = set()  # vertex elements the shader reads
        self.warnings = []

    # -- leaves -----------------------------------------------------------------------
    def const_reg(self, prog, cmap, num):
        """4 expressions for float constant register c<num>: a literal (def),
        a material parameter, an engine global, or a stand-in value."""
        g = self.g
        if num in prog.defs:
            return [g.k(x) for x in prog.defs[num]]
        ent = cmap.get(('float4', num))
        if ent is None:
            return [g.k(0.0)] * 4
        name, el = ent          # parameter name, row of it (for matrices)
        if name in IDENTITY_MATRICES:
            return [g.k(1.0 if c == el else 0.0) for c in range(4)]
        if name == 'g_ViewInverse':
            # camera matrix: identity rotation + the camera position in row 3
            if el < 3:
                return [g.k(1.0 if c == el else 0.0) for c in range(3)] + [g.n('glob', 'campos', 0, el)]
            return [g.k(0.0)] * 3 + [g.k(1.0)]
        if name in ZERO_GLOBALS:
            return [g.k(0.0)] * 4
        if name in CONST_GLOBALS:
            return [g.k(x) for x in CONST_GLOBALS[name]]
        p = self.params.get(name)
        if p is not None and p.get('Scope') in ('PerMaterial', 'PerInstance', 'NoRegister') and \
                name not in ('objectSH', 'g_FinalAlpha'):
            return [g.n('par', name, el, c) for c in range(4)]
        return [g.n('glob', name, el, c) for c in range(4)]

    # -- execution ----------------------------------------------------------------------
    def run(self, prog, inputs):
        """inputs: {reg num: [4 exprs]} -> (regs dict, outputs {reg: [4]})"""
        g = self.g
        cmap = d3dbc.constant_map(prog)
        regs = {}       # temporaries r#
        outs = {}       # output registers
        samplers = {c['reg']: c['name'] for c in prog.constants if c['set'] == 'sampler'}
        stack = []      # open if/else blocks

        def read(r):
            # value of a source operand (swizzle and modifier applied)
            if r.rel:
                if r.type != d3dbc.CONST:
                    raise Unsupported('relative addressing')
                # skinning: c[a0 + base] indexes the bone palette. Blender's armature
                # already deforms the mesh, so every bone matrix is the identity
                ent = next((c for c in prog.constants if c['set'] == 'float4' and c['reg'] <= r.num < c['reg'] + c['count']),
                           None)
                if ent is None:
                    raise Unsupported('relative addressing outside a constant array')
                per = 3 if ent['count'] % 3 == 0 else 4
                row = (r.num - ent['reg']) % per
                vals = [g.k(1.0 if c == row else 0.0) for c in range(4)]
                return _swz(vals, r)
            if r.type == d3dbc.TEMP:
                vals = regs.get(r.num, [g.k(0.0)] * 4)
            elif r.type == d3dbc.INPUT:
                vals = inputs.get(r.num, [g.k(0.0)] * 4)
            elif r.type == d3dbc.CONST:
                vals = self.const_reg(prog, cmap, r.num)
            elif r.type == d3dbc.MISCTYPE:
                # VFACE = 1 (front face), VPOS = 0
                vals = [g.k(1.0)] * 4 if r.num == 1 else [g.k(0.0)] * 4
            elif r.type == d3dbc.CONSTBOOL:
                vals = [g.k(float(prog.defb.get(r.num, False)))] * 4
            elif r.type == d3dbc.CONSTINT:
                vals = [g.k(float(x)) for x in prog.defi.get(r.num, (0, 0, 0, 0))]
            else:
                raise Unsupported('read register type %d' % r.type)
            v = _swz(vals, r)
            if r.mod == 'neg':
                v = [g.neg(x) for x in v]
            elif r.mod == 'abs':
                v = [g.un('abs', x) for x in v]
            elif r.mod == 'absneg':
                v = [g.neg(g.un('abs', x)) for x in v]
            elif r.mod:
                raise Unsupported('source modifier %s' % r.mod)
            return v

        def write(d, vals):
            # store into a destination register, only the masked components
            if d.type == d3dbc.TEMP:
                cur = list(regs.get(d.num, [g.k(0.0)] * 4))
                tgt = regs
            elif d.type in (d3dbc.OUTPUT, d3dbc.COLOROUT, d3dbc.RASTOUT, d3dbc.ATTROUT, d3dbc.DEPTHOUT):
                key = (d.type, d.num)
                cur = list(outs.get(key, [g.k(0.0)] * 4))
                tgt = None
            elif d.type in (d3dbc.ADDR, d3dbc.PREDICATE):
                raise Unsupported('address/predicate registers')
            else:
                raise Unsupported('write register type %d' % d.type)
            for c in d.mask:
                x = vals[c]
                cur[c] = g.sat(x) if d.sat else x
            if tgt is None:
                outs[(d.type, d.num)] = cur
            else:
                tgt[d.num] = cur

        def texread(sampler_reg, coord):
            # a texture lookup leaf (or a constant for engine-only buffers)
            name = samplers.get(sampler_reg, 's%d' % sampler_reg)
            self.sampler_types[name] = prog.samplers.get(sampler_reg, '2d')
            if name in ENGINE_SAMPLERS:
                return [g.k(x) for x in ENGINE_SAMPLERS[name]]
            u, v, w = coord[0], coord[1], coord[2]
            # stage = how many texture reads deep the coordinates are (a lookup
            # whose UV comes from another lookup is a later stage; fxnodes
            # builds one node group per stage)
            stage = 0
            for e in (u, v, w):
                stage = max(stage, self._stage(e))
            return [g.n('tex', name, stage, u, v, w, c) for c in range(4)]

        # one branch per instruction: the D3D9 semantics, component by component
        for ins in prog.instructions:
            op = ins.op
            s = [read(x) if x.type != d3dbc.SAMPLER else x for x in ins.src]
            if op == 'mov':
                write(ins.dst, s[0])
            elif op == 'add':
                write(ins.dst, [g.add(a, b) for a, b in zip(s[0], s[1])])
            elif op == 'sub':
                write(ins.dst, [g.sub(a, b) for a, b in zip(s[0], s[1])])
            elif op == 'mul':
                write(ins.dst, [g.mul(a, b) for a, b in zip(s[0], s[1])])
            elif op == 'mad':
                write(ins.dst, [g.add(g.mul(a, b), c) for a, b, c in zip(s[0], s[1], s[2])])
            elif op in ('dp3', 'dp4'):
                n = 3 if op == 'dp3' else 4
                acc = g.mul(s[0][0], s[1][0])
                for i in range(1, n):
                    acc = g.add(acc, g.mul(s[0][i], s[1][i]))
                write(ins.dst, [acc] * 4)
            elif op == 'dp2add':
                acc = g.add(g.add(g.mul(s[0][0], s[1][0]), g.mul(s[0][1], s[1][1])), s[2][0])
                write(ins.dst, [acc] * 4)
            elif op in ('rcp', 'rsq', 'frc', 'abs'):
                if op == 'frc':
                    write(ins.dst, [g.un('frc', a) for a in s[0]])
                elif op == 'abs':
                    write(ins.dst, [g.un('abs', a) for a in s[0]])
                else:  # scalar ops read the (replicated) first swizzled component
                    x = g.un(op, s[0][0])
                    write(ins.dst, [x] * 4)
            elif op in ('exp', 'log'):
                x = g.un('exp2' if op == 'exp' else 'log2', s[0][0] if op == 'exp' else g.un('abs', s[0][0]))
                write(ins.dst, [x] * 4)
            elif op in ('min', 'max'):
                write(ins.dst, [g.bin(op, a, b) for a, b in zip(s[0], s[1])])
            elif op == 'pow':
                x = g.bin('pow', g.un('abs', s[0][0]), s[1][0])
                write(ins.dst, [x] * 4)
            elif op == 'lrp':
                # lerp: a * (b - c) + c
                write(ins.dst, [g.add(g.mul(a, g.sub(b, c)), c) for a, b, c in zip(s[0], s[1], s[2])])
            elif op == 'nrm':
                ln = g.un('rsq', g.add(g.add(g.mul(s[0][0], s[0][0]), g.mul(s[0][1], s[0][1])), g.mul(s[0][2], s[0][2])))
                write(ins.dst, [g.mul(s[0][i], ln) for i in range(3)] + [g.mul(s[0][3], ln)])
            elif op == 'cmp':
                # a < 0 ? c : b
                write(ins.dst, [g.sel(g.bin('lt', a, g.k(0.0)), c, b) for a, b, c in zip(s[0], s[1], s[2])])
            elif op in ('slt', 'sge'):
                vals = [g.bin('lt', a, b) for a, b in zip(s[0], s[1])]
                if op == 'sge':
                    vals = [g.sub(g.k(1.0), x) for x in vals]
                write(ins.dst, vals)
            elif op == 'sincos':  # dst.x = cos, dst.y = sin of the replicated source
                a = s[0][0]
                write(ins.dst, [g.un('cos', a), g.un('sin', a), g.k(0.0), g.k(0.0)])
            elif op == 'crs':
                # cross product
                a, b = s[0], s[1]
                write(ins.dst, [g.sub(g.mul(a[1], b[2]), g.mul(a[2], b[1])),
                                g.sub(g.mul(a[2], b[0]), g.mul(a[0], b[2])),
                                g.sub(g.mul(a[0], b[1]), g.mul(a[1], b[0])), g.k(0.0)])
            elif op in ('dsx', 'dsy'):
                write(ins.dst, [g.k(0.0)] * 4)      # screen derivatives: not available, 0
            elif op in ('texld', 'texldl', 'texldd'):
                write(ins.dst, _swz(texread(ins.src[1].num, s[0]), ins.src[1]))
            elif op == 'texkill':
                # discard the pixel if any masked component < 0 (alpha test)
                v = read(ins.dst)
                self.kill.extend(v[c] for c in ins.dst.mask)
            elif op in ('if', 'ifc'):
                if op == 'ifc':
                    a, b = s[0][0], s[1][0]
                    cmpk = d3dbc.COMPARISONS.get(ins.control, 'ne')
                    lt = g.bin('lt', a, b)
                    gt = g.bin('lt', b, a)
                    one = g.k(1.0)
                    cond = {'gt': gt, 'lt': lt, 'ge': g.sub(one, lt), 'le': g.sub(one, gt),
                            'eq': g.sub(one, g.add(lt, gt)), 'ne': g.add(lt, gt)}[cmpk]
                else:
                    cond = s[0][0]
                # remember the state before the branch
                stack.append({'cond': cond, 'pre': (dict(regs), dict(outs)), 'then': None})
            elif op == 'else':
                # keep the "then" state, restart from the state before the branch
                fr = stack[-1]
                fr['then'] = (dict(regs), dict(outs))
                regs.clear(); regs.update(fr['pre'][0])
                outs.clear(); outs.update(fr['pre'][1])
            elif op == 'endif':
                # merge: every register becomes select(cond, then value, else value)
                fr = stack.pop()
                then_r, then_o = fr['then'] if fr['then'] is not None else (dict(regs), dict(outs))
                else_r, else_o = (dict(regs), dict(outs)) if fr['then'] is not None else fr['pre']
                for store, t_, e_ in ((regs, then_r, else_r), (outs, then_o, else_o)):
                    merged = {}
                    for key in set(t_) | set(e_):
                        tv = t_.get(key, [g.k(0.0)] * 4)
                        ev = e_.get(key, [g.k(0.0)] * 4)
                        merged[key] = [g.sel(fr['cond'], a, b) for a, b in zip(tv, ev)]
                    store.clear()
                    store.update(merged)
            elif op in ('nop', 'mova'):
                pass  # mova only feeds the (identity) bone palette lookups
            else:
                raise Unsupported('instruction %s' % op)
        if stack:
            raise Unsupported('unbalanced if')
        return regs, outs

    def _stage(self, e, memo=None):
        """Texture-read depth of expression e (0 = needs no texture read)."""
        if memo is None:
            memo = self._stage_memo = getattr(self, '_stage_memo', {})
        if id(e) in memo:
            return memo[id(e)]
        if e[0] == 'tex':
            r = e[2] + 1
        elif e[0] in ('k', 'in', 'par', 'glob'):
            r = 0
        else:
            r = max(self._stage(x, memo) for x in e[1:] if isinstance(x, tuple))
        memo[id(e)] = r
        return r

    def translate(self, vs_code, ps_code):
        """-> dict(color=[r,g,b], alpha=a, kill=[...], textures={sampler: info})"""
        g = self.g
        vs = d3dbc.decode(vs_code)
        ps = d3dbc.decode(ps_code)
        # vertex inputs: each declared input register becomes 'in' leaves
        # named after the mesh's vertex element
        vin = {}
        for reg, (usage, idx, mask) in vs.inputs.items():
            if not isinstance(reg, int):
                continue
            if usage == 'POSITION' and idx == 0:
                name = 'Position'
            else:
                name = self.streams.get((usage, idx))
                if name is None:
                    name = {'NORMAL': 'Normal', 'TANGENT': 'Tangent', 'BINORMAL': 'Binormal'}.get(usage, '%s%d' % (usage, idx))
            self.used_inputs.add(name)
            if name in ('BlendWeights', 'BoneIndices'):
                # identity bone palette (see read()): all weight on the first bone
                vin[reg] = [g.k(1.0 if (name == 'BlendWeights' and c == 0) else 0.0) for c in range(4)]
                continue
            if name == 'Position':
                vin[reg] = [g.n('in', 'Position', 0, c) for c in range(3)] + [g.k(1.0)]
            else:
                vin[reg] = [g.n('in', name, 0, c) for c in range(4)]
        # run the vertex shader, then feed its outputs to the pixel shader's
        # inputs by matching (usage, index), e.g. TEXCOORD3 -> TEXCOORD3
        _, vouts = self.run(vs, vin)
        vsout = {}
        for reg, (usage, idx, mask) in vs.outputs.items():
            vsout[(usage, idx)] = vouts.get((d3dbc.OUTPUT, reg), [g.k(0.0)] * 4)
        pin = {}
        for reg, (usage, idx, mask) in ps.inputs.items():
            if isinstance(reg, int):
                pin[reg] = vsout.get((usage, idx), [g.k(0.0)] * 4)
        _, pouts = self.run(ps, pin)
        col = pouts.get((d3dbc.COLOROUT, 0), [g.k(0.0)] * 4)       # oC0 = the pixel colour
        return {'color': col[:3], 'alpha': col[3], 'kill': self.kill}


def nodes_of(roots):
    """Topologically ordered unique expression nodes reachable from roots."""
    seen = {}
    order = []
    stack = [(r, False) for r in roots]
    # iterative depth-first walk (expression trees can be deep)
    while stack:
        e, done = stack.pop()
        if done:
            if id(e) not in seen:
                seen[id(e)] = True
                order.append(e)
            continue
        if id(e) in seen:
            continue
        stack.append((e, True))
        if e[0] not in ('k', 'in', 'par', 'glob'):
            for x in e[1:]:
                if isinstance(x, tuple):
                    stack.append((x, False))
    return order


def evaluate(e, env, memo=None):
    """Numeric evaluation for testing: env(leaf) -> float; tex leaves -> env too."""
    import math
    if memo is None:
        memo = {}
    if id(e) in memo:
        return memo[id(e)]
    op = e[0]
    if op == 'k':
        r = e[1]
    elif op in ('in', 'par', 'glob'):
        r = env(e)
    elif op == 'tex':
        r = env(('tex', e[1], evaluate(e[3], env, memo), evaluate(e[4], env, memo), evaluate(e[5], env, memo), e[6]))
    else:
        a = [evaluate(x, env, memo) for x in e[1:]]
        try:
            r = {'add': lambda: a[0] + a[1], 'mul': lambda: a[0] * a[1], 'neg': lambda: -a[0],
                 'min': lambda: min(a[0], a[1]), 'max': lambda: max(a[0], a[1]),
                 'pow': lambda: abs(a[0]) ** a[1], 'exp2': lambda: 2.0 ** a[0],
                 'log2': lambda: math.log2(abs(a[0])) if a[0] else -1e30,
                 'rcp': lambda: 1.0 / a[0] if a[0] else 1e30, 'rsq': lambda: 1.0 / math.sqrt(abs(a[0])) if a[0] else 1e30,
                 'frc': lambda: a[0] - math.floor(a[0]), 'abs': lambda: abs(a[0]), 'sin': lambda: math.sin(a[0]),
                 'cos': lambda: math.cos(a[0]), 'sat': lambda: min(1.0, max(0.0, a[0])),
                 'lt': lambda: 1.0 if a[0] < a[1] else 0.0, 'sel': lambda: a[1] if a[0] else a[2]}[op]()
        except (OverflowError, ValueError, ZeroDivisionError):
            r = 0.0
    memo[id(e)] = r
    return r
