"""Ambient light volumes: the light fireworks, explosions, muzzle flashes and
pickups throw on the world around them.

A SubGeometryAmbient part (in a choreography package or a world .oct) is a
light volume, not something drawn. sdaemon.exe reads its node
(FUN_00c95dc0): AmbientVolumeType (1 box, 2 sphere, 3 plane),
AmbientVolumeExtents (the volume's half size), AmbientVolumeScaleBias (how
much the drawn mesh is enlarged), AmbientVolumeFadeDist, and
AmbientVolumeFalloffDist (read, then reset to 0: the game never uses it).
The light is the part material's emissiveColor x emissiveMultiplier
(Gamma: sRGB -> linear). Choreographies animate both, e.g. the fireworks'
lights pulse from 0 to ~3.6 at each burst.

Each frame the engine draws the volumes into the ambient buffer with the
'ambient' effect (startup zip; its source is ambient.hlsl): for every pixel
it rebuilds the surface position from the linear depth buffer, takes its
offset from the volume centre in the volume's own axes / extents, and
adds colour x multiplier x (1 - r)^3 (r: the distance, a sphere's radius
or a box mapped onto a sphere), blended One / One. Every world shader then
adds the buffer to its diffuse lighting (lightingDiffuse +=
samAmbientBuffer), which tints whatever the volume covers. Plane volumes
only write the buffer's alpha (ColorWriteEnable 8) and add no colour.

The Cars 2 Game renderer does the same here, before the opaque pass
(gamerender: samAmbientBuffer is a frame texture). Each volume object
carries its settings in obj.c2_ambient (keyframeable): previews fill them
from the package and bake the clip's colour and multiplier onto them;
world volumes export them back to their node and material.
"""
import math

import bpy
import gpu
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty
from mathutils import Matrix

TYPES = {1: 'BOX', 2: 'SPHERE', 3: 'PLANE'}
TYPE_IDS = {v: k for k, v in TYPES.items()}

VS = '''
void main()
{
  gl_Position = vec4(pos, 0.0, 1.0);
}
'''
# ambient.hlsl cubePS / spherePS on a full-screen pass (falloff fadeParams.x = 0,
# as the engine sets it)
FS = '''
vec3 cube_to_sphere(vec3 p)
{
  vec3 q = p * p;
  return p * sqrt(vec3(1.0 - q.y / 2.0 - q.z / 2.0 + q.z * q.y / 3.0,
                       1.0 - q.z / 2.0 - q.x / 2.0 + q.x * q.z / 3.0,
                       1.0 - q.x / 2.0 - q.y / 2.0 + q.y * q.x / 3.0));
}
void main()
{
  vec2 uv = gl_FragCoord.xy / target.xy;
  float d = texture(depth, uv).r;
  if (d >= 0.9999) discard;                       // nothing there (sky, cleared depth)
  vec4 v = pinv * vec4(uv * 2.0 - 1.0, 1.0, 1.0);
  vec3 ray = v.xyz / v.w;
  ray /= -ray.z;
  vec3 wp = (vinv * vec4(ray * (d * target.z), 1.0)).xyz;
  vec3 a = (winv * vec4(wp - centre.xyz, 0.0)).xyz / size.xyz;
  float r = size.w > 1.5 ? length(a) : length(cube_to_sphere(clamp(a, -1.0, 1.0)));
  float b = 1.0 - clamp(r, 0.0, 1.0);
  fragColor = vec4(color.rgb * (b * b * b), 0.0);
}
'''

_shader = [None]
_batch = [None]
_target = [None]


def _srgb_to_linear(c):
    c = max(0.0, float(c))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _update(self, context):
    invalidate()
    from . import gamerender
    gamerender.RENDERER.version += 1          # redraw the viewport with the new light


