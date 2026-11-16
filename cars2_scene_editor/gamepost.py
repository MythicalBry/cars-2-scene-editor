"""The end of a Cars 2 Game frame: exposure and the game's own post-process.

  1. Auto exposure, as the game measures it (downsample4x4_2t
     DownSampleToLum + avglum): the mean of log(Luminance(colour) + 0.00001)
     over the HDR frame, exp() of it, clamped to the world's
     g_HDR_Min/Max_Luminance. The game eases towards it over time
     (g_HDR_Adaptive_Luminance_Rate); the editor shows where it settles.
  2. postprocess.tfx (technique PostProcess), run from its own bytecode:
     exposure, the Hejl tone curve, motion blur and bloom blends, gamma and
     the colour-grade LUT. Bloom and motion blur come from the game's own
     chains (Bloom, MotionBlur, below).
  3. A blit into Blender's framebuffer: display colour -> scene-linear (what
     Blender expects from a render engine), plus depth for the overlays.

The engine effects come from the player's own game (assets/startup*.zip).
"""
import gpu
import numpy as np

LUM_SIZE = 64        # the log-luminance image the average is taken over

QUAD_VS = '''
void main()
{
  uv = pos * 0.5 + 0.5;
  gl_Position = vec4(pos, 0.0, 1.0);
}
'''
# HelperFunctions.inc Luminance() and DownSampleLum_PS: 4 bilinear taps per texel
LUM_FS = '''
float lum(vec3 c)
{
  float l = dot(c, vec3(0.2125, 0.7154, 0.0721));
  return l < 0.0001 ? 0.0001 : l;
}
void main()
{
  vec3 c = texture(hdr, uv + texel * vec2(-0.25, -0.25)).rgb;
  c += texture(hdr, uv + texel * vec2(0.25, -0.25)).rgb;
  c += texture(hdr, uv + texel * vec2(-0.25, 0.25)).rgb;
  c += texture(hdr, uv + texel * vec2(0.25, 0.25)).rgb;
  fragColor = vec4(log(lum(c * 0.25) + 0.00001));
}
'''
COPY_FS = '''
void main()
{
  fragColor = texture(image, uv);
}
'''
BLIT_FS = '''
void main()
{
  vec3 d = texture(image, uv).rgb;
  if (linear > 0.5) d = mix(d / 12.92, pow(max((d + 0.055) / 1.055, vec3(0.0)), vec3(2.4)), step(vec3(0.04045), d));
  fragColor = vec4(d, 1.0);
  // depth from the renderer's projection to Blender's (the overlays sort against it)
  float w = texture(depth, uv).r;
  float n = w * 2.0 - 1.0;
  float z = conv.w > 0.5 ? -conv.y / (n + conv.x) : (n - conv.y) / conv.x;
  float n2 = conv.w > 0.5 ? (conv2.x * z + conv2.y) / (-z) : conv2.x * z + conv2.y;
  gl_FragDepth = w >= 1.0 ? 1.0 : clamp(n2 * 0.5 + 0.5, 0.0, 1.0);
  // Film > Transparent: nothing drawn here (depth left at the clear) -> no background (premultiplied)
  if (transparent > 0.5 && w >= 1.0) fragColor = vec4(0.0);
}
'''


def _uniform_setter(sh):
    """sh.uniform_float that skips a uniform the GLSL compiler removed (Blender 4.1 compiles the depth write of
    the blit away without depth_write(), and with it the uniforms only that used)."""
    def uf(name, value):
        try:
            sh.uniform_float(name, value)
        except ValueError:
            pass
    return uf


_GLSL_TYPES = {'FLOAT': 'float', 'VEC2': 'vec2', 'VEC3': 'vec3', 'VEC4': 'vec4', 'INT': 'int', 'MAT4': 'mat4'}


