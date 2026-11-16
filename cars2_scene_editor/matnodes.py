"""Viewport node trees for game materials (an approximation of the shaders).

Every material is   base colour = albedo x mix(1, baked light, Baked)
and, in "Game Look", an emission of albedo x baked light instead of the BSDF.
`Baked` / `Unlit` come from the shared node group C2_LightingMode, so one
switch (Lighting panel > Viewport Lighting) changes every material; the bake
operators flip it to albedo-only while Cycles runs.

PC / Steam:  baked light = 2 x ColorPrelight.
Arcade:      baked light = RGBM lightmap (lightmapUV) or RGBM VertexBaked,
             x ColorOcclusion; texture layers 2-4 blended by ColorBlend
             (see core/arcade.py for the decoding, read from the shaders).

Node names other code relies on: C2_BSDF (Principled), C2_Tint (layer-1
diffuse x diffuseColorA1, input 7 = the colour), C2_Diffuse (layer-1 image).

These simplified trees are the fallback. When the game's effect files are
available, attach_game() also adds the exact game shader (fxnodes "Game
Look") to the same material; the mode switch picks which one shows.
"""
import bpy

import arcade

GROUP = 'C2_LightingMode'
# viewport mode -> (Baked, Unlit) switch values read by every material
MODES = {'BAKED': (1.0, 0.0), 'GAME': (1.0, 1.0), 'ALBEDO': (0.0, 0.0)}


def set_data_image(img):
    """Non-colour, channel-packed. Only written when different: any write tags
    the image, which refreshes every material that uses it."""
    try:
        if img.colorspace_settings.name != 'Non-Color':
            img.colorspace_settings.name = 'Non-Color'
    except TypeError:
        pass
    if img.alpha_mode != 'CHANNEL_PACKED':
        img.alpha_mode = 'CHANNEL_PACKED'


def lighting_group():
    """The shared C2_LightingMode node group (two Value outputs: Baked, Unlit),
    created on first use."""
    g = bpy.data.node_groups.get(GROUP)
    if g is not None:
        return g
    g = bpy.data.node_groups.new(GROUP, 'ShaderNodeTree')
    g.use_fake_user = True
    for name in ('Baked', 'Unlit'):
        g.interface.new_socket(name, in_out='OUTPUT', socket_type='NodeSocketFloat')
    out = g.nodes.new('NodeGroupOutput')
    out.location = (200, 0)
    for k, name in enumerate(('Baked', 'Unlit')):
        v = g.nodes.new('ShaderNodeValue')
        v.name = v.label = name
        v.location = (0, -100 * k)
        v.outputs[0].default_value = MODES['BAKED'][k]
        g.links.new(v.outputs[0], out.inputs[name])
    return g


def set_lighting_mode(mode):
    """Switch every material at once ('BAKED' / 'GAME' / 'ALBEDO')."""
    g = lighting_group()
    for k, name in enumerate(('Baked', 'Unlit')):
        g.nodes[name].outputs[0].default_value = MODES[mode][k]


def get_lighting_mode():
    """The current mode (read back from the group's values)."""
    g = bpy.data.node_groups.get(GROUP)
    if g is None:
        return 'BAKED'
    cur = tuple(g.nodes[n].outputs[0].default_value for n in ('Baked', 'Unlit'))
    return next((k for k, v in MODES.items() if v == cur), 'BAKED')


# ----------------------------------------------------------------------
# small node helpers
# ----------------------------------------------------------------------

