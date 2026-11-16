"""Dynamic shadows for the Cars 2 Game render engine, done the way the game does them.

What sdaemon.exe does:
  * two shadow casters, each a depth map of the scene parts whose SubGeometry
    RenderCaps contain its bit:
      dynamic  bit 0x40000  1024 px, 2 cascades stacked vertically (hi-res: 2048 px, 3)
      static   bit 0x4000    256 px, 1 cascade, far 150      (hi-res: 2048 px)
    In the shipped worlds the casters are the invisible `shadowonly` proxy
    parts plus a few moving props; cars and characters cast into the dynamic map.
    Ordinary world geometry casts nothing: its shadows are baked.
  * cascade splits: near 0.1, far = the shadow distance, the "practical"
    split (FUN_006356d0): lerp(linear, logarithmic, 0.5).
  * a full-screen mask pass, shadowmask_c2_rot8_nodb_ss_cs (hi-res: _c3_),
    run here from its own bytecode like every other effect: it rebuilds each
    pixel's world position from the linear view depth (depthViewBuffer),
    filters the cascades with 8 rotated taps (x), tests the static map (y)
    and subtracts the scrolling cloud texture from x.
  * surface shaders read the mask (samShadowMask) at their screen position:
    baked surfaces darken towards g_ShadowBlobColor by x, run-time lit ones
    (cars, characters) multiply their sun light by x * y.

The linear depth comes from a depth pre-pass of the opaque parts (the game
draws one too); it is also what soft particles read (samDepthViewBuffer).
"""
import math

import gpu
import numpy as np
from mathutils import Matrix, Vector

import fxglsl

DYNAMIC_BIT = 0x40000
STATIC_BIT = 0x4000
MASK_EFFECT = {False: 'shadowmask_c2_rot8_nodb_ss_cs', True: 'shadowmask_c3_rot8_nodb_ss_cs'}
SUN_DISTANCE = 2000.0        # the game places the sun camera this far back
CASTER_REACH = 500.0         # casters this far towards the sun still land in a cascade
DEPTH_BIAS = 0.0004          # g_LinearDepthBias, in shadow-map depth units

# depth-only drawing (pre-pass and shadow maps): position, and optionally
# an alpha test on the diffuse texture for cut-outs (leaves, fences)
DEPTH_VS = '''
void main()
{
  vec4 wp = world * vec4(a_Position, 1.0);
  vzv = (pass.v * wp).z;
  tc = a_Uv1;
  gl_Position = pass.vp * wp;
}
'''
DEPTH_FS = '''
void main()
{
  if (alpha.x > 0.5 && texture(tex, vec2(tc.x, 1.0 - tc.y)).a < alpha.y) discard;
  // mode 0: linear view depth (-z / far, the game's depthViewBuffer); 1: shadow-map depth
  fragColor = vec4(pass.p.y > 0.5 ? gl_FragCoord.z : -vzv / pass.p.x);
  // the motion-blur object id (the game's stencil value / 256), read as .z
  objId = vec4(alpha.z / 256.0);
}
'''
DEPTH_TYPEDEFS = '''
struct C2Pass {
  mat4 vp;
  mat4 v;
  vec4 p;
};
'''


def split_distance(i, near, far, n, bias=0.5):
    """Far end of cascade i-1 (the game's FUN_006356d0)."""
    t = float(i) / n
    log_ = near * (far / near) ** t
    lin = near + (far - near) * t
    return (log_ - lin) * bias + lin


def _gl_mat(m):
    """A Blender matrix as the 16 floats of a GLSL mat4 (column-major)."""
    return np.array(m, np.float32).T.ravel()


def _ortho(r, zn, zf):
    """GL orthographic projection of the box [-r, r]^2 x depth [zn, zf]."""
    return Matrix(((1.0 / r, 0, 0, 0), (0, 1.0 / r, 0, 0),
                   (0, 0, -2.0 / (zf - zn), -(zf + zn) / (zf - zn)), (0, 0, 0, 1)))


def _light_view(center, d):
    """Right-handed view matrix looking from center + d * SUN_DISTANCE along -d."""
    z = d.normalized()
    up = Vector((0.0, 1.0, 0.0)) if abs(z.y) < 0.99 else Vector((1.0, 0.0, 0.0))
    x = up.cross(z).normalized()
    y = z.cross(x)
    eye = center + z * SUN_DISTANCE
    rot = Matrix((x, y, z))
    m = rot.to_4x4()
    m.translation = -(rot @ eye)
    return m


class Cascade:
    """One shadow-map slot: GL view-projection for drawing, the D3D shadow
    matrix the shaders use, and its viewport in the texture."""

    def __init__(self, vp_gl, shadow_matrix, viewport):
        self.vp_gl = vp_gl
        self.shadow_matrix = shadow_matrix
        self.viewport = viewport


