"""Bringing a custom scene into a world: clear the old geometry/collision, and
give new meshes the closest Cars 2 material (by name and kind of surface) with
their own textures moved into it.

Custom-scene workflow (Custom Scene box in the sidebar):
    1. Replace World Geometry        delete the shipped meshes + collision
    2. bring in your meshes
    3. Add + Auto-Assign Materials   copy the best-matching game material per
                                     source material, move your images in
    (Import Materials from World     shaders this world lacks, e.g. water)
    (Make Collision                  collision-only meshes, split into pieces)
    (Restore World Materials         world materials lost from the .blend)
"""
import difflib
import math
import os
import re

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

from . import common

# ----------------------------------------------------------------------------
# replace world
# ----------------------------------------------------------------------------


class C2_OT_replace_world(bpy.types.Operator):
    """Clear the imported world's geometry and collision."""
    bl_idname = 'c2.replace_world'
    bl_label = 'Replace World Geometry'
    bl_description = ('Delete the world\'s imported meshes and all collision, ready for a custom scene. '
                      'Walk-in trigger volumes and actor roots (pickups, characters) are kept so the world\'s '
                      'logic still loads. Deleted meshes export hidden and empty (Ctrl+Z undoes this)')
    bl_options = {'REGISTER', 'UNDO'}
    keep_sky: BoolProperty(name='Keep Sky', default=False, description='Keep the sky dome (sky effects)')
    keep_triggers: BoolProperty(name='Keep Trigger Volumes', default=True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, title='Delete the world geometry and collision?')

    def execute(self, context):
        scene = context.scene
        root = scene.c2.root
        if root is None:
            self.report({'ERROR'}, 'Import a Cars 2 world first')
            return {'CANCELLED'}
        from . import ops
        remember_material_users(scene)     # auto-assign still needs to know them afterwards
        geo, col = [], []
        for o in common.descendants(root):
            k = o.c2.kind
            if k in ('COLLISION', 'COLSHAPE'):
                col.append(o)
            elif k == 'GEOMETRY' and o.c2.node_index >= 0:
                if o.c2.detached:                       # actor roots: .ap actors spawn from them
                    continue
                if self.keep_triggers and (o.get('c2_helper') == 'trigger' or ops.is_trigger(scene, o)):
                    continue
                if self.keep_sky and any(s.material and (s.material.c2.variant_of or s.material).c2.effect
                                         .startswith('skyd') for s in o.material_slots):
                    continue
                geo.append(o)
        doomed = geo + col
        names = {o.name for o in doomed}
        # children that stay (locators, lights ...) move up to the root, keeping their place
        for o in common.descendants(root):
            if o.name not in names and o.parent is not None and o.parent.name in names:
                mw = o.matrix_world.copy()
                p = o.parent
                while p is not None and p.name in names:
                    p = p.parent
                o.parent = p or root
                o.matrix_parent_inverse = (p or root).matrix_world.inverted()
                o.matrix_world = mw
        common.remove_ids(doomed)
        self.report({'INFO'}, 'Removed %d meshes and %d collision objects' % (len(geo), len(col)))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# material matching
# ----------------------------------------------------------------------------

# words that say nothing about the surface (ignored when matching names)
STOP = {'mat', 'mtl', 'material', 'materials', 'mesh', 'obj', 'object', 'geo', 'geom', 'shape', 'poly',
        'polysurface', 'lod', 'lod0', 'lod1', 'model', 'default', 'new', 'copy', 'blinn', 'lambert', 'phong',
        'standard', 'surface', 'tex', 'texture', 'diffuse', 'diff', 'color', 'colour', 'albedo', 'base',
        'basecolor', 'png', 'tga', 'dds', 'jpg', 'map', 'c2', 'the', 'and', 'of', 'a'}

# kind of surface -> words that say so
CATEGORIES = {
    'sky': {'sky', 'skybox', 'skydome', 'cloud', 'clouds', 'horizon', 'dome', 'backdrop', 'atmosphere', 'skies',
            'background', 'bg', 'stars', 'sun'},
    'water': {'water', 'sea', 'ocean', 'river', 'lake', 'pond', 'wave', 'waves', 'pool', 'harbor', 'harbour'},
    'glass': {'glass', 'window', 'windows', 'windshield', 'pane', 'transparent'},
    'foliage': {'leaf', 'leaves', 'tree', 'trees', 'bush', 'bushes', 'foliage', 'plant', 'plants', 'fern', 'palm',
                'hedge', 'vine', 'ivy', 'shrub', 'flower', 'flowers', 'cactus', 'canopy', 'branch', 'branches',
                'decal', 'fence', 'chainlink', 'cutout', 'alpha'},
    'road': {'road', 'roads', 'asphalt', 'tarmac', 'street', 'track', 'pavement', 'highway', 'lane', 'curb',
             'kerb', 'sidewalk', 'parking', 'bridge'},
    'ground': {'grass', 'dirt', 'ground', 'terrain', 'sand', 'mud', 'soil', 'gravel', 'rock', 'rocks', 'cliff',
               'mountain', 'mountains', 'hill', 'hills', 'desert', 'canyon', 'mesa', 'stone', 'land', 'floor'},
    'building': {'wall', 'walls', 'building', 'buildings', 'brick', 'bricks', 'roof', 'house', 'concrete',
                 'door', 'store', 'shop', 'garage', 'facade', 'tower', 'station', 'hotel', 'diner', 'motel'},
    'metal': {'metal', 'steel', 'iron', 'rail', 'rails', 'pipe', 'pipes', 'barrier', 'guardrail', 'pole', 'tank',
              'crane', 'girder'},
    'wood': {'wood', 'wooden', 'plank', 'planks', 'crate', 'barrel', 'log', 'logs', 'board', 'boards', 'pier'},
    'light': {'light', 'lights', 'lamp', 'lamps', 'glow', 'neon', 'sign', 'signs', 'emissive', 'bulb', 'lantern'},
}
# shaders that need things a plain mesh doesn't have (layer masks, particles,
# engine buffers ...) - never handed out automatically
EXCLUDED_FAMILIES = ('Error', 'SolidColor', 'scene', 'shadowonly', 'm2', 'm3', 'm4', 'part', 'dist', 'car',
                     'shadowmask', 'math')
# effect-name suffixes: '_a..k' = alpha-tested (cut-out), '_a..b' = alpha-blended
ALPHA_TEST = re.compile(r'_a[a-z0-9]*k(_|$)')
ALPHA_BLEND = re.compile(r'_a[a-z0-9]*b(_|$)')