def legacy_quad_shader(vs, fs, samplers, consts):
    """A full-screen quad shader that writes gl_FragDepth, for Blender without GPUShaderCreateInfo.depth_write()
    (4.1 and older: there the depth write - and the uniforms only it uses - is compiled away, the overlays lost
    their depth). The older GPUShader(vertex, fragment) constructor keeps it."""
    head = ''.join('uniform sampler2D %s;\n' % s for s in samplers) + \
        ''.join('uniform %s %s;\n' % (_GLSL_TYPES[t], n) for t, n in consts)
    return gpu.types.GPUShader('in vec2 pos;\nout vec2 uv;\n' + vs,
                               'in vec2 uv;\n' + head + 'out vec4 fragColor;\n' + fs)


def needs_legacy_depth():
    return not hasattr(gpu.types.GPUShaderCreateInfo, 'depth_write')


def _quad_shader(name, fs, samplers, consts, depth_write=False):
    if depth_write and needs_legacy_depth():
        return legacy_quad_shader(QUAD_VS, fs, samplers, consts)
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC2', 'pos')
    iface = gpu.types.GPUStageInterfaceInfo(name)
    iface.smooth('VEC2', 'uv')
    info.vertex_out(iface)
    for i, s in enumerate(samplers):
        info.sampler(i, 'FLOAT_2D', s)
    for typ, n in consts:
        info.push_constant(typ, n)
    info.fragment_out(0, 'VEC4', 'fragColor')
    if depth_write:
        info.depth_write('ANY')
    info.vertex_source(QUAD_VS)
    info.fragment_source(fs)
    return gpu.shader.create_from_info(info)


class Post:
    """GPU resources of the exposure measurement and the final passes."""

    def __init__(self):
        self.lum_shader = None
        self.blit_shader = None
        self.batch = None
        self.lum = None          # (fb, R32F texture) LUM_SIZE^2
        self.lum_tex = None      # 1x1: the average luminance postprocess reads
        self.ldr = None          # (w, h, fb, texture)
        self.measured = None     # last exp(mean log luminance), unclamped
        self.bloom = Bloom()
        self.copy_shader = None
        self.copied = None       # (w, h, fb, texture): the refraction copy
        self.motion = MotionBlur()

    def _batch(self, sh):
        if self.batch is None:
            from gpu_extras.batch import batch_for_shader
            self.batch = batch_for_shader(sh, 'TRI_FAN', {'pos': ((-1, -1), (1, -1), (1, 1), (-1, 1))})
        return self.batch

    def measure(self, hdr, w, h):
        """exp(mean log luminance) of the HDR frame (the game's avglum input)."""
        if self.lum_shader is None:
            self.lum_shader = _quad_shader('c2_lum_iface', LUM_FS, ['hdr'], [('VEC2', 'texel')])
            tex = gpu.types.GPUTexture((LUM_SIZE, LUM_SIZE), format='R32F')
            self.lum = (gpu.types.GPUFrameBuffer(color_slots=tex), tex)
        fb, tex = self.lum
        sh = self.lum_shader
        with fb.bind():
            gpu.state.viewport_set(0, 0, LUM_SIZE, LUM_SIZE)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
            sh.bind()
            sh.uniform_sampler('hdr', hdr)
            sh.uniform_float('texel', (1.0 / LUM_SIZE, 1.0 / LUM_SIZE))
            self._batch(sh).draw(sh)
            buf = fb.read_color(0, 0, LUM_SIZE, LUM_SIZE, 1, 0, 'FLOAT')
        buf.dimensions = LUM_SIZE * LUM_SIZE
        logs = np.array(buf, np.float64)
        self.measured = float(np.exp(logs.mean()))
        return self.measured

    def lum_texture(self, value):
        if self.lum_tex is None:
            self.lum_tex = gpu.types.GPUTexture((1, 1), format='RGBA16F')
        self.lum_tex.clear(format='FLOAT', value=(value, value, value, 1.0))
        return self.lum_tex

    def run_game_post(self, eff, w, h, eng, drw, binds):
        """postprocess.tfx over the frame into the LDR target; returns it."""
        if self.ldr is None or self.ldr[:2] != (w, h):
            tex = gpu.types.GPUTexture((w, h), format='RGBA16F')
            self.ldr = (w, h, gpu.types.GPUFrameBuffer(color_slots=tex), tex)
        _w, _h, fb, tex = self.ldr
        batch = _effect_quad(eff)
        with fb.bind():
            gpu.state.viewport_set(0, 0, w, h)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
            gpu.state.face_culling_set('NONE')
            sh = eff.shader
            sh.bind()
            sh.uniform_block('eng', eng)
            sh.uniform_block('drw', drw)
            for name, t in binds:
                sh.uniform_sampler(name, t)
            batch.draw(sh)
        return tex

    def copy(self, src, w, h):
        """A copy of a texture (the opaque frame for refraction: the game's colorBuffer)."""
        if self.copy_shader is None:
            self.copy_shader = _quad_shader('c2_copy_iface', COPY_FS, ['image'], [])
        if self.copied is None or self.copied[:2] != (w, h):
            tex = gpu.types.GPUTexture((w, h), format='RGBA16F')
            self.copied = (w, h, gpu.types.GPUFrameBuffer(color_slots=tex), tex)
        _w, _h, fb, tex = self.copied
        sh = self.copy_shader
        with fb.bind():
            gpu.state.viewport_set(0, 0, w, h)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
            sh.bind()
            sh.uniform_sampler('image', src)
            self._batch(sh).draw(sh)
        return tex

    def blit(self, image, depth, linear, proj=None, depth_proj=None, transparent=False):
        """image -> the bound framebuffer (scene-linear when `linear`), with depth
        re-projected from `proj` to `depth_proj` (when given). transparent (Film >
        Transparent): where nothing was drawn the background is left out (alpha 0)."""
        if self.blit_shader is None:
            self.blit_shader = _quad_shader('c2_blit_iface', BLIT_FS, ['image', 'depth'],
                                            [('FLOAT', 'linear'), ('VEC4', 'conv'), ('VEC4', 'conv2'), ('FLOAT', 'transparent')],
                                            depth_write=True)
        sh = self.blit_shader
        sh.bind()
        sh.uniform_sampler('image', image)
        sh.uniform_sampler('depth', depth)
        uf = _uniform_setter(sh)
        uf('linear', 1.0 if linear else 0.0)
        uf('transparent', 1.0 if transparent else 0.0)
        a = proj if proj is not None else depth_proj
        b = depth_proj if depth_proj is not None else proj
        if a is None:
            uf('conv', (1.0, 0.0, 0.0, 0.0))
            uf('conv2', (1.0, 0.0, 0.0, 0.0))      # orthographic identity: depth unchanged
        else:
            persp = 1.0 if abs(a[3][3]) < 1e-9 else 0.0
            uf('conv', (a[2][2], a[2][3], 0.0, persp))
            uf('conv2', (b[2][2], b[2][3], 0.0, 0.0))
        gpu.state.depth_test_set('ALWAYS')
        gpu.state.depth_mask_set(True)
        gpu.state.blend_set('ALPHA_PREMULT' if transparent else 'NONE')
        self._batch(sh).draw(sh)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)


