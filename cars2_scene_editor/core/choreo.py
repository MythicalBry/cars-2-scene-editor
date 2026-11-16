"""Choreography packages (assets/choreographies/<name>/ or <name>.zip):
locate, load and evaluate their animation network over time.

A choreography .oct is a small scene: an anchor Transform (ANC_xxx_00) with
geometry, bones, cameras and lights under it, plus a SubNetwork that animates
them. The network pieces handled here (measured on all 538 shipping packages):

* DataNodes are addressed by header paths that mirror the scene, e.g.
  ANC_X_00/SKIN/smoke03/Transform. A scene node is bound to its DataNode
  through an association named the same with '|' separators.
* Transform/Dilation data: Translation, Orientation#Rotor (stored
  conjugated) or Orientation#OrientProxy (ParentProxyToParent x
  LocalProxyToParentProxy x LocalToLocalProxy, all conjugated), Scale.
  The local matrix is T * R * S. BasisDataNodeRef is the parent DataNode.
* ClipProcessor: Global.Time -> ClipDataBlock -> Output DataNodes. Clip values
  are absolute: location replaces Translation, Euler replaces the rotation
  (LocalProxyToParentProxy for jointed nodes), scale replaces Scale; scalar and
  Bool outputs replace the value.
* AnimCurve3ToPosition / AnimCurve3Euler(ZXY|XYZ)ToRotor: three Maya-style
  Hermite curves in the NetworkBuffer drive '<X>/Transform.Translation' and
  '<X>/Transform.Orientation', which feed '<X>/Transform'.
* Connection: copies Input to Output.
* OffsetKinematicStateUpdate: places a referenced character (car) at its
  '<Namespace>:Anchor/Transform' relative to the choreography anchor.

* LayerBind: a clip played into a referenced character ('Luigi:Choreography').
  Clip target slot s is the character's DataNode
  ConnectionMapUsedDataNodes[DataNodeLinks[s]], DataNodeLinks from the
  character's BaseDataNodeLinks entry with the same ConnectionTemplate (see
  character_layer_targets). Values are offsets from the rig's rest pose
  (all clips start and end at zero). Transforms on 'Anchor/Control<Bone>' and
  'Anchor/<Bone>' move that bone; face/eye scalars are not shown.

Not evaluated: UberConstraint, RadialBasis, Relationship and DynamicChain.

Used by the editor to (a) find choreographies and the characters they need
when wiring them into a world, and (b) preview them in Blender (preview.py
samples Choreography.evaluate() over time and bakes keyframes).
"""
import math
import os
import struct
import zipfile

import numpy as np

import c2oct
import c2headers
import clipeval
import octscene

# Euler processor type -> rotation order
ORDERS = {'AnimCurve3EulerZXYToRotor': 'ZXY', 'AnimCurve3EulerXYZToRotor': 'XYZ'}


# ----------------------------------------------------------------------
# packages
# ----------------------------------------------------------------------

class Package:
    """Files of one choreography (or character) from a folder or a zip."""

    def __init__(self, stem, folder=None, zpath=None, prefix=''):
        self.stem = stem            # file name of the main .oct without extension
        self.folder = folder        # loose files: their folder
        self.zpath = zpath          # zipped: the zip file
        self.prefix = prefix        # zipped: folder inside the zip
        self._zip = zipfile.ZipFile(zpath) if zpath else None

    def names(self):
        """All file names in the package (relative, '/' separators)."""
        if self._zip:
            return [n[len(self.prefix):] for n in self._zip.namelist() if n.startswith(self.prefix)]
        out = []
        for root, _, files in os.walk(self.folder):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.folder).replace('\\', '/'))
        return out

    def read(self, name):
        """Bytes of one file of the package."""
        if self._zip:
            return self._zip.read(self.prefix + name)
        with open(os.path.join(self.folder, name), 'rb') as f:
            return f.read()

    def top_files(self):
        """name -> bytes of the files next to the .oct (buffers)."""
        out = {}
        for n in self.names():
            if '/' not in n and n.lower().endswith(('.vbuf', '.ibuf')):
                out[n] = self.read(n)
        return out

    def scene(self):
        """The package's main .oct as an OctScene (buffers from the package)."""
        files = self.top_files()
        return octscene.OctScene(self.stem + '.oct', raw=self.read(self.stem + '.oct'), files=files)

    def __repr__(self):
        return 'Package(%s, %s)' % (self.stem, self.zpath or self.folder)