def fit_cascades(frame, game_view, d, near, far, count, size, slot_offset=0):
    """Cascades over the view frustum from near to far: each slice is wrapped
    in a sphere (stable while the camera turns), snapped to whole texels."""
    inv_p = frame.proj.inverted_safe()
    inv_v = game_view.inverted_safe()
    lines = []
    for x in (-1.0, 1.0):
        for y in (-1.0, 1.0):
            a = inv_p @ Vector((x, y, -1.0, 1.0))
            b = inv_p @ Vector((x, y, 1.0, 1.0))
            lines.append((a.xyz / a.w, b.xyz / b.w))
    out = []
    h = size // count
    for i in range(count):
        z0 = split_distance(i, near, far, count)
        z1 = split_distance(i + 1, near, far, count)
        pts = []
        for a, b in lines:
            da, db = -a.z, -b.z
            for z in (z0, z1):
                t = (z - da) / (db - da) if abs(db - da) > 1e-9 else 0.0
                pts.append(inv_v @ (a + (b - a) * t))
        c = sum(pts, Vector()) / len(pts)
        r = max((p - c).length for p in pts) * 1.02 + 0.5
        # snap the centre to whole texels in light space, so shadows don't crawl
        v0 = _light_view(Vector(), d)
        lc = v0 @ c
        texel = 2.0 * r / size
        lc.x = math.floor(lc.x / texel) * texel
        lc.y = math.floor(lc.y / texel) * texel
        c = v0.inverted_safe() @ lc
        V = _light_view(c, d)
        zn = max(0.1, SUN_DISTANCE - r - CASTER_REACH)
        zf = SUN_DISTANCE + r
        P = _ortho(r, zn, zf)
        vp_gl = P @ V
        k = i + slot_offset
        # world -> (u, v from the top, depth 0..1) in a texture holding `count` slots stacked vertically
        to_tex = Matrix(((0.5, 0, 0, 0.5), (0, -0.5 / count, 0, (0.5 + i) / count),
                         (0, 0, 0.5, 0.5), (0, 0, 0, 1)))
        out.append(Cascade(vp_gl, to_tex @ vp_gl, (0, size - (i + 1) * h, size, h)))
    return out


def cull(items, bounds, vp):
    """The casters whose bounding sphere reaches the cascade's square (its
    clip-space x / y; any depth: a caster between the sun and the slice still
    shadows it). bounds: (centres n x 3, radii n) in game space."""
    ctr, rad = bounds
    if not len(ctr):
        return []
    m = np.array(vp, np.float64)
    clip = ctr @ m[:3, :3].T + m[:3, 3]
    # an orthographic sun view: radius in clip units per axis
    sx = rad * np.linalg.norm(m[0, :3])
    sy = rad * np.linalg.norm(m[1, :3])
    keep = (np.abs(clip[:, 0]) <= 1.0 + sx) & (np.abs(clip[:, 1]) <= 1.0 + sy)
    return [it for it, k in zip(items, keep) if k]


