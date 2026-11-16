"""Material editing operators (Arcade Game Look): textures, effects, exposure.

These back the buttons in the Material panel:
    Pick Texture / Load Image As Texture   choose what goes into a texture slot
    Pick Effect                            switch the material's shader
    Reset To Effect Default                undo a parameter edit
    Auto Exposure                          set Scene Brightness like the game does
    Build Game Shaders                     (re)build Game Look for every material
A material's game data lives in mat.c2 (props.C2MaterialSettings): effect
name, parameters (c2.props) and texture slots (c2.textures, usage -> world
texture index). Changing those triggers a live Game Look rebuild (fxnodes).
"""
import os

import bpy
from bpy.props import EnumProperty, StringProperty

import arcade
from . import fxnodes, matio


def _material(context, name):
    """The named material, or the active object's active material."""
    return bpy.data.materials.get(name) if name else (context.active_object.active_material
                                                      if context.active_object else None)


def _base(mat):
    """The game material behind a per-lightmap variant (or the material itself)."""
    return mat.c2.variant_of or mat if mat else None


# ----------------------------------------------------------------------
# textures
# ----------------------------------------------------------------------

def _texture_items(self, context):
    """Search-list entries: every texture of the material's table (the world's,
    or a character's own), those of the slot's kind (2D or cube) first, the
    others marked; a thumbnail icon where Blender can load the file."""
    scene = context.scene
    items = [('-1', '(none)', 'No texture: the slot uses a neutral constant', 'X', 0)]
    want_cube = self.cube
    mat = _base(_material(context, self.material))
    table = fxnodes.texture_table(scene, mat) if mat is not None else scene.c2.textures
    for t in sorted(table or (), key=lambda t: ((t.ttype.lower() == 'cube') != want_cube, t.name.lower())):
        other = (t.ttype.lower() == 'cube') != want_cube
        icon = 0
        if t.ttype == '2d':
            path = bpy.path.abspath(t.replace or t.file)
            img = matio.image_at(path)
            if img is None and path and os.path.exists(path):
                try:
                    img = bpy.data.images.load(path, check_existing=True)
                except RuntimeError:
                    img = None
            if img is not None:
                img.preview_ensure()
                icon = img.preview.icon_id
        label = t.name + ((' (cube map)' if t.ttype.lower() == 'cube' else ' (2D)') if other else '')
        tip = '%s  %s' % (t.info, t.source_path) + (
            '  -  not the kind this slot samples (%s)' % ('a cube map' if want_cube else '2D') if other else '')
        items.append((str(t.index), label, tip, icon, t.index + 1))
    _texture_items.cache = items  # Blender needs the list kept alive
    return items


class C2_OT_pick_texture(bpy.types.Operator):
    bl_idname = 'c2.pick_texture'
    bl_label = 'Pick Texture'
    bl_description = 'Choose one of the world\'s textures for this slot'
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'texture'
    material: StringProperty()      # material name ('' = active material)
    usage: StringProperty()         # slot name, e.g. 'diffuseMapA1'
    cube: bpy.props.BoolProperty(default=False)
    texture: EnumProperty(items=_texture_items)

    def invoke(self, context, event):
        # a searchable drop-down of the world's textures
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        mat = _base(_material(context, self.material))
        if mat is None:
            return {'CANCELLED'}
        idx = int(self.texture)
        slot = next((t for t in mat.c2.textures if t.usage == self.usage), None)
        if slot is None:
            # a slot the effect has but the material didn't list yet
            slot = mat.c2.textures.add()
            slot.usage, slot.extra = self.usage, True
        slot.texture = idx      # live update rebuilds the material
        return {'FINISHED'}


