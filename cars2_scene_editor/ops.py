"""Editing operators.

Sections:
    scene       add/remove objects to/from the world, locators
    collision   make collision from meshes or primitives, surface painting
    lighting    light rigs (SH), light sets, probe cube maps
    meridian    choreographies, their anchors and triggers
    actors      .ap key/value editing
    materials   Make Cars 2 Material
    checks      Check Scene (validate) and report windows
Operators only change Blender data; nothing is written to game files until
Export Scene.
"""
import math
import os
import tempfile

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

import dds
import meridian
import shlight
from . import common


def _root(context):
    """The active world root (None before an import)."""
    return context.scene.c2.root


def _need_root(op, context):
    """Report an error and return False when no world is imported."""
    if _root(context) is None:
        op.report({'ERROR'}, 'Import a Cars 2 scene first (File > Import > Cars 2 Scene)')
        return False
    return True


def actor_for(scene, obj):
    """The .ap actor (scene.c2.actors entry) named like this object, or None."""
    if obj is None:
        return None
    name = obj.get('c2_name', obj.name)
    for a in scene.c2.actors:
        if a.name == name or a.name == obj.name:
            return a
    return None


def actor_value(actor, key, default=None):
    """Value of `key` in an actor's key/value lines."""
    for kv in actor.kvs:
        if kv.key == key:
            return kv.value
    return default


def is_trigger(scene, obj):
    """True when the object's actor has a walk-in trigger component."""
    a = actor_for(scene, obj)
    return a is not None and any(kv.key in ('AddComponent_WalkInTrigger', 'AddComponent_WalkInTriggerFiltered')
                                 and kv.value.lower() == 'true' for kv in a.kvs)


# ----------------------------------------------------------------------
# scene
# ----------------------------------------------------------------------

class C2_OT_add_to_scene(bpy.types.Operator):
    """Make the selected objects part of the Cars 2 world."""
    bl_idname = 'c2.add_to_scene'
    bl_label = 'Add Selected to Scene'
    bl_description = ('Parent the selected objects under the Cars 2 scene root (keeping their placement), '
                      'including objects still under an older import\'s root; '
                      'they become new scene nodes on export. Meshes get baked-lighting colour layers')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _need_root(self, context):
            return {'CANCELLED'}
        root = _root(context)
        n = 0
        for o in context.selected_objects:
            if o == root or o.c2.kind == 'ROOT':
                continue
            if 'c2_excluded' in o:
                del o['c2_excluded']
            # a parent that is another (older) import's root counts as loose too:
            # only the scene's own root is exported
            if o.parent is None or (o.parent != root and o.parent.c2.kind in ('NONE', 'ROOT')):
                mw = o.matrix_world.copy()
                o.parent = root
                o.matrix_parent_inverse = Matrix.Identity(4)
                o.matrix_world = mw
            # node_index -1 = a new scene node, created on export
            if o.c2.kind == 'NONE':
                o.c2.kind = {'MESH': 'GEOMETRY', 'LIGHT': 'LIGHT', 'CAMERA': 'CAMERA'}.get(o.type, 'NODE')
                o.c2.node_index = -1
            if o.type == 'MESH':
                # the layers the viewport materials read (Arcade: the exporter writes
                # whatever streams the part's effect binds)
                arc = context.scene.c2.platform == 'ARCADE'
                for name in (('ColorOcclusion', 'VertexBaked') if arc else ('ColorPrelight',)):
                    if name not in o.data.color_attributes:
                        import vertexfmt
                        ca = o.data.color_attributes.new(name, 'BYTE_COLOR', 'POINT')
                        ca.data.foreach_set('color_srgb', np.tile(np.asarray(vertexfmt.default_values(name, 1)[0],
                                                                             np.float32), len(ca.data)))
            # a Blender light: pick the matching game light type and intensity
            if o.type == 'LIGHT':
                o.data.c2.light_type = {'SUN': 'Directional', 'SPOT': 'Spot'}.get(
                    o.data.type, 'Point' if context.scene.c2.platform == 'ARCADE' else 'Ambient')
                o.data.c2.intensity = o.data.energy / math.pi if o.data.type == 'SUN' else 1.0
            n += 1
        self.report({'INFO'}, '%d object(s) added to the Cars 2 scene' % n)
        return {'FINISHED'}