class C2AmbientVolume(bpy.types.PropertyGroup):
    enabled: BoolProperty(name='Ambient Light Volume', default=False, update=_update,
                          description='This mesh is a light volume (SubGeometryAmbient): it lights the world it covers')
    vtype: EnumProperty(name='Shape', items=[('SPHERE', 'Sphere', 'Light fading out from the centre to the extents'),
                                             ('BOX', 'Box', 'Light fading out towards the box faces'),
                                             ('PLANE', 'Plane', 'Writes only the buffer alpha: adds no light in the game')],
                        default='SPHERE', update=_update)
    extents: FloatVectorProperty(name='Extents', size=3, default=(10.0, 10.0, 10.0), min=1.0, subtype='XYZ',
                                 update=_update, description='Half size of the volume along its own axes (game units: '
                                                             'the light reaches zero there)')
    scale_bias: FloatProperty(name='Scale Bias', default=1.05, min=0.0, update=_update,
                              description='How much the game enlarges the drawn volume mesh (AmbientVolumeScaleBias)')
    falloff: FloatProperty(name='Falloff Dist', default=0.0, min=0.0, max=1.0,
                           description='AmbientVolumeFalloffDist: kept on export, but the game resets it to 0')
    fade: FloatProperty(name='Fade Dist', default=150.0, min=0.0,
                        description='AmbientVolumeFadeDist: the camera distance plane volumes fade at')
    color: FloatVectorProperty(name='Color', size=3, default=(1.0, 1.0, 1.0), min=0.0, soft_max=1.0,
                               subtype='COLOR_GAMMA', update=_update,
                               description="The light colour (the material's emissiveColor)")
    multiplier: FloatProperty(name='Multiplier', default=1.0, min=0.0, soft_max=10.0, update=_update,
                              description="Light strength (the material's emissiveMultiplier; 0 = off)")


def material_values(m):
    """(emissiveColor, emissiveMultiplier) of a MaterialPool entry (None when absent)."""
    col = mul = None
    for pe in (m.get('PropertyEntries') or {}).values():
        if pe.get('Name') == 'emissiveColor' and len(pe.get('Value') or ()) >= 3:
            col = tuple(float(x) for x in pe['Value'][:3])
        elif pe.get('Name') == 'emissiveMultiplier':
            v = pe.get('Value')
            mul = float(v[0] if isinstance(v, (list, tuple)) else v)
    return col, mul


def fill(obj, node, material):
    """Settings of a SubGeometryAmbient node + its MaterialPool entry onto obj.c2_ambient."""
    a = obj.c2_ambient
    a.enabled = True
    a.vtype = TYPES.get(int(node.get('AmbientVolumeType', 2) or 2), 'SPHERE')
    ext = node.get('AmbientVolumeExtents') or (10.0, 10.0, 10.0)
    a.extents = [max(1.0, float(x)) for x in ext[:3]]
    a.scale_bias = float(node.get('AmbientVolumeScaleBias', 1.05))
    a.falloff = float(node.get('AmbientVolumeFalloffDist', 0.0))
    a.fade = float(node.get('AmbientVolumeFadeDist', 150.0))
    col, mul = material_values(material or {})
    a.color = col if col is not None else (0.0, 0.0, 0.0)     # SolidColor volumes: no emissive, black
    a.multiplier = mul if mul is not None else 1.0
    obj['c2_ambient0'] = [TYPE_IDS[a.vtype]] + list(a.extents) + [a.scale_bias, a.falloff, a.fade] + \
        list(a.color) + [a.multiplier]


def changed(obj):
    """True when the volume's settings differ from what was imported."""
    a = obj.c2_ambient
    now = [TYPE_IDS[a.vtype]] + list(a.extents) + [a.scale_bias, a.falloff, a.fade] + list(a.color) + [a.multiplier]
    old = obj.get('c2_ambient0')
    return old is None or any(abs(x - y) > 1e-6 for x, y in zip(now, list(old)))


