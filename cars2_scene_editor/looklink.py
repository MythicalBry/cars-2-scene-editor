"""Who runs the shared material system (Material.c2 and friends, the Cars 2
Game render engine, the material operators): the Cars 2 Scene Editor or the
Cars 2 Toolkit, which carries a copy of it (look/, synced from the Scene
Editor). Both register the same names, so only one copy may be registered.

    Scene Editor enabled  registers its copy; a toolkit copy registered before
                          it is released first. The toolkit then uses the
                          Scene Editor's modules (modules()).
    Toolkit enabled       registers its copy only when the Scene Editor's
                          isn't; else it waits, and takes over when the Scene
                          Editor is disabled.

The material data lives in the .blend under the same property names whichever
copy runs, so both add-ons edit the same materials.

Blender empties bpy.app.driver_namespace on every file load (File > Open,
File > New, opening a .blend from the OS). The records are therefore kept a
second time in a small module in sys.modules, which a file load leaves alone,
and owner() puts them back when they are gone. Without that, opening a file
lost the record of the Scene Editor's copy: the toolkit then saw no material
system ("Template not imported: the material system is not running") while
the Scene Editor's copy was still registered.

This file is the same in both add-ons.
"""
import sys
import types

import bpy

KEY = 'cars2_look'                  # bpy.app.driver_namespace: the running copy
WAITING = 'cars2_look_waiting'      # bpy.app.driver_namespace: {name: take over()}
MODULES = ('props', 'common', 'fxnodes', 'matnodes', 'matops', 'matio', 'lookui', 'gamerender', 'meshdata')
_KEEP = 'cars2_looklink_records'    # sys.modules: the same records, kept across file loads


def _keep():
    m = sys.modules.get(_KEEP)
    if m is None:
        m = sys.modules[_KEEP] = types.ModuleType(_KEEP)
        m.owner, m.waiting = None, {}
    return m


def owner():
    """{'name', 'package', 'release'} of the running copy, or None."""
    ns = bpy.app.driver_namespace
    o = ns.get(KEY)
    if o is None:
        k = _keep()
        if k.owner is not None:             # a file load emptied the namespace: put the records back
            o = ns[KEY] = k.owner
            if k.waiting:
                ns.setdefault(WAITING, {}).update(k.waiting)
    return o


def claim(name, package, release=None):
    """Record that `package` ('cars2_scene_editor' / 'cars2_toolkit.look') now
    runs the material system. release(): unregister it (None: can't be asked)."""
    bpy.app.driver_namespace[KEY] = _keep().owner = {'name': name, 'package': package, 'release': release}


def drop(name):
    """`name`'s copy is no longer registered: let a waiting add-on take over."""
    o = owner()
    if o is not None and o['name'] == name:
        del bpy.app.driver_namespace[KEY]
        _keep().owner = None
        for other, take in list(bpy.app.driver_namespace.get(WAITING, {}).items()):
            if other != name:
                try:
                    take()
                except Exception as e:
                    print('Cars 2: %s could not take over the material system (%s)' % (other, e))
                if owner() is not None:
                    break


def wait(name, take):
    """`name` uses another add-on's copy for now; take() registers its own."""
    bpy.app.driver_namespace.setdefault(WAITING, {})[name] = take
    _keep().waiting[name] = take


def stop_waiting(name):
    bpy.app.driver_namespace.get(WAITING, {}).pop(name, None)
    _keep().waiting.pop(name, None)


def forget(name):
    """`name`'s copy was unregistered outside drop() (deleting the record by hand): forget it too."""
    k = _keep()
    if k.owner is not None and k.owner.get('name') == name:
        k.owner = None


def modules():
    """The running copy's modules (attributes fxnodes, gamerender, matops ...),
    or None when no copy runs."""
    o = owner()
    if o is None:
        return None
    import importlib
    pkg = o['package']
    found = {}
    for m in MODULES:
        mod = sys.modules.get('%s.%s' % (pkg, m))
        if mod is None:
            try:
                mod = importlib.import_module('%s.%s' % (pkg, m))
            except ImportError:
                continue
        found[m] = mod
    return type('Look', (), found)
