"""Property groups stored in the .blend (everything the exporter needs besides
the original game files, which are re-read from disk at export time).

Where each group is attached (see register()):
    Object.c2       C2ObjectProps    what the object is in the game (kind) and
                                     which original record it came from
    Mesh.c2col      C2ShapeProps     collision shape settings of a mesh
    Light.c2        C2LightProps     game light fields
    Material.c2     C2MaterialProps  effect, parameters, texture slots
    Scene.c2        C2SceneProps     the imported world: paths, textures,
                                     light rigs, choreographies, gameplay ...
"index" fields (node_index, col_index, index ...) point back to the record
in the source file; -1 means "new - create it on export".
Functions named _xxx_update are called by Blender whenever that property
changes (live viewport updates)."""
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, IntVectorProperty, PointerProperty,
                       StringProperty)

# What an object stands for. The exporter decides what to write from this:
# e.g. GEOMETRY objects become scene Geometry nodes, COLLISION objects become
# collision instances, PREVIEW objects are skipped.
OBJECT_KINDS = [
    ('NONE', 'None', 'Not part of a Cars 2 scene'),
    ('ROOT', 'Scene Root', 'Root of an imported Cars 2 world (game Y-up space)'),
    ('NODE', 'Transform', 'Scene tree Transform node'),
    ('GEOMETRY', 'Geometry', 'Scene tree Geometry node (its SubGeometry parts are the material groups)'),
    ('LIGHT', 'Light', 'Scene tree Light node'),
    ('CAMERA', 'Camera', 'Scene tree Camera node'),
    ('COLLISION', 'Collision Instance', 'CollisionInstancePool entry'),
    ('COLSHAPE', 'Compound Child Shape', 'Child shape of a compound collision instance'),
    ('PROBE', 'Light Probe', 'LightProbePool entry'),
    ('AI_TRACK', 'AI Track Graph', 'The AI track graph (.oct Track pools): vertices = nodes, edges = links'),
    ('AI_MARKER', 'AI Marker', 'TrackSplineMarkup: where AI cars hop, sidestep or drift'),
    ('PATH', 'Path (.dbl)', 'A named path in the world .dbl (lanes, TrackA lap spline, cameras, bridges)'),
    ('NAVMESH', 'Nav Mesh', 'Arena nav mesh (.oct NavMeshPool entry)'),
    ('PREVIEW', 'Preview', 'Editor display only (grid slots, track edges); never exported'),
]

MARKER_TYPES = [
    ('0', 'Bunny Hop', 'AI hops here'),
    ('1', 'Sidestep Left', ''),
    ('2', 'Sidestep Right', ''),
    ('3', 'Drift Left', 'Start drifting left'),
    ('4', 'Drift Left End', ''),
    ('5', 'Drift Right', 'Start drifting right'),
    ('6', 'Drift Right End', ''),
    ('7', 'Type 7', 'Meaning not decoded (8 uses in the shipping worlds)'),
]

SHAPE_TYPES = [
    ('TriangleList', 'Triangle Mesh', 'Bullet triangle mesh stored in the .btd'),
    ('Box', 'Box', 'Box primitive (half extents from the mesh bounds)'),
    ('Sphere', 'Sphere', 'Sphere primitive (radius from the mesh bounds)'),
    ('Cylinder', 'Cylinder', 'Cylinder primitive along local Y'),
    ('ConvexHull', 'Convex Hull', 'Convex hull of the mesh vertices'),
    ('Capsule', 'Capsule', 'Capsule primitive'),
    ('Cone', 'Cone', 'Cone primitive'),
]

MERIDIAN_KINDS = [
    ('NONE', 'None', ''),
    ('LOCATOR', 'Locator', 'Meridian LocatorNode bound to this Transform'),
    ('INSTANCE', 'Instance', 'Meridian InstanceNode bound to this Geometry'),
]


class C2ObjRef(bpy.types.PropertyGroup):
    """A reference to an object (used in lists, e.g. the lights of a light rig)."""
    obj: PointerProperty(type=bpy.types.Object)


class C2KV(bpy.types.PropertyGroup):
    """One key = value line (actor properties, extra light fields, node settings)."""
    key: StringProperty(name='Key')
    value: StringProperty(name='Value')