def assets_dir_from(path):
    """.../assets/worlds/<w>/<w>.oct -> .../assets"""
    # walk up a few folders looking for 'assets' (or a folder with choreographies/)
    p = os.path.abspath(path)
    for _ in range(6):
        p = os.path.dirname(p)
        if os.path.basename(p).lower() == 'assets' or os.path.isdir(os.path.join(p, 'choreographies')):
            return p
    return None


def find_choreography(assets, choreography_name):
    """'choreographies/X/X' or 'X' -> Package or None."""
    name = choreography_name.replace('\\', '/').strip('/')
    stem = name.split('/')[-1]
    if stem.lower().endswith('.oct'):
        stem = stem[:-4]
    base = os.path.join(assets, 'choreographies')
    if not os.path.isdir(base):
        return None
    entries = {e.lower(): e for e in os.listdir(base)}      # case-insensitive lookup
    s = stem.lower()
    # 1) an unpacked folder choreographies/<X>/<X>.oct
    if s in entries and os.path.isdir(os.path.join(base, entries[s])):
        folder = os.path.join(base, entries[s])
        if any(f.lower() == s + '.oct' for f in os.listdir(folder)):
            real = next(f for f in os.listdir(folder) if f.lower() == s + '.oct')[:-4]
            return Package(real, folder=folder)
    # 2) a zip choreographies/<X>.zip
    if s + '.zip' in entries:
        zp = os.path.join(base, entries[s + '.zip'])
        z = zipfile.ZipFile(zp)
        root_octs = [n for n in z.namelist() if '/' not in n and n.lower().endswith('.oct')]
        for n in root_octs:
            if n.lower() == s + '.oct':
                return Package(n[:-4], zpath=zp)
        # variant packages (x_PC.zip holding x.oct, x_master.oct ...): their main .oct
        import re
        mains = [n for n in root_octs if not re.search(r'_d\d+\.oct$', n.lower())]
        if mains:
            return Package(sorted(mains, key=len)[0][:-4], zpath=zp)
    return None


_char_index = {}        # characters folder -> {name: location} (built once, it's a slow walk)


def character_slots(assets, choreography_name):
    """Characters a choreography needs a world actor for: [(binding, character source)].
    Each is a ReferenceFile of Type 'Reference' with a BindingName; the world
    has to hand it an actor through a ChoreographyActorReference of that name
    (radiator_a's bg_Mater: an .ap 'compactor' actor whose model is the
    character). 'Player*' bindings are filled by the game with the player car."""
    pkg = find_choreography(assets, choreography_name) if assets else None
    if pkg is None:
        return []
    import c2oct
    try:
        tree, _ = c2oct.decode(pkg.read(pkg.stem + '.oct'))
    except (KeyError, OSError, ValueError):
        return []
    out = []
    for r in tree.get('ReferenceFilePool', {}).values():
        b = r.get('BindingName') or ''
        # Flags & 3: 1 = bound to a world actor (21, 5), 2 = the choreography
        # spawns the character itself (22, 6), 3 = the player car
        if r.get('Type') == 'Reference' and b and (r.get('Flags', 0) & 3) == 1:
            out.append((b, r.get('SourceFile', '')))
    return out


def character_model_name(assets, source):
    """'materbg' -> 'characters\\misc\\materbg\\materbg' (an .ap model_name)."""
    pkg = find_character(assets, source) if assets else None
    if pkg is None:
        path = 'characters/%s/%s' % (source, source)        # best guess
    elif pkg.zpath:
        path = pkg.prefix + pkg.stem
    else:
        path = os.path.relpath(os.path.join(pkg.folder, pkg.stem), assets)
    return path.replace('/', '\\')