class _Tree:
    """Small wrapper to build a material's node tree from scratch. Inputs can
    be sockets (linked) or plain values (set as defaults)."""

    def __init__(self, mat):
        mat.use_nodes = True
        self.nt = mat.node_tree
        self.nt.nodes.clear()

    def node(self, kind, loc, name=None, **props):
        n = self.nt.nodes.new(kind)
        n.location = loc
        if name:
            n.name = n.label = name
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, a, b):
        self.nt.links.new(a, b)

    def mix(self, loc, blend, a=None, b=None, fac=1.0, name=None):
        """Colour Mix node (blend = 'MIX', 'MULTIPLY' ...); inputs 6/7 are A/B."""
        m = self.node('ShaderNodeMix', loc, name, data_type='RGBA', blend_type=blend)
        m.inputs['Factor'].default_value = fac if not hasattr(fac, 'node') else 0.0
        if hasattr(fac, 'node'):
            self.link(fac, m.inputs['Factor'])
        for sock, v in ((6, a), (7, b)):
            if v is None:
                continue
            if hasattr(v, 'node'):
                self.link(v, m.inputs[sock])
            else:
                m.inputs[sock].default_value = (*v, 1.0) if len(v) == 3 else v
        return m

    def math(self, loc, op, a, b=None):
        m = self.node('ShaderNodeMath', loc, operation=op)
        for k, v in enumerate((a, b)):
            if v is None:
                continue
            if hasattr(v, 'node'):
                self.link(v, m.inputs[k])
            else:
                m.inputs[k].default_value = v
        return m.outputs[0]

    def gamma(self, loc, color, g):
        n = self.node('ShaderNodeGamma', loc)
        self.link(color, n.inputs['Color'])
        n.inputs['Gamma'].default_value = g
        return n.outputs[0]

    def scale(self, loc, vec, s):
        n = self.node('ShaderNodeVectorMath', loc, operation='SCALE')
        self.link(vec, n.inputs[0])
        if hasattr(s, 'node'):
            self.link(s, n.inputs['Scale'])
        else:
            n.inputs['Scale'].default_value = s
        return n.outputs[0]

    def attr(self, loc, layer):
        n = self.node('ShaderNodeVertexColor', loc)
        n.layer_name = layer
        return n

    def uv(self, loc, layer, scale=1.0):
        n = self.node('ShaderNodeUVMap', loc)
        n.uv_map = layer
        if abs(scale - 1.0) < 1e-6:
            return n.outputs[0]
        # game uv * s; Blender stores v flipped (1 - v), so scale about v = 1
        m = self.node('ShaderNodeMapping', (loc[0] + 180, loc[1]))
        m.inputs['Scale'].default_value = (scale, scale, 1.0)
        m.inputs['Location'].default_value = (0.0, 1.0 - scale, 0.0)
        self.link(n.outputs[0], m.inputs['Vector'])
        return m.outputs[0]

    def image(self, loc, img, vector=None, name=None, data=False):
        n = self.node('ShaderNodeTexImage', loc, name)
        n.image = img
        if data and img is not None:
            set_data_image(img)
        if vector is not None:
            self.link(vector, n.inputs['Vector'])
        return n


def _props(c):
    """Parameter values of a material (preview values first: fxnodes.PREVIEW_VALUES)."""
    out = _stored_props(c)
    from . import fxnodes
    out.update(fxnodes.PREVIEW_VALUES.get(c.id_data.name, {}))
    return out


def _stored_props(c):
    """A material's parameters as {name: value or tuple}."""
    out = {}
    for p in c.props:
        if p.ptype == 'Float':
            out[p.name] = tuple(p.fvalue[:p.size]) if p.size > 1 else p.fvalue[0]
        else:
            out[p.name] = p.ivalue
    return out