class C2ObjectProps(bpy.types.PropertyGroup):
    """Object.c2: the object's role in the world. Only the fields of its kind matter."""
    kind: EnumProperty(items=OBJECT_KINDS, default='NONE')
    node_index: IntProperty(name='Node Index', default=-1,
                            description='SceneTreeNodePool index in the source file (-1 = new, created on export)')
    detached: BoolProperty(name='Actor Root', default=False,
                           description='Node has no scene parent (actor roots such as triggers are spawned from the .ap)')
    display_layer: IntProperty(name='Display Layer', default=-1)
    visible: BoolProperty(name='Visible', default=True)
    # common.mesh_hash at import; unchanged on export = original mesh data is copied as is
    mesh_hash: StringProperty()
    lightset: IntProperty(name='Light Set', default=-1, min=-1,
                          description='LightSetPool index for all parts of this mesh (-1 keeps each part\'s own)')
    # meridian
    meridian: EnumProperty(name='Meridian', items=MERIDIAN_KINDS, default='NONE')
    mer_index: IntProperty(default=-1)
    # collision instance
    friction: FloatProperty(name='Friction', default=0.8, min=0.0, max=1.0)
    restitution: FloatProperty(name='Restitution', default=0.5, min=0.0, max=1.0)
    col_index: IntProperty(default=-1)
    # light probe
    probe_index: IntProperty(default=-1)
    probe_type: EnumProperty(name='Probe Type', items=[('Environment', 'Environment', 'Reflection cube map probe'),
                                                       ('Volume', 'Volume', 'Volume lighting probe')],
                             default='Environment')
    probe_name: StringProperty(name='Probe Name')
    probe_texture: IntProperty(name='Texture', default=-1, description='TexturePool index of the probe cube map')
    probe_node: PointerProperty(type=bpy.types.Object, name='Node')
    probe_density: IntVectorProperty(name='Density', size=3, default=(32, 10, 32), min=1)
    probe_fade: StringProperty(name='Fade Type', default='Outside')
    probe_outside: FloatVectorProperty(name='Outside Size', size=3, default=(20.0, 20.0, 20.0))
    probe_scene: BoolProperty(name='Scene Probe', default=False)
    probe_luminance: FloatProperty(name='Average Luminance', default=0.0)
    probe_has_luminance: BoolProperty(default=False)
    probe_direct_sh: FloatVectorProperty(name='Direct SH', size=30)
    probe_has_sh: BoolProperty(default=False)
    # Arcade volume probes: DirectSH is a Density.x*y*z grid of 27-float cells,
    # kept in the object's 'c2_direct_sh' custom property
    probe_inside: FloatProperty(name='Inside Size', default=0.9, min=0.0, max=1.0)
    probe_has_inside: BoolProperty(default=False)
    # AI marker (TrackSplineMarkup)
    marker_type: EnumProperty(name='Marker', items=MARKER_TYPES, default='0')
    marker_track: IntProperty(name='Track', default=0, min=0, max=2,
                              description='Which track it belongs to: 0 = A, 1 = B, 2 = C')
    marker_distance: FloatProperty(name='Lap Distance', default=0.0,
                                   description='Distance along the lap; recomputed on export when the marker moved')


class C2ShapeProps(bpy.types.PropertyGroup):
    """On collision mesh data (shared meshes == shared shapes)."""
    shape_type: EnumProperty(name='Shape', items=SHAPE_TYPES, default='TriangleList')
    shape_index: IntProperty(default=-1)        # CollisionShapePool index (-1 = new)
    serialize_index: IntProperty(default=-1)    # its entry in the .btm/.btd
    collision_layer: IntProperty(name='Collision Layer', default=0)
    surface_type: IntProperty(name='Surface Type', default=0,
                              description='SurfaceType of primitive shapes (triangle meshes store it per face)')
    btm_type: IntProperty(default=1)            # .btm entry type / alignment, kept as read
    btm_align: IntProperty(default=16)
    # btcol.geometry_hash at import: unchanged geometry keeps its original BVH
    geo_hash: StringProperty()
    extra_json: StringProperty(description='Original shape fields not otherwise edited')


def light_preview_energy(ld):
    """Blender light strength used to preview / bake a game light: a sun of game
    intensity I lights a facing surface with I (Blender divides by pi)."""
    import math
    c = ld.c2
    if ld.type == 'SUN':
        return c.intensity * math.pi
    if ld.type == 'SPOT' or c.light_type == 'Point':
        return c.intensity * 1000.0
    return 0.0  # ambient: baked through the world colour instead


def _intensity_update(self, context):
    """Keep the Blender light's strength in step with the game intensity."""
    ld = self.id_data
    if isinstance(ld, bpy.types.Light):
        ld.energy = light_preview_energy(ld)


