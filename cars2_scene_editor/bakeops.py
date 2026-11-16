"""Baked lighting tools.

* Vertex lighting: PC ColorPrelight (light x Prelight Scale) or Arcade
  VertexBaked (RGBM, gamma 2.2), Cycles-baked from the Blender lights.
* Arcade lightmaps: Cycles-baked into the lightMap1 textures (RGBM, squared)
  through lightmapUV; existing atlases are re-baked in place.
* Switching Arcade parts between lightmaps and vertex lighting (_lmh <-> _vb).
* Arcade volume probes: DirectSH grids baked from panoramas of the scene's
  game look, or filled from the light rig.

How a bake works here: BakeEnv switches every material to "albedo only"
(white texture colour, so only light is measured), turns sky domes and
helper meshes invisible to rays, and runs Cycles' Diffuse bake with direct +
indirect light and no colour. The result is linear light (1.0 = the texture
shown at full brightness), which is then encoded in the game's format.
"""
import math
import os
import tempfile

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from mathutils import Matrix, Vector

import arcade
import dds
import shlight
from . import common, matnodes

TMP_ATTR = 'C2_BakeTmp'             # temporary float colour layer a vertex bake writes into
TARGET_NODE = 'C2_BakeTarget'       # temporary image node a lightmap bake writes into


def _root(context):
    """The active world root."""
    return context.scene.c2.root


def _arcade(context):
    """True for an Arcade world."""
    return context.scene.c2.platform == 'ARCADE'


def scene_images(scene):
    """TexturePool index -> bpy image (the extracted .dds files)."""
    out = {}
    for t in scene.c2.textures:
        if t.file and t.ttype == '2d' and os.path.exists(bpy.path.abspath(t.file)):
            try:
                out[t.index] = bpy.data.images.load(bpy.path.abspath(t.file), check_existing=True)
            except RuntimeError:
                pass
    return out


def baked_dir(scene, sub):
    """Folder for baked files: <source folder>/<world>.oct.textures/<sub> (created)."""
    d = os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)),
                     '%s.oct.textures' % scene.c2.world_name, sub)
    os.makedirs(d, exist_ok=True)
    return d


def register_lightmap_texture(scene, path, name):
    """A baked lightmap .dds as a world texture (exported as a new TexturePool
    entry; Game Look reads it through the material's lightmap index)."""
    from . import fxnodes
    for t in scene.c2.textures:
        if t.file and bpy.path.abspath(t.file) == bpy.path.abspath(path):
            return t.index
    fxnodes.SUSPEND[0] += 1
    try:
        t = scene.c2.textures.add()
        t.index = max((x.index for x in scene.c2.textures), default=-1) + 1
        t.name = name
        t.ttype = '2d'
        t.file = path
        t.source_path = 'world_data\\%s\\lightmaps\\%s.hdr' % (scene.c2.world_name, name)
        t.info = 'lightmap (baked)'
        t.srgb = False
    finally:
        fxnodes.SUSPEND[0] -= 1
    return t.index


def neutral_lightmap_texture(scene):
    """World texture of light 1.0 everywhere: what lightmapped parts show until
    they are baked (the world's 'white' texture would read as 8x overbright)."""
    name = '%s_neutral_lightmap' % scene.c2.world_name
    for t in scene.c2.textures:
        if t.name == name:
            return t.index
    path = os.path.join(baked_dir(scene, 'baked_lightmaps'), name + '.dds')
    if not os.path.exists(path):
        with open(path, 'wb') as f:
            f.write(arcade.neutral_lightmap_dds())
    return register_lightmap_texture(scene, path, name)


def set_lightmap_slot(scene, mat, lightmapped):
    """Give a new material a neutral lightMap1 slot (or drop it)."""
    from . import fxnodes
    fxnodes.SUSPEND[0] += 1
    try:
        slots = [i for i, t in enumerate(mat.c2.textures) if t.usage == 'lightMap1']
        if lightmapped:
            idx = neutral_lightmap_texture(scene)
            if slots:
                mat.c2.textures[slots[0]].texture = idx
            else:
                t = mat.c2.textures.add()
                t.usage, t.extra, t.texture = 'lightMap1', True, idx
        else:
            for i in reversed(slots):
                mat.c2.textures.remove(i)
    finally:
        fxnodes.SUSPEND[0] -= 1


_world_level = {}       # source .oct path -> cached world_lightmap_level


