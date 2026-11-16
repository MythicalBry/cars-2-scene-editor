"""Order-3 spherical harmonics for LightRigPool.LightRigConsts.

What a light rig is: the game lights moving things (cars, characters) with a
"light rig" - the incoming light from every direction compressed into 9
spherical-harmonic numbers per colour channel (27 floats: 9 red, then 9 green,
then 9 blue, as stored in the .oct). Static world geometry uses lightmaps /
vertex colours instead. This module builds those 27 numbers from simple
lights so a rig can be edited in Blender.

The maths is the standard real spherical-harmonic lighting from the public
literature:
  * R. Ramamoorthi and P. Hanrahan, "An Efficient Representation for
    Irradiance Environment Maps", SIGGRAPH 2001 (basis, irradiance).
  * P.-P. Sloan, "Stupid Spherical Harmonics (SH) Tricks", GDC 2008
    (basis with the Condon-Shortley phase, coefficient order l*(l+1)+m,
    directional-light normalisation 16*pi/17).

The shipped rigs use a different overall scale than this projection, so
`project_lights(..., match=...)` rescales to keep a reference rig's brightness.
"""
import math

NUM = 9     # coefficients per colour channel (bands 0, 1 and 2)

# Real SH normalisation constants (Ramamoorthi & Hanrahan 2001, eq. 3)
_K00 = 0.5 * math.sqrt(1.0 / math.pi)           # Y(0, 0)
_K1 = math.sqrt(3.0 / (4.0 * math.pi))          # Y(1, -1..1)
_K2 = 0.5 * math.sqrt(15.0 / math.pi)           # Y(2, -2), Y(2, -1), Y(2, 1)
_K20 = 0.25 * math.sqrt(5.0 / math.pi)          # Y(2, 0)
_K22 = 0.25 * math.sqrt(15.0 / math.pi)         # Y(2, 2)

# Sloan 2008: a directional light scaled by 16*pi/17 gives exit radiance 1 on
# a white diffuse surface facing it, after order-3 windowing.
_DIRECTIONAL = 16.0 * math.pi / 17.0
# A constant radiance L has only the l=0 coefficient: L * integral(Y00) = L * 2*sqrt(pi).
_AMBIENT = 2.0 * math.sqrt(math.pi)


def eval_basis(x, y, z):
    """The 9 real SH basis functions for unit direction (x, y, z), in the order
    index = l*(l+1) + m, with the Condon-Shortley phase (odd m negative)."""
    return [
        _K00,                               # l=0
        -_K1 * y,                           # l=1, m=-1
        _K1 * z,                            # l=1, m=0
        -_K1 * x,                           # l=1, m=1
        _K2 * x * y,                        # l=2, m=-2
        -_K2 * y * z,                       # l=2, m=-1
        _K20 * (3.0 * z * z - 1.0),         # l=2, m=0
        -_K2 * x * z,                       # l=2, m=1
        _K22 * (x * x - y * y),             # l=2, m=2
    ]


def zero():
    """An empty (black) rig: 27 zeros."""
    return [0.0] * 27


def _accumulate(sh, rgb_scale, basis):
    for ch in range(3):
        base = ch * NUM
        for i, b in enumerate(basis):
            sh[base + i] += rgb_scale[ch] * b


def add_directional(sh, color, intensity, direction):
    """Add a directional (sun-like) light to `sh` in place.

    direction: vector pointing *towards* the light (the light's local +Z in
    world space); it does not need to be normalised."""
    length = math.sqrt(sum(v * v for v in direction)) or 1.0
    ux, uy, uz = (v / length for v in direction)
    _accumulate(sh, [c * intensity * _DIRECTIONAL for c in color], eval_basis(ux, uy, uz))


def add_ambient(sh, color, intensity):
    """Add a uniform (all directions) light: only the constant term changes."""
    for ch in range(3):
        sh[ch * NUM] += color[ch] * intensity * _AMBIENT


def irradiance(sh, n):
    """Evaluate the SH (as stored) in direction n -> rgb (for previews)."""
    basis = eval_basis(*n)
    return [sum(sh[ch * NUM + i] * b for i, b in enumerate(basis)) for ch in range(3)]


def project_lights(lights, match=None):
    """lights: [(kind, color3, intensity, dir3)], kind in {'Directional','Ambient'}.
    match: optional reference 27-float rig; the result is scaled so the sum of
    its constant terms (r + g + b) equals the reference's."""
    sh = zero()
    for kind, color, intensity, d in lights:
        if kind == 'Directional':
            add_directional(sh, color, intensity, d)
        elif kind == 'Ambient':
            add_ambient(sh, color, intensity)
    if match:
        want = sum(match[ch * NUM] for ch in range(3))
        have = sum(sh[ch * NUM] for ch in range(3))
        if have > 1e-9:
            sh = [v * (want / have) for v in sh]
    return sh