class C2_OT_remove_from_scene(bpy.types.Operator):
    """Take new objects out of the Cars 2 world again."""
    bl_idname = 'c2.remove_from_scene'
    bl_label = 'Remove Selected from Scene'
    bl_description = ('Take the selected new objects out of the Cars 2 scene again (they stay in Blender, where '
                      'they are, with their materials) so they are not exported. Objects that came with the '
                      'imported world are not touched: hide them, or delete them to remove them from the world')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _need_root(self, context):
            return {'CANCELLED'}
        root = _root(context)
        n, kept = 0, []
        for o in context.selected_objects:
            if o == root or o.c2.kind == 'ROOT':
                continue
            if o.c2.kind not in ('NONE', 'NODE', 'GEOMETRY', 'LIGHT', 'CAMERA'):
                continue        # collision, gameplay, probes ...: their own tools
            if o.c2.node_index >= 0:
                kept.append(o.name)
                continue
            if o.parent is not None and o.parent.c2.kind in ('ROOT', 'NODE', 'GEOMETRY', 'LIGHT', 'CAMERA'):
                mw = o.matrix_world.copy()
                o.parent = None
                o.matrix_world = mw
            o.c2.kind = 'NONE'
            o.c2.node_index = -1
            o['c2_excluded'] = True       # left out on purpose: no export warning
            n += 1
        msg = '%d object(s) removed from the Cars 2 scene' % n
        if kept:
            msg += '; %d from the imported world left as they are (hide or delete those): %s' % (
                len(kept), ', '.join(kept[:5]))
        self.report({'WARNING'} if kept else {'INFO'}, msg)
        return {'FINISHED'}


class C2_OT_add_locator(bpy.types.Operator):
    """Add a Meridian locator."""
    bl_idname = 'c2.add_locator'
    bl_label = 'Add Meridian Locator'
    bl_description = 'New Transform node bound to a Meridian LocatorNode (anchors choreographies, spawn points ...)'
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty(name='Name', default='LOC_New')
    at_active: BoolProperty(name='At Active Object', default=True)

    def execute(self, context):
        if not _need_root(self, context):
            return {'CANCELLED'}
        o = make_locator(context, self.name, context.active_object if self.at_active else None)
        for s in context.selected_objects:
            s.select_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


def make_locator(context, name, at=None):
    """A new locator empty under the root (at `at`'s position or the 3D cursor),
    upright in game space. It becomes a Transform node + Meridian LocatorNode
    on export.
    """
    root = _root(context)
    o = bpy.data.objects.new(name, None)
    o.empty_display_type = 'SINGLE_ARROW'
    o.empty_display_size = 3.0
    col = bpy.data.collections.get('%s Nodes' % context.scene.c2.world_name) or context.collection
    col.objects.link(o)
    o.parent = root
    o.matrix_parent_inverse = Matrix.Identity(4)
    if at is not None:
        o.matrix_world = Matrix.Translation(at.matrix_world.translation) @ root.matrix_world.to_3x3().to_4x4()
    else:
        o.matrix_world = Matrix.Translation(context.scene.cursor.location) @ root.matrix_world.to_3x3().to_4x4()
    o.c2.kind = 'NODE'
    o.c2.node_index = -1
    o.c2.meridian = 'LOCATOR'
    o.c2.mer_index = -1
    return o


# ----------------------------------------------------------------------
# collision
# ----------------------------------------------------------------------

def _col_collection(context):
    """The world's Collision collection."""
    return bpy.data.collections.get('%s Collision' % context.scene.c2.world_name) or context.collection


class C2_OT_collision_from_mesh(bpy.types.Operator):
    """Make triangle-mesh collision from the selected meshes."""
    bl_idname = 'c2.collision_from_mesh'
    bl_label = 'Collision From Mesh'
    bl_description = ('Create a triangle-mesh collision instance from each selected mesh, attached to it: '
                      'it follows the object, and its game transform is computed on export')
    bl_options = {'REGISTER', 'UNDO'}
    surface_type: IntProperty(name='Surface Type', default=0)

    def execute(self, context):
        if not _need_root(self, context):
            return {'CANCELLED'}
        n = 0
        for src in [o for o in context.selected_objects if o.type == 'MESH' and o.c2.kind == 'GEOMETRY']:
            # a stripped, triangulated copy of the mesh (collision has no UVs,
            # colours or materials - only a surface type per face)
            me = src.data.copy()
            me.name = 'COLSHAPE_' + src.name
            me.materials.clear()
            for a in list(me.uv_layers):
                me.uv_layers.remove(a)
            for a in list(me.color_attributes):
                me.color_attributes.remove(a)
            bm = bmesh.new()
            bm.from_mesh(me)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.to_mesh(me)
            bm.free()
            for nm in ('surface_type', 'surface_color'):
                if nm in me.attributes:
                    me.attributes.remove(me.attributes[nm])
            me.attributes.new('surface_type', 'INT', 'FACE').data.foreach_set(
                'value', np.full(len(me.polygons), self.surface_type, np.int32))
            me.attributes.new('surface_color', 'INT', 'FACE').data.foreach_set(
                'value', np.zeros(len(me.polygons), np.int32))
            me.c2col.shape_type = 'TriangleList'
            me.c2col.shape_index = -1
            o = bpy.data.objects.new('COL_' + src.name, me)
            _col_collection(context).objects.link(o)
            o.parent = src
            o.matrix_parent_inverse = Matrix.Identity(4)
            o.matrix_basis = Matrix.Identity(4)
            o.display_type = 'WIRE'
            o.c2.kind = 'COLLISION'
            o.c2.col_index = -1
            n += 1
        self.report({'INFO'}, '%d collision instance(s) created' % n)
        return {'FINISHED'}