class C2LightProps(bpy.types.PropertyGroup):
    """Light.c2: the game light's own fields (written back on export)."""
    light_type: EnumProperty(name='Game Type', items=[('Directional', 'Directional', ''), ('Spot', 'Spot', ''),
                                                      ('Point', 'Point', 'Arcade point light'),
                                                      ('Ambient', 'Ambient', '')], default='Directional')
    intensity: FloatProperty(name='Intensity', default=1.0, update=_intensity_update)
    shadow_color: FloatVectorProperty(name='Shadow Color', subtype='COLOR', size=3, default=(0, 0, 0), min=0)
    exclusion_type: IntProperty(name='Exclusion Type', default=0)
    ambient_shade: FloatProperty(name='Ambient Shade', default=0.45)
    decay_rate: IntProperty(name='Decay Rate', default=0)
    cone_angle: FloatProperty(name='Cone Angle', default=0.698, subtype='ANGLE')
    penumbra_angle: FloatProperty(name='Penumbra Angle', default=0.0, subtype='ANGLE')
    dropoff: FloatProperty(name='Dropoff', default=0.0)
    falloff_radius: StringProperty(name='Falloff Radius', default='10.')
    softness: StringProperty(name='Softness', default='0.')
    is_roaming: BoolProperty(name='Roaming', default=False)
    use_tertiary: BoolProperty(name='Use Tertiary', default=False)
    is_negative: BoolProperty(name='Negative', default=False)
    lm_normal: BoolProperty(name='Lightmap Normal', default=False)
    lm_specular: BoolProperty(name='Lightmap Specular', default=False)
    extra_kv: CollectionProperty(type=C2KV)


def _lightmap_update(self, context):
    """Show the newly picked lightmap in the viewport."""
    mat = self.id_data
    nd = mat.node_tree.nodes.get('C2_Lightmap') if mat.node_tree else None
    t = next((t for t in context.scene.c2.textures if t.index == self.lightmap), None)
    if nd is not None and t is not None and t.file:
        try:
            from .matnodes import set_data_image
            img = bpy.data.images.load(bpy.path.abspath(t.file), check_existing=True)
            set_data_image(img)
            nd.image = img
        except RuntimeError:
            pass


def _matprop_update(self, context):
    """A material parameter changed: update its value in Game Look."""
    from . import fxnodes
    fxnodes.on_param_changed(context.scene, self.id_data, self)


def _mattex_update(self, context):
    """A texture slot changed: rebuild the material's Game Look."""
    from . import fxnodes
    fxnodes.on_material_changed(context.scene, self.id_data)


def _effect_update(self, context):
    """The effect (shader) changed: add its parameters and rebuild."""
    from . import fxnodes
    fxnodes.on_material_changed(context.scene, self.id_data, effect_changed=True)


class C2MatProp(bpy.types.PropertyGroup):
    """One material parameter (a MaterialPool PropertyEntry), e.g. specularShineA1."""
    name: StringProperty()
    ptype: StringProperty(default='Float')       # Float / Int / Bool
    datatype: IntProperty(default=0)             # 0 scalar float, 2 vec3, 3 vec4, 10 int/bool
    size: IntProperty(default=1)
    fvalue: FloatVectorProperty(size=4, update=_matprop_update)
    ivalue: IntProperty(update=_matprop_update)
    # a parameter the effect has but the game material doesn't list (written on
    # export only when it differs from the effect's default)
    extra: BoolProperty(default=False)
    fdefault: FloatVectorProperty(size=4)
    # colour-picker view of fvalue[:3] (edits go through fvalue's live update)
    color: FloatVectorProperty(name='Colour', subtype='COLOR', size=3, soft_min=0.0, soft_max=1.0,
                               get=lambda s: tuple(s.fvalue[:3]),
                               set=lambda s, v: setattr(s, 'fvalue', (v[0], v[1], v[2], s.fvalue[3])))
    # the same for colours the game stores sRGB-encoded (Gamma parameters)
    color_gamma: FloatVectorProperty(name='Colour', subtype='COLOR_GAMMA', size=3, soft_min=0.0, soft_max=1.0,
                                     get=lambda s: tuple(s.fvalue[:3]),
                                     set=lambda s, v: setattr(s, 'fvalue', (v[0], v[1], v[2], s.fvalue[3])))


class C2MatTex(bpy.types.PropertyGroup):
    """One texture slot (a MaterialPool TextureEntry): usage name -> world texture."""
    usage: StringProperty()
    texture: IntProperty(default=0, description='TexturePool index (-1 = none)', update=_mattex_update)
    extra: BoolProperty(default=False)


class C2MaterialProps(bpy.types.PropertyGroup):
    """Material.c2: the game material. Custom properties used alongside it:
    'c2_world' (world it belongs to), 'c2_auto_from' (Auto-Assign template),
    'c2_library' / 'c2_library_entry' (imported from another world)."""
    index: IntProperty(default=-1, description='MaterialPool index (-1 = new, cloned from Template on export)')
    template: IntProperty(default=-1, description='MaterialPool index a new material is cloned from')
    effect: StringProperty(name='Effect', update=_effect_update,
                           description='The shader (assets/effects/effects/<effect>.tfx)')
    render_caps: IntProperty(name='RenderCaps', default=64)
    props: CollectionProperty(type=C2MatProp)
    textures: CollectionProperty(type=C2MatTex)
    # Arcade per-instance textures (SubGeometryLit InstanceTextureEntries):
    # one Blender material per (game material, lightmap) pair
    variant_of: PointerProperty(type=bpy.types.Material, name='Game Material',
                                description='The game material this lightmap variant exports as')
    lightmap: IntProperty(name='Lightmap', default=-1, min=-1, update=lambda self, ctx: _lightmap_update(self, ctx),
                          description='TexturePool index of the lightMap1 texture of the parts using this material')
    envmap_inst: IntProperty(name='Instance Env Map', default=-1, min=-1,
                             description='TexturePool index of the envMapInstance cube map (-1 = none)')
    # a character's material (Cars 2 Toolkit): texture indices are the character's
    # own TexturePool, listed on this object (Object.c2tex), not the world's
    owner: PointerProperty(type=bpy.types.Object, name='Character',
                           description="The character whose textures this material's slots point at")


