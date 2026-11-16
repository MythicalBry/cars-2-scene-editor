"""3D View > Sidebar > Cars 2 Scene.

Only drawing code lives here: every button calls an operator defined in
ops / matops / bakeops / scenetools / gameplay / exporter, and every field is
a property from props.py. Panels with bl_parent_id are sub-panels (e.g. the
Lighting panel's Light Rig / Light Sets / Probes / Baked Lighting).
"""
import bpy

from . import common, ops
from .lookui import (LAYER_SUFFIX, STATE_PARAMS, draw_arcade, draw_mat_props, draw_states,  # noqa: F401
                     draw_texture_list, draw_texture_slot, nice_name)

CATEGORY = 'Cars 2 Scene'


def _has(context):
    """True when this scene holds an imported Cars 2 world (most panels hide otherwise)."""
    return context.scene.c2.root is not None


class C2Panel:
    """Mixin: puts a panel in 3D View > Sidebar > Cars 2 Scene."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CATEGORY


class C2_UL_simple(bpy.types.UIList):
    """List row showing just the item's name."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name or '(unnamed)')


class C2_UL_choreos(bpy.types.UIList):
    """Choreography list row: its name (new ones marked +) and its file."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.authored, icon='SEQUENCE' if item.uuid else 'ADD')
        row.label(text=item.choreography_name.split('/')[-1])


class C2_UL_kv(bpy.types.UIList):
    """Key = value list row (editable)."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'key', text='', emboss=False)
        row.prop(item, 'value', text='')


class C2_PT_scene(C2Panel, bpy.types.Panel):
    """Source, export folder, export and check buttons, custom-scene tools."""
    bl_label = 'Scene'

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        if not _has(context):
            l.operator('import_scene.cars2_scene', icon='IMPORT')
            l.label(text='Import a world .oct to start.')
            return
        col = l.column(align=True)
        col.label(text='World: %s' % sc.world_name, icon='WORLD')
        col.prop(sc, 'oct_path', text='Source')
        src = bpy.path.abspath(sc.oct_path).replace('\\', '/').lower()
        if '/assets/worlds/' in src:
            box = col.box()
            box.label(text='Source is inside the game folder:', icon='ERROR')
            box.label(text='copying an export there replaces it.')
            box.label(text='Keep the original world elsewhere.')
        plat = {i.identifier: i.name for i in sc.bl_rna.properties['platform'].enum_items}[sc.platform]
        col.label(text=plat, icon='DESKTOP')
        if len(context.scene.get('c2_variants', '{}')) > 2:
            row = l.row(align=True)
            row.prop(sc, 'track_variant', text='Track', icon='MOD_CURVE')
            sub = row.row(align=True)
            sub.enabled = sc.track_variant != 'ALL'
            sub.prop(sc, 'track_mode', text='')
            if context.scene.get('c2_collect_gate_count') and (sc.track_variant == 'ALL' or sc.track_mode == 'Collect'):
                l.row(align=True).prop(sc, 'collect_lap', expand=True)   # Collect: one lap's gates, rings, line
        if len(context.scene.get('c2_agent_places', '{}')) > 2:
            from . import objplace
            objplace.draw_panel(l, context)    # bashables / breakables / pickups at their spawn points
        from . import ambientvol, crowd
        ambientvol.draw_panel(l, context)       # light volumes (fireworks, explosions ...)
        crowd.draw_panel(l, context)            # grandstand crowds
        if context.scene.get('c2_collect_gate_count'):
            from . import collectgates
            collectgates.draw_panel(l, context)  # Collect mode's gates and their posts
        if sc.has_mer:
            from . import finishpoint
            finishpoint.draw_panel(l, context)  # point-to-point finish
        l.prop(sc, 'viewport_lighting', text='Viewport')
        if sc.platform == 'ARCADE':
            # the game shaders are built from the player's own game files, never shipped
            import arcade
            from . import common
            if not arcade.effects_dir(common.game_assets(context.scene)):
                box = l.box()
                box.label(text="Game shaders need your game's assets folder", icon='ERROR')
                box.label(text='(the folder holding effects/effects/*.tfx)')
                box.prop(sc, 'assets_dir', text='')
        if sc.viewport_lighting == 'GAME' and sc.platform == 'ARCADE':
            box = l.box()
            row = box.row(align=True)
            if context.scene.render.engine == 'C2_GAME':
                row.prop(sc, 'game_auto_exposure', toggle=True)
            sub = row.row(align=True)
            sub.active = not (context.scene.render.engine == 'C2_GAME' and sc.game_auto_exposure)
            sub.prop(sc, 'exposure_luminance')
            sub.operator('c2.auto_exposure', text='', icon='EYEDROPPER')
            box.prop(sc, 'inspect')
            # the game's shaders: the Cars 2 Game render engine (Rendered view / F12)
            row = box.row(align=True)
            if context.scene.render.engine == 'C2_GAME':
                row.label(text='Rendered view: game shaders', icon='SHADING_RENDERED')
            else:
                row.label(text='Render engine is not Cars 2 Game', icon='INFO')
                row.prop(context.scene.render, 'engine', text='')
            row = box.row(align=True)
            row.prop(sc, 'game_shadows', toggle=True)
            sub = row.row(align=True)
            sub.active = sc.game_shadows
            sub.prop(sc, 'game_shadow_distance', text='Distance')
            sub.prop(sc, 'game_hires_shadows', text='', icon='SHADING_SOLID')
            row = box.row(align=True)
            row.prop(sc, 'game_bloom', toggle=True)
            row.prop(sc, 'game_reflections', toggle=True)
            row.prop(sc, 'game_motion_blur', toggle=True)
            box.prop(sc, 'eevee_game_shaders')
            from . import fxnodes
            if not sc.eevee_game_shaders:
                pass
            elif fxnodes.outdated_groups():
                box.label(text='Game shaders from an older version', icon='ERROR')
                box.operator('c2.rebuild_game_materials', text='Rebuild Game Shaders', icon='FILE_REFRESH')
            elif bpy.data.node_groups.get(fxnodes.ENGINE) is None:
                box.label(text='Eevee game shaders not built', icon='ERROR')
                box.prop(sc, 'assets_dir', text='')
                box.operator('c2.rebuild_game_materials', icon='FILE_REFRESH')
        l.prop(sc, 'export_dir')
        l.prop(sc, 'export_only_c2')
        row = l.row(align=True)
        row.scale_y = 1.4
        row.operator('export_scene.cars2_scene', icon='EXPORT', text='Export Scene')
        row = l.row(align=True)
        row.operator('c2.validate', icon='CHECKMARK')
        row.operator('c2.add_to_scene', icon='ADD', text='Add Selected')
        row.operator('c2.remove_from_scene', icon='REMOVE', text='Remove Selected')
        l.operator('c2.add_locator', icon='EMPTY_SINGLE_ARROW')
        box = l.box()
        box.label(text='Custom Scene', icon='IMPORT')
        col = box.column(align=True)
        col.operator('c2.replace_world', text='1. Replace World Geometry', icon='TRASH')
        col.label(text='2. Append / paste your scene, select it')
        col.operator('c2.auto_assign_materials', text='3. Add + Auto-Assign Materials', icon='MATERIAL')
        if bpy.data.texts.get('Cars2AutoMaterials'):
            col.operator('c2.show_report', text='Material Matches', icon='TEXT').name = 'Cars2AutoMaterials'
        col.operator('c2.import_world_materials', text='Import Materials from World...', icon='IMPORT')
        col.operator('c2.restore_materials', icon='FILE_REFRESH')
        l.operator('c2.delete_world', icon='TRASH')
        row = l.row(align=True)
        if bpy.data.texts.get('Cars2SceneCheck'):
            row.operator('c2.show_report', text='Check Results', icon='TEXT').name = 'Cars2SceneCheck'
        if bpy.data.texts.get('Cars2SceneExportReport'):
            row.operator('c2.show_report', text='Export Report', icon='TEXT').name = 'Cars2SceneExportReport'


