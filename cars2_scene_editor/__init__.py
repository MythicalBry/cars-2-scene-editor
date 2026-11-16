# SPDX-License-Identifier: GPL-3.0-or-later
"""Cars 2 Scene Editor - Blender add-on entry point.

What this add-on does
---------------------
It imports a Cars 2: The Video Game world (PC/Steam or Arcade build) into
Blender, lets you edit it, and writes it back in the game's own file formats:

    <world>.oct           scene tree, geometry, materials, textures, lights ...
    <world>_0.vbuf/.ibuf  vertex / index buffers the .oct points into
    <world>.btm/.btd      collision meshes (Bullet BVH trees)
    <world>.mer           Meridian: choreographies, triggers, locators, spawns
    <world>.ap            actor properties (INI text, one section per actor)
    <world>.dbl           paths, surface-type names (GBWIN container)

How the code is organised
-------------------------
core/       Pure Python, no Blender: readers/writers for every file format
            (c2oct, btcol, meridian, dbl, navmesh, track, dds ...) plus the
            shader translators (d3dbc; fxgraph for Eevee nodes, fxglsl for
            GLSL). They are imported as plain top-level modules (e.g.
            `import c2oct`), which is why this file puts the core/ folder on
            sys.path.
*.py        The Blender side: properties (props), import (importer), export
            (exporter), operators (ops, matops, bakeops, scenetools, gameplay,
            preview), viewport shading (matnodes, fxnodes), the Cars 2 Game
            render engine (gamerender, gameshadow, gamepost, gamereflect,
            gameeyes), animated characters in previews through the Cars 2 Toolkit
            (charrig) and the sidebar UI (ui).

Each Blender-side module has its own register()/unregister(); this file only
calls them in order.
"""

# Blender reads this dictionary to show the add-on in Preferences > Add-ons.
bl_info = {
    "name": "Cars 2 Scene Editor",
    "author": "Cars 2 modding community",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export > Cars 2 Scene (.oct); 3D View > Sidebar > Cars 2 Scene",
    "description": "Edit Cars 2: The Video Game worlds (PC/Steam and Arcade) - scene tree, geometry, "
                   "materials, lights, light rigs/probes, lightmaps and baked vertex lighting, collision, "
                   "Meridian choreographies/triggers/locators, spawn points, starting grids, AI track graph, "
                   "AI markers, .dbl paths, arena nav meshes and actor properties",
    "category": "Import-Export",
}

import os
import sys

# Folder holding the format readers/writers. They are not a package: the
# Blender modules import them by bare name (`import c2oct`), so the folder
# must be on sys.path before anything else is imported.
_CORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core')
# core modules are plain top-level modules; drop stale copies on re-enable
# (otherwise disabling/re-enabling the add-on, or installing a new version
# without restarting Blender, would keep running the old code)
_CORE_MODULES = ('c2oct', 'c2headers', 'vertexfmt', 'btcol', 'apfile', 'meridian', 'dds',
                 'shlight', 'octscene', 'c2clipblock', 'clipeval', 'choreo', 'arcade', 'd3dbc', 'fxgraph', 'cubemap',
                 'dbl', 'navmesh', 'track', 'fxglsl', 'anchors', 'choreoplace', 'variants', 'luatable', 'pigpen',
                 'p2p', 'spawnsplit', 'collectline')
for _m in _CORE_MODULES:
    sys.modules.pop(_m, None)
# the same for this add-on's own submodules (cars2_scene_editor.xxx)
for _m in [m for m in sys.modules if m.startswith(__name__ + '.')]:
    del sys.modules[_m]
# put core/ first on the search path (and only once)
if _CORE in sys.path:
    sys.path.remove(_CORE)
sys.path.insert(0, _CORE)

from . import props        # noqa: E402   property groups stored on scenes/objects/materials
from . import bakeops      # noqa: E402   lightmap / vertex-light baking
from . import fxnodes      # noqa: E402   "Game Look": the game's shaders as Blender nodes
from . import matops       # noqa: E402   material operators (textures, effects)
from . import importer     # noqa: E402   .oct (+ side files) -> Blender
from . import exporter     # noqa: E402   Blender -> .oct (+ side files)
from . import ops          # noqa: E402   general scene operators (add to scene, collision, check ...)
from . import preview      # noqa: E402   choreography / character preview
from . import objplace     # noqa: E402   spawned map objects (bashables, breakables, pickups)
from . import finishpoint  # noqa: E402   point-to-point race finish
from . import collectgates  # noqa: E402  Collect mode's gates between their posts
from . import gameplay     # noqa: E402   AI track, markers, spawns, grids, paths, nav meshes
from . import scenetools   # noqa: E402   custom-scene helpers (replace world, auto-assign ...)
from . import lookui       # noqa: E402   the Material panel's contents (shared with the Cars 2 Toolkit)
from . import ui           # noqa: E402   sidebar panels
from . import gamerender   # noqa: E402   Cars 2 Game render engine (the game's shaders in GLSL)
from . import particles    # noqa: E402   particle effects of choreography previews
from . import ambientvol   # noqa: E402   ambient light volumes (the light of fireworks, explosions ...)
from . import crowd        # noqa: E402   grandstand crowds (CrowdMemberPlane sprites)
from . import modecols     # noqa: E402   Battle Race / Attack / Survival collections

# Registration order matters: props first (everything else stores data in
# its property groups), the UI last (its panels reference the operators).
_modules = (props, ambientvol, importer, exporter, ops, bakeops, fxnodes, matops, preview, objplace, finishpoint, collectgates, gameplay, scenetools, lookui, ui, gamerender, particles, crowd, modecols)


def register():
    """Called by Blender when the add-on is enabled."""
    from . import looklink
    o = looklink.owner()
    if o is not None and o['name'] != 'scene_editor' and o['release'] is not None:
        o['release']()      # the Cars 2 Toolkit's copy of the material system: ours runs instead
    for m in _modules:
        m.register()
    looklink.claim('scene_editor', __name__)
    from . import charrig
    charrig.remember_core()


def unregister():
    """Called by Blender when the add-on is disabled (reverse order)."""
    for m in reversed(_modules):
        m.unregister()
    from . import looklink
    looklink.drop('scene_editor')       # a waiting Cars 2 Toolkit registers its own copy