class C2_OT_load_texture(bpy.types.Operator):
    bl_idname = 'c2.load_texture'
    bl_label = 'Load Image As Texture'
    bl_description = ('Add an image file (.dds kept as is, others written as uncompressed DDS) as a new texture '
                      'and use it in this slot. A file named like an existing texture replaces that texture, '
                      'everywhere it is used')
    bl_options = {'REGISTER', 'UNDO'}
    material: StringProperty()
    usage: StringProperty()
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.dds;*.png;*.tga;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.exr', options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        mat = _base(_material(context, self.material))
        if mat is None or not self.filepath:
            return {'CANCELLED'}
        # An image Blender already has from this file is read again: load(check_existing=True) handed back
        # the copy from the first load, so a texture edited and saved over the same file showed the old
        # pixels until Blender was restarted.
        img = matio.image_at(self.filepath)
        try:
            if img is not None:
                img.reload()
            else:
                img = bpy.data.images.load(self.filepath, check_existing=True)
            img.size[0]
        except RuntimeError as e:
            self.report({'ERROR'}, 'Cannot read %s: %s' % (self.filepath, e))
            return {'CANCELLED'}
        try:
            if img.preview is not None:
                img.preview.reload()       # the panel's thumbnail
        except (AttributeError, RuntimeError):
            pass
        # register it as a new texture of the material's table (the world's, or
        # its character's): the next free TexturePool index. The exporter adds
        # it to the .oct (exporter.MaterialWriter.add_user_textures; for a
        # character, the Cars 2 Toolkit's export).
        table = fxnodes.texture_table(scene, mat)
        if table is None:
            self.report({'ERROR'}, "A choreography preview's material: textures can't be added to it")
            return {'CANCELLED'}
        owner = mat.c2.owner
        name = bpy.path.clean_name(os.path.splitext(os.path.basename(self.filepath))[0])
        # A texture of that name already in the table (case ignored, as the game's names come both ways:
        # McQueenRookie_body_d / mcqueenrookie_body_d) is replaced in its own slot - not added a second time,
        # which left the TexturePool with two entries of one name. Its other users get the new image too.
        same = sorted((x for x in table if x.name.lower() == name.lower()), key=lambda x: x.index)
        existing = same[0] if same else None
        if existing is not None:
            t = existing
        else:
            t = table.add()
            t.index = max((x.index for x in table), default=-1) + 1
            t.name = name
            t.ttype = '2d'
            t.source_path = ('characters\\%s\\maps\\%s.png' % (owner.get('c2_stem', owner.name), name) if owner
                             else 'materiallibrary\\custom\\%s.png' % name)
        t.file = self.filepath if existing is None else t.file
        # SUSPEND: don't rebuild Game Look for every property set below
        fxnodes.SUSPEND[0] += 1
        try:
            # as in the shipped worlds: colour, specular and emissive maps are sampled
            # as sRGB; normal maps, lightmaps and reflection cube maps raw. A replaced
            # texture keeps its own flag (the slot it fills is the same kind).
            if existing is None:
                t.srgb = not self.usage.lower().startswith(('normalmap', 'lightmap', 'envmap', 'environmentmap',
                                                            'fallbackmap'))
            t.replace = self.filepath
        finally:
            fxnodes.SUSPEND[0] -= 1
        # size, format and mips as the game will get them (a .dds as is, other images as uncompressed DDS)
        matio.refresh_info(t)
        # the cached texture list is stale now
        if owner is not None:
            owner['c2tex_version'] = owner.get('c2tex_version', 0) + 1
        for k in list(fxnodes._contexts):
            if k[0] == scene.name:
                del fxnodes._contexts[k]
        if existing is not None:
            # every material using the texture shows the new image now (the GPU texture cache is dropped
            # too), not only the one whose slot was clicked
            if owner is not None:
                fxnodes.on_owner_texture_replaced(scene, owner, t.index)
            else:
                fxnodes.on_texture_replaced(scene, t.index)
        slot = next((s for s in mat.c2.textures if s.usage == self.usage), None)
        if slot is None:
            slot = mat.c2.textures.add()
            slot.usage, slot.extra = self.usage, True
        slot.texture = t.index
        if existing is not None:
            self.report({'INFO'}, 'Texture %d "%s" replaced with %s (%s; written into its own TexturePool slot '
                                  'on export)' % (t.index, t.name, os.path.basename(self.filepath), t.info))
        else:
            self.report({'INFO'}, 'New texture %d "%s" (%s; added to the .oct on export)' % (t.index, name, t.info))
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}