class C2_PT_object(C2Panel, bpy.types.Panel):
    """What the active object is in the game, and its settings."""
    bl_label = 'Selected Object'

    @classmethod
    def poll(cls, context):
        return _has(context) and context.active_object is not None

    def draw(self, context):
        l = self.layout
        o = context.active_object
        c = o.c2
        scene = context.scene
        box = l.box()
        kinds = {i.identifier: i.name for i in c.bl_rna.properties['kind'].enum_items}
        box.label(text='%s  (%s)' % (o.name, kinds[c.kind]))
        if c.kind == 'NONE':
            box.label(text='Not part of the scene yet', icon='INFO')
            box.operator('c2.add_to_scene', icon='ADD')
            return
        top = o
        while top.parent is not None:
            top = top.parent
        if c.kind != 'ROOT' and top != scene.c2.root:
            # e.g. still parented to an older import's root: not exported
            box.label(text='Not in the active world (%s) - not exported' % scene.c2.root.name, icon='ERROR')
            box.operator('c2.add_to_scene', icon='ADD', text='Move to Active World')
        if c.kind in ('NODE', 'GEOMETRY', 'LIGHT', 'CAMERA'):
            box.label(text='Node %s' % (c.node_index if c.node_index >= 0 else 'new (created on export)'))
            if c.node_index < 0:
                box.operator('c2.remove_from_scene', icon='REMOVE', text='Remove from Scene')
            if o.get('c2_name') and o.get('c2_name') != o.name:
                box.label(text='Game name: %s' % o['c2_name'])
            if c.detached:
                box.label(text='Actor root (no scene parent)', icon='OUTLINER_OB_EMPTY')
        if c.kind == 'GEOMETRY':
            box.prop(c, 'visible')
            box.prop(c, 'lightset')
            if o.type == 'MESH':
                row = box.row(align=True)
                row.label(text='%d verts, %d faces' % (len(o.data.vertices), len(o.data.polygons)))
                if scene.c2.platform == 'ARCADE':
                    draw_baked_parts(box, scene, o)
                elif 'ColorPrelight' not in o.data.color_attributes:
                    box.label(text='No ColorPrelight layer', icon='ERROR')
        if c.meridian != 'NONE':
            box.label(text='Meridian %s%s' % (c.meridian.lower(), '' if c.mer_index >= 0 else ' (new)'),
                      icon='NODETREE')
        a = ops.actor_for(scene, o)
        if a is not None:
            comps = [kv.key[len('AddComponent_'):] for kv in a.kvs
                     if kv.key.startswith('AddComponent_') and kv.value.lower() == 'true']
            box.label(text='Actor: ' + ', '.join(comps), icon='GHOST_ENABLED')
        if ops.is_trigger(scene, o):
            tb = l.box()
            tb.label(text='Walk-In Trigger', icon='MOD_PHYSICS')
            plays = [(ch, t) for ch in scene.c2.choreos for t in ch.triggers if t.obj == o]
            for ch, t in plays:
                r = tb.row()
                r.label(text='%s %s' % ('on exit' if t.event == 'EXIT' else 'on enter', ch.authored),
                         icon='SEQUENCE' if t.exists else 'ADD')
            if not plays:
                tb.label(text='Plays no choreography directly')
            tb.operator('c2.play_choreo_on_trigger', icon='PLAY')