class C2Texture(bpy.types.PropertyGroup):
    """One world texture (TexturePool entry). Indices past the original pool
    are new textures, written to the .oct on export in index order."""
    index: IntProperty(default=-1)
    source_path: StringProperty()
    ttype: StringProperty(default='2d')
    info: StringProperty()
    file: StringProperty(name='Extracted File', subtype='FILE_PATH')
    replace: StringProperty(name='Replace With', subtype='FILE_PATH', update=lambda s, c: _texture_replace_update(s, c),
                            description='.dds (copied verbatim) or any image Blender can read (written as uncompressed DDS)')
    srgb: BoolProperty(name='sRGB Colour', default=True, update=lambda s, c: _texture_replace_update(s, c),
                       description='The game decodes this texture from sRGB when sampling it (colour maps). '
                                   'Off for data: lightmaps, normal maps, reflection cube maps')


def _texture_replace_update(self, context):
    """A texture's file or sRGB flag changed: refresh every material using it
    (a world texture, or one of a character's own: Object.c2tex)."""
    from . import fxnodes, matio
    try:
        matio.refresh_info(self)        # the list's size / format line follows the new file
    except (OSError, RuntimeError):
        pass
    if isinstance(self.id_data, bpy.types.Object):
        fxnodes.on_owner_texture_replaced(context.scene, self.id_data, self.index)
    else:
        fxnodes.on_texture_replaced(context.scene, self.index)


class C2LightRig(bpy.types.PropertyGroup):
    """A light rig (LightRigPool): spherical-harmonic lighting for moving things (see core/shlight)."""
    index: IntProperty(default=-1)
    rig_name: StringProperty(name='Rig Name')
    type_name: StringProperty(name='Type', default='PrimarySceneRig')
    position: FloatVectorProperty(name='Position', size=3)
    sh: FloatVectorProperty(name='SH', size=27)
    sh_order: IntProperty(default=3)
    scene_rig: IntProperty(default=1)
    lights: CollectionProperty(type=C2ObjRef)


class C2LightSet(bpy.types.PropertyGroup):
    """A light set (LightSetPool): which lights affect the mesh parts that use it."""
    index: IntProperty(default=-1)
    lights: CollectionProperty(type=C2ObjRef)


class C2Anchor(bpy.types.PropertyGroup):
    """A choreography anchor slot (ChoreographyTransformReference) and the locator placing it."""
    name: StringProperty(name='Anchor', default='ANC_00')
    ref_index: IntProperty(default=-1)
    ref_uuid: StringProperty()
    locator: PointerProperty(type=bpy.types.Object, name='Locator',
                             description='Scene object (with a Meridian locator) that places this anchor')
    source: StringProperty(description='What feeds the anchor in the source file')
    resolved: StringProperty(description='Where the game puts the anchor at run time (traced through the '
                                         'Meridian graph): an object, or a racer')


class C2ChoreoActor(bpy.types.PropertyGroup):
    """A character the choreography animates (name = its binding, e.g. materbg_00)."""
    source: StringProperty(name='Character')
    model: StringProperty(name='Model')
    spawn: BoolProperty(name='Spawn Actor', default=True,
                        description='Create a world actor for this character on export (the choreography '
                                    'moves it; without one the character never appears)')
    exists: BoolProperty(default=False)


def _choreo_name_update(self, context):
    """Choreography file changed: re-list the characters it needs."""
    from . import gameplay
    gameplay.refresh_choreo_actors(context.scene, self)


class C2ChoreoTrigger(bpy.types.PropertyGroup):
    """A walk-in trigger that plays the choreography on enter/exit."""
    obj: PointerProperty(type=bpy.types.Object, name='Trigger')
    event: EnumProperty(name='On', items=[('ENTER', 'Enter', 'Play when something enters the trigger'),
                                          ('EXIT', 'Exit', 'Play when it leaves the trigger')], default='ENTER')
    exists: BoolProperty(default=False)