def find_character(assets, source):
    """Character model package by its reference SourceFile name (e.g. 'mcqueen')."""
    root = os.path.join(assets, 'characters')
    if not os.path.isdir(root):
        return None
    if root not in _char_index:
        # index every character .oct, loose or inside zips
        # A name can exist twice: Pacer and Gremlin, the playable minions, are
        # characters/pacer and characters/misc/pacer (a cut-down cutscene copy
        # without the face rig). A choreography's reference ('pacer', bound to
        # the actor pacer_00) gets the car the game spawns, the one in
        # characters/<name>: the shallowest package wins.
        idx, depth = {}, {}

        def add(st, hit, where):
            d = os.path.relpath(where, root).replace(os.sep, '/').count('/')
            if st not in idx or d < depth[st]:
                idx[st], depth[st] = hit, d
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            for f in sorted(files):
                fl = f.lower()
                full = os.path.join(dirpath, f)
                if fl.endswith('.oct'):
                    add(fl[:-4], ('dir', dirpath, f[:-4]), full)
                elif fl.endswith('.zip'):
                    try:
                        for n in zipfile.ZipFile(full).namelist():
                            nl = n.lower()
                            if nl.endswith('.oct') and '/motions/' not in nl:
                                st = nl.rsplit('/', 1)[-1][:-4]
                                parent = nl.rsplit('/', 2)[-2] if nl.count('/') >= 1 else ''
                                if parent == st:  # characters/<..>/<name>/<name>.oct
                                    add(st, ('zip', full, n), full)
                    except (zipfile.BadZipFile, OSError):
                        pass
        _char_index[root] = idx
    hit = _char_index[root].get(source.lower())
    if hit is None:
        return None
    if hit[0] == 'dir':
        return Package(hit[2], folder=hit[1])
    n = hit[2]
    prefix = n.rsplit('/', 1)[0] + '/'
    return Package(n.rsplit('/', 1)[1][:-4], zpath=hit[1], prefix=prefix)


# ----------------------------------------------------------------------
# math
# ----------------------------------------------------------------------

def dbl_spline(data, name):
    """(points (n, 3), closed) of spline `name` in a world's .dbl (game
    space), or None. A record is the name in a 64-byte field (zero-terminated;
    the rest of the field can hold leftovers of a longer name), uint32 point
    count, uint32 closed flag (1 on the looping cow paths), then count x
    (x, y, z) float32. Meridian SplineNodes name them
    (BoundObjectName); spline followers move actors along them."""
    key = name.encode('latin1') + b'\0'
    i = data.find(key)
    # the name can also appear elsewhere (e.g. in the B-spline section):
    # accept the first hit that looks like a valid polyline record
    while i >= 0:
        if i + 72 <= len(data):
            n, z = struct.unpack_from('<II', data, i + 64)
            if 1 < n < 100000 and z in (0, 1) and i + 72 + 12 * n <= len(data):
                pts = np.frombuffer(data, '<f4', 3 * n, i + 72).reshape(n, 3).astype(np.float64)
                if np.all(np.isfinite(pts)) and np.abs(pts).max() < 1e6:
                    return pts, bool(z)
        i = data.find(key, i + 1)
    return None