class C2_PT_collision(C2Panel, bpy.types.Panel):
    """Collision tools: make collision, surfaces, show/hide."""
    bl_label = 'Collision'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context)

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        o = context.active_object
        if o is not None and o.c2.kind in ('COLLISION', 'COLSHAPE'):
            box = l.box()
            if o.c2.kind == 'COLLISION':
                box.prop(o.c2, 'friction')
                box.prop(o.c2, 'restitution')
                p = o.parent
                box.label(text='Attached to: %s' % (p.name if p and p.c2.kind != 'ROOT' else 'nothing (world)'),
                          icon='LINKED')
            if o.type == 'MESH':
                me = o.data
                box.prop(me.c2col, 'shape_type')
                box.prop(me.c2col, 'collision_layer')
                if me.c2col.shape_type != 'TriangleList':
                    box.prop(me.c2col, 'surface_type')
                if me.users > 1:
                    box.label(text='Shape shared by %d instances' % me.users, icon='LINKED')
        l.operator('c2.toggle_collision', icon='HIDE_OFF')
        col = l.column(align=True)
        col.operator('c2.collision_from_mesh', icon='MOD_TRIANGULATE')
        col.operator('c2.make_collision', icon='MOD_PHYSICS')
        row = col.row(align=True)
        for s in ('Box', 'Sphere', 'Cylinder'):
            row.operator('c2.collision_primitive', text=s).shape = s
        col.operator('c2.attach_collision', icon='CONSTRAINT')
        box = l.box()
        box.label(text='Surface Paint')
        row = box.row(align=True)
        row.prop(sc, 'surface_brush')
        row.prop(sc, 'surface_color')
        row = box.row(align=True)
        row.operator('c2.set_surface', icon='BRUSH_DATA')
        row.operator('c2.select_surface', icon='RESTRICT_SELECT_OFF', text='Select')


class C2_PT_lighting(C2Panel, bpy.types.Panel):
    """Viewport lighting mode, Game Look exposure and inspection."""
    bl_label = 'Lighting'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context)

    def draw(self, context):
        l = self.layout
        o = context.active_object
        if o is not None and o.type == 'LIGHT' and o.c2.kind == 'LIGHT':
            c = o.data.c2
            box = l.box()
            box.label(text='Light: ' + o.name, icon='LIGHT')
            box.prop(c, 'light_type')
            box.prop(o.data, 'color')
            box.prop(c, 'intensity')
            box.prop(c, 'shadow_color')
            if c.light_type == 'Spot':
                box.prop(c, 'cone_angle')
                box.prop(c, 'penumbra_angle')
                box.prop(c, 'dropoff')
                box.prop(c, 'decay_rate')
            if c.light_type == 'Point':
                box.prop(c, 'decay_rate')
            if c.light_type == 'Ambient':
                box.prop(c, 'ambient_shade')
            box.prop(c, 'exclusion_type')
            col = box.column(align=True)
            col.prop(c, 'falloff_radius')
            col.prop(c, 'softness')
            row = box.row(align=True)
            row.prop(c, 'is_roaming', toggle=True)
            row.prop(c, 'use_tertiary', toggle=True)
            row.prop(c, 'is_negative', toggle=True)
            row = box.row(align=True)
            row.prop(c, 'lm_normal', toggle=True)
            row.prop(c, 'lm_specular', toggle=True)


class C2_PT_scene_settings(C2Panel, bpy.types.Panel):
    """The world's scene_settings material (fog, sun, bloom ...)."""
    bl_label = 'Scene Settings (fog, sun, bloom)'
    bl_parent_id = 'C2_PT_lighting'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        m = context.scene.c2.scene_settings
        if m is None:
            self.layout.label(text='This world has no scene_settings material')
            return
        self.layout.label(text=m.name, icon='MATERIAL')
        draw_mat_props(self.layout, m)


class C2_PT_light_rigs(C2Panel, bpy.types.Panel):
    """Light rigs: SH lighting for moving objects."""
    bl_label = 'Light Rig (SH ambient)'
    bl_parent_id = 'C2_PT_lighting'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sc = context.scene.c2
        l = self.layout
        if not len(sc.light_rigs):
            l.label(text='No LightRigPool in this world')
            return
        l.template_list('C2_UL_simple', 'rigs', sc, 'light_rigs', sc, 'active_rig', rows=2)
        rig = sc.light_rigs[sc.active_rig]
        l.prop(rig, 'rig_name')
        l.prop(rig, 'type_name')
        l.prop(rig, 'position')
        l.label(text='Lights: ' + ', '.join(r.obj.name for r in rig.lights if r.obj))
        row = l.row(align=True)
        row.operator('c2.rig_lights_from_selection', icon='RESTRICT_SELECT_OFF')
        row.operator('c2.rig_from_lights', icon='LIGHT_SUN')
        box = l.box()
        box.label(text='SH coefficients (L0, L1 x3, L2 x5)')
        for ch, name in enumerate(('R', 'G', 'B')):
            row = box.row(align=True)
            row.label(text=name)
            for k in range(9):
                row.prop(rig, 'sh', index=ch * 9 + k, text='')