class C2Choreo(bpy.types.PropertyGroup):
    """A Choreography node of the world's Meridian graph (or a new one)."""
    node_index: IntProperty(default=-1)
    uuid: StringProperty()
    authored: StringProperty(name='Name')
    choreography_name: StringProperty(name='Choreography', update=_choreo_name_update,
                                      description='Choreography file, e.g. "ExplosionFireyB" or '
                                                  '"choreographies/ExplosionFireyB/ExplosionFireyB"')
    loop: BoolProperty(name='Loop')
    visible_before: BoolProperty(name='Visible Before')
    visible_after: BoolProperty(name='Visible After')
    enableAsyncLoad: BoolProperty(name='Async Load')
    enableMusicSync: BoolProperty(name='Music Sync')
    hide_gameplay_elements: BoolProperty(name='Hide Gameplay Elements')
    use_master: BoolProperty(name='Use Master')
    fullScreen_CHO: BoolProperty(name='Full Screen')
    anchors: CollectionProperty(type=C2Anchor)
    triggers: CollectionProperty(type=C2ChoreoTrigger)
    drivers: StringProperty(description='Existing graph nodes that play this choreography')
    play_on_start: BoolProperty(name='Play On Level Start',
                                description='Start this choreography when the level loads (a Catalyst node, '
                                            'how the shipping worlds start their background choreographies)')
    start_exists: BoolProperty(default=False)
    actors: CollectionProperty(type=C2ChoreoActor)


def _grid_update(self, context):
    """A grid setting changed: redraw the starting-grid preview."""
    from . import gameplay
    gameplay.refresh_grid_preview(context.scene, self)


class C2Grid(bpy.types.PropertyGroup):
    """Meridian StartingGrid node (slot layout: sdaemon FUN_0056c9a0)."""
    uuid: StringProperty()
    track: IntProperty(name='Track', default=0, min=0, max=2, description='0 = A, 1 = B, 2 = C')
    row_count: IntProperty(name='Row Count', default=6, min=1, max=12, update=_grid_update,
                           description='Cars per row = 12 / Row Count')
    h_spacing: FloatProperty(name='Horizontal Spacing', default=6.0, update=_grid_update)
    v_spacing: FloatProperty(name='Vertical Spacing', default=8.0, update=_grid_update)
    stagger: FloatProperty(name='Row Stagger', default=0.0, update=_grid_update)
    custom23: BoolProperty(name='2-3 Rows', default=False, update=_grid_update)
    custom32: BoolProperty(name='3-2 Rows', default=False, update=_grid_update)
    locator: PointerProperty(type=bpy.types.Object, name='Grid Position', update=_grid_update)
    collect_locator: PointerProperty(type=bpy.types.Object, name='Collect Grid Position')


class C2SpawnList(bpy.types.PropertyGroup):
    """A named location list (TransformMarkup / ActorMarkup) the game reads."""
    kind: StringProperty()
    has_entry: BoolProperty()
    info: StringProperty()


class C2NodeSettings(bpy.types.PropertyGroup):
    """Plain fields of a gameplay Meridian node (takedown/collect/AI hint/arena ...)."""
    uuid: StringProperty()
    node_type: StringProperty()
    kvs: CollectionProperty(type=C2KV)
    active_kv: IntProperty()


class C2Actor(bpy.types.PropertyGroup):
    """One .ap section (an actor): name = section name, kvs = its key=value lines."""
    kvs: CollectionProperty(type=C2KV)
    active_kv: IntProperty()


# The two game builds store baked lighting differently (detected on import)
PLATFORMS = [
    ('PC', 'PC / Steam (Win32_Wii)', 'Baked light in ColorPrelight vertex colours'),
    ('ARCADE', 'Arcade (Win32 / Xbox 360)', 'Lightmaps (lightmapUV) or VertexBaked colours, multi-layer '
                                             'texture blending, volume light probes'),
]

VIEWPORT_LIGHTING = [
    ('BAKED', 'Baked x Blender Lights', 'Base colour = texture x baked lighting; Blender lights shade on top'),
    ('GAME', 'Game Look (unlit)', 'Emission = texture x baked lighting, like the game with no dynamic lights'),
    ('ALBEDO', 'Textures Only', 'Base colour = texture; only Blender lights shade (what the bakes use)'),
]


def _viewport_lighting_update(self, context):
    """Switch every material between the viewport lighting modes."""
    from . import matnodes, fxnodes
    matnodes.set_lighting_mode(self.viewport_lighting)
    vs = context.scene.view_settings
    # Game Look already applies the game's tone curve: show it untouched
    if self.viewport_lighting == 'GAME':
        if 'c2_prev_view' not in context.scene:
            context.scene['c2_prev_view'] = vs.view_transform
        vs.view_transform = 'Standard'
        fxnodes.sync_engine(context.scene)
        # sky domes sit thousands of units out (in the game they follow the camera)
        for scr in (context.window_manager.windows[0].screen,) if context.window_manager.windows else ():
            for area in scr.areas:
                if area.type == 'VIEW_3D':
                    sp = area.spaces[0]
                    sp.clip_end = max(sp.clip_end, 20000.0)
                    sp.clip_start = max(sp.clip_start, 0.1)
    elif 'c2_prev_view' in context.scene:
        try:
            vs.view_transform = context.scene['c2_prev_view']
        except TypeError:
            pass
        del context.scene['c2_prev_view']