class C2_OT_collision_primitive(bpy.types.Operator):
    """Add a box / sphere / cylinder collision shape."""
    bl_idname = 'c2.collision_primitive'
    bl_label = 'Add Collision Primitive'
    bl_description = 'Add a primitive collision instance fitted to the active object\'s bounds and attached to it'
    bl_options = {'REGISTER', 'UNDO'}
    shape: EnumProperty(items=[('Box', 'Box', ''), ('Sphere', 'Sphere', ''), ('Cylinder', 'Cylinder', '')], default='Box')

    def execute(self, context):
        if not _need_root(self, context):
            return {'CANCELLED'}
        src = context.active_object
        root = _root(context)
        if src is not None and src.type == 'MESH':
            bb = [Vector(c) for c in src.bound_box]
            mn = Vector((min(v.x for v in bb), min(v.y for v in bb), min(v.z for v in bb)))
            mx = Vector((max(v.x for v in bb), max(v.y for v in bb), max(v.z for v in bb)))
        else:
            mn, mx = Vector((-1, -1, -1)), Vector((1, 1, 1))
        c, h = (mn + mx) * 0.5, (mx - mn) * 0.5
        bm = bmesh.new()
        if self.shape == 'Box':
            bmesh.ops.create_cube(bm, size=2.0)
            for v in bm.verts:
                v.co = Vector((v.co.x * h.x, v.co.y * h.y, v.co.z * h.z)) + c
        elif self.shape == 'Sphere':
            bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=max(h))
            for v in bm.verts:
                v.co += c
        else:
            r = max(h.x, h.z)
            bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=1.0, radius2=1.0, depth=2.0)
            for v in bm.verts:
                x, y, z = v.co
                v.co = Vector((x * r, z * h.y, y * r)) + c
        me = bpy.data.meshes.new('COLSHAPE_%s' % self.shape)
        bm.to_mesh(me)
        bm.free()
        me.c2col.shape_type = self.shape
        me.c2col.surface_type = context.scene.c2.surface_brush
        o = bpy.data.objects.new('COL_%s' % (src.name if src else self.shape), me)
        _col_collection(context).objects.link(o)
        o.parent = src if (src is not None and src.c2.kind != 'NONE') else root
        o.matrix_parent_inverse = Matrix.Identity(4)
        o.matrix_basis = Matrix.Identity(4)
        o.display_type = 'WIRE'
        o.c2.kind = 'COLLISION'
        o.c2.col_index = -1
        return {'FINISHED'}


class C2_OT_attach_collision(bpy.types.Operator):
    """Attach collision to a scene node."""
    bl_idname = 'c2.attach_collision'
    bl_label = 'Attach Collision To Active'
    bl_description = ('Re-parent the selected collision instances to the active scene node, keeping their world '
                      'placement. The export writes the association and the node-relative transform for you')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        tgt = context.active_object
        if tgt is None or tgt.c2.kind not in ('NODE', 'GEOMETRY', 'ROOT'):
            self.report({'ERROR'}, 'Make a scene node (Transform/Geometry) the active object')
            return {'CANCELLED'}
        n = 0
        for o in context.selected_objects:
            if o.c2.kind == 'COLLISION' and o != tgt:
                mw = o.matrix_world.copy()
                o.parent = tgt
                o.matrix_parent_inverse = Matrix.Identity(4)
                o.matrix_world = mw
                n += 1
        self.report({'INFO'}, '%d collision instance(s) attached to %s' % (n, tgt.name))
        return {'FINISHED'}


class C2_OT_set_surface(bpy.types.Operator):
    """Paint the surface type onto collision faces."""
    bl_idname = 'c2.set_surface'
    bl_label = 'Assign Surface To Faces'
    bl_description = 'Set surface_type / surface_color on the selected faces (Edit Mode, triangle-mesh collision)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        n = 0
        for o in context.objects_in_mode or [context.active_object]:
            if o is None or o.type != 'MESH':
                continue
            if o.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(o.data)
                lt = bm.faces.layers.int.get('surface_type') or bm.faces.layers.int.new('surface_type')
                lc = bm.faces.layers.int.get('surface_color') or bm.faces.layers.int.new('surface_color')
                for f in bm.faces:
                    if f.select:
                        f[lt] = sc.surface_brush
                        f[lc] = sc.surface_color
                        n += 1
                bmesh.update_edit_mesh(o.data)
            else:
                me = o.data
                if me.c2col.shape_type != 'TriangleList':
                    me.c2col.surface_type = sc.surface_brush
                    n += 1
                    continue
                for nm, val in (('surface_type', sc.surface_brush), ('surface_color', sc.surface_color)):
                    a = me.attributes.get(nm) or me.attributes.new(nm, 'INT', 'FACE')
                    a.data.foreach_set('value', np.full(len(me.polygons), val, np.int32))
                n += len(me.polygons)
        self.report({'INFO'}, 'surface set on %d face(s)' % n)
        return {'FINISHED'}