class C2_PT_light_sets(C2Panel, bpy.types.Panel):
    """Light sets: which dynamic lights affect which meshes."""
    bl_label = 'Light Sets (dynamic lights per mesh)'
    bl_parent_id = 'C2_PT_lighting'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sc = context.scene.c2
        l = self.layout
        l.template_list('C2_UL_simple', 'sets', sc, 'light_sets', sc, 'active_lightset', rows=2)
        if len(sc.light_sets):
            ls = sc.light_sets[sc.active_lightset]
            l.label(text=', '.join(r.obj.name for r in ls.lights if r.obj) or '(empty)')
        row = l.row(align=True)
        row.operator('c2.lightset_from_selection', text='Set From Selected')
        row.operator('c2.lightset_from_selection', text='New From Selected').new = True
        l.operator('c2.assign_lightset')


class C2_PT_probes(C2Panel, bpy.types.Panel):
    """Light probes (reflection cube maps, Arcade volume lighting)."""
    bl_label = 'Light Probes'
    bl_parent_id = 'C2_PT_lighting'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        o = context.active_object
        if o is None or o.c2.kind != 'PROBE':
            l.label(text='Select a probe (Environment = cube map, Volume = lighting volume)')
            return
        c = o.c2
        l.prop(c, 'probe_name')
        l.prop(c, 'probe_type')
        l.prop(c, 'probe_node')
        l.prop(c, 'probe_texture')
        tex = next((t for t in context.scene.c2.textures if t.index == c.probe_texture), None)
        if tex:
            l.label(text='%s  %s' % (tex.name, tex.info))
            l.prop(tex, 'replace')
        if c.probe_type == 'Volume':
            l.prop(c, 'probe_density')
            l.prop(c, 'probe_fade')
            l.prop(c, 'probe_outside')
            if c.probe_has_inside:
                l.prop(c, 'probe_inside')
            if o.parent is not None and o.parent == c.probe_node:
                l.label(text='Volume = this box (moves with %s)' % o.parent.name, icon='CUBE')
            else:
                l.label(text='Volume extents = object scale')
            grid = o.get('c2_direct_sh')
            if grid is not None:
                import arcade
                cells = arcade.probe_cells(c.probe_density)
                ok = len(grid) == cells * 27
                l.label(text='Lighting grid: %d cells%s' % (len(grid) // 27, '' if ok else
                        ' (resampled to %d on export)' % cells), icon='LIGHTPROBE_VOLUME' if ok else 'ERROR')
            if c.probe_has_luminance:
                l.prop(c, 'probe_luminance')
            col = l.column(align=True)
            col.operator('c2.bake_probe_sh', icon='RENDER_STILL')
            col.operator('c2.probe_sh_fill', icon='MOD_TINT')
        l.prop(c, 'probe_scene')
        if c.probe_type == 'Environment' or c.probe_texture >= 0:
            l.operator('c2.bake_probe', icon='RENDER_STILL')



class C2_PT_prelight(C2Panel, bpy.types.Panel):
    """Baking: lightmaps, vertex lighting, switching between them."""
    bl_label = 'Baked Lighting (lightmaps / vertex colours)'
    bl_parent_id = 'C2_PT_lighting'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        l.prop(sc, 'viewport_lighting', text='Viewport')
        if sc.platform == 'ARCADE':
            box = l.box()
            box.label(text='Each part uses a lightmap (_lmh) or', icon='INFO')
            box.label(text='VertexBaked colours (_vb), never both')
            col = l.column(align=True)
            col.operator('c2.bake_lightmaps', icon='RENDER_STILL')
            col.operator('c2.bake_prelight', icon='RENDER_STILL')
            col.operator('c2.prelight_fill', icon='BRUSH_DATA')
            row = l.row(align=True)
            row.operator('c2.switch_baked_lighting', text='To Lightmaps').mode = 'LIGHTMAP'
            row.operator('c2.switch_baked_lighting', text='To Vertex Colours').mode = 'VERTEX'
            l.label(text='Paint: Vertex Paint on VertexBaked (alpha = brightness)')
            if not sc.assets_dir and not common.game_assets(context.scene):
                l.prop(sc, 'assets_dir')
        else:
            l.label(text='World lighting lives in ColorPrelight', icon='INFO')
            l.prop(sc, 'prelight_scale')
            l.operator('c2.bake_prelight', icon='RENDER_STILL')
            l.operator('c2.prelight_fill', icon='BRUSH_DATA')
            l.label(text='Paint by hand: Vertex Paint mode on ColorPrelight')


def draw_baked_parts(layout, scene, o):
    """Arcade: which baked lighting each material slot of a mesh uses."""
    import arcade
    names = {t.index: t.name for t in scene.c2.textures}
    for slot in o.material_slots:
        m = slot.material
        if m is None:
            continue
        base = m.c2.variant_of or m
        mode = arcade.baked_mode(base.c2.effect)
        if mode == 'LIGHTMAP':
            if m.get('c2_new_lightmap'):
                lm = bpy.path.basename(m['c2_new_lightmap'])
            else:
                lm = names.get(m.c2.lightmap, 'none yet')
            layout.label(text='%s: lightmap %s' % (base.name, lm), icon='IMAGE_DATA')
        elif mode == 'VERTEX':
            ok = 'VertexBaked' in o.data.color_attributes
            layout.label(text='%s: VertexBaked%s' % (base.name, '' if ok else ' (missing)'),
                         icon='VPAINT_HLT' if ok else 'ERROR')
        elif base.c2.effect not in ('SolidColor', 'shadowonly'):
            layout.label(text='%s: lit at run time' % base.name, icon='LIGHT_SUN')


