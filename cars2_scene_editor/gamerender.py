"""Cars 2 Game render engine: the world drawn with the game's own shaders.

Pick "Cars 2 Game" as the render engine. Rendered viewport shading and F12
renders then draw every Arcade material with its effect's shipped vertex and
pixel shader, converted to GLSL by core/fxglsl (one shader per effect, not
per material; compiling one takes milliseconds). Material Preview keeps using
Eevee with the simplified materials, Solid mode is Blender's own.

How a frame is drawn (Renderer.draw):
  1. Engine block (one uniform buffer for every draw): camera matrices, target
     size, time and the scene's engine values (fxnodes.ENGINE_VALUES: sun,
     fog, lightmap settings, light rig SH ...), laid out by fxglsl.EngineLayout.
  2. Every visible mesh part: its material's per-draw block (parameters,
     world matrices, texture decode modes, alpha test) is built once and
     cached; editing a value only rebuilds that block, never a shader.
     Opaque parts first, then blended ones back to front.
  3. The HDR image goes through the game's tone map (exposure / average
     luminance, Hejl filmic curve) into the viewport, writing depth as well
     so Blender's overlays (grid, outlines, gizmos) sort against the world.

Dynamic shadows and the view depth come from gameshadow.py, the planar
reflection from gamereflect.py, refraction from a copy of the opaque frame
(samColorBuffer), exposure / bloom / postprocess from gamepost.py. The
ambient buffer is still a constant stand-in (ENGINE_TEXTURES).
"""
import math
import os
import time

import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

import arcade
import cubemap
import fxglsl
import fxgraph

from . import common
from . import fxnodes
from . import gameeyes
from . import gamepost
from . import gamereflect
from . import gameshadow

ENGINE_ID = 'C2_GAME'
# the depth pre-pass draws what is this close (shadows can't fall further: static map 150 m)
PREPASS_RANGE = 160.0
LAYOUT = fxglsl.EngineLayout()

# the per-draw block (fxglsl.TYPEDEFS C2Draw) in vec4 units
OFF_VC, OFF_PC, OFF_VB, OFF_PB, OFF_VI, OFF_PI, OFF_TM, OFF_MISC = 0, 256, 480, 496, 512, 528, 544, 564
DRAW_VEC4 = 565
# instance parameters filled from the object's placement, by the matrix they hold
WORLD_NAMES = {'g_WorldMatrix': 'W', 'g_World': 'W', 'g_WorldInverseMatrix': 'WI', 'g_WorldInverse': 'WI',
               'g_WorldInverseTransposeMatrix': 'WIT'}
# run-time buffers the editor doesn't render yet: constant textures
ENGINE_TEXTURES = dict(fxgraph.ENGINE_SAMPLERS)
# engine buffers the renderer makes every frame (bound by name at draw time)
FRAME_TEXTURES = ('samShadowMask', 'samShadowTexture', 'samShadowTexture1', 'samDepthViewBuffer',
                  'samDepthViewHalfBuffer', 'samReflectionPlaneTexture', 'samColorBuffer', 'samAmbientBuffer')
# cube maps are sampled from their equirectangular image (fxglsl c2_cube)
SAMPLER_TYPES = {'2d': 'FLOAT_2D', 'cube': 'FLOAT_2D', 'volume': 'FLOAT_3D'}
ATTR_TYPES = {1: 'FLOAT', 2: 'VEC2', 3: 'VEC3', 4: 'VEC4'}
# D3DBLEND pairs -> Blender's blend presets
BLENDS = {('SRCALPHA', 'INVSRCALPHA'): 'ALPHA', ('ONE', 'INVSRCALPHA'): 'ALPHA_PREMULT',
          ('SRCALPHA', 'ONE'): 'ADDITIVE', ('ONE', 'ONE'): 'ADDITIVE_PREMULT',
          ('DESTCOLOR', 'ZERO'): 'MULTIPLY', ('ZERO', 'SRCCOLOR'): 'MULTIPLY'}

POST_VS = '''
void main()
{
  uv = pos * 0.5 + 0.5;
  gl_Position = vec4(pos, 0.0, 1.0);
}
'''
# HDR -> display: the game's tone map (see fxnodes.post_group). `linear`
# converts the display-referred result back to scene-linear, which is what
# Blender expects from a render engine: with the Standard view transform the
# screen then shows exactly the game's colours.
POST_FS = '''
void main()
{
  vec3 c = texture(hdr, uv).rgb;
  vec3 x = max(c * scale - 0.004, vec3(0.0));
  vec3 d = clamp((x * (6.2 * x + 0.5)) / (x * (6.2 * x + 1.7) + 0.06), 0.0, 1.0);
  if (raw > 0.5) d = c;
  else if (linear > 0.5) d = mix(d / 12.92, pow((d + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), d));
  fragColor = vec4(d, 1.0);
  gl_FragDepth = texture(depth, uv).r;
}
'''
# meshes without a usable game effect: texture x simple sun shading
FALLBACK_VS = '''
void main()
{
  vec4 wp = world * vec4(a_Position, 1.0);
  nrm = normalize((world * vec4(a_Normal, 0.0)).xyz);
  tc = a_Uv1;
  vec4 p = wp * mat4(eng.e[VP], eng.e[VP + 1], eng.e[VP + 2], eng.e[VP + 3]);
  gl_Position = vec4(p.xy, 2.0 * p.z - p.w, p.w);
}
'''
FALLBACK_FS = '''
void main()
{
  vec4 t = texture(tex, vec2(tc.x, 1.0 - tc.y)) * color;
  float l = 0.45 + 0.55 * max(dot(nrm, normalize(eng.e[SUN].xyz)), 0.0);
  fragColor = vec4(t.rgb * l * scale, t.a);
}
'''


def _hlsl_regs(mb, cls, count):
    """Registers of a matrix given in Blender form (column vectors). The HLSL
    value is its transpose (row vectors): column-major packing (class 3)
    stores HLSL columns = Blender rows, row-major (class 2) the other way."""
    m = np.array(mb, np.float32).reshape(4, 4)
    if cls == 2:
        m = m.T
    return m[:count]


# ----------------------------------------------------------------------
# effects
# ----------------------------------------------------------------------

class _Meta:
    """What fxnodes' parameter / texture helpers need from an effect
    (the parts of fxnodes.Plan that don't need the node translation)."""

    def __init__(self, effect, tfx, tr):
        self.effect = effect
        self.tfx = tfx
        self.tr = tr
        self.sampler_usage = dict(tr.samplers)
        self.sampler_types = {}
        self.states = {k.split('#')[1]: v for k, v in tr.pass0.items() if k.startswith('RenderState')}
        self.defaults = {n: p.get('Default') for n, p in tr.params.items()}


class Effect:
    """One effect's compiled shader and how to feed it."""

    def __init__(self, meta, sh, shader):
        self.meta = meta
        self.sh = sh
        self.shader = shader
        # (GLSL sampler name, tmode index, type, CTAB name)
        self.samplers = [(n, r, t, c) for n, r, t, c in sh.ps_samplers] + \
                        [(n, 16 + r, t, c) for n, r, t, c in sh.vs_samplers]
        for n, r, t, c in self.samplers:
            meta.sampler_types[c] = t
        self.world = [(e, WORLD_NAMES[e[1]]) for e in sh.draw_entries if e[1] in WORLD_NAMES]


def _create_shader(sh):
    """GPU shader for a converted technique."""
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(fxglsl.TYPEDEFS)
    info.uniform_buf(0, 'C2Draw', 'drw')
    info.uniform_buf(1, 'C2Engine', 'eng')
    for i, (a, _el, n) in enumerate(sh.attrs):
        info.vertex_in(i, ATTR_TYPES[n], a)
    iface = gpu.types.GPUStageInterfaceInfo('c2_iface')
    for v in sh.varyings:
        iface.smooth('VEC4', v)
    info.vertex_out(iface)
    for slot, (name, _r, typ, _c) in enumerate(sh.ps_samplers + sh.vs_samplers):
        info.sampler(slot, SAMPLER_TYPES[typ], name)
    info.fragment_out(0, 'VEC4', 'fragColor')
    info.vertex_source(sh.vs_src)
    info.fragment_source(sh.fs_src)
    return gpu.shader.create_from_info(info)


_effects = {}       # (assets, effect lower, technique, package) -> Effect or None
_metas = {}         # (assets, effect lower, technique, package) -> _Meta or None

# Materials of character / choreography packages (preview.py): their textures
# and the effects the package ships itself. {base material name: GameContext}
def _optional(name):
    """A world-only module of the Scene Editor (crowds, particles, light
    volumes), or None in the Cars 2 Toolkit's copy of the Game Look renderer."""
    if name not in _OPTIONAL:
        import importlib
        try:
            _OPTIONAL[name] = importlib.import_module('.' + name, __package__)
        except ImportError:
            _OPTIONAL[name] = None
    return _OPTIONAL[name]


_OPTIONAL = {}


PACKAGES = {}
# a character's reflection cube (envMapInstance) in a world: the world texture
# index of the light probe it is in (set by the Cars 2 Toolkit; the game picks
# it at run time). {character armature name: world TexturePool index}
ENV_OVERRIDE = {}
# an eye material's parameters as the character's eye rig sets them now (lids,
# look offset): the game evaluates the rig every frame and never stores the
# result, so these only feed renderedEyeTexture (set by the Cars 2 Toolkit).
# {material name: {parameter: value}}
EYE_VALUES = {}
# a character's reflection with no world (see fxnodes.NEUTRAL_LIGHT): RGBM like
# a real probe cube (Tokyo B's locator3 probe averages rgb 0.74 / 0.71 / 0.78,
# multiplier 0.042: a plain bright constant bleaches a car's paint)
NEUTRAL_ENV = (0.74, 0.71, 0.78, 0.042)


def register_package(materials, game):
    """Remember the package context (textures, own effects) of preview materials."""
    for m in materials:
        if m is not None:
            PACKAGES[m.name] = game


def _redraw_views():
    """Timer: redraw the 3D views once (a skipped exposure measure is taken then)."""
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return None


def reset_after_undo():
    """After an undo / redo (or a failed draw): drop every reference to Blender
    data and rebuild from scratch on the next draw. Undo re-creates the
    data-blocks it changes (an edited material, its images): references kept
    from before it point at freed memory, and every later draw failed on them."""
    RENDERER.forget_blender_data()
    seen = set()
    for g in PACKAGES.values():
        t = g.textures if g is not None else None
        while t is not None and id(t) not in seen:
            seen.add(id(t))
            t.revalidate()
            t = t.parent


def _package(mat):
    """The GameContext of a preview package's or a character's material (None:
    the world's)."""
    base = mat.c2.variant_of or mat
    g = PACKAGES.get(base.name)
    if g is None and base.c2.owner is not None:
        g = fxnodes.owner_context(bpy.context.scene, base.c2.owner)
    return g


def _read_tfx(assets, effect, local):
    """(decoded .tfx, cache tag): the package's own copy first, then the game's."""
    name = (effect or '').lower()
    if local and name in local:
        import c2oct
        try:
            return c2oct.decode(local[name])[0], '@pkg%d' % len(local[name])
        except ValueError:
            pass
    return (arcade.read_effect(assets, effect) if assets and effect else None), ''


def meta_for(assets, effect, technique='Standard', local=None):
    """An effect's parameters, samplers and states (no GPU needed), or None.
    Enough for fxnodes.ensure_effect_entries / param_value / texture_index."""
    name = (effect or '').lower()
    pkg = '@pkg' if local and name in local else ''
    key = (assets, name, technique, pkg + (str(len(local[name])) if pkg else ''))
    if key not in _metas:
        meta = None
        tfx, _tag = _read_tfx(assets, effect, local)
        if tfx is not None:
            try:
                meta = _Meta(effect, tfx, fxgraph.Translator(tfx, technique))
            except (fxgraph.Unsupported, KeyError) as e:
                print('Cars 2 Game render: effect %s not read (%s)' % (effect, e))
        _metas[key] = meta
    return _metas[key]


def effect_for(assets, effect, technique='Standard', local=None):
    """Compiled Effect for an effect name, or None (no .tfx, no such
    technique, or an instruction the converter doesn't support). local: the
    {name: .tfx bytes} a character / choreography package ships."""
    name = (effect or '').lower()
    pkg = '@pkg' if local and name in local else ''
    key = (assets, name, technique, pkg + (str(len(local[name])) if pkg else ''))
    if key in _effects:
        return _effects[key]
    eff = None
    meta = meta_for(assets, effect, technique, local)
    if meta is not None:
        try:
            tfx, tr = meta.tfx, meta.tr
            p0 = tr.pass0
            conv = fxglsl.Converter(tr.streams, lambda n: n in tr.params and n != 'objectSH', LAYOUT)
            sh = conv.convert(tfx['VertexShaderPool']['VertexShader#%d' % p0['VertexShaderReference']]['Data'],
                              tfx['PixelShaderPool']['PixelShader#%d' % p0['PixelShaderReference']]['Data'])
            eff = Effect(meta, sh, _create_shader(sh))
        except Exception as e:     # fxglsl.Unsupported, a GLSL compile error ...
            print('Cars 2 Game render: effect %s not converted (%s)' % (effect, e))
    _effects[key] = eff
    return eff


# ----------------------------------------------------------------------
# textures
# ----------------------------------------------------------------------

def _gpu_buffer(arr):
    a = np.ascontiguousarray(arr, np.float32).ravel()
    return gpu.types.Buffer('FLOAT', a.size, a)