def world_lightmap_level(scene):
    """90th percentile of the light in the world's own lightmaps (radiator_a:
    0.6 - median 0.2, 99% 1.1; the encoding clips at 2.0)."""
    key = scene.c2.oct_path
    if key in _world_level:
        return _world_level[key]
    vals = []
    for t in scene.c2.textures:
        if 'lightmaps' not in (t.source_path or '').lower() or 'baked' in (t.info or ''):
            continue
        p = bpy.path.abspath(t.file) if t.file else ''
        if not p.lower().endswith('.dds') or not os.path.exists(p):
            continue
        try:
            with open(p, 'rb') as f:
                l = arcade.rgbm_decode(arcade.dds_rgba(f.read()), arcade.LIGHTMAP_POWER).mean(-1).ravel()
        except (ValueError, OSError):
            continue
        vals.append(l[::max(1, len(l) // 4096)])
    level = float(np.percentile(np.concatenate(vals), 90)) if vals else 0.6
    _world_level[key] = max(0.2, min(level, 1.5))
    return _world_level[key]


def ambient_sum(root):
    """Summed colour x intensity of the world's Ambient lights (used as the
    bake's sky colour with 'Game Ambient Lights As Sky').
    """
    amb = [0.0, 0.0, 0.0]
    for o in common.descendants(root) if root else []:
        if o.type == 'LIGHT' and o.data.c2.light_type == 'Ambient' and not o.hide_render:
            col = o.data.color
            div = 255.0 if max(col) > 1.5 else 1.0      # colours stored 0..255
            for k in range(3):
                amb[k] += col[k] / div * o.data.c2.intensity
    return amb


# object ray-visibility switches turned off for meshes that must not block light
RAY_ATTRS = ('visible_shadow', 'visible_diffuse', 'visible_glossy', 'visible_transmission', 'visible_volume_scatter')


def blocks_nothing(o):
    """Meshes that must not cast light or shadow into a bake: sky domes and
    geometry the game never draws."""
    if o.get('c2_helper') or o.c2.kind in ('COLLISION', 'COLSHAPE', 'NAVMESH', 'PREVIEW'):
        return True
    for s in o.material_slots:
        m = s.material
        if m is not None and (m.c2.variant_of or m).c2.effect.startswith('sky'):
            return True
    return 'sky' in o.name.lower() and not o.select_get()


class BakeEnv:
    """Cycles + albedo-only materials (+ the game's ambient lights as the sky)
    for the duration of a bake; everything is restored afterwards."""

    def __init__(self, context, samples, ambient_sky, mode='ALBEDO'):
        """samples: Cycles samples; ambient_sky: light the bake with the ambient
        lights' colour as the world; mode: viewport material mode during the bake
        ('ALBEDO' for lighting bakes, 'GAME' for probe panoramas).
        """
        self.context = context
        self.samples = samples
        self.ambient_sky = ambient_sky
        self.mode = mode

    def __enter__(self):
        """Switch to Cycles and the bake materials; remember everything changed."""
        scene = self.context.scene
        self.prev = (scene.render.engine, scene.cycles.samples, scene.world, matnodes.get_lighting_mode())
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = self.samples
        matnodes.set_lighting_mode(self.mode)
        # game shaders: no inspect colours, and raw HDR light (not tone mapped) for probes
        from . import fxnodes
        fxnodes.set_engine('c2Inspect', 0.0)
        fxnodes.set_engine('c2Raw', 1.0 if self.mode == 'GAME' else 0.0)
        # some worlds store light colours 0..255 (radiator_a's sunLight is 216,191,141):
        # read as 0..1 for the bake so one light doesn't swamp the rest
        self.light_cols = []
        for ld in bpy.data.lights:
            if max(ld.color) > 1.5:
                self.light_cols.append((ld, tuple(ld.color)))
                ld.color = tuple(c / 255.0 for c in ld.color)
        # sky domes (and helpers the game never draws) must not block light: in
        # Cycles a skybox is a closed shell that leaves everything inside in shadow
        self.ray_vis = []
        for o in scene.objects:
            if o.type == 'MESH' and blocks_nothing(o):
                self.ray_vis.append((o, [getattr(o, a) for a in RAY_ATTRS]))
                for a in RAY_ATTRS:
                    setattr(o, a, False)
        self.tmp_world = None
        if self.ambient_sky or self.mode == 'GAME':
            w = bpy.data.worlds.new('C2_BakeSky')
            w.use_nodes = True
            bg = w.node_tree.nodes.get('Background')
            # game look: the skydome geometry is the sky
            sky = ambient_sum(_root(self.context)) if self.mode != 'GAME' else (0.0, 0.0, 0.0)
            bg.inputs['Color'].default_value = (*sky, 1.0)
            bg.inputs['Strength'].default_value = 1.0
            scene.world = w
            self.tmp_world = w
        return self

    def __exit__(self, *exc):
        """Put back the render engine, samples, world, light colours and ray visibility."""
        scene = self.context.scene
        scene.render.engine, scene.cycles.samples, scene.world, mode = self.prev
        matnodes.set_lighting_mode(mode)
        from . import fxnodes
        fxnodes.set_engine('c2Raw', 0.0)
        fxnodes.set_inspect(scene.c2.inspect)
        for ld, col in self.light_cols:
            ld.color = col
        for o, vals in self.ray_vis:
            for a, v in zip(RAY_ATTRS, vals):
                setattr(o, a, v)
        if self.tmp_world is not None:
            bpy.data.worlds.remove(self.tmp_world)
        return False


def _select_only(context, objs):
    """Select exactly `objs` and make the first one active (bake operators need it)."""
    for o in context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    context.view_layer.objects.active = objs[0]


# ----------------------------------------------------------------------
# vertex lighting
# ----------------------------------------------------------------------

def vertex_layer_name(context):
    """Name of the baked vertex-light layer for this build."""
    return 'VertexBaked' if _arcade(context) else 'ColorPrelight'


def vertex_layer(me, name):
    """The mesh's colour layer `name`, created with neutral values if missing."""
    ca = me.color_attributes.get(name)
    if ca is None:
        import vertexfmt
        ca = me.color_attributes.new(name, 'BYTE_COLOR', 'POINT')
        ca.data.foreach_set('color_srgb', np.tile(np.asarray(vertexfmt.default_values(name, 1)[0], np.float32),
                                                  len(ca.data)))
    return ca


def read_vertex_light(ca, arcade_world, prelight_scale):
    """-> (n, 3) linear light of a vertex-lighting layer."""
    a = np.empty(len(ca.data) * 4, np.float32)
    ca.data.foreach_get('color_srgb', a)
    a = a.reshape(-1, 4)
    if arcade_world:
        return arcade.rgbm_decode(a, arcade.VERTEX_BAKED_POWER)
    return a[:, :3] / max(prelight_scale, 1e-6)


def write_vertex_light(ca, light, arcade_world, prelight_scale):
    """Store linear light (n, 3) in a vertex-lighting layer: RGBM on Arcade,
    light x Prelight Scale on PC.
    """
    light = np.asarray(light, np.float64)
    if arcade_world:
        rgba = arcade.rgbm_encode(light, arcade.VERTEX_BAKED_POWER)
    else:
        rgba = np.concatenate([np.clip(light * prelight_scale, 0, 1), np.ones((len(light), 1))], 1)
    ca.data.foreach_set('color_srgb', rgba.astype(np.float32).ravel())  # raw bytes


class C2_OT_bake_vertex_lighting(bpy.types.Operator):
    """Bake Blender lighting into baked vertex colours."""
    bl_idname = 'c2.bake_prelight'
    bl_label = 'Bake Vertex Lighting'
    bl_description = ('Cycles-bake the Blender lighting (direct + indirect diffuse, no albedo) of the selected '
                      'meshes into their baked vertex colours: ColorPrelight on PC, VertexBaked (RGBM) on Arcade')
    bl_options = {'REGISTER', 'UNDO'}
    samples: IntProperty(name='Samples', default=64, min=1)
    ambient_sky: BoolProperty(name='Game Ambient Lights As Sky', default=True,
                              description='Light the bake with a world colour equal to the sum of the '
                                          'scene\'s Cars 2 Ambient lights')
    include_ao: BoolProperty(name='Multiply Ambient Occlusion', default=False)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not meshes:
            self.report({'ERROR'}, 'Select meshes to bake')
            return {'CANCELLED'}
        arc = _arcade(context)
        name = vertex_layer_name(context)
        scale = context.scene.c2.prelight_scale
        if arc:
            wrong = [o.name for o in meshes if not any(
                arcade.baked_mode((s.material.c2.variant_of or s.material).c2.effect) == 'VERTEX'
                for s in o.material_slots if s.material)]
            if wrong:
                self.report({'WARNING'}, '%d mesh(es) have no _vb (vertex-lit) material, so the game ignores '
                                         'their VertexBaked colours: %s (Switch Baked Lighting first)'
                            % (len(wrong), ', '.join(wrong[:5])))
        try:
            with BakeEnv(context, self.samples, self.ambient_sky):
                _select_only(context, meshes)
                # Cycles bakes into the active colour layer: give each mesh a
                # temporary float layer, read it back, then remove it
                for o in meshes:
                    me = o.data
                    tmp = me.color_attributes.get(TMP_ATTR) or me.color_attributes.new(TMP_ATTR, 'FLOAT_COLOR', 'POINT')
                    me.color_attributes.active_color = tmp
                bpy.ops.object.bake(type='DIFFUSE', pass_filter={'DIRECT', 'INDIRECT'}, target='VERTEX_COLORS')
                light = {}
                for o in meshes:
                    tmp = o.data.color_attributes[TMP_ATTR]
                    a = np.empty(len(tmp.data) * 4, np.float32)
                    tmp.data.foreach_get('color', a)
                    light[o.name] = a.reshape(-1, 4)[:, :3].astype(np.float64)
                if self.include_ao:
                    bpy.ops.object.bake(type='AO', target='VERTEX_COLORS')
                    for o in meshes:
                        tmp = o.data.color_attributes[TMP_ATTR]
                        a = np.empty(len(tmp.data) * 4, np.float32)
                        tmp.data.foreach_get('color', a)
                        light[o.name] *= a.reshape(-1, 4)[:, :1]
        except RuntimeError as e:
            self.report({'ERROR'}, 'Bake failed: %s' % e)
            return {'CANCELLED'}
        finally:
            for o in meshes:
                me = o.data
                tmp = me.color_attributes.get(TMP_ATTR)
                if tmp is not None:
                    me.color_attributes.remove(tmp)
        for o in meshes:
            ca = vertex_layer(o.data, name)
            write_vertex_light(ca, light[o.name], arc, scale)
            o.data.color_attributes.active_color = ca
        self.report({'INFO'}, 'Baked %s on %d mesh(es)' % (name, len(meshes)))
        return {'FINISHED'}


class C2_OT_vertex_light_fill(bpy.types.Operator):
    """Set / multiply / add baked vertex lighting by hand."""
    bl_idname = 'c2.prelight_fill'
    bl_label = 'Fill Vertex Lighting'
    bl_description = ('Set, multiply or add to the baked vertex lighting of the selected meshes '
                      '(ColorPrelight on PC, VertexBaked on Arcade), in linear light units')
    bl_options = {'REGISTER', 'UNDO'}
    color: FloatVectorProperty(name='Light', subtype='COLOR', size=3, default=(1.0, 1.0, 1.0), min=0, soft_max=1)
    strength: FloatProperty(name='Strength', default=1.0, min=0.0, soft_max=8.0)
    mode: EnumProperty(items=[('SET', 'Set', ''), ('MULTIPLY', 'Multiply', ''), ('ADD', 'Add', '')], default='SET')
    selected_only: BoolProperty(name='Selected Vertices Only', default=False,
                                description='Only vertices selected in Edit Mode')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        arc = _arcade(context)
        name = vertex_layer_name(context)
        scale = context.scene.c2.prelight_scale
        val = np.asarray(self.color, np.float64) * self.strength
        n = 0
        for o in context.selected_objects:
            if o.type != 'MESH':
                continue
            if o.mode == 'EDIT':
                o.update_from_editmode()
            ca = vertex_layer(o.data, name)
            light = read_vertex_light(ca, arc, scale)
            sel = np.ones(len(light), bool)
            if self.selected_only and ca.domain == 'POINT':
                sel = np.empty(len(o.data.vertices), bool)
                o.data.vertices.foreach_get('select', sel)
            if self.mode == 'SET':
                light[sel] = val
            elif self.mode == 'MULTIPLY':
                light[sel] *= val
            else:
                light[sel] += val
            write_vertex_light(ca, light, arc, scale)
            o.data.color_attributes.active_color = ca
            o.data.update()
            n += 1
        self.report({'INFO'}, '%s changed on %d mesh(es)' % (name, n))
        return {'FINISHED'}


# ----------------------------------------------------------------------
# lightmaps (Arcade)
# ----------------------------------------------------------------------

def ensure_lightmap_uv(context, obj):
    """Give the mesh a lightmapUV layer (Lightmap Pack) if it has none."""
    me = obj.data
    if 'lightmapUV' in me.uv_layers:
        return False
    keep = me.uv_layers.active.name if me.uv_layers.active else None
    uv = me.uv_layers.new(name='lightmapUV')
    me.uv_layers.active = uv
    _select_only(context, [obj])
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.lightmap_pack(PREF_CONTEXT='ALL_FACES', PREF_PACK_IN_ONE=True, PREF_NEW_UVLAYER=False,
                             PREF_MARGIN_DIV=0.3)
    bpy.ops.object.mode_set(mode='OBJECT')
    if keep:
        me.uv_layers.active = me.uv_layers[keep]
    return True


def lightmap_target(mat):
    """('tex', TexturePool index) / ('file', baked .dds) / None for a material."""
    if mat is None:
        return None
    base = mat.c2.variant_of or mat
    if arcade.baked_mode(base.c2.effect) != 'LIGHTMAP':
        return None
    if mat.get('c2_new_lightmap'):
        return ('file', bpy.path.abspath(mat['c2_new_lightmap']))
    if mat.c2.lightmap >= 0:
        return ('tex', mat.c2.lightmap)
    return ('new', mat.name)


def _decode_lightmap_file(path):
    """RGBM lightmap .dds -> (h, w, 3) linear light."""
    with open(path, 'rb') as f:
        return arcade.rgbm_decode(arcade.dds_rgba(f.read()), arcade.LIGHTMAP_POWER)


def _float_image(name, light):
    """A float Blender image holding `light` (the bake target, seeded with the
    old lightmap so shared atlases keep other meshes' light).
    """
    h, w = light.shape[:2]
    img = bpy.data.images.new(name, w, h, alpha=False, float_buffer=True)
    px = np.ones((h, w, 4), np.float32)
    px[..., :3] = light
    img.pixels.foreach_set(px[::-1].ravel())  # Blender rows are bottom-up
    return img


class C2_OT_bake_lightmaps(bpy.types.Operator):
    """Bake Blender lighting into the Arcade lightmaps."""
    bl_idname = 'c2.bake_lightmaps'
    bl_label = 'Bake Lightmaps'
    bl_description = ('Arcade: Cycles-bake the Blender lighting (direct + indirect, no albedo) of the selected '
                      'meshes into their lightmaps through lightmapUV. Shared lightmap atlases are re-baked in '
                      'place (only the selected meshes\' areas change); lightmapped parts without a lightmap get '
                      'a new one')
    bl_options = {'REGISTER', 'UNDO'}
    samples: IntProperty(name='Samples', default=128, min=1)
    margin: IntProperty(name='Margin (px)', default=4, min=0, max=64)
    ambient_sky: BoolProperty(name='Game Ambient Lights As Sky', default=True)
    match_world: BoolProperty(name='Match World Brightness', default=True,
                              description='Scale the bake so its bright areas sit where the world\'s own lightmaps '
                                          'do (the game reads 1.0 as full brightness and clips at 2.0: a strong '
                                          'Blender sun would otherwise bake white)')
    brightness: FloatProperty(name='Brightness', default=1.0, min=0.0, soft_max=4.0,
                              description='Extra multiplier on the baked light')
    new_size: EnumProperty(name='New Lightmap Size', items=[(str(s), str(s), '') for s in (128, 256, 512, 1024, 2048)],
                           default='512')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    @classmethod
    def poll(cls, context):
        return context.scene.c2.platform == 'ARCADE'

    def execute(self, context):
        scene = context.scene
        meshes = [o for o in context.selected_objects if o.type == 'MESH' and o.c2.kind in ('GEOMETRY', 'NONE')]
        meshes = [o for o in meshes if any(lightmap_target(s.material) for s in o.material_slots)]
        if not meshes:
            self.report({'ERROR'}, 'Select meshes with lightmapped (_lmh) materials '
                                   '(Switch Baked Lighting turns vertex-lit parts into lightmapped ones)')
            return {'CANCELLED'}
        # 1) one bake image per lightmap the selected meshes use. A lightmap
        #    is an existing world texture ('tex'), an earlier bake ('file')
        #    or none yet ('new'). Existing ones are decoded into the image
        #    first, so areas of a shared atlas this bake doesn't touch keep
        #    their light.
        out_dir = baked_dir(scene, 'baked_lightmaps')
        tex_by_index = {t.index: t for t in scene.c2.textures}
        images = {}       # target key -> bpy float image
        seeds = {}        # target key -> the light the image started with
        files = {}        # target key -> output .dds path
        size = int(self.new_size)
        created = []
        for o in meshes:
            ensure_lightmap_uv(context, o)
            for slot in o.material_slots:
                key = lightmap_target(slot.material)
                if key is not None and key[0] == 'new':
                    # each object packs its own lightmapUV, so each gets its own lightmap
                    m = slot.material
                    if not (m.c2.variant_of is not None and m.users == 1):
                        v = m.copy()
                        v.c2.variant_of = m.c2.variant_of or m
                        v.name = '%s|%s' % ((m.c2.variant_of or m).name, o.name)
                        slot.material = v
                    key = lightmap_target(slot.material)
                if key is None or key in images:
                    continue
                if key[0] == 'tex':
                    t = tex_by_index.get(key[1])
                    src = bpy.path.abspath(t.replace) if t and t.replace and t.replace.lower().endswith('.dds') \
                        else (bpy.path.abspath(t.file) if t else '')
                    light = _decode_lightmap_file(src) if src and os.path.exists(src) else np.ones((size, size, 3))
                    name = t.name if t else 'lightmap%d' % key[1]
                elif key[0] == 'file':
                    light = _decode_lightmap_file(key[1]) if os.path.exists(key[1]) else np.ones((size, size, 3))
                    name = os.path.splitext(os.path.basename(key[1]))[0]
                else:
                    light = np.ones((size, size, 3))
                    name = bpy.path.clean_name('%s_%s_lm' % (o.name, slot.material.name.split('|')[0]))
                images[key] = _float_image('C2Bake_' + name, light)
                seeds[key] = np.asarray(light, np.float32)[..., :3] * np.ones(3, np.float32)
                files[key] = os.path.join(out_dir, name + '.dds')
        # 2) Cycles bakes into each material's *active image node* through
        #    the active render UV map: add a temporary target node per
        #    material (materials without a lightmap get a throwaway image) and
        #    make lightmapUV the render UV map for the duration
        dummy = bpy.data.images.new('C2Bake_unused', 32, 32, float_buffer=True)
        touched = []
        uv_state = {}
        try:
            for o in meshes:
                me = o.data
                uv_state[o.name] = (me.uv_layers.active.name if me.uv_layers.active else None,
                                    next((u.name for u in me.uv_layers if u.active_render), None))
                me.uv_layers.active = me.uv_layers['lightmapUV']
                me.uv_layers['lightmapUV'].active_render = True
                for slot in o.material_slots:
                    m = slot.material
                    if m is None or not m.use_nodes or m in touched:
                        continue
                    key = lightmap_target(m)
                    nd = m.node_tree.nodes.new('ShaderNodeTexImage')
                    nd.name = TARGET_NODE
                    nd.image = images[key] if key else dummy
                    m.node_tree.nodes.active = nd
                    touched.append(m)
            with BakeEnv(context, self.samples, self.ambient_sky):
                _select_only(context, meshes)
                bpy.ops.object.bake(type='DIFFUSE', pass_filter={'DIRECT', 'INDIRECT'}, target='IMAGE_TEXTURES',
                                    margin=self.margin, use_clear=False)
        except RuntimeError as e:
            self.report({'ERROR'}, 'Bake failed: %s' % e)
            return {'CANCELLED'}
        finally:
            for m in touched:
                nd = m.node_tree.nodes.get(TARGET_NODE)
                if nd is not None:
                    m.node_tree.nodes.remove(nd)
            for o in meshes:
                act, rnd = uv_state.get(o.name, (None, None))
                if act and act in o.data.uv_layers:
                    o.data.uv_layers.active = o.data.uv_layers[act]
                if rnd and rnd in o.data.uv_layers:
                    o.data.uv_layers[rnd].active_render = True
            bpy.data.images.remove(dummy)
        # 3) encode, write, hook up
        lights, changed = {}, {}
        for key, img in images.items():
            w, h = img.size
            px = np.empty(w * h * 4, np.float32)
            img.pixels.foreach_get(px)
            lights[key] = px.reshape(h, w, 4)[::-1, :, :3].copy()
            seed = seeds.get(key)
            # only the texels this bake wrote (shared atlases keep everyone else's light)
            changed[key] = np.abs(lights[key] - seed).max(-1) > 1e-6 if seed is not None and                 seed.shape == lights[key].shape else np.ones(lights[key].shape[:2], bool)
        # Blender light units don't match the game's: scale the new light so
        # its 90th percentile matches the world's own lightmaps (otherwise a
        # strong sun bakes past the 2.0 clip and reads white in game)
        scale = self.brightness
        if self.match_world and lights:
            lum = np.concatenate([l.mean(-1)[changed[k]] for k, l in lights.items()])
            lum = lum[lum > 1e-4]                      # texels the meshes cover
            if len(lum):
                p90 = float(np.percentile(lum, 90))
                target = world_lightmap_level(scene)
                if p90 > 1e-6:
                    scale *= target / p90
                self.report({'INFO'}, 'Bake scaled x%.2f to match the world lightmaps (90%% level %.2f)'
                            % (scale, target))
        # write each lightmap as an RGBM .dds (pixels written directly:
        # image.copy()/save of a generated image would come out black)
        new_imgs = {}
        for key, img in images.items():
            light = lights[key]
            light[changed[key]] *= scale
            data = dds.write_2d(arcade.rgbm_encode(light, arcade.LIGHTMAP_POWER).astype(np.float32))
            with open(files[key], 'wb') as f:
                f.write(data)
            bpy.data.images.remove(img)
            view = bpy.data.images.load(files[key], check_existing=False)
            view.colorspace_settings.name = 'Non-Color'
            view.alpha_mode = 'CHANNEL_PACKED'
            new_imgs[key] = view
            if key[0] == 'tex' and key[1] in tex_by_index:
                tex_by_index[key[1]].replace = files[key]
        from . import fxnodes
        game = fxnodes.game_context(scene)
        rebuild = {}
        for m in bpy.data.materials:
            key = lightmap_target(m)
            if key is None:
                continue
            if key[0] == 'new' and key in new_imgs:
                # a new lightmap becomes a world texture the material points at
                idx = register_lightmap_texture(scene, files[key], os.path.splitext(os.path.basename(files[key]))[0])
                fxnodes.SUSPEND[0] += 1
                try:
                    m.c2.lightmap = idx
                finally:
                    fxnodes.SUSPEND[0] -= 1
                created.append(m.name)
                new_imgs[('tex', idx)] = new_imgs[key]
                key = ('tex', idx)
            if key in new_imgs and m.node_tree:
                base = m.c2.variant_of or m
                if game is not None and base.get('c2_platform') == 'ARCADE':
                    rebuild[base.name] = base       # Game Look: rebuilt with the new lightmap below
                    continue
                nd = m.node_tree.nodes.get('C2_Lightmap')
                if nd is not None:
                    nd.image = new_imgs[key]
                else:
                    matnodes.build_arcade(m, scene_images(scene), src=m.c2.variant_of, lightmap=new_imgs[key])
        if rebuild:
            for k in list(fxnodes._contexts):
                if k[0] == scene.name:
                    del fxnodes._contexts[k]
            for base in rebuild.values():
                fxnodes.rebuild_material(scene, base)
        self.report({'INFO'}, 'Baked %d lightmap(s) into %s%s' % (
            len(images), out_dir, (' (%d new)' % len(created)) if created else ''))
        return {'FINISHED'}


class C2_OT_switch_baked_lighting(bpy.types.Operator):
    """Switch materials between lightmap (_lmh) and vertex (_vb) lighting."""
    bl_idname = 'c2.switch_baked_lighting'
    bl_label = 'Switch Baked Lighting'
    bl_description = ('Arcade: switch the selected meshes\' materials between lightmaps (_lmh effects, lightmapUV) '
                      'and vertex lighting (_vb effects, VertexBaked). New game materials are made on export; '
                      'bake afterwards')
    bl_options = {'REGISTER', 'UNDO'}
    mode: EnumProperty(name='To', items=[('LIGHTMAP', 'Lightmap', 'Per-texel baked lighting (lightmapUV)'),
                                         ('VERTEX', 'Vertex Colours', 'Per-vertex baked lighting (VertexBaked)')],
                       default='LIGHTMAP')

    @classmethod
    def poll(cls, context):
        return context.scene.c2.platform == 'ARCADE'

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        assets = common.game_assets(scene)
        if not arcade.effects_dir(assets):
            self.report({'ERROR'}, 'Set the Game Assets Folder (the effects folder is needed to check the shaders)')
            return {'CANCELLED'}
        images = None
        made, missing, n = {}, set(), 0
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        for o in objs:
            changed = False
            for slot in o.material_slots:
                m = slot.material
                if m is None:
                    continue
                base = m.c2.variant_of or m
                eff = base.c2.effect
                if arcade.baked_mode(eff) == self.mode or eff in ('SolidColor', 'shadowonly', 'scene_settings'):
                    continue
                target = arcade.baked_variant(eff, self.mode)
                if not arcade.effect_exists(assets, target):
                    missing.add(target)
                    continue
                # one new material per original (shared by every selected mesh using it)
                if base.name not in made:
                    nm = base.copy()
                    nm.name = '%s_%s' % (base.name, 'lm' if self.mode == 'LIGHTMAP' else 'vb')
                    nm.c2.index = -1
                    # a material that is itself new keeps the game material it came from
                    nm.c2.template = base.c2.index if base.c2.index >= 0 else base.c2.template
                    nm.c2.effect = target
                    nm.c2.variant_of = None
                    nm.c2.lightmap = -1
                    if 'c2_new_lightmap' in nm:
                        del nm['c2_new_lightmap']
                    set_lightmap_slot(scene, nm, self.mode == 'LIGHTMAP')
                    made[base.name] = nm
                slot.material = made[base.name]
                changed = True
            if changed:
                n += 1
                if self.mode == 'LIGHTMAP':
                    ensure_lightmap_uv(context, o)
                else:
                    vertex_layer(o.data, 'VertexBaked')
        from . import fxnodes
        game = fxnodes.game_context(scene)
        for k in list(fxnodes._contexts):
            if k[0] == scene.name:
                del fxnodes._contexts[k]
        for nm in made.values():
            if game is not None and nm.get('c2_platform') == 'ARCADE':
                fxnodes.rebuild_material(scene, nm)
            else:
                images = images if images is not None else scene_images(scene)
                matnodes.build(nm, images)
        if missing:
            self.report({'WARNING'}, 'No such effect in assets/effects, left unchanged: %s' % ', '.join(sorted(missing)))
        self.report({'INFO'}, '%d mesh(es) switched to %s - now bake them' %
                    (n, 'lightmaps' if self.mode == 'LIGHTMAP' else 'vertex lighting'))
        return {'FINISHED'}


# ----------------------------------------------------------------------
# volume probes (Arcade)
# ----------------------------------------------------------------------

def probe_cell_world(obj):
    """World (Blender) positions of a volume probe's cells, DirectSH order.
    The probe object's local unit cube is the volume (scale = extents)."""
    pts = arcade.probe_cell_positions(obj.c2.probe_density)
    return [obj.matrix_world @ Vector(p) for p in pts]


def project_panorama(px, w, h):
    """Equirect radiance (h, w, 3), rows top-down, camera space = game space
    -> 27 SH coefficients (R, G, B x 9), radiance projection."""
    # direction of every pixel, and its solid angle (pixels near the poles
    # cover less of the sphere: weight by cos(latitude))
    v = (np.arange(h) + 0.5) / h
    u = (np.arange(w) + 0.5) / w
    th = math.pi * (0.5 - v)        # +pi/2 at the top row
    ph = 2 * math.pi * (u - 0.5)
    ct, st = np.cos(th)[:, None], np.sin(th)[:, None]
    x = np.sin(ph)[None, :] * ct
    y = np.repeat(st, w, 1)
    z = -np.cos(ph)[None, :] * ct
    dw = (2 * math.pi / w) * (math.pi / h) * np.repeat(ct, w, 1)
    sh = np.zeros((3, 9))
    basis = [np.asarray(b) for b in _basis_grid(x, y, z)]
    for j in range(9):
        wj = basis[j] * dw
        for c in range(3):
            sh[c, j] = (px[..., c] * wj).sum()
    return sh.ravel()


def _basis_grid(x, y, z):
    """SH basis functions evaluated over whole arrays of directions."""
    b = shlight.eval_basis(x, y, z)
    return [np.broadcast_to(np.asarray(v, np.float64), x.shape) for v in b]


class C2_OT_bake_probe_sh(bpy.types.Operator):
    """Bake volume probe lighting from panoramas of the scene."""
    bl_idname = 'c2.bake_probe_sh'
    bl_label = 'Bake Volume Probe Lighting'
    bl_description = ('Arcade: render a small panorama of the scene\'s game look (unlit: textures x baked '
                      'lighting) at every cell of the selected volume probes and store it as their DirectSH grid '
                      '(what cars driving through pick up)')
    bl_options = {'REGISTER', 'UNDO'}
    resolution: IntProperty(name='Panorama Width', default=32, min=8, max=256)
    samples: IntProperty(name='Samples', default=4, min=1)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        root = _root(context)
        probes = [o for o in context.selected_objects if o.c2.kind == 'PROBE' and o.c2.probe_type == 'Volume']
        if not probes:
            self.report({'ERROR'}, 'Select Volume light probes')
            return {'CANCELLED'}
        # a temporary 360-degree camera renders a small panorama at every
        # probe cell; each panorama is projected to 27 SH numbers. The render
        # settings changed for this are restored in `finally`.
        w, h = self.resolution, max(4, self.resolution // 2)
        cam = bpy.data.objects.new('C2ProbeCam', bpy.data.cameras.new('C2ProbeCam'))
        scene.collection.objects.link(cam)
        cam.data.type = 'PANO'
        try:
            cam.data.panorama_type = 'EQUIRECTANGULAR'
        except (AttributeError, TypeError):
            cam.data.cycles.panorama_type = 'EQUIRECTANGULAR'
        r = scene.render
        prev = (scene.camera, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
                r.image_settings.file_format, r.image_settings.color_depth, scene.view_settings.view_transform,
                r.use_persistent_data)
        scene.camera = cam
        r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
        r.image_settings.file_format = 'OPEN_EXR'
        r.image_settings.color_depth = '32'
        scene.view_settings.view_transform = 'Standard'
        r.use_persistent_data = True
        tmp = os.path.join(tempfile.mkdtemp(), 'cell.exr')
        rot = root.matrix_world.to_3x3().to_4x4()  # camera axes = game axes
        total = 0
        try:
            with BakeEnv(context, self.samples, False, mode='GAME'):
                for o in probes:
                    cells = probe_cell_world(o)
                    grid = []
                    for k, p in enumerate(cells):
                        m = rot.copy()
                        m.translation = p
                        cam.matrix_world = m
                        r.filepath = tmp
                        bpy.ops.render.render(write_still=True)
                        img = bpy.data.images.load(tmp)
                        px = np.empty(w * h * 4, np.float32)
                        img.pixels.foreach_get(px)
                        bpy.data.images.remove(img)
                        grid.extend(project_panorama(px.reshape(h, w, 4)[::-1, :, :3], w, h).tolist())
                        total += 1
                    o['c2_direct_sh'] = grid
                    g = np.asarray(grid).reshape(-1, 3, 9)
                    lum = (g[:, :, 0] * [0.2126, 0.7152, 0.0722]).sum(1).mean() * 0.282094791774
                    o.c2.probe_luminance = float(lum)
                    o.c2.probe_has_luminance = True
                    print('Cars 2 Scene: probe %s baked (%d cells)' % (o.name, len(cells)))
        finally:
            (scene.camera, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
             r.image_settings.file_format, r.image_settings.color_depth, scene.view_settings.view_transform,
             r.use_persistent_data) = prev
            data = cam.data
            bpy.data.objects.remove(cam)
            bpy.data.cameras.remove(data)
        self.report({'INFO'}, 'Baked %d cell(s) in %d probe(s)' % (total, len(probes)))
        return {'FINISHED'}


class C2_OT_probe_sh_fill(bpy.types.Operator):
    """Scale a volume probe's lighting or fill it from the light rig."""
    bl_idname = 'c2.probe_sh_fill'
    bl_label = 'Fill / Scale Probe Lighting'
    bl_description = 'Arcade volume probes: scale the DirectSH grid, or fill every cell from the light rig'
    bl_options = {'REGISTER', 'UNDO'}
    mode: EnumProperty(items=[('SCALE', 'Scale', 'Multiply every cell'),
                              ('RIG', 'From Light Rig', 'Every cell = the active light rig x Scale')], default='SCALE')
    scale: FloatProperty(name='Scale', default=1.0, min=0.0, soft_max=4.0)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        sc = context.scene.c2
        probes = [o for o in context.selected_objects if o.c2.kind == 'PROBE' and o.c2.probe_type == 'Volume']
        if not probes:
            self.report({'ERROR'}, 'Select Volume light probes')
            return {'CANCELLED'}
        for o in probes:
            cells = arcade.probe_cells(o.c2.probe_density)
            if self.mode == 'RIG':
                if not (0 <= sc.active_rig < len(sc.light_rigs)):
                    self.report({'ERROR'}, 'No light rig')
                    return {'CANCELLED'}
                one = np.asarray(sc.light_rigs[sc.active_rig].sh, np.float64) * self.scale
                o['c2_direct_sh'] = np.tile(one, cells).tolist()
            else:
                g = o.get('c2_direct_sh')
                if g is None:
                    continue
                o['c2_direct_sh'] = (np.asarray(list(g), np.float64) * self.scale).tolist()
        return {'FINISHED'}


_classes = (C2_OT_bake_vertex_lighting, C2_OT_vertex_light_fill, C2_OT_bake_lightmaps,
            C2_OT_switch_baked_lighting, C2_OT_bake_probe_sh, C2_OT_probe_sh_fill)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
