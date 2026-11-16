"""The Arcade game's own shaders as Eevee nodes ("Game Look" in Material Preview).

Only built when Scene > Eevee Game Shaders is on: the Cars 2 Game render engine
(gamerender.py) runs the same shaders as GLSL without any of this. The engine
values (sync_engine / set_engine) serve both: they are also kept in ENGINE_VALUES.

Each material's effect (.tfx) is translated by core/fxgraph into a scalar
expression graph and emitted as Blender node groups:

    C2A|<effect>|0 .. |K   texture coordinates of each texture stage
    C2A|<effect>|K+1       final colour, alpha and alpha-test clip

shared by every material that uses the effect. A material only holds its
parameter values (Value nodes named "P:<name>.<element>.<component>"), its
textures (Image nodes "G_T<n>") and the blending setup, so editing a value or
swapping a texture shows immediately and needs no shader rebuild.

Engine inputs (sun, fog, light rig SH, lightmap settings, time, exposure)
come from the shared group C2A_Engine (see sync_engine). The colour goes
through C2A_Post: the game's Hejl filmic tone map with its HDR exposure, then
converted for Blender's "Standard" view transform. Run-time buffers (dynamic
shadows, ambient volumes, reflections planes, refraction) are constants.

Colour space, as sdaemon.exe does it: a texture whose DDS has the sRGB flag
(dds.is_srgb) is sampled with sRGB decoding, and a float3/float4 parameter
whose effect marks it "Gamma" is converted sRGB -> linear when a material
loads. Effect defaults are used as they are.
"""
import math
import os
import sys

import bpy

import arcade
import cubemap
import dds
import fxgraph

PREFIX = 'C2A|'
ENGINE = 'C2A_Engine'
POST = 'C2A_Post'
L2S = 'C2A_LinearToSRGB'
S2L = 'C2A_SRGBToLinear'
CUBEUV = 'C2A_CubeUV'
# scene_settings colours the engine linearises (Gamma in effects/shared.tfx)
SHARED_GAMMA = ('g_sunLightPrimary', 'g_sunLightTertiary', 'g_LightMapSpecularColor', 'g_ShadowBlobColor',
                'g_Bloom_Tint')
GAME_TAG = 'G_'          # name prefix of game-shader nodes inside a material

# CTAB sampler names of engine-bound textures -> texture usage in scene_settings
GLOBAL_SAMPLERS = {'samFogMap': 'fogMap', 'samAmbientMap': 'ambientMap', 'samFallbackMap': 'fallbackMap',
                   'samCloudTexture1': 'CloudTexture1', 'samColorGradeLut': 'colorGradeLut'}
INSTANCE_USAGES = ('lightMap1', 'envMapInstance')
# constant stand-ins for a missing texture, by usage prefix
# (an empty glow map glows nothing: white made a whole body glow as soon as a
# shader switch added the slot, before a texture was picked for it)
MISSING = {'normalMap': (0.5, 0.5, 1.0, 1.0), 'lightMap': (1.0, 1.0, 1.0, 43 / 255.0),
           'emissiveMap': (0.0, 0.0, 0.0, 0.0)}

_plans = {}


# ----------------------------------------------------------------------
# translation plans
# ----------------------------------------------------------------------

class Plan:
    """A translated effect, split into texture stages."""

    def __init__(self, effect, tfx, result, tr):
        g = tr.g
        self.effect = effect
        self.tfx = tfx
        self.tr = tr
        clip = g.k(0.0)
        for k in result['kill']:
            clip = g.bin('max', clip, g.bin('lt', k, g.k(0.0)))
        self.color = result['color']
        self.alpha = result['alpha']
        self.clip = clip
        roots = list(self.color) + [self.alpha, clip]
        self.samples = []          # [(sampler, stage, u, v, w)]
        seen = {}
        for e in fxgraph.nodes_of(roots):
            if e[0] == 'tex':
                key = (e[1], e[3], e[4], e[5])
                if key not in seen:
                    seen[key] = len(self.samples)
                    self.samples.append((e[1], e[2], e[3], e[4], e[5]))
        self.sample_index = seen
        self.last = max([s[1] for s in self.samples] or [-1]) + 1
        self.sampler_types = tr.sampler_types
        self.sampler_usage = dict(tr.samplers)
        self.states = {k.split('#')[1]: v for k, v in tr.pass0.items() if k.startswith('RenderState')}
        self.defaults = {n: p.get('Default') for n, p in tr.params.items()}

    def group_name(self, stage):
        return '%s%s|%d|v%d' % (PREFIX, getattr(self, 'group_key', self.effect), stage, fxgraph.TRANSLATOR_VERSION)


def plan_for(assets, effect, local=None):
    """Plan for an effect, or None when it can't be translated (no file, no
    Standard technique, unsupported instruction). local: {effect name (lower):
    .tfx bytes} shipped inside a choreography / character package, used when
    the game's effects folder doesn't have it."""
    tfx = arcade.read_effect(assets, effect) if assets else None
    tag = ''
    if tfx is None and local and effect.lower() in local:
        import c2oct
        try:
            tfx = c2oct.decode(local[effect.lower()])[0]
            tag = '@pkg'
        except ValueError:
            tfx = None
    key = (assets, effect.lower() + tag)
    if key in _plans:
        return _plans[key]
    plan = None
    if tfx is not None:
        try:
            tr = fxgraph.Translator(tfx)
            p0 = tr.pass0
            res = tr.translate(tfx['VertexShaderPool']['VertexShader#%d' % p0['VertexShaderReference']]['Data'],
                               tfx['PixelShaderPool']['PixelShader#%d' % p0['PixelShaderReference']]['Data'])
            plan = Plan(effect, tfx, res, tr)
            plan.group_key = effect + tag   # package copies get their own node groups
        except (fxgraph.Unsupported, KeyError) as e:
            print('Cars 2 Scene: effect %s not translated (%s)' % (effect, e))
    _plans[key] = plan
    return plan


# ----------------------------------------------------------------------
# small shared groups
# ----------------------------------------------------------------------

def _new_group(name, inputs, outputs):
    """A new, empty shader node group with the given input and output sockets
    (Group Input / Group Output nodes already placed).
    """
    g = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    for n, t in inputs:
        g.interface.new_socket(n, in_out='INPUT', socket_type=t)
    for n, t in outputs:
        g.interface.new_socket(n, in_out='OUTPUT', socket_type=t)
    gi = g.nodes.new('NodeGroupInput')
    go = g.nodes.new('NodeGroupOutput')
    gi.location = (-600, 0)
    go.location = (600, 0)
    return g, gi, go