_quads = {}


def _effect_quad(eff):
    """Full-screen quad with an engine effect's inputs: clip-space Position and
    the screen uv with v from the top (BaseUV / TEXCOORD0)."""
    q = _quads.get(id(eff))
    if q is not None and q[0] is eff:
        return q[1]
    fmt = gpu.types.GPUVertFormat()
    for a, el, n in eff.sh.attrs:
        fmt.attr_add(id=a, comp_type='F32', len=n, fetch_mode='FLOAT')
    vbo = gpu.types.GPUVertBuf(fmt, 4)
    pos = np.array([(-1, -1, 0, 1), (1, -1, 0, 1), (1, 1, 0, 1), (-1, 1, 0, 1)], np.float32)
    uv = np.array([(0, 1, 0, 0), (1, 1, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0)], np.float32)
    for a, el, n in eff.sh.attrs:
        src = pos if el == 'Position' else uv
        vbo.attr_fill(id=a, data=np.ascontiguousarray(src[:, :n]))
    batch = gpu.types.GPUBatch(type='TRI_FAN', buf=vbo)
    _quads[id(eff)] = (eff, batch)
    return batch


# ----------------------------------------------------------------------
# bloom (sdaemon _init_bloom / _submit_bloom)
# ----------------------------------------------------------------------

