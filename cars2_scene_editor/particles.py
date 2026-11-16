"""Particle effects in choreography previews (the tokyo_b fireworks ...).

A choreography package's .mer starts particles with two node types:

* ParticleEffect (effect_name): one burst of an effect each time an Impulse
  link fires it, at the locator its TransformFetch link names (LocPrtcl_*).
  The impulses come from tickers: an EventList in the package's network
  (<anchor>/<ticker>/InputImpulse/EventList_EventListData) lists their times.
* ParticleEmitter (emitter_name): starts its effect `rate` times a second
  while its Surge link is on (the ticker's InputSurge Bool, keyed by the clip).

The names resolve through the world's particle library
(assets/particles/<world>_d1.zip, then universal.zip; core/pigpen.py). Each
effect starts a few particle systems; each particle is a camera-facing quad
with the system's texture, colour (start -> end, or the colour curves), size
and alpha curves, lifetime, speed and direction, simulated here from the
preview's time - nothing is baked into the scene and nothing is exported.
Drawn by the Cars 2 Game renderer (in its HDR target, after the blended
parts) and over the viewport for the other engines; not in F12 renders of
Eevee / Cycles. Randomisation is approximate (the game's random-number stream
is not reproduced), so bursts land at the right place and time, not on the
same pixels.
"""
import math
import os
import zipfile

import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

import pigpen

SOURCES = []            # [Source] of the previews on screen
_libs = {}              # zip path -> pigpen.Library (or None when it can't be read)
_tex = {}               # (zip path, texture index) -> gpu texture
_images = {}            # (zip path, texture index) -> bpy image name
_shader = [None]
_handler = [None]

VS = '''
void main()
{
  tc = uv;
  vc = col;
  gl_Position = vp * vec4(pos, 1.0);
}
'''
FS = '''
void main()
{
  vec4 c = texture(tex, vec2(tc.x, 1.0 - tc.y)) * vc;
  if (mode == 1) {
    if (c.a < 0.5) discard;
    fragColor = vec4(c.rgb * scale, 1.0);
  } else {
    fragColor = vec4(c.rgb * c.a * scale, c.a);
  }
}
'''


def libraries(assets, world):
    """The particle libraries a world uses: its own (detail level 1), then universal."""
    out = []
    for stem in ('%s_d1' % (world or '').lower(), 'universal'):
        zp = os.path.join(assets or '', 'particles', stem + '.zip')
        if zp not in _libs:
            lib = None
            if os.path.exists(zp):
                try:
                    lib = pigpen.Library(zp)
                except (ValueError, KeyError, zipfile.BadZipFile, OSError) as e:
                    print('Cars 2 particles: %s not read (%s)' % (zp, e))
            _libs[zp] = lib
        if _libs[zp] is not None:
            out.append(_libs[zp])
    return out


def _find(libs, kind, name):
    key = (name or '').strip().lower()
    for lib in libs:
        hit = (lib.effect_by_name if kind == 'effect' else lib.emitter_by_name).get(key)
        if hit is not None:
            return lib, hit
    return None, None


class Source:
    """One ParticleEffect / ParticleEmitter of a preview."""

    def __init__(self, lib, effect, obj, spawns, emitter=None):
        self.lib = lib
        self.effect = effect
        self.obj = obj.name
        self.ref = obj                  # direct reference (name lookups cost ~0.13 ms each)
        self.spawns = spawns          # [seconds] the effect starts at
        self.emitter = emitter
        self.duration = 0.0
        self.loop = False
        self.instances = [Matrix.Identity(4)]
        self.root3 = Matrix.Identity(3)
        self.seed = 0
        self.holder = ''