class Shadows:
    """The shadow textures, the depth pre-pass and the mask pass."""

    def __init__(self):
        self.depth_shader = None
        self.pass_ubo = None
        self.view = None          # (w, h, fb, linear depth tex, depth)
        self.mask = None          # (w, h, fb, tex)
        self.maps = {}            # 'dynamic' / 'static' -> (size, fb, tex, depth)
        self.rotate = None
        self.cascades = {}
        self.stats = {}

    # -- resources --------------------------------------------------------------------
    def _shader(self):
        if self.depth_shader is None:
            info = gpu.types.GPUShaderCreateInfo()
            info.typedef_source(DEPTH_TYPEDEFS)
            info.uniform_buf(0, 'C2Pass', 'pass')
            info.vertex_in(0, 'VEC3', 'a_Position')
            info.vertex_in(1, 'VEC2', 'a_Uv1')
            iface = gpu.types.GPUStageInterfaceInfo('c2_depth_iface')
            iface.smooth('FLOAT', 'vzv')
            iface.smooth('VEC2', 'tc')
            info.vertex_out(iface)
            info.sampler(0, 'FLOAT_2D', 'tex')
            info.push_constant('MAT4', 'world')
            info.push_constant('VEC4', 'alpha')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.fragment_out(1, 'VEC4', 'objId')
            info.vertex_source(DEPTH_VS)
            info.fragment_source(DEPTH_FS)
            self.depth_shader = gpu.shader.create_from_info(info)
        return self.depth_shader

    def _pass(self, vp, v, far, shadow_mode):
        data = np.zeros(36, np.float32)
        data[0:16] = _gl_mat(vp)
        data[16:32] = _gl_mat(v)
        data[32:36] = (far, 1.0 if shadow_mode else 0.0, 0.0, 0.0)
        buf = gpu.types.Buffer('FLOAT', 36, data)
        if self.pass_ubo is None:
            self.pass_ubo = gpu.types.GPUUniformBuf(buf)
        else:
            self.pass_ubo.update(buf)
        return self.pass_ubo

    @staticmethod
    def _target(w, h, fmt):
        col = gpu.types.GPUTexture((w, h), format=fmt)
        dep = gpu.types.GPUTexture((w, h), format='DEPTH_COMPONENT32F')
        return gpu.types.GPUFrameBuffer(depth_slot=dep, color_slots=col), col, dep

    def _view_target(self, w, h):
        if self.view is None or self.view[:2] != (w, h):
            col = gpu.types.GPUTexture((w, h), format='R32F')
            dep = gpu.types.GPUTexture((w, h), format='DEPTH_COMPONENT32F')
            ids = gpu.types.GPUTexture((w, h), format='RGBA16F')   # motion-blur object ids
            fb = gpu.types.GPUFrameBuffer(depth_slot=dep, color_slots=(col, ids))
            self.view = (w, h, fb, col, dep, ids)
            fb2, col2, _d = self._target(w, h, 'RGBA16F')
            self.mask = (w, h, fb2, col2)
        return self.view

    def _map(self, key, size):
        m = self.maps.get(key)
        if m is None or m[0] != size:
            fb, col, dep = self._target(size, size, 'R32F')
            m = self.maps[key] = (size, fb, col, dep)
        return m

    def rotate_map(self):
        """The engine's random rotation texture (sin, cos per texel) for the rot8 filter."""
        if self.rotate is None:
            n = 32
            a = np.random.default_rng(7).uniform(0.0, 2.0 * math.pi, (n, n)).astype(np.float32)
            px = np.stack([np.sin(a), np.cos(a), np.zeros_like(a), np.ones_like(a)], -1)
            self.rotate = gpu.types.GPUTexture((n, n), format='RGBA16F',
                                               data=gpu.types.Buffer('FLOAT', px.size, px.ravel()))
            try:
                self.rotate.filter_mode(False)
            except (AttributeError, TypeError):
                pass
        return self.rotate

    # -- per frame --------------------------------------------------------------------
    def setup(self, frame, game_view, sun_dir, distance, hires):
        """Cascade matrices for this frame. Returns the engine values to add
        ({name: register rows}) and the map sizes."""
        d = Vector(sun_dir)
        if d.length < 1e-6:
            d = Vector((0.0, 1.0, 0.0))
        d.normalize()
        dyn_size, dyn_n = (2048, 3) if hires else (1024, 2)
        st_size = 2048 if hires else 256
        dyn = fit_cascades(frame, game_view, d, 0.1, max(1.0, distance), dyn_n, dyn_size)
        st = fit_cascades(frame, game_view, d, 0.1, 150.0, 1, st_size)
        self.cascades = {'dynamic': (dyn_size, dyn), 'static': (st_size, st)}
        mats = [c.shadow_matrix for c in dyn] + [Matrix.Identity(4)] * (3 - len(dyn)) + [st[0].shadow_matrix]
        rows = np.concatenate([np.array(m, np.float32)[:4] for m in mats])     # column-major packing
        return {
            'g_ShadowMatrix': rows,
            'ShadowMapDimensions': np.array([[dyn_size, dyn_size, 1.0 / dyn_size, 1.0 / dyn_size]], np.float32),
            'g_ShadowZScale': np.array([[1.0, 1.0, 1.0, 0.0]], np.float32),
            'g_LinearDepthBias': np.array([[DEPTH_BIAS, 0, 0, 0]], np.float32),
        }

    def _draw_depth(self, items, ubo, white):
        """items: [(batch, world gl floats, (texture or None, alpha ref))]"""
        sh = self._shader()
        sh.bind()
        sh.uniform_block('pass', ubo)
        sh.uniform_sampler('tex', white)
        last_tex, last_alpha, last_world = white, None, None
        for it in items:
            batch, world, (tex, ref) = it[:3]
            mid = it[3] if len(it) > 3 else 0
            if world is not last_world:         # parts of one object share it
                sh.uniform_float('world', world)
                last_world = world
            alpha = (1.0 if tex is not None else 0.0, ref if tex is not None else 0.0, float(mid), 0.0)
            if alpha != last_alpha:
                sh.uniform_float('alpha', alpha)
                last_alpha = alpha
            if tex is not None and tex is not last_tex:
                sh.uniform_sampler('tex', tex)
                last_tex = tex
            batch.draw(sh)

    def prepass(self, frame, game_view, items, white):
        """Linear view depth of the opaque parts -> the depthViewBuffer texture."""
        w, h, fb, col, dep, _ids = self._view_target(frame.width, frame.height)
        P = frame.proj
        ubo = self._pass(P @ game_view, game_view, frame.far, False)
        st = gpu.state
        with fb.bind():
            st.viewport_set(0, 0, w, h)
            fb.clear(color=(1.0, 0.0, 0.0, 0.0), depth=1.0)   # far depth, motion id 0
            st.depth_test_set('LESS_EQUAL')
            st.depth_mask_set(True)
            st.face_culling_set('NONE')
            st.blend_set('NONE')
            self._draw_depth(items, ubo, white)
            st.depth_test_set('NONE')
            st.depth_mask_set(False)
        return col

    def render_maps(self, casters, white):
        """Shadow depth of the casters into the dynamic and static maps.
        casters: {'dynamic': items, 'static': items}"""
        st = gpu.state
        out = {}
        for key in ('dynamic', 'static'):
            size, cascades = self.cascades[key]
            _s, fb, col, _dep = self._map(key, size)
            with fb.bind():
                st.viewport_set(0, 0, size, size)
                fb.clear(color=(1.0, 1.0, 1.0, 1.0), depth=1.0)
                st.depth_test_set('LESS_EQUAL')
                st.depth_mask_set(True)
                st.face_culling_set('NONE')
                st.blend_set('NONE')
                items = casters.get(key) or []
                bounds = casters.get('bounds', {}).get(key)
                drawn = 0
                for c in cascades:
                    if not items:
                        break
                    ubo = self._pass(c.vp_gl, Matrix.Identity(4), 1.0, True)
                    st.viewport_set(*c.viewport)
                    sel = items if bounds is None else cull(items, bounds, c.vp_gl)
                    drawn += len(sel)
                    self._draw_depth(sel, ubo, white)
                self.stats[key] = drawn
                st.depth_test_set('NONE')
                st.depth_mask_set(False)
            out[key] = col
        return out

    def mask_pass(self, eff, frame, game_view, eng, drw, binds):
        """Run the game's shadowmask effect over the screen into the mask texture."""
        w, h, fb, col = self.mask
        # frustum vectors: camera -> far-plane corner (view z = -far), in game world space
        inv_p = frame.proj.inverted_safe()
        rot = game_view.inverted_safe().to_3x3()
        corners = {}
        for x in (-1.0, 1.0):
            for y in (-1.0, 1.0):
                f = inv_p @ Vector((x, y, 1.0, 1.0))
                f = f.xyz / f.w
                f *= frame.far / max(1e-6, -f.z)
                corners[(x, y)] = rot @ f
        fr = np.array([corners[(-1, -1)], corners[(1, -1)], corners[(1, 1)], corners[(-1, 1)]], np.float32)
        batch = self._quad_batch(eff, fr)      # 4 vertices: rebuilt per frame (a drawn buffer can't be refilled)
        st = gpu.state
        with fb.bind():
            st.viewport_set(0, 0, w, h)
            fb.clear(color=(1.0, 1.0, 1.0, 1.0))
            st.depth_test_set('NONE')
            st.face_culling_set('NONE')
            st.blend_set('NONE')
            sh = eff.shader
            sh.bind()
            sh.uniform_block('eng', eng)
            sh.uniform_block('drw', drw)
            for name, tex in binds:
                sh.uniform_sampler(name, tex)
            batch.draw(sh)
        return col

    @staticmethod
    def _quad_batch(eff, frustum):
        """Full-screen quad with the mask effect's inputs: clip-space position,
        the frustum vectors and the screen uv (v from the top)."""
        names = {el: a for a, el, n in eff.sh.attrs}
        sizes = {el: n for a, el, n in eff.sh.attrs}
        fmt = gpu.types.GPUVertFormat()
        for a, el, n in eff.sh.attrs:
            fmt.attr_add(id=a, comp_type='F32', len=n, fetch_mode='FLOAT')
        vbo = gpu.types.GPUVertBuf(fmt, 4)
        pos = np.array([(-1, -1, 0, 1), (1, -1, 0, 1), (1, 1, 0, 1), (-1, 1, 0, 1)], np.float32)
        uv = np.array([(0, 1, 0, 0), (1, 1, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0)], np.float32)
        for el, a in names.items():
            n = sizes[el]
            if el == 'Position':
                vbo.attr_fill(id=a, data=np.ascontiguousarray(pos[:, :n]))
            elif el == 'Normal':
                f = np.zeros((4, n), np.float32)
                f[:, :3] = frustum
                vbo.attr_fill(id=a, data=f)
            else:
                vbo.attr_fill(id=a, data=np.ascontiguousarray(uv[:, :n]))
        return gpu.types.GPUBatch(type='TRI_FAN', buf=vbo)