def tokens(*names):
    """Words in names (object, material, image) for matching: split on
    non-letters and camelCase, lower-cased, filler words (STOP) dropped,
    plural 's' also added without it.
    """
    out = set()
    for n in names:
        if not n:
            continue
        n = os.path.splitext(n)[0]
        n = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', n)
        for w in re.split(r'[^A-Za-z]+', n.lower()):
            if len(w) >= 2 and w not in STOP:
                out.add(w)
                if w.endswith('s') and len(w) > 3:
                    out.add(w[:-1])
    return out


def categories(toks):
    """Kinds of surface (CATEGORIES keys) the words point to; a word also counts
    when it starts with a category word of 4+ letters ('rocky' -> rock).
    """
    cats = set()
    for c, words in CATEGORIES.items():
        if toks & words or any(any(t.startswith(w) for w in words if len(w) >= 4) for t in toks):
            cats.add(c)
    return cats


def effect_kind(effect):
    """'sky' / 'water' / 'glass' / 'cutout' / 'blend' / 'opaque' from the effect name."""
    fam = effect.split('_')[0]
    if fam.startswith('sky'):
        return 'sky'
    if fam.startswith('wtr'):
        return 'water'
    if fam == 'glass':
        return 'glass'
    if ALPHA_TEST.search(effect):
        return 'cutout'
    if ALPHA_BLEND.search(effect):
        return 'blend'
    return 'opaque'


def _image_into(mat, socket_name, exclude=None):
    """Image feeding a Principled BSDF input (through Normal Map / colour nodes)."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None or socket_name not in bsdf.inputs:
        return None
    stack = [l.from_node for l in bsdf.inputs[socket_name].links]
    seen = set()
    while stack:
        n = stack.pop()
        if n.name in seen:
            continue
        seen.add(n.name)
        if n.type == 'TEX_IMAGE' and n.image is not None and n.image != exclude:
            return n.image
        for i in n.inputs:
            stack.extend(l.from_node for l in i.links)
    return None


def base_color(mat):
    """The plain colour of a material without an image (sRGB-ish 0..1)."""
    if mat is not None and mat.use_nodes and mat.node_tree:
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None and 'Base Color' in bsdf.inputs:
            return tuple(max(0.0, min(1.0, v)) for v in bsdf.inputs['Base Color'].default_value[:3])
    if mat is not None:
        return tuple(mat.diffuse_color[:3])
    return (0.8, 0.8, 0.8)


LIGHTMAP_NAME = re.compile(r'(lightmap|light_map|lmap|(^|_)lm($|_)|bake|baked|shadowmap|lighting)')


def _uv_of(node):
    """UV map name an image node reads (through Mapping nodes), '' = the default."""
    stack = [l.from_node for l in node.inputs['Vector'].links] if 'Vector' in node.inputs else []
    while stack:
        n = stack.pop()
        if n.type == 'UVMAP':
            return n.uv_map
        if n.type == 'ATTRIBUTE':
            return n.attribute_name
        for i in n.inputs:
            stack.extend(l.from_node for l in i.links)
    return ''


def find_lightmap(mat, me):
    """(image, uv map name) of a lightmap the user baked into this material: an
    image read through a UV map other than the mesh's first, or named like a
    lightmap / bake. None when there is none."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    first = me.uv_layers[0].name if me is not None and len(me.uv_layers) else ''
    by_name = None
    for n in mat.node_tree.nodes:
        if n.type != 'TEX_IMAGE' or n.image is None:
            continue
        uv = _uv_of(n)
        if uv and uv != first and me is not None and uv in me.uv_layers:
            return n.image, uv
        if by_name is None and LIGHTMAP_NAME.search(os.path.splitext(n.image.name.lower())[0]):
            by_name = (n.image, uv)
    return by_name


def source_images(mat, exclude=None):
    """(colour image, normal image, uses alpha) of a plain Blender material."""
    col = _image_into(mat, 'Base Color', exclude) or _image_into(mat, 'Emission Color', exclude)
    nrm = _image_into(mat, 'Normal')
    if col is None and mat and mat.use_nodes and mat.node_tree:
        # other shader setups (Diffuse/Emission BSDF, glTF groups ...): the first
        # colour image that isn't the normal map or a data map
        data_names = {'non-color', 'raw', 'linear'}
        imgs = [n.image for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image is not None
                and n.image != nrm and n.image != exclude]
        col = next((i for i in imgs if i.colorspace_settings.name.lower() not in data_names
                    and not re.search(r'(_n|_nrm|normal|_r|rough|_m|metal|_ao|spec)(\.|_|$)',
                                      os.path.splitext(i.name.lower())[0] + '.')), None)
    alpha = False
    if mat and mat.use_nodes and mat.node_tree:
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        alpha = bsdf is not None and 'Alpha' in bsdf.inputs and bool(bsdf.inputs['Alpha'].links)
    # Blender 4.2+ reports new materials as HASHED: only real blending counts
    if mat is not None and (getattr(mat, 'surface_render_method', '') == 'BLENDED' or
                            getattr(mat, 'blend_method', 'OPAQUE') == 'BLEND'):
        alpha = True
    return col, nrm, alpha


class Candidate:
    """A world material Auto-Assign can copy, with what is known about it:
    its words (name, the meshes that used it, its textures), kind of surface
    and whether it is lightmapped / has a colour map / glows.
    """
    def __init__(self, mat, users, tex_names):
        """mat: the material; users: names of the meshes that used it; tex_names:
        names of its textures.
        """
        self.mat = mat
        self.effect = mat.c2.effect or ''
        self.kind = effect_kind(self.effect)
        self.users = users
        self.toks = tokens(mat.name, *users, *tex_names)
        self.cats = categories(self.toks)
        if self.kind in ('sky', 'water', 'glass'):
            self.cats.add(self.kind)
        if self.kind == 'cutout':
            self.cats.add('foliage')
        self.lightmapped = 'lightMap1' in {t.usage for t in mat.c2.textures}
        self.has_diffuse = any(t.usage == 'diffuseMapA1' for t in mat.c2.textures)
        self.emissive = any(t.usage.startswith('emissiveMap') for t in mat.c2.textures)
        if self.emissive:
            self.cats.add('light')