def _finish(t, mat, albedo, light, alpha, effect):
    """albedo/light: colour sockets (light may be None = no baked light).

    Builds the end of every simplified material:
        lit path    Principled BSDF, base = albedo x mix(1, light, Baked)
        unlit path  Emission = albedo x light (with alpha through Transparent)
        C2_Switch   picks between them with the group's Unlit value"""
    g = t.node('ShaderNodeGroup', (-450, -520), 'C2_Mode')
    g.node_tree = lighting_group()
    bsdf = t.node('ShaderNodeBsdfPrincipled', (350, 0), 'C2_BSDF')
    try:
        bsdf.inputs['Roughness'].default_value = 0.8
    except KeyError:
        pass
    if light is not None:
        lit = t.mix((-150, -350), 'MIX', (1.0, 1.0, 1.0), light, fac=g.outputs['Baked'])
        base = t.mix((50, -150), 'MULTIPLY', albedo, lit.outputs[2])
        glow = t.mix((50, -350), 'MULTIPLY', albedo, light)
        base_out, glow_out = base.outputs[2], glow.outputs[2]
    else:
        base_out = glow_out = albedo
    t.link(base_out, bsdf.inputs['Base Color'])
    if alpha is not None:
        t.link(alpha, bsdf.inputs['Alpha'])
        try:
            mat.blend_method = 'HASHED' if '_ak' in effect else 'BLEND'
        except (AttributeError, TypeError):
            pass
    em = t.node('ShaderNodeEmission', (350, -350))
    t.link(glow_out, em.inputs['Color'])
    unlit = em.outputs[0]
    if alpha is not None:
        tr = t.node('ShaderNodeBsdfTransparent', (350, -480))
        m = t.node('ShaderNodeMixShader', (550, -400))
        t.link(alpha, m.inputs[0])
        t.link(tr.outputs[0], m.inputs[1])
        t.link(em.outputs[0], m.inputs[2])
        unlit = m.outputs[0]
    sw = t.node('ShaderNodeMixShader', (750, 0), 'C2_Switch')
    t.link(g.outputs['Unlit'], sw.inputs[0])
    t.link(bsdf.outputs[0], sw.inputs[1])
    t.link(unlit, sw.inputs[2])
    out = t.node('ShaderNodeOutputMaterial', (950, 0))
    t.link(sw.outputs[0], out.inputs[0])
    return bsdf


def _tex_for(c, usage):
    """World texture index in the slot `usage` (None if the material has no such slot)."""
    return next((x.texture for x in c.textures if x.usage == usage), None)


# ----------------------------------------------------------------------
# PC / Steam
# ----------------------------------------------------------------------

def build_pc(mat, images):
    """PC/Steam material: diffuse x tint, lit by 2 x ColorPrelight."""
    t = _Tree(mat)
    c = mat.c2
    p = _props(c)
    color = p.get('diffuseColorA1', p.get('Color', (1.0, 1.0, 1.0)))
    color = tuple(color[:3]) if isinstance(color, tuple) and len(color) >= 3 else (1.0, 1.0, 1.0)
    img = images.get(_tex_for(c, 'diffuseMapA1'))
    alpha = None
    tex = None
    if img is not None:
        tex = t.image((-700, 100), img, name='C2_Diffuse')
        if '_ab' in c.effect or '_ak' in c.effect:
            alpha = tex.outputs['Alpha']
    tint = t.mix((-450, 100), 'MULTIPLY', tex.outputs['Color'] if tex else (1.0, 1.0, 1.0), color, name='C2_Tint')
    pre = t.attr((-700, -250), 'ColorPrelight')
    light = t.mix((-450, -250), 'MULTIPLY', pre.outputs['Color'], (2.0, 2.0, 2.0))
    _finish(t, mat, tint.outputs[2], light.outputs[2], alpha, c.effect)


# ----------------------------------------------------------------------
# Arcade
# ----------------------------------------------------------------------

# texture layer -> ColorBlend channel holding its weight
BLEND_CHANNEL = {2: 'Blue', 3: 'Green', 4: 'Red'}


class GameContext:
    """What the game shaders need: the game's assets folder (effects) and the
    world's textures (fxnodes.Textures)."""

    def __init__(self, assets, textures, local_effects=None):
        self.assets = assets
        self.textures = textures
        self.local_effects = local_effects    # {name: .tfx bytes} of a package


def attach_game(mat, game):
    """Game Look = the effect's own shader (fxnodes); False if unavailable."""
    if game is None or not game.assets:
        return False
    if not bpy.context.scene.c2.eevee_game_shaders:
        return False          # the Cars 2 Game render engine draws the game shaders itself
    from . import fxnodes
    base = mat.c2.variant_of or mat
    plan = fxnodes.plan_for(game.assets, base.c2.effect, game.local_effects)
    return fxnodes.attach(mat, plan, game.textures) if plan is not None else False


