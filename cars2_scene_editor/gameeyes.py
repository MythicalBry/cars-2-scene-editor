"""renderedEyeTexture: the eye image a car's eye material samples.

Cars 2 doesn't draw a character's eyes with the eye mesh's own material:
each frame it first renders the eyes into a small texture
(renderedEyeTexture), which the car_eyes_* / car_double_eyes_* effect then
samples like any other map. The image is built on the CPU from the
material's eye parameters (the eye rig drives them: lids, look offset), in
this order (sdaemon.exe 0x4B3EF0 and its helpers, as transcribed by the
Cars 2 Toolkit's c2_model.build_eye_graph):

  1. clear to the sclera colour (alpha 1)
  2. the two eyeballs: quads textured with eyeBallTexture (eyeball.tfx,
     alpha blended, the target's alpha kept)
  3. the outline band: the top lid again, 0.04 lower, in a solid grey
  4. the lids (eyelids.tfx writes (0, 0, 0, 0); eyelidstextured.tfx writes
     (diffuseMapA1.rgb, 0) with the game's lid texture coordinates)

so alpha 0 marks the lids, where the eye effect shows its lid colour.
Coordinates: x across the strip 0..1, y down it 0..1; parameters are
percents. The geometry is evaluated per pixel here (one full-target pass)
instead of being drawn as triangle strips.

The two colours the game sets at run time (sclera, outline grey) aren't in
the files; they are the toolkit's calibrated values.
"""

import gpu
from gpu_extras.batch import batch_for_shader

SIZE = (512, 256)
SCLERA = 0.95
LINE_DARKNESS = 0.45
LINE_OFFSET = 0.04          # the outline copy of the top lid, in strip heights
EYE_EFFECTS = ('car_eyes', 'car_double_eyes')
MAX_TOP, MAX_BOTTOM = 6, 4

_TYPEDEF = '''
struct EyeBlock {
  vec4 seg[%d];
  vec4 eyes;    /* left cx, right cx, left cy, right cy */
  vec4 size;    /* eyeball half width, half height, sclera, outline offset */
  vec4 flags;   /* lid texture, -, top segment count, bottom segment count */
  vec4 modes;   /* eyeball texture mode, outline grey, lid texture mode, lid notch x */
};
''' % (2 * (MAX_TOP + MAX_BOTTOM))

_VS = '''
void main() {
  v_uv = pos * 0.5 + 0.5;
  gl_Position = vec4(pos, 0.0, 1.0);
}
'''

_FS = '''
vec4 conv(vec4 c, float mode) {
  /* a texture sampled as the game does (1: decode sRGB, 2: encode) */
  if (mode > 1.5) return vec4(pow(max(c.rgb, 0.0), vec3(1.0 / 2.2)), c.a);
  if (mode > 0.5) return vec4(pow(max(c.rgb, 0.0), vec3(2.2)), c.a);
  return c;
}

float lid_curve(int first, int n, float x) {
  /* x linear in t, y a cubic Hermite in t with the raw angles as tangents;
     the notch segments are straight */
  float r = 0.0;
  for (int i = 0; i < n; i++) {
    vec4 a = eye.seg[first + 2 * i];
    vec4 b = eye.seg[first + 2 * i + 1];
    float t = clamp((x - a.x) / max(a.z - a.x, 1e-5), 0.0, 1.0);
    float h;
    if (b.z > 0.5) {
      h = a.y + (a.w - a.y) * t;
    } else {
      float t2 = t * t;
      float t3 = t2 * t;
      h = (2.0 * t3 - 3.0 * t2 + 1.0) * a.y + (t3 - 2.0 * t2 + t) * b.x +
          (-2.0 * t3 + 3.0 * t2) * a.w + (t3 - t2) * b.y;
    }
    if (i == 0 || x > a.x) r = h;
  }
  return r;
}

void main() {
  float x = v_uv.x;
  float y = 1.0 - v_uv.y;
  vec4 col = vec4(vec3(eye.size.z), 1.0);
  for (int k = 0; k < 2; k++) {
    float cx = (k == 0) ? eye.eyes.x : eye.eyes.y;
    float cy = (k == 0) ? eye.eyes.z : eye.eyes.w;
    vec2 d = vec2((x - cx) / (2.0 * eye.size.x) + 0.5, (y - cy) / (2.0 * eye.size.y) + 0.5);
    if (d.x >= 0.0 && d.x <= 1.0 && d.y >= 0.0 && d.y <= 1.0) {
      vec4 t = conv(texture(eyeTex, vec2(d.x, 1.0 - d.y)), eye.modes.x);
      col.rgb = mix(col.rgb, t.rgb, t.a);
    }
  }
  float top_y = lid_curve(0, int(eye.flags.z), x);
  float bot_y = lid_curve(%d, int(eye.flags.w), x);
  if (y < top_y + eye.size.w) {
    col = vec4(vec3(eye.modes.y), 1.0);
  }
  if (y < top_y || y > bot_y) {
    if (eye.flags.x > 0.5) {
      float c = eye.modes.w;
      float lu = (x < c) ? 0.5 / max(c, 1e-4) * x : (x - c) / max(1.0 - c, 1e-4) * 0.5 + 0.5;
      float tv = (y > top_y) ? 0.5 * (y - bot_y) : 1.0 - 0.5 * (top_y - y);
      col = vec4(conv(texture(lidTex, vec2(lu, 1.0 - tv)), eye.modes.z).rgb, 0.0);
    } else {
      col = vec4(0.0);
    }
  }
  fragColor = col;
}
''' % (2 * MAX_TOP)