def remember_material_users(scene):
    """Store on each imported game material which meshes use it and whether
    they are all helpers the game never draws (render-collision ramps, trigger
    volumes ...), so matching still knows after the meshes are deleted."""
    users, shown = {}, {}
    for o in scene.objects:
        if o.type == 'MESH' and o.c2.kind == 'GEOMETRY' and o.c2.node_index >= 0:
            for s in o.material_slots:
                m = s.material.c2.variant_of or s.material if s.material else None
                if m is not None:
                    users.setdefault(m.name, set()).add(o.get('c2_name', o.name))
                    if not o.get('c2_helper'):
                        shown[m.name] = True
    for m in bpy.data.materials:
        if m.c2.index >= 0 and m.get('c2_world'):
            m.use_fake_user = True    # not dropped on save once no mesh uses it
        if m.c2.index >= 0 and m.name in users:
            m['c2_users'] = sorted(users[m.name])[:50]
            m['c2_helper_only'] = not shown.get(m.name, False)


def world_candidates(scene):
    """Every material Auto-Assign may copy: the world's own game materials plus
    ones imported from other worlds - minus helpers the game never draws,
    excluded shader families and (except water/sky/glass) ones without a
    colour map.
    """
    world = scene.c2.world_name
    remember_material_users(scene)
    texname = {t.index: t.name for t in scene.c2.textures}
    out = []
    for m in bpy.data.materials:
        if m.get('c2_world') != world or m.c2.variant_of is not None:
            continue
        # the world's own materials, plus ones imported from other worlds
        library = bool(m.get('c2_library')) and not m.get('c2_auto_from')
        if m.c2.index < 0 and not library:
            continue
        if not m.c2.effect or m.c2.effect.split('_')[0].startswith(EXCLUDED_FAMILIES):
            continue
        low = m.name.lower()
        if m.get('c2_helper_only') or low.startswith('col:') or 'collision' in low or 'colramp' in low:
            continue              # surfaces the game never draws
        c = Candidate(m, set(m.get('c2_users', [])), [texname.get(t.texture, '') for t in m.c2.textures])
        if c.has_diffuse or c.kind in ('water', 'sky', 'glass'):
            out.append(c)
    return out


def score(cand, toks, cats, alpha):
    """How well a candidate fits a source material (higher is better).
    toks/cats: words and kinds of surface of the source; alpha: the source
    uses transparency. Kind of surface weighs most, then shared words, then
    name similarity; cut-out/blended/glowing shaders are penalised unless asked
    for.
    """
    s = 0.0
    want = cats & {'sky', 'water', 'glass'}
    if want:
        s += 4.0 if cand.kind in want else -4.0
    elif cand.kind in ('sky', 'water', 'glass'):
        s -= 5.0                                   # never sky/water/glass for an ordinary mesh
    if alpha:
        s += 1.5 if cand.kind in ('cutout', 'blend') else -0.5
    elif cand.kind in ('cutout', 'blend'):
        s -= 1.5 if 'foliage' in cats else 3.5     # a solid texture on a cut-out shader shows holes
    if cand.emissive and 'light' not in cats:
        s -= 4.0                                   # glow maps light up the wrong places
    s += 2.0 * len(cats & cand.cats)
    if toks and cand.toks:
        s += 3.0 * len(toks & cand.toks) / len(toks | cand.toks)
        best = max(difflib.SequenceMatcher(None, a, b).ratio() for a in toks for b in cand.toks)
        s += 1.0 * best if best > 0.75 else 0.0
    s += 0.15 * math.log1p(len(cand.users))     # widely used materials are safer defaults
    if cand.lightmapped:
        s -= 0.3                                 # new meshes have no baked lightmap yet
    return s


def ranked_templates(cands, toks, cats, alpha):
    """[(score, candidate)] best first (for the Material Matches report)."""
    if not alpha and 'foliage' not in cats:
        cands = [c for c in cands if c.kind not in ('cutout', 'blend')] or cands
    return sorted(((score(c, toks, cats, alpha), c) for c in cands), key=lambda x: -x[0])


def best_template(cands, toks, cats, alpha):
    """The best-scoring candidate (None if there are none)."""
    if not alpha and 'foliage' not in cats:
        # a solid texture on a cut-out / blended shader shows holes or see-through
        solid = [c for c in cands if c.kind not in ('cutout', 'blend')]
        cands = solid or cands
    ranked = sorted(cands, key=lambda c: score(c, toks, cats, alpha), reverse=True)
    return ranked[0] if ranked else None


# ----------------------------------------------------------------------------
# textures
# ----------------------------------------------------------------------------

def image_file(scene, img):
    """A file on disk for the image (packed / generated images are saved next to the export)."""
    p = bpy.path.abspath(img.filepath) if img.filepath else ''
    if p and os.path.exists(p) and not img.packed_file and not img.is_dirty:
        return p
    folder = bpy.path.abspath(scene.c2.export_dir) if scene.c2.export_dir else bpy.app.tempdir
    folder = os.path.join(folder, 'custom_textures')
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, bpy.path.clean_name(os.path.splitext(img.name)[0]) + '.dds')
    # straight from the pixels (an image .copy() loses a generated/packed image's
    # pixels and saves black); uncompressed DDS, read as is on export
    from .exporter import image_to_dds
    data = image_to_dds(img)
    with open(out, 'wb') as f:
        f.write(data)
    return out


def add_world_texture(scene, img, usage, cache):
    """scene.c2.textures entry for an image (one per image), like Load Image As Texture."""
    key = (img.name, usage.startswith('normal'))
    if key in cache:
        return cache[key]
    path = image_file(scene, img)
    t = scene.c2.textures.add()
    t.index = max((x.index for x in scene.c2.textures), default=-1) + 1
    name = bpy.path.clean_name(os.path.splitext(img.name)[0])
    t.name = name
    t.ttype = '2d'
    t.file = path
    t.source_path = 'materiallibrary\\custom\\%s.png' % name
    t.info = '%dx%d (new)' % tuple(img.size)
    t.srgb = not usage.lower().startswith(('normalmap', 'lightmap', 'envmap', 'environmentmap'))
    t.replace = path
    cache[key] = t.index
    return t.index


def lightmap_from_image(scene, img):
    """A user-baked lightmap image -> the game's RGBM lightmap .dds (light 1.0 =
    the plain texture colour, clipped at 2.0) registered as a world texture."""
    import arcade
    import dds
    import numpy as np
    from . import bakeops
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    rgb = px.reshape(h, w, 4)[::-1, :, :3].astype(np.float64)   # Blender rows are bottom-up
    if not img.is_float and img.colorspace_settings.name.lower().startswith('srgb'):
        rgb = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    folder = os.path.join(bpy.path.abspath(scene.c2.export_dir) if scene.c2.export_dir else bpy.app.tempdir,
                          'custom_textures')
    os.makedirs(folder, exist_ok=True)
    name = bpy.path.clean_name(os.path.splitext(img.name)[0])
    path = os.path.join(folder, name + '_rgbm.dds')
    with open(path, 'wb') as f:
        f.write(dds.write_2d(arcade.rgbm_encode(rgb, arcade.LIGHTMAP_POWER).astype(np.float32)))
    return bakeops.register_lightmap_texture(scene, path, name), float(np.percentile(rgb.mean(-1), 90))