def attach(context, ch, pkg, objs, item, report):
    """Particle sources of a choreography preview (objs: scene node index ->
    preview object). Returns how many."""
    from . import common
    import c2oct
    import meridian
    scene = context.scene
    libs = libraries(common.game_assets(scene), scene.c2.world_name)
    mer = next((n for n in pkg.names() if n.lower() == pkg.stem.lower() + '.mer'), None)
    if mer is None:
        return 0
    try:
        g = meridian.Graph(c2oct.decode(pkg.read(mer))[0])
    except (ValueError, KeyError) as e:
        report.append('particles: %s not read (%s)' % (mer, e))
        return 0
    kinds = {i: n.get('Type') for i, n in enumerate(g.nodes)}
    if not any(k in ('ParticleEffect', 'ParticleEmitter') for k in kinds.values()):
        return 0
    if not libs:
        report.append('particles: no particle library for %s in assets/particles' % scene.c2.world_name)
        return 0
    by_name = {}
    for i, o in objs.items():
        if i > 0:
            by_name.setdefault(ch.scene.nodes[i].get('NodeName', ''), o)
    sn = next(iter(ch.tree.get('SubNetworkPool', {}).values()), {})
    nb = sn.get('NetworkBuffer', b'')
    anc = ch.paths[0].split('/')[0] if ch.paths else ''
    fps = scene.render.fps / scene.render.fps_base
    duration = ch.duration or 1.0
    root = scene.c2.root
    root3 = root.matrix_world.to_3x3() if root is not None else Matrix.Rotation(math.pi / 2, 3, 'X')

    def ticker_of(origin):
        """The ticker group (NodeGroup name) an Event / EventSurge belongs to."""
        n = g.nodes[origin]
        name = n.get('AuthoredName', '')
        return name.split(' / ')[0]

    def impulse_times(group):
        di = ch.path_index.get('%s/%s/InputImpulse/EventList_EventListData' % (anc, group))
        if di is None or 'NetworkBufferOffset' not in ch.dn[di]:
            return []
        try:
            return pigpen.event_list(nb, ch.dn[di]['NetworkBufferOffset'])
        except Exception:
            return []

    def surge_on(group):
        """[(start, end)] seconds the surge is on."""
        path = '%s/%s/InputSurge' % (anc, group)
        if path not in ch.path_index:
            return [(0.0, duration)]
        out, start = [], None
        steps = max(1, int(math.ceil(duration * fps)))
        for k in range(steps + 1):
            t = k / fps
            on = (ch.value(path, ch.evaluate(t), 0.0) or 0.0) >= 0.5
            if on and start is None:
                start = t
            elif not on and start is not None:
                out.append((start, t))
                start = None
        if start is not None:
            out.append((start, duration))
        return out

    made, missing = [], set()
    for ni, ty in kinds.items():
        if ty not in ('ParticleEffect', 'ParticleEmitter'):
            continue
        node = g.nodes[ni]
        locs, trig = [], []
        for l in g.links:
            if l.get('Target') != ni:
                continue
            if l.get('Type') == 'TransformFetch':
                loc = g.locator_of_transform_source(l['Origin'])
                if loc is not None:
                    locs.append(g.nodes[loc].get('BoundObjectName', '').split('|')[-1])
            elif l.get('Type') in ('Impulse', 'Surge'):
                trig.append((l['Type'], ticker_of(l['Origin'])))
        obj = next((by_name[n] for n in locs if n in by_name), None)
        if obj is None:
            continue
        if ty == 'ParticleEffect':
            lib, eff = _find(libs, 'effect', node.get('effect_name'))
            if eff is None:
                missing.add(node.get('effect_name'))
                continue
            spawns = sorted(t for kind, grp in trig if kind == 'Impulse' for t in impulse_times(grp))
            src = Source(lib, eff, obj, spawns)
        else:
            lib, em = _find(libs, 'emitter', node.get('emitter_name'))
            if em is None or not 0 <= em.effect < len(lib.effects):
                missing.add(node.get('emitter_name'))
                continue
            spans = [s for kind, grp in trig if kind == 'Surge' for s in surge_on(grp)] or [(0.0, duration)]
            spawns = []
            step = 1.0 / em.rate if em.rate > 0 else duration
            for a, b in spans:
                t = a
                while t < b:
                    spawns.append(t)
                    t += step
            src = Source(lib, lib.effects[em.effect], obj, spawns, em)
        src.duration, src.loop, src.root3 = duration, bool(item.loop), root3
        src.seed = len(SOURCES) * 7919 + ni
        src.holder = objs[0].name
        SOURCES.append(src)
        made.append(src)
        _load_images(src)
    if missing:
        report.append('particles: %s not in the particle library' % ', '.join(sorted(str(m) for m in missing)))
    if made:
        report.append('%d particle effects (%s)' % (len(made), ', '.join(sorted({s.effect.name for s in made}))))
        ensure_handler()
    return len(made)


def add_instances(mats, holder):
    """The preview of `holder` is also played at these places (collection instances)."""
    for s in SOURCES:
        if s.holder == holder:
            s.instances = [Matrix.Identity(4)] + [m.copy() for m in mats]


def clear():
    SOURCES.clear()


# ----------------------------------------------------------------------
# simulation
# ----------------------------------------------------------------------