def _gauss1(x, sigma):
    """FUN_00e8a560: 1D normal distribution."""
    import math
    return math.exp(-(x * x) / (2.0 * sigma * sigma)) / (math.sqrt(6.2831854820251465) * sigma)


def _gauss2(x, y, sigma):
    """FUN_00e8a4e0: 2D normal distribution."""
    import math
    return math.exp(-(x * x + y * y) / (2.0 * sigma * sigma)) / (2.0 * 3.1415927 * sigma * sigma)


def downsample_gauss_params(w, h, sigma=1.0):
    """FUN_00e8af90: the 3x3 tap grid (+-0.666 texel of the destination size)
    and its normalised Gaussian weights -> (offsets [9 x (u, v)], weights [9])."""
    tw, th = 1.0 / w, 1.0 / h
    offs, wts = [], []
    steps = (-0.666, 0.0, 0.666)
    for x in steps:
        for y in steps:
            offs.append((x * tw, y * th))
            # FUN_00e8b230: four samples half a texel around the tap
            wts.append(_gauss2(x - tw * 0.5, y - th * 0.5, sigma) + _gauss2(x + tw * 0.5, y - th * 0.5, sigma) +
                       _gauss2(x - tw * 0.5, y + th * 0.5, sigma) + _gauss2(x + tw * 0.5, y + th * 0.5, sigma))
    s = sum(wts)
    return offs, [x / s for x in wts]