def _inspect_update(self, context):
    """Switch the Game Look inspection colouring."""
    from . import fxnodes
    fxnodes.set_inspect(self.inspect)


def _assets_update(self, context):
    """Game Assets Folder set: switch an Arcade world to the game shaders."""
    import arcade
    from . import common, fxnodes, gamerender
    scene = context.scene
    fxnodes._contexts.clear()
    gamerender.RENDERER.clear()
    if self.platform == 'ARCADE' and self.root is not None and arcade.effects_dir(common.game_assets(scene)):
        scene.render.engine = 'C2_GAME'
        if self.viewport_lighting != 'GAME':
            self.viewport_lighting = 'GAME'
        fxnodes.sync_engine(scene)


def _game_redraw(self, context):
    """A Cars 2 Game render setting changed: redraw."""
    from . import gamerender
    gamerender.RENDERER.rebuild()


def _exposure_update(self, context):
    """Scene Brightness changed: push it to the shared engine values."""
    from . import fxnodes
    fxnodes.sync_engine(context.scene)


def _track_items(self, context):
    from . import trackvariant
    return trackvariant.items(self, context)


def _mode_items(self, context):
    from . import trackvariant
    return trackvariant.mode_items(self, context)


def _track_update(self, context):
    from . import trackvariant
    trackvariant.update(self, context)


def _collect_cb(fn, self, context):
    try:
        from . import collectgates      # the Scene Editor's (the Cars 2 Toolkit's copy of this file has none)
    except ImportError:
        return
    getattr(collectgates, fn)(self, context)


def _lap_update(self, context):
    try:
        from . import collectgates      # the Scene Editor's (the Cars 2 Toolkit's copy of this file has none)
    except ImportError:
        return
    collectgates.lap_update(self, context)


def _track_preview_changed(s, c):
    from . import gameplay
    gameplay._track_preview_changed(s, c)