def use_lightmap_uv(me, uv_name):
    """Make the UV map the lightmap was baked on the mesh's lightmapUV."""
    if 'lightmapUV' in me.uv_layers:
        return
    if uv_name and uv_name in me.uv_layers and uv_name != me.uv_layers[0].name:
        me.uv_layers[uv_name].name = 'lightmapUV'
        return
    import numpy as np
    src = me.uv_layers[uv_name] if uv_name in me.uv_layers else (me.uv_layers[0] if len(me.uv_layers) else None)
    if src is None:
        return
    data = np.empty(len(me.loops) * 2, np.float32)
    src.data.foreach_get('uv', data)
    dst = me.uv_layers.new(name='lightmapUV', do_init=False)
    dst.data.foreach_set('uv', data)


def flat_image(name, rgb, srgb=True):
    """A tiny one-colour image (created once), used when a material has no
    image of its own.
    """
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, 4, 4)
        img.pixels = [rgb[0], rgb[1], rgb[2], 1.0] * 16
        if not srgb:
            img.colorspace_settings.name = 'Non-Color'
        img.use_fake_user = True
    return img


def flat_normal_image():
    """Neutral normal map (no bumps) for copies whose template had one."""
    img = bpy.data.images.get('c2_flat_normal')
    if img is None:
        img = bpy.data.images.new('c2_flat_normal', 4, 4)
        img.pixels = [0.5, 0.5, 1.0, 1.0] * 16
        img.colorspace_settings.name = 'Non-Color'
        img.use_fake_user = True
    return img


# ----------------------------------------------------------------------------
# auto-assign
# ----------------------------------------------------------------------------

class C2_OT_auto_assign_materials(bpy.types.Operator):
    """Give new meshes matching Cars 2 materials."""
    bl_idname = 'c2.auto_assign_materials'
    bl_label = 'Auto-Assign Materials'
    bl_description = ('Give the selected new meshes (all new meshes if none are selected) the closest Cars 2 '
                      'material of this world, matched by object/material/texture names and kind of surface '
                      '(sky, water, glass, foliage ...). Each gets its own copy with the mesh\'s colour and '
                      'normal images moved in. Meshes not yet in the Cars 2 scene are added first')
    bl_options = {'REGISTER', 'UNDO'}
    transfer_textures: BoolProperty(name='Transfer Textures', default=True,
                                    description='Use the mesh material\'s own colour / normal images')

    def execute(self, context):
        scene = context.scene
        root = scene.c2.root
        if root is None:
            self.report({'ERROR'}, 'Import a Cars 2 world first')
            return {'CANCELLED'}
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            objs = [o for o in common.descendants(root) if o.type == 'MESH' and
                    (o.c2.kind == 'NONE' or (o.c2.kind == 'GEOMETRY' and o.c2.node_index < 0))]
        objs = [o for o in objs if o.c2.kind in ('NONE', 'GEOMETRY')]
        if not objs:
            self.report({'WARNING'}, 'No new meshes (select the custom scene\'s meshes)')
            return {'CANCELLED'}
        # make sure they are part of the scene
        in_world = set(common.descendants(root))
        loose = [o for o in objs if o.c2.kind == 'NONE' or o not in in_world]
        if loose:
            sel = list(context.selected_objects)
            for o in sel:
                o.select_set(False)
            for o in loose:
                o.select_set(True)
            bpy.ops.c2.add_to_scene()
            for o in loose:
                o.select_set(False)
            for o in sel:
                o.select_set(True)
        cands = world_candidates(scene)
        if len(cands) < 20:
            # shipping worlds have well over a hundred: most were dropped from the
            # file (no mesh used them after Replace World) - bring them back
            from . import importer
            try:
                if importer.restore_world_materials(scene)[0]:
                    cands = world_candidates(scene)
            except OSError:
                pass
        if not cands:
            self.report({'ERROR'}, 'This world has no usable materials to copy (Restore World Materials '
                                   'needs the source .oct - check Source in the Cars 2 Scene panel)')
            return {'CANCELLED'}
        from . import fxnodes
        made = {}        # source material name -> new material
        tex_cache = {}
        lines = []
        fxnodes.SUSPEND[0] += 1
        try:
            for o in objs:
                if not o.material_slots:
                    o.data.materials.append(None)
                for slot in o.material_slots:
                    src = slot.material
                    if src is not None and (src.get('c2_world') or src.get('c2_auto_from') or src.c2.effect):
                        continue          # already a Cars 2 material (imported, auto-assigned or made by hand)
                    key = src.name if src is not None else '<none>:' + o.name
                    # one Cars 2 material per source material (shared by every
                    # mesh using it); steps: find the user's own lightmap and
                    # images, pick the best template, copy it, move images in
                    if key not in made:
                        lm = find_lightmap(src, o.data) if self.transfer_textures else None
                        col_img, nrm_img, alpha = source_images(src, lm[0] if lm else None) if src \
                            else (None, None, False)
                        toks = tokens(o.name, src.name if src else '', col_img.name if col_img else '')
                        cats = categories(toks)
                        pool = cands
                        if lm:      # the user's own lightmap needs a lightmapped shader
                            pool = [c for c in cands if c.lightmapped and c.kind not in ('sky', 'water')] or cands
                        cand = best_template(pool, toks, cats, alpha)
                        # a copy of the template: index -1 = a new MaterialPool
                        # entry on export, cloned from `template`
                        new = cand.mat.copy()
                        new.name = 'C2 %s' % (src.name if src else o.name)
                        new.c2.index = -1
                        new.c2.template = cand.mat.c2.index
                        new['c2_auto_from'] = cand.mat.name
                        for k in ('c2_new_lightmap',):
                            if k in new:
                                del new[k]
                        from . import bakeops
                        if cand.lightmapped:     # neutral light until it is baked
                            bakeops.set_lightmap_slot(scene, new, True)
                        if self.transfer_textures:
                            if col_img is None:
                                # no image of its own: its flat colour, never a Cars 2 texture
                                c = base_color(src)
                                col_img = flat_image('c2_flat_%02x%02x%02x' % tuple(int(round(v * 255)) for v in c),
                                                     c)
                            # no normal map of its own: a flat one rather than the template's bumps
                            # (water keeps its wave normal maps)
                            nrm = nrm_img if nrm_img is not None or cand.kind == 'water' else flat_normal_image()
                            spec = _image_into(src, 'Specular IOR Level') or flat_image('c2_flat_specular',
                                                                                        (0.2, 0.2, 0.2))
                            for usage, img in (('diffuseMapA1', col_img), ('normalMapA1', nrm),
                                               ('specularMapA1', spec)):
                                ts = next((t for t in new.c2.textures if t.usage == usage), None)
                                if img is None or ts is None:
                                    continue
                                try:
                                    ts.texture = add_world_texture(scene, img, usage, tex_cache)
                                except (RuntimeError, OSError) as e:
                                    lines.append('  ! %s: could not use image %s (%s)' % (key, img.name, e))
                        if lm and cand.lightmapped:
                            try:
                                idx, level = lightmap_from_image(scene, lm[0])
                                ts = next((t for t in new.c2.textures if t.usage == 'lightMap1'), None)
                                if ts is not None:
                                    ts.texture = idx
                                new.c2.lightmap = idx
                                new['c2_lightmap_uv'] = lm[1]
                                lines.append('  + your lightmap %s (UV %s)%s' % (
                                    lm[0].name, lm[1] or 'default',
                                    '  - bright: >1.5 clips in game' if level > 1.5 else ''))
                            except (RuntimeError, OSError, ValueError) as e:
                                lines.append('  ! %s: could not use lightmap %s (%s)' % (key, lm[0].name, e))
                        made[key] = new
                        lines.append('%-40s -> %-34s %s%s' % (key[:40], cand.mat.name[:34], cand.effect,
                                                              '  [%s]' % col_img.name if col_img else ''))
                    slot.material = made[key]
                    if made[key].c2.lightmap >= 0 and 'c2_lightmap_uv' in made[key]:
                        use_lightmap_uv(o.data, made[key]['c2_lightmap_uv'])
        finally:
            fxnodes.SUSPEND[0] -= 1
        for k in list(fxnodes._contexts):
            if k[0] == scene.name:
                del fxnodes._contexts[k]
        for m in made.values():
            if m.get('c2_platform') == 'ARCADE':
                try:
                    fxnodes.rebuild_material(scene, m)
                except Exception as e:      # viewport only; export does not depend on it
                    print('Cars 2 Scene: viewport rebuild of %s failed: %s' % (m.name, e))
        txt = bpy.data.texts.get('Cars2AutoMaterials') or bpy.data.texts.new('Cars2AutoMaterials')
        txt.clear()
        txt.write('Material  ->  Cars 2 template  effect  [transferred colour image]\n' + '\n'.join(lines))
        self.report({'INFO'}, '%d material(s) assigned on %d mesh(es) - see the Cars2AutoMaterials text'
                    % (len(made), len(objs)))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# collision-only meshes