class C2_OT_select_surface(bpy.types.Operator):
    """Select the collision faces that have the current surface type"""
    bl_idname = 'c2.select_surface'
    bl_label = 'Select Faces With Surface'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        o = context.active_object
        if o is None or o.mode != 'EDIT':
            self.report({'ERROR'}, 'Edit Mode on a collision mesh')
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(o.data)
        lt = bm.faces.layers.int.get('surface_type')
        for f in bm.faces:
            f.select_set(lt is not None and f[lt] == context.scene.c2.surface_brush)
        bmesh.update_edit_mesh(o.data)
        return {'FINISHED'}


class C2_OT_toggle_collision(bpy.types.Operator):
    """Show or hide the world's collision objects"""
    bl_idname = 'c2.toggle_collision'
    bl_label = 'Show/Hide Collision'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        col = bpy.data.collections.get('%s Collision' % context.scene.c2.world_name)
        if col is not None:
            lc = _find_layer_collection(context.view_layer.layer_collection, col.name)
            if lc is not None:
                lc.hide_viewport = not lc.hide_viewport
        return {'FINISHED'}


def _find_layer_collection(lc, name):
    """The view-layer entry of a collection (where its eye/visibility lives)."""
    if lc.collection.name == name:
        return lc
    for c in lc.children:
        r = _find_layer_collection(c, name)
        if r is not None:
            return r
    return None


# ----------------------------------------------------------------------
# lighting
# ----------------------------------------------------------------------

class C2_OT_rig_from_lights(bpy.types.Operator):
    """Rebuild a light rig from its lights."""
    bl_idname = 'c2.rig_from_lights'
    bl_label = 'Project SH From Rig Lights'
    bl_description = ('Rebuild the rig\'s spherical harmonics from its directional/ambient lights using '
                      'the standard SH projection of directional and ambient lights (Sloan, GDC 2008)')
    bl_options = {'REGISTER', 'UNDO'}
    match: BoolProperty(name='Keep Overall Brightness', default=True,
                        description='Scale the result so its ambient (DC) term matches the current rig. '
                                    'Cars 2\'s shipped rigs use a different normalisation than the runtime formula')

    def execute(self, context):
        sc = context.scene.c2
        if not (0 <= sc.active_rig < len(sc.light_rigs)):
            return {'CANCELLED'}
        rig = sc.light_rigs[sc.active_rig]
        root = _root(context)
        lights = []
        for r in rig.lights:
            o = r.obj
            if o is None or o.type != 'LIGHT':
                continue
            G = common.game_matrix(root, o)
            d = Vector((G[0][2], G[1][2], G[2][2]))  # local +Z in game space (towards the light)
            lights.append((o.data.c2.light_type, list(o.data.color), o.data.c2.intensity, tuple(d)))
        if not lights:
            self.report({'ERROR'}, 'The rig has no lights (add Directional/Ambient lights to it)')
            return {'CANCELLED'}
        rig.sh = shlight.project_lights(lights, list(rig.sh) if self.match else None)
        return {'FINISHED'}


class C2_OT_rig_lights_from_selection(bpy.types.Operator):
    """Make the selected Cars 2 lights the active light rig's lights"""
    bl_idname = 'c2.rig_lights_from_selection'
    bl_label = 'Use Selected Lights'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        rig = sc.light_rigs[sc.active_rig]
        rig.lights.clear()
        for o in context.selected_objects:
            if o.type == 'LIGHT' and o.c2.kind == 'LIGHT':
                rig.lights.add().obj = o
        return {'FINISHED'}


class C2_OT_lightset_from_selection(bpy.types.Operator):
    """Fill the active light set from the selected lights."""
    bl_idname = 'c2.lightset_from_selection'
    bl_label = 'Set From Selected Lights'
    bl_description = 'Replace the active light set\'s lights with the selected Cars 2 lights'
    bl_options = {'REGISTER', 'UNDO'}
    new: BoolProperty(default=False)

    def execute(self, context):
        sc = context.scene.c2
        if self.new or not len(sc.light_sets):
            ls = sc.light_sets.add()
            ls.index = len(sc.light_sets) - 1
            ls.name = 'LightSet %d' % ls.index
            sc.active_lightset = ls.index
        ls = sc.light_sets[sc.active_lightset]
        ls.lights.clear()
        for o in context.selected_objects:
            if o.type == 'LIGHT' and o.c2.kind in ('LIGHT',):
                ls.lights.add().obj = o
        return {'FINISHED'}


class C2_OT_assign_lightset(bpy.types.Operator):
    """Make the selected meshes use the active light set"""
    bl_idname = 'c2.assign_lightset'
    bl_label = 'Assign Light Set To Selected Meshes'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        for o in context.selected_objects:
            if o.c2.kind == 'GEOMETRY':
                o.c2.lightset = sc.active_lightset
        return {'FINISHED'}