class C2SceneProps(bpy.types.PropertyGroup):
    """Scene.c2: the imported world and every editor setting for it."""
    platform: EnumProperty(name='Game Version', items=PLATFORMS, default='PC',
                           description='Detected from the world on import')
    viewport_lighting: EnumProperty(name='Viewport Lighting', items=VIEWPORT_LIGHTING, default='BAKED',
                                    update=_viewport_lighting_update)
    inspect: EnumProperty(name='Inspect', items=[('OFF', 'Off', 'The game shading'),
                                                 ('SPECULAR', 'Shininess', 'Specular map x specular colour (yellow)'),
                                                 ('REFLECTION', 'Reflections', 'How much cube-map reflection shows (cyan)'),
                                                 ('TRANSPARENCY', 'Transparency', 'Blue = blended, orange = additive glow, '
                                                  'purple = multiply, green = cut-out, grey = opaque'),
                                                 ('BAKED', 'Baked Lighting', 'Magenta = lightmap, yellow = VertexBaked, '
                                                  'cyan = lit at run time'),
                                                 ('EMISSIVE', 'Glow', 'Emissive (self-lit) parts'),
                                                 ('EFFECT', 'Effect', 'One colour per effect (shader)')],
                          default='OFF', update=_inspect_update,
                          description='Game Look: colour surfaces by what their material does')
    game_shadows: BoolProperty(name='Shadows', default=True, update=_game_redraw,
                               description='Cars 2 Game render engine: dynamic shadows like the game (car and '
                                           'character shadows, shadow proxies, cloud shadows)')
    game_shadow_distance: FloatProperty(name='Shadow Distance', default=60.0, min=5.0, max=1000.0, update=_game_redraw,
                                        description="How far from the camera dynamic shadows reach (the game's "
                                                    "shadowDistance setting)")
    game_hires_shadows: BoolProperty(name='High-Res Shadows', default=False, update=_game_redraw,
                                     description="The game's -hiresshadows mode: 2048 px maps, 3 cascades")
    game_reflections: BoolProperty(name='Reflections', default=True, update=_game_redraw,
                                   description="Cars 2 Game render engine: the game's planar reflection (water, "
                                               "glossy floors), drawn from a mirror camera like the game does")
    game_motion_blur: BoolProperty(name='Motion Blur', default=True, update=_game_redraw,
                                   description="Cars 2 Game render engine: the game's motion blur (camera and "
                                               "moving previews) when the timeline advances a frame: playback "
                                               "and animation renders. A still view is never blurred")
    game_bloom: BoolProperty(name='Bloom', default=True, update=_game_redraw,
                             description="Cars 2 Game render engine: the game's bloom (high pass, Gaussian "
                                         "downsamples and blurs, from scene_settings' g_Bloom_ values)")
    game_auto_exposure: BoolProperty(name='Auto Exposure', default=True, update=_game_redraw,
                                     description="Cars 2 Game render engine: expose every view like the game's "
                                                 "auto-exposure settles on it (average log luminance, clamped to "
                                                 "the world's HDR min/max). Off: Scene Brightness")
    eevee_game_shaders: BoolProperty(name='Eevee Game Shaders', default=False,
                                     description="Also translate every material's game shader into Eevee nodes "
                                                 "(Material Preview / Eevee renders in Game Look). Slow to import and "
                                                 "to draw; the Cars 2 Game render engine doesn't need it")
    exposure_luminance: FloatProperty(name='Scene Brightness', default=0.0, min=0.0, soft_max=4.0,
                                      update=_exposure_update,
                                      description='Game Look exposure: the average scene luminance the game\'s '
                                                  'auto-exposure would settle on (clamped to the world\'s HDR '
                                                  'min/max). 0 = the middle of that range')
    oct_path: StringProperty(name='Source .oct', subtype='FILE_PATH',
                             description='The original world this scene was imported from. Every export re-reads '
                                         'it, so keep it outside the game folder and never copy an export over it')
    world_name: StringProperty(name='World')
    track_variant: EnumProperty(name='Track Variant', items=lambda s, c: _track_items(s, c),
                                update=lambda s, c: _track_update(s, c),
                                description='Show one route of the world: what the game shows and hides for it '
                                            '(bridges, detour walls, barriers, sky ...). All: every route at once')
    track_mode: EnumProperty(name='Mode', update=lambda s, c: _track_update(s, c),
                             items=lambda s, c: _mode_items(s, c),
                             description="Game mode to preview with the track: shows that mode's objects "
                                         "and hides the other modes' objects")
    collect_lap: EnumProperty(name='Lap', update=lambda s, c: _lap_update(s, c),
                              items=(('ALL', 'All Laps', "Every lap's Collect gates, rings and line at once"),
                                     ('1', 'Lap 1', 'Collect mode: the gates, rings and line of lap 1'),
                                     ('2', 'Lap 2', 'Collect mode: the gates, rings and line of lap 2'),
                                     ('3', 'Lap 3', 'Collect mode: the gates, rings and line of lap 3')),
                              description="Collect (Survival): show what the game shows on one lap - that lap's "
                                          "gates, the rings where the collect line crosses them and that lap's "
                                          "part of the line")
    path_outlines: BoolProperty(name='Path Outlines', default=True,
                                update=lambda s, c: _collect_cb('path_outlines_update', s, c),
                                description='Outline the .dbl paths (not the collect line laps) in light blue, with their '
                                            'names, so they are told apart from the collect lines and the AI paths')
    collect_show_lines: BoolProperty(name='Lap Lines', default=True, update=lambda s, c: _collect_cb('show_lines_update', s, c),
                                     description='Collect (Survival): draw each lap of the collect line in its own colour, '
                                                 'labelled (LineALap1 ...), a little apart so overlapping laps can be told apart')
    collect_gate_anim: BoolProperty(name='Animate Gates', default=False, update=lambda s, c: _collect_cb('anim_update', s, c),
                                    description="Collect (Survival): play the gates' hologram animation on the timeline "
                                                "(the side scanlines opening and closing). Off: still gates, nothing to "
                                                "update per frame (faster playback and editing)")
    root: PointerProperty(type=bpy.types.Object)
    export_dir: StringProperty(name='Export Folder', subtype='DIR_PATH')
    assets_dir: StringProperty(name='Game Assets Folder', subtype='DIR_PATH', update=_assets_update,
                               description='The game assets folder (holds effects/, choreographies/ and '
                                           'characters/). The game shaders are built from its effects. Found '
                                           'automatically when the world was imported from inside it')
    namespace: StringProperty(name='Meridian Namespace')
    has_mer: BoolProperty()
    has_ap: BoolProperty()
    has_col: BoolProperty()
    textures: CollectionProperty(type=C2Texture)
    active_texture: IntProperty()
    light_rigs: CollectionProperty(type=C2LightRig)
    active_rig: IntProperty()
    light_sets: CollectionProperty(type=C2LightSet)
    active_lightset: IntProperty()
    choreos: CollectionProperty(type=C2Choreo)
    active_choreo: IntProperty()
    # point-to-point race: the race ends when a player reaches this locator (finishpoint.py)
    finish_point: PointerProperty(type=bpy.types.Object, name='Finish Point',
                                  description='Locator where the race ends when a player gets within the radius '
                                              '(point-to-point tracks). Empty: the race ends on laps as usual')
    finish_radius: FloatProperty(name='Radius', default=25.0, min=1.0, max=500.0, unit='LENGTH',
                                 description='How close (metres, in 3D) a player has to get to the finish point')
    finish_mode: EnumProperty(name='Finish', default='LAP', items=[
        ('LAP', 'Lap Spline', 'The game finishes the race itself: Build Point-to-Point Lap reshapes the lap '
                              'spline so crossing the finish point completes the lap (placement stinger, real '
                              'finish times). Set Number of Laps to 1 in the game\'s settings'),
        ('MESSAGE', 'End Message', 'The world ends the race when a player gets within the radius (message '
                                   'EndTutorial: results at once, no stinger, finish times not exact). Works '
                                   'with any lap count')])
    p2p_start_line: PointerProperty(type=bpy.types.Object, name='Start Line',
                                    description='Where the cars pass the start line, when the grid sits off the '
                                                'lap line (empty: found automatically)')
    actors: CollectionProperty(type=C2Actor)
    active_actor: IntProperty()
    scene_settings: PointerProperty(type=bpy.types.Material)
    surface_brush: IntProperty(name='Surface Type', default=0)
    surface_color: IntProperty(name='Surface Color', default=0)
    grids: CollectionProperty(type=C2Grid)
    active_grid: IntProperty()
    grid_racers: IntProperty(name='Preview Racers', default=10, min=1, max=10,
                             description='How many grid slots the preview shows (the grid columns depend on it)')
    spawn_lists: CollectionProperty(type=C2SpawnList)
    active_spawn_list: IntProperty()
    node_settings: CollectionProperty(type=C2NodeSettings)
    active_node_settings: IntProperty()
    track_preview: EnumProperty(name='Racing Lines', items=[('A', 'Track A', ''), ('B', 'Track B', ''),
                                                             ('C', 'Track C', '')], default='A',
                                description='Which track racing lines the track preview draws',
                                update=lambda s, c: _track_preview_changed(s, c))
    track_preview_show: BoolProperty(name='Show Track Preview', default=True,
                                     update=lambda s, c: _track_preview_changed(s, c),
                                     description='Draw the AI track\'s road edges, direction arrows and racing '
                                                 'lines in the viewport (it follows every edit)')
    track_preview_width: FloatProperty(name='Line Width', default=1.5, min=0.5, max=8.0, subtype='PIXEL',
                                       update=lambda s, c: _track_preview_changed(s, c),
                                       description='Thickness of the track preview lines, in pixels')
    track_preview_opacity: FloatProperty(name='Opacity', default=0.55, min=0.05, max=1.0, subtype='FACTOR',
                                         update=lambda s, c: _track_preview_changed(s, c),
                                         description='How solid the track preview lines are (lower shows the '
                                                     'nodes under them more clearly)')
    has_gameplay: BoolProperty()
    export_only_c2: BoolProperty(name='Only Meshes With Cars 2 Materials', default=False,
                                 description='New meshes are exported only when every material slot holds a Cars 2 '
                                             'material (auto-assigned or made by hand); others are left out. The '
                                             'imported world is always exported')
    prelight_scale: FloatProperty(name='Prelight Scale', default=0.5, min=0.0, max=4.0,
                                  description='Baked light of 1.0 is stored as this vertex colour '
                                              '(shipping prelight averages ~0.5, i.e. 2x overbright)')