class TextureCache:
    """GPU textures by source: Blender images, cube-map DDS files, constants."""

    def __init__(self):
        self.consts = {}
        self.cubes = {}
        self.images = {}

    def constant(self, rgba, typ='2d'):
        key = (tuple(round(float(x), 4) for x in rgba), typ)
        t = self.consts.get(key)
        if t is None:
            if typ == 'cube':
                t = gpu.types.GPUTexture(1, is_cubemap=True, format='RGBA16F', data=_gpu_buffer(list(rgba) * 6))
            elif typ == 'volume':
                # identity colour LUT (colour grading off)
                n = 16
                g = (np.arange(n, dtype=np.float32) + 0.5) / n
                b_, g_, r_ = np.meshgrid(g, g, g, indexing='ij')
                lut = np.stack([r_, g_, b_, np.ones_like(r_)], -1)
                t = gpu.types.GPUTexture((n, n, n), format='RGBA16F', data=_gpu_buffer(lut))
            else:
                t = gpu.types.GPUTexture((1, 1), format='RGBA16F', data=_gpu_buffer(rgba))
            self.consts[key] = t
        return t

    def image(self, img):
        t = self.images.get(img.name)
        if t is None:
            try:
                t = gpu.texture.from_image(img)
            except Exception:
                t = None
            self.images[img.name] = t
        return t

    def cube(self, path):
        if path in self.cubes:
            return self.cubes[path]
        t = None
        try:
            with open(path, 'rb') as f:
                faces = cubemap.cube_faces(f.read())
            n = faces[0].shape[0]
            t = gpu.types.GPUTexture(n, is_cubemap=True, format='RGBA16F', data=_gpu_buffer(np.stack(faces)))
        except (OSError, ValueError, Exception) as e:
            print('Cars 2 Game render: cube map %s: %s' % (path, e))
        self.cubes[path] = t
        return t

    def clear(self):
        self.consts.clear()
        self.cubes.clear()
        self.images.clear()


def _sampler_state(tex, st):
    """D3D sampler states on a texture (Blender 5.x; older versions keep the
    texture's own states)."""
    try:
        tex.extend_mode('EXTEND' if st.get('AddressU') == 'CLAMP' else 'REPEAT')
        point = st.get('MinFilter') == 'POINT'
        tex.filter_mode(not point)
        tex.mipmap_mode(st.get('MipFilter') not in (None, 'NONE'), not point)
    except (AttributeError, TypeError, ValueError, SystemError):
        pass


# ----------------------------------------------------------------------
# meshes
# ----------------------------------------------------------------------

def _mesh_groups(obj, depsgraph):
    """Per material: the triangle corners of an object, with every vertex
    element in the game's conventions (exporter.extract_groups)."""
    from . import meshdata
    rep = meshdata.Report()
    try:
        return meshdata.extract_groups(obj, depsgraph, rep, binormal_sign=-1.0, force_eval=True)
    except Exception as e:
        print('Cars 2 Game render: mesh %s: %s' % (obj.name, e))
        return []


def _element(g, name, n):
    """Values of vertex element `name` for a group (float32 (count, n))."""
    import vertexfmt
    count = len(g['corners'])
    if name == 'Position':
        v = g['co']
    elif name == 'Normal':
        v = g['normal']
    elif name.startswith(('Tangent', 'Binormal')):
        suffix = name.replace('Tangent', '').replace('Binormal', '')
        uv = {'2': 'Uv2', 'Mod': 'UvMod'}.get(suffix, 'Uv1')
        fr = g['frames'].get(uv) or g['frames'].get('Uv1')
        v = None if fr is None else fr[0 if name.startswith('Tangent') else 1]
    elif name in g['uvs']:
        v = g['uvs'][name]
    elif name in g['colors']:
        v = g['colors'][name]
    else:
        v = None
    if v is None:
        v = vertexfmt.default_values(name, count)
    v = np.asarray(v, np.float32)
    if v.shape[1] < n:
        v = np.concatenate([v, np.zeros((count, n - v.shape[1]), np.float32)], 1)
    return np.ascontiguousarray(v[:, :n])


def _batch(g, attrs):
    """GPUBatch of a group's corners with the given (GLSL name, element, n)
    attributes. The filled arrays are kept in g['data'] (deform-only refresh)."""
    data = {a: _element(g, el, n) for a, el, n in attrs}
    g['data'] = data
    return _batch_of(attrs, data, len(g['corners']))


def _batch_of(attrs, data, count):
    """GPUBatch from filled attribute arrays ({GLSL name: array})."""
    fmt = gpu.types.GPUVertFormat()
    for a, _el, n in attrs:
        fmt.attr_add(id=a, comp_type='F32', len=n, fetch_mode='FLOAT')
    vbo = gpu.types.GPUVertBuf(fmt, count)
    for a, _el, _n in attrs:
        vbo.attr_fill(id=a, data=data[a])
    return gpu.types.GPUBatch(type='TRIS', buf=vbo)


FALLBACK_ATTRS = [('a_Position', 'Position', 3), ('a_Normal', 'Normal', 3), ('a_Uv1', 'Uv1', 2)]


class MeshEntry:
    """GPU data of one object's evaluated mesh: one batch per material slot."""

    def __init__(self, parts, center, radius):
        self.parts = parts        # [(material, batch, effect, RenderCaps, Reflect-technique effect)]
        self.center = center
        self.radius = radius
        self.deform = None        # (vertex count, corner count, [(part, attrs, arrays, corners, corner vertices)])


# ----------------------------------------------------------------------
# the renderer
# ----------------------------------------------------------------------

class Frame:
    """Camera and target of one drawing: Blender view / projection matrices
    (world space) and the target size."""

    def __init__(self, view, proj, width, height, near, far):
        self.view = view
        self.proj = proj
        self.width = width
        self.height = height
        self.near = near
        self.far = far
        self.depth_proj = None      # Blender's projection for the depth handed to the overlays