def build_arcade(mat, images, src=None, lightmap=None, game=None):
    """src: material whose game properties to use (the primary of a lightmap
    variant); lightmap: bpy image of this instance's lightMap1; game: a
    GameContext to also build the game's own shader for Game Look."""
    t = _Tree(mat)
    c = (src or mat).c2
    p = _props(c)
    effect = c.effect
    split = effect[:2] in ('m2', 'm3', 'm4')
    alpha = None
    # -- albedo: layer 1 + blended layers 2..4 --------------------------------
    acc = None
    blend = None
    y = 300
    for k in range(1, 5):
        img = images.get(_tex_for(c, 'diffuseMapA%d' % k))
        if img is None and k > 1:
            continue
        sc = float(p.get('split%d_scale' % k, 1.0)) if split else 1.0
        tint = p.get('diffuseColorA%d' % k, (1.0, 1.0, 1.0))
        tint = tuple(tint[:3]) if isinstance(tint, tuple) else (tint, tint, tint)
        # a_2_ effects put layer 2 on its own UV set
        vec = t.uv((-1300, y), 'Uv2' if (k == 2 and effect.startswith('a_2')) else 'Uv1', sc)
        tex = t.image((-1000, y), img, vec, name='C2_Diffuse' if k == 1 else 'C2_DiffuseA%d' % k) if img else None
        col = t.mix((-700, y), 'MULTIPLY', tex.outputs['Color'] if tex else (1.0, 1.0, 1.0), tint,
                    name='C2_Tint' if k == 1 else 'C2_TintA%d' % k)
        if k == 1:
            acc = col.outputs[2]
            if tex is not None and ('_ab' in effect or '_ak' in effect or '_apb' in effect):
                alpha = tex.outputs['Alpha']
        else:
            if blend is None:
                cb = t.attr((-1300, -900), 'ColorBlend')
                raw = t.gamma((-1100, -900), cb.outputs['Color'], 1.0 / 2.2)
                blend = t.node('ShaderNodeSeparateColor', (-900, -900)).outputs
                t.link(raw, blend[0].node.inputs[0])
            w = t.math((-700, y - 150), 'MINIMUM', tex.outputs['Alpha'], blend[BLEND_CHANNEL[k]])
            acc = t.mix((-450, y), 'MIX', acc, col.outputs[2], fac=w).outputs[2]
        y -= 300
    # -- normal map (layer 1) ----------------------------------------------------------
    nimg = images.get(_tex_for(c, 'normalMapA1'))
    # -- baked light ------------------------------------------------------------
    mode = arcade.baked_mode(effect)
    light = None
    if mode == 'LIGHTMAP':
        # RGBM decode: light = (rgb * a * 6) ** 2  (see core/arcade.py)
        lm = t.image((-1000, -500), lightmap, t.uv((-1300, -500), 'lightmapUV'), name='C2_Lightmap', data=True)
        m6 = t.math((-800, -650), 'MULTIPLY', lm.outputs['Alpha'], arcade.LIGHTMAP_RANGE)
        light = t.gamma((-450, -550), t.scale((-650, -550), lm.outputs['Color'], m6), arcade.LIGHTMAP_POWER)
        if lightmap is None:
            light = None
    elif mode == 'VERTEX':
        # the same RGBM idea stored in a vertex colour, power 2.2
        vb = t.attr((-1300, -500), 'VertexBaked')
        raw = t.gamma((-1100, -500), vb.outputs['Color'], 1.0 / 2.2)
        m6 = t.math((-1100, -650), 'MULTIPLY', vb.outputs['Alpha'], arcade.LIGHTMAP_RANGE)
        light = t.gamma((-650, -550), t.scale((-850, -550), raw, m6), arcade.VERTEX_BAKED_POWER)
    if light is not None:  # the importer gives every Arcade mesh a ColorOcclusion layer (white if absent)
        occ = t.attr((-1300, -750), 'ColorOcclusion')
        raw = t.gamma((-1100, -750), occ.outputs['Color'], 1.0 / 2.2)
        light = t.mix((-450, -700), 'MULTIPLY', light, raw).outputs[2]
    bsdf = _finish(t, mat, acc, light, alpha, effect)
    if nimg is not None:
        sc = float(p.get('split1_scale', 1.0)) if split else 1.0
        ntex = t.image((-1000, 650), nimg, t.uv((-1300, 650), 'Uv1', sc), name='C2_NormalMap', data=True)
        sep = t.node('ShaderNodeSeparateColor', (-750, 650))
        t.link(ntex.outputs['Color'], sep.inputs[0])
        inv = t.math((-600, 600), 'SUBTRACT', 1.0, sep.outputs['Green'])  # DirectX -> OpenGL green
        comb = t.node('ShaderNodeCombineColor', (-450, 650))
        t.link(sep.outputs['Red'], comb.inputs[0])
        t.link(inv, comb.inputs[1])
        t.link(sep.outputs['Blue'], comb.inputs[2])
        nm = t.node('ShaderNodeNormalMap', (-250, 650))
        nm.uv_map = 'Uv1'
        nm.inputs['Strength'].default_value = float(p.get('normalHeightA1', 1.0))
        t.link(comb.outputs[0], nm.inputs['Color'])
        t.link(nm.outputs[0], bsdf.inputs['Normal'])
    eimg = images.get(_tex_for(c, 'emissiveMapA1'))
    if eimg is not None:
        e = t.image((-1000, 950), eimg, t.uv((-1300, 950), 'Uv1'), name='C2_Emissive')
        t.link(e.outputs['Color'], bsdf.inputs['Emission Color'])
        bsdf.inputs['Emission Strength'].default_value = float(p.get('emissiveMultiplier', 1.0))
    attach_game(mat, game)
    return bsdf