_shader = [None]
_batch = [None]


def _get_shader():
    if _shader[0] is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.typedef_source(_TYPEDEF)
        info.uniform_buf(0, 'EyeBlock', 'eye')
        info.vertex_in(0, 'VEC2', 'pos')
        iface = gpu.types.GPUStageInterfaceInfo('c2_eye_iface')
        iface.smooth('VEC2', 'v_uv')
        info.vertex_out(iface)
        info.sampler(0, 'FLOAT_2D', 'eyeTex')
        info.sampler(1, 'FLOAT_2D', 'lidTex')
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source(_VS)
        info.fragment_source(_FS)
        _shader[0] = gpu.shader.create_from_info(info)
        _batch[0] = batch_for_shader(_shader[0], 'TRI_STRIP', {'pos': [(-1, -1), (1, -1), (-1, 1), (1, 1)]})
    return _shader[0]


def is_eye_effect(effect):
    return (effect or '').lower().startswith(EYE_EFFECTS)


def layout(P, double):
    """Eye geometry from the parameters (P(name) -> value): (eyeball centres
    and half sizes, top lid segments, bottom lid segments, notch x). A
    segment is ((x0, y0), (x1, y1), tangent0, tangent1), tangents None for a
    straight notch segment."""
    p = lambda n: P(n) * 0.01
    half_w, half_h = 0.25 * P('eyeBallScale_u'), 0.5 * P('eyeBallScale_v')
    if double:
        cx = (0.25 + 0.25 * p('eyeBall_left_offset_u'), 0.75 + 0.25 * p('eyeBall_right_offset_u'))
        cy = (0.5 - 0.5 * p('eyeBall_left_offset_v'), 0.5 - 0.5 * p('eyeBall_right_offset_v'))
    else:
        sp = P('eyeBallSpacing_u')
        shift = (1.0 - sp) * 0.5 * p('eyeBallOffset_u')
        cx = (0.5 + shift - 0.5 * sp, 0.5 + shift + 0.5 * sp)
        cy = (0.5 - 0.5 * p('eyeBallOffset_v'),) * 2

    lo = 0.0 if double else p('eyeLidOffset_u')
    to01 = lambda X: X * 0.5 + 0.5
    yd = lambda n: 0.5 - 0.5 * p(n)
    if double:
        x_nl = x_nr = 0.0
    else:
        x_nl = lo + (1.0 + lo) * p('eyeLidTop_notch_leftPos_u')
        x_nr = (1.0 - lo) * p('eyeLidTop_notch_rightPos_u') + lo
    x_lm = (p('eyeLidTop_left_middlePercent_u') * 0.5 + 0.5) * (x_nl + 1.0) - 1.0
    x_rm = (p('eyeLidTop_right_middlePercent_u') * 0.5 + 0.5) * (1.0 - x_nr) + x_nr
    x_blm = (p('eyeLidBottom_left_middlePercent_u') * 0.5 + 0.5) * (lo + 1.0) - 1.0
    x_brm = (p('eyeLidBottom_right_middlePercent_u') * 0.5 + 0.5) * (1.0 - lo) + lo
    bot_nl = 'eyeLidBottom_notch_leftPos_v' if double else 'eyeLidBottom_notchPos_v'
    bot_nr = 'eyeLidBottom_notch_rightPos_v' if double else 'eyeLidBottom_notchPos_v'
    top = [((0.0, yd('eyeLidTop_left_leftPos_v')), (to01(x_lm), yd('eyeLidTop_left_middlePos_v')),
            -P('eyeLidTop_left_leftAngle'), -P('eyeLidTop_left_middleAngle')),
           ((to01(x_lm), yd('eyeLidTop_left_middlePos_v')), (to01(x_nl), yd('eyeLidTop_notch_leftPos_v')),
            -P('eyeLidTop_left_middleAngle'), P('eyeLidTop_left_rightAngle'))]
    if not double:
        top += [((to01(x_nl), yd('eyeLidTop_notch_leftPos_v')), (to01(lo), yd('eyeLidTop_notchPos_v')), None, None),
                ((to01(lo), yd('eyeLidTop_notchPos_v')), (to01(x_nr), yd('eyeLidTop_notch_rightPos_v')), None, None)]
    top += [((to01(x_nr), yd('eyeLidTop_notch_rightPos_v')), (to01(x_rm), yd('eyeLidTop_right_middlePos_v')),
             -P('eyeLidTop_right_leftAngle'), P('eyeLidTop_right_middleAngle')),
            ((to01(x_rm), yd('eyeLidTop_right_middlePos_v')), (1.0, yd('eyeLidTop_right_rightPos_v')),
             P('eyeLidTop_right_middleAngle'), P('eyeLidTop_right_rightAngle'))]
    bottom = [((0.0, yd('eyeLidBottom_left_leftPos_v')), (to01(x_blm), yd('eyeLidBottom_left_middlePos_v')),
               -P('eyeLidBottom_left_leftAngle'), -P('eyeLidBottom_left_middleAngle')),
              ((to01(x_blm), yd('eyeLidBottom_left_middlePos_v')), (to01(lo), yd(bot_nl)),
               -P('eyeLidBottom_left_middleAngle'), P('eyeLidBottom_left_rightAngle')),
              ((to01(lo), yd(bot_nr)), (to01(x_brm), yd('eyeLidBottom_right_middlePos_v')),
               -P('eyeLidBottom_right_leftAngle'), P('eyeLidBottom_right_middleAngle')),
              ((to01(x_brm), yd('eyeLidBottom_right_middlePos_v')), (1.0, yd('eyeLidBottom_right_rightPos_v')),
               P('eyeLidBottom_right_middleAngle'), P('eyeLidBottom_right_rightAngle'))]
    return (cx, cy, half_w, half_h), top, bottom, to01(lo)