# D3D cube face order +X -X +Y -Y +Z -Z: (forward, up) in game space
CUBE_FACES = [((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, 1, 0)), ((0, 1, 0), (0, 0, -1)),
              ((0, -1, 0), (0, 0, 1)), ((0, 0, 1), (0, 1, 0)), ((0, 0, -1), (0, 1, 0))]


class C2_OT_bake_probe(bpy.types.Operator):
    """Render a probe's reflection cube map."""
    bl_idname = 'c2.bake_probe'
    bl_label = 'Bake Probe Cube Map'
    bl_description = ('Render a cube map from the active light probe\'s position with the current render engine '
                      'and use it as the probe\'s texture on export (uncompressed DDS)')
    bl_options = {'REGISTER'}
    size: IntProperty(name='Face Size', default=64, min=8, max=1024)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        o = context.active_object
        scene = context.scene
        if o is None or o.c2.kind != 'PROBE':
            self.report({'ERROR'}, 'Make a Cars 2 light probe active')
            return {'CANCELLED'}
        root = _root(context)
        cam_data = bpy.data.cameras.new('C2ProbeCam')
        cam_data.angle = math.pi / 2
        cam = bpy.data.objects.new('C2ProbeCam', cam_data)
        scene.collection.objects.link(cam)
        prev = (scene.camera, scene.render.resolution_x, scene.render.resolution_y,
                scene.render.resolution_percentage, scene.render.filepath, scene.render.image_settings.file_format)
        scene.camera = cam
        scene.render.resolution_x = scene.render.resolution_y = self.size
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        faces = []
        tmp = tempfile.mkdtemp()
        try:
            # one 90-degree render per cube face, camera axes set from the
            # face's game-space forward/up (Blender cameras look down -Z)
            pos = o.matrix_world.translation
            R = root.matrix_world.to_3x3()
            for k, (fwd, up) in enumerate(CUBE_FACES):
                f = (R @ Vector(fwd)).normalized()
                u = (R @ Vector(up)).normalized()
                z = -f
                x = u.cross(z).normalized()
                y = z.cross(x)
                m = Matrix((x, y, z)).transposed().to_4x4()
                m.translation = pos
                cam.matrix_world = m
                path = os.path.join(tmp, 'face%d.png' % k)
                scene.render.filepath = path
                bpy.ops.render.render(write_still=True)
                img = bpy.data.images.load(path)
                px = np.empty(self.size * self.size * 4, np.float32)
                img.pixels.foreach_get(px)
                faces.append(px.reshape(self.size, self.size, 4)[::-1].copy())
                bpy.data.images.remove(img)
            data = dds.write_cube(faces)
            out = os.path.join(os.path.dirname(bpy.path.abspath(scene.c2.oct_path)),
                               '%s.oct.textures' % scene.c2.world_name, 'baked_probes')
            os.makedirs(out, exist_ok=True)
            fn = os.path.join(out, '%s.dds' % (o.c2.probe_name or o.name))
            with open(fn, 'wb') as fh:
                fh.write(data)
        finally:
            (scene.camera, scene.render.resolution_x, scene.render.resolution_y,
             scene.render.resolution_percentage, scene.render.filepath, scene.render.image_settings.file_format) = prev
            bpy.data.objects.remove(cam)
            bpy.data.cameras.remove(cam_data)
        tex = next((t for t in scene.c2.textures if t.index == o.c2.probe_texture), None)
        if tex is not None:
            tex.replace = fn
            self.report({'INFO'}, 'Cube map baked to %s (replaces texture "%s" on export)' % (fn, tex.name))
        else:
            self.report({'WARNING'}, 'Cube map baked to %s, but the probe has no texture to replace' % fn)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# meridian
# ----------------------------------------------------------------------