# ----------------------------------------------------------------------
# material sets: base materials + per-instance lightmap variants
# ----------------------------------------------------------------------

class MaterialSet:
    """Blender materials for one .oct. Arcade SubGeometryLit parts carry their
    own lightMap1 (and envMapInstance) textures, so a game material used with
    several lightmaps becomes one Blender material per lightmap ("variants",
    mat.c2.variant_of = the base material). Variants export as the base
    material plus that instance texture."""

    def __init__(self, sc, mats, images, is_arcade, game=None):
        self.sc = sc
        self.mats = mats
        self.images = images
        self.arcade = is_arcade
        self.game = game
        self.variants = {}

    # behaves as the list of base materials (MaterialPool order)
    def __len__(self):
        return len(self.mats)

    def __getitem__(self, i):
        return self.mats[i]

    def __iter__(self):
        return iter(self.mats)

    def key(self, sub_node):
        """(material index, lightmap texture, env map texture) of a part:
        parts with the same key share one Blender material."""
        mi = sub_node.get('MaterialReference', 0)
        if not self.arcade:
            return (mi, -1, -1)
        lm = env = -1
        for te in sub_node.get('InstanceTextureEntries', {}).values():
            if te.get('Usage') == 'lightMap1':
                lm = te.get('TextureReference', -1)
            elif te.get('Usage') == 'envMapInstance':
                env = te.get('TextureReference', -1)
        return (mi, lm, env)

    def material(self, key):
        """The Blender material for a key: the base material, or a variant
        named '<material>|<lightmap>' created on first use."""
        mi, lm, env = key
        base = self.mats[mi] if 0 <= mi < len(self.mats) else None
        if base is None or (lm < 0 and env < 0):
            return base
        if key in self.variants:
            return self.variants[key]
        tex = self.sc.textures[lm].get('Name', 'lm%d' % lm) if 0 <= lm < len(self.sc.textures) else 'env%d' % env
        v = base.copy()
        v.name = '%s|%s' % (base.name, tex)
        v.c2.variant_of = base
        v.c2.lightmap = lm
        v.c2.envmap_inst = env
        build_arcade(v, self.images, src=base, lightmap=self.images.get(lm), game=self.game)
        self.variants[key] = v
        return v


def build(mat, images, game=None):
    """(Re)build the node tree of an imported material; images: TexturePool
    index -> bpy image."""
    if mat.c2.effect in ('scene_settings', 'Error'):
        return
    if mat.get('c2_platform', 'PC') == 'ARCADE':
        build_arcade(mat, images, src=mat.c2.variant_of, lightmap=images.get(mat.c2.lightmap), game=game)
    else:
        build_pc(mat, images)