# ----------------------------------------------------------------------------

def surface_names(scene):
    """The world's surface type names (index = the per-face surface_type)."""
    import dbl
    p = os.path.splitext(bpy.path.abspath(scene.c2.oct_path))[0] + '.dbl'
    try:
        return list(dbl.Dbl.load(p).surface_types())
    except (OSError, ValueError):
        return []


class C2_OT_make_collision(bpy.types.Operator):
    """Turn meshes into collision-only meshes."""
    bl_idname = 'c2.make_collision'
    bl_label = 'Make Collision (collision-only mesh)'
    bl_description = ('Turn the selected meshes into world collision without drawing them in game: split into '
                      'pieces like the shipping worlds, attached to a new scene node, with starting surface types '
                      '(steep faces = wall barrier, the rest = road). Paint surfaces afterwards with Surface Paint')
    bl_options = {'REGISTER', 'UNDO'}
    max_tris: IntProperty(name='Max Triangles per Piece', default=6000, min=500, max=20000,
                          description='Shipping worlds keep each collision mesh under about 7,400 triangles')
    wall_angle: FloatProperty(name='Wall Angle', default=50.0, min=0.0, max=90.0,
                              description='Faces steeper than this (degrees from flat) become walls')
    wall_surface: StringProperty(name='Wall Surface', default='barrier_Concrete')
    floor_surface: StringProperty(name='Floor Surface', default='asphalt')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        import bmesh
        import numpy as np
        from mathutils import Matrix
        scene = context.scene
        root = scene.c2.root
        if root is None:
            self.report({'ERROR'}, 'Import a Cars 2 world first')
            return {'CANCELLED'}
        srcs = [o for o in context.selected_objects if o.type == 'MESH' and o.c2.kind in ('NONE', 'GEOMETRY')]
        if not srcs:
            self.report({'ERROR'}, 'Select the collision mesh(es)')
            return {'CANCELLED'}
        names = surface_names(scene)
        lower = [n.lower() for n in names]
        wall = lower.index(self.wall_surface.lower()) if self.wall_surface.lower() in lower else 2
        floor = lower.index(self.floor_surface.lower()) if self.floor_surface.lower() in lower else 11
        from . import ops
        col = ops._col_collection(context)
        cos_wall = math.cos(math.radians(self.wall_angle))
        made = 0
        for src in srcs:
            # anchor: a plain scene node at the world origin (every shipping
            # collision instance hangs off a scene node)
            anchor = bpy.data.objects.new('COLROOT_' + src.name, None)
            anchor.empty_display_type = 'PLAIN_AXES'
            col.objects.link(anchor)
            anchor.parent = root
            anchor.matrix_parent_inverse = Matrix.Identity(4)
            anchor.matrix_basis = Matrix.Identity(4)
            anchor.c2.kind = 'NODE'
            anchor.c2.node_index = -1
            # mesh in game space (scale applied: collision transforms carry none)
            G = root.matrix_world.inverted() @ src.matrix_world
            bm = bmesh.new()
            bm.from_mesh(src.data)
            bm.transform(G)
            if G.determinant() < 0:            # mirrored: keep the faces pointing out
                bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.faces.ensure_lookup_table()
            bm.verts.index_update()
            n = len(bm.faces)
            if n == 0:
                bm.free()
                continue
            # split the faces into a grid of ground cells so no piece has more
            # than max_tris triangles (shipping collision meshes stay under
            # ~7,400); steep faces get the wall surface, the rest the floor one
            cent = np.array([f.calc_center_median()[:] for f in bm.faces])
            nrm = np.array([f.normal[:] for f in bm.faces])
            steep = np.abs(nrm[:, 1]) < cos_wall          # game space is Y up
            # a grid on the ground plane, fine enough for the triangle budget
            side = max(1, math.ceil(math.sqrt(math.ceil(n / self.max_tris) * 1.3)))
            lo, hi = cent.min(0), cent.max(0)
            span = np.maximum(hi - lo, 1e-6)
            gx = np.minimum((cent[:, 0] - lo[0]) / span[0] * side, side - 1).astype(int)
            gz = np.minimum((cent[:, 2] - lo[2]) / span[2] * side, side - 1).astype(int)
            cell_of = gx * side + gz
            for c in np.unique(cell_of):
                ids = np.nonzero(cell_of == c)[0]
                for part in range(0, len(ids), self.max_tris):
                    chunk = ids[part:part + self.max_tris]
                    sub = bmesh.new()
                    vmap = {}
                    kept = []
                    for fi in chunk:
                        f = bm.faces[int(fi)]
                        vs = []
                        for v in f.verts:
                            if v.index not in vmap:
                                vmap[v.index] = sub.verts.new(v.co)
                            vs.append(vmap[v.index])
                        try:
                            sub.faces.new(vs)
                            kept.append(fi)
                        except ValueError:      # duplicate face
                            pass
                    me = bpy.data.meshes.new('COLSHAPE_%s_%02d' % (src.name, made))
                    sub.to_mesh(me)
                    sub.free()
                    st = np.where(steep[np.array(kept, int)], wall, floor).astype(np.int32)
                    me.attributes.new('surface_type', 'INT', 'FACE').data.foreach_set('value', st)
                    me.attributes.new('surface_color', 'INT', 'FACE').data.foreach_set(
                        'value', np.zeros(len(me.polygons), np.int32))
                    me.c2col.shape_type = 'TriangleList'
                    me.c2col.shape_index = -1
                    o = bpy.data.objects.new('COL_%s_%02d' % (src.name, made), me)
                    col.objects.link(o)
                    o.parent = anchor
                    o.matrix_parent_inverse = Matrix.Identity(4)
                    o.matrix_basis = Matrix.Identity(4)
                    o.display_type = 'WIRE'
                    o.c2.kind = 'COLLISION'
                    o.c2.col_index = -1
                    made += 1
            bm.free()
            # the source is only a template now: out of the Cars 2 scene, so it is never exported
            mw = src.matrix_world.copy()
            src.parent = None
            src.matrix_world = mw
            src.c2.kind = 'NONE'
            src.hide_set(True)
            src.hide_render = True
        wn = names[wall] if wall < len(names) else str(wall)
        fn = names[floor] if floor < len(names) else str(floor)
        self.report({'INFO'}, '%d collision piece(s); source mesh(es) hidden (delete when happy). '
                              'Walls: %s, floors: %s' % (made, wn, fn))
        return {'FINISHED'}