# ----------------------------------------------------------------------
# effects
# ----------------------------------------------------------------------

_effect_cache = {}      # effects folder -> search-list items

# The search list's descriptions need each effect translated (~30 ms each,
# 83 s for the game's 1,265 effects). They never change, so they're read from
# EFFECT_LIST (made from the shipped effects by tests/build_effect_list.py)
# and, for effects it doesn't know or whose file size differs (modded), worked
# out once and kept in the user's cache file.
EFFECT_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'effect_list.json')


def _listed_effect(n, skin=False):
    """Effects the picker offers: for world meshes not skinned / character or
    engine-only ones; for a character's (skinned) meshes the _skin ones."""
    if n.startswith(('shadowmask', 'debug', 'depth', 'post', 'downsample')):
        return False
    return n.endswith('_skin') if skin else not n.endswith(('_skin', '_c'))


def is_skinned(mat):
    """A character's material (skinned meshes): its effects are the _skin ones."""
    base = mat.c2.variant_of or mat
    return base.c2.owner is not None or (base.c2.effect or '').lower().endswith('_skin')


def _effect_list_file(path):
    """{effect: [file size, description or None (not usable)]} from a list file."""
    import json
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('effects', {})
    except (OSError, ValueError):
        return {}


def _user_effect_list():
    return os.path.join(bpy.utils.user_resource('CONFIG', path='cars2_scene_editor', create=True), 'effect_list.json')


def describe_effect(assets, name):
    """The search list's description of an effect, or None when it can't be used."""
    plan = fxnodes.plan_for(assets, name)
    return None if plan is None else (', '.join(fxnodes.describe(plan)) or 'basic')


def effect_list(assets, skin=False):
    """[(name, description)] of every usable effect in the game's effects folder
    (skin: the skinned ones, for characters)."""
    import json
    folder = arcade.effects_dir(assets)
    known = _effect_list_file(EFFECT_LIST)
    user_path = _user_effect_list()
    user = _effect_list_file(user_path)
    out, changed = [], False
    for f in sorted(os.listdir(folder)):
        n = f[:-4]
        if not f.endswith('.tfx') or not _listed_effect(n, skin):
            continue
        size = os.path.getsize(os.path.join(folder, f))
        hit = user.get(n) if user.get(n, [None])[0] == size else known.get(n)
        if hit is None or hit[0] != size:
            hit = user[n] = [size, describe_effect(assets, n)]
            changed = True
        if hit[1] is not None:
            out.append((n, hit[1]))
    if changed:
        try:
            with open(user_path, 'w', encoding='utf-8') as f:
                json.dump({'effects': user}, f, indent=0, sort_keys=True)
        except OSError as e:
            print('Cars 2 Scene: effect list cache not saved (%s)' % e)
    return out


def _effect_items(self, context):
    """Search-list entries: every usable effect in the game's effects folder."""
    mat = _base(_material(context, self.material))
    game = fxnodes.material_context(context.scene, mat) if mat is not None else fxnodes.game_context(context.scene)
    if game is None:
        return [('', '(set the Game Assets Folder)', '')]
    skin = mat is not None and is_skinned(mat)
    key = (arcade.effects_dir(game.assets), skin)
    if key not in _effect_cache:
        _effect_cache[key] = [(n, n, d) for n, d in effect_list(game.assets, skin)]
    return _effect_cache[key]