class C2_OT_play_choreo_on_trigger(bpy.types.Operator):
    """Play a choreography from a walk-in trigger."""
    bl_idname = 'c2.play_choreo_on_trigger'
    bl_label = 'Play Choreography On Trigger'
    bl_description = ('Make the active walk-in trigger play a choreography: type the choreography name, pick '
                      'where it is anchored, done. The Meridian graph is wired on export')
    bl_options = {'REGISTER', 'UNDO'}
    choreography: StringProperty(name='Choreography', default='',
                                 description='e.g. ExplosionFireyB, or choreographies/ExplosionFireyB/ExplosionFireyB')
    event: EnumProperty(name='Play On', items=[('ENTER', 'Enter', ''), ('EXIT', 'Exit', '')], default='ENTER')
    anchor: StringProperty(name='Anchor Name', default='ANC_00',
                           description='Anchor (transform reference) name used inside the choreography')
    anchor_mode: EnumProperty(name='Anchor At', items=[
        ('NEW', 'New Locator At Trigger', 'Create a locator at the trigger (move it afterwards)'),
        ('ACTIVE_LOCATOR', 'Selected Locator', 'Use the other selected object (must be a Meridian locator)'),
        ('NONE', 'No Anchor', 'Choreography plays in its own space')], default='NEW')
    loop: BoolProperty(name='Loop', default=False)
    hide_gameplay: BoolProperty(name='Hide Gameplay Elements', default=False)

    def invoke(self, context, event):
        if not is_trigger(context.scene, context.active_object):
            self.report({'ERROR'}, 'The active object is not a walk-in trigger (no WalkInTrigger in the .ap)')
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        sc = context.scene.c2
        trig = context.active_object
        if not self.choreography.strip():
            self.report({'ERROR'}, 'Type a choreography name')
            return {'CANCELLED'}
        # only the list entry is made here; exporter.apply_choreos builds the
        # Meridian nodes and links (choreography, anchor, trigger impulse)
        c = sc.choreos.add()
        base = os.path.basename(meridian.choreo_path(self.choreography))
        c.name = c.authored = 'Cho_%s_%s' % (trig.name, base)
        c.choreography_name = meridian.choreo_path(self.choreography)
        c.loop = self.loop
        c.hide_gameplay_elements = self.hide_gameplay
        t = c.triggers.add()
        t.obj = trig
        t.event = self.event
        loc = None
        if self.anchor_mode == 'NEW':
            loc = make_locator(context, 'LOC_%s' % c.authored, trig)
        elif self.anchor_mode == 'ACTIVE_LOCATOR':
            loc = next((o for o in context.selected_objects if o != trig and o.c2.meridian == 'LOCATOR'), None)
            if loc is None:
                self.report({'WARNING'}, 'No selected Meridian locator; anchor left empty')
        if self.anchor_mode != 'NONE':
            a = c.anchors.add()
            a.name = self.anchor
            a.locator = loc
        sc.active_choreo = len(sc.choreos) - 1
        self.report({'INFO'}, '%s will play %s (written on export)' % (trig.name, c.choreography_name))
        return {'FINISHED'}


class C2_OT_choreo_add(bpy.types.Operator):
    """Add a new choreography to the list (wired into the Meridian graph on export)"""
    bl_idname = 'c2.choreo_add'
    bl_label = 'New Choreography'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        c = sc.choreos.add()
        c.name = c.authored = 'Choreography_New%d' % len(sc.choreos)
        a = c.anchors.add()
        a.name = 'ANC_00'
        sc.active_choreo = len(sc.choreos) - 1
        return {'FINISHED'}


class C2_OT_choreo_remove(bpy.types.Operator):
    """Remove a choreography added in Blender."""
    bl_idname = 'c2.choreo_remove'
    bl_label = 'Remove New Choreography'
    bl_description = 'Remove a choreography added in Blender (existing ones can be edited but not deleted)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        if 0 <= sc.active_choreo < len(sc.choreos):
            if sc.choreos[sc.active_choreo].uuid:
                self.report({'ERROR'}, 'Choreographies from the game file cannot be removed; '
                                       'point them at another file or detach their triggers in-game logic instead')
                return {'CANCELLED'}
            sc.choreos.remove(sc.active_choreo)
            sc.active_choreo = max(0, sc.active_choreo - 1)
        return {'FINISHED'}


class C2_OT_choreo_anchor_add(bpy.types.Operator):
    """Add an anchor to the active choreography (the active locator, if one is selected)"""
    bl_idname = 'c2.choreo_anchor_add'
    bl_label = 'Add Anchor'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        c = sc.choreos[sc.active_choreo]
        a = c.anchors.add()
        a.name = 'ANC_%02d' % (len(c.anchors) - 1)
        o = context.active_object
        if o is not None and o.c2.meridian == 'LOCATOR':
            a.locator = o
        return {'FINISHED'}


class C2_OT_choreo_anchor_remove(bpy.types.Operator):
    """Remove an anchor added in Blender"""
    bl_idname = 'c2.choreo_anchor_remove'
    bl_label = 'Remove Anchor'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        sc = context.scene.c2
        c = sc.choreos[sc.active_choreo]
        if c.anchors[self.index].ref_uuid:
            self.report({'ERROR'}, 'Anchors from the game file can be re-pointed but not removed')
            return {'CANCELLED'}
        c.anchors.remove(self.index)
        return {'FINISHED'}


class C2_OT_choreo_trigger_add(bpy.types.Operator):
    """Make the active trigger start the choreography."""
    bl_idname = 'c2.choreo_trigger_add'
    bl_label = 'Add Active Trigger'
    bl_description = 'Play the active choreography when the active walk-in trigger is entered'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene.c2
        o = context.active_object
        if not is_trigger(context.scene, o):
            self.report({'ERROR'}, 'Active object is not a walk-in trigger')
            return {'CANCELLED'}
        t = sc.choreos[sc.active_choreo].triggers.add()
        t.obj = o
        return {'FINISHED'}


class C2_OT_choreo_trigger_remove(bpy.types.Operator):
    """Stop a trigger from starting the active choreography"""
    bl_idname = 'c2.choreo_trigger_remove'
    bl_label = 'Remove Trigger'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        sc = context.scene.c2
        sc.choreos[sc.active_choreo].triggers.remove(self.index)
        return {'FINISHED'}