class PathMotion:
    """An actor driven by a SplineFollower, moving to the end of the path (or
    round and round a closed one), facing along it (game +Z forward, +Y up).

    As the game runs it (sdaemon SplineFollower): the start is `pct` (0..1)
    of the path (outside (0, 1]: the start of it), or `start` metres along it
    when given (PlaceActorAsInWorld=1: the point of the path nearest the
    spawned actor, see nearest()). Move sets the speed to 0 and the target to
    MaxSpeed; with no acceleration the actor runs at MaxSpeed at once, else it
    speeds up by `accel` m/s/s. InitialSpeed (`v0`) is overwritten by Move."""

    def __init__(self, points, pct=0.0, v0=0.0, vmax=0.0, accel=0.0, closed=False, start=None):
        if closed:
            points = np.vstack([points, points[:1]])    # repeat the first point to close the loop
        self.closed = closed
        self.p = points
        # cumulative distance at each point
        seg = np.linalg.norm(np.diff(points, axis=0), axis=1)
        self.cum = np.concatenate([[0.0], np.cumsum(seg)])
        self.length = float(self.cum[-1])
        if start is not None:
            self.s0 = max(0.0, min(self.length, float(start)))
        else:
            self.s0 = (pct if 0.0 < pct <= 1.0 else 0.0) * self.length
        self.v0, self.vmax, self.accel = v0, vmax, accel
        v = max(self.vmax, 1e-6)
        moving = vmax > 0
        # time to reach top speed, and the distance covered meanwhile
        self.ta = vmax / accel if accel > 1e-7 else 0.0
        self.da = 0.5 * accel * self.ta * self.ta
        # a closed path never ends: preview one lap
        rest = self.length if closed else self.length - self.s0
        if not moving:
            self.travel = 0.0
        elif rest >= self.da or self.ta == 0.0:
            self.travel = self.ta + (rest - self.da) / v
        else:
            self.travel = math.sqrt(2.0 * rest / accel)     # still speeding up at the end

    def nearest(self, q):
        """Distance along the path of its point nearest q (game space)."""
        a, b = self.p[:-1], self.p[1:]
        ab = b - a
        den = np.maximum(np.einsum('ij,ij->i', ab, ab), 1e-12)
        t = np.clip(np.einsum('ij,ij->i', np.asarray(q, float) - a, ab) / den, 0.0, 1.0)
        d = np.linalg.norm(a + ab * t[:, None] - q, axis=1)
        k = int(np.argmin(d))
        return float(self.cum[k] + t[k] * (self.cum[k + 1] - self.cum[k]))

    def distance(self, t):
        """Distance along the path at time t (constant acceleration up to vmax)."""
        t = max(0.0, t)
        if self.ta > 0:
            if t < self.ta:
                d = 0.5 * self.accel * t * t
            else:
                d = self.da + self.vmax * (t - self.ta)
        else:
            d = self.vmax * t
        if self.closed and self.length > 0:
            return (self.s0 + d) % self.length
        return min(self.length, self.s0 + d)

    def matrix(self, t):
        """4x4 (column-vector) placement at time t: on the path, facing along
        it, kept upright."""
        s = self.distance(t)
        k = int(np.clip(np.searchsorted(self.cum, s) - 1, 0, len(self.p) - 2))
        a, b = self.p[k], self.p[k + 1]
        u = 0.0 if self.cum[k + 1] == self.cum[k] else (s - self.cum[k]) / (self.cum[k + 1] - self.cum[k])
        pos = a + (b - a) * u
        fwd = b - a
        fwd[1] = 0.0                    # flatten: actors stay level
        n = np.linalg.norm(fwd)
        fwd = fwd / n if n > 1e-9 else np.array([0.0, 0.0, 1.0])
        up = np.array([0.0, 1.0, 0.0])
        x = np.cross(up, fwd)
        M = np.eye(4)
        M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = x, up, fwd, pos
        return M


def character_layer_targets(char_scene, template):
    """{clip slot: DataNode header path} for a layer clip played into a
    character (char_scene: octscene of the character package)."""
    for sn in char_scene.tree.get('SubNetworkPool', {}).values():
        used = list(sn.get('ConnectionMapUsedDataNodes', []))
        links = next((list(b.get('DataNodeLinks', [])) for b in sn.get('BaseDataNodeLinksPool', {}).values()
                      if b.get('ConnectionTemplate', '').lower() == template.lower()), None)
        if links is None:
            continue
        paths = c2headers.decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
        dn = list(sn['DataNodePool'].values())
        out = {}
        for slot, li in enumerate(links):
            if 0 <= li < len(used) and 0 <= used[li] < len(dn) and 'Header' in dn[used[li]]:
                out[slot] = paths[dn[used[li]]['Header'] - 1]
        return out
    return {}