class Renderer:
    """Everything that stays on the GPU between redraws."""

    def __init__(self):
        self.meshes = {}            # object name -> MeshEntry
        self.kept = {}              # object name -> MeshEntry from before an undo (reused when unchanged)
        self.suspect = set()        # object names whose mesh may have changed (checked on the next draw)
        self._mat_map = None        # material name -> material, made once after an undo
        self.mat_cache = {}         # (material, effect, textures, version) -> (bindings, modes, block rows)
        self.mesh_users = {}        # mesh datablock name -> names of the objects drawn with it
        self.blocks = {}            # (object name, slot, material) -> (key, ubo)
        self.mat_version = {}       # material name -> edit counter
        self.mat_users = {}         # material name -> names of the objects drawing it
        self.textures = TextureCache()
        self.eyes = gameeyes.EyeTextures()
        self.engine_ubo = None
        self.engine_data = np.zeros((fxglsl.ENGINE_MAX, 4), np.float32)
        self.targets = None         # (w, h, hdr fb, hdr tex, depth tex)
        self.post = None
        self.fallback = None
        self.post_batch = None
        self.last = None            # cache key of the image in the HDR target
        self.cmds = None            # (cmd_version, opaque, blended, casters) prepared draw commands
        self.version = 0            # bumped by any change that needs a redraw
        self.cmd_version = 0        # bumped by changes to what is drawn (not by a frame change)
        self.shadow_key = None      # what the shadow maps in self.shadow_tex were drawn for
        self.shadow_tex = None
        self.stats = {}
        self._caps = None           # (oct path, RenderCaps per scene node)
        self.shadows = gameshadow.Shadows()
        self.post = gamepost.Post()
        self.post_effect = None     # postprocess.tfx (the game's final pass), or None
        self.shared = {}            # engine defaults from shared.tfx: name -> register rows
        self.shared_src = None
        self.default_blocks = {}    # engine effect -> per-draw block of its defaults
        self.bloom_effs = None      # the bloom chain's effects (gamepost.Bloom), or None
        self.bloom_tex = None       # this frame's blur_1_4
        self.engine_ubos = {}       # 'main' / 'mirror' engine blocks
        self.reflection = gamereflect.Reflection()
        self.refl = ([], [], None)  # reflection commands (opaque, blended) and the plane height
        self.motion_ids = {}        # moving object name -> motion-blur id (1..15)
        self.motion_objs = {}       # the same names -> their objects
        self.mb_now = None          # {'view' / 'render': this frame's camera and object placements}
        self.motion_key = 'view'
        self.mb_values = None       # MotionBlur parameters when the timeline just advanced a frame
        self.mb_effs = None
        self.mb_tex = None
        self.dyn_blocks = {}
        self.mask_block = None      # per-draw block of the shadow-mask effect
        self.obj_draw = {}          # object / instance name -> (opaque, blended) commands
        self.obj_keys = {}          # object / instance name -> what its commands were built from (_draw_key)
        self.soft_full = False      # the pending full rebuild may keep unchanged objects' commands
        self.obj_cast = {}          # object name -> (dynamic, static) shadow-caster draws
        self.dirty = set()          # objects whose commands must be rebuilt
        self.deformed = set()       # objects whose mesh only needs new positions / normals
        self.full = True            # rebuild every object's commands
        self.bounds = {}            # 'opaque' / 'blended' -> (centres, radii) for culling
        self.cmd_pos = {}           # id(command) -> ('opaque' / 'blended', index in its list)

    # -- invalidation ------------------------------------------------------------------
    def touch_material(self, mat):
        """A material's parameters changed: rebuild the draws of the objects
        using it (a previewed character's eyes are keyed every frame: a full
        rebuild of every object would cost ~200 ms a frame)."""
        n = mat.name
        self.mat_version[n] = self.mat_version.get(n, 0) + 1
        users = self.mat_users.get(n)
        if users is None:
            # nothing draws it (mat_users records every drawn material): a light
            # volume's keyed material changes every frame of the fireworks, and
            # rebuilding everything for it cost ~340 ms a frame. Whatever draws it
            # later is built fresh and picks up the new version.
            if self.cmds is None:
                self.full = True
        elif any('#' in u for u in users):
            self.full = True                # collection instances
        else:
            self.dirty.update(users)
        self.version += 1
        self.cmd_version += 1

    def mark(self, name):
        """An object moved: rebuild its commands only."""
        self.dirty.add(name)
        self.version += 1
        self.cmd_version += 1

    def drop_mesh(self, name, deform_only=False):
        """An object's mesh changed. deform_only: the same mesh bent by an
        armature / shape keys (a playing preview): only its positions and
        normals are refreshed, not the whole mesh."""
        if deform_only and name in self.meshes and self.meshes[name].deform is not None:
            self.deformed.add(name)
        else:
            self.meshes.pop(name, None)
            self.deformed.discard(name)
        self.mark(name)

    def rebuild(self, soft=False):
        """Something scene-wide changed: rebuild every command. soft: only objects were shown,
        hidden, added or removed - unchanged objects keep their commands (_draw_key)."""
        self.soft_full = soft and (self.soft_full or not self.full)
        self.full = True
        self.version += 1
        self.cmd_version += 1

    def clear(self):
        self.soft_full = False
        self.suspect = set()
        self.obj_keys = {}
        self.meshes.clear()
        self.kept.clear()
        self.mat_cache.clear()
        self.mesh_users.clear()
        self.deformed.clear()
        self.blocks.clear()
        self.cmds = None
        self.full = True
        self.textures.clear()
        self.eyes.clear()
        self.last = None
        self.version += 1
        self.cmd_version += 1
        self.shadow_key = None

    def _draw_key(self, orig, mw, is_instance, ctx):
        """What an object's commands were built from: shown, world matrix, mesh, material
        versions, display. Equal = its commands can be kept on a soft rebuild."""
        e = self.meshes.get(orig.name)
        if e is None:
            return None
        visible = ctx['visible']
        shown = is_instance or visible is None or visible(orig)
        mv = self.mat_version
        return (shown, orig.display_type, tuple(x for r in mw for x in r), id(e),
                tuple(mv.get(m.name, 0) if m is not None else 0 for m, _b, _e, _c, _r in e.parts))

    def forget_blender_data(self):
        """Undo / redo re-create the edited data-blocks: every command, block and
        list holding a material, object or image is dropped and rebuilt. The GPU
        meshes are kept aside: Blender's undo leaves the data-blocks it didn't change
        where they were, so a mesh whose fingerprint still matches is reused (its
        materials looked up again) instead of read again - rebuilding all of italy's
        6300 meshes made every undo / redo take ~10 s."""
        kept = {n: e for n, e in self.meshes.items() if e.deform is None and getattr(e, 'fp', None) is not None}
        self.clear()
        self.kept = kept
        self._mat_map = None
        for d in (self.obj_draw, self.obj_cast, self.mat_users, self.cmd_pos, self.dyn_blocks, self.motion_ids,
                  self.motion_objs):
            d.clear()
        self.dirty.clear()
        self.refl = ([], [], None)
        self.mb_now = None
        self.shadow_tex = None

    # -- shared shaders ----------------------------------------------------------------
    def _post_shader(self):
        if self.post is None and gamepost.needs_legacy_depth():
            # Blender 4.1: no depth_write() - the older constructor keeps the depth write (gamepost)
            self.post = gamepost.legacy_quad_shader(POST_VS, POST_FS, ['hdr', 'depth'],
                                                    [('FLOAT', 'scale'), ('FLOAT', 'linear'), ('FLOAT', 'raw')])
            from gpu_extras.batch import batch_for_shader
            self.post_batch = batch_for_shader(self.post, 'TRI_FAN',
                                               {'pos': ((-1, -1), (1, -1), (1, 1), (-1, 1))})
        if self.post is None:
            info = gpu.types.GPUShaderCreateInfo()
            info.vertex_in(0, 'VEC2', 'pos')
            iface = gpu.types.GPUStageInterfaceInfo('c2_post_iface')
            iface.smooth('VEC2', 'uv')
            info.vertex_out(iface)
            info.sampler(0, 'FLOAT_2D', 'hdr')
            info.sampler(1, 'FLOAT_2D', 'depth')
            info.push_constant('FLOAT', 'scale')
            info.push_constant('FLOAT', 'linear')
            info.push_constant('FLOAT', 'raw')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.depth_write('ANY')
            info.vertex_source(POST_VS)
            info.fragment_source(POST_FS)
            self.post = gpu.shader.create_from_info(info)
            from gpu_extras.batch import batch_for_shader
            self.post_batch = batch_for_shader(self.post, 'TRI_FAN',
                                               {'pos': ((-1, -1), (1, -1), (1, 1), (-1, 1))})
        return self.post

    def _fallback_shader(self):
        if self.fallback is None:
            vp = LAYOUT.slot('g_ViewProjection', 4, 3)
            sun = LAYOUT.slot('g_sunLightDirection', 1, 1)
            info = gpu.types.GPUShaderCreateInfo()
            info.typedef_source(fxglsl.TYPEDEFS)
            info.uniform_buf(1, 'C2Engine', 'eng')
            for i, (a, _el, n) in enumerate(FALLBACK_ATTRS):
                info.vertex_in(i, ATTR_TYPES[n], a)
            iface = gpu.types.GPUStageInterfaceInfo('c2_fb_iface')
            iface.smooth('VEC3', 'nrm')
            iface.smooth('VEC2', 'tc')
            info.vertex_out(iface)
            info.sampler(0, 'FLOAT_2D', 'tex')
            info.push_constant('MAT4', 'world')
            info.push_constant('VEC4', 'color')
            info.push_constant('FLOAT', 'scale')
            info.fragment_out(0, 'VEC4', 'fragColor')
            info.vertex_source(FALLBACK_VS.replace('VP', str(vp)))
            info.fragment_source(FALLBACK_FS.replace('SUN', str(sun)))
            self.fallback = gpu.shader.create_from_info(info)
        return self.fallback

    # -- meshes ------------------------------------------------------------------------
    def node_caps(self, scene):
        """RenderCaps of every scene node of the source .oct (by node index):
        which parts cast into which shadow map. Read once per world."""
        path = bpy.path.abspath(scene.c2.oct_path) if scene.c2.oct_path else ''
        if self._caps is None or self._caps[0] != path:
            caps = []
            if path and os.path.exists(path):
                try:
                    import c2oct
                    with open(path, 'rb') as f:
                        t = c2oct.decode(f.read())[0]
                    caps = [int(n.get('RenderCaps', 0) or 0) for n in t['SceneTreeNodePool'].values()]
                except (OSError, ValueError, KeyError) as e:
                    print('Cars 2 Game render: RenderCaps not read (%s)' % e)
            self._caps = (path, caps)
        return self._caps[1]

    def mesh(self, obj, depsgraph, assets, caps_of=None):
        """MeshEntry of an object (built on first use)."""
        name = obj.original.name if obj.original else obj.name
        e = self.meshes.get(name)
        if e is not None and name in self.deformed:
            self.deformed.discard(name)
            if self._redeform(e, obj, depsgraph):
                return e
            e = None                        # the topology changed after all: rebuild
        if e is not None and name in self.suspect:
            self.suspect.discard(name)
            try:
                now = _exact(obj.original or obj)
                if now is None or e.exact is None or now != e.exact:
                    e = None                # really changed (or can't tell): read it again
            except ReferenceError:
                e = None
        if e is not None:
            return e
        t0 = time.perf_counter()
        orig = obj.original or obj
        if orig.data is not None:
            self.mesh_users.setdefault(orig.data.name, set()).add(name)
        old = self.kept.pop(name, None)
        if old is not None:
            e = self._reuse(old, orig)
            if e is not None:
                self.meshes[name] = e
                self.stats['reused'] = self.stats.get('reused', 0) + 1
                return e
        parts = []
        deform = []
        lo, hi = None, None
        for g in _mesh_groups(orig, depsgraph):
            mat = g['material']
            if len(g['corners']) == 0:
                continue
            if orig.get('c2_preview'):
                caps = 0x241000            # cars and characters cast shadows and reflect
            else:
                sub = g.get('subgeo', -1)
                caps = caps_of[sub] if caps_of is not None and 0 <= sub < len(caps_of) else 0
            eff = eff_r = None
            if mat is not None and mat.get('c2_platform') == 'ARCADE':
                base = mat.c2.variant_of or mat
                pkg = _package(mat)
                local = pkg.local_effects if pkg else None
                eff = effect_for(assets, base.c2.effect, local=local)
                if caps & gamereflect.REFLECT_BIT:
                    # the mirror view draws with the effect's Reflect technique
                    eff_r = effect_for(assets, base.c2.effect, 'Reflect', local)
            attrs = list(eff.sh.attrs) if eff is not None else []
            for a in (list(eff_r.sh.attrs) if eff_r is not None else []) + FALLBACK_ATTRS:
                if a[0] not in [x[0] for x in attrs]:
                    attrs.append(a)
            parts.append((mat, _batch(g, attrs), eff, caps, eff_r))
            deform.append((len(parts) - 1, attrs, g['data'], g['corners'], g.get('vidx')))
            co = g['co']
            lo = co.min(0) if lo is None else np.minimum(lo, co.min(0))
            hi = co.max(0) if hi is None else np.maximum(hi, co.max(0))
        center = (lo + hi) / 2 if lo is not None else np.zeros(3)
        radius = float(np.linalg.norm(hi - lo) / 2) if lo is not None else 0.0
        e = MeshEntry(parts, center, radius)
        e.exact = _exact(orig)
        e.fp = e.exact
        e.mat_names = [m.name if m is not None else None for m, _b, _e, _c, _r in parts]
        if deform and all(d[4] is not None for d in deform) and _deforms(orig):
            ev = orig.evaluated_get(depsgraph)
            me = ev.to_mesh()
            e.deform = (len(me.vertices), len(me.loops), deform)
            ev.to_mesh_clear()
        self.meshes[name] = e
        self.stats['mesh_s'] = self.stats.get('mesh_s', 0.0) + time.perf_counter() - t0
        return e

    def _reuse(self, old, orig):
        """A mesh kept from before an undo, if its object's mesh is unchanged: the same
        entry with its materials looked up again (undo may have re-made them). None: build it."""
        try:
            now = _exact(orig)
            if now is None or old.fp is None or now != old.fp:
                return None
            if self._mat_map is None:       # one name -> material map per undo (not a lookup each)
                self._mat_map = {m.name: m for m in bpy.data.materials}
            mats = []
            for n in old.mat_names:
                m = self._mat_map.get(n) if n is not None else None
                if n is not None and m is None:
                    return None
                mats.append(m)
            old.parts = [(m, b, eff, caps, er) for m, (_m, b, eff, caps, er) in zip(mats, old.parts)]
            return old
        except (ReferenceError, AttributeError):
            return None

    def _redeform(self, e, obj, depsgraph):
        """New positions and normals of a deformed mesh: new vertex buffers
        from the kept attribute arrays (a drawn buffer can't be refilled), the
        rest of the mesh not read again; tangent frames stay as first built.
        False when the mesh's topology no longer matches."""
        t0 = time.perf_counter()
        orig = obj.original or obj
        ev = orig.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            nv, nl, parts = e.deform
            if len(me.vertices) != nv or len(me.loops) != nl:
                return False
            co = np.empty(nv * 3, np.float32)
            me.vertices.foreach_get('co', co)
            co = co.reshape(-1, 3)
            cn = np.empty(nl * 3, np.float32)
            try:
                me.corner_normals.foreach_get('vector', cn)
            except AttributeError:
                me.calc_normals_split()
                me.loops.foreach_get('normal', cn)
            cn = cn.reshape(-1, 3)
            for i, attrs, data, corners, vidx in parts:
                for a, el, n in attrs:
                    if el == 'Position':
                        data[a] = co[vidx]
                    elif el == 'Normal':
                        data[a] = cn[corners]
                mat, _b, eff, caps, eff_r = e.parts[i]
                e.parts[i] = (mat, _batch_of(attrs, data, len(corners)), eff, caps, eff_r)
            if nv:
                lo, hi = co.min(0), co.max(0)
                e.center = (lo + hi) / 2
                e.radius = float(np.linalg.norm(hi - lo) / 2)
        finally:
            ev.to_mesh_clear()
        self.stats['deform_ms'] = (time.perf_counter() - t0) * 1000.0
        return True

    # -- per-draw blocks ---------------------------------------------------------------
    def _material_block(self, mat, eff, textures):
        """Per-draw values that depend on the material only (vec4 array)."""
        meta = eff.meta
        d = np.zeros((DRAW_VEC4, 4), np.float32)
        base = {'float4': {'vs': OFF_VC, 'ps': OFF_PC}, 'bool': {'vs': OFF_VB, 'ps': OFF_PB},
                'int4': {'vs': OFF_VI, 'ps': OFF_PI}}
        for e in eff.sh.draw_entries:
            stage, name, reg, count, cls, set_ = e
            if name in WORLD_NAMES:
                continue
            off = base[set_][stage] + reg
            if name in eff.sh.relative:
                # bone palette: identity (the mesh is already deformed)
                per = 3 if count % 3 == 0 else 4
                for k in range(count):
                    d[off + k] = 0.0
                    d[off + k][k % per] = 1.0
                continue
            for k in range(count):
                d[off + k] = [fxnodes.param_value(mat, meta, name, k, c) for c in range(4)]
        # alpha test
        if bool(fxnodes._state(meta, mat, 'AlphaTestEnable', 0)):
            fn = fxglsl.ALPHA_FUNCS.get(str(fxnodes._state(meta, mat, 'AlphaFunc', 'GREATER')).upper(), 5)
            d[OFF_MISC] = (1.0, float(fxnodes._state(meta, mat, 'AlphaRef', 0)) / 255.0, fn, 0.0)
        return d

    def _bindings(self, mat, eff, textures, gtime):
        """[(GLSL sampler, GPUTexture)] and the tmode values for a material."""
        out = []
        modes = {}
        anim = fxnodes.texture_animation(mat)
        for name, ti, typ, cname in eff.samplers:
            tex = None
            mode = 0
            if cname in FRAME_TEXTURES:
                tex = cname                # bound per frame (shadow mask, view depth ...)
            elif cname in ENGINE_TEXTURES:
                tex = self.textures.constant(ENGINE_TEXTURES[cname], '2d' if typ == 'cube' else typ)
            else:
                usage, idx = fxnodes.texture_index(mat, eff.meta, cname)
                if usage == 'renderedEyeTexture' and \
                        gameeyes.is_eye_effect((mat.c2.variant_of or mat).c2.effect):
                    # the eye image the game renders before drawing the eye strip
                    out.append((name, self._eye_texture(mat, eff, textures)))
                    modes[ti] = 0
                    continue
                if usage == 'envMapInstance' and (mat.c2.variant_of or mat).c2.owner is not None and \
                        (textures is None or textures.parent is None) and (idx is None or idx < 0):
                    # a character with no world and no cube of its own in the slot (characters
                    # carry the errorsky cube there, the game's stand-in until a map's probe
                    # replaces it: that one is used)
                    out.append((name, self.textures.constant(NEUTRAL_ENV, '2d')))
                    modes[ti] = 0
                    continue
                src = textures
                if idx is None and textures is not None and usage in textures.global_entries:
                    idx = textures.global_entries[usage]
                elif idx is None and textures is not None and textures.parent is not None and \
                        usage in textures.parent.global_entries:
                    src = textures.parent      # a package's fog / environment come from the world
                    idx = src.global_entries[usage]
                if usage == 'envMapInstance' and textures is not None and textures.parent is not None:
                    owner = (mat.c2.variant_of or mat).c2.owner
                    env = ENV_OVERRIDE.get(owner.name, -1) if owner is not None else -1
                    if env >= 0:
                        src, idx = textures.parent, env     # the map's light probe cube, as the game sets it
                if idx is None and usage in fxnodes.ENGINE_FILES:
                    eng, i = fxnodes.engine_texture(common.game_assets(bpy.context.scene), usage)
                    if eng is not None:
                        src, idx = eng, i       # the engine binds these itself (environmentScroll.dds)
                if usage in anim and idx is not None and typ == '2d':
                    fr = anim[usage]
                    if fr['textures']:
                        idx = fr['textures'][int(math.floor(gtime * fr['rate'])) % len(fr['textures'])]
                if idx is not None and src is not None:
                    want = src.is_srgb(idx)
                    if typ == 'cube':
                        img = src.cube_image(idx)       # equirectangular PNG (raw values), with mipmaps
                        tex = self.textures.image(img) if img is not None else None
                        decoded = False
                    elif typ == '2d':
                        img = src.images.get(idx)
                        tex = self.textures.image(img) if img is not None else None
                        decoded = img is not None and img.colorspace_settings.name not in \
                            ('Non-Color', 'Raw', 'Linear', 'Linear Rec.709')
                    else:
                        decoded = want
                    if tex is not None and decoded != want:
                        mode = 1 if want else 2
                    if tex is not None:
                        _sampler_state(tex, eff.meta.tr.sampler_states.get(cname, {}))
                if tex is None:
                    const = next((c for p, c in fxnodes.MISSING.items() if usage and usage.startswith(p)),
                                 (1.0, 1.0, 1.0, 1.0))
                    if typ == 'cube' and (_package(mat) is not None):
                        # a character's empty cube slot (a shader switch adds its slots empty):
                        # white is RGBM at full multiplier, ~36x a real probe once decoded, and
                        # blew the car out white until the slot got a texture
                        const = NEUTRAL_ENV
                    tex = self.textures.constant(const, '2d' if typ == 'cube' else typ)
            out.append((name, tex))
            modes[ti] = mode
        return out, modes

    def _usage_image(self, mat, textures, usage):
        """(GPUTexture, tmode) of the material's texture for `usage`, or (None, 0)."""
        base = mat.c2.variant_of or mat
        t = next((t for t in base.c2.textures if t.usage == usage and t.texture >= 0), None)
        if t is None or textures is None:
            return None, 0
        img = textures.images.get(t.texture)
        tex = self.textures.image(img) if img is not None else None
        if tex is None:
            return None, 0
        want = textures.is_srgb(t.texture)
        decoded = img.colorspace_settings.name not in ('Non-Color', 'Raw', 'Linear', 'Linear Rec.709')
        return tex, (0 if decoded == want else (1 if want else 2))

    def _eye_texture(self, mat, eff, textures):
        """renderedEyeTexture of an eye material (gameeyes.py), redrawn when
        its parameters change (the preview keys them every frame)."""
        base = mat.c2.variant_of or mat
        eye, eye_mode = self._usage_image(mat, textures, 'eyeBallTexture')
        lid, lid_mode = self._usage_image(mat, textures, 'diffuseMapA1')
        meta = eff.meta

        live = EYE_VALUES.get(base.name)

        def P(n):
            if live is not None and n in live:
                return live[n]          # the eye rig's value now (the material keeps its stored one)
            return fxnodes.param_value(mat, meta, n, 0, 0)
        rows = gameeyes.block_values(P, base.c2.effect.lower().startswith('car_double_eyes'),
                                     lid is not None, eye_mode, lid_mode)
        key = (tuple(round(x, 5) for r in rows for x in r), id(eye), id(lid))
        white = self.textures.constant((1.0, 1.0, 1.0, 0.0))
        return self.eyes.texture(key, mat.name, rows, eye or white, lid or white)

    def block(self, key, mat, eff, textures, world, gtime):
        """(GPUUniformBuf, bindings) of one draw, rebuilt only when the
        material was edited or the object moved."""
        ver = self.mat_version.get(mat.name, 0), self.mat_version.get((mat.c2.variant_of or mat).name, 0)
        wkey = tuple(round(x, 6) for row in world for x in row)
        cached = self.blocks.get(key)
        if cached is not None and cached[0][0] == ver:
            # the material is unchanged: its bindings too (whatever changes them -
            # an edit, keyed values, a replaced texture - bumps the version or
            # clears this cache). A moved object only rewrites its world rows.
            binds, modes = cached[4], cached[3]
            if cached[0][1] == wkey:
                return cached[1], binds
            d = cached[2]
        else:
            # the material's own values and textures are the same for every object using it:
            # worked out once per material (italy draws its 157 materials over ~12,500 parts)
            # (one entry per material, holding its version: a material keyed every frame of a
            # playing preview replaces its entry instead of piling up new ones)
            mkey = (mat.as_pointer(), id(eff), id(textures), mat.name)
            hit = self.mat_cache.get(mkey) if not fxnodes.texture_animation(mat) else None
            if hit is None or hit[0] != ver:
                binds, modes = self._bindings(mat, eff, textures, gtime)
                d0 = self._material_block(mat, eff, textures)
                hit = (ver, binds, modes, d0)
                self.mat_cache[mkey] = hit
            _v, binds, modes, d0 = hit
            d = d0.copy()
        for ti, mode in modes.items():
            d[OFF_TM + ti] = (mode, 0.0, 0.0, 0.0)
        mats = {'W': world}
        if any(m in ('WI', 'WIT') for _e, m in eff.world):
            wi = world.inverted_safe()
            mats['WI'] = wi
            mats['WIT'] = wi.transposed()
        for (stage, name, reg, count, cls, set_), m in eff.world:
            off = (OFF_VC if stage == 'vs' else OFF_PC) + reg
            d[off:off + count] = _hlsl_regs(mats[m], cls, count)
        buf = _gpu_buffer(d)
        if cached is not None:
            ubo = cached[1]
            ubo.update(buf)
        else:
            ubo = gpu.types.GPUUniformBuf(buf)
        self.blocks[key] = ((ver, wkey), ubo, d, modes, binds)
        return ubo, binds

    # -- engine block ------------------------------------------------------------------
    def engine_block(self, frame, root_mw, extra=None, which='main'):
        """Fill the engine uniform buffer for a frame. extra: {name: register
        rows} computed elsewhere (the shadow matrices)."""
        V = frame.view @ root_mw                      # game world -> view (right-handed, -z forward)
        dz = Matrix(((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 0.5, 0.5), (0, 0, 0, 1)))
        P = dz @ frame.proj                           # D3D clip z 0..1
        w, h = frame.width, frame.height
        mats = {'g_ViewProjection': P @ V, 'g_View': V, 'g_ViewInverse': V.inverted_safe(), 'g_Projection': P,
                'g_ViewProjectionWithoutAspect': P @ V}
        vecs = {'g_TargetDimensions': (w, h, 1.0 / w, 1.0 / h), '#target': (w, h, 1.0 / w, 1.0 / h),
                'g_CameraParams': (frame.near, frame.far, 0.0, 0.0),
                'ShadowMapDimensions': (2048.0, 2048.0, 1.0 / 2048, 1.0 / 2048)}
        vals = fxnodes.ENGINE_VALUES
        d = self.engine_data
        d[:] = 0.0
        for name, (base, count, cls) in LAYOUT.regions():
            if extra is not None and name in extra:
                rows = extra[name][:count]
                d[base:base + len(rows)] = rows
            elif name in mats:
                d[base:base + count] = _hlsl_regs(mats[name], cls, count)
            elif name in vecs:
                d[base] = vecs[name]
            elif name in ('g_ShadowMatrix', 'g_ReflectionPlaneProj'):
                continue
            else:
                for el in range(count):
                    v = vals.get((name, el))
                    if v is None and el == 0 and name in self.shared:
                        v = self.shared[name][0]           # the game's default (shared.tfx)
                    if v is None:
                        v = [fxnodes._engine_default(name, c) for c in range(4)]
                    row = list(v)[:4] + [0.0] * (4 - min(4, len(v)))
                    d[base + el] = row
        buf = _gpu_buffer(d)
        ubo = self.engine_ubos.get(which)
        if ubo is None:
            ubo = self.engine_ubos[which] = gpu.types.GPUUniformBuf(buf)
        else:
            ubo.update(buf)
        if which == 'main':
            self.engine_ubo = ubo
        return ubo

    # -- drawing -----------------------------------------------------------------------
    def _target(self, w, h):
        if self.targets is None or self.targets[:2] != (w, h):
            col = gpu.types.GPUTexture((w, h), format='RGBA16F')
            dep = gpu.types.GPUTexture((w, h), format='DEPTH_COMPONENT32F')
            fb = gpu.types.GPUFrameBuffer(depth_slot=dep, color_slots=col)
            self.targets = (w, h, fb, col, dep)
            self.last = None
        return self.targets

    def draw(self, depsgraph, frame, out_linear=False, visible=None):
        """Draw the scene into the HDR target, then expose / tone map it (the
        game's postprocess) into the currently bound framebuffer."""
        scene = depsgraph.scene
        t0 = time.perf_counter()
        # the viewport's (or F12's) framebuffer: everything below binds its own
        # targets, and creating GPU textures / shaders on a first frame can leave
        # another one bound, so the final pass binds this one again explicitly
        out_fb = gpu.state.active_framebuffer_get()
        out_vp = gpu.state.viewport_get()
        w, h = frame.width, frame.height
        _w, _h, fb, col, dep = self._target(w, h)
        auto = scene.c2.game_auto_exposure
        key = (tuple(x for r in frame.view for x in r), tuple(x for r in frame.proj for x in r), w, h,
               self.version, fxnodes.ENGINE_VERSION[0], auto)
        redrawn = key != self.last
        if redrawn:
            self._draw_scene(depsgraph, frame, fb, visible)
            self.last = key
            # the exposure measure reads the frame back (a GPU stall, ~5 ms): while the view
            # keeps changing (orbiting, playback) once every 0.12 s is plenty - the game
            # adapts over seconds. F12 renders (no viewport visibility) always measure
            now = time.perf_counter()
            if auto and (visible is None or self.post.measured is None
                         or now - getattr(self, '_measured_at', 0.0) > 0.12):
                self.post.measure(col, w, h)
                self._measured_at = now
                self._measure_pending = False
            elif auto:
                self._measure_pending = True    # measured on a later redraw (the view may stop here)
                if not bpy.app.timers.is_registered(_redraw_views):
                    bpy.app.timers.register(_redraw_views, first_interval=0.15)
        elif auto and getattr(self, '_measure_pending', False):
            self.post.measure(col, w, h)        # the frame drawn last, still in the target
            self._measured_at = time.perf_counter()
            self._measure_pending = False
            self.stats['scene_ms'] = (time.perf_counter() - t0) * 1000.0
        ex = self.engine_value('g_HDR_Exposure')
        if auto and self.post.measured is not None:
            # where the game's adaptation settles (avglum: clamped to the world's range)
            avg = min(max(self.post.measured, self.engine_value('g_HDR_Min_Luminance')),
                      self.engine_value('g_HDR_Max_Luminance'))
        else:
            avg = self.engine_value('avgLum')
        self.stats['avg_lum'] = round(avg, 4)
        # bloom: the game's chain on the HDR frame (only when the frame changed)
        bloom_on = scene.c2.game_bloom and self.bloom_effs is not None
        if bloom_on and (redrawn or self.bloom_tex is None):
            t1 = time.perf_counter()
            self.bloom_tex = self.post.bloom.run(self.bloom_effs, self.param_block, col, w, h, self.engine_ubo,
                                                 self.post.lum_texture(avg))
            self.stats['bloom_ms'] = (time.perf_counter() - t1) * 1000.0
        elif not bloom_on:
            self.bloom_tex = None
        # motion blur: only when the timeline just moved on by one frame
        if redrawn:
            self.mb_tex = None
            if scene.c2.game_motion_blur and self.mb_effs is not None and self.mb_values is not None \
                    and self.shadows.view is not None:
                prep_eff, mb_eff = self.mb_effs
                self.mb_tex = self.post.motion.run(prep_eff, mb_eff, self._defaults_block(prep_eff),
                                                   self.dynamic_block(mb_eff, self.mb_values), col,
                                                   self.post.lum_texture(avg), self.shadows.view[5],
                                                   self.shadows.view[3], self.engine_ubo)
        self.stats['measured'] = round(self.post.measured or 0.0, 4)
        raw = fxnodes.ENGINE_VALUES.get(('c2Raw', 0), (0.0,))[0]
        eff = self.post_effect
        if eff is not None and not raw:
            binds = []
            black = self.textures.constant((0.0, 0.0, 0.0, 0.0))
            for name, ti, typ, cname in eff.samplers:
                if cname == 'samColorBuffer':
                    t = col
                elif cname == 'samLumBuffer':
                    t = self.post.lum_texture(avg)
                elif cname in ('samDepthViewBuffer', 'samDepthViewHalfBuffer') and self.shadows.view is not None:
                    t = self.shadows.view[3]
                elif cname == 'samBlur_1_4' and self.bloom_tex is not None:
                    t = self.bloom_tex
                elif cname == 'samMotionBlur' and self.mb_tex is not None:
                    t = self.mb_tex
                elif typ != '2d':
                    t = self.textures.constant((1.0, 1.0, 1.0, 1.0), typ)   # identity colour-grade LUT
                else:
                    t = black      # bloom / motion blur / translucency buffers: not made yet
                binds.append((name, t))
            ldr = self.post.run_game_post(eff, w, h, self.engine_ubo, self._defaults_block(eff), binds)
            with out_fb.bind():
                gpu.state.viewport_set(*out_vp)
                self.post.blit(ldr, dep, out_linear, frame.proj, frame.depth_proj,
                               transparent=scene.render.film_transparent)
            return
        # fallback (no postprocess.tfx, or the raw HDR for exposure measuring)
        post = self._post_shader()
        with out_fb.bind():
            gpu.state.viewport_set(*out_vp)
            self._post_fallback(post, col, dep, ex, avg, out_linear, raw)

    def _post_fallback(self, post, col, dep, ex, avg, out_linear, raw):
        post.bind()
        post.uniform_sampler('hdr', col)
        post.uniform_sampler('depth', dep)
        post.uniform_float('scale', ex / max(avg, 1e-6))
        post.uniform_float('linear', 1.0 if out_linear else 0.0)
        post.uniform_float('raw', raw)
        gpu.state.depth_test_set('ALWAYS')
        gpu.state.depth_mask_set(True)
        gpu.state.blend_set('NONE')
        self.post_batch.draw(post)
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)

    def engine_value(self, name, comp=0):
        """An engine global: the scene's value, else the game's default (shared.tfx)."""
        v = fxnodes.ENGINE_VALUES.get((name, 0))
        if v is not None:
            return v[comp] if comp < len(v) else 0.0
        d = self.shared.get(name)
        if d is not None:
            return float(d[0][comp])
        return fxnodes._engine_default(name, comp)

    def load_shared(self, assets):
        """The engine's default for every global (shared.tfx), as register rows;
        colours the effect marks Gamma are linearised like the engine does."""
        if self.shared_src == assets:
            return self.shared
        self.shared_src = assets
        self.shared = {}
        tfx = arcade.read_effect(assets, 'shared') if assets else None
        if tfx is None:
            return self.shared
        for p in tfx.get('Parameters', {}).values():
            if not isinstance(p, dict) or p.get('Default') is None:
                continue
            d = p['Default']
            vals = [float(x) for x in d] if isinstance(d, (list, tuple)) else [float(d)]
            if p.get('Gamma'):
                vals = [fxnodes.srgb_to_linear(x) if i < 3 else x for i, x in enumerate(vals)]
            self.shared[p['Name']] = np.array([(vals + [0.0] * 4)[:4]], np.float32)
        return self.shared

    def param_block(self, eff, key, values):
        """Per-draw block of an engine effect with some parameters set
        ({name: register rows}), cached under key."""
        ck = (id(eff), key)
        b = self.default_blocks.get(ck)
        if b is not None and b[0] is eff:
            return b[1]
        d = np.zeros((DRAW_VEC4, 4), np.float32)
        base = {'float4': {'vs': OFF_VC, 'ps': OFF_PC}, 'bool': {'vs': OFF_VB, 'ps': OFF_PB},
                'int4': {'vs': OFF_VI, 'ps': OFF_PI}}
        for stage, name, reg, count, cls, set_ in eff.sh.draw_entries:
            off = base[set_][stage] + reg
            if name in values:
                rows = values[name][:count]
                d[off:off + len(rows)] = rows
                continue
            dv = eff.meta.defaults.get(name)
            if dv is not None:
                vals = [float(x) for x in dv] if isinstance(dv, (list, tuple)) else [float(dv)]
                d[off] = (vals + [0.0] * 4)[:4]
        ubo = gpu.types.GPUUniformBuf(_gpu_buffer(d))
        self.default_blocks[ck] = (eff, ubo)
        return ubo

    def dynamic_block(self, eff, values):
        """Per-draw block of an engine effect whose values change every frame
        (one buffer per effect, updated in place)."""
        d = np.zeros((DRAW_VEC4, 4), np.float32)
        base = {'float4': {'vs': OFF_VC, 'ps': OFF_PC}, 'bool': {'vs': OFF_VB, 'ps': OFF_PB},
                'int4': {'vs': OFF_VI, 'ps': OFF_PI}}
        for stage, name, reg, count, cls, set_ in eff.sh.draw_entries:
            if name in values:
                rows = values[name][:count]
                off = base[set_][stage] + reg
                d[off:off + len(rows)] = rows
        buf = _gpu_buffer(d)
        ubo = self.dyn_blocks.get(id(eff))
        if ubo is None:
            ubo = self.dyn_blocks[id(eff)] = gpu.types.GPUUniformBuf(buf)
        else:
            ubo.update(buf)
        return ubo

    def _motion_state(self, scene, frame, root_mw):
        """Where the camera and the moving objects are this frame; with last
        frame's, the MotionBlur parameters (_bind_motion_blur_params):
          MotionBlurMatrix[0]  view -> world -> last frame's clip (the camera)
          MotionBlurMatrix[i]  the same through object i's move since last frame
          MotionBlurParams.zw  1 / projection scale (view position from depth)
          MotionBlurScale      (42 / frame ms x g_MotionBlurScale / 30,
                                length of 2 x g_MotionBlurMaxVelocity texels)
        Redrawing the same frame (orbiting the view) blurs nothing."""
        root_inv = root_mw.inverted_safe()
        game_view = frame.view @ root_mw
        dz = Matrix(((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 0.5, 0.5), (0, 0, 0, 1)))
        # the movers' objects, kept from the last full rebuild (looking an object
        # up by name walks every object in the file)
        worlds = {}
        for n, o in self.motion_objs.items():
            try:
                worlds[n] = root_inv @ o.matrix_world
            except ReferenceError:
                pass
        now = {'frame': scene.frame_current, 'vp': dz @ frame.proj @ game_view, 'worlds': worlds}
        # the viewport and F12 renders each keep their own previous frame
        prev = self.mb_now.get(self.motion_key) if self.mb_now else None
        self.mb_now = dict(self.mb_now or {})
        self.mb_now[self.motion_key] = now
        self.mb_values = None
        if prev is None or prev['frame'] != now['frame'] - 1:
            return
        vinv = game_view.inverted_safe()
        mats = [prev['vp'] @ vinv] + [Matrix.Identity(4)] * 15
        for n, i in self.motion_ids.items():
            if i < 16 and n in now['worlds'] and n in prev['worlds']:
                delta = prev['worlds'][n] @ now['worlds'][n].inverted_safe()
                mats[i] = prev['vp'] @ delta @ vinv
        P = frame.proj
        rs = scene.render
        frame_ms = 1000.0 * rs.fps_base / max(1, rs.fps)
        hw, hh = max(1, frame.width // 2), max(1, frame.height // 2)
        maxv = 2.0 * self.engine_value('g_MotionBlurMaxVelocity')
        self.mb_values = {
            'MotionBlurMatrix': np.concatenate([np.array(m, np.float32) for m in mats]),
            'MotionBlurParams': np.array([[0.0, 0.0, 1.0 / P[0][0], 1.0 / P[1][1]]], np.float32),
            'MotionBlurScale': np.array([[(42.0 / frame_ms) * self.engine_value('g_MotionBlurScale') / 30.0,
                                          math.hypot(maxv / hw, maxv / hh), 0.0, 0.0]], np.float32)}

    def _defaults_block(self, eff):
        """Per-draw block of an engine effect: its parameter defaults."""
        b = self.default_blocks.get(id(eff))
        if b is not None and b[0] is eff:
            return b[1]
        d = np.zeros((DRAW_VEC4, 4), np.float32)
        base = {'float4': {'vs': OFF_VC, 'ps': OFF_PC}, 'bool': {'vs': OFF_VB, 'ps': OFF_PB},
                'int4': {'vs': OFF_VI, 'ps': OFF_PI}}
        for stage, name, reg, count, cls, set_ in eff.sh.draw_entries:
            dv = eff.meta.defaults.get(name)
            if dv is None:
                continue
            vals = [float(x) for x in dv] if isinstance(dv, (list, tuple)) else [float(dv)]
            d[base[set_][stage] + reg] = (vals + [0.0] * 4)[:4]
        ubo = gpu.types.GPUUniformBuf(_gpu_buffer(d))
        self.default_blocks[id(eff)] = (eff, ubo)
        return ubo

    def _commands(self, depsgraph, visible):
        """(opaque, blended, casters) draw commands of the scene. Everything a
        draw needs - uniform blocks, textures, render states - is prepared
        here, outside any framebuffer binding (creating a GPU texture may
        rebind framebuffers). They are kept per object: when only some objects
        moved or deformed (self.dirty, e.g. a playing character preview) only
        theirs are rebuilt; anything else (self.full) rebuilds all of them.
        casters: {'dynamic': [...], 'static': [...]} depth-only draws for the
        shadow maps, chosen by the parts' RenderCaps like the game does."""
        # a frame change only redraws (self.version): the commands stay
        if self.cmds is not None and self.cmds[0] == self.cmd_version:
            return self.cmds[1], self.cmds[2], self.cmds[3]
        t0 = time.perf_counter()
        scene = depsgraph.scene
        root = scene.c2.root
        root_mw = root.matrix_world.copy() if root is not None else Matrix.Rotation(math.pi / 2, 4, 'X')
        game = fxnodes.game_context(scene)
        ctx = {'root': root, 'root_inv': root_mw.inverted_safe(), 'assets': common.game_assets(scene),
               'textures': game.textures if game is not None else None,
               'gtime': fxnodes.ENGINE_VALUES.get(('g_Time', 0), (0.0,))[0],
               'caps': self.node_caps(scene), 'shadows': scene.c2.game_shadows, 'visible': visible}
        partial = not self.full and self.cmds is not None
        if partial:
            for name in self.dirty:
                ob = bpy.data.objects.get(name)
                if ob is not None and ob.instance_type == 'COLLECTION' and ob.instance_collection is not None:
                    partial = False         # instancers: their instances are rebuilt with everything
                    break
        if partial:
            relist = False      # the flattened lists must be rebuilt (something structural changed)
            recast = False
            for name in self.dirty:
                ob = bpy.data.objects.get(name)
                if ob is None:
                    self.obj_draw.pop(name, None)
                    self.obj_cast.pop(name, None)
                    relist = recast = True
                    continue
                if not self._refresh(depsgraph, ob, name, ctx):
                    ev = ob.evaluated_get(depsgraph)
                    self.obj_draw[name] = self._object_draw(depsgraph, ev, ob, ev.matrix_world.copy(), name,
                                                            False, ctx)
                    relist = True
                old_cast = self.obj_cast.get(name)
                new_cast = self._object_cast(depsgraph, ob, ctx)
                if old_cast or new_cast[0] or new_cast[1]:
                    self.obj_cast[name] = new_cast
                    recast = True
            if not relist:
                # every change was made in place: keep the sorted lists, refresh
                # only the shadow casters if one of them moved
                self.dirty.clear()
                _v, opaque, blended, casters = self.cmds
                if recast:
                    casters = self._caster_lists()
                self.cmds = (self.cmd_version, opaque, blended, casters)
                self.stats['build_ms'] = (time.perf_counter() - t0) * 1000.0
                return opaque, blended, casters
        else:
            # a soft rebuild (objects shown / hidden / added: the Track / Mode / Lap pickers, a
            # preview loaded) keeps the commands of every object whose visibility, place, mesh and
            # materials are what they were - rebuilding all ~6300 of italy's took ~0.7 s each time
            soft = self.soft_full
            old_draw, old_keys = self.obj_draw, self.obj_keys
            self.obj_draw = {}
            self.obj_keys = {}
            self.obj_cast = {}
            movers = sorted((o for o in scene.objects if o.type == 'MESH' and o.get('c2_preview')), key=lambda o: o.name)
            self.motion_ids = {o.name: i + 1 for i, o in enumerate(movers[:15])}
            self.motion_objs = {o.name: o for o in movers[:15]}
            for inst in depsgraph.object_instances:
                ob = inst.object
                if ob.type != 'MESH':
                    continue
                orig = ob.original
                # an instance: unique per instancer too (every collect gate / preview copy instances the
                # same objects, and the index inside the collection alone made them overwrite each other)
                name = orig.name + ('#%s#%d' % (inst.parent.original.name if inst.parent else '', inst.persistent_id[0])
                                    if inst.is_instance else '')
                mw = inst.matrix_world.copy()
                key = None
                if not orig.get('c2_preview') and name not in self.dirty:
                    key = self._draw_key(orig, mw, inst.is_instance, ctx)
                    if soft and key is not None and old_keys.get(name) == key and name in old_draw:
                        self.obj_draw[name] = old_draw[name]
                        self.obj_keys[name] = key
                        continue
                self.obj_draw[name] = self._object_draw(depsgraph, ob, orig, mw, name, inst.is_instance, ctx)
                if key is not None:
                    self.obj_keys[name] = self._draw_key(orig, mw, inst.is_instance, ctx)
            if ctx['shadows']:
                for ob in scene.objects:
                    self.obj_cast[ob.name] = self._object_cast(depsgraph, ob, ctx)
        self.full = False
        self.soft_full = False
        self.dirty.clear()
        opaque = [c for v in self.obj_draw.values() for c in v[0]]
        blended = [c for v in self.obj_draw.values() for c in v[1]]
        refl = [c for v in self.obj_draw.values() for c in v[2]]
        plane = next((v[3] for v in self.obj_draw.values() if v[3] is not None), None)
        self.refl = ([c for c in refl if c.blend == 'NONE'], [c for c in refl if c.blend != 'NONE'], plane)
        casters = self._caster_lists()
        _game_order(opaque)
        _game_order(self.refl[0])
        self.bounds = {k: (np.array([tuple(c.center) for c in lst], np.float64).reshape(-1, 3),
                           np.array([c.radius for c in lst], np.float64))
                       for k, lst in (('opaque', opaque), ('blended', blended))}
        # where each command's bounds sit (in-place refreshes update them there)
        self.cmd_pos = {id(c): (k, i) for k, lst in (('opaque', opaque), ('blended', blended))
                        for i, c in enumerate(lst)}
        self.cmds = (self.cmd_version, opaque, blended, casters)
        self.stats['parts'] = len(opaque) + len(blended)
        self.stats['casters'] = (len(casters['dynamic']), len(casters['static']))
        self.stats['build_ms'] = (time.perf_counter() - t0) * 1000.0
        return opaque, blended, casters

    def _caster_lists(self):
        """The flattened shadow-caster draws and their bounding spheres (game space)."""
        casters = {'dynamic': [i for d, _s in self.obj_cast.values() for i in d],
                   'static': [i for _d, s in self.obj_cast.values() for i in s]}
        casters['bounds'] = {k: (np.array([i[4] for i in casters[k]], np.float64).reshape(-1, 3),
                                 np.array([i[5] for i in casters[k]], np.float64))
                             for k in ('dynamic', 'static')}
        return casters

    def _object_draw(self, depsgraph, ob, orig, mw, name, is_instance, ctx):
        """(opaque, blended, reflection commands, reflection plane height or
        None) of one object (or instance)."""
        if ob.type != 'MESH' or orig.display_type in ('WIRE', 'BOUNDS'):
            return [], [], [], None
        visible = ctx['visible']
        if visible is not None and not is_instance and not visible(orig):
            return [], [], [], None
        if _collapsed(mw):
            return [], [], [], None             # scaled to nothing (a map object not spawned yet)
        entry = self.mesh(ob, depsgraph, ctx['assets'], ctx['caps'])
        world = ctx['root_inv'] @ mw
        center = mw @ Vector(entry.center)
        radius = entry.radius * max(mw.to_scale()) + 0.01
        opaque, blended, refl = [], [], []
        plane = None
        for slot, (mat, batch, eff, caps, eff_r) in enumerate(entry.parts):
            if mat is not None:
                self.mat_users.setdefault(mat.name, set()).add(name)
                if mat.c2.variant_of is not None:
                    self.mat_users.setdefault(mat.c2.variant_of.name, set()).add(name)
            pkg = _package(mat) if mat is not None else None
            tex = pkg.textures if pkg else ctx['textures']
            c = self._command(name, slot, mat, batch, eff, world, tex, ctx['gtime'])
            c.center = center
            c.radius = radius
            c.layer = caps & 0x3F
            c.motion_id = self.motion_ids.get(orig.name, 0)
            c.order = (orig.c2.node_index, slot, (id(c.shader), mat.name if mat is not None else ''))
            (blended if c.blend != 'NONE' else opaque).append(c)
            if caps & gamereflect.REFLECT_BIT and (eff_r is not None or eff is None):
                r = self._command(name + '|R', slot, mat, batch, eff_r, world, tex, ctx['gtime'])
                r.center = center
                r.radius = radius
                r.layer = caps & 0x3F
                r.order = (orig.c2.node_index, slot, (id(r.shader), mat.name if mat is not None else ''))
                refl.append(r)
            # the first part whose material reflects sets the plane (FUN_0063c170)
            if plane is None and eff is not None and mat is not None and 'g_AlphaReflection' in eff.meta.defaults \
                    and fxnodes.param_value(mat, eff.meta, 'g_AlphaReflection', 0, 0) > 0.0:
                plane = world.translation.y
        return opaque, blended, refl, plane

    def _object_cast(self, depsgraph, ob, ctx):
        """(dynamic, static) shadow-caster draws of one object: every part with
        a caster bit. The world's helper meshes (c2_helper: the shipped casters
        are invisible shadowonly proxies) cast whether shown or not, like in the
        game; anything else only while it is shown (a map object the Track /
        Mode picker hid, a hidden template, a hidden collection casts nothing).
        Preview cars and characters cast dynamic shadows.
        Each item: (batch, world gl, cutout, motion id 0, game-space centre, radius)."""
        if not ctx['shadows'] or ob.type != 'MESH' or ob.data is None:
            return [], []
        root = ctx['root']
        if not ob.get('c2_preview') and not ob.get('c2_map_object') and not (root is not None and _below(ob, root)):
            return [], []
        visible = ctx['visible']
        if ob.get('c2_helper') is None and visible is not None and not visible(ob):
            return [], []
        if _collapsed(ob.matrix_world):
            return [], []
        entry = self.mesh(ob, depsgraph, ctx['assets'], ctx['caps'])
        world = ctx['root_inv'] @ ob.matrix_world
        wgl = [x for r in world.transposed() for x in r]
        center = tuple(world @ Vector(entry.center))
        radius = entry.radius * max(world.to_scale()) + 0.01
        dyn, st = [], []
        for mat, batch, eff, caps, _er in entry.parts:
            if not caps & (gameshadow.DYNAMIC_BIT | gameshadow.STATIC_BIT):
                continue
            item = (batch, wgl, self._cutout(mat, eff, ctx['textures']), 0, center, radius)
            if caps & gameshadow.DYNAMIC_BIT:
                dyn.append(item)
            if caps & gameshadow.STATIC_BIT:
                st.append(item)
        return dyn, st

    def _cutout(self, mat, eff, textures):
        """(diffuse texture, alpha reference) of an alpha-tested / texkill part
        for depth-only drawing, else (None, 0)."""
        if eff is None or mat is None:
            return None, 0.0
        meta = eff.meta
        test = bool(fxnodes._state(meta, mat, 'AlphaTestEnable', 0))
        if not test and not eff.sh.kill:
            return None, 0.0
        for name, ti, typ, cname in eff.samplers:
            if typ != '2d' or not str(meta.sampler_usage.get(cname, '')).startswith('diffuseMapA1'):
                continue
            usage, idx = fxnodes.texture_index(mat, meta, cname)
            pkg = _package(mat)
            src = pkg.textures if pkg else textures
            img = src.images.get(idx) if src is not None and idx is not None else None
            tex = self.textures.image(img) if img is not None else None
            if tex is None:
                return None, 0.0
            ref = float(fxnodes._state(meta, mat, 'AlphaRef', 128)) / 255.0 if test else 0.5
            return tex, ref
        return None, 0.0

    def _command(self, name, slot, mat, batch, eff, world, textures, gtime):
        """A prepared draw (_Cmd) for one material part of an object."""
        c = _Cmd()
        c.batch = batch
        c.mat, c.eff, c.slot = mat, eff, slot
        c.mirrored = world.determinant() < 0
        c.world_gl = [x for r in world.transposed() for x in r]
        if eff is None or mat is None:
            col, img = _fallback_look(mat)
            tex = self.textures.image(img) if img is not None else None
            c.shader = self._fallback_shader()
            c.binds = [('tex', tex or self.textures.constant((1.0, 1.0, 1.0, 1.0)))]
            c.fallback = (c.world_gl, col)
            alpha = col[3] < 0.999
            c.blend, c.zwrite, c.ztest, c.cull = ('ALPHA' if alpha else 'NONE'), not alpha, 'LESS_EQUAL', 'NONE'
            return c
        c.shader = eff.shader
        c.key = (name, slot, mat.name)
        c.ubo, c.binds = self.block(c.key, mat, eff, textures, world, gtime)
        self._states(c, mat, eff, textures)
        return c

    def _states(self, c, mat, eff, textures):
        """A command's cut-out, flipbook and render states from its material."""
        meta = eff.meta
        c.cutout = self._cutout(mat, eff, textures)
        c.dynamic = (mat, eff, textures) if fxnodes.texture_animation(mat) else None
        c.blend = 'NONE'
        if bool(fxnodes._state(meta, mat, 'AlphaBlendEnable', 0)):
            src = str(fxnodes._state(meta, mat, 'SrcBlend', 'SRCALPHA')).upper()
            dst = str(fxnodes._state(meta, mat, 'DestBlend', 'INVSRCALPHA')).upper()
            c.blend = BLENDS.get((src, dst), 'ALPHA')
        # the game lays depth down in an earlier pass, so Standard techniques
        # often say ZWriteEnable=0: opaque and cut-out parts write it here
        c.zwrite = bool(fxnodes._state(meta, mat, 'ZWriteEnable', 1)) if c.blend != 'NONE' else True
        c.ztest = 'LESS_EQUAL' if bool(fxnodes._state(meta, mat, 'ZEnable', 1)) else 'ALWAYS'
        c.cull = 'BACK' if bool(fxnodes._state(meta, mat, 'CullBackFaces', 1)) else 'NONE'

    def _refresh(self, depsgraph, ob, name, ctx):
        """Bring an object's existing draw commands up to date in place: a
        moved, deformed (armature) or re-keyed (material values) object keeps
        its commands, only their mesh batch, world matrix, uniform block,
        states and culling bounds change. The flattened, sorted command lists
        stay as they are: rebuilding and re-sorting all ~7000 of tokyo_a's
        commands because one character moved cost ~30 ms a frame. False when
        something structural changed (shown / hidden, parked at zero scale, a
        part's material, opaque <-> blended): the caller rebuilds it."""
        if ob.type != 'MESH' or ob.display_type in ('WIRE', 'BOUNDS'):
            return True             # draws nothing either way (a camera, an empty, a helper)
        old = self.obj_draw.get(name)
        had = bool(old and (old[0] or old[1]))
        visible = ctx['visible']
        ev = ob.evaluated_get(depsgraph)
        mw = ev.matrix_world.copy()
        shown = (visible is None or visible(ob)) and not _collapsed(mw)
        if not had:
            return not shown        # still hidden / parked: nothing to update
        if not shown:
            return False
        entry = self.mesh(ev, depsgraph, ctx['assets'], ctx['caps'])     # re-deforms a bent mesh
        cmds = old[0] + old[1] + old[2]
        for c in cmds:
            # a part's material, or its effect (a shader switch), changed: rebuild. A
            # refresh in place kept the old effect's shader, so a switched material
            # drew as before (or black) until something rebuilt everything
            # (a mirror-pass command draws with the effect's Reflect technique: eff_r)
            if c.slot >= len(entry.parts) or entry.parts[c.slot][0] is not c.mat or \
                    (c.eff is not entry.parts[c.slot][2] and c.eff is not entry.parts[c.slot][4]):
                return False
        world = ctx['root_inv'] @ mw
        wgl = [x for r in world.transposed() for x in r]
        mirrored = world.determinant() < 0
        center = mw @ Vector(entry.center)
        radius = entry.radius * max(mw.to_scale()) + 0.01
        pos = self.cmd_pos
        for c in cmds:
            mat, batch, eff, caps, eff_r = entry.parts[c.slot]
            c.batch = batch
            c.mirrored = mirrored
            c.world_gl = wgl
            c.center = center
            c.radius = radius
            if c.fallback is not None:
                c.fallback = (wgl, c.fallback[1])
            else:
                pkg = _package(mat)
                tex = pkg.textures if pkg else ctx['textures']
                was = c.blend != 'NONE'
                c.ubo, c.binds = self.block(c.key, mat, c.eff, tex, world, ctx['gtime'])
                self._states(c, mat, c.eff, tex)
                if (c.blend != 'NONE') != was:
                    return False        # moved between the opaque and blended lists
            at = pos.get(id(c))
            if at is not None:
                ctr, rad = self.bounds[at[0]]
                ctr[at[1]] = tuple(center)
                rad[at[1]] = radius
        return True

    def _shadow_passes(self, depsgraph, frame, root_mw, opaque, casters, assets):
        """Depth pre-pass, shadow maps and the game's shadow-mask pass. Returns
        (engine rows for the shadow matrices, {sampler name: texture})."""
        scene = depsgraph.scene
        sh = self.shadows
        white = self.textures.constant((1.0, 1.0, 1.0, 1.0))
        game_view = frame.view @ root_mw
        hires = scene.c2.game_hires_shadows
        mask_eff = effect_for(assets, gameshadow.MASK_EFFECT[hires], 'Default')
        sun = fxnodes.ENGINE_VALUES.get(('g_sunLightDirection', 0), (0.0, 1.0, 0.0))
        dist = scene.c2.game_shadow_distance
        rows = sh.setup(frame, game_view, sun[:3], dist, hires)
        depth = sh.prepass(frame, game_view, [(c.batch, c.world_gl, c.cutout, c.motion_id) for c in opaque], white)
        # the maps depend on the casters and the cascades only: playing the
        # timeline with a still view and nothing moving reuses them
        key = (self.cmd_version, id(casters),
               tuple(tuple(x for r in c.vp_gl for x in r) for k in ('dynamic', 'static') for c in sh.cascades[k][1]),
               sh.cascades['dynamic'][0], sh.cascades['static'][0])
        if key != self.shadow_key or self.shadow_tex is None:
            self.shadow_tex = sh.render_maps(casters, white)
            self.shadow_key = key
            self.stats['shadow_maps'] = 'drawn'
        else:
            self.stats['shadow_maps'] = 'reused'
        maps = self.shadow_tex
        tex = {'samDepthViewBuffer': depth, 'samDepthViewHalfBuffer': depth,
               'samShadowTexture': maps['dynamic'], 'samShadowTexture1': maps['static']}
        if mask_eff is None:
            tex['samShadowMask'] = white
            return rows, tex
        # the mask effect: its sampler textures and an (almost empty) per-draw block
        game = fxnodes.game_context(scene)
        binds = []
        for name, ti, typ, cname in mask_eff.samplers:
            if cname == 'samRotateMap':
                t = sh.rotate_map()
            elif cname == 'samCloudTexture1':
                t = None
                if game is not None and 'CloudTexture1' in game.textures.global_entries:
                    img = game.textures.images.get(game.textures.global_entries['CloudTexture1'])
                    t = self.textures.image(img) if img is not None else None
                t = t or self.textures.constant((0.0, 0.0, 0.0, 0.0))   # no clouds: nothing subtracted
            else:
                t = tex.get(cname) or white
            binds.append((name, t))
        if self.mask_block is None:
            self.mask_block = gpu.types.GPUUniformBuf(_gpu_buffer(np.zeros((DRAW_VEC4, 4), np.float32)))
        tex['samShadowMask'] = (rows, mask_eff, game_view, binds)   # run after the engine block exists
        return rows, tex

    def _draw_scene(self, depsgraph, frame, fb, visible):
        """Reflection, shadow and depth passes, then the prepared commands into
        the HDR target (opaque, a copy of the colour for refraction, blended)."""
        scene = depsgraph.scene
        root = scene.c2.root
        root_mw = root.matrix_world.copy() if root is not None else Matrix.Rotation(math.pi / 2, 4, 'X')
        opaque, blended, casters = self._commands(depsgraph, visible)
        assets = common.game_assets(scene)
        self.load_shared(assets)
        # converted before the engine block is filled, so its globals have slots
        self.post_effect = effect_for(assets, 'postprocess', 'PostProcess')
        effs = {'highpass': effect_for(assets, 'highpass', 'HighPass'),
                'down': effect_for(assets, 'downsamplegauss', 'DownsampleGauss'),
                'blurh': effect_for(assets, 'glowgauss', 'BlurH9'),
                'blurv': effect_for(assets, 'glowgauss', 'BlurV9'),
                'accum': effect_for(assets, 'upsampleaccum', 'Default')}
        self.bloom_effs = effs if all(effs.values()) else None
        refl_blur = (effect_for(assets, 'glowgauss', 'BlurH5'), effect_for(assets, 'glowgauss', 'BlurV5'))
        mb = (effect_for(assets, 'motionblurprepare', 'MotionBlurPrepare'), effect_for(assets, 'motionblur', 'MotionBlur'))
        self.mb_effs = mb if all(mb) else None
        self._motion_state(scene, frame, root_mw)
        # frustum culling (bounding spheres against the six clip planes)
        cam_pos = frame.view.inverted_safe().translation
        planes = _frustum_planes(frame.proj @ frame.view)
        near_o = []
        # in the viewport, parts smaller than a pixel on screen are left out too (a big world's
        # distant bolts, signs and lamps are ~a third of its draws and add nothing visible).
        # F12 renders draw everything
        tiny = _subpixel(frame, cam_pos) if visible is not None else None
        if len(opaque):
            ctr, rad = self.bounds['opaque']
            keep = _in_frustum(planes, ctr, rad)
            if tiny is not None:
                keep &= ~tiny(ctr, rad)
            # the shadow mask only matters where shadows can fall (static map: 150 m)
            near = keep & (np.linalg.norm(ctr - np.array(cam_pos), axis=1) - rad < PREPASS_RANGE)
            near_o = [c for c, k in zip(opaque, near) if k]
            opaque = [c for c, k in zip(opaque, keep) if k]
        if len(blended):
            ctr, rad = self.bounds['blended']
            keep = _in_frustum(planes, ctr, rad)
            if tiny is not None:
                keep &= ~tiny(ctr, rad)
            blended = [c for c, k in zip(blended, keep) if k]
        self.stats['drawn'] = len(opaque) + len(blended)
        # ambient light volumes (ambientvol.py) light whatever they cover, out
        # to any distance: the depth pre-pass then needs every opaque part
        ambientvol = _optional('ambientvol')
        vols = ambientvol.volumes(depsgraph, root_mw.inverted_safe(), visible) if ambientvol else []
        if vols:
            near_o = opaque
        self.stats['ambient_volumes'] = len(vols)
        gtime = fxnodes.ENGINE_VALUES.get(('g_Time', 0), (0.0,))[0]
        r_opaque, r_blended, plane_y = self.refl
        for c in opaque + blended + r_opaque + r_blended:
            if c.dynamic is not None:          # flipbook textures follow the time
                c.binds = self._bindings(c.dynamic[0], c.dynamic[1], c.dynamic[2], gtime)[0]
        ex = fxnodes.ENGINE_VALUES.get(('g_HDR_Exposure', 0), (0.18,))[0]
        avg = fxnodes.ENGINE_VALUES.get(('avgLum', 0), (0.18,))[0]
        fscale = avg / max(ex, 1e-6)
        white = self.textures.constant((1.0, 1.0, 1.0, 1.0))
        extra = {}
        frame_tex = {}

        # -- planar reflection (gamereflect): the mirror view of the reflection casters
        t0 = time.perf_counter()
        if scene.c2.game_reflections and plane_y is not None and (r_opaque or r_blended):
            fog_end = self.engine_value('g_ReflectionFogEnd')
            mframe, mgame_view = gamereflect.Reflection.mirror_frame(frame, root_mw, plane_y, fog_end, Frame)
            mplanes = _frustum_planes(mframe.proj @ mframe.view)
            mcam = mframe.view.inverted_safe().translation

            def in_mirror(lst):
                if not lst:
                    return []
                ctr = np.array([tuple(c.center) for c in lst], np.float64)
                rad = np.array([c.radius for c in lst], np.float64)
                keep = _in_frustum(mplanes, ctr, rad) & \
                    (np.linalg.norm(ctr - np.array(mcam), axis=1) - rad < fog_end)   # far plane = fog end
                return [c for c, k in zip(lst, keep) if k]
            ro, rb = in_mirror(r_opaque), in_mirror(r_blended)
            rb.sort(key=lambda c: (c.layer, -(c.center - mcam).length_squared))
            meng = self.engine_block(mframe, root_mw, which='mirror')
            w, h, rfb, rcol, _rdep, rfb2, rcol2 = self.reflection.targets(frame.width, frame.height)
            mtex = {'samShadowMask': white}
            with rfb.bind():
                gpu.state.viewport_set(0, 0, w, h)
                rfb.clear(color=(0.0, 0.0, 0.0, 1.0), depth=1.0)
                self._run_cmds(ro + rb, meng, mtex, fscale, flip=True)
            out = rcol
            if all(refl_blur):
                bh, bv = refl_blur
                o, wt = gamepost.glow_gauss_params(w, h, 5)
                vals = {'pixelOffset': [(u, v, 0.0, 0.0) for u, v in o],
                        'pixelWeight': [(x, 0.0, 0.0, 0.0) for x in wt]}
                gamepost._run_pass(bh, rfb2, w, h, meng, self.param_block(bh, ('rblur', w, h), vals),
                                   [(n, rcol) for n, _t, _y, _c in bh.samplers])
                gamepost._run_pass(bv, rfb, w, h, meng, self.param_block(bv, ('rblur', w, h), vals),
                                   [(n, rcol2) for n, _t, _y, _c in bv.samplers])
            frame_tex['samReflectionPlaneTexture'] = out
            dz = Matrix(((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 0.5, 0.5), (0, 0, 0, 1)))
            extra['g_ReflectionPlaneProj'] = np.array(dz @ mframe.proj @ mgame_view, np.float32)
            self.stats['reflect'] = len(ro) + len(rb)
        self.stats['reflect_ms'] = (time.perf_counter() - t0) * 1000.0

        # -- depth pre-pass, shadow maps and mask (gameshadow)
        t0 = time.perf_counter()
        if scene.c2.game_shadows:
            rows, stex = self._shadow_passes(depsgraph, frame, root_mw, near_o, casters, assets)
            extra.update(rows)
            frame_tex.update(stex)
            eng = self.engine_block(frame, root_mw, extra)
            pending = frame_tex.get('samShadowMask')
            if isinstance(pending, tuple):
                _rows, mask_eff, game_view, binds = pending
                frame_tex['samShadowMask'] = self.shadows.mask_pass(mask_eff, frame, game_view, eng,
                                                                    self.mask_block, binds)
        else:
            # the view depth is still needed (refraction, soft particles)
            depth = self.shadows.prepass(frame, frame.view @ root_mw,
                                         [(c.batch, c.world_gl, c.cutout, c.motion_id) for c in near_o], white)
            frame_tex['samDepthViewBuffer'] = frame_tex['samDepthViewHalfBuffer'] = depth
            eng = self.engine_block(frame, root_mw, extra)
        self.stats['shadow_ms'] = (time.perf_counter() - t0) * 1000.0
        # the ambient buffer: the light volumes over the view depth (black without any)
        if vols and frame_tex.get('samDepthViewBuffer') is not None:
            frame_tex['samAmbientBuffer'] = ambientvol.render(vols, frame, frame.view @ root_mw,
                                                              frame_tex['samDepthViewBuffer'], frame.width,
                                                              frame.height)

        # -- the frame: opaque, the refraction copy, blended back to front
        # the game's order (FUN_0087ac90 sort key): the part's draw layer
        # (RenderCaps & 0x3F, e.g. foam strips 2 over the ocean 0), then back to front
        blended = sorted(blended, key=lambda c: (c.layer, -(c.center - cam_pos).length_squared))
        refract = any(isinstance(t, str) and t == 'samColorBuffer' for c in blended for _n, t in c.binds)
        with fb.bind():
            gpu.state.viewport_set(0, 0, frame.width, frame.height)
            fb.clear(color=(0.0, 0.0, 0.0, 1.0), depth=1.0)
            self._run_cmds(opaque, eng, frame_tex, fscale)
        if refract:
            # what the water sees through itself: the opaque scene (the game's colorBuffer)
            frame_tex['samColorBuffer'] = self.post.copy(self.targets[3], frame.width, frame.height)
        with fb.bind():
            gpu.state.viewport_set(0, 0, frame.width, frame.height)
            self._run_cmds(blended, eng, frame_tex, fscale)
            # the grandstand crowds (crowd.py): alpha-tested sprites
            crowd = _optional('crowd')
            try:
                if crowd is not None:
                    self.stats['crowd'] = crowd.draw(scene, frame.proj @ frame.view, frame.view, fscale)
            except Exception as e:
                print('Cars 2 crowd: not drawn (%s)' % e)
            # choreography particles (particles.py): camera-facing quads, last
            particles = _optional('particles')
            try:
                if particles is not None:
                    self.stats['particles'] = particles.draw(scene, frame.proj @ frame.view, frame.view, fscale)
            except Exception as e:      # never lose the frame over its particles
                print('Cars 2 particles: not drawn (%s)' % e)

    def _run_cmds(self, cmds, eng, frame_tex, fscale, flip=False):
        """Draw prepared commands into the bound framebuffer. flip: mirrored
        view (the winding of every triangle is reversed)."""
        st = gpu.state
        cur = [None] * 6        # shader, blend, zwrite, ztest, cull, front facing
        # frame textures (FRAME_TEXTURES) resolved once per pass, not per part:
        # tokyo_a draws ~4000 parts, each naming a few of them
        frame = {}

        def frame_texture(name):
            t = frame.get(name)
            if t is None:
                t = frame[name] = frame_tex.get(name) or self.textures.constant(ENGINE_TEXTURES.get(name, (1, 1, 1, 1)))
            return t
        bound = {}              # sampler -> texture bound since the shader was bound
        for c in cmds:
            sh = c.shader
            if cur[0] is not sh:
                sh.bind()
                sh.uniform_block('eng', eng)
                if c.fallback is not None:
                    sh.uniform_float('scale', fscale)
                cur[0] = sh
                bound = {}
            if c.fallback is not None:
                sh.uniform_float('world', c.fallback[0])
                sh.uniform_float('color', c.fallback[1])
            else:
                sh.uniform_block('drw', c.ubo)
            for sname, tex in c.binds:
                if isinstance(tex, str):       # a frame texture (FRAME_TEXTURES)
                    tex = frame_texture(tex)
                if bound.get(sname) is not tex:
                    sh.uniform_sampler(sname, tex)
                    bound[sname] = tex
            if cur[1] != c.blend:
                st.blend_set(c.blend)
                cur[1] = c.blend
            if cur[2] != c.zwrite:
                st.depth_mask_set(c.zwrite)
                cur[2] = c.zwrite
            if cur[3] != c.ztest:
                st.depth_test_set(c.ztest)
                cur[3] = c.ztest
            if cur[4] != c.cull:
                st.face_culling_set(c.cull)
                cur[4] = c.cull
            ff = c.mirrored != flip
            if cur[5] != ff:
                st.front_facing_set(ff)
                cur[5] = ff
            c.batch.draw(sh)
        st.depth_test_set('NONE')
        st.depth_mask_set(False)
        st.blend_set('NONE')
        st.face_culling_set('NONE')
        st.front_facing_set(False)


def _frustum_planes(m):
    """(6, 4) plane equations (normal, d) of a GL view-projection's frustum."""
    a = np.array(m, np.float64)
    pl = np.array([a[3] + a[0], a[3] - a[0], a[3] + a[1], a[3] - a[1], a[3] + a[2], a[3] - a[2]])
    n = np.linalg.norm(pl[:, :3], axis=1, keepdims=True)
    return pl / np.maximum(n, 1e-12)


SUBPIXEL = 0.75     # projected radius (pixels) under which a viewport part isn't drawn


def _subpixel(frame, cam_pos):
    """A test (centres, radii) -> bool per sphere: smaller than SUBPIXEL pixels on screen
    in this (perspective) view, or None for an orthographic one."""
    p = frame.proj
    if abs(p[3][3]) > 1e-6:         # orthographic: sizes don't shrink with distance
        return None
    scale = abs(p[1][1]) * frame.height * 0.5
    cam = np.array(cam_pos, np.float64)

    def test(centers, radii):
        dist = np.linalg.norm(centers - cam, axis=1)
        return (radii * scale) < (SUBPIXEL * np.maximum(dist - radii, 1e-3))
    return test


def _in_frustum(planes, centers, radii):
    """Bool per sphere: inside or touching the frustum."""
    d = centers @ planes[:, :3].T + planes[:, 3]
    return np.all(d > -radii[:, None], axis=1)


def _below(ob, root):
    """Is ob somewhere under the world root?"""
    p = ob.parent
    while p is not None:
        if p == root:
            return True
        p = p.parent
    return False


def _game_order(cmds):
    """Sort opaque draws like the game's command key (FUN_0087ac90 /
    r_allocPass): the draw layer (RenderCaps & 0x3F) first, then the pass
    slot, which the game hands out the first time a material's pass is
    submitted while walking the scene in node order. The order matters where
    depth does not decide: the sky domes (oilrig's star_noise1 / outer_dome)
    are all drawn at the far plane, so the later one wins."""
    rank = {}
    for c in sorted(cmds, key=lambda c: c.order[:2]):
        rank.setdefault(c.order[2], len(rank))
    cmds.sort(key=lambda c: (c.layer, rank[c.order[2]]))


class _Cmd:
    """One prepared draw: shader + uniform block + textures + render states."""
    __slots__ = ('shader', 'ubo', 'binds', 'blend', 'zwrite', 'ztest', 'cull', 'mirrored', 'batch', 'center',
                 'dynamic', 'fallback', 'world_gl', 'cutout', 'radius', 'layer', 'motion_id', 'order',
                 'key', 'eff', 'mat', 'slot')

    def __init__(self):
        self.ubo = None
        self.binds = []
        self.blend = 'NONE'
        self.zwrite = True
        self.ztest = 'LESS_EQUAL'
        self.cull = 'BACK'
        self.mirrored = False
        self.center = None
        self.dynamic = None
        self.fallback = None
        self.world_gl = None
        self.cutout = (None, 0.0)
        self.radius = 0.0
        self.layer = 0              # draw layer: RenderCaps & 0x3F
        self.motion_id = 0          # motion-blur object id (0: moves with the world)
        self.order = (0, 0, None)   # (node index, part, pass key) for the game's opaque order
        self.key = None             # its uniform block's cache key (Renderer.block)
        self.eff = None
        self.mat = None
        self.slot = 0               # the mesh part it draws


def _fallback_alpha(mat):
    return float(mat.diffuse_color[3]) if mat is not None else 1.0


def _fallback_look(mat):
    """(RGBA, image) for a material without a game shader: its simplified
    material's diffuse image, else its first image node, and its viewport colour."""
    if mat is None:
        return (0.6, 0.6, 0.6, 1.0), None
    img = None
    nt = mat.node_tree
    if nt is not None:
        n = nt.nodes.get('C2_Diffuse')
        if n is None or getattr(n, 'image', None) is None:
            n = next((x for x in nt.nodes if x.type == 'TEX_IMAGE' and x.image is not None), None)
        img = n.image if n is not None else None
    col = tuple(mat.diffuse_color)
    if img is not None:
        col = (1.0, 1.0, 1.0, col[3])
    return col, img


RENDERER = Renderer()


def touch_material(mat):
    """A material changed (parameters, textures, effect): rebuild its per-draw blocks."""
    if mat is None:
        return
    RENDERER.touch_material(mat)
    for m in fxnodes.variants_of(mat):
        RENDERER.touch_material(m)


def effect_changed(mat):
    """A material's effect changed: the meshes drawing it were built with the old
    effect's vertex inputs (a normal-mapped effect needs tangents ...). A shader
    reading inputs its batch doesn't carry drew the part flat white until
    something rebuilt everything: rebuild those meshes too."""
    if mat is None:
        return
    for m in [mat] + fxnodes.variants_of(mat):
        for name in list(RENDERER.mat_users.get(m.name, ())):
            RENDERER.drop_mesh(name.split('#')[0])
    touch_material(mat)


def textures_changed():
    """A TexturePool image was replaced: drop the GPU texture cache."""
    RENDERER.textures.clear()
    RENDERER.blocks.clear()
    RENDERER.mat_cache.clear()
    RENDERER.rebuild()


# ----------------------------------------------------------------------
# the render engine
# ----------------------------------------------------------------------

def _view_frame(context):
    region = context.region
    rv3d = context.region_data
    space = context.space_data
    near, far = space.clip_start, space.clip_end
    if rv3d.view_perspective == 'CAMERA' and context.scene.camera is not None:
        cam = context.scene.camera.data
        near, far = cam.clip_start, cam.clip_end
    # the game draws out to its own far plane (scene_settings SceneFarPlane):
    # a short viewport clip end would cut the sea off mid-way
    far2 = max(far, RENDERER.engine_value('SceneFarPlane'))
    frame = Frame(rv3d.view_matrix.copy(), _with_far(rv3d.window_matrix, near, far2), region.width, region.height,
                  near, far2)
    frame.depth_proj = rv3d.window_matrix.copy()     # what Blender's overlays expect in the depth buffer
    return frame


_kids = {'key': None, 'map': {}}


def _changed(r, o):
    """An object's mesh may have changed: drop it (Edit Mode, deformed, modifiers) or have
    it checked against its fingerprint on the next draw (anything else)."""
    if o.type == 'MESH' and o.mode != 'EDIT' and not len(o.modifiers) and not _deforms(o) \
            and o.name in r.meshes and getattr(r.meshes[o.name], 'exact', None) is not None:
        r.suspect.add(o.name)
        r.mark(o.name)
    else:
        r.drop_mesh(o.name, deform_only=o.mode != 'EDIT' and _deforms(o))


def _exact(obj):
    """Every position, corner and UV of an object's mesh, hashed, with its materials and
    modifiers: a change anywhere shows. Used to keep a drawn mesh when Blender reports an
    update that didn't change it (shown / hidden) and across undo / redo (undo leaves the
    data-blocks it didn't change where they were)."""
    me = obj.data
    if me is None or not hasattr(me, 'vertices') or obj.mode == 'EDIT':
        return None                     # (in Edit Mode the mesh's own arrays aren't filled in)
    try:
        nv, nl = len(me.vertices), len(me.loops)
        co = np.empty(nv * 3, np.float32)
        me.vertices.foreach_get('co', co)
        lv = np.empty(nl, np.int32)
        me.loops.foreach_get('vertex_index', lv)
        uvh = 0
        for layer in me.uv_layers:
            if len(layer.data) != nl:
                return None
            uv = np.empty(nl * 2, np.float32)
            layer.data.foreach_get('uv', uv)
            uvh = hash((uvh, uv.tobytes()))
    except (TypeError, RuntimeError, ReferenceError):
        return None
    return (me.as_pointer(), me.name, nv, nl, len(me.polygons), hash(co.tobytes()), hash(lv.tobytes()), uvh,
            tuple(s.material.name if s.material is not None else None for s in obj.material_slots),
            tuple((m.type, m.show_viewport) for m in obj.modifiers))


def _descendant_names(o, count=None):
    """Names of everything parented under o. Object.children_recursive scans
    every object in the file per call; during playback dozens of animated
    objects report a transform change each frame, so a parent map built once
    per object count is used instead. count: len(bpy.data.objects), taken once
    per update by the caller (asking for it per object cost ~1.4 s a Track / Mode
    switch on italy)."""
    key = len(bpy.data.objects) if count is None else count
    if _kids['key'] != key:
        m = {}
        for x in bpy.data.objects:
            if x.parent is not None:
                m.setdefault(x.parent.name, []).append(x.name)
        _kids.update(key=key, map=m)
    m = _kids['map']
    out, stack = [], list(m.get(o.name, ()))
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(m.get(n, ()))
    return out


def _deforms(o):
    """An object whose mesh an armature or shape keys bend (same topology every frame)."""
    if o.type != 'MESH' or o.data is None:
        return False
    if o.data.shape_keys is not None:
        return True
    return any(m.type == 'ARMATURE' for m in o.modifiers) and \
        all(m.type in ('ARMATURE', 'TRIANGULATE') for m in o.modifiers)


def _collapsed(mw):
    """A matrix scaled to nothing (objplace parks unspawned copies that way)."""
    return max(abs(x) for x in mw.to_scale()) < 1e-9


def _with_far(proj, near, far):
    """A GL projection with its near / far planes replaced."""
    p = proj.copy()
    if abs(p[3][3]) < 1e-9:      # perspective
        p[2][2] = -(far + near) / (far - near)
        p[2][3] = -2.0 * far * near / (far - near)
    else:                        # orthographic
        p[2][2] = -2.0 / (far - near)
        p[2][3] = -(far + near) / (far - near)
    return p


class C2_RE_game(bpy.types.RenderEngine):
    """Cars 2 Game: the game's own shaders (Arcade worlds)."""
    bl_idname = ENGINE_ID
    bl_label = 'Cars 2 Game'
    bl_use_preview = False
    bl_use_gpu_context = True
    bl_use_eevee_viewport = True       # Material Preview stays Eevee

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._synced = False

    def view_update(self, context, depsgraph):
        r = RENDERER
        if not self._synced:
            fxnodes.sync_engine(depsgraph.scene.original if hasattr(depsgraph.scene, 'original') else context.scene)
            self._synced = True
        # An object shown / hidden reports geometry (and mesh data) updates too: re-reading
        # every such mesh was most of a Track / Mode / Lap switch (~1500 meshes on italy).
        # Outside Edit Mode, an undeformed mesh is only checked (an exact fingerprint, ~0.1 ms)
        # and read again when it really changed (~1 ms).
        objs = None
        nobj = len(bpy.data.objects)
        for u in depsgraph.updates:
            idb = u.id
            if isinstance(idb, bpy.types.Object):
                o = idb.original
                if u.is_updated_geometry:
                    _changed(r, o)
                if u.is_updated_transform:
                    r.mark(o.name)
                    for ch in _descendant_names(o, nobj):     # children move with it
                        r.mark(ch)
            elif isinstance(idb, bpy.types.Mesh):
                # the objects drawn with this mesh, from the renderer's own record
                # (looking every drawn object up by name took ~0.7 s a frame on
                # tokyo_a while an armature-bent preview mesh played; one name map per update)
                users = r.mesh_users.get(idb.original.name, ())
                if users and objs is None:
                    objs = {x.name: x for x in bpy.data.objects}
                for name in list(users):
                    o = objs.get(name)
                    if o is None or o.data is None or o.data.name != idb.original.name:
                        r.mesh_users.get(idb.original.name, set()).discard(name)
                        continue
                    _changed(r, o)
            elif isinstance(idb, bpy.types.Material):
                touch_material(idb.original)
            elif isinstance(idb, bpy.types.Collection):
                r.rebuild(soft=True)        # objects shown / hidden / added (unchanged ones keep their draws)
            elif isinstance(idb, bpy.types.Scene):
                r.version += 1              # frame change, settings: redraw

    def view_draw(self, context, depsgraph):
        frame = _view_frame(context)
        space = context.space_data
        vl = context.view_layer

        def visible(o):
            return o.visible_get(view_layer=vl, viewport=space)
        # Blender colour-manages an engine's viewport output like a render
        # (scene-linear in, view transform applied), so both hand over linear
        try:
            RENDERER.draw(depsgraph, frame, out_linear=True, visible=visible)
        except ReferenceError as e:
            # a data-block this renderer still held was freed (undo / redo, a
            # removed material): start over rather than fail on every frame
            print('Cars 2 Game render: rebuilding after a removed data-block (%s)' % e)
            fxnodes.reset_after_undo()
            self.tag_redraw()

    def render(self, depsgraph):
        scene = depsgraph.scene
        rs = scene.render
        w = max(1, int(rs.resolution_x * rs.resolution_percentage / 100))
        h = max(1, int(rs.resolution_y * rs.resolution_percentage / 100))
        cam = scene.camera
        if cam is None:
            self.report({'ERROR'}, 'No camera')
            return
        cam_eval = cam.evaluated_get(depsgraph)
        proj = cam_eval.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=rs.pixel_aspect_x, scale_y=rs.pixel_aspect_y)
        far2 = max(cam.data.clip_end, RENDERER.engine_value('SceneFarPlane'))
        frame = Frame(cam_eval.matrix_world.inverted(), _with_far(proj, cam.data.clip_start, far2), w, h,
                      cam.data.clip_start, far2)
        fxnodes.sync_engine(scene.original if hasattr(scene, 'original') else scene)
        out = gpu.types.GPUTexture((w, h), format='RGBA16F')
        dep = gpu.types.GPUTexture((w, h), format='DEPTH_COMPONENT32F')
        fb = gpu.types.GPUFrameBuffer(depth_slot=dep, color_slots=out)
        RENDERER.last = None
        RENDERER.motion_key = 'render'

        def visible(o):
            return not o.hide_render
        # No depsgraph updates reach a final render. The first frame of a render
        # job builds every object's commands (with render visibility); each later
        # frame only those that moved, were shown / hidden or are bent by an
        # armature / shape keys (a full rebuild every frame cost ~370 ms a frame
        # in a 5,000-object world). Keyed material values reach the renderer
        # through the frame-change handler (touch_material).
        job = _RENDER_JOB
        if job['fresh']:
            RENDERER.rebuild()
            job['fresh'] = False
            job['sig'] = {}
        sig = job['sig']
        for ob in depsgraph.objects:
            o = ob.original
            name = o.name
            key = (tuple(x for row in ob.matrix_world for x in row), o.hide_render)
            if sig.get(name) != key:
                if name in sig:
                    RENDERER.mark(name)
                sig[name] = key
            if _deforms(o):
                RENDERER.drop_mesh(name, deform_only=True)
        # the draw commands first, outside our framebuffer: preparing them
        # draws the characters' eye textures into framebuffers of their own,
        # which can't nest inside a Python-bound one
        RENDERER._commands(depsgraph, visible)
        with fb.bind():
            gpu.state.viewport_set(0, 0, w, h)
            # Film > Transparent: the render starts empty and only what's drawn covers it
            fb.clear(color=(0.0, 0.0, 0.0, 0.0 if scene.render.film_transparent else 1.0), depth=1.0)
            RENDERER.draw(depsgraph, frame, out_linear=True, visible=visible)
            buf = fb.read_color(0, 0, w, h, 4, 0, 'FLOAT')
        RENDERER.motion_key = 'view'
        RENDERER.last = None
        buf.dimensions = w * h * 4
        px = np.array(buf, np.float32).reshape(h * w, 4)
        result = self.begin_result(0, 0, w, h)
        combined = result.layers[0].passes['Combined']
        try:
            # in one call: assigning the array converted it value by value (~1.4 s
            # a frame at 1280 x 720, nearly all of an animation render's time)
            combined.rect.foreach_set(px.ravel())
        except (AttributeError, TypeError, RuntimeError):
            combined.rect = px
        self.end_result(result)