class C2_PT_meridian(C2Panel, bpy.types.Panel):
    """Choreographies of the Meridian graph: list, anchors, triggers, preview."""
    bl_label = 'Meridian: Choreographies'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context) and context.scene.c2.has_mer

    def draw(self, context):
        sc = context.scene.c2
        l = self.layout
        l.label(text='Namespace: %s' % sc.namespace)
        if sc.assets_dir or not common.game_assets(context.scene):
            l.prop(sc, 'assets_dir')
        row = l.row()
        row.template_list('C2_UL_choreos', 'choreos', sc, 'choreos', sc, 'active_choreo', rows=5)
        col = row.column(align=True)
        col.operator('c2.choreo_add', icon='ADD', text='')
        col.operator('c2.choreo_remove', icon='REMOVE', text='')
        if not (0 <= sc.active_choreo < len(sc.choreos)):
            return
        c = sc.choreos[sc.active_choreo]
        row = l.row(align=True)
        row.scale_y = 1.3
        row.operator('c2.choreo_preview', icon='PLAY')
        row.operator('c2.choreo_preview_clear', icon='X', text='')
        l.operator('c2.choreo_preview_all', icon='SEQ_SEQUENCER')
        box = l.box()
        box.prop(c, 'authored')
        box.prop(c, 'choreography_name')
        grid = box.grid_flow(columns=2, align=True)
        for f in ('loop', 'visible_before', 'visible_after', 'enableAsyncLoad', 'enableMusicSync',
                  'hide_gameplay_elements', 'use_master', 'fullScreen_CHO'):
            grid.prop(c, f, toggle=True)
        box.label(text='Anchors (where the choreography is placed):')
        for k, a in enumerate(c.anchors):
            row = box.row(align=True)
            row.prop(a, 'name', text='')
            row.prop(a, 'locator', text='')
            if a.locator:
                row.operator('c2.select_object', text='', icon='RESTRICT_SELECT_OFF').name = a.locator.name
            if not a.ref_uuid:
                row.operator('c2.choreo_anchor_remove', text='', icon='X').index = k
            elif a.locator is None and a.source:
                box.label(text='   fed by %s' % a.source)
            if a.locator is None and a.resolved:
                box.label(text='   in game: %s' % a.resolved, icon='INFO')
        box.operator('c2.choreo_anchor_add', icon='ADD')
        box.prop(c, 'play_on_start', icon='PLAY')
        if len(c.actors):
            box.label(text='Characters (each needs a world actor):')
            for a in c.actors:
                row = box.row(align=True)
                if a.exists:
                    row.label(text='%s: actor in the world' % a.name, icon='CHECKMARK')
                else:
                    row.prop(a, 'spawn', text='%s (%s)' % (a.name, a.source))
            if any(not a.exists and not a.spawn for a in c.actors):
                box.label(text='Characters without an actor stay invisible', icon='ERROR')
        if not (c.play_on_start or len(c.triggers) or c.drivers):
            box.label(text='Nothing starts this choreography', icon='ERROR')
        box.label(text='Played by triggers:')
        for k, t in enumerate(c.triggers):
            row = box.row(align=True)
            row.prop(t, 'obj', text='')
            row.prop(t, 'event', text='')
            if not t.exists:
                row.operator('c2.choreo_trigger_remove', text='', icon='X').index = k
        box.operator('c2.choreo_trigger_add', icon='ADD')
        if c.drivers:
            box.label(text='Graph inputs: ' + c.drivers)


class C2_PT_actor(C2Panel, bpy.types.Panel):
    """The active object's .ap actor lines."""
    bl_label = 'Actor Properties (.ap)'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context) and context.scene.c2.has_ap

    def draw(self, context):
        l = self.layout
        a = ops.actor_for(context.scene, context.active_object)
        if a is None:
            l.label(text='The active object has no .ap section')
            l.label(text='(bashables, triggers, pickups and spawners do)')
            return
        l.label(text='[%s]' % a.name)
        row = l.row()
        row.template_list('C2_UL_kv', 'kvs', a, 'kvs', a, 'active_kv', rows=8)
        col = row.column(align=True)
        col.operator('c2.actor_kv_add', icon='ADD', text='')
        col.operator('c2.actor_kv_remove', icon='REMOVE', text='')