def qmat(q):
    """x y z w -> 3x3 column-vector rotation."""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def conj(q):
    """Quaternion conjugate (the file stores rotations conjugated)."""
    return [-q[0], -q[1], -q[2], q[3]]


def euler_mat(a, order):
    """Maya-style Euler (radians), order like 'XYZ' = X applied first."""
    x, y, z = a
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    R = {'X': np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]),
         'Y': np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]),
         'Z': np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])}
    m = np.eye(3)
    for axis in order:      # first axis applied first -> leftmost multiply last
        m = R[axis] @ m
    return m


class HermiteCurve:
    """AnimCurve blob from the NetworkBuffer.

    Header: f32 start, f32 end, u32 flags, u32 key_count; then per key
    u32 flags, f32 time, f32 value, f32 in_dx, f32 in_dy, f32 out_dx, f32 out_dy
    (tangents pre-scaled to the adjacent segment's duration)."""

    def __init__(self, blob):
        self.start, self.end, self.flags, n = struct.unpack_from('<ffII', blob, 0)
        self.keys = [struct.unpack_from('<Iffffff', blob, 16 + 28 * k) for k in range(n)]

    def __call__(self, t):
        """Value at time t (held at the first/last key outside the range)."""
        k = self.keys
        if not k:
            return 0.0
        if t <= k[0][1]:
            return k[0][2]
        if t >= k[-1][1]:
            return k[-1][2]
        for a, b in zip(k, k[1:]):
            if a[1] <= t <= b[1]:
                dt = b[1] - a[1]
                if dt <= 0:
                    return b[2]
                # tangents are (dx, dy) vectors: slope = dy/dx, scaled to dt
                m0 = (a[6] / a[5]) * dt if a[5] else 0.0
                m1 = (b[4] / b[3]) * dt if b[3] else 0.0
                u = (t - a[1]) / dt
                # Hermite segment written as a cubic Bezier
                p0, p3 = a[2], b[2]
                p1, p2 = p0 + m0 / 3.0, p3 - m1 / 3.0
                v = 1.0 - u
                return v * v * v * p0 + 3 * v * v * u * p1 + 3 * v * u * u * p2 + u * u * u * p3
        return k[-1][2]


# ----------------------------------------------------------------------
# the network
# ----------------------------------------------------------------------