# Blender panels that should also show for this engine (render size, colour
# management, output ...)
_PANELS = ('RENDER_PT_format', 'RENDER_PT_frame_range', 'RENDER_PT_output', 'RENDER_PT_color_management',
           'RENDER_PT_encoding', 'RENDER_PT_stamp', 'RENDER_PT_post_processing', 'DATA_PT_lens',
           'DATA_PT_camera', 'DATA_PT_camera_display', 'RENDER_PT_dimensions', 'RENDER_PT_frame_remapping',
           'RENDER_PT_time_stretching', 'RENDER_PT_output_views', 'RENDER_PT_format_presets',
           'RENDER_PT_opengl_film')     # Film > Transparent


def _panels():
    for name in _PANELS:
        p = getattr(bpy.types, name, None)
        if p is not None and hasattr(p, 'COMPAT_ENGINES'):
            yield p


_RENDER_JOB = {'fresh': True, 'sig': {}}     # a final render / animation in progress (see render())


@bpy.app.handlers.persistent
def _render_init(*_a):
    """A render job starts: its first frame builds every command."""
    _RENDER_JOB['fresh'] = True
    _RENDER_JOB['sig'] = {}


@bpy.app.handlers.persistent
def _render_done(*_a):
    """The render job ended: the viewport draws with its own visibility again."""
    _RENDER_JOB['fresh'] = True
    _RENDER_JOB['sig'] = {}
    RENDERER.rebuild()


_RENDER_HANDLERS = (('render_init', _render_init), ('render_complete', _render_done),
                    ('render_cancel', _render_done))


def register():
    bpy.utils.register_class(C2_RE_game)
    for p in _panels():
        p.COMPAT_ENGINES.add(ENGINE_ID)
    for h, f in _RENDER_HANDLERS:
        lst = getattr(bpy.app.handlers, h)
        if f not in lst:
            lst.append(f)


def unregister():
    for h, f in _RENDER_HANDLERS:
        lst = getattr(bpy.app.handlers, h)
        if f in lst:
            lst.remove(f)
    for p in _panels():
        p.COMPAT_ENGINES.discard(ENGINE_ID)
    bpy.utils.unregister_class(C2_RE_game)