def block_values(P, double, lid_textured, eye_mode, lid_mode):
    """The EyeBlock uniform values (list of 4-float rows)."""
    (cx, cy, hw, hh), top, bottom, notch = layout(P, double)
    rows = []
    for segs, cap in ((top, MAX_TOP), (bottom, MAX_BOTTOM)):
        for k in range(cap):
            if k < len(segs):
                (x0, y0), (x1, y1), m0, m1 = segs[k]
                rows.append((x0, y0, x1, y1))
                rows.append((m0 or 0.0, m1 or 0.0, 1.0 if m0 is None else 0.0, 0.0))
            else:
                rows += [(0.0,) * 4, (0.0,) * 4]
    rows.append((cx[0], cx[1], cy[0], cy[1]))
    rows.append((max(hw, 1e-5), max(hh, 1e-5), SCLERA, LINE_OFFSET))
    rows.append((1.0 if lid_textured else 0.0, 0.0, float(len(top)), float(len(bottom))))
    rows.append((float(eye_mode), SCLERA * (1.0 - LINE_DARKNESS), float(lid_mode), notch))
    return rows


class EyeTextures:
    """One render target per eye material, redrawn when its values change."""

    def __init__(self):
        self.targets = {}       # material name -> (key, GPUOffScreen, GPUUniformBuf)

    def clear(self):
        self.targets.clear()

    def texture(self, key, name, rows, eye_tex, lid_tex):
        """The eye image of material `name` for these block rows (redrawn when
        `key` changes)."""
        import numpy as np
        hit = self.targets.get(name)
        if hit is not None and hit[0] == key:
            return hit[1]
        if hit is not None:
            tex, fb, ubo = hit[1:]
        else:
            tex = gpu.types.GPUTexture(SIZE, format='RGBA8')
            fb = gpu.types.GPUFrameBuffer(color_slots=tex)
            ubo = None
        data = np.asarray(rows, np.float32).ravel()
        buf = gpu.types.Buffer('FLOAT', data.size, data)
        if ubo is None:
            ubo = gpu.types.GPUUniformBuf(buf)
        else:
            ubo.update(buf)
        sh = _get_shader()
        vp = gpu.state.viewport_get()
        blend = gpu.state.blend_get()
        depth = gpu.state.depth_test_get()
        try:
            with fb.bind():
                gpu.state.viewport_set(0, 0, SIZE[0], SIZE[1])
                gpu.state.blend_set('NONE')
                gpu.state.depth_test_set('NONE')
                sh.bind()
                sh.uniform_block('eye', ubo)
                sh.uniform_sampler('eyeTex', eye_tex)
                sh.uniform_sampler('lidTex', lid_tex)
                _batch[0].draw(sh)
        finally:
            gpu.state.viewport_set(*vp)
            gpu.state.blend_set(blend)
            gpu.state.depth_test_set(depth)
        self.targets[name] = (key, tex, fb, ubo)
        return tex