class C2_PT_material(C2Panel, bpy.types.Panel):
    """The active material's game settings and the world's texture list."""
    bl_label = 'Material'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context)

    def draw(self, context):
        l = self.layout
        o = context.active_object
        mat = o.active_material if o else None
        sc = context.scene.c2
        if o is not None and o.type == 'MESH' and len(o.material_slots) > 1:
            l.template_list('MATERIAL_UL_matslots', '', o, 'material_slots', o, 'active_material_index', rows=2)
        if mat is not None and mat.c2.variant_of is not None:
            box = l.box()
            box.label(text='Lightmap variant of %s' % mat.c2.variant_of.name, icon='LINKED')
            box.prop(mat.c2, 'lightmap')
            tex = next((t for t in sc.textures if t.index == mat.c2.lightmap), None)
            if mat.get('c2_new_lightmap'):
                box.label(text='Baked: ' + bpy.path.basename(mat['c2_new_lightmap']))
            elif tex:
                box.label(text='%s  %s' % (tex.name, tex.info))
            if mat.c2.envmap_inst >= 0:
                box.prop(mat.c2, 'envmap_inst')
            mat = mat.c2.variant_of  # the game properties live on the base material
        if mat is not None:
            c = mat.c2
            l.label(text=mat.name, icon='MATERIAL')
            if c.index < 0 and not c.effect:
                l.label(text='Plain Blender material: cloned from a', icon='INFO')
                l.label(text='world material on export (Image Texture = diffuse)')
                l.operator('c2.material_from_template')
            elif mat.get('c2_platform', sc.platform) == 'ARCADE':
                draw_arcade(context, l, mat)
            else:
                l.label(text='Pool index: %s' % (c.index if c.index >= 0 else 'new'))
                l.prop(c, 'effect')
                l.prop(c, 'render_caps')
                draw_mat_props(l.box(), mat)
                for t in c.textures:
                    row = l.row(align=True)
                    row.label(text=t.usage)
                    row.prop(t, 'texture', text='')
        l.separator()
        draw_texture_list(l, context, mat)

# ----------------------------------------------------------------------
# layers (collection visibility)
# ----------------------------------------------------------------------

class C2_PT_layers(C2Panel, bpy.types.Panel):
    """Eye toggles for every collection of the world (Shift-click: solo)."""
    bl_label = 'Layers'

    @classmethod
    def poll(cls, context):
        return _has(context)

    def draw(self, context):
        from . import gameplay
        l = self.layout
        cols = gameplay.collections(context.scene, False)
        if not cols:
            return
        col = l.column(align=True)
        for key, label, parent in gameplay.LAYOUT:
            c = cols.get(key)
            if c is None:
                continue
            self._row(context, col, c, label, 1 if parent else 0, gameplay)
            if key == 'Spawns':
                for ch in sorted(c.children, key=lambda x: x.name):
                    self._row(context, col, ch, ch.name.split('Spawn: ', 1)[-1], 2, gameplay)
        from . import modecols
        modes = modecols.mode_collections(context.scene)
        if modes:
            col.separator()
            col.label(text='Game modes (only what that mode uses)')
            for label, mc, kinds in modes:
                self._row(context, col, mc, label, 1, gameplay)
                for kc in kinds:
                    self._row(context, col, kc, kc.name.split(label + ' ', 1)[-1].split('Spawn: ', 1)[-1], 2, gameplay)
        l.operator('c2.organize_modes', icon='OUTLINER_COLLECTION')
        l.label(text='Shift+click: show only that layer', icon='INFO')
        if not context.scene.c2.has_gameplay:
            l.operator('c2.load_gameplay', icon='IMPORT')

    @staticmethod
    def _row(context, layout, c, label, depth, gameplay):
        """One layer row: eye toggle, name and object count, indented by depth."""
        lc = gameplay.layer_collection(context, c)
        if lc is None:
            return
        row = layout.row(align=True)
        if depth:
            row.separator(factor=2.0 * depth)
        n = len(c.all_objects)
        op = row.operator('c2.toggle_layer', text='', emboss=False,
                          icon='HIDE_ON' if lc.hide_viewport else 'HIDE_OFF')
        op.collection = c.name
        row.label(text='%s  (%d)' % (label, n))


# ----------------------------------------------------------------------
# gameplay
# ----------------------------------------------------------------------

class C2_UL_spawn_lists(bpy.types.UIList):
    """Spawn list row (lock = no entry port, can't add points)."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name, icon='EMPTY_SINGLE_ARROW' if item.has_entry else 'LOCKED')
        row.label(text=item.info)


class C2_UL_node_settings(bpy.types.UIList):
    """Gameplay node row: name and node type."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name, icon='NODETREE')
        row.label(text=item.node_type)


class C2_PT_gameplay(C2Panel, bpy.types.Panel):
    """Gameplay data: AI track, markers, spawns, grids, paths, nav meshes."""
    bl_label = 'Gameplay (AI, spawns, nav meshes)'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _has(context)

    def draw(self, context):
        l = self.layout
        if not context.scene.c2.has_gameplay:
            l.label(text='Imported by an older version', icon='INFO')
            l.operator('c2.load_gameplay', icon='IMPORT')
            return
        l.label(text='Written back on Export Scene', icon='INFO')