def write_node(obj, node):
    """Export: the volume settings back onto its SubGeometryAmbient node."""
    a = obj.c2_ambient
    node['AmbientVolumeType'] = TYPE_IDS[a.vtype]
    import c2oct
    node['AmbientVolumeExtents'] = c2oct.FloatList(float(x) for x in a.extents)
    node['AmbientVolumeScaleBias'] = float(a.scale_bias)
    node['AmbientVolumeFalloffDist'] = float(a.falloff)
    node['AmbientVolumeFadeDist'] = float(a.fade)


def write_material(obj, mat):
    """Export: colour / multiplier into the volume material's game parameters.
    False when the material's effect has no emissiveColor (SolidColor)."""
    if mat is None:
        return False
    a = obj.c2_ambient
    props = {p.name: p for p in mat.c2.props}
    if 'emissiveColor' not in props:
        return False
    p = props['emissiveColor']
    p.fvalue = (a.color[0], a.color[1], a.color[2], p.fvalue[3])
    if 'emissiveMultiplier' in props:
        q = props['emissiveMultiplier']
        q.fvalue = (a.multiplier, q.fvalue[1], q.fvalue[2], q.fvalue[3])
    return True


# ----------------------------------------------------------------------
# the ambient buffer
# ----------------------------------------------------------------------

def _get_shader():
    if _shader[0] is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, 'VEC2', 'pos')
        info.push_constant('MAT4', 'pinv')
        info.push_constant('MAT4', 'vinv')
        info.push_constant('MAT4', 'winv')
        info.push_constant('VEC4', 'centre')
        info.push_constant('VEC4', 'size')
        info.push_constant('VEC4', 'color')
        info.push_constant('VEC4', 'target')
        info.sampler(0, 'FLOAT_2D', 'depth')
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source(VS)
        info.fragment_source(FS)
        _shader[0] = gpu.shader.create_from_info(info)
        from gpu_extras.batch import batch_for_shader
        _batch[0] = batch_for_shader(_shader[0], 'TRIS', {'pos': ((-1, -1), (3, -1), (-1, 3))})
    return _shader[0]


_found = {'key': None, 'objs': [], 'inst': []}


def _scan(scene):
    """The scene's volume objects, and the collection instancers that show
    copies of them (a preview played at several places). Rescanned only when
    the object count changes or a volume setting is edited (_update): walking
    every object each frame cost 11-13 ms on tokyo_b's 5400 objects."""
    key = (scene.name, len(scene.objects), _found.get('gen', 0))
    if _found['key'] == key:
        return _found
    objs = [o for o in scene.objects if o.type == 'MESH' and o.c2_ambient.enabled]
    vol = set(o.name for o in objs)
    inst = []
    for o in scene.objects:
        col = o.instance_collection if o.instance_type == 'COLLECTION' else None
        if col is not None:
            for x in col.all_objects:
                if x.name in vol:
                    inst.append((o, x))
    _found.update(key=key, objs=objs, inst=inst)
    return _found


def invalidate(*_args):
    _found['gen'] = _found.get('gen', 0) + 1


_undo = bpy.app.handlers.persistent(invalidate)     # undo / redo re-create the objects


def volumes(depsgraph, root_inv, visible=None):
    """[(game world matrix, settings, linear colour)] of the volumes that give
    light now: enabled, not disabled in viewports (the eye only hides their
    wireframe), a non-zero colour x multiplier. Read from the original
    objects (the timeline writes the clip's keys onto them, in the viewport
    and in F12), including eye-hidden and render-disabled ones."""
    scene = depsgraph.scene.original if hasattr(depsgraph.scene, 'original') else depsgraph.scene
    found = _scan(scene)
    if not found['objs']:
        return []
    out = []

    def add(o, mw):
        a = o.c2_ambient
        if not a.enabled or o.hide_viewport or a.vtype == 'PLANE':
            return
        col = [_srgb_to_linear(c) * a.multiplier for c in a.color]
        if max(col) > 0.0:
            out.append((root_inv @ mw, a, col))
    try:
        for o in found['objs']:
            add(o, o.matrix_world)
        for e, o in found['inst']:
            if e.hide_viewport:
                continue
            off = Matrix.Translation(-e.instance_collection.instance_offset)
            add(o, e.matrix_world @ off @ o.matrix_world)
    except ReferenceError:          # an object was deleted: look again next frame
        invalidate()
    return out