def particles(scene, t):
    """{(library, texture index, blend): (positions, (half w, half h), spin, rgba)}
    of the particles alive `t` seconds into the preview (Blender world space)."""
    out = {}
    for s in SOURCES:
        try:
            base = np.array(s.ref.matrix_world.translation, np.float32)
        except ReferenceError:          # undo re-creates objects: find it again by name
            s.ref = bpy.data.objects.get(s.obj)
            if s.ref is None:
                continue
            base = np.array(s.ref.matrix_world.translation, np.float32)
        tt = t % s.duration if s.loop and s.duration > 0 else t
        starts = [(k, x) for k, x in enumerate(s.spawns)]
        if s.loop:
            starts += [(k + 10000, x - s.duration) for k, x in enumerate(s.spawns)]
        r3 = np.array(s.root3, np.float32)
        for k, ts in starts:
            if ts > tt:
                continue
            rng = np.random.default_rng((s.seed, k))
            off = np.zeros(3, np.float32)
            if s.emitter is not None and s.emitter.radius > 0:
                v = rng.normal(size=3)
                off = (v / max(np.linalg.norm(v), 1e-6) * s.emitter.radius * rng.random()).astype(np.float32)
            for si, delay, spread, count, var, chance in s.effect.entries:
                if rng.random() > chance or not 0 <= si < len(s.lib.systems):
                    continue
                sy = s.lib.systems[si]
                n = max(1, count + int(rng.integers(-var, var + 1)) if var else count)
                life = sy.life if sy.life > 0 else s.duration
                for p in range(n):
                    born = ts + delay + (spread * rng.random() if spread > 0 else 0.0)
                    age = tt - born
                    if age < 0.0 or age > life:
                        continue
                    x = age / life
                    d = sy.direction.copy()
                    if sy.cone > 0:
                        d = d + rng.normal(size=3).astype(np.float32) * math.sin(min(sy.cone, math.pi) * 0.5)
                    nd = np.linalg.norm(d)
                    vel = (d / nd * sy.speed) if nd > 1e-6 else np.zeros(3, np.float32)
                    pos = base + r3 @ (off + vel * age)
                    size = float(sy.curves[0](x))
                    if sy.flags & 0x20:         # one size for both axes
                        size_y, half_y = size, sy.size
                    else:
                        size_y, half_y = float(sy.curves[1](x)), sy.size_y
                    if sy.curve_colour:
                        rgb = np.array([sy.curves[c](x) for c in (2, 3, 4)], np.float32)
                    else:
                        rgb = sy.color0 + (sy.color1 - sy.color0) * x
                    a = float(np.clip(sy.curves[5](x), 0.0, 1.0))
                    hw = max(size, 0.0) * sy.size
                    hh = max(size_y, 0.0) * half_y
                    tex, tint, blend = s.lib.material(sy.material)
                    if tex is None or a <= 0.0 or hw <= 0.0:
                        continue
                    rot = sy.spin * age + rng.random() * 2.0 * math.pi * bool(sy.flags & 0x40)
                    col = (rgb[0] * tint[0], rgb[1] * tint[1], rgb[2] * tint[2], a)
                    lst = out.setdefault((s.lib.zpath, tex, blend), ([], [], [], []))
                    for M in s.instances:
                        lst[0].append(np.array(M @ Vector(pos.tolist()), np.float32))
                        lst[1].append((hw, hh))
                        lst[2].append(rot)
                        lst[3].append(col)
    return out


# ----------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------

def _get_shader():
    if _shader[0] is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, 'VEC3', 'pos')
        info.vertex_in(1, 'VEC2', 'uv')
        info.vertex_in(2, 'VEC4', 'col')
        iface = gpu.types.GPUStageInterfaceInfo('c2_part_iface')
        iface.smooth('VEC2', 'tc')
        iface.smooth('VEC4', 'vc')
        info.vertex_out(iface)
        info.push_constant('MAT4', 'vp')
        info.push_constant('FLOAT', 'scale')
        info.push_constant('INT', 'mode')
        info.sampler(0, 'FLOAT_2D', 'tex')
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source(VS)
        info.fragment_source(FS)
        _shader[0] = gpu.shader.create_from_info(info)
    return _shader[0]


def _load_images(src):
    """Load the textures a source draws with as Blender images. Called when the
    preview is built: Blender refuses to add images from inside a viewport
    draw (the render engine's view_draw, a draw handler)."""
    lib = src.lib
    for si, *_ in src.effect.entries:
        if not 0 <= si < len(lib.systems):
            continue
        ti = lib.material(lib.systems[si].material)[0]
        key = (lib.zpath, ti)
        if ti is None or bpy.data.images.get(_images.get(key, '')) is not None:
            continue
        try:
            name, data, _frames = lib.texture_bytes(ti)
            folder = os.path.join(bpy.app.tempdir or os.environ.get('TEMP', '.'), 'c2_particles',
                                  os.path.basename(lib.zpath)[:-4])
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, '%s.dds' % name)
            if not os.path.exists(path):
                with open(path, 'wb') as f:
                    f.write(data)
            img = bpy.data.images.load(path, check_existing=True)
            img['c2_particle'] = True
            _images[key] = img.name
            _tex.pop(key, None)
        except Exception as e:
            print('Cars 2 particles: texture %d not loaded (%s)' % (ti, e))