class C2_PT_gameplay_track(C2Panel, bpy.types.Panel):
    """AI track graph: preview, editing, per-node/link values."""
    bl_label = 'AI Track'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from . import gameplay
        l = self.layout
        sc = context.scene.c2
        objs = gameplay._objs(context.scene, 'AI_TRACK')
        if not objs:
            l.label(text='This world has no AI track')
            l.operator('c2.track_new', icon='ADD')
            return
        t = objs[0]
        row = l.row(align=True)
        row.label(text='%d nodes, %d links' % (len(t.data.vertices), len(t.data.edges)), icon='CURVE_PATH')
        row.operator('c2.select_object', text='', icon='RESTRICT_SELECT_OFF').name = t.name
        row = l.row(align=True)
        row.prop(sc, 'track_preview_show', text='Preview', toggle=True,
                 icon='HIDE_OFF' if sc.track_preview_show else 'HIDE_ON')
        row.prop(sc, 'track_preview', text='')
        if sc.track_preview_show:
            row = l.row(align=True)
            row.prop(sc, 'track_preview_width', text='Width')
            row.prop(sc, 'track_preview_opacity', text='Opacity', slider=True)
        box = l.box()
        box.label(text='Edit Mode: vertices = nodes,')
        box.label(text='edges = links (travel direction,')
        box.label(text='see arrows). E extrudes, F joins')
        if context.active_object == t and t.mode == 'EDIT':
            import bmesh
            bm = bmesh.from_edit_mesh(t.data)
            v = bm.select_history.active if isinstance(bm.select_history.active, bmesh.types.BMVert) else \
                next((v for v in bm.verts if v.select), None)
            e = bm.select_history.active if isinstance(bm.select_history.active, bmesh.types.BMEdge) else \
                next((e for e in bm.edges if e.select), None)
            if v is not None:
                nb = l.box()
                nb.label(text='Node %d' % v.index, icon='VERTEXSEL')
                for k in gameplay.NODE_FLOATS + gameplay.NODE_INTS:
                    lay = (bm.verts.layers.int if k in gameplay.NODE_INTS else bm.verts.layers.float).get(k)
                    val = v[lay] if lay is not None else 0
                    self._setter(nb, k, val)
                nb.operator('c2.track_frames', icon='ORIENTATION_GIMBAL')
            if e is not None:
                eb = l.box()
                eb.label(text='Link %d: node %d -> %d' % (e.index, e.verts[0].index, e.verts[1].index),
                         icon='EDGESEL')
                for k in gameplay.LINK_INTS + gameplay.LINK_FLOATS:
                    lay = (bm.edges.layers.int if k in gameplay.LINK_INTS else bm.edges.layers.float).get(k)
                    val = e[lay] if lay is not None else 0
                    self._setter(eb, k, val)
                eb.operator('c2.track_flip', icon='ARROW_LEFTRIGHT')
        else:
            l.label(text='Select the graph, Tab to edit')

    @staticmethod
    def _setter(layout, key, val):
        """A row 'name  [value]' whose button sets that value on the selection."""
        row = layout.row(align=True)
        row.label(text=short_name(key))
        txt = ('%g' % val) if isinstance(val, float) else str(val)
        op = row.operator('c2.track_set', text=txt)
        op.attr, op.value = key, txt


def short_name(key):
    """'EnergyLineUTrackB' -> 'Energy line B', 'FastLineHopVTrackA' -> 'Fast hop A'."""
    import re
    m = re.match(r'(Fast|Energy)Line(U|HopV|TurboV)Track([ABC])$', key)
    if m:
        what = {'U': 'line', 'HopV': 'hop', 'TurboV': 'turbo'}[m.group(2)]
        return '%s %s %s' % (m.group(1), what, m.group(3))
    return {'LookAheadDistance': 'Look ahead', 'EndOfRaceMarker': 'End of race',
            'IgnoreOtherPlayers': 'Ignore others', 'AvoidanceBitfield': 'Avoid lanes',
            'StraightawayLength': 'Straightaway', 'ForceDrifting': 'Force drift'}.get(key, nice_name(key))


class C2_PT_gameplay_markers(C2Panel, bpy.types.Panel):
    """AI markers (hop / sidestep / drift points)."""
    bl_label = 'AI Markers (hop / sidestep / drift)'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        o = context.active_object
        if o is not None and o.c2.kind == 'AI_MARKER':
            box = l.box()
            box.label(text=o.name, icon='EMPTY_DATA')
            box.prop(o.c2, 'marker_type')
            box.prop(o.c2, 'marker_track')
            box.prop(o.c2, 'marker_distance')
        l.operator_menu_enum('c2.marker_add', 'marker_type', icon='ADD', text='Add Marker at Cursor')


class C2_PT_gameplay_spawns(C2Panel, bpy.types.Panel):
    """Spawn and restart point lists."""
    bl_label = 'Spawn & Restart Points'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        if not len(sc.spawn_lists):
            l.label(text='No spawn / restart lists in this world')
            return
        l.template_list('C2_UL_spawn_lists', 'spawns', sc, 'spawn_lists', sc, 'active_spawn_list', rows=5)
        if not (0 <= sc.active_spawn_list < len(sc.spawn_lists)):
            return
        it = sc.spawn_lists[sc.active_spawn_list]
        if not it.has_entry:
            l.label(text='Filled by game code / actors, not locators', icon='LOCKED')
            return
        col = l.column(align=True)
        col.operator('c2.spawn_add', icon='ADD')
        row = col.row(align=True)
        row.operator('c2.spawn_membership', text='Add Selected', icon='LINKED').remove = False
        row.operator('c2.spawn_membership', text='Remove Selected', icon='UNLINKED').remove = True
        if it.name in SPAWN_HELP:
            l.label(text=SPAWN_HELP[it.name], icon='INFO')


SPAWN_HELP = {
    'SpawnRabbit': 'Arena: where Rabbit attack NPCs appear',
    'SpawnRam': 'Arena: where Ram attack NPCs appear',
    'SpawnSpotter': 'Arena: where Spotter attack NPCs appear',
    'SpawnHenchmen': 'Arena: where Henchmen appear',
    'RabbitHidingPlaces': 'Arena: where Rabbits run to hide',
    'HunterRestartLocations': 'Hunter: player start / respawn (by player)',
    'TakedownTrackA': 'Takedown: enemy spawn points on track A',
    'TakedownTrackB': 'Takedown: enemy spawn points on track B',
    'ResetTrackA': 'Race: reset points on track A',
    'ResetTrackB': 'Race: reset points on track B',
    'CollectTrackA': 'Collect: start of the collect run (track A)',
    'CollectTrackB': 'Collect: start of the collect run (track B)',
}