class C2_OT_restore_materials(bpy.types.Operator):
    """Rebuild missing world materials."""
    bl_idname = 'c2.restore_materials'
    bl_label = 'Restore World Materials'
    bl_description = ('Rebuild the world\'s Cars 2 materials that are missing from this file (Blender drops '
                      'materials no mesh uses when saving, e.g. after Replace World) from the source .oct. '
                      'Meshes and your own materials are not touched')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import importer
        scene = context.scene
        if scene.c2.root is None or not scene.c2.oct_path:
            self.report({'ERROR'}, 'Import a Cars 2 world first')
            return {'CANCELLED'}
        try:
            restored, kept = importer.restore_world_materials(scene)
        except OSError as e:
            self.report({'ERROR'}, 'Cannot read the source world (%s) - check Source in the Cars 2 Scene panel' % e)
            return {'CANCELLED'}
        self.report({'INFO'}, '%d material(s) restored, %d already there' % (restored, kept))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# materials from other worlds
# ----------------------------------------------------------------------------

def read_world_tree(path):
    """(tree, world name) of a world .oct, or of the .oct inside a world .zip."""
    import c2oct
    import zipfile
    if path.lower().endswith('.zip'):
        with zipfile.ZipFile(path) as z:
            octs = [n for n in z.namelist() if n.lower().endswith('.oct') and '/' not in n]
            if not octs:
                raise OSError('no world .oct in %s' % os.path.basename(path))
            world = os.path.splitext(octs[0])[0]
            tmp = os.path.join(bpy.app.tempdir, 'c2lib_%s.oct' % world)
            with open(tmp, 'wb') as f:
                f.write(z.read(octs[0]))
        try:
            return c2oct.load(tmp), world
        finally:
            os.remove(tmp)
    return c2oct.load(path), os.path.splitext(os.path.basename(path))[0]