class C2_OT_select_object(bpy.types.Operator):
    """Select and show this object"""
    bl_idname = 'c2.select_object'
    bl_label = 'Select'
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty()

    def execute(self, context):
        o = bpy.data.objects.get(self.name)
        if o is None:
            return {'CANCELLED'}
        for s in context.selected_objects:
            s.select_set(False)
        o.hide_set(False)
        o.select_set(True)
        context.view_layer.objects.active = o
        return {'FINISHED'}


# ----------------------------------------------------------------------
# actors (.ap)
# ----------------------------------------------------------------------

class C2_OT_actor_kv_add(bpy.types.Operator):
    """Add a key = value line to the active object's actor (.ap)"""
    bl_idname = 'c2.actor_kv_add'
    bl_label = 'Add Property'
    bl_options = {'REGISTER', 'UNDO'}
    key: StringProperty(name='Key')
    value: StringProperty(name='Value')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        a = actor_for(context.scene, context.active_object)
        if a is None:
            return {'CANCELLED'}
        kv = a.kvs.add()
        kv.key, kv.value = self.key, self.value
        return {'FINISHED'}


class C2_OT_actor_kv_remove(bpy.types.Operator):
    """Remove the selected key = value line from the active object's actor (.ap)"""
    bl_idname = 'c2.actor_kv_remove'
    bl_label = 'Remove Property'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        a = actor_for(context.scene, context.active_object)
        if a is None or not len(a.kvs):
            return {'CANCELLED'}
        a.kvs.remove(a.active_kv)
        a.active_kv = max(0, a.active_kv - 1)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# materials
# ----------------------------------------------------------------------

class C2_OT_material_from_template(bpy.types.Operator):
    """Turn the active material into a Cars 2 material."""
    bl_idname = 'c2.material_from_template'
    bl_label = 'Make Cars 2 Material'
    bl_description = ('Give the active Blender material Cars 2 shader settings copied from a world material; '
                      'its Image Texture becomes the diffuse map on export')
    bl_options = {'REGISTER', 'UNDO'}
    template: StringProperty(name='Template Material')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop_search(self, 'template', bpy.data, 'materials')

    def execute(self, context):
        o = context.active_object
        mat = o.active_material if o else None
        src = bpy.data.materials.get(self.template)
        src = src.c2.variant_of or src if src is not None else None
        # a world material, or one brought in with Import Materials from World
        # (also through an Auto-Assign copy of it)
        library = src.get('c2_library_entry') if src is not None else None
        if mat is None or src is None or (src.c2.index < 0 and not library):
            self.report({'ERROR'}, 'Pick a Cars 2 material of this world (or one imported from another world) '
                                   'as the template')
            return {'CANCELLED'}
        if mat == src:
            self.report({'ERROR'}, 'The template is the active material itself')
            return {'CANCELLED'}
        c = mat.c2
        c.index = -1
        c.template = src.c2.index if src.c2.index >= 0 else src.c2.template
        c.effect = src.c2.effect
        c.render_caps = src.c2.render_caps
        c.props.clear()
        for p in src.c2.props:
            q = c.props.add()
            q.name, q.ptype, q.datatype, q.size, q.fvalue, q.ivalue = p.name, p.ptype, p.datatype, p.size, p.fvalue, p.ivalue
        c.textures.clear()
        for k in ('c2_library_entry', 'c2_library'):
            if k in mat:
                del mat[k]
        if library:
            # its own entry and textures go with it (water keeps its wave and reflection maps)
            mat['c2_library_entry'] = library
            mat['c2_library'] = src.get('c2_library', '')
            mat['c2_auto_from'] = src.name        # a user of it, not another import candidate
            for t in src.c2.textures:
                q = c.textures.add()
                q.usage, q.texture = t.usage, t.texture
        mat['c2_world'] = context.scene.c2.world_name
        mat['c2_platform'] = context.scene.c2.platform
        if mat['c2_platform'] == 'ARCADE':
            from . import fxnodes
            try:
                fxnodes.rebuild_material(context.scene, mat)
            except Exception as e:      # viewport only
                print('Cars 2 Scene: viewport rebuild of %s failed: %s' % (mat.name, e))
        return {'FINISHED'}


def arcade_problems(scene, root):
    """Effects the game doesn't have, and baked lighting a part's effect expects but its mesh lacks."""
    import arcade
    assets = common.game_assets(scene)
    have_fx = arcade.effects_dir(assets) is not None
    out, seen = [], set()
    for o in common.descendants(root):
        if o.type != 'MESH' or o.c2.kind not in ('GEOMETRY', 'NONE') or o.get('c2_helper'):
            continue
        for slot in o.material_slots:
            m = slot.material
            if m is None:
                continue
            base = m.c2.variant_of or m
            eff = base.c2.effect
            if not eff:
                continue
            if have_fx and eff not in seen and not arcade.effect_exists(assets, eff):
                out.append('%s: effect %s is not in assets/effects (the game cannot load it)' % (base.name, eff))
            seen.add(eff)
            mode = arcade.baked_mode(eff)
            if mode == 'LIGHTMAP' and 'lightmapUV' not in o.data.uv_layers:
                out.append('%s: lightmapped material %s but no lightmapUV UV map (Bake Lightmaps makes one)'
                           % (o.name, base.name))
            elif mode == 'LIGHTMAP' and m.c2.lightmap < 0 and not m.get('c2_new_lightmap'):
                out.append('%s: %s has no lightmap yet (exported with a neutral one) - Bake Lightmaps'
                           % (o.name, base.name))
            elif mode == 'VERTEX' and 'VertexBaked' not in o.data.color_attributes:
                out.append('%s: vertex-lit material %s but no VertexBaked colours' % (o.name, base.name))
    if not have_fx:
        out.append('Game Assets Folder not found: effects could not be checked (set it in the Meridian or '
                   'Baked Lighting panel)')
    return out