class C2_PT_gameplay_grids(C2Panel, bpy.types.Panel):
    """Starting grids and their preview."""
    bl_label = 'Starting Grids'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        if not len(sc.grids):
            l.label(text='No StartingGrid node in this world')
            return
        l.template_list('C2_UL_simple', 'grids', sc, 'grids', sc, 'active_grid', rows=2)
        if not (0 <= sc.active_grid < len(sc.grids)):
            return
        g = sc.grids[sc.active_grid]
        col = l.column(align=True)
        col.prop(g, 'locator', text='Grid')
        col.prop(g, 'collect_locator', text='Collect')
        col.prop(g, 'track')
        col = l.column(align=True)
        col.prop(g, 'row_count')
        col.prop(g, 'h_spacing')
        col.prop(g, 'v_spacing')
        col.prop(g, 'stagger')
        row = l.row(align=True)
        row.prop(g, 'custom23', toggle=True)
        row.prop(g, 'custom32', toggle=True)
        row = l.row(align=True)
        row.prop(sc, 'grid_racers')
        row.operator('c2.grid_preview', text='', icon='FILE_REFRESH')
        l.label(text='Slots follow the grid locator', icon='INFO')
        l.label(text='Car order: xml/gameprogression.xml')


class C2_PT_gameplay_paths(C2Panel, bpy.types.Panel):
    """Named .dbl paths (lap spline, lanes, cameras ...)."""
    bl_label = 'Paths (.dbl)'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        o = context.active_object
        if o is not None and o.c2.kind == 'PATH':
            box = l.box()
            box.label(text=o.get('c2_path_name', o.name), icon='CURVE_DATA')
            if o.data.splines:
                sp = o.data.splines[0]
                box.label(text='%d points' % len(sp.points))
                box.prop(sp, 'use_cyclic_u', text='Closed Loop')
            if o.get('c2_bspline_only'):
                box.label(text='Spline-only path (control points)', icon='INFO')
            name = o.get('c2_path_name', o.name)
            if name in ('TrackA', 'TrackB', 'TrackC', 'lane1'):
                box.label(text='Lap progress / race positions follow this path', icon='TIME')
        l.operator('c2.path_new', icon='ADD')
        l.prop(context.scene.c2, 'path_outlines', toggle=True, icon='CURVE_PATH')
        l.label(text='Light blue outline: a .dbl path (not a collect line)')
        l.label(text='TrackA/B/C = lap progress')
        l.label(text='NavMesh_Spline_* = bridges')


class C2_PT_gameplay_nav(C2Panel, bpy.types.Panel):
    """Arena nav meshes."""
    bl_label = 'Nav Meshes (arenas)'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from . import gameplay
        l = self.layout
        objs = gameplay._objs(context.scene, 'NAVMESH')
        l.label(text='%d nav mesh(es), %d triangles' % (len(objs), sum(len(o.data.polygons) for o in objs)))
        o = context.active_object
        if o is not None and o.c2.kind == 'NAVMESH':
            box = l.box()
            box.label(text=o.get('c2_navmesh_name', o.name), icon='MESH_GRID')
            box.label(text='%d faces (triangulated on export)' % len(o.data.polygons))
        l.operator('c2.navmesh_from_mesh', icon='MESH_GRID')
        l.label(text='One mesh per walkable island')
        l.label(text='Edited ones are rebuilt on export')


class C2_PT_gameplay_collect(C2Panel, bpy.types.Panel):
    """Collect mode (Survival): the gates per track and lap, in the game's order."""
    bl_label = 'Collect Gates (Survival)'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.scene.c2.has_mer

    def draw(self, context):
        from . import collectgates
        collectgates.draw_gates_panel(self.layout, context)


class C2_PT_gameplay_nodes(C2Panel, bpy.types.Panel):
    """Plain settings of gameplay Meridian nodes."""
    bl_label = 'Gameplay Node Settings'
    bl_parent_id = 'C2_PT_gameplay'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        l = self.layout
        sc = context.scene.c2
        if not len(sc.node_settings):
            l.label(text='No gameplay settings nodes')
            return
        l.template_list('C2_UL_node_settings', 'nodes', sc, 'node_settings', sc, 'active_node_settings', rows=5)
        if 0 <= sc.active_node_settings < len(sc.node_settings):
            it = sc.node_settings[sc.active_node_settings]
            l.template_list('C2_UL_kv', 'nodekv', it, 'kvs', it, 'active_kv', rows=8)


_classes = (C2_UL_simple, C2_UL_choreos, C2_UL_kv, C2_UL_spawn_lists, C2_UL_node_settings,
            C2_PT_scene, C2_PT_layers, C2_PT_object, C2_PT_collision,
            C2_PT_lighting, C2_PT_scene_settings, C2_PT_light_rigs, C2_PT_light_sets, C2_PT_probes,
            C2_PT_prelight, C2_PT_meridian, C2_PT_actor, C2_PT_gameplay, C2_PT_gameplay_track,
            C2_PT_gameplay_markers, C2_PT_gameplay_spawns, C2_PT_gameplay_grids, C2_PT_gameplay_paths,
            C2_PT_gameplay_nav, C2_PT_gameplay_collect, C2_PT_gameplay_nodes, C2_PT_material)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