def _plain(v):
    """Tree values -> JSON-able (kept on the Blender material for the exporter)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, bytes):
        return None
    return v


def import_library_materials(scene, path, kind='ALL', search='', limit=40):
    """Copy materials of another world (effect, parameters and their textures)
    into this world as Auto-Assign candidates. Returns (materials, notes)."""
    import json
    import dds
    from . import importer
    if not len(scene.c2.textures):
        # new textures are numbered after the world's own: those must be known
        raise ValueError('this world was imported without textures - re-import it with textures')
    tree, src_world = read_world_tree(path)
    world = scene.c2.world_name
    # each match: the source entry is copied as JSON (c2_library_entry) with
    # its texture references renumbered to new textures of this world; the
    # exporter writes it as a new MaterialPool entry (exporter.library_entry)
    mats = list(tree.get('MaterialPool', {}).values())
    texs = list(tree.get('TexturePool', {}).values())
    linked = {a.get('Material') for a in tree.get('AssociationPool', {}).values()
              if a.get('Type') == 'MaterialComponentRef_MaterialLink'}
    folder = os.path.join(bpy.path.abspath(scene.c2.export_dir) if scene.c2.export_dir else bpy.app.tempdir,
                          'custom_textures', 'from_' + src_world)
    existing = {m.get('c2_library_src'): m for m in bpy.data.materials if m.get('c2_library_src')}
    words = [w for w in search.lower().split() if w]
    tex_map, out, notes = {}, [], []
    for i, m in enumerate(mats):
        name, eff = m.get('Name', 'Material%d' % i), m.get('Effect', '')
        k = effect_kind(eff)
        if kind != 'ALL' and k != kind.lower():
            continue
        if words and not all(w in name.lower() or w in eff.lower() for w in words):
            continue
        if not eff or eff.split('_')[0].startswith(EXCLUDED_FAMILIES):
            continue
        tes = list(m.get('TextureEntries', {}).values())
        if any(te.get('Usage', '').endswith('Instance') for te in tes):
            notes.append('skipped %s (%s): its textures are set per mesh in %s' % (name, eff, src_world))
            continue
        key = '%s:%s:%s' % (src_world, name, eff)
        if key in existing:
            out.append(existing[key])
            continue
        if len(out) >= limit:
            notes.append('stopped after %d materials - narrow the search' % limit)
            break
        entry = {'Name': name, 'Type': m.get('Type', 'Shader'), 'Effect': eff,
                 'RenderCaps': m.get('RenderCaps', 0), 'TextureEntries': {}, 'PropertyEntries':
                 _plain(m.get('PropertyEntries', {}))}
        for j, te in enumerate(tes):
            r = te.get('TextureReference', -1)
            if not 0 <= r < len(texs):
                continue
            if r not in tex_map:
                t = texs[r]
                data = bytes(t.get('Data', b''))
                tname = t.get('Name', 'tex%d' % r)
                os.makedirs(folder, exist_ok=True)
                p = os.path.join(folder, bpy.path.clean_name(tname) + '.dds')
                with open(p, 'wb') as f:
                    f.write(data)
                item = scene.c2.textures.add()
                item.index = max((x.index for x in scene.c2.textures), default=-1) + 1
                item.name = tname
                item.ttype = t.get('Type', '2d')
                item.file = p
                item.source_path = t.get('SourceFilePath', '')
                item.srgb = dds.is_srgb(data)
                inf = dds.info(data)
                item.info = ('%dx%d %s%s (from %s)' % (inf['width'], inf['height'], inf['format'],
                                                        ' cube' if inf['cube'] else '', src_world)) if inf else ''
                tex_map[r] = item.index
            entry['TextureEntries']['TextureEntry#%d' % j] = {'TextureReference': tex_map[r],
                                                              'Usage': te.get('Usage', '')}
        mat = bpy.data.materials.new('%s (%s)' % (name.split(':')[-1], src_world))
        mat['c2_world'] = world
        mat['c2_platform'] = scene.c2.platform
        mat['c2_library'] = src_world
        mat['c2_library_src'] = key
        mat['c2_library_entry'] = json.dumps(entry)
        mat.use_fake_user = True
        importer.fill_material_props(mat, entry, -1)
        mat.c2.template = -1
        if i in linked:
            notes.append('%s: its texture animation (material component) is not copied' % name)
        out.append(mat)
    return out, notes


class C2_OT_import_world_materials(bpy.types.Operator):
    """Copy materials from another world."""
    bl_idname = 'c2.import_world_materials'
    bl_label = 'Import Materials from World'
    bl_description = ('Copy materials (shader, settings and textures) from another Cars 2 world .zip / .oct into '
                      'this one, e.g. water from italy or oilrig: Auto-Assign can then pick them, or they go '
                      'straight onto the selected meshes')
    bl_options = {'REGISTER', 'UNDO'}
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.zip;*.oct', options={'HIDDEN'})
    kind: bpy.props.EnumProperty(name='Kind', default='WATER', items=(
        ('WATER', 'Water', 'Water shaders (wtr...)'), ('SKY', 'Sky', 'Sky dome / cloud shaders (sky...)'),
        ('GLASS', 'Glass', 'Glass shaders'), ('ALL', 'All', 'Any material (use the name filter)')))
    search: StringProperty(name='Name Filter', description='Only materials whose name or shader contains these '
                                                           'words (e.g. "ocean")')
    assign: BoolProperty(name='Assign to Selected Meshes', default=True,
                         description='Give the selected meshes the best match of the imported materials '
                                     '(replaces what they have)')

    def invoke(self, context, event):
        assets = bpy.path.abspath(context.scene.c2.assets_dir) if context.scene.c2.assets_dir else ''
        if assets and os.path.isdir(os.path.join(assets, 'worlds')):
            self.filepath = os.path.join(assets, 'worlds', '')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from . import fxnodes
        scene = context.scene
        if scene.c2.root is None:
            self.report({'ERROR'}, 'Import a Cars 2 world first')
            return {'CANCELLED'}
        path = bpy.path.abspath(self.filepath)
        try:
            mats, notes = import_library_materials(scene, path, self.kind, self.search)
        except (OSError, ValueError) as e:
            self.report({'ERROR'}, 'Cannot read %s: %s' % (os.path.basename(path), e))
            return {'CANCELLED'}
        if not mats:
            self.report({'WARNING'}, 'No matching materials in %s%s' % (os.path.basename(path),
                                                                         ('; ' + notes[0]) if notes else ''))
            return {'CANCELLED'}
        for k in list(fxnodes._contexts):
            if k[0] == scene.name:
                del fxnodes._contexts[k]
        for m in mats:
            try:
                fxnodes.rebuild_material(scene, m)
            except Exception as e:      # viewport only
                print('Cars 2 Scene: viewport build of %s failed: %s' % (m.name, e))
        assigned = 0
        if self.assign:
            from . import bakeops
            cands = [Candidate(m, set(), []) for m in mats]
            # a part without a baked lightmap is better off on the unlightmapped variant
            plain = [c for c in cands if not c.lightmapped] or cands
            objs = [o for o in context.selected_objects if o.type == 'MESH' and o.c2.kind in ('NONE', 'GEOMETRY')]
            loose = [o for o in objs if o.c2.kind == 'NONE' or o not in set(common.descendants(scene.c2.root))]
            if loose:
                sel = list(context.selected_objects)
                for o in sel:
                    o.select_set(o in loose)
                bpy.ops.c2.add_to_scene()
                for o in sel:
                    o.select_set(True)
            for o in objs:
                cur = o.active_material
                toks = tokens(o.name, cur.name if cur else '')
                cand = best_template(plain, toks, categories(toks), False)
                if not o.material_slots:
                    o.data.materials.append(None)
                for slot in o.material_slots:
                    slot.material = cand.mat
                if cand.lightmapped:
                    bakeops.set_lightmap_slot(scene, cand.mat, True)
                assigned += 1
        txt = bpy.data.texts.get('Cars2AutoMaterials') or bpy.data.texts.new('Cars2AutoMaterials')
        txt.clear()
        txt.write('Imported from %s:\n%s\n%s' % (os.path.basename(path),
                                                 '\n'.join('  %s  [%s]' % (m.name, m.c2.effect) for m in mats),
                                                 '\n'.join('  ! ' + n for n in notes)))
        self.report({'INFO'}, '%d material(s) imported%s - see Material Matches' % (
            len(mats), ', %d mesh(es) assigned' % assigned if assigned else ''))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# deleting a world / collections fast
# ----------------------------------------------------------------------------
# Blender's own Outliner > Delete Hierarchy removes objects one at a time
# (~4.4 ms each in a 5,000-object world: 18 s for tokyo_b). One batch removal
# does the same in ~0.2 s.

def _collection_tree(cols):
    """The collections and every collection inside them."""
    out, stack = [], list(cols)
    while stack:
        c = stack.pop()
        if c not in out:
            out.append(c)
            stack.extend(c.children)
    return out


def delete_world(scene):
    """Remove the scene's imported world: its collections, objects, previews,
    their data, the world's materials, textures and Game Look groups, and the
    editor's state for it. Returns (objects, collections) removed."""
    from . import fxnodes, gameplay
    c2 = scene.c2
    world = c2.world_name
    from . import objplace
    main = bpy.data.collections.get('C2 ' + world) if world else None
    lib = bpy.data.collections.get(objplace.LIBRARY)       # map-object models (not linked to the scene)
    cols = _collection_tree([c for c in (main, lib) if c is not None])
    cols += [c for c in _collection_tree([c for c in bpy.data.collections if c.get('c2_preview')]) if c not in cols]
    objs = {o for c in cols for o in c.all_objects}
    if c2.root is not None:
        objs.add(c2.root)
        objs.update(common.descendants(c2.root))
    n = (len(objs), len(cols))
    others = {s.c2.world_name for s in bpy.data.scenes if s != scene}     # worlds of other scenes stay
    mats = [m for m in bpy.data.materials if (m.get('c2_world') and m.get('c2_world') not in others) or
            m.name.startswith('C2_Surface_')]
    c2.scene_settings = None
    c2.root = None
    common.remove_ids(list(objs), cols)
    # what the world brought with it and nothing uses any more. A lightmap
    # variant points at its game material (a use of it): variants go first.
    for _ in range(3):
        doomed = [m for m in mats if m.users - (1 if m.use_fake_user else 0) == 0]
        if not doomed:
            break
        for m in doomed:
            m.use_fake_user = False
        gone = set(doomed)
        mats = [m for m in mats if m not in gone]
        bpy.data.batch_remove(doomed)
    folders = [os.path.normcase(os.path.abspath(os.path.dirname(bpy.path.abspath(c2.oct_path))))]         if c2.oct_path else []
    assets = common.game_assets(scene)
    if assets:
        folders.append(os.path.normcase(os.path.abspath(assets)))
    imgs = [i for i in bpy.data.images if i.users == 0 and not i.use_fake_user and i.filepath and
            any(os.path.normcase(os.path.abspath(bpy.path.abspath(i.filepath))).startswith(f) for f in folders)]
    groups = [g for g in bpy.data.node_groups if g.users == 0 and g.name.startswith(fxnodes.PREFIX)]
    if imgs or groups:
        bpy.data.batch_remove(imgs + groups)
    for coll in (c2.textures, c2.light_rigs, c2.light_sets, c2.choreos, c2.actors, c2.grids, c2.spawn_lists,
                 c2.node_settings):
        coll.clear()
    for k in [k for k in scene.keys() if k.startswith('c2_')]:
        del scene[k]
    c2.world_name = ''
    c2.oct_path = ''
    reset_caches()
    return n