def render(vols, frame, game_view, depth, width, height):
    """Draw the volumes into the ambient buffer (RGBA16F, One / One). Returns the texture."""
    if _target[0] is None or _target[0][:2] != (width, height):
        tex = gpu.types.GPUTexture((width, height), format='RGBA16F')
        _target[0] = (width, height, gpu.types.GPUFrameBuffer(color_slots=tex), tex)
    _w, _h, fb, tex = _target[0]
    sh = _get_shader()
    pinv = frame.proj.inverted_safe()
    vinv = game_view.inverted_safe()
    # frustum test of each volume's bounding sphere (game space)
    planes = _planes(frame.proj @ game_view)
    st = gpu.state
    with fb.bind():
        st.viewport_set(0, 0, width, height)
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        st.depth_test_set('NONE')
        st.depth_mask_set(False)
        st.face_culling_set('NONE')
        st.blend_set('ADDITIVE_PREMULT')
        sh.bind()
        sh.uniform_float('pinv', pinv)
        sh.uniform_float('vinv', vinv)
        sh.uniform_float('target', (float(width), float(height), float(frame.far), 0.0))
        sh.uniform_sampler('depth', depth)
        for mw, a, col in vols:
            c = mw.translation
            rad = max(a.extents) * max(mw.to_scale())
            if any(p[0] * c.x + p[1] * c.y + p[2] * c.z + p[3] < -rad for p in planes):
                continue
            # into the volume's own axes (g_WorldInverseMatrix), then / extents
            sh.uniform_float('winv', mw.to_3x3().inverted_safe().to_4x4())
            sh.uniform_float('centre', (c.x, c.y, c.z, 1.0))
            sh.uniform_float('size', (a.extents[0], a.extents[1], a.extents[2], 2.0 if a.vtype == 'SPHERE' else 1.0))
            sh.uniform_float('color', (col[0], col[1], col[2], 0.0))
            _batch[0].draw(sh)
        st.blend_set('NONE')
    return tex


def _planes(m):
    rows = [m[i] for i in range(4)]
    out = []
    for i in range(3):
        for s in (1.0, -1.0):
            p = [rows[3][k] + s * rows[i][k] for k in range(4)]
            n = math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2) or 1.0
            out.append([x / n for x in p])
    return out


# ----------------------------------------------------------------------
# panel
# ----------------------------------------------------------------------

def draw_panel(layout, context):
    o = context.object
    if o is None or o.type != 'MESH' or not (o.c2_ambient.enabled or 'c2_ambient0' in o):
        return
    a = o.c2_ambient
    box = layout.box()
    box.label(text='Ambient Light Volume', icon='LIGHT_SUN')
    col = box.column(align=True)
    col.prop(a, 'vtype')
    col.prop(a, 'color')
    col.prop(a, 'multiplier')
    col.prop(a, 'extents')
    col.prop(a, 'scale_bias')
    sub = box.column(align=True)
    sub.prop(a, 'fade')
    sub.prop(a, 'falloff')
    if a.vtype == 'PLANE':
        box.label(text='Plane volumes add no light in the game', icon='INFO')
    if o.get('c2_preview'):
        box.label(text='Preview: keyed by the clip, not exported', icon='INFO')
    elif max(a.color) <= 0.0:
        box.label(text="Black: its material has no emissiveColor", icon='INFO')
    box.prop(a, 'enabled', text='Lights the World')


def register():
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _undo not in h:
            h.append(_undo)
    bpy.utils.register_class(C2AmbientVolume)
    bpy.types.Object.c2_ambient = bpy.props.PointerProperty(type=C2AmbientVolume)


def unregister():
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _undo in h:
            h.remove(_undo)
    del bpy.types.Object.c2_ambient
    bpy.utils.unregister_class(C2AmbientVolume)
    _shader[0] = _batch[0] = _target[0] = None