class C2_OT_validate(bpy.types.Operator):
    """Check the scene for export problems."""
    bl_idname = 'c2.validate'
    bl_label = 'Check Scene'
    bl_description = 'Look for things the export would reject or that the game cannot load'

    def execute(self, context):
        root = _root(context)
        if root is None:
            return {'CANCELLED'}
        problems = []
        for o in common.descendants(root):
            if o.type == 'MESH' and o.c2.kind in ('GEOMETRY', 'NONE'):
                if not o.material_slots or any(s.material is None for s in o.material_slots):
                    problems.append('%s: empty material slot (falls back to the template material)' % o.name)
                if o.c2.kind == 'GEOMETRY' and not o.data.uv_layers and not o.get('c2_helper'):
                    problems.append('%s: no UV map' % o.name)
            if o.c2.kind == 'COLLISION' and o.type == 'MESH' and len(o.data.vertices) > 65535:
                problems.append('%s: collision mesh over 65535 vertices' % o.name)
            if o.c2.kind == 'COLLISION' and common.node_parent(o, root) in (None, root):
                problems.append('%s: collision not attached to a scene node (it will not move with anything)' % o.name)
        if context.scene.c2.platform == 'ARCADE':
            problems += arcade_problems(context.scene, root)
        for c in context.scene.c2.choreos:
            if not c.choreography_name:
                problems.append('choreography %s has no file' % c.authored)
            if not (c.play_on_start or len(c.triggers) or c.drivers):
                problems.append('choreography %s is never started: tick Play On Level Start or add a trigger'
                                % c.authored)
            for a in c.anchors:
                if a.locator is None and not a.ref_uuid:
                    problems.append('choreography %s anchor %s has no locator' % (c.authored, a.name))
        txt = bpy.data.texts.get('Cars2SceneCheck') or bpy.data.texts.new('Cars2SceneCheck')
        txt.clear()
        txt.write('\n'.join(problems) or 'No problems found.')
        if problems:
            show_text(context, txt)
        self.report({'WARNING'} if problems else {'INFO'},
                    '%d issue(s) - listed in the window that opened' % len(problems) if problems
                    else 'No problems found')
        return {'FINISHED'}


def show_text(context, txt):
    """Show a report text: in an open Text Editor if there is one, else in a new window."""
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces[0].text = txt
                area.tag_redraw()
                return
    try:
        bpy.ops.wm.window_new()
        win = context.window_manager.windows[-1]
        area = max(win.screen.areas, key=lambda a: a.width * a.height)
        area.type = 'TEXT_EDITOR'
        area.spaces[0].text = txt
        area.spaces[0].show_word_wrap = True
    except Exception as e:   # background mode / no window
        print('Cars 2 Scene: could not open the report window (%s)' % e)


class C2_OT_show_report(bpy.types.Operator):
    """Open a report text."""
    bl_idname = 'c2.show_report'
    bl_label = 'Show Report'
    bl_description = 'Open a Cars 2 report text in a Text Editor window'
    name: StringProperty()

    def execute(self, context):
        txt = bpy.data.texts.get(self.name)
        if txt is None:
            return {'CANCELLED'}
        show_text(context, txt)
        return {'FINISHED'}


_classes = (C2_OT_add_to_scene, C2_OT_remove_from_scene, C2_OT_add_locator, C2_OT_collision_from_mesh, C2_OT_collision_primitive,
            C2_OT_attach_collision, C2_OT_set_surface, C2_OT_select_surface, C2_OT_toggle_collision,
            C2_OT_rig_from_lights, C2_OT_rig_lights_from_selection, C2_OT_lightset_from_selection,
            C2_OT_assign_lightset, C2_OT_bake_probe,
            C2_OT_play_choreo_on_trigger, C2_OT_choreo_add, C2_OT_choreo_remove, C2_OT_choreo_anchor_add,
            C2_OT_choreo_anchor_remove, C2_OT_choreo_trigger_add, C2_OT_choreo_trigger_remove,
            C2_OT_select_object, C2_OT_actor_kv_add, C2_OT_actor_kv_remove, C2_OT_material_from_template,
            C2_OT_validate, C2_OT_show_report)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