def glow_gauss_params(w, h, taps=5, sigma=1.0):
    """FUN_00e8ad40: a one-sided Gaussian of `taps` values folded into
    bilinear taps (centre + pairs) -> (offsets [3 x (u, v)], weights [3])."""
    k = [_gauss1(float(i), sigma) for i in range(taps)]
    total = k[0] + 2.0 * sum(k[1:])
    k = [x / total for x in k]
    offs, wts = [(0.0, 0.0)], [k[0]]
    for i in range(1, (taps + 1) // 2):
        a, b = k[2 * i - 1], k[2 * i]
        wts.append(a + b)
        t = b / (a + b)
        d = float(i << 1) - 0.5 - t          # as the engine computes it
        offs.append((d / w, d / h))
    return offs, wts


class Bloom:
    """The game's bloom chain: high pass at 1/4, Gaussian downsamples to 1/16
    and 1/64, separable 9-tap blurs and accumulation back up to 1/4. The
    result is what postprocess reads as blur_1_4."""

    def __init__(self):
        self.size = None
        self.levels = {}         # 4 / 16 / 64 -> [(fb, tex), (fb, tex)]

    def _targets(self, w, h):
        if self.size != (w, h):
            self.levels = {}
            for div in (4, 16, 64):
                lw, lh = max(1, w // div), max(1, h // div)
                pair = []
                for _ in range(2):
                    t = gpu.types.GPUTexture((lw, lh), format='RGBA16F')
                    pair.append((gpu.types.GPUFrameBuffer(color_slots=t), t, lw, lh))
                self.levels[div] = pair
            self.size = (w, h)
        return self.levels

    def run(self, effs, blocks, hdr, w, h, eng, lum_tex):
        """effs: {'highpass', 'down', 'blurh', 'blurv', 'accum'}: Effects.
        blocks(eff, key, values): per-draw block with parameter values.
        Returns the 1/4 bloom texture."""
        L = self._targets(w, h)
        a, a2 = L[4]
        b, b2 = L[16]
        c, c2 = L[64]

        def params(kind, lw, lh):
            if kind == 'down':
                o, wt = downsample_gauss_params(lw, lh)
            else:
                o, wt = glow_gauss_params(lw, lh)
            return {'pixelOffset': [(u, v, 0.0, 0.0) for u, v in o], 'pixelWeight': [(x, 0.0, 0.0, 0.0) for x in wt]}

        def blk(eff, key, vals=None):
            return blocks(eff, key, vals or {})

        def step(eff, drw, dst, srcs):
            fb, _t, lw, lh = dst
            binds = []
            for name, ti, typ, cname in eff.samplers:
                binds.append((name, srcs.get(cname, srcs.get('*'))))
            _run_pass(eff, fb, lw, lh, eng, drw, binds)

        hp, dn, bh, bv, acc = effs['highpass'], effs['down'], effs['blurh'], effs['blurv'], effs['accum']
        step(hp, blk(hp, 'hp'), a, {'samSource': hdr, 'samLumBuffer': lum_tex, '*': hdr})
        step(dn, blk(dn, ('down', b[2], b[3]), params('down', b[2], b[3])), b, {'*': a[1]})
        step(dn, blk(dn, ('down', c[2], c[3]), params('down', c[2], c[3])), c, {'*': b[1]})
        p64 = params('blur', c[2], c[3])
        step(bh, blk(bh, ('blur', c[2], c[3]), p64), c2, {'*': c[1]})
        step(bv, blk(bv, ('blur', c[2], c[3]), p64), c, {'*': c2[1]})
        step(acc, blk(acc, 'acc'), b2, {'samLowTarget': c[1], 'samHighTarget': b[1], '*': b[1]})
        p16 = params('blur', b[2], b[3])
        step(bh, blk(bh, ('blur', b[2], b[3]), p16), b, {'*': b2[1]})
        step(bv, blk(bv, ('blur', b[2], b[3]), p16), b2, {'*': b[1]})
        step(acc, blk(acc, 'acc'), a2, {'samLowTarget': b2[1], 'samHighTarget': a[1], '*': a[1]})
        p4 = params('blur', a[2], a[3])
        step(bh, blk(bh, ('blur', a[2], a[3]), p4), a, {'*': a2[1]})
        step(bv, blk(bv, ('blur', a[2], a[3]), p4), a2, {'*': a[1]})
        return a2[1]


def _run_pass(eff, fb, w, h, eng, drw, binds):
    """One engine effect over a whole target."""
    batch = _effect_quad(eff)
    with fb.bind():
        gpu.state.viewport_set(0, 0, w, h)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
        gpu.state.face_culling_set('NONE')
        sh = eff.shader
        sh.bind()
        sh.uniform_block('eng', eng)
        sh.uniform_block('drw', drw)
        for name, t in binds:
            sh.uniform_sampler(name, t)
        batch.draw(sh)


# ----------------------------------------------------------------------
# motion blur (sdaemon _init_motion_blur / _submit_motion_blur / _bind_motion_blur_params)
# ----------------------------------------------------------------------

class MotionBlur:
    """MotionBlurPrepare (half size: the tone-mapped frame, alpha = the
    motion-blur object id) then MotionBlur (8 taps along each pixel's
    screen velocity, from MotionBlurMatrix[object id]). postprocess blends
    the result over the frame by its alpha."""

    def __init__(self):
        self.size = None
        self.prep = None         # (fb, tex, w, h)
        self.out = None

    def _targets(self, w, h):
        hw, hh = max(1, w // 2), max(1, h // 2)
        if self.size != (hw, hh):
            pair = []
            for _ in range(2):
                t = gpu.types.GPUTexture((hw, hh), format='RGBA16F')
                pair.append((gpu.types.GPUFrameBuffer(color_slots=t), t, hw, hh))
            self.prep, self.out = pair
            self.size = (hw, hh)
        return self.prep, self.out

    def run(self, prep_eff, mb_eff, prep_block, mb_block, hdr, lum_tex, ids, depth, eng):
        """-> the half-size motion-blur texture (rgb blurred colour, a amount)."""
        prep, out = self._targets(*_size_of(hdr))
        srcs = {'samColorBuffer': hdr, 'samLumBuffer': lum_tex, 'samStencilBuffer': ids, 'samDepthBuffer': depth}
        _run_pass(prep_eff, prep[0], prep[2], prep[3], eng, prep_block,
                  [(n, srcs.get(c, hdr)) for n, _t, _y, c in prep_eff.samplers])
        srcs = {'samColorBuffer': prep[1], 'samDepthViewHalfBuffer': depth}
        _run_pass(mb_eff, out[0], out[2], out[3], eng, mb_block,
                  [(n, srcs.get(c, prep[1])) for n, _t, _y, c in mb_eff.samplers])
        return out[1]


def _size_of(tex):
    return tex.width, tex.height
