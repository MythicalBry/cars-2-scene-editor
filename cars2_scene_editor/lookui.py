"""The Material panel's contents, shared by the Cars 2 Scene Editor (worlds)
and the Cars 2 Toolkit (characters; it carries a copy of this material
system): effect and what it does, texture slots, parameters, blending, the
texture list of the material's table (the world's or the character's).
"""
import bpy


class C2_UL_textures(bpy.types.UIList):
    """World texture list row: index, name, size/format (refresh icon = replaced)."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text='%d  %s' % (item.index, item.name), icon='TEXTURE' if not item.replace else 'FILE_REFRESH')
        row.label(text=item.info)


# ----------------------------------------------------------------------


# ----------------------------------------------------------------------

LAYER_SUFFIX = {'A1': 'layer 1', 'A2': 'layer 2', 'A3': 'layer 3', 'A4': 'layer 4'}
STATE_PARAMS = ('CullBackFaces', 'AlphaBlendEnable', 'AlphaTestEnable', 'AlphaRef', 'ZWriteEnable', 'MaxAnisotropy',
                'DepthTestAndWrite', 'SeparateAlphaBlendEnable', 'SrcBlend', 'DestBlend')


def nice_name(name):
    """'specularShineA1' -> 'Specular Shine (layer 1)', 'g_AlphaModifier' -> 'Alpha Modifier'."""
    import re
    n = name[2:] if name.startswith('g_') else name
    suffix = ''
    for k, v in LAYER_SUFFIX.items():
        if n.endswith(k):
            n, suffix = n[:-2], ' (%s)' % v
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', n).replace('_', ' ').split()
    return ' '.join(w[:1].upper() + w[1:] for w in words) + suffix


def draw_mat_props(layout, mat, reset=False):
    """Material parameters as rows (colour pickers for colours, * = changed from
    the effect default, optional reset buttons). Render-state parameters are
    left to draw_states.
    """
    from . import fxnodes
    c = mat.c2
    gamma = set(fxnodes.SHARED_GAMMA) | set(mat.get('c2_gamma_params', ()))
    params = [p for p in c.props if p.name not in STATE_PARAMS]
    for p in sorted(params, key=lambda p: (p.name[-2:] if p.name[-2:] in LAYER_SUFFIX else '', p.name)):
        row = layout.row(align=True)
        changed = p.extra and any(abs(a - b) > 1e-6 for a, b in zip(p.fvalue, p.fdefault)) if p.ptype == 'Float' else False
        row.label(text=nice_name(p.name) + (' *' if changed else ''))
        if p.ptype == 'Float':
            if p.size >= 3 and ('olor' in p.name or 'Light' in p.name or 'Tint' in p.name):
                row.prop(p, 'color_gamma' if p.name in gamma else 'color', text='')
                if p.size == 4:
                    row.prop(p, 'fvalue', index=3, text='')
            else:
                for k in range(p.size):
                    row.prop(p, 'fvalue', index=k, text='')
        else:
            row.prop(p, 'ivalue', text='')
        if reset:
            op = row.operator('c2.reset_param', text='', icon='LOOP_BACK')
            op.material, op.name = mat.name, p.name


def draw_states(layout, mat):
    """Render-state parameters (culling, blending, alpha test ...) as a grid."""
    c = mat.c2
    states = [p for p in c.props if p.name in STATE_PARAMS]
    if not states:
        return
    flow = layout.grid_flow(columns=2, align=True)
    for p in states:
        if p.ptype == 'Bool':
            flow.prop(p, 'ivalue', text=nice_name(p.name), toggle=True)
        elif p.ptype == 'Int':
            flow.prop(p, 'ivalue', text=nice_name(p.name))
        else:
            flow.prop(p, 'fvalue', index=0, text=nice_name(p.name))


class _PackageTexture:
    """Stand-in for a C2Texture of a preview package (name / file only)."""
    ttype = '2d'
    replace = ''
    info = 'preview'

    def __init__(self, path):
        import os
        self.file = path
        self.name = os.path.splitext(os.path.basename(path))[0]


def draw_texture_slot(layout, scene, mat, slot, required=False):
    """One texture slot: thumbnail, name/info, Pick and Load Image buttons.
    required: the effect needs a texture here (an empty one blocks export)."""
    from . import fxnodes
    table = fxnodes.texture_table(scene, mat)
    tex = next((t for t in table if t.index == slot.texture), None) if table is not None else None
    if table is None and slot.texture >= 0:        # a preview package's texture: its file name
        game = fxnodes.material_context(scene, mat)
        path = game.textures.tex_files.get(slot.texture) if game is not None else None
        tex = _PackageTexture(path) if path else None
    row = layout.row()
    row.alert = required and tex is None
    col = row.column()
    img = None
    if tex is not None and tex.ttype == '2d':
        from . import matio
        img = matio.image_at(tex.replace or tex.file)
    if img is not None:
        img.preview_ensure()
        col.template_icon(icon_value=img.preview.icon_id, scale=3.0)
    else:
        col.label(text='', icon='IMAGE_DATA' if tex else 'X')
    col = row.column(align=True)
    col.label(text=nice_name(slot.usage))
    col.label(text=('%s  (%s)' % (tex.name, tex.info)) if tex else
              ('(none) - needs a texture before export' if required else
               "(none) - the game binds its own" if slot.usage in fxnodes.ENGINE_TEXTURES else '(none)'),
              icon='NONE' if tex or not required else 'ERROR')
    r = col.row(align=True)
    op = r.operator('c2.pick_texture', text='Pick', icon='VIEWZOOM')
    cube = fxnodes.slot_is_cube(scene, mat, slot.usage)     # what the effect samples there
    op.material, op.usage = mat.name, slot.usage
    op.cube = cube if cube is not None else bool(tex and tex.ttype.lower() == 'cube')
    op = r.operator('c2.load_texture', text='Load Image', icon='FILEBROWSER')
    op.material, op.usage = mat.name, slot.usage


def draw_arcade(context, l, mat):
    """Arcade material: effect picker + what it does, texture slots, parameters,
    blending.
    """
    from . import fxnodes
    c = mat.c2
    game = fxnodes.material_context(context.scene, mat)
    plan = fxnodes.plan_for(game.assets, c.effect, game.local_effects) if game else None
    box = l.box()
    row = box.row(align=True)
    row.label(text=c.effect or '(no shader)', icon='NODE_MATERIAL')
    # switching goes through Pick Effect: it asks before dropping slots / parameters
    row.operator('c2.pick_effect', text='Change', icon='VIEWZOOM').material = mat.name
    if plan is not None:
        feats = fxnodes.describe(plan)
        box.label(text=', '.join(feats) if feats else 'plain texture', icon='SHADING_RENDERED')
    elif game is None:
        box.label(text='Set the Game Assets Folder to see the game shaders', icon='ERROR')
        box.prop(context.scene.c2, 'assets_dir', text='')
        if c.owner is None:
            box.operator('c2.rebuild_game_materials', icon='FILE_REFRESH')
    else:
        box.label(text='Effect not found in assets/effects', icon='ERROR')
    users = [m for m in bpy.data.materials if m.c2.variant_of == mat]
    if users:
        box.label(text='%d lightmap variant(s) follow this material' % len(users), icon='LINKED')
    # textures
    slots = [t for t in c.textures]
    required = ()
    if plan is not None:
        wanted = fxnodes.material_samplers(plan)
        required = fxnodes.required_textures(plan)
        slots = [t for t in c.textures if t.usage in wanted] + [t for t in c.textures if t.usage not in wanted]
    if slots:
        tb = l.box()
        tb.label(text='Textures', icon='TEXTURE')
        for t in slots:
            draw_texture_slot(tb, context.scene, mat, t, t.usage in required)
    kept = fxnodes.dropped_cache(mat) if mat.get('c2_dropped') else None
    if kept:
        kb = l.box()
        kb.label(text='Kept from earlier shaders (not exported):', icon='RECOVER_LAST')
        names = {t.index: t.name for t in (fxnodes.texture_table(context.scene, mat) or ())}
        for u, v in kept['textures'].items():
            kb.label(text='%s: %s' % (fxnodes.usage_label(u), names.get(v['texture'], '?')), icon='BLANK1')
        if kept['params']:
            kb.label(text='%d parameter(s): %s' % (len(kept['params']), ', '.join(list(kept['params'])[:4]) +
                                                  (' ...' if len(kept['params']) > 4 else '')), icon='BLANK1')
    pb = l.box()
    pb.label(text='Parameters', icon='PROPERTIES')
    draw_mat_props(pb, mat, reset=plan is not None)
    sb = l.box()
    sb.label(text='Blending / culling', icon='MOD_OPACITY')
    draw_states(sb, mat)
    if mat.get('c2_game_mode'):
        sb.label(text='Game Look draws it as: %s' % {'OPAQUE': 'opaque', 'BLEND': 'alpha blended',
                                                     'ADD': 'additive glow', 'MULTIPLY': 'multiply'}.get(
            mat['c2_game_mode'], mat['c2_game_mode']))
    l.label(text='Pool index: %s   RenderCaps: %d' % (c.index if c.index >= 0 else 'new', c.render_caps))


def draw_texture_list(layout, context, mat):
    """The texture list a material's slots point into (the world's, or its
    character's own) with the active texture's file, replacement and sRGB."""
    from . import fxnodes
    scene = context.scene
    base = (mat.c2.variant_of or mat) if mat is not None else None
    owner = base.c2.owner if base is not None else None
    if owner is not None:
        holder, prop, active = owner, 'c2tex', 'c2tex_active'
        layout.label(text='Textures of %s:' % owner.name)
    else:
        table = fxnodes.texture_table(scene, base) if base is not None else scene.c2.textures
        if table is None:
            layout.label(text="A choreography preview's textures (not editable)", icon='INFO')
            return
        holder, prop, active = scene.c2, 'textures', 'active_texture'
        layout.label(text='Textures in this world:')
    layout.template_list('C2_UL_textures', 'tex', holder, prop, holder, active, rows=4)
    items, i = getattr(holder, prop), getattr(holder, active)
    if 0 <= i < len(items):
        t = items[i]
        layout.label(text=t.source_path)
        layout.prop(t, 'replace')
        if base is None or base.get('c2_platform', scene.c2.platform) == 'ARCADE':
            layout.prop(t, 'srgb')


_classes = (C2_UL_textures,)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