class C2_OT_pick_effect(bpy.types.Operator):
    bl_idname = 'c2.pick_effect'
    bl_label = 'Pick Effect'
    bl_description = ('Choose the shader for this material (search by name; the tooltip lists what each effect '
                      'does). The vertex streams it needs are written on export')
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'effect'
    material: StringProperty()
    effect: EnumProperty(items=_effect_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        mat = _base(_material(context, self.material))
        if mat is None or not self.effect or self.effect == mat.c2.effect:
            return {'CANCELLED'}
        sw = _switch_preview(context, mat, self.effect)
        if sw is not None and (sw['drop_textures'] or sw['drop_params']):
            # something is about to be dropped: ask first (the dialog does the switch)
            bpy.ops.c2.switch_effect('INVOKE_DEFAULT', material=mat.name, effect=self.effect)
            return {'CANCELLED'}
        mat.c2.effect = self.effect      # live update fits its slots / parameters and rebuilds
        _report_blank(self, mat, sw)
        return {'FINISHED'}


def _switch_preview(context, mat, effect):
    """fxnodes.effect_switch for switching mat to effect (None: effect unreadable)."""
    from . import gamerender
    game = fxnodes.material_context(context.scene, mat)
    plan = gamerender.meta_for(game.assets, effect, local=game.local_effects) if game else None
    return fxnodes.effect_switch(mat, plan) if plan is not None else None


def _report_blank(op, mat, sw):
    if sw and sw['blank']:
        op.report({'WARNING'}, '%s: %s need%s a texture before export' % (
            mat.name, ', '.join(fxnodes.usage_label(u) for u in sw['blank']), 's' if len(sw['blank']) == 1 else ''))


class C2_OT_switch_effect(bpy.types.Operator):
    """Switch a material's shader after confirming what it drops"""
    bl_idname = 'c2.switch_effect'
    bl_label = 'Switch Shader'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    material: StringProperty()
    effect: StringProperty()

    def invoke(self, context, event):
        mat = _base(_material(context, self.material))
        if mat is None:
            return {'CANCELLED'}
        self._sw = _switch_preview(context, mat, self.effect)
        return context.window_manager.invoke_props_dialog(self, width=460, title='Switch shader to %s?' % self.effect,
                                                          confirm_text='Switch Shader')

    def draw(self, context):
        mat = _base(_material(context, self.material))
        sw = getattr(self, '_sw', None) or _switch_preview(context, mat, self.effect)
        l = self.layout
        l.label(text='%s: %s  ->  %s' % (mat.name, mat.c2.effect, self.effect), icon='MATERIAL')
        names = {t.index: t.name for t in (fxnodes.texture_table(context.scene, mat) or ())}
        if sw['drop_textures'] or sw['drop_params']:
            box = l.box()
            box.label(text='The new shader doesn\'t use these, so they will be dropped:', icon='ERROR')
            for u, k in sw['drop_textures']:
                box.label(text='%s: %s' % (fxnodes.usage_label(u), names.get(k, '(none)')), icon='TEXTURE')
            if sw['drop_params']:
                col = box.column(align=True)
                p = sw['drop_params']
                for i in range(0, len(p), 2):
                    col.label(text=', '.join(p[i:i + 2]), icon='PROPERTIES' if i == 0 else 'BLANK1')
            box.label(text='They stay with the material and come back if you switch', icon='INFO')
            box.label(text='to a shader that uses them. Export leaves them out.', icon='BLANK1')
        if sw['restore']:
            l.label(text='Brought back: ' + ', '.join(sw['restore']), icon='RECOVER_LAST')
        if sw['blank']:
            box = l.box()
            box.label(text='New texture slots (pick a texture before exporting):', icon='TEXTURE')
            for u in sw['blank']:
                box.label(text=fxnodes.usage_label(u), icon='BLANK1')

    def execute(self, context):
        mat = _base(_material(context, self.material))
        if mat is None or not self.effect:
            return {'CANCELLED'}
        sw = _switch_preview(context, mat, self.effect)
        mat.c2.effect = self.effect
        _report_blank(self, mat, sw)
        return {'FINISHED'}


class C2_OT_reset_param(bpy.types.Operator):
    bl_idname = 'c2.reset_param'
    bl_label = 'Reset To Effect Default'
    bl_options = {'REGISTER', 'UNDO'}
    material: StringProperty()
    name: StringProperty()          # parameter name

    def execute(self, context):
        mat = _base(_material(context, self.material))
        game = fxnodes.material_context(context.scene, mat) if mat else None
        plan = fxnodes.plan_for(game.assets, mat.c2.effect, game.local_effects) if (mat and game) else None
        if plan is None:
            return {'CANCELLED'}
        # the default comes from the effect file itself
        for name, ptype, dtype, size, d in fxnodes.editable_params(plan):
            if name == self.name:
                p = next(p for p in mat.c2.props if p.name == name)
                vals = (d + [0.0] * 4)[:4]
                if p.ptype == 'Float':
                    p.fvalue = vals
                else:
                    p.ivalue = int(vals[0])
        return {'FINISHED'}


# ----------------------------------------------------------------------
# viewport
# ----------------------------------------------------------------------

class C2_OT_auto_exposure(bpy.types.Operator):
    bl_idname = 'c2.auto_exposure'
    bl_label = 'Auto Exposure'
    bl_description = ('Measure the scene brightness of this view like the game\'s auto-exposure does (renders a '
                      'small image) and set Scene Brightness from it')

    def execute(self, context):
        # find a 3D view to measure (the one the button was pressed in, else any)
        rv3d = context.region_data if context.area and context.area.type == 'VIEW_3D' else None
        if rv3d is None and context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    rv3d = area.spaces[0].region_3d
                    break
        from . import gamerender
        if context.scene.render.engine == gamerender.ENGINE_ID and gamerender.RENDERER.post.measured is not None:
            lum = gamerender.RENDERER.post.measured    # what the Cars 2 Game view last measured
        else:
            lum = fxnodes.measure_exposure(context, 160, rv3d)
        if lum is None:
            self.report({'ERROR'}, 'No view to measure')
            return {'CANCELLED'}
        context.scene.c2.exposure_luminance = lum
        self.report({'INFO'}, 'Scene brightness %.3f' % lum)
        return {'FINISHED'}


class C2_OT_rebuild_game_materials(bpy.types.Operator):
    bl_idname = 'c2.rebuild_game_materials'
    bl_label = 'Build Eevee Game Shaders'
    bl_description = ('Translate every material\'s game shader into Eevee nodes (Material Preview / Eevee '
                      'renders in Game Look). The Cars 2 Game render engine doesn\'t need this')

    def execute(self, context):
        scene = context.scene
        scene.c2.eevee_game_shaders = True     # asked for: build the Eevee node versions
        fxnodes._contexts.clear()
        game = fxnodes.game_context(scene)
        if game is None:
            self.report({'ERROR'}, 'Set the Game Assets Folder (it must contain effects/effects/*.tfx)')
            return {'CANCELLED'}
        try:   # flipbook textures (files imported before they were read)
            import arcade, c2oct
            with open(bpy.path.abspath(scene.c2.oct_path), 'rb') as f:
                anims = arcade.texture_animations(c2oct.decode(f.read())[0])
            for m in bpy.data.materials:
                if m.get('c2_world') == scene.c2.world_name and m.c2.variant_of is None and m.c2.index in anims:
                    m['c2_texanim'] = anims[m.c2.index]
        except (OSError, ValueError) as e:
            self.report({'WARNING'}, 'Texture animations not read: %s' % e)
        # every game material of this world: make sure it lists all of its
        # effect's parameters/slots, then rebuild its Game Look nodes
        n = 0
        for m in [m for m in bpy.data.materials if m.get('c2_world') == scene.c2.world_name and m.c2.variant_of is None]:
            plan = fxnodes.plan_for(game.assets, m.c2.effect)
            if plan is not None:
                fxnodes.SUSPEND[0] += 1
                try:
                    fxnodes.ensure_effect_entries(m, plan)
                finally:
                    fxnodes.SUSPEND[0] -= 1
            fxnodes.rebuild_material(scene, m)
            n += 1
        fxnodes.sync_engine(scene)
        for g in [g for g in bpy.data.node_groups if g.name.startswith(fxnodes.PREFIX) and g.users == 0]:
            bpy.data.node_groups.remove(g)   # groups of an older translator
        self.report({'INFO'}, '%d material(s) rebuilt' % n)
        return {'FINISHED'}


_classes = (C2_OT_pick_texture, C2_OT_load_texture, C2_OT_pick_effect, C2_OT_switch_effect, C2_OT_reset_param,
            C2_OT_auto_exposure, C2_OT_rebuild_game_materials)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
