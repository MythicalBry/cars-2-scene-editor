"""Planar reflections for the Cars 2 Game render engine, as sdaemon.exe makes them.

R_Reflection_Plane (FUN_0063c170, Update, _submit_reflection_plane):
  * the plane: the first scene node whose material has g_AlphaReflection > 0
    (water, glossy floors) gives a horizontal plane (normal +Y) at its height.
    No such material: no reflection pass at all.
  * a mirror camera (the view reflected in the plane, far plane =
    g_ReflectionFogEnd) draws the parts whose SubGeometry RenderCaps contain
    0x200000 with their effect's Reflect technique (opaque, then
    translucent), clipped at the plane (oblique near plane), into a
    screen-sized texture;
  * glowgauss BlurH5 / BlurV5 soften it (5-tap Gaussian, sigma 1);
  * surface shaders read it as ReflectionPlaneTexture at
    g_ReflectionPlaneProj (the mirror camera's view-projection).

Cars and characters reflect too (their packages carry the bit).
"""
import math

import gpu
from mathutils import Matrix, Vector

REFLECT_BIT = 0x200000


def mirror_matrix(height):
    """Game-space reflection in the horizontal plane y = height."""
    m = Matrix.Identity(4)
    m[1][1] = -1.0
    m[1][3] = 2.0 * height
    return m


def oblique(proj, plane_view):
    """GL projection whose near plane is plane_view (view space, kept side
    positive): Lengyel's oblique near-plane clipping."""
    P = proj.copy()
    c = Vector(plane_view)

    def sgn(x):
        return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)
    if abs(P[0][0]) < 1e-12 or abs(P[1][1]) < 1e-12 or abs(P[2][3]) < 1e-12:
        return P            # orthographic: keep the normal near plane
    q = Vector(((sgn(c.x) + P[0][2]) / P[0][0], (sgn(c.y) + P[1][2]) / P[1][1], -1.0,
                (1.0 + P[2][2]) / P[2][3]))
    d = c.dot(q)
    if abs(d) < 1e-12:
        return P
    cc = c * (2.0 / d)
    for k in range(4):
        P[2][k] = cc[k] - P[3][k]
    return P


class Reflection:
    """The mirror view's target and blur."""

    def __init__(self):
        self.target = None       # (w, h, fb, colour, depth, fb2, colour2)

    def targets(self, w, h):
        if self.target is None or self.target[:2] != (w, h):
            col = gpu.types.GPUTexture((w, h), format='RGBA16F')
            dep = gpu.types.GPUTexture((w, h), format='DEPTH_COMPONENT32F')
            col2 = gpu.types.GPUTexture((w, h), format='RGBA16F')
            self.target = (w, h, gpu.types.GPUFrameBuffer(depth_slot=dep, color_slots=col), col, dep,
                           gpu.types.GPUFrameBuffer(color_slots=col2), col2)
        return self.target

    @staticmethod
    def mirror_frame(frame, root_mw, height, far, frame_cls):
        """The mirror camera as a Frame (Blender view / projection), and its
        game-space view matrix."""
        M = mirror_matrix(height)
        root_inv = root_mw.inverted_safe()
        view = frame.view @ root_mw @ M @ root_inv
        game_view = view @ root_mw
        # keep what is above the plane: (0, 1, 0, -h) in game space, to view space
        plane = Vector((0.0, 1.0, 0.0, -height))
        pv = game_view.inverted_safe().transposed() @ plane
        if pv.w > 0:        # the camera must sit on the clipped side
            pv = -pv
        proj = oblique(frame.proj, pv)
        f = frame_cls(view, proj, frame.width, frame.height, frame.near, max(far, frame.near * 2))
        return f, game_view