def reset_caches():
    """Drop every cache holding objects, materials or images of a removed world
    (what loading another file does)."""
    from . import ambientvol, collectgates, crowd, fxnodes, gameplay, gamerender, objplace, particles
    fxnodes._load_post()
    gamerender.PACKAGES.clear()
    gamerender.textures_changed()
    crowd.PLANES.clear()
    crowd._members.clear()
    ambientvol.invalidate()
    collectgates._cache.clear()
    objplace._anim.clear()
    particles.SOURCES.clear()
    gameplay._track_reload()


class C2_OT_delete_world(bpy.types.Operator):
    """Delete the imported world: every object and collection it made, choreography previews, and the
    materials, textures and shader groups only it used (one fast batch, unlike Outliner > Delete Hierarchy)"""
    bl_idname = 'c2.delete_world'
    bl_label = 'Delete World'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.c2.root is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title='Delete %s?' % context.scene.c2.world_name, confirm_text='Delete World',
            message='Removes the imported world and everything it brought into this file. Undo brings it back.',
            icon='WARNING')

    def execute(self, context):
        import time
        t = time.perf_counter()
        n = delete_world(context.scene)
        self.report({'INFO'}, 'Deleted %d objects and %d collections in %.1f s' % (*n, time.perf_counter() - t))
        return {'FINISHED'}


class C2_OT_outliner_delete_fast(bpy.types.Operator):
    """Delete the selected collections and objects with everything inside or parented under them, in one fast
    batch (what Delete Hierarchy does, without its per-object cost)"""
    bl_idname = 'c2.outliner_delete_fast'
    bl_label = 'Delete Hierarchy (Fast)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(getattr(context, 'selected_ids', None))

    def execute(self, context):
        ids = context.selected_ids
        cols = _collection_tree([i for i in ids if isinstance(i, bpy.types.Collection)])
        objs = {o for c in cols for o in c.all_objects}
        roots = [i for i in ids if isinstance(i, bpy.types.Object)]
        if roots:
            kids = {}
            for o in bpy.data.objects:
                if o.parent is not None:
                    kids.setdefault(o.parent.as_pointer(), []).append(o)
            stack = list(roots)
            while stack:
                o = stack.pop()
                if o not in objs:
                    objs.add(o)
                    stack.extend(kids.get(o.as_pointer(), ()))
        scene = context.scene
        if scene.c2.root is not None and scene.c2.root in objs:
            scene.c2.root = None
        common.remove_ids(list(objs), cols)
        reset_caches()
        self.report({'INFO'}, 'Deleted %d objects and %d collections' % (len(objs), len(cols)))
        return {'FINISHED'}


def _outliner_menu(self, context):
    self.layout.separator()
    self.layout.operator(C2_OT_outliner_delete_fast.bl_idname, icon='TRASH')


_classes = (C2_OT_replace_world, C2_OT_auto_assign_materials, C2_OT_make_collision, C2_OT_restore_materials,
            C2_OT_import_world_materials, C2_OT_delete_world, C2_OT_outliner_delete_fast)
_MENUS = ('OUTLINER_MT_collection', 'OUTLINER_MT_object')


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    for m in _MENUS:
        getattr(bpy.types, m).append(_outliner_menu)


def unregister():
    for m in _MENUS:
        getattr(bpy.types, m).remove(_outliner_menu)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