class Choreography:
    """A loaded choreography package, ready to evaluate at any time."""

    def __init__(self, pkg):
        self.pkg = pkg
        self.scene = pkg.scene()
        t = self.scene.tree
        self.tree = t
        self.nodes = self.scene.nodes
        self.subnets = []
        sn = next(iter(t.get('SubNetworkPool', {}).values()), None)
        self.dn, self.paths = [], []
        self.bind = {}            # scene node -> DataNode
        self.path_index = {}      # DataNode header path -> DataNode index
        # animation sources found in the network
        self.clips, self.curves, self.connections, self.kinematic = [], [], [], []
        self.layers = []
        self.warnings = []
        if sn is not None:
            self.paths_all = c2headers.decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
            self.dn = list(sn['DataNodePool'].values())
            self.paths = [self.paths_all[d['Header'] - 1] if 'Header' in d else '' for d in self.dn]
            self.path_index = {p: i for i, p in enumerate(self.paths)}
            nb = sn.get('NetworkBuffer', b'')
            # sort every processor into the kinds we can evaluate
            for p in sn.get('ProcessorNodePool', {}).values():
                ty = p.get('Type')
                attrs = dict(zip(p.get('DataNodeAttributes', []), p.get('DataNodeRefs', [])))
                if ty == 'ClipProcessor':
                    try:
                        clip = clipeval.Clip(self.dn[p['ClipData']]['ClipDataBlock'])
                        self.warnings += clip.warnings
                        self.clips.append((clip, list(p['Output'])))
                    except Exception as e:
                        self.warnings.append('clip skipped: %s' % e)
                elif ty in ('AnimCurve3ToPosition',) or ty in ORDERS:
                    try:
                        cs = [HermiteCurve(nb[self.dn[attrs[a]]['NetworkBufferOffset']:])
                              for a in ('CurveDataX', 'CurveDataY', 'CurveDataZ')]
                        self.curves.append((ty, cs, attrs['Output']))
                    except Exception as e:
                        self.warnings.append('curve skipped: %s' % e)
                elif ty == 'Connection':
                    self.connections.append((attrs.get('Input'), attrs.get('Output')))
                elif ty == 'OffsetKinematicStateUpdate':
                    self.kinematic.append((attrs.get('Input'), attrs.get('Output'), attrs.get('Offset')))
                elif ty not in ('EventList',):
                    self.warnings.append('%s not evaluated' % ty)
            # layer clips: animation played into a referenced character's rig
            for lb in sn.get('LayerBindPool', {}).values():
                try:
                    block = self.dn[lb['ClipDataNodeRef']]['ClipDataBlock']
                    clip = clipeval.Clip(block)
                except Exception as e:
                    self.warnings.append('layer clip %s skipped: %s' % (lb.get('Name'), e))
                    continue
                ns = lb.get('Name', '').split(':')[0]
                self.layers.append({'namespace': ns, 'name': lb.get('Name', ''),
                                    'template': lb.get('ConnectionTemplate', ''), 'clip': clip,
                                    'block': bytes(block)})
        # scene node -> DataNode bindings through same-named associations
        for a in t.get('AssociationPool', {}).values():
            if 'SceneTreeNodeRef' in a and a.get('Type') in ('Transform', 'Bone', 'Geometry', 'Camera', 'Light'):
                key = a['NodeName'].replace('|', '/') + '/Transform'
                if key in self.path_index:
                    self.bind.setdefault(a['SceneTreeNodeRef'], self.path_index[key])
        self.material_assoc = {a['NodeName'].replace('|', '/'): a['MaterialRef'] for a in t.get('AssociationPool', {}).values()
                               if a.get('Type') == 'Material' and 'MaterialRef' in a}
        # the longest animation decides the duration
        ends = [c.end for c, _ in self.clips] + [cv.end for _, cs, _ in self.curves for cv in cs] + \
            [ly['clip'].end for ly in self.layers]
        self.duration = max(ends) if ends else 0.0
        self.references = [r for r in t.get('ReferenceFilePool', {}).values() if r.get('Type') == 'Reference']
        self.anchor_name = next((a['NodeName'] for a in t.get('AssociationPool', {}).values()
                                 if a.get('Type') == 'PrimaryAnchor'), '')
        self.placement = [p.get('PlacementName', '') for p in t.get('ChoreographyPlacementNodePool', {}).values()]

    # -- state -------------------------------------------------------------
    def evaluate(self, t):
        """{DataNode index: {prop: value}} overrides at time t."""
        st = {}
        # 1) clips write their output DataNodes
        for clip, outputs in self.clips:
            for tgt, props in clip.evaluate(min(t, clip.end)).items():
                if tgt < len(outputs):
                    st.setdefault(outputs[tgt], {}).update(props)
        # 2) Hermite curves
        for ty, cs, out in self.curves:
            v = [c(t) for c in cs]
            if ty == 'AnimCurve3ToPosition':
                st.setdefault(out, {})['position'] = v
            else:
                st.setdefault(out, {})['rotor_euler'] = (v, ORDERS[ty])
        # 3) connections copy values along
        for i, o in self.connections:
            if i is not None and o is not None:
                src = st.get(i)
                if src is not None:
                    st.setdefault(o, {}).update(src)
                elif 'Data' in self.dn[i] and not isinstance(self.dn[i]['Data'], dict):
                    st.setdefault(o, {})['value'] = [self.dn[i]['Data']]
        # Transform.Translation / Transform.Orientation feed Transform
        for idx, props in list(st.items()):
            p = self.paths[idx] if idx < len(self.paths) else ''
            for suffix, key in (('/Transform.Translation', 'position'), ('/Transform.Orientation', 'rotor_euler')):
                if p.endswith(suffix) and key in props:
                    tgt = self.path_index.get(p[:-len(suffix)] + '/Transform')
                    if tgt is not None:
                        st.setdefault(tgt, {})[key] = props[key]
        return st

    def local(self, di, st):
        """4x4 column-vector local matrix of Transform DataNode di."""
        d = self.dn[di]
        data = d.get('Data', {}) if isinstance(d.get('Data'), dict) else {}
        o = st.get(di, {})          # this frame's overrides
        # translation: stored value, replaced by animated values
        tr = list(data.get('Translation', [0.0, 0.0, 0.0]))
        if 'position' in o:
            tr = list(o['position'])
        if 'loc' in o:
            for k, v in enumerate(o['loc']):
                if v is not None:
                    tr[k] = v
        sc = list(data.get('Scale', [1.0, 1.0, 1.0]))
        if 'scale' in o:
            for k, v in enumerate(o['scale']):
                if v is not None:
                    sc[k] = v
        # rotation: jointed nodes use a three-part proxy rotation; the middle
        # part (E) is the one animation replaces
        if 'Orientation#OrientProxy' in data:
            op = data['Orientation#OrientProxy']
            P = qmat(conj(op.get('ParentProxyToParent#Rotor', [0, 0, 0, 1])))
            E = qmat(conj(op.get('LocalProxyToParentProxy#Rotor', [0, 0, 0, 1])))
            L = qmat(conj(op.get('LocalToLocalProxy#Rotor', [0, 0, 0, 1])))
        else:
            P = L = np.eye(3)
            E = qmat(conj(data['Orientation#Rotor'])) if 'Orientation#Rotor' in data else np.eye(3)
        if 'euler' in o:
            E = euler_mat([v or 0.0 for v in o['euler']], o.get('order', 'ZXY'))
        if 'quat' in o and all(v is not None for v in o['quat']):
            E = qmat(conj(o['quat']))
        if 'rotor_euler' in o:
            E = euler_mat(o['rotor_euler'][0], o['rotor_euler'][1])
        M = np.eye(4)
        M[:3, :3] = P @ E @ L @ np.diag(sc)
        M[:3, 3] = tr
        return M

    def chain(self, di, st, stop=None):
        """Local of di composed with its BasisDataNodeRef parents (up to `stop`)."""
        M = self.local(di, st)
        p = self.dn[di].get('BasisDataNodeRef')
        guard = 0                   # protects against reference loops
        while p is not None and p != stop and guard < 64:
            M = self.local(p, st) @ M
            p = self.dn[p].get('BasisDataNodeRef')
            guard += 1
        return M

    def value(self, path, st, default=None):
        """Current scalar value of the DataNode at `path` (animated or stored)."""
        i = self.path_index.get(path)
        if i is None:
            return default
        o = st.get(i, {})
        if 'value' in o and o['value'] and o['value'][0] is not None:
            return o['value'][0]
        d = self.dn[i].get('Data')
        return d if isinstance(d, (int, float)) else default

    def node_path(self, ni):
        """DataNode path (without '/Transform') bound to scene node ni."""
        di = self.bind.get(ni)
        return self.paths[di][:-len('/Transform')] if di is not None else None

    def float_outputs(self):
        """Animated scalar DataNodes: [(index, path)] (materials, focal length, visibility)."""
        out = set()
        for clip, outputs in self.clips:
            for tgt in clip.animated_targets():
                if tgt < len(outputs):
                    i = outputs[tgt]
                    if self.dn[i].get('Type') in ('Float', 'Bool'):
                        out.add(i)
        return sorted((i, self.paths[i]) for i in out)

    def layer_for(self, namespace):
        """The layer clip played into the character with this namespace (or None)."""
        return next((ly for ly in self.layers if ly['namespace'] == namespace), None)

    def character_anchors(self):
        """[(reference dict, Transform DataNode index)] for referenced characters."""
        out = []
        for r in self.references:
            ns = r.get('Namespace', '')
            hit = next((i for i, p in enumerate(self.paths) if p.endswith('/%s:Anchor/Transform' % ns)
                        and self.dn[i].get('Type') in ('Transform', 'Dilation')), None)
            out.append((r, hit))
        return out