def _texture(scene, lib, i):
    """GPU texture of a loaded particle image (None until _load_images ran)."""
    key = (lib.zpath, i)
    tex = _tex.get(key)
    if tex is None:
        img = bpy.data.images.get(_images.get(key, ''))
        if img is None:
            return None
        try:
            tex = _tex[key] = gpu.texture.from_image(img)
        except Exception as e:
            print('Cars 2 particles: texture %d not made (%s)' % (i, e))
            return None
    return tex


def _time(scene):
    fps = scene.render.fps / scene.render.fps_base
    return max(0.0, (scene.frame_current + scene.frame_subframe - 1) / fps)


def draw(scene, vp, view, scale=1.0):
    """Draw the live particles: vp = projection @ view (Blender world ->
    clip), view: the view matrix (for the billboard axes)."""
    if not SOURCES:
        return 0
    from gpu_extras.batch import batch_for_shader
    groups = particles(scene, _time(scene))
    if not groups:
        return 0
    inv = view.inverted_safe().to_3x3()
    right = np.array(inv.col[0], np.float32)
    up = np.array(inv.col[1], np.float32)
    sh = _get_shader()
    sh.bind()
    sh.uniform_float('vp', vp)
    sh.uniform_float('scale', scale)
    st = gpu.state
    st.depth_test_set('LESS_EQUAL')
    st.depth_mask_set(False)
    st.face_culling_set('NONE')
    drawn = 0
    corners = np.array([(-1, -1), (1, -1), (1, 1), (-1, -1), (1, 1), (-1, 1)], np.float32)
    uvs = (corners + 1.0) * 0.5
    for (zpath, ti, blend), (pos, half, rot, col) in groups.items():
        lib = _libs.get(zpath)
        tex = _texture(scene, lib, ti) if lib is not None else None
        if tex is None:
            continue
        pos = np.array(pos, np.float32)
        half = np.array(half, np.float32)
        rot = np.array(rot, np.float32)
        c, s_ = np.cos(rot)[:, None], np.sin(rot)[:, None]
        r = right[None] * c + up[None] * s_          # rotated billboard axes per particle
        u = up[None] * c - right[None] * s_
        n = len(pos)
        p = (pos[:, None, :] + corners[None, :, 0:1] * (half[:, None, 0:1] * r[:, None, :]) +
             corners[None, :, 1:2] * (half[:, None, 1:2] * u[:, None, :])).reshape(-1, 3)
        batch = batch_for_shader(sh, 'TRIS', {'pos': p, 'uv': np.tile(uvs, (n, 1)),
                                              'col': np.repeat(np.array(col, np.float32), 6, axis=0)})
        sh.uniform_int('mode', 1 if blend == 'ALPHA' else 0)
        sh.uniform_sampler('tex', tex)
        st.blend_set('NONE' if blend == 'ALPHA' else 'ALPHA_PREMULT')
        batch.draw(sh)
        drawn += n
    st.blend_set('NONE')
    st.depth_test_set('NONE')
    return drawn


def _viewport_draw():
    """POST_VIEW: the particles over the other render engines' viewports."""
    ctx = bpy.context
    scene = ctx.scene
    if not SOURCES or scene is None:
        return
    from . import gamerender
    if scene.render.engine == gamerender.ENGINE_ID:
        sp = ctx.space_data
        if sp is not None and getattr(sp.shading, 'type', '') == 'RENDERED':
            return              # drawn by the game renderer itself
    rv3d = ctx.region_data
    if rv3d is None:
        return
    try:
        draw(scene, rv3d.perspective_matrix, rv3d.view_matrix, 1.0)
    except Exception as e:
        print('Cars 2 particles: not drawn (%s)' % e)


def ensure_handler():
    if _handler[0] is None:
        _handler[0] = bpy.types.SpaceView3D.draw_handler_add(_viewport_draw, (), 'WINDOW', 'POST_VIEW')


def register():
    pass


def unregister():
    if _handler[0] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler[0], 'WINDOW')
        _handler[0] = None
    SOURCES.clear()
    _tex.clear()
    _shader[0] = None