_classes = (C2ObjRef, C2KV, C2ObjectProps, C2ShapeProps, C2LightProps, C2MatProp, C2MatTex,
            C2MaterialProps, C2Texture, C2LightRig, C2LightSet, C2Anchor, C2ChoreoActor, C2ChoreoTrigger, C2Choreo,
            C2Grid, C2SpawnList, C2NodeSettings, C2Actor, C2SceneProps)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    # attach the groups: every object/mesh/light/material/scene gets a .c2 (or .c2col)
    bpy.types.Object.c2 = PointerProperty(type=C2ObjectProps)
    bpy.types.Mesh.c2col = PointerProperty(type=C2ShapeProps)
    bpy.types.Light.c2 = PointerProperty(type=C2LightProps)
    bpy.types.Material.c2 = PointerProperty(type=C2MaterialProps)
    bpy.types.Scene.c2 = PointerProperty(type=C2SceneProps)
    # a character's own textures (Cars 2 Toolkit characters; see C2MaterialProps.owner)
    bpy.types.Object.c2tex = CollectionProperty(type=C2Texture)
    bpy.types.Object.c2tex_active = IntProperty()


def unregister():
    del bpy.types.Object.c2tex_active
    del bpy.types.Object.c2tex
    del bpy.types.Scene.c2
    del bpy.types.Material.c2
    del bpy.types.Light.c2
    del bpy.types.Mesh.c2col
    del bpy.types.Object.c2
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