def _math(nt, op, a=None, b=None, c=None, clamp=False, loc=(0, 0)):
    """Add a Math node with `op`; each operand may be a number (typed into the
    input) or a socket (linked). Returns the node's output socket.
    """
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    n.use_clamp = clamp
    n.location = loc
    for i, v in enumerate((a, b, c)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            n.inputs[i].default_value = float(v)
        else:
            nt.links.new(v, n.inputs[i])
    return n.outputs[0]


def _l2s_channel(nt, x, loc):
    """exact linear -> sRGB of one channel"""
    lo = _math(nt, 'MULTIPLY', x, 12.92, loc=(loc[0], loc[1]))
    p = _math(nt, 'POWER', _math(nt, 'MAXIMUM', x, 0.0, loc=(loc[0], loc[1] - 40)), 1 / 2.4, loc=(loc[0] + 150, loc[1] - 40))
    hi = _math(nt, 'MULTIPLY_ADD', p, 1.055, -0.055, loc=(loc[0] + 300, loc[1] - 40))
    sel = _math(nt, 'LESS_THAN', x, 0.0031308, loc=(loc[0] + 150, loc[1] + 40))
    d = _math(nt, 'SUBTRACT', lo, hi, loc=(loc[0] + 300, loc[1] + 40))
    return _math(nt, 'MULTIPLY_ADD', sel, d, hi, loc=(loc[0] + 450, loc[1]))


def _s2l_channel(nt, x, loc):
    """exact sRGB -> linear of one channel"""
    lo = _math(nt, 'DIVIDE', x, 12.92, loc=loc)
    p = _math(nt, 'POWER', _math(nt, 'DIVIDE', _math(nt, 'ADD', x, 0.055, loc=(loc[0], loc[1] - 40)), 1.055,
                                 loc=(loc[0] + 150, loc[1] - 40)), 2.4, loc=(loc[0] + 300, loc[1] - 40))
    sel = _math(nt, 'LESS_THAN', x, 0.04045, loc=(loc[0] + 150, loc[1] + 40))
    d = _math(nt, 'SUBTRACT', lo, p, loc=(loc[0] + 300, loc[1] + 40))
    return _math(nt, 'MULTIPLY_ADD', sel, d, p, loc=(loc[0] + 450, loc[1]))


def _channel_group(name, channel):
    """A colour group that applies the same per-channel float function to R, G
    and B (alpha passes through). Used for the sRGB <-> linear converters.
    """
    g = bpy.data.node_groups.get(name)
    if g is not None:
        return g
    g, gi, go = _new_group(name, [('Color', 'NodeSocketColor')], [('Color', 'NodeSocketColor')])
    sep = g.nodes.new('ShaderNodeSeparateColor')
    g.links.new(gi.outputs[0], sep.inputs[0])
    comb = g.nodes.new('ShaderNodeCombineColor')
    comb.location = (450, 0)
    for k in range(3):
        g.links.new(channel(g, sep.outputs[k], (-200, 300 - 200 * k)), comb.inputs[k])
    g.links.new(comb.outputs[0], go.inputs[0])
    return g


def linear_to_srgb_group():
    """Shared group: linear colour -> sRGB-encoded colour."""
    return _channel_group(L2S, _l2s_channel)


def srgb_to_linear_group():
    """Shared group: sRGB-encoded colour -> linear colour."""
    return _channel_group(S2L, _s2l_channel)


def srgb_to_linear(x):
    """Exact sRGB -> linear of one value (the engine's conversion of Gamma parameters)."""
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def cube_uv_group():
    """direction -> equirect UV (see core/cubemap.py)"""
    g = bpy.data.node_groups.get(CUBEUV)
    if g is not None:
        return g
    g, gi, go = _new_group(CUBEUV, [('Direction', 'NodeSocketVector')], [('UV', 'NodeSocketVector')])
    sep = g.nodes.new('ShaderNodeSeparateXYZ')
    g.links.new(gi.outputs[0], sep.inputs[0])
    x, y, z = sep.outputs[0], sep.outputs[1], sep.outputs[2]
    phi = _math(g, 'ARCTAN2', x, _math(g, 'MULTIPLY', z, -1.0))
    hxz = _math(g, 'SQRT', _math(g, 'MULTIPLY_ADD', x, x, _math(g, 'MULTIPLY', z, z)))
    th = _math(g, 'ARCTAN2', y, hxz)
    u = _math(g, 'MULTIPLY_ADD', phi, 1 / (2 * math.pi), 0.5)
    v = _math(g, 'MULTIPLY_ADD', th, 1 / math.pi, 0.5)   # Blender image space: from the bottom
    comb = g.nodes.new('ShaderNodeCombineXYZ')
    comb.location = (400, 0)
    g.links.new(u, comb.inputs[0])
    g.links.new(v, comb.inputs[1])
    g.links.new(comb.outputs[0], go.inputs[0])
    return g


# ----------------------------------------------------------------------
# engine globals
# ----------------------------------------------------------------------

ENGINE_DEFAULTS = {
    'g_FinalAlpha': 1.0, 'g_AlphaModifier': 1.0, 'g_EnvMapExposure': 1.0, 'g_ShadowBlobColor': (0.5, 0.5, 0.5),
    'g_sunLightIntensity': 1.0, 'g_sunLightPrimary': (1.0, 1.0, 1.0), 'g_sunLightDirection': (0.0, 1.0, 0.0),
    'g_LightMapNormalDirection': (0.0, 1.0, 0.0), 'g_LightMapSpecularDir': (0.0, 1.0, 0.0),
    'g_LightmapNormalAmount': 0.66, 'g_LightmapStartThresh': 0.1, 'g_LightmapEndThresh': 0.75,
    'g_FogStart': 1.0e6, 'g_FogEnd': 2.0e6, 'g_HDR_Exposure': 0.18, 'avgLum': 0.18, 'g_ShadowZScale': 1.0,
}


# Engine values are Value nodes in one group (C2A_Engine) that every game
# material uses. Changing a value is cheap (a shader uniform). Changing the
# group's sockets regenerates every shader in the world, so all the values
# any effect reads are created with the group; and no drivers: an animated
# value (g_Time) made every frame regenerate every shader. g_Time follows the
# timeline through a frame handler instead.
ENGINE_KEYS = (
    [('g_EnvMapExposure', 0, 0), ('g_FinalAlpha', 0, 0), ('g_FogEnd', 0, 0), ('g_FogStart', 0, 0),
     ('g_LightmapEndThresh', 0, 0), ('g_LightmapNormalAmount', 0, 0), ('g_LightmapStartThresh', 0, 0),
     ('g_ShadowZScale', 0, 1), ('g_Time', 0, 0), ('g_primaryTertiaryBlend', 0, 0), ('g_sunLightIntensity', 0, 0),
     ('g_sunLightSoftness', 0, 0), ('g_HDR_Exposure', 0, 0), ('avgLum', 0, 0), ('c2Raw', 0, 0), ('c2Inspect', 0, 0)] +
    [(n, 0, c) for n in ('g_LightMapNormalDirection', 'g_LightMapSpecularColor', 'g_LightMapSpecularDir',
                         'g_ShadowBlobColor', 'g_sunLightDirection', 'g_sunLightPrimary', 'g_sunLightTertiary')
     for c in range(3)] +
    [('objectSH', e, c) for e in range(7) for c in range(4) if (e, c) != (6, 3)])


ENGINE_IMAGE = 'C2A_EngineValues'
ENGINE_W = 64                      # pixels, 4 values each


def _engine_default(name, comp):
    """Default value of one engine global component (used before the scene sets it)."""
    d = ENGINE_DEFAULTS.get(name, 0.0)
    if isinstance(d, tuple):
        return float(d[comp]) if comp < len(d) else 0.0
    return float(d)


def _engine_keys(g):
    """Output socket names of the C2A_Engine group, one per engine-global
    component ('name[element].c').
    """
    return [s.name for s in g.interface.items_tree if s.item_type == 'SOCKET' and s.in_out == 'OUTPUT']


def engine_image():
    """The values themselves: a float image every game shader samples. Writing
    pixels re-uploads a texture; it never regenerates a shader (a value inside
    a node tree would)."""
    img = bpy.data.images.get(ENGINE_IMAGE)
    if img is None:
        img = bpy.data.images.new(ENGINE_IMAGE, ENGINE_W, 1, alpha=True, float_buffer=True)
        img.use_fake_user = True
        img.colorspace_settings.name = 'Non-Color'
        img.alpha_mode = 'CHANNEL_PACKED'
        try:
            img.use_half_precision = False
        except AttributeError:
            pass
        g = bpy.data.node_groups.get(ENGINE)
        if g is not None and g.get('c2_values') is not None:
            _upload(img, list(g['c2_values']))
    return img


def _upload(img, values):
    """New pixel values to the GPU. Packing keeps the image from counting as an
    unsaved change (Blender would ask to save it on quit) and stores the values
    in the .blend; it takes well under a millisecond."""
    img.pixels.foreach_set(values)
    img.update()
    img.pack()


def _engine_slot_nodes(g, slot):
    """Output of the image fetch for value slot `slot` inside the engine group."""
    px, ch = divmod(slot, 4)
    name = 'px%d' % px
    tn = g.nodes.get(name)
    if tn is None:
        tn = g.nodes.new('ShaderNodeTexImage')
        tn.name = name
        tn.image = engine_image()
        tn.interpolation = 'Closest'
        tn.extension = 'EXTEND'
        tn.location = (-500, -260 * px)
        uv = g.nodes.new('ShaderNodeCombineXYZ')
        uv.name = 'uv%d' % px
        uv.location = (-700, -260 * px)
        uv.inputs[0].default_value = (px + 0.5) / ENGINE_W
        uv.inputs[1].default_value = 0.5
        g.links.new(uv.outputs[0], tn.inputs['Vector'])
        sep = g.nodes.new('ShaderNodeSeparateColor')
        sep.name = 'sep%d' % px
        sep.location = (-300, -260 * px)
        g.links.new(tn.outputs['Color'], sep.inputs[0])
    return tn.outputs['Alpha'] if ch == 3 else g.nodes['sep%d' % px].outputs[ch]


def _fill_engine_group(g, values):
    """Build the inside of the C2A_Engine group: every output reads one pixel
    of the C2A_EngineValues float image, so changing a value only rewrites
    the image instead of regenerating every material's shader.
    """
    out = g.nodes.new('NodeGroupOutput')
    out.name = 'Output'
    out.location = (400, 0)
    for slot, key in enumerate(_engine_keys(g)):
        g.links.new(_engine_slot_nodes(g, slot), out.inputs[key])
    g['c2_values'] = values
    img = engine_image()
    _upload(img, values)


def engine_group():
    """The shared C2A_Engine node group (created / upgraded when missing).
    Every game-shader material reads engine globals (sun direction, fog, time,
    exposure...) from it; the values live in the C2A_EngineValues float image.
    """
    g = bpy.data.node_groups.get(ENGINE)
    if g is not None:
        return g
    g = bpy.data.node_groups.new(ENGINE, 'ShaderNodeTree')
    g.use_fake_user = True
    values = [0.0] * (ENGINE_W * 4)
    for slot, (name, el, c) in enumerate(ENGINE_KEYS):
        g.interface.new_socket('%s.%d.%d' % (name, el, c), in_out='OUTPUT', socket_type='NodeSocketFloat')
        values[slot] = _engine_default(name, c)
    _fill_engine_group(g, values)
    return g


def upgrade_engine_group():
    """Files from before 0.3 keep the values in Value nodes (and g_Time in a
    driver): move them into the image. The group's sockets stay as they are,
    so the materials keep working; their shaders are generated once."""
    g = bpy.data.node_groups.get(ENGINE)
    if g is None or g.get('c2_values') is not None:
        return
    keys = _engine_keys(g)
    values = [0.0] * (ENGINE_W * 4)
    for slot, key in enumerate(keys):
        n = g.nodes.get(key)
        if n is not None and slot < len(values):
            values[slot] = n.outputs[0].default_value
    if g.animation_data is not None:
        g.animation_data_clear()
    g.nodes.clear()
    _fill_engine_group(g, values)
    for k in ENGINE_KEYS:      # add the missing ones now, before the first compile
        engine_socket(*k)


def engine_socket(name, element, comp):
    """Output socket name of an engine value (created on first use; a new
    socket regenerates every game shader, so ENGINE_KEYS has them all)."""
    g = engine_group()
    key = '%s.%d.%d' % (name, element, comp)
    keys = _engine_keys(g)
    if key not in keys:
        slot = len(keys)
        if slot >= ENGINE_W * 4:
            raise ValueError('too many engine values')
        g.interface.new_socket(key, in_out='OUTPUT', socket_type='NodeSocketFloat')
        g.links.new(_engine_slot_nodes(g, slot), g.nodes['Output'].inputs[key])
        values = list(g['c2_values'])
        values[slot] = _engine_default(name, comp)
        g['c2_values'] = values
        img = engine_image()
        _upload(img, values)
    return key


def engine_value(nt, name, element, comp, loc=(-2600, 600)):
    """Output socket of an engine value inside node tree nt (one engine group node per tree)."""
    key = engine_socket(name, element, comp)
    n = nt.nodes.get(GAME_TAG + 'Engine')
    if n is None:
        n = nt.nodes.new('ShaderNodeGroup')
        n.name = GAME_TAG + 'Engine'
        n.location = loc
        n.node_tree = engine_group()
    elif key not in n.outputs:
        n.node_tree = engine_group()   # the group gained a socket after this node was made
    return n.outputs[key]


def outdated_groups():
    """True when materials still use effect groups of an older translator."""
    tag = '|v%d' % fxgraph.TRANSLATOR_VERSION
    return any(g.name.startswith(PREFIX) and not g.name.endswith(tag) and g.users
               for g in bpy.data.node_groups)


def engine_active(scene=None):
    """True when engine values matter: game-shader materials use the engine
    group, or the scene renders with the Cars 2 Game engine."""
    if bpy.data.node_groups.get(ENGINE) is not None:
        return True
    sc = scene if scene is not None else getattr(bpy.context, 'scene', None)
    return sc is not None and sc.render.engine == 'C2_GAME'


_slots = {}     # (group pointer, key) -> slot
# every engine value set, {(name, element): (x, y, z, w)}: the Cars 2 Game render
# engine (gamerender) reads them from here
ENGINE_VALUES = {}
ENGINE_VERSION = [0]  # bumped when a value changes


def set_engine(name, values, element=0, scene=None):
    """Set an engine value (scalar or vector) if any shader uses it."""
    vals = values if isinstance(values, (list, tuple)) else (values,)
    rec = tuple(float(x) for x in vals)
    if ENGINE_VALUES.get((name, element)) != rec:
        ENGINE_VALUES[(name, element)] = rec
        ENGINE_VERSION[0] += 1
        if bpy.context.scene is not None and bpy.context.scene.render.engine == 'C2_GAME':
            _redraw()
    g = bpy.data.node_groups.get(ENGINE)
    if g is None or g.get('c2_values') is None:
        return False
    store = g['c2_values']
    changed = False
    for c, x in enumerate(vals):
        ck = (g.as_pointer(), '%s.%d.%d' % (name, element, c))
        if ck not in _slots:
            _slots.clear()
            _slots.update({(g.as_pointer(), k): i for i, k in enumerate(_engine_keys(g))})
        slot = _slots.get(ck)
        if slot is not None and abs(store[slot] - float(x)) > 1e-7:
            store[slot] = float(x)
            changed = True
    if not changed:
        return False
    img = engine_image()
    _upload(img, list(g['c2_values']))
    _redraw()
    return True


def _redraw():
    """Redraw every 3D viewport."""
    for w in bpy.context.window_manager.windows if bpy.context.window_manager else ():
        for a in w.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()


_set_engine = set_engine


def post_group():
    """HDR colour -> Blender emission: x = c * exposure / avgLum - 0.004,
    Hejl filmic, then sRGB -> linear for the Standard view transform."""
    g = bpy.data.node_groups.get(POST)
    if g is not None:
        return g
    g, gi, go = _new_group(POST, [('R', 'NodeSocketFloat'), ('G', 'NodeSocketFloat'), ('B', 'NodeSocketFloat')],
                           [('Color', 'NodeSocketColor')])
    exp_ = engine_value(g, 'g_HDR_Exposure', 0, 0, (-600, -400))
    avg = engine_value(g, 'avgLum', 0, 0, (-600, -550))
    scale = _math(g, 'DIVIDE', exp_, avg, loc=(-400, -400))
    comb = g.nodes.new('ShaderNodeCombineColor')
    comb.location = (500, 0)
    for k in range(3):
        y0 = 400 - 300 * k
        x = _math(g, 'MAXIMUM', _math(g, 'MULTIPLY_ADD', gi.outputs[k], scale, -0.004, loc=(-300, y0)), 0.0, loc=(-150, y0))
        num = _math(g, 'MULTIPLY', x, _math(g, 'MULTIPLY_ADD', x, 6.2, 0.5, loc=(0, y0 + 40)), loc=(150, y0 + 40))
        den = _math(g, 'MULTIPLY_ADD', x, _math(g, 'MULTIPLY_ADD', x, 6.2, 1.7, loc=(0, y0 - 40)), 0.06, loc=(150, y0 - 40))
        disp = _math(g, 'DIVIDE', num, den, clamp=True, loc=(300, y0))
        g.links.new(_s2l_channel(g, disp, (350, y0 - 120)), comb.inputs[k])
    # c2Raw = 1 outputs the untouched HDR colour (Auto Exposure measures it)
    raw = g.nodes.new('ShaderNodeCombineColor')
    raw.location = (500, -300)
    for k in range(3):
        g.links.new(gi.outputs[k], raw.inputs[k])
    mix = g.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.location = (700, 0)
    g.links.new(engine_value(g, 'c2Raw', 0, 0, (500, -500)), mix.inputs['Factor'])
    g.links.new(comb.outputs[0], mix.inputs[6])
    g.links.new(raw.outputs[0], mix.inputs[7])
    g.links.new(mix.outputs[2], go.inputs[0])
    return g


def measure_exposure(context, size=160, rv3d=None):
    """The game's auto-exposure target for a view: log-average luminance of
    the raw HDR image (avglum.tfx), before the world's HDR min/max clamp.
    Renders rv3d's view (a 3D viewport's region_data), else the scene camera."""
    import tempfile
    import numpy as np
    scene = context.scene
    r = scene.render
    cam = scene.camera
    tmp_cam = None
    if rv3d is not None and rv3d.view_perspective != 'CAMERA':
        tmp_cam = bpy.data.objects.new('C2ExposureCam', bpy.data.cameras.new('C2ExposureCam'))
        scene.collection.objects.link(tmp_cam)
        tmp_cam.matrix_world = rv3d.view_matrix.inverted()
        space = context.space_data if getattr(context, 'space_data', None) and context.space_data.type == 'VIEW_3D' else None
        tmp_cam.data.lens = space.lens if space else 50.0
        tmp_cam.data.clip_end = space.clip_end if space else 10000.0
        scene.camera = tmp_cam
    if scene.camera is None:
        return None
    prev = (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
            r.image_settings.file_format, r.image_settings.color_depth)
    path = os.path.join(tempfile.mkdtemp(), 'exposure.exr')
    set_engine('c2Raw', 1.0)
    try:
        r.engine = 'BLENDER_EEVEE_NEXT'
        r.resolution_x, r.resolution_y = size, max(1, size * 9 // 16)
        r.resolution_percentage = 100
        r.image_settings.file_format = 'OPEN_EXR'
        r.image_settings.color_depth = '32'
        r.filepath = path
        scene.frame_set(scene.frame_current)  # re-evaluate (a camera moved since the last render)
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(path)
        px = np.empty(img.size[0] * img.size[1] * 4, np.float32)
        img.pixels.foreach_get(px)
        bpy.data.images.remove(img)
    finally:
        set_engine('c2Raw', 0.0)
        (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
         r.image_settings.file_format, r.image_settings.color_depth) = prev
        scene.camera = cam
        if tmp_cam is not None:
            data = tmp_cam.data
            bpy.data.objects.remove(tmp_cam)
            bpy.data.cameras.remove(data)
    rgb = px.reshape(-1, 4)[:, :3].astype(np.float64)
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    return float(np.exp(np.mean(np.log(np.maximum(lum, 1e-4)))))


# ----------------------------------------------------------------------
# effect groups
# ----------------------------------------------------------------------

def _is_leaf(e):
    """Is this expression a leaf (constant, input, parameter, texture sample)
    rather than an operation?
    """
    return e[0] in ('k', 'in', 'par', 'glob', 'tex')


class _Emitter:
    """Turns fxgraph expression tuples into shader nodes.

    Sub-expressions used more than once are emitted once and their socket is
    shared; mul followed by add is fused into a single MULTIPLY_ADD node;
    leaves (mesh inputs, parameters, engine values, texture samples) become
    input nodes. Nodes are laid out left to right by depth.
    """
    def __init__(self, nt, gi, plan):
        """nt: the node tree to fill; the other arguments supply mesh inputs,
        parameter sockets and texture-sample sockets for leaves.
        """
        self.nt = nt
        self.gi = gi
        self.plan = plan
        self.sock = {}
        self.inputs = {}
        self.eng = None
        self.uses = {}
        self.y = 0

    def count_uses(self, roots):
        """Count how often each sub-expression is referenced (shared ones are
        emitted once).
        """
        stack = list(roots)
        seen = set()
        while stack:
            e = stack.pop()
            if id(e) in seen or _is_leaf(e):
                continue
            seen.add(id(e))
            for a in e[1:]:
                if isinstance(a, tuple):
                    self.uses[id(a)] = self.uses.get(id(a), 0) + 1
                    stack.append(a)

    def _pos(self, depth):
        """Node location for an expression at a given depth."""
        self.y -= 40
        return (depth * 160 - 2000, self.y % 6000 - 3000)

    # -- leaves ------------------------------------------------------------------
    def engine(self, name, el, c):
        """Socket of one engine-global component (from the C2A_Engine group)."""
        return engine_value(self.nt, name, el, c, (-2800, 600 - 40 * len(self.nt.nodes) % 4000))

    def mesh_input(self, name):
        """Socket of a vertex-shader input: Position/Normal/camera position from
        Geometry nodes rotated into the game's Y-up space, tangents/binormals,
        UV maps (v flipped back to the game's convention) and vertex colours
        (converted linear -> sRGB, as the game stores them).
        """
        if name in self.inputs:
            return self.inputs[name]
        nt = self.nt
        loc = (-2600, -200 - 220 * len(self.inputs))

        def to_game(vec, point):
            """Rotate a Blender (Z-up) vector socket into game (Y-up) space."""
            m = nt.nodes.new('ShaderNodeMapping')
            m.vector_type = 'POINT' if point else 'VECTOR'
            m.inputs['Rotation'].default_value = (-math.pi / 2, 0.0, 0.0)
            m.location = (loc[0] + 200, loc[1])
            nt.links.new(vec, m.inputs['Vector'])
            return m.outputs[0]

        def sep(vec):
            """Separate XYZ of a vector socket (cached per socket)."""
            s = nt.nodes.new('ShaderNodeSeparateXYZ')
            s.location = (loc[0] + 400, loc[1])
            nt.links.new(vec, s.inputs[0])
            return [s.outputs[0], s.outputs[1], s.outputs[2], 1.0]

        if name in ('Position', 'Normal', '#campos'):
            geo = nt.nodes.new('ShaderNodeNewGeometry')
            geo.location = loc
            if name == 'Position':
                out = sep(to_game(geo.outputs['Position'], True))
            elif name == 'Normal':
                out = sep(to_game(geo.outputs['Normal'], False))
                out[3] = 0.0
            else:
                cam = nt.nodes.new('ShaderNodeCameraData')
                cam.location = (loc[0], loc[1] - 120)
                vm = nt.nodes.new('ShaderNodeVectorMath')
                vm.operation = 'MULTIPLY_ADD'
                vm.location = (loc[0] + 100, loc[1])
                nt.links.new(geo.outputs['Incoming'], vm.inputs[0])
                nt.links.new(cam.outputs['View Distance'], vm.inputs[1])
                nt.links.new(geo.outputs['Position'], vm.inputs[2])
                out = sep(to_game(vm.outputs[0], True))
        elif name.startswith(('Tangent', 'Binormal')):
            suffix = name.replace('Tangent', '').replace('Binormal', '')
            uv = {'2': 'Uv2', 'Mod': 'UvMod'}.get(suffix, 'Uv1')
            if name.startswith('Tangent'):
                t = nt.nodes.new('ShaderNodeTangent')
                t.direction_type = 'UV_MAP'
                t.uv_map = uv
                t.location = loc
                vec = t.outputs[0]
            else:
                # the game's Binormal is -(Blender bitangent); a Normal Map node fed
                # tangent-space (0, 1, 0) returns the bitangent with the right sign
                nm = nt.nodes.new('ShaderNodeNormalMap')
                nm.space = 'TANGENT'
                nm.uv_map = uv
                nm.inputs['Color'].default_value = (0.5, 1.0, 0.5, 1.0)
                nm.location = loc
                vm = nt.nodes.new('ShaderNodeVectorMath')
                vm.operation = 'SCALE'
                vm.inputs['Scale'].default_value = -1.0
                vm.location = (loc[0] + 100, loc[1])
                nt.links.new(nm.outputs[0], vm.inputs[0])
                vec = vm.outputs[0]
            out = sep(to_game(vec, False))
            out[3] = 0.0
        elif name.startswith('Uv') or name.endswith('UV'):
            uvn = nt.nodes.new('ShaderNodeUVMap')
            uvn.uv_map = name
            uvn.location = loc
            s = nt.nodes.new('ShaderNodeSeparateXYZ')
            s.location = (loc[0] + 200, loc[1])
            nt.links.new(uvn.outputs[0], s.inputs[0])
            v = _math(nt, 'SUBTRACT', 1.0, s.outputs[1], loc=(loc[0] + 400, loc[1]))  # Blender stores 1 - v
            out = [s.outputs[0], v, 0.0, 1.0]
        else:
            col = nt.nodes.new('ShaderNodeVertexColor')
            col.layer_name = name
            col.location = loc
            g = nt.nodes.new('ShaderNodeGroup')
            g.node_tree = linear_to_srgb_group()
            g.location = (loc[0] + 200, loc[1])
            nt.links.new(col.outputs['Color'], g.inputs[0])
            s = nt.nodes.new('ShaderNodeSeparateColor')
            s.location = (loc[0] + 400, loc[1])
            nt.links.new(g.outputs[0], s.inputs[0])
            out = [s.outputs[0], s.outputs[1], s.outputs[2], col.outputs['Alpha']]
        self.inputs[name] = out
        return out

    def leaf(self, e):
        """Socket for a leaf expression."""
        k = e[0]
        if k == 'k':
            return e[1]
        if k == 'par':
            return self.gi.outputs['P:%s.%d.%d' % (e[1], e[2], e[3])]
        if k == 'glob':
            if e[1] == 'campos':
                return self.mesh_input('#campos')[e[3]]
            return self.engine(e[1], e[2], e[3])
        if k == 'in':
            return self.mesh_input(e[1])[e[3]]
        if k == 'tex':
            i = self.plan.sample_index[(e[1], e[3], e[4], e[5])]
            if e[6] == 3:
                return self.gi.outputs['T%d.a' % i]
            if ('sepT', i) not in self.inputs:
                s = self.nt.nodes.new('ShaderNodeSeparateXYZ')
                s.location = (-2400, 1200 - 100 * i)
                self.nt.links.new(self.gi.outputs['T%d' % i], s.inputs[0])
                self.inputs[('sepT', i)] = s
            return self.inputs[('sepT', i)].outputs[e[6]]
        raise ValueError(k)

    # -- operations ----------------------------------------------------------------
    def val(self, e, depth=0, clamp=False):
        """Socket (or plain float) of any expression; the main recursive emitter."""
        if _is_leaf(e):
            return self.leaf(e)
        if id(e) in self.sock and not clamp:
            return self.sock[id(e)]
        op = e[0]
        single = lambda a: (not _is_leaf(a)) and self.uses.get(id(a), 0) == 1 and id(a) not in self.sock
        nt = self.nt
        if op == 'sat':
            a = e[1]
            if single(a) and a[0] not in ('sat',):
                r = self.val(a, depth, clamp=True)
            else:
                r = _math(nt, 'ADD', self.val(a, depth + 1), 0.0, clamp=True, loc=self._pos(depth))
        elif op == 'add':
            a, b = e[1], e[2]
            fused = None
            for m, o in ((a, b), (b, a)):
                if m[0] == 'mul' and single(m):
                    fused = (m, o)
                    break
            if fused:
                m, o = fused
                r = _math(nt, 'MULTIPLY_ADD', self.val(m[1], depth + 1), self.val(m[2], depth + 1),
                          self.val(o, depth + 1), clamp=clamp, loc=self._pos(depth))
            elif b[0] == 'neg' and not _is_leaf(b):
                r = _math(nt, 'SUBTRACT', self.val(a, depth + 1), self.val(b[1], depth + 1), clamp=clamp, loc=self._pos(depth))
            else:
                r = _math(nt, 'ADD', self.val(a, depth + 1), self.val(b, depth + 1), clamp=clamp, loc=self._pos(depth))
        elif op == 'sel':
            c, a, b = e[1], e[2], e[3]
            d = _math(nt, 'SUBTRACT', self.val(a, depth + 1), self.val(b, depth + 1), loc=self._pos(depth + 1))
            r = _math(nt, 'MULTIPLY_ADD', self.val(c, depth + 1), d, self.val(b, depth + 1), clamp=clamp, loc=self._pos(depth))
        else:
            args = [self.val(x, depth + 1) for x in e[1:]]
            if op == 'mul':
                r = _math(nt, 'MULTIPLY', args[0], args[1], clamp=clamp, loc=self._pos(depth))
            elif op == 'neg':
                r = _math(nt, 'MULTIPLY', args[0], -1.0, clamp=clamp, loc=self._pos(depth))
            elif op in ('min', 'max'):
                r = _math(nt, 'MINIMUM' if op == 'min' else 'MAXIMUM', args[0], args[1], clamp=clamp, loc=self._pos(depth))
            elif op == 'pow':
                r = _math(nt, 'POWER', args[0], args[1], clamp=clamp, loc=self._pos(depth))
            elif op == 'exp2':
                r = _math(nt, 'POWER', 2.0, args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'log2':
                r = _math(nt, 'LOGARITHM', args[0], 2.0, clamp=clamp, loc=self._pos(depth))
            elif op == 'rcp':
                r = _math(nt, 'DIVIDE', 1.0, args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'rsq':
                r = _math(nt, 'INVERSE_SQRT', args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'frc':
                r = _math(nt, 'FRACT', args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'abs':
                r = _math(nt, 'ABSOLUTE', args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'sin':
                r = _math(nt, 'SINE', args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'cos':
                r = _math(nt, 'COSINE', args[0], clamp=clamp, loc=self._pos(depth))
            elif op == 'lt':
                r = _math(nt, 'LESS_THAN', args[0], args[1], clamp=clamp, loc=self._pos(depth))
            else:
                raise ValueError('op %s' % op)
        if not clamp:
            self.sock[id(e)] = r
        return r


def _deps(roots):
    """params and texture samples a set of roots needs (stopping at texture reads)."""
    pars, texs = set(), set()
    stack = list(roots)
    seen = set()
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if e[0] == 'par':
            pars.add((e[1], e[2], e[3]))
        elif e[0] == 'tex':
            texs.add((e[1], e[3], e[4], e[5]))
        elif not _is_leaf(e):
            stack.extend(x for x in e[1:] if isinstance(x, tuple))
    return pars, texs


def _socket_value(sock):
    """A plain float when `s` is a constant, else the socket itself."""
    return isinstance(sock, (int, float))


def stage_group(plan, stage):
    """One node group per texture stage of an effect. Inputs are the values
    the stage needs (earlier samples, UVs, parameters); the final stage
    outputs R/G/B/Alpha/Clip, which the material wires to its output.
    """
    name = plan.group_name(stage)
    g = bpy.data.node_groups.get(name)
    if g is not None:
        return g
    final = stage == plan.last
    if final:
        roots = list(plan.color) + [plan.alpha, plan.clip]
    else:
        roots = [x for s in plan.samples if s[1] == stage for x in s[2:5]]
    pars, texs = _deps(roots)
    inputs = [('P:%s.%d.%d' % p, 'NodeSocketFloat') for p in sorted(pars)]
    for t in sorted(texs, key=lambda k: plan.sample_index[k]):
        i = plan.sample_index[t]
        inputs += [('T%d' % i, 'NodeSocketVector'), ('T%d.a' % i, 'NodeSocketFloat')]
    if final:
        outputs = [('R', 'NodeSocketFloat'), ('G', 'NodeSocketFloat'), ('B', 'NodeSocketFloat'),
                   ('Alpha', 'NodeSocketFloat'), ('Clip', 'NodeSocketFloat')]
    else:
        outputs = [('C%d' % plan.sample_index[(s[0], s[2], s[3], s[4])], 'NodeSocketVector')
                   for s in plan.samples if s[1] == stage]
    g, gi, go = _new_group(name, inputs, outputs)
    g.use_fake_user = False
    gi.location = (-3000, 0)
    go.location = (400, 0)
    em = _Emitter(g, gi, plan)
    em.count_uses(roots)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        if final:
            for k, e in enumerate(roots):
                v = em.val(e)
                if _socket_value(v):
                    go.inputs[k].default_value = v
                else:
                    g.links.new(v, go.inputs[k])
        else:
            for s in plan.samples:
                if s[1] != stage:
                    continue
                i = plan.sample_index[(s[0], s[2], s[3], s[4])]
                comb = g.nodes.new('ShaderNodeCombineXYZ')
                comb.location = (200, -200 * i)
                cube = plan.sampler_types.get(s[0]) == 'cube'
                u, v, w = em.val(s[2]), em.val(s[3]), em.val(s[4])
                if not cube:
                    v = 1.0 - v if _socket_value(v) else _math(g, 'SUBTRACT', 1.0, v, loc=(100, -200 * i))
                for k, x in enumerate((u, v, w) if cube else (u, v)):
                    if _socket_value(x):
                        comb.inputs[k].default_value = x
                    else:
                        g.links.new(x, comb.inputs[k])
                g.links.new(comb.outputs[0], go.inputs['C%d' % i])
    finally:
        sys.setrecursionlimit(old)
    return g


# ----------------------------------------------------------------------
# materials
# ----------------------------------------------------------------------

class Textures:
    """Where a material's textures come from: TexturePool index -> bpy image
    (2D) and DDS bytes (cube maps, converted to panoramas on demand)."""

    def __init__(self, images, tex_files, global_entries, cache_dir, parent=None, srgb=None):
        """Images of the world's TexturePool by index (decoded lazily)."""
        self.images = images                  # index -> bpy image (may be sRGB)
        self.tex_files = tex_files            # index -> .dds path
        self.global_entries = global_entries  # usage -> texture index (scene_settings)
        self.cache_dir = cache_dir
        self.parent = parent                  # the world's Textures, for a choreography's fog / env maps
        self.srgb = dict(srgb or {})          # index -> sampled as sRGB (else read from the DDS)
        self.cubes = {}
        self._names = {i: img.name for i, img in images.items() if img is not None}   # for revalidate()

    def revalidate(self):
        """Undo / redo re-create images: look this context's up again by name."""
        names = getattr(self, '_names', None)
        if names is None:
            return
        for i, name in names.items():
            img = bpy.data.images.get(name)
            if img is None:
                self.images.pop(i, None)
            else:
                self.images[i] = img
        self.cubes.clear()

    def is_srgb(self, index):
        """The engine decodes a texture from sRGB when its DDS says so (dds.is_srgb)."""
        if index not in self.srgb:
            path = self.tex_files.get(index)
            head = b''
            if path and path.lower().endswith('.dds') and os.path.exists(path):
                with open(path, 'rb') as f:
                    head = f.read(128)
            self.srgb[index] = dds.is_srgb(head)
        return self.srgb[index]

    def cube_image(self, index):
        """A cube-map DDS as an equirectangular image Eevee can sample (the PNG is
        cached on disk next to the other converted textures).
        """
        if index in self.cubes:
            return self.cubes[index]
        img = None
        path = self.tex_files.get(index)
        if path and os.path.exists(path):
            png = os.path.join(self.cache_dir, os.path.splitext(os.path.basename(path))[0] + '_equirect.png')
            try:
                if not os.path.exists(png):
                    os.makedirs(self.cache_dir, exist_ok=True)
                    with open(path, 'rb') as f:
                        faces = cubemap.cube_faces(f.read())
                    cubemap.write_png(png, cubemap.equirect(faces))
                img = _load_data_image(png)
                if img is None:
                    return None         # not now: loaded after this viewport draw, then redrawn
            except (ValueError, RuntimeError, OSError) as e:
                print('Cars 2 Scene: cube map %s: %s' % (path, e))
        self.cubes[index] = img
        return img


_DEFERRED = set()


def _load_data_image(path):
    """bpy image of a non-colour PNG, or None when Blender refuses to add or
    change data-blocks right now (a viewport draw: the Cars 2 Game engine reads
    cube maps while drawing). Then a timer loads it just after the draw and
    makes the game engine rebuild, so the cube map shows on the next frame."""
    from .matnodes import set_data_image
    try:
        img = bpy.data.images.load(path, check_existing=True)
        set_data_image(img)
        return img
    except AttributeError:          # "Writing to ID classes in this context is not allowed"
        if path not in _DEFERRED:
            _DEFERRED.add(path)
            bpy.app.timers.register(lambda: _deferred_load(path), first_interval=0.0)
        return None


def _deferred_load(path):
    from .matnodes import set_data_image
    _DEFERRED.discard(path)
    try:
        set_data_image(bpy.data.images.load(path, check_existing=True))
    except (RuntimeError, AttributeError) as e:
        print('Cars 2 Scene: cube map %s: %s' % (path, e))
        return None
    _game_touch(None)
    _redraw()
    return None


def texture_index(mat, plan, sampler):
    """TexturePool index a sampler reads for this material, or None."""
    base = mat.c2.variant_of or mat
    usage = plan.sampler_usage.get(sampler, GLOBAL_SAMPLERS.get(sampler))
    if usage is None:
        return None, None
    if usage == 'lightMap1' and mat.c2.lightmap >= 0:
        return usage, mat.c2.lightmap
    if usage == 'envMapInstance' and mat.c2.envmap_inst >= 0:
        return usage, mat.c2.envmap_inst
    for t in base.c2.textures:
        if t.usage == usage:
            return usage, t.texture
    return usage, None


def gamma_params(plan):
    """Colour parameters the engine converts sRGB -> linear when a material loads."""
    return {n for n, p in plan.tr.params.items() if p.get('Gamma') and (p.get('Cols') or 0) >= 3}


def loaded_value(prop, comp, gamma):
    """What the shader sees for component `comp` of a material value. An extra
    parameter left at the effect's default isn't written, so the game uses the
    default as it is; anything written goes through the engine's conversion."""
    v = float(prop.fvalue[comp]) if comp < prop.size else 0.0
    if gamma and comp < 3 and prop.size >= 3 and \
            not (prop.extra and all(abs(a - b) < 1e-6 for a, b in zip(prop.fvalue, prop.fdefault))):
        return srgb_to_linear(v)
    return v


# Preview-only values (never written to the material, never exported): the
# Cars 2 Toolkit's Emissions toggle shows a character's glow maps, which the
# game switches on at run time (85 of 86 character materials with an
# emissiveMapA1 store emissiveMultiplier 0). {material name: {parameter: value}}
PREVIEW_VALUES = {}


def param_value(mat, plan, name, element, comp):
    """Current value of one parameter component of a material: a preview
    value (PREVIEW_VALUES), the material's own entry when it has one, else the
    effect default.
    """
    base = mat.c2.variant_of or mat
    ov = PREVIEW_VALUES.get(base.name)
    if ov is not None and name in ov:
        v = ov[name]
        return float(v[comp]) if isinstance(v, (list, tuple)) else (float(v) if comp == 0 else 0.0)
    for p in base.c2.props:
        if p.name == name:
            if p.ptype == 'Float':
                return loaded_value(p, comp, name in gamma_params(plan))
            return float(p.ivalue)
    d = plan.defaults.get(name)
    if isinstance(d, (list, tuple)):
        return float(d[comp]) if comp < len(d) else 0.0
    if d is None:
        return 1.0 if name in ('g_AlphaModifier',) else 0.0
    return float(d)


def _state(plan, mat, key, default=None):
    """Render state (blend mode, cutout, culling...) of a material from its
    effect and parameters.
    """
    v = plan.states.get(key, default)
    if isinstance(v, str) and v.startswith('<') and v.endswith('>'):
        return param_value(mat, plan, v[1:-1], 0, 0)
    return v


def texture_animation(mat):
    """{usage: {'textures': [...], 'rate': fps}} of a material's flipbook slots (arcade.texture_animations)."""
    base = mat.c2.variant_of or mat
    a = base.get('c2_texanim')
    return {u: {'textures': list(v['textures']), 'rate': float(v['rate'])} for u, v in a.items()} if a else {}


def _flipbook(nt, anim, textures, first, vector, col0, alpha0, pos, i):
    """The game's TextureAnimation: textures[floor(time * rate) % count], with
    time = g_Time (follows the timeline). `first` is the frame-0 image node."""
    texs = anim['textures']
    n = len(texs)
    x, y = pos[0] + 500, pos[1]
    t = engine_value(nt, 'g_Time', 0, 0, (x - 400, y + 200))
    scaled = _math(nt, 'MULTIPLY', t, anim['rate'], loc=(x - 200, y + 200))
    whole = _math(nt, 'FLOOR', scaled, loc=(x, y + 200))
    frame = _math(nt, 'FLOORED_MODULO', whole, float(n), loc=(x + 200, y + 200))
    for j, s in enumerate((scaled, whole, frame)):
        s.node.name = '%sFB%dM%d' % (GAME_TAG, i, j)
    col, alpha = col0, alpha0
    for k in range(1, n):
        img = textures.images.get(texs[k])
        if img is None:
            continue
        tn = nt.nodes.new('ShaderNodeTexImage')
        tn.name = '%sT%dF%d' % (GAME_TAG, i, k)
        tn.label = first.label
        tn.image = img
        tn.extension, tn.interpolation = first.extension, first.interpolation
        tn.location = (x, y - 260 * k)
        if vector is not None:
            nt.links.new(vector, tn.inputs['Vector'])
        c = tn.outputs['Color']
        decoded = img.colorspace_settings.name not in ('Non-Color', 'Raw', 'Linear', 'Linear Rec.709')
        if decoded != textures.is_srgb(texs[k]):
            cv = nt.nodes.new('ShaderNodeGroup')
            cv.node_tree = srgb_to_linear_group() if textures.is_srgb(texs[k]) else linear_to_srgb_group()
            cv.name = '%sRAW%dF%d' % (GAME_TAG, i, k)
            cv.location = (x + 250, y - 260 * k)
            nt.links.new(c, cv.inputs[0])
            c = cv.outputs[0]
        on = _math(nt, 'COMPARE', frame, float(k), 0.5, loc=(x + 450, y - 260 * k + 100))
        on.node.name = '%sFB%dK%d' % (GAME_TAG, i, k)
        mc = nt.nodes.new('ShaderNodeMix')
        mc.data_type = 'RGBA'
        mc.name = '%sFB%dC%d' % (GAME_TAG, i, k)
        mc.location = (x + 650, y - 260 * k)
        nt.links.new(on, mc.inputs['Factor'])
        nt.links.new(col, mc.inputs[6])
        nt.links.new(c, mc.inputs[7])
        col = mc.outputs[2]
        ma = nt.nodes.new('ShaderNodeMix')
        ma.data_type = 'FLOAT'
        ma.name = '%sFB%dA%d' % (GAME_TAG, i, k)
        ma.location = (x + 650, y - 260 * k - 120)
        nt.links.new(on, ma.inputs['Factor'])
        if isinstance(alpha, float):
            ma.inputs[2].default_value = alpha
        else:
            nt.links.new(alpha, ma.inputs[2])
        nt.links.new(tn.outputs['Alpha'], ma.inputs[3])
        alpha = ma.outputs[0]
    return col, alpha


def remove_game_nodes(mat):
    """Delete every node this module added (names starting with GAME_TAG),
    leaving the simplified material nodes alone.
    """
    nt = mat.node_tree
    for n in [n for n in nt.nodes if n.name.startswith((GAME_TAG, 'P:'))]:
        nt.nodes.remove(n)


def attach(mat, plan, textures):
    """Add the translated game shader to an imported material's node tree and
    feed it into the Game Look switch. Returns True on success."""
    nt = mat.node_tree
    sw = nt.nodes.get('C2_Switch')
    if sw is None or plan is None:
        return False
    remove_game_nodes(mat)
    groups = [stage_group(plan, s) for s in range(plan.last + 1)]
    x0 = 1200
    # parameter values
    pnodes = {}
    used = set()
    for g in groups:
        for s in g.interface.items_tree:
            if s.in_out == 'INPUT' and s.name.startswith('P:'):
                used.add(s.name)
    for k, key in enumerate(sorted(used)):
        name, el, comp = key[2:].rsplit('.', 2)
        v = nt.nodes.new('ShaderNodeValue')
        v.name = v.label = key
        v.location = (x0, -100 - 60 * k)
        v.outputs[0].default_value = param_value(mat, plan, name, int(el), int(comp))
        pnodes[key] = v
    gnodes = []
    for s, g in enumerate(groups):
        gn = nt.nodes.new('ShaderNodeGroup')
        gn.node_tree = g
        gn.name = '%sS%d' % (GAME_TAG, s)
        gn.location = (x0 + 400 + 700 * s, 400)
        gnodes.append(gn)
        for inp in gn.inputs:
            if inp.name in pnodes:
                nt.links.new(pnodes[inp.name].outputs[0], inp)
    # textures, stage by stage
    l2s = None
    for i, (sampler, stage, u, v, w) in enumerate(plan.samples):
        src = gnodes[stage].outputs.get('C%d' % i)
        usage, idx = texture_index(mat, plan, sampler)
        src_tex = textures
        if idx is None and usage in textures.global_entries:
            idx = textures.global_entries[usage]   # fog / ambient cube maps from scene_settings
        elif idx is None and textures.parent is not None and usage in textures.parent.global_entries:
            src_tex = textures.parent
            idx = src_tex.global_entries[usage]
        cube = plan.sampler_types.get(sampler) == 'cube'
        img = None
        if idx is not None:
            img = src_tex.cube_image(idx) if cube else src_tex.images.get(idx)
        pos = (x0 + 400 + 700 * stage + 250, -400 - 220 * i)
        if img is None:
            const = next((c for p, c in MISSING.items() if usage and usage.startswith(p)), (1.0, 1.0, 1.0, 1.0))
            rgb = nt.nodes.new('ShaderNodeCombineXYZ')
            rgb.name = '%sT%d' % (GAME_TAG, i)
            rgb.location = pos
            for k in range(3):
                rgb.inputs[k].default_value = const[k]
            col_out, alpha_out = rgb.outputs[0], const[3]
        else:
            tn = nt.nodes.new('ShaderNodeTexImage')
            tn.name = '%sT%d' % (GAME_TAG, i)
            tn.label = usage or sampler
            tn.image = img
            tn.location = pos
            st = plan.tr.sampler_states.get(sampler, {})
            tn.extension = 'EXTEND' if st.get('AddressU') == 'CLAMP' else 'REPEAT'
            tn.interpolation = 'Closest' if st.get('MinFilter') == 'POINT' else 'Linear'
            if cube:
                cu = nt.nodes.new('ShaderNodeGroup')
                cu.node_tree = cube_uv_group()
                cu.name = '%sCU%d' % (GAME_TAG, i)
                cu.location = (pos[0] - 200, pos[1])
                nt.links.new(src, cu.inputs[0])
                nt.links.new(cu.outputs[0], tn.inputs['Vector'])
            elif src is not None:
                nt.links.new(src, tn.inputs['Vector'])
            col_out, alpha_out = tn.outputs['Color'], tn.outputs['Alpha']
            decoded = img.colorspace_settings.name not in ('Non-Color', 'Raw', 'Linear', 'Linear Rec.709')
            want = src_tex.is_srgb(idx)
            if decoded != want:
                # match the game: sRGB-flagged textures decoded, others raw
                cv = nt.nodes.new('ShaderNodeGroup')
                cv.node_tree = srgb_to_linear_group() if want else linear_to_srgb_group()
                cv.name = '%sRAW%d' % (GAME_TAG, i)
                cv.location = (pos[0] + 250, pos[1])
                nt.links.new(col_out, cv.inputs[0])
                col_out = cv.outputs[0]
            anim = texture_animation(mat).get(usage)
            if anim and not cube:
                col_out, alpha_out = _flipbook(nt, anim, src_tex, tn, src, col_out, alpha_out, pos, i)
        for gn in gnodes[stage + 1:]:
            if 'T%d' % i in gn.inputs:
                nt.links.new(col_out, gn.inputs['T%d' % i])
                if isinstance(alpha_out, float):
                    gn.inputs['T%d.a' % i].default_value = alpha_out
                else:
                    nt.links.new(alpha_out, gn.inputs['T%d.a' % i])
    # output: tone map, blending, alpha test
    fin = gnodes[-1]
    post = nt.nodes.new('ShaderNodeGroup')
    post.node_tree = post_group()
    post.name = GAME_TAG + 'Post'
    post.location = (fin.location.x + 300, fin.location.y)
    for k in range(3):
        nt.links.new(fin.outputs[k], post.inputs[k])
    em = nt.nodes.new('ShaderNodeEmission')
    em.name = GAME_TAG + 'Emission'
    em.location = (post.location.x + 250, post.location.y)
    nt.links.new(post.outputs[0], em.inputs['Color'])
    out = em.outputs[0]
    blend = bool(_state(plan, mat, 'AlphaBlendEnable', 0))
    src_b = str(_state(plan, mat, 'SrcBlend', 'SRCALPHA')).upper()
    dst_b = str(_state(plan, mat, 'DestBlend', 'INVSRCALPHA')).upper()
    alpha = fin.outputs['Alpha']
    mode = 'OPAQUE'
    if blend:
        tr_ = nt.nodes.new('ShaderNodeBsdfTransparent')
        tr_.name = GAME_TAG + 'Transparent'
        tr_.location = (em.location.x, em.location.y - 150)
        if dst_b == 'ONE':
            mode = 'ADD'
            if src_b == 'SRCALPHA':
                # emission x alpha: mixing with an empty (black) shader input
                m = nt.nodes.new('ShaderNodeMixShader')
                m.name = GAME_TAG + 'AddAlpha'
                m.location = (em.location.x + 200, em.location.y - 60)
                nt.links.new(alpha, m.inputs[0])
                nt.links.new(em.outputs[0], m.inputs[2])
                out = m.outputs[0]
            ad = nt.nodes.new('ShaderNodeAddShader')
            ad.name = GAME_TAG + 'Add'
            ad.location = (em.location.x + 400, em.location.y)
            nt.links.new(tr_.outputs[0], ad.inputs[0])
            nt.links.new(out, ad.inputs[1])
            out = ad.outputs[0]
        elif src_b in ('DESTCOLOR', 'ZERO') and dst_b in ('ZERO', 'SRCCOLOR'):
            mode = 'MULTIPLY'
            nt.links.new(post.outputs[0], tr_.inputs['Color'])
            out = tr_.outputs[0]
        else:
            mode = 'BLEND'
            m = nt.nodes.new('ShaderNodeMixShader')
            m.name = GAME_TAG + 'Blend'
            m.location = (em.location.x + 200, em.location.y)
            nt.links.new(alpha, m.inputs[0])
            nt.links.new(tr_.outputs[0], m.inputs[1])
            nt.links.new(em.outputs[0], m.inputs[2])
            out = m.outputs[0]
    test = bool(_state(plan, mat, 'AlphaTestEnable', 0))
    clip = fin.outputs['Clip']
    if test:
        ref = float(_state(plan, mat, 'AlphaRef', 0)) / 255.0
        cut = _math(nt, 'LESS_THAN', alpha, ref + 1e-6, loc=(em.location.x, em.location.y - 300))
        clip = _math(nt, 'MAXIMUM', clip, cut, loc=(em.location.x + 150, em.location.y - 300))
        clip.node.name = GAME_TAG + 'ClipMax'
        cut.node.name = GAME_TAG + 'Cut'
    if test or plan.tr.kill:
        tr2 = nt.nodes.new('ShaderNodeBsdfTransparent')
        tr2.name = GAME_TAG + 'Clipped'
        tr2.location = (em.location.x + 400, em.location.y - 250)
        m = nt.nodes.new('ShaderNodeMixShader')
        m.name = GAME_TAG + 'Clip'
        m.location = (em.location.x + 600, em.location.y)
        nt.links.new(clip, m.inputs[0])
        nt.links.new(out, m.inputs[1])
        nt.links.new(tr2.outputs[0], m.inputs[2])
        out = m.outputs[0]
    mat['c2_game_mode'] = mode
    out = _add_inspect(mat, plan, textures, out, (em.location.x + 800, em.location.y), mode, test or bool(plan.tr.kill))
    nt.links.new(out, sw.inputs[2])
    try:
        if mode in ('BLEND', 'ADD', 'MULTIPLY'):
            mat.surface_render_method = 'BLENDED'
        elif test or plan.tr.kill:
            mat.surface_render_method = 'DITHERED'
    except (AttributeError, TypeError):
        pass
    try:
        if mode in ('BLEND', 'ADD', 'MULTIPLY'):
            mat.blend_method = 'BLEND'
        elif test or plan.tr.kill:
            mat.blend_method = 'CLIP' if hasattr(mat, 'alpha_threshold') else 'HASHED'
    except (AttributeError, TypeError):
        pass
    mat.use_backface_culling = bool(_state(plan, mat, 'CullBackFaces', 1))
    mat['c2_game_shader'] = plan.effect
    mat['c2_gamma_params'] = sorted(gamma_params(plan))
    return True


def set_param(mat, name, values):
    """Push edited parameter values into the material's Value nodes."""
    nt = mat.node_tree
    if nt is None:
        return
    vals = values if isinstance(values, (list, tuple)) else (values,)
    for c, x in enumerate(vals):
        n = nt.nodes.get('P:%s.0.%d' % (name, c))
        if n is not None:
            n.outputs[0].default_value = float(x)


# ----------------------------------------------------------------------
# engine values from the scene
# ----------------------------------------------------------------------

def sh_constants(sh27):
    """27 SH coefficients (R, G, B x 9) -> objectSH[7], the 7 float4 constants the
    game's shaders read: the packed irradiance form of P.-P. Sloan, "Stupid
    Spherical Harmonics (SH) Tricks" (GDC 2008), with the convolution constants
    folded in."""
    sp = math.sqrt(math.pi)
    c0 = 1.0 / (2.0 * sp)
    c1 = math.sqrt(3.0) / (3.0 * sp)
    c2 = math.sqrt(15.0) / (8.0 * sp)
    c3 = math.sqrt(5.0) / (16.0 * sp)
    c4 = 0.5 * c2
    sh = [sh27[9 * c:9 * c + 9] for c in range(3)]
    out = []
    for c in range(3):
        s = sh[c]
        out.append((-c1 * s[3], -c1 * s[1], c1 * s[2], c0 * s[0] - c3 * s[6]))
    for c in range(3):
        s = sh[c]
        out.append((c2 * s[4], -c2 * s[5], 3.0 * c3 * s[6], -c2 * s[7]))
    out.append((c4 * sh[0][8], c4 * sh[1][8], c4 * sh[2][8], 1.0))
    return out


# No world loaded (a character on its own in the Cars 2 Toolkit): the game never
# draws a car without a map, so Game Look stands in a neutral daylight: Tokyo B's
# sun, exposure and PrimarySceneRig ambient (its SH averaged to grey), white
# sun light, no fog. A character's reflection cube (envMapInstance: in game the
# map's light probe) is a plain grey then (gamerender.NEUTRAL_ENV).
_TOKYO_B_RIG = (3.21515, -2.92023, 0.84082, 0.2339, -0.0, -0.03559, -1.80963, 0.0046, -2.27407,
                3.13925, -3.18001, 0.39127, -0.17133, -0.0, -0.03865, -2.56189, -0.00074, -3.00692,
                3.62312, -3.75383, 0.12839, -0.03435, -0.0, -0.04539, -3.35224, 0.00082, -4.39421)
NEUTRAL_SH = tuple(sum(_TOKYO_B_RIG[9 * c + k] for c in range(3)) / 3.0 for c_ in range(3) for k in range(9))
NEUTRAL_LIGHT = {'g_sunLightDirection': (-0.40, 0.80, -0.45), 'g_sunLightIntensity': 3.0,
                 'g_sunLightPrimary': (1.0, 1.0, 1.0), 'g_sunLightTertiary': (0.3, 0.3, 0.3),
                 'g_primaryTertiaryBlend': 1.0, 'g_sunLightSoftness': 0.0, 'g_HDR_Exposure': 0.08, 'avgLum': 0.18,
                 'g_HDR_Min_Luminance': 0.18, 'g_HDR_Max_Luminance': 14.8, 'g_EnvMapExposure': 1.0,
                 'g_FogStart': 100000.0, 'g_FogEnd': 100001.0, 'g_FogShadowEnd': 2000.0}


def _neutral_light(set_engine):
    """The engine values of the neutral daylight (no world)."""
    for k, v in NEUTRAL_LIGHT.items():
        if k == 'g_sunLightDirection':
            ln = math.sqrt(sum(x * x for x in v)) or 1.0
            v = tuple(x / ln for x in v)
        set_engine(k, v)
    for el, v in enumerate(sh_constants(list(NEUTRAL_SH))):
        set_engine('objectSH', v, element=el)


def sync_engine(scene):
    """Engine globals from the scene: scene_settings values, the sunLight node,
    lightmap specular / normal lights and the primary light rig."""
    if not engine_active(scene):
        return

    def set_engine(name, values, element=0):
        """Shorthand for _set_engine on this scene."""
        _set_engine(name, values, element, scene)
    sc = scene.c2
    ss = sc.scene_settings
    if ss is not None:
        for p in ss.c2.props:
            if p.ptype == 'Float':
                set_engine(p.name, tuple(loaded_value(p, c, p.name in SHARED_GAMMA) for c in range(p.size))
                           if p.size > 1 else p.fvalue[0])
        vals = {p.name: (tuple(p.fvalue[:p.size]) if p.size > 1 else p.fvalue[0]) for p in ss.c2.props}
        lo = vals.get('g_HDR_Min_Luminance', 0.05)
        hi = vals.get('g_HDR_Max_Luminance', 10.0)
        # unmeasured: the game's adaptation mostly settles at the world's minimum
        set_engine('avgLum', min(max(sc.exposure_luminance, lo), hi) if sc.exposure_luminance > 0 else lo)
    root = sc.root
    if root is None:
        if ss is None:
            _neutral_light(set_engine)
        return
    from . import common
    sun = None
    spec = normal = None
    for o in common.descendants(root):
        if o.type != 'LIGHT':
            continue
        if o.get('c2_name', o.name) == 'sunLight':
            sun = o
        if o.data.c2.lm_specular:
            spec = o
        if o.data.c2.lm_normal:
            normal = o

    def direction(o):
        """Unit direction (game space) a light points along: its local Z axis."""
        m = common.game_matrix(root, o)
        d = (m[0][2], m[1][2], m[2][2])
        ln = math.sqrt(sum(x * x for x in d)) or 1.0
        return tuple(x / ln for x in d)
    if sun is not None:
        d = direction(sun)
        set_engine('g_sunLightDirection', d)
        set_engine('g_LightMapNormalDirection', direction(normal) if normal else d)
        set_engine('g_LightMapSpecularDir', direction(spec) if spec else d)
        if spec is not None:
            set_engine('g_LightMapSpecularColor', tuple(c * spec.data.c2.intensity for c in spec.data.color))
        elif ss is not None:
            vals = {p.name: p for p in ss.c2.props}
            pc = vals.get('g_sunLightPrimary')
            it = vals.get('g_sunLightIntensity')
            if pc is not None and it is not None:
                set_engine('g_LightMapSpecularColor', tuple(loaded_value(pc, c, True) * it.fvalue[0] for c in range(3)))
    rig = next((r for r in sc.light_rigs if r.type_name == 'PrimarySceneRig'), sc.light_rigs[0] if len(sc.light_rigs) else None)
    if rig is not None:
        for el, v in enumerate(sh_constants(list(rig.sh))):
            set_engine('objectSH', v, element=el)


# ----------------------------------------------------------------------
# inspection views (Lighting panel > Inspect): what each surface does
# ----------------------------------------------------------------------

INSPECT_MODES = [
    ('OFF', 'Off', 'The game shading'),
    ('SPECULAR', 'Shininess', 'Specular highlights: specular map x specular colour (yellow); grey = not shiny'),
    ('REFLECTION', 'Reflections', 'Cube-map reflections: how much the environment map shows (cyan); grey = none'),
    ('TRANSPARENCY', 'Transparency', 'Blue = alpha blended, orange = additive glow, purple = multiply, '
                                     'green = alpha tested (cut-out), grey = opaque'),
    ('BAKED', 'Baked Lighting', 'Magenta = lightmap, yellow = VertexBaked colours, cyan = lit at run time'),
    ('EMISSIVE', 'Glow', 'Emissive (self-lit) parts'),
    ('EFFECT', 'Effect', 'One colour per effect (shader), to see which surfaces share one'),
]
INSPECT_INDEX = {k: i for i, (k, _a, _b) in enumerate(INSPECT_MODES)}
INSPECT = 'C2A_Inspect'
GREY = (0.12, 0.12, 0.12)


def inspect_group():
    """mode + one colour per mode -> colour (mode 0 is never used)."""
    g = bpy.data.node_groups.get(INSPECT)
    if g is not None:
        return g
    ins = [('Mode', 'NodeSocketFloat')] + [(k, 'NodeSocketColor') for k, _a, _b in INSPECT_MODES[1:]]
    g, gi, go = _new_group(INSPECT, ins, [('Color', 'NodeSocketColor')])
    acc = None
    for i, (k, _a, _b) in enumerate(INSPECT_MODES[1:], 1):
        sel = _math(g, 'COMPARE', gi.outputs['Mode'], float(i), 0.25, loc=(-300, -150 * i))
        m = g.nodes.new('ShaderNodeVectorMath')
        m.operation = 'SCALE'
        m.location = (-100, -150 * i)
        g.links.new(gi.outputs[k], m.inputs[0])
        g.links.new(sel, m.inputs['Scale'])
        if acc is None:
            acc = m.outputs[0]
        else:
            a = g.nodes.new('ShaderNodeVectorMath')
            a.operation = 'ADD'
            a.location = (100, -150 * i)
            g.links.new(acc, a.inputs[0])
            g.links.new(m.outputs[0], a.inputs[1])
            acc = a.outputs[0]
    g.links.new(acc, go.inputs[0])
    return g


def _effect_color(effect):
    """A stable colour per effect name (hash -> hue) for the Effect inspect mode."""
    import zlib
    h = zlib.crc32(effect.encode())
    import colorsys
    return colorsys.hsv_to_rgb((h % 360) / 360.0, 0.65, 0.9)


def _add_inspect(mat, plan, textures, out, loc, mode, cutout):
    """Add the inspect overlay: false-colour views (specular, reflection,
    transparency, baked lighting, glow, effect) chosen by the engine value
    c2Inspect, shaded by facing angle. Returns the shader socket that mixes
    the overlay over the normal material output.
    """
    nt = mat.node_tree
    x, y = loc
    usage = {s: u for s, u in plan.sampler_usage.items()}
    used = {s for s, *_ in plan.samples}

    def image_node(u, name, yy):
        """An Image Texture node (on UV map Uv1) for the sampler with usage u, or None."""
        for s in used:
            if usage.get(s) == u:
                _u, idx = texture_index(mat, plan, s)
                img = textures.images.get(idx) if idx is not None else None
                if img is not None:
                    tn = nt.nodes.new('ShaderNodeTexImage')
                    tn.name = GAME_TAG + name
                    tn.label = u
                    tn.image = img
                    tn.location = (x, yy)
                    uv = nt.nodes.new('ShaderNodeUVMap')
                    uv.uv_map = 'Uv1'
                    uv.name = GAME_TAG + name + 'UV'
                    uv.location = (x - 200, yy)
                    nt.links.new(uv.outputs[0], tn.inputs['Vector'])
                    return tn
        return None

    def rgb(name, col, yy):
        """An RGB node with a fixed colour; returns its output socket."""
        n = nt.nodes.new('ShaderNodeRGB')
        n.name = GAME_TAG + name
        n.outputs[0].default_value = (*col, 1.0)
        n.location = (x + 200, yy)
        return n.outputs[0]

    def times(a, b, name, yy):
        """Multiply two colour sockets (Mix node, MULTIPLY)."""
        m = nt.nodes.new('ShaderNodeMix')
        m.data_type = 'RGBA'
        m.blend_type = 'MULTIPLY'
        m.name = GAME_TAG + name
        m.inputs['Factor'].default_value = 1.0
        m.location = (x + 400, yy)
        nt.links.new(a, m.inputs[6])
        nt.links.new(b, m.inputs[7])
        return m.outputs[2]

    params = set(plan.defaults)
    cols = {}
    # shininess
    if any(u.startswith('specularMap') for u in usage.values()) or 'specularShineA1' in params:
        sc = [param_value(mat, plan, 'specularColorA1', 0, c) for c in range(3)] if 'specularColorA1' in params \
            else [1.0, 1.0, 1.0]
        shine = param_value(mat, plan, 'specularShineA1', 0, 0) if 'specularShineA1' in params else 8.0
        k = min(1.0, 0.25 + shine / 48.0)
        col = rgb('InsSpec', (sc[0] * k, sc[1] * k * 0.9, sc[2] * k * 0.2), y)
        tn = image_node('specularMapA1', 'InsSpecMap', y)
        cols['SPECULAR'] = times(col, tn.outputs['Color'], 'InsSpecMul', y) if tn else col
    else:
        cols['SPECULAR'] = rgb('InsSpec', GREY, y)
    # reflections
    cube = [s for s in used if plan.sampler_types.get(s) == 'cube' and usage.get(s) in ('environmentMap', 'envMapInstance')]
    if cube:
        if 'environmentReflectivityColorA1' in params:
            amt = sum(1.0 - param_value(mat, plan, 'environmentReflectivityColorA1', 0, c) for c in range(3)) / 3.0
        else:
            amt = param_value(mat, plan, 'environmentReflectivityMapAmountA1', 0, 0) \
                if 'environmentReflectivityMapAmountA1' in params else 0.6
        amt = max(0.15, min(1.0, amt))
        col = rgb('InsRefl', (0.1 * amt, amt, amt), y - 300)
        tn = image_node('environmentReflectivityMapA1', 'InsReflMap', y - 300)
        if tn:   # the map is stored inverted: white = no reflection
            inv = nt.nodes.new('ShaderNodeInvert')
            inv.name = GAME_TAG + 'InsReflInv'
            inv.location = (x + 200, y - 380)
            nt.links.new(tn.outputs['Color'], inv.inputs['Color'])
            col = times(col, inv.outputs[0], 'InsReflMul', y - 300)
        cols['REFLECTION'] = col
    else:
        cols['REFLECTION'] = rgb('InsRefl', GREY, y - 300)
    cols['TRANSPARENCY'] = rgb('InsAlpha', {'BLEND': (0.1, 0.35, 1.0), 'ADD': (1.0, 0.5, 0.05),
                                            'MULTIPLY': (0.6, 0.2, 0.9)}.get(mode, (0.1, 0.85, 0.2) if cutout else GREY),
                               y - 600)
    baked = arcade.baked_mode(plan.effect)
    cols['BAKED'] = rgb('InsBaked', {'LIGHTMAP': (1.0, 0.15, 0.9), 'VERTEX': (1.0, 0.85, 0.1)}.get(baked, (0.1, 0.8, 1.0)),
                        y - 700)
    if any(u.startswith('emissiveMap') for u in usage.values()):
        col = rgb('InsGlow', (1.0, 0.6, 0.2), y - 800)
        tn = image_node('emissiveMapA1', 'InsGlowMap', y - 800)
        cols['EMISSIVE'] = times(col, tn.outputs['Color'], 'InsGlowMul', y - 800) if tn else col
    else:
        cols['EMISSIVE'] = rgb('InsGlow', GREY, y - 800)
    cols['EFFECT'] = rgb('InsEffect', _effect_color(plan.effect), y - 900)
    sel = nt.nodes.new('ShaderNodeGroup')
    sel.node_tree = inspect_group()
    sel.name = GAME_TAG + 'InspectSelect'
    sel.location = (x + 650, y - 400)
    mode_sock = engine_value(nt, 'c2Inspect', 0, 0, (x + 450, y - 1050))
    nt.links.new(mode_sock, sel.inputs['Mode'])
    for k, c in cols.items():
        nt.links.new(c, sel.inputs[k])
    # simple facing shading so shapes stay readable
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    geo.name = GAME_TAG + 'InsGeo'
    geo.location = (x + 650, y - 700)
    d = nt.nodes.new('ShaderNodeVectorMath')
    d.operation = 'DOT_PRODUCT'
    d.name = GAME_TAG + 'InsDot'
    d.location = (x + 850, y - 700)
    nt.links.new(geo.outputs['Normal'], d.inputs[0])
    nt.links.new(geo.outputs['Incoming'], d.inputs[1])
    shade = _math(nt, 'MULTIPLY_ADD', _math(nt, 'ABSOLUTE', d.outputs['Value'], loc=(x + 1000, y - 700)), 0.65, 0.35,
                  loc=(x + 1150, y - 700))
    shade.node.name = GAME_TAG + 'InsShade'
    sc = nt.nodes.new('ShaderNodeVectorMath')
    sc.operation = 'SCALE'
    sc.name = GAME_TAG + 'InsScale'
    sc.location = (x + 900, y - 400)
    nt.links.new(sel.outputs[0], sc.inputs[0])
    nt.links.new(shade, sc.inputs['Scale'])
    em = nt.nodes.new('ShaderNodeEmission')
    em.name = GAME_TAG + 'InsEmission'
    em.location = (x + 1100, y - 400)
    nt.links.new(sc.outputs[0], em.inputs['Color'])
    on = _math(nt, 'GREATER_THAN', mode_sock, 0.5, loc=(x + 1100, y - 250))
    on.node.name = GAME_TAG + 'InsOn'
    mix = nt.nodes.new('ShaderNodeMixShader')
    mix.name = GAME_TAG + 'InspectMix'
    mix.location = (x + 1300, y)
    nt.links.new(on, mix.inputs[0])
    nt.links.new(out, mix.inputs[1])
    nt.links.new(em.outputs[0], mix.inputs[2])
    return mix.outputs[0]


def set_inspect(mode):
    """Switch every material's inspect overlay (mode from INSPECT_INDEX; 0 = off)."""
    set_engine('c2Inspect', float(INSPECT_INDEX.get(mode, 0)))


# ----------------------------------------------------------------------
# effect descriptions
# ----------------------------------------------------------------------

USAGE_LABELS = [('diffuseMapA2', 'texture blending'), ('normalMap', 'normal map'), ('specularMap', 'specular map'),
                ('emissiveMap', 'glow map'), ('environmentMap', 'cube-map reflections'),
                ('envMapInstance', 'probe reflections'), ('environmentReflectivityMap', 'reflection mask'),
                ('lightMap1', 'lightmap'), ('modMap', 'detail map'), ('foamMap', 'foam'), ('colorBuffer', 'refraction'),
                ('ReflectionPlaneTexture', 'planar reflection')]


def describe(plan):
    """Short list of what an effect does, for the Material panel."""
    if plan is None:
        return []
    if getattr(plan, 'description', None) is None:
        plan.description = _describe(plan)
    return plan.description


def _describe(plan):
    """Build the feature list describe() caches on the plan."""
    usages = set(plan.sampler_usage.values())
    out = []
    for key, label in USAGE_LABELS:
        if any(u.startswith(key) for u in usages) and label not in out:
            out.append(label)
    if 'specularShineA1' in plan.defaults and 'specular map' not in out:
        out.append('specular')
    if 'VertexBaked' in plan.tr.used_inputs:
        out.append('vertex lighting')
    if 'objectSH' in str(plan.color) or any(e[0] == 'glob' and e[1] == 'objectSH' for e in fxgraph.nodes_of(plan.color)):
        out.append('lit at run time')
    if 'g_rimLightColor' in plan.defaults:
        out.append('rim light')
    if 'subsurfaceColorA1' in plan.defaults:
        out.append('subsurface')
    states = plan.states
    blend = states.get('AlphaBlendEnable')
    if blend not in (0, None) and str(blend) != '0':
        out.append('additive glow' if str(states.get('DestBlend')).upper() == 'ONE' else 'transparent')
    if states.get('AlphaTestEnable') not in (0, None) or plan.tr.kill:
        out.append('cut-out')
    if any(e[0] == 'glob' and e[1] == 'g_Time' for e in fxgraph.nodes_of(plan.color + [plan.alpha])):
        out.append('animated')
    return out


def editable_params(plan):
    """[(name, ptype, datatype, size, default list)] of the parameters a
    material sets for this effect (not engine-driven ones)."""
    out = []
    for name, p in plan.tr.params.items():
        if name.startswith('g_World') or name in ('objectSH', 'g_FinalAlpha', 'BoneTransforms') or \
                p.get('Scope') not in ('PerMaterial', 'PerInstance', 'NoRegister'):
            continue
        typ = str(p.get('Type', 'float')).lower()
        rows, cols = int(p.get('Rows', 1) or 1), int(p.get('Cols', 1) or 1)
        if rows > 1:
            continue  # matrices are engine-set
        d = p.get('Default')
        d = list(d) if isinstance(d, (list, tuple)) else ([d] if d is not None else [0.0])
        if typ == 'float':
            out.append((name, 'Float', {1: 0, 3: 2, 4: 3}.get(cols, 0), cols, [float(x) for x in d]))
        else:
            out.append((name, 'Bool' if typ == 'bool' else 'Int', 10, 1, [float(d[0])]))
    return out


def material_samplers(plan):
    """Texture usages a material provides for this effect (not engine/instance ones)."""
    out = []
    for s, u in plan.sampler_usage.items():
        if u in INSTANCE_USAGES or u in GLOBAL_SAMPLERS.values() or s in fxgraph.ENGINE_SAMPLERS:
            continue
        if u not in out:
            out.append(u)
    return out


def ensure_effect_entries(mat, plan):
    """Give a material every parameter and texture slot its effect uses
    (missing ones get the effect's defaults and are marked extra)."""
    c = mat.c2
    have = {p.name for p in c.props}
    for name, ptype, dtype, size, d in editable_params(plan):
        if name in have:
            continue
        p = c.props.add()
        p.name, p.ptype, p.datatype, p.size, p.extra = name, ptype, dtype, size, True
        vals = (d + [0.0] * 4)[:4]
        if ptype == 'Float':
            p.fvalue = vals
        else:
            p.ivalue = int(vals[0])
        p.fdefault = vals
    have_t = {t.usage for t in c.textures}
    for u in material_samplers(plan):
        if u not in have_t:
            t = c.textures.add()
            t.usage, t.texture, t.extra = u, -1, True


# ----------------------------------------------------------------------
# switching effects: dropped and required texture slots
# ----------------------------------------------------------------------
# Every shipped material lists exactly the texture slots and parameters its
# effect uses (3,798 materials in 21 worlds and 295 in the 58 characters),
# except two slots the game fills itself: renderedEyeTexture (the engine
# renders it) and environmentScrollMap (left out by 90 character materials
# whose effects sample it). So a switch takes
# off what the new effect doesn't use. Dropped slots and parameters are kept
# on the material (mat['c2_dropped']) and come back when a later switch picks
# an effect that uses them; export leaves them out of the MaterialPool entry.

ENGINE_TEXTURES = ('renderedEyeTexture', 'environmentScrollMap')     # never required: the game fills them


def usage_label(usage):
    """'glow map (emissiveMap)' for a texture usage."""
    for key, label in USAGE_LABELS + [('diffuseMap', 'diffuse map')]:
        if usage.startswith(key):
            return '%s (%s)' % (label, usage)
    return usage


def required_textures(plan):
    """Texture usages a material must fill for this effect before export."""
    return [u for u in material_samplers(plan) if u not in ENGINE_TEXTURES]


def dropped_cache(mat):
    """{'textures': {usage: {texture, extra}}, 'params': {name: values}} kept on the material."""
    import json
    try:
        d = json.loads(mat.get('c2_dropped') or '{}')
    except ValueError:
        d = {}
    d.setdefault('textures', {})
    d.setdefault('params', {})
    return d


def effect_switch(mat, plan):
    """What switching mat to the effect of `plan` does, without doing it:
    {'drop_textures': [(usage, texture index)], 'drop_params': [names],
     'restore': [usages and parameter names from the cache],
     'blank': [usages the new effect needs that will start empty]}."""
    c = mat.c2
    cache = dropped_cache(mat)
    want_t = set(material_samplers(plan))
    want_p = {e[0] for e in editable_params(plan)}
    have_t = {t.usage: t.texture for t in c.textures}
    out = {'drop_textures': [(u, k) for u, k in have_t.items() if u not in want_t and u not in INSTANCE_USAGES],
           'drop_params': [p.name for p in c.props if p.name not in want_p],
           'restore': [u for u in want_t if u not in have_t and cache['textures'].get(u, {}).get('texture', -1) >= 0]
           + [n for n in want_p if n in cache['params'] and n not in {p.name for p in c.props}]}
    out['blank'] = [u for u in required_textures(plan) if have_t.get(u, -1) < 0 and u not in out['restore']]
    return out


def switch_effect_entries(mat, plan):
    """Fit the material's slots and parameters to its new effect: drop what the
    effect doesn't use into the cache, bring cached ones back, then add the
    rest with the effect's defaults (textures blank). Returns effect_switch()."""
    import json
    sw = effect_switch(mat, plan)
    c = mat.c2
    cache = dropped_cache(mat)
    drop_t = {u for u, _k in sw['drop_textures']}
    for i in reversed(range(len(c.textures))):
        t = c.textures[i]
        if t.usage in drop_t:
            if t.texture >= 0:       # a blank slot holds nothing worth keeping
                cache['textures'][t.usage] = {'texture': t.texture, 'extra': t.extra}
            c.textures.remove(i)
    drop_p = set(sw['drop_params'])
    for i in reversed(range(len(c.props))):
        p = c.props[i]
        if p.name in drop_p:
            if p.extra and _at_default(p):
                c.props.remove(i)        # added by an earlier switch and never set: nothing to keep
                continue
            cache['params'][p.name] = {'ptype': p.ptype, 'datatype': p.datatype, 'size': p.size,
                                       'fvalue': list(p.fvalue), 'ivalue': p.ivalue, 'extra': p.extra,
                                       'fdefault': list(p.fdefault)}
            c.props.remove(i)
    for u in sw['restore']:
        if u in cache['textures']:
            v = cache['textures'].pop(u)
            t = c.textures.add()
            t.usage, t.texture, t.extra = u, v['texture'], v['extra']
        elif u in cache['params']:
            v = cache['params'].pop(u)
            p = c.props.add()
            p.name, p.ptype, p.datatype, p.size, p.extra = u, v['ptype'], v['datatype'], v['size'], v['extra']
            p.fvalue, p.ivalue, p.fdefault = v['fvalue'], v['ivalue'], v['fdefault']
    ensure_effect_entries(mat, plan)
    if cache['textures'] or cache['params']:
        mat['c2_dropped'] = json.dumps(cache)
    elif 'c2_dropped' in mat:
        del mat['c2_dropped']
    return sw


def _at_default(p):
    """Does a material parameter still hold its effect default?"""
    if p.ptype == 'Float':
        return all(abs(a - b) < 1e-6 for a, b in zip(p.fvalue[:p.size], p.fdefault[:p.size]))
    return p.ivalue == int(p.fdefault[0])


def missing_textures(scene):
    """[(material, usage)] of required texture slots left blank (or pointing at
    no world texture) in the materials this world exports."""
    if game_context(scene) is None:
        return []
    world = scene.c2.world_name
    users = set()       # materials of meshes the export writes (not package previews)
    for o in scene.objects:
        if o.type == 'MESH' and not o.get('c2_excluded') and not o.get('c2_preview'):
            for s in o.material_slots:
                if s.material is not None:
                    users.add((s.material.c2.variant_of or s.material).name)
    mats = []
    for m in bpy.data.materials:
        c = m.c2
        if c.variant_of is not None or c.owner is not None:
            continue        # characters' materials are the Cars 2 Toolkit's to export
        # the world's own materials are all written; new ones when an exported
        # mesh uses them. Materials of other packages (pickups, choreographies)
        # aren't written by the world export.
        if m.get('c2_world', world) != world or (c.index < 0 and m.name not in users):
            continue
        mats.append(m)
    return missing_in(scene, mats)


def missing_in(scene, materials):
    """[(material, usage)] of these materials' required texture slots that are
    blank or point at no texture of their table (world or character)."""
    from . import gamerender
    out = []
    for m in materials:
        c = m.c2
        if c.variant_of is not None or m.get('c2_platform') != 'ARCADE' or not c.effect:
            continue
        game = material_context(scene, m)
        table = texture_table(scene, m)
        if game is None or table is None:
            continue
        plan = gamerender.meta_for(game.assets, c.effect, local=game.local_effects)
        if plan is None:
            continue
        have = {t.index for t in table}
        slots = {t.usage: t.texture for t in c.textures}
        for u in required_textures(plan):
            if slots.get(u, -1) not in have:
                out.append((m, u))
    return out


# ----------------------------------------------------------------------
# live editing
# ----------------------------------------------------------------------

SUSPEND = [0]          # >0 while importing / rebuilding: ignore property callbacks
RENDER_STATE_PARAMS = ('AlphaBlendEnable', 'AlphaTestEnable', 'AlphaRef', 'SrcBlend', 'DestBlend', 'CullBackFaces',
                       'ZWriteEnable', 'DepthTestAndWrite', 'SeparateAlphaBlendEnable')
_contexts = {}


def game_context(scene):
    """matnodes.GameContext rebuilt from the scene (after import)."""
    from . import common, matnodes
    if scene.c2.platform != 'ARCADE' or scene.c2.root is None:
        return None
    assets = common.game_assets(scene)
    if not arcade.effects_dir(assets):
        return None
    key = (scene.name, scene.c2.oct_path, assets)
    ctx = _contexts.get(key)
    if ctx is None:
        images = scene_images(scene)
        files = {t.index: bpy.path.abspath(t.replace if t.replace and t.replace.lower().endswith('.dds') else t.file)
                 for t in scene.c2.textures if t.file}
        ss = scene.c2.scene_settings
        glob = {t.usage: t.texture for t in ss.c2.textures} if ss is not None else {}
        folder = os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)),
                              scene.c2.world_name + '.oct.textures', 'equirect')
        ctx = matnodes.GameContext(assets, Textures(images, files, glob, folder, srgb=texture_srgb(scene)))
        _contexts[key] = ctx
    return ctx


_owner_contexts = {}   # character object pointer -> (key, GameContext, object name)

# Textures the engine binds itself to every material that samples them
# (sdaemon.exe loads misc\environmentScroll.dds from the
# startup packages -> the shared material's environmentScrollMap). No
# shipped material lists them. usage -> file in the startup packages.
ENGINE_FILES = {'environmentScrollMap': 'misc/environmentscroll.dds'}
_engine_textures = {}   # assets folder -> Textures of the ENGINE_FILES (index = position in it)


def engine_texture(assets, usage):
    """(Textures, index) of a texture the engine binds for `usage`, or (None, None)."""
    if usage not in ENGINE_FILES or not assets:
        return None, None
    tex = _engine_textures.get(assets)
    if tex is None:
        import tempfile
        cache = os.path.join(tempfile.gettempdir(), 'cars2_look', 'engine')
        files = {}
        for i, (u, member) in enumerate(sorted(ENGINE_FILES.items())):
            data = arcade.engine_file_bytes(assets, member)
            if data is None:
                continue
            path = os.path.join(cache, os.path.basename(member))
            if not (os.path.exists(path) and os.path.getsize(path) == len(data)):
                os.makedirs(cache, exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
            files[i] = path
        tex = _engine_textures[assets] = Textures({}, files, {}, cache)
    i = sorted(ENGINE_FILES).index(usage)
    return (tex, i) if i in tex.tex_files else (None, None)


def owner_assets(scene, owner):
    """The game's assets folder for a character's materials: the Game Assets
    Folder (or the world's), else the one above the character's file."""
    from . import common
    a = common.game_assets(scene) if scene is not None else None
    if a and arcade.effects_dir(a):
        return a
    a = owner.get('c2_assets')          # found when the character was imported (Cars 2 Toolkit)
    if a and arcade.effects_dir(bpy.path.abspath(a)):
        return bpy.path.abspath(a)
    src = owner.get('c2_source')
    if src:
        a = common.assets_dir_from(bpy.path.abspath(src))
        if a and arcade.effects_dir(a):
            return a
    return None


def owner_context(scene, owner):
    """matnodes.GameContext of a character's materials: its own textures
    (Object.c2tex), its own effects folder when it ships one, fog and
    reflections from the world when a world is loaded."""
    from . import matnodes
    assets = owner_assets(scene, owner)
    if assets is None:
        return None
    world = game_context(scene) if scene is not None else None
    key = (len(owner.c2tex), owner.get('c2tex_version', 0), assets, id(world))
    hit = _owner_contexts.get(owner.as_pointer())
    if hit is not None and hit[0] == key and hit[2] == owner.name:
        return hit[1]
    images, files, srgb = {}, {}, {}
    for t in owner.c2tex:
        path = bpy.path.abspath(t.replace or t.file) if (t.replace or t.file) else ''
        if t.file or t.replace:
            files[t.index] = path if path.lower().endswith('.dds') else bpy.path.abspath(t.file)
        if t.is_property_set('srgb'):
            srgb[t.index] = t.srgb
        if t.ttype == '2d' and path and os.path.exists(path):
            try:
                images[t.index] = bpy.data.images.load(path, check_existing=True)
            except RuntimeError:
                pass
    local = {}
    fx = bpy.path.abspath(owner.get('c2_effects_dir', '') or '')
    if fx and os.path.isdir(fx):
        for f in os.listdir(fx):
            if f.lower().endswith('.tfx'):
                with open(os.path.join(fx, f), 'rb') as fh:
                    local[f[:-4].lower()] = fh.read()
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), 'cars2_look', bpy.path.clean_name(owner.name), 'equirect')
    ctx = matnodes.GameContext(assets, Textures(images, files, {}, cache,
                                                parent=world.textures if world is not None else None, srgb=srgb),
                               local or None)
    _owner_contexts[owner.as_pointer()] = (key, ctx, owner.name)
    return ctx


def material_context(scene, mat):
    """The GameContext a material's textures and effect come from: a preview
    package's, a character's (C2MaterialProps.owner), else the world's."""
    base = mat.c2.variant_of or mat
    from . import gamerender
    g = gamerender.PACKAGES.get(base.name)
    if g is not None:
        return g
    if base.c2.owner is not None:
        return owner_context(scene, base.c2.owner)
    return game_context(scene)


def texture_table(scene, mat):
    """The C2Texture list a material's slot indices point into (a character's
    own, else the world's), or None for a preview package's material."""
    base = mat.c2.variant_of or mat
    if base.c2.owner is not None:
        return base.c2.owner.c2tex
    from . import gamerender
    if gamerender.PACKAGES.get(base.name) is not None:
        return None
    return scene.c2.textures


def slot_is_cube(scene, mat, usage):
    """Does the material's effect sample `usage` as a cube map? None: unknown."""
    game = material_context(scene, mat)
    if game is None:
        return None
    # the sampler kinds come from the pixel shader (the translated plan, cached per effect)
    plan = plan_for(game.assets, (mat.c2.variant_of or mat).c2.effect, game.local_effects)
    if plan is None:
        return None
    for s, u in plan.sampler_usage.items():
        if u == usage:
            return plan.sampler_types.get(s) == 'cube'
    return None


def owner_materials(owner):
    """The game materials whose textures are this character's."""
    return [m for m in bpy.data.materials if m.c2.variant_of is None and m.c2.owner == owner]


def on_owner_texture_replaced(scene, owner, index):
    """One of a character's textures changed: rebuild the materials using it."""
    _game_touch(None)
    owner['c2tex_version'] = owner.get('c2tex_version', 0) + 1
    if SUSPEND[0] or scene is None:
        return
    for m in owner_materials(owner):
        if any(t.texture == index for t in m.c2.textures):
            rebuild_material(scene, m)


def texture_srgb(scene):
    """TexturePool index -> the scene's sRGB setting (textures from older
    .blend files without one fall back to their DDS)."""
    return {t.index: t.srgb for t in scene.c2.textures if t.is_property_set('srgb')}


def scene_images(scene):
    """TexturePool index -> bpy image (the replacement when one is set)."""
    out = {}
    for t in scene.c2.textures:
        path = t.replace or t.file
        if not path or t.ttype != '2d':
            continue
        p = bpy.path.abspath(path)
        if not os.path.exists(p):
            p = bpy.path.abspath(t.file)
        try:
            out[t.index] = bpy.data.images.load(p, check_existing=True)
        except RuntimeError:
            pass
    return out


def _game_touch(mat):
    """Tell the Cars 2 Game render engine a material (None: the textures) changed."""
    from . import gamerender
    if mat is None:
        gamerender.textures_changed()
    else:
        gamerender.touch_material(mat.c2.variant_of or mat)


_variants = {'key': None, 'map': {}}


def variants_of(mat):
    """Per-object material variants (lightmap / probe copies) made from mat.
    The map is built once per material count: scanning every material per
    call cost ~1.5 ms, and keyed eye materials ask every frame."""
    key = len(bpy.data.materials)
    if _variants['key'] != key:
        m_ = {}
        for m in bpy.data.materials:
            v = m.c2.variant_of
            if v is not None:
                m_.setdefault(v.name, []).append(m.name)
        _variants.update(key=key, map=m_)
    out = []
    for n in _variants['map'].get(mat.name, ()):
        m = bpy.data.materials.get(n)
        if m is None or m.c2.variant_of != mat:     # renamed / re-pointed: look again
            _variants['key'] = None
            return [m for m in bpy.data.materials if m.c2.variant_of == mat]
        out.append(m)
    return out


def rebuild_material(scene, mat):
    """Rebuild the nodes of a material and all its variants (handlers suspended
    while doing so).
    """
    from . import matnodes
    game = material_context(scene, mat)
    images = game.textures.images if game else scene_images(scene)
    SUSPEND[0] += 1
    try:
        for m in [mat] + variants_of(mat):
            matnodes.build(m, images, game)
    finally:
        SUSPEND[0] -= 1


def on_param_changed(scene, mat, prop):
    """A material parameter was edited in the panel: update the node values in
    place (fast), or rebuild when it is a render-state parameter. Edits to the
    scene_settings material update the engine globals instead.
    """
    if mat is not None:
        _game_touch(mat)
    if SUSPEND[0] or mat is None or scene is None:
        return
    if scene.c2.scene_settings == mat:
        sync_engine(scene)
        return
    base = mat.c2.variant_of or mat
    if prop.name in RENDER_STATE_PARAMS:
        rebuild_material(scene, base)
        return
    raw = tuple(prop.fvalue[:prop.size]) if prop.ptype == 'Float' else (float(prop.ivalue),)
    gamma = prop.name in base.get('c2_gamma_params', ())
    vals = tuple(loaded_value(prop, c, gamma) for c in range(prop.size)) if prop.ptype == 'Float' else raw
    for m in [base] + variants_of(base):
        set_param(m, prop.name, vals)
        if prop.name == 'diffuseColorA1' and m.node_tree:   # simplified material too
            t = m.node_tree.nodes.get('C2_Tint')
            if t is not None and len(raw) >= 3:
                t.inputs[7].default_value = (*raw[:3], 1.0)


# simplified-material image nodes (matnodes) by texture usage
SIMPLE_IMAGE_NODES = {'diffuseMapA1': 'C2_Diffuse', 'diffuseMapA2': 'C2_DiffuseA2', 'diffuseMapA3': 'C2_DiffuseA3',
                      'diffuseMapA4': 'C2_DiffuseA4', 'normalMapA1': 'C2_NormalMap', 'emissiveMapA1': 'C2_Emissive'}


def retexture(scene, mat):
    """After a texture slot change: put the new images on the existing nodes.
    A rebuild regenerates the material's shaders (seconds each); an image swap
    doesn't. Returns False when the node setup itself has to change."""
    game = material_context(scene, mat)
    if game is None:
        return False
    textures = game.textures
    slots = {t.usage for t in mat.c2.textures}
    todo = []
    for m in [mat] + variants_of(mat):
        nt = m.node_tree
        plan = plan_for(game.assets, m.get('c2_game_shader', '')) if nt is not None else None
        if plan is None:
            return False
        for i, s in enumerate(plan.samples):
            usage, idx = texture_index(m, plan, s[0])
            if usage not in slots:
                continue          # lightmaps, engine maps: not what changed
            if usage in texture_animation(m):
                return False      # flipbook frames: rebuild
            if plan.sampler_types.get(s[0]) == 'cube':
                return False
            img = textures.images.get(idx) if idx is not None else None
            node = nt.nodes.get('%sT%d' % (GAME_TAG, i))
            if img is None or node is None or node.type != 'TEX_IMAGE':
                return False      # placeholder <-> image: different nodes
            decoded = img.colorspace_settings.name not in ('Non-Color', 'Raw', 'Linear', 'Linear Rec.709')
            conv = nt.nodes.get('%sRAW%d' % (GAME_TAG, i))
            want = None if decoded == textures.is_srgb(idx) else \
                (S2L if textures.is_srgb(idx) else L2S)
            if (conv.node_tree.name if conv is not None else None) != want:
                return False
            todo += [(n, img) for n in nt.nodes if n.type == 'TEX_IMAGE' and n.label == usage]
            simple = nt.nodes.get(SIMPLE_IMAGE_NODES.get(usage, ''))
            if simple is not None:
                todo.append((simple, img))
    for n, img in todo:
        if n.image != img:
            n.image = img
    return True


def on_material_changed(scene, mat, effect_changed=False):
    """Texture slots or the effect changed: swap images in place when possible
    (see retexture), else rebuild. A new effect first gets its parameter
    entries added.
    """
    if mat is not None:
        _game_touch(mat)
        if effect_changed:
            from . import gamerender
            gamerender.effect_changed(mat.c2.variant_of or mat)
    if SUSPEND[0] or mat is None or scene is None or mat.get('c2_platform') != 'ARCADE':
        return
    base = mat.c2.variant_of or mat
    if not effect_changed and retexture(scene, base):
        return
    if effect_changed:
        game = material_context(scene, base)
        from . import gamerender
        plan = gamerender.meta_for(game.assets, base.c2.effect, local=game.local_effects) if game else None
        if plan is not None:
            SUSPEND[0] += 1
            try:
                switch_effect_entries(base, plan)
            finally:
                SUSPEND[0] -= 1
    rebuild_material(scene, base)


def on_texture_replaced(scene, index):
    """A TexturePool image was replaced: drop cached contexts and rebuild every
    material that uses that texture (directly or as lightmap / probe).
    """
    _game_touch(None)
    if SUSPEND[0] or scene is None:
        return
    for k in list(_contexts):
        if k[0] == scene.name:
            del _contexts[k]
    for m in bpy.data.materials:
        if m.get('c2_world') != scene.c2.world_name or m.c2.variant_of is not None:
            continue
        uses = any(t.texture == index for t in m.c2.textures) or \
            any(v.c2.lightmap == index or v.c2.envmap_inst == index for v in variants_of(m))
        if uses:
            rebuild_material(scene, m)


# ----------------------------------------------------------------------
# keep the engine values in step with the scene
# ----------------------------------------------------------------------

@bpy.app.handlers.persistent
def _depsgraph_update(scene, depsgraph):
    """Handler: re-sync engine globals when a light moves or a light's
    settings change.
    """
    if SUSPEND[0] or scene.c2.platform != 'ARCADE' or not engine_active(scene):
        return
    for u in depsgraph.updates:
        idb = u.id
        if isinstance(idb, bpy.types.Object) and idb.type == 'LIGHT' and u.is_updated_transform:
            sync_engine(scene)
            return
        if isinstance(idb, bpy.types.Light):
            sync_engine(scene)
            return


@bpy.app.handlers.persistent
def _frame_change(scene, depsgraph=None):
    """g_Time (scrolling UVs, water) follows the timeline."""
    if scene.c2.platform == 'ARCADE':
        set_engine('g_Time', scene.frame_current * scene.render.fps_base / max(1, scene.render.fps))


_animated = {'key': None, 'mats': [], 'last': {}}     # keyed materials and their last values


@bpy.app.handlers.persistent
def _frame_change_post(scene, depsgraph=None):
    """Materials with keyed parameters (a previewed character's eyes): the
    renderer rebuilds their per-draw blocks for the new values."""
    if scene.c2.platform != 'ARCADE':
        return
    from . import gamerender
    key = len(bpy.data.materials)
    if _animated['key'] != key:      # scanning every material each frame is wasted work
        _animated.update(key=key, mats=[m for m in bpy.data.materials if m.get('c2_animated')], last={})
    last = _animated['last']
    try:
        for m in _animated['mats']:
            if m.animation_data is None:
                continue
            # only when a keyed value really changed: a touched material makes the
            # renderer rebuild the per-draw blocks of every part using it
            sig = tuple(tuple(p.fvalue) for p in m.c2.props)
            if last.get(m.name) != sig:
                last[m.name] = sig
                gamerender.touch_material(m)
    except ReferenceError:              # undo re-created the materials
        _animated['key'] = None


def reset_after_undo():
    """Drop every cached reference to Blender data (contexts with their images,
    node slots, keyed-material lists; the renderer's commands): undo / redo
    re-create the data-blocks they changed, so anything held from before
    points at freed memory."""
    from . import gamerender
    _contexts.clear()
    _owner_contexts.clear()
    _engine_textures.clear()
    _slots.clear()
    _variants['key'] = None
    _animated['key'] = None
    gamerender.reset_after_undo()


@bpy.app.handlers.persistent
def _undo_post(*_args):
    reset_after_undo()


@bpy.app.handlers.persistent
def _load_post(*_args):
    """Handler after a .blend loads: clear caches, upgrade an old engine group,
    and refill the engine image (its pixels are not saved with the file).
    """
    from . import gamerender
    gamerender.RENDERER.clear()
    ENGINE_VALUES.clear()
    _contexts.clear()
    _owner_contexts.clear()
    _slots.clear()
    upgrade_engine_group()
    g = bpy.data.node_groups.get(ENGINE)
    if g is not None and bpy.data.images.get(ENGINE_IMAGE) is not None:
        # the image's pixels aren't saved with the file: refill from the group
        img = engine_image()
        _upload(img, list(g['c2_values']))


_HANDLERS = ((bpy.app.handlers.depsgraph_update_post, _depsgraph_update),
             (bpy.app.handlers.frame_change_pre, _frame_change),
             (bpy.app.handlers.frame_change_post, _frame_change_post),
             (bpy.app.handlers.load_post, _load_post),
             (bpy.app.handlers.undo_post, _undo_post),
             (bpy.app.handlers.redo_post, _undo_post))


def register():
    """Install the handlers."""
    for lst, fn in _HANDLERS:
        if fn not in lst:
            lst.append(fn)


def unregister():
    """Remove the handlers."""
    for lst, fn in _HANDLERS:
        if fn in lst:
            lst.remove(fn)
