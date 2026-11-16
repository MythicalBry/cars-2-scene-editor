"""AI track graph (.oct Track* pools) and starting-grid layout.

Pools (loaded by sdaemon FUN_0040d400 -> AITrack::cTrack; every field that is
missing takes the Track's Default* value, so the exporter only writes values
that differ):
    TrackPool[0]          counts + Default* values + ID_COLLECT_TRACK_* and
                          TYPE_* constants
    TrackNodePool         Position, Rotation (3x3 rows: across, up, forward),
                          Width, Fast/EnergyLineUTrackA/B/C (0 = one edge, 1 =
                          the other), CustomDataIndex -> TrackNodeCustomData
                          (IgnoreOtherPlayers, EndOfRaceMarker, SpeedLimit,
                          LookAheadDistance)
    TrackLinkPool         Node1Index -> Node2Index (direction of travel),
                          StraightawayLength, AvoidanceBitfield (9 bits),
                          CustomDataIndex -> TrackLinkCustomData (ForceDrifting,
                          DriftLeft, BashZone, CanRespawn, Fast/EnergyLine
                          Hop/TurboV TrackA/B/C)
    TrackSectionPool      BranchName, CustomDataIndex -> TrackSectionCustomData
                          (SplitChance, SplitMinSpeed, EnergyPath, WeaponPath,
                          InTheZone/HighPerformanceShortcutPath,
                          OnlyIfOnRacingLine, ClosedForOffTrackCheck,
                          LimitToBestAI, NeedsTurbo, OnFast/EnergyLine and
                          Valid per track A/B/C), FrontIndex (first link;
                          written by the exporter, not read by the game)
    TrackSplineMarkupPool ID (collect track 0/1/2), Type (TYPE_* below),
                          Position, Distance (along the lap)

Sections are the runs of links between branch/merge nodes. The game rebuilds
them on load: it walks the links in pool order and hands the next section
record to each link that has none yet, then grows it back and forward to
the nearest branch/merge (FUN_00433b50 / FUN_0043e690). So section records
must be ordered by the first link (in pool order) of each run.

Race progress itself is not this graph: the game's lap-path lookup (0x004f5340) uses the .dbl
B-spline named TrackA/TrackB/TrackC (fallback 'lane1') and progress =
NearestDistance / Length.

In short: the AI drives along a graph of nodes (points across the road,
with a width) joined by one-way links. A "racing line" is a fraction U
between the two road edges at each node. Branches are where one node has
several outgoing links (shortcuts).
"""
import math

# TrackSplineMarkup Type values: hints placed along the lap for the AI
MARKUP_TYPES = {0: 'BUNNY_HOP', 1: 'SIDESTEP_LEFT', 2: 'SIDESTEP_RIGHT', 3: 'DRIFT_LEFT',
                4: 'DRIFT_LEFT_END', 5: 'DRIFT_RIGHT', 6: 'DRIFT_RIGHT_END', 7: 'UNKNOWN_7'}

# field orders of the shipping files (written in this order for tidy output)
NODE_KEYS = ('Position', 'Rotation', 'FastLineUTrackA', 'EnergyLineUTrackA', 'FastLineUTrackB',
             'EnergyLineUTrackB', 'FastLineUTrackC', 'EnergyLineUTrackC', 'Width', 'CustomDataIndex')
LINK_KEYS = ('Node1Index', 'Node2Index', 'StraightawayLength', 'AvoidanceBitfield', 'CustomDataIndex')
SECTION_KEYS = ('BranchName', 'CustomDataIndex', 'FrontIndex')
MARKUP_KEYS = ('ID', 'Type', 'Position', 'Distance')
NODE_CD_KEYS = ('IgnoreOtherPlayers', 'EndOfRaceMarker', 'SpeedLimit', 'LookAheadDistance')
LINK_CD_KEYS = ('ForceDrifting', 'DriftLeft', 'BashZone', 'CanRespawn') + tuple(
    '%sLine%sVTrack%s' % (l, k, t) for t in 'ABC' for l in ('Fast', 'Energy') for k in ('Hop', 'Turbo'))
SECTION_CD_KEYS = ('SplitChance', 'SplitMinSpeed', 'EnergyPath', 'WeaponPath', 'InTheZoneShortcutPath',
                   'HighPerformanceShortcutPath', 'OnlyIfOnRacingLine', 'ClosedForOffTrackCheck',
                   'LimitToBestAI', 'NeedsTurbo') + tuple(
    '%s%s' % (k, 'Track' + t) for t in 'ABC' for k in ('OnFastLine', 'OnEnergyLine', 'Valid'))

# pool order the loader needs (custom data before the records that use it)
POOLS = ('TrackPool', 'TrackNodeCustomDataPool', 'TrackLinkCustomDataPool', 'TrackSectionCustomDataPool',
         'TrackNodePool', 'TrackLinkPool', 'TrackSectionPool', 'TrackSplineMarkupPool')


def _default_for(track, key):
    """Track default for a node/link/section field (per-track suffixes share
    one default: FastLineUTrackA -> DefaultNodeRacingLineU, HopV -> ...)."""
    if key.endswith(('LineUTrackA', 'LineUTrackB', 'LineUTrackC')):
        return track.get('DefaultNodeRacingLineU', 0.5)
    if 'LineHopV' in key:
        return track.get('DefaultLinkHopV', 100.0)
    if 'LineTurboV' in key:
        return track.get('DefaultLinkTurboV', 100.0)
    if key.startswith(('OnFastLine', 'OnEnergyLine')):
        return track.get('DefaultSectionOnRacingLine', 0)
    if key.startswith('Valid'):
        return track.get('DefaultSectionValid', 1)
    for prefix in ('DefaultNode', 'DefaultLink', 'DefaultSection'):
        if prefix + key in track:
            return track[prefix + key]
    return None


class Track:
    """Editable view of a world's Track* pools (tree = c2oct dict)."""

    def __init__(self, tree):
        self.tree = tree
        self.track = next(iter(tree['TrackPool'].values()))     # the single TrackPool entry (defaults, counts)
        self.nodes = list(tree.get('TrackNodePool', {}).values())
        self.links = list(tree.get('TrackLinkPool', {}).values())
        self.sections = list(tree.get('TrackSectionPool', {}).values())
        self.markups = list(tree.get('TrackSplineMarkupPool', {}).values())
        self.node_cd = list(tree.get('TrackNodeCustomDataPool', {}).values())
        self.link_cd = list(tree.get('TrackLinkCustomDataPool', {}).values())
        self.section_cd = list(tree.get('TrackSectionCustomDataPool', {}).values())

    @staticmethod
    def has_track(tree):
        """True when the world has an AI track graph (race worlds do; arenas don't)."""
        return bool(tree.get('TrackPool'))

    # -- field access with defaults ---------------------------------------
    # A value comes from the record itself, else its custom-data record,
    # else the Track's default.
    def node_value(self, i, key):
        n = self.nodes[i]
        if key in n:
            return n[key]
        if key in NODE_CD_KEYS and 'CustomDataIndex' in n:
            cd = self.node_cd[n['CustomDataIndex']]
            if key in cd:
                return cd[key]
        return _default_for(self.track, key)

    def link_value(self, i, key):
        l = self.links[i]
        if key in l:
            return l[key]
        if key in LINK_CD_KEYS and 'CustomDataIndex' in l:
            cd = self.link_cd[l['CustomDataIndex']]
            if key in cd:
                return cd[key]
        return _default_for(self.track, key)

    def section_value(self, i, key):
        s = self.sections[i]
        if key in s:
            return s[key]
        if 'CustomDataIndex' in s:
            cd = self.section_cd[s['CustomDataIndex']]
            if key in cd:
                return cd[key]
        return _default_for(self.track, key)

    # -- geometry -----------------------------------------------------------
    def frame(self, i):
        """(position, across, up, forward) of node i, game space."""
        n = self.nodes[i]
        p = tuple(float(x) for x in n['Position'])
        r = [float(x) for x in n['Rotation']]
        return p, tuple(r[0:3]), tuple(r[3:6]), tuple(r[6:9])

    def edges(self, i):
        """The two track edges at node i: position +- width/2 along 'across'
        (the game's left/right edge points, FUN_0043d0e0)."""
        p, a, _, _ = self.frame(i)
        h = 0.5 * float(self.node_value(i, 'Width'))
        return (tuple(p[k] + a[k] * h for k in range(3)), tuple(p[k] - a[k] * h for k in range(3)))

    def line_point(self, i, line='Fast', track='A'):
        """Racing-line point at node i: lerp between the edges by U."""
        e0, e1 = self.edges(i)
        u = float(self.node_value(i, '%sLineUTrack%s' % (line, track)))
        return tuple(e0[k] + (e1[k] - e0[k]) * u for k in range(3))

    # -- topology -------------------------------------------------------------
    def out_links(self):
        """{node: [links leaving it]}"""
        out = {}
        for li, l in enumerate(self.links):
            out.setdefault(l['Node1Index'], []).append(li)
        return out

    def in_links(self):
        """{node: [links arriving at it]}"""
        inn = {}
        for li, l in enumerate(self.links):
            inn.setdefault(l['Node2Index'], []).append(li)
        return inn

    def section_runs(self):
        """[(section index, [link indices])] as the game assigns them."""
        return section_runs_of(self.links)

    def check(self):
        """Problems the game would hit (list of strings)."""
        msgs = []
        runs = self.section_runs()
        if len(runs) != len(self.sections):
            msgs.append('%d link runs but %d section records' % (len(runs), len(self.sections)))
        for si, run in runs:
            if si < len(self.sections):
                fi = self.sections[si].get('FrontIndex')
                if fi is not None and fi != run[0]:
                    msgs.append('section %d FrontIndex %d, run starts at link %d' % (si, fi, run[0]))
        nn = len(self.nodes)
        for li, l in enumerate(self.links):
            if not (0 <= l['Node1Index'] < nn and 0 <= l['Node2Index'] < nn):
                msgs.append('link %d points outside the node pool' % li)
        return msgs

    # -- writing --------------------------------------------------------------
    @staticmethod
    def _ordered(d, keys):
        """Copy of d with `keys` first (in that order), then any other keys."""
        out = {k: d[k] for k in keys if k in d}
        out.update({k: v for k, v in d.items() if k not in out})
        return out

    def commit(self):
        """Write the lists back into the tree (canonical key order, counts,
        pool names). Sections are re-matched to the link runs and FrontIndex
        refreshed; keep self.sections in run order when editing."""
        runs = self.section_runs()
        # one section record per run: add plain ones / drop extras
        while len(self.sections) < len(runs):
            self.sections.append({'BranchName': 'AI_TrackLink%d' % (runs[len(self.sections)][1][0] + 1)})
        del self.sections[len(runs):]
        for si, run in runs:
            self.sections[si]['FrontIndex'] = run[0]
        t, tr = self.tree, self.track

        def pool(name, item, rows, keys):
            # write a pool as {'Item#0': ..., 'Item#1': ...}; keys None keeps each
            # record's own field order
            if rows or name in t:
                t[name] = {'%s#%d' % (item, i): (dict(r) if keys is None else self._ordered(r, keys))
                           for i, r in enumerate(rows)}
        # custom-data flags: the shipped files list them in no fixed order (the
        # game reads them by name), so keep each record's order for a byte-exact
        # round trip; records rebuilt from Blender are created in key order
        pool('TrackNodeCustomDataPool', 'TrackNodeCustomData', self.node_cd, None)
        pool('TrackLinkCustomDataPool', 'TrackLinkCustomData', self.link_cd, None)
        pool('TrackSectionCustomDataPool', 'TrackSectionCustomData', self.section_cd, None)
        # records keep their field order too: rebuild() lays rebuilt records out
        # like the file's (or in key order when new)
        pool('TrackNodePool', 'TrackNode', self.nodes, None)
        pool('TrackLinkPool', 'TrackLink', self.links, None)
        pool('TrackSectionPool', 'TrackSection', self.sections, None)
        pool('TrackSplineMarkupPool', 'TrackSplineMarkup', self.markups, None)
        # the counts in the TrackPool entry must match the pools
        tr['NodeCustomData'] = len(self.node_cd)
        tr['LinkCustomData'] = len(self.link_cd)
        tr['SectionCustomData'] = len(self.section_cd)
        tr['Nodes'], tr['Links'] = len(self.nodes), len(self.links)
        tr['Sections'], tr['SplineMarkup'] = len(self.sections), len(self.markups)
        # the loader resolves CustomDataIndex while reading nodes/links/sections,
        # so the custom-data pools must come first: keep the POOLS order, at
        # the place of the first Track pool
        items = list(t.items())
        track = {k: v for k, v in items if k in POOLS}
        t.clear()
        placed = False
        for k, v in items:
            if k in POOLS:
                if not placed:
                    t.update((p, track[p]) for p in POOLS if p in track)
                    placed = True
            else:
                t[k] = v

    def set_custom(self, kind, i, key, value):
        """Set a custom-data field on node/link/section i, sharing identical
        custom-data records the way the exporter does."""
        rows, cds = {'node': (self.nodes, self.node_cd), 'link': (self.links, self.link_cd),
                     'section': (self.sections, self.section_cd)}[kind]
        r = rows[i]
        cur = dict(cds[r['CustomDataIndex']]) if 'CustomDataIndex' in r else {}
        # a default value is simply left out
        if value is None or value == _default_for(self.track, key):
            cur.pop(key, None)
        else:
            cur[key] = value
        if not cur:
            r.pop('CustomDataIndex', None)
            return
        # reuse an identical record if there is one
        for ci, c in enumerate(cds):
            if c == cur:
                r['CustomDataIndex'] = ci
                return
        cds.append(cur)
        r['CustomDataIndex'] = len(cds) - 1


    # -- flat values (editor side) --------------------------------------------
    def node_values(self, i):
        """Every node field resolved against the defaults (custom data included)."""
        n = self.nodes[i]
        out = {k: self.node_value(i, k) for k in NODE_KEYS[2:9]}
        out.update({k: self.node_value(i, k) for k in NODE_CD_KEYS})
        out['Position'] = [float(x) for x in n['Position']]
        out['Rotation'] = [float(x) for x in n['Rotation']]
        return out

    def link_values(self, i):
        """Every link field resolved against the defaults."""
        l = self.links[i]
        out = {k: self.link_value(i, k) for k in ('StraightawayLength', 'AvoidanceBitfield') + LINK_CD_KEYS}
        out['Node1Index'], out['Node2Index'] = l['Node1Index'], l['Node2Index']
        return out

    def section_values(self, i):
        """Every section field resolved against the defaults."""
        out = {k: self.section_value(i, k) for k in SECTION_CD_KEYS}
        out['BranchName'] = self.sections[i].get('BranchName', '')
        return out

    def rebuild(self, nodes, links, sections, markups=None):
        """Replace the graph from flat values (as node_values/link_values/
        section_values give them; sections in run order). Writes only what
        differs from the Track defaults, the way the shipping exporter did:
        racing-line U always, Width/StraightawayLength/AvoidanceBitfield and
        custom data only when not default; identical custom-data records are
        shared, numbered in first-use order (nodes, links, sections)."""
        tr = self.track
        # the records read from the file: an unchanged record keeps its field order
        before = self.node_cd + self.link_cd + self.section_cd
        old = {'nodes': list(self.nodes), 'links': list(self.links), 'sections': list(self.sections),
               'markups': list(self.markups)}
        self.node_cd, self.link_cd, self.section_cd = [], [], []

        def cd_index(cds, values, keys):
            # custom-data record index for the non-default values (None if all default)
            d = {k: values[k] for k in keys if k in values and values[k] is not None
                 and values[k] != _default_for(tr, k)}
            if not d:
                return None
            for ci, c in enumerate(cds):
                if c == d:
                    return ci
            # dict equality ignores order: reuse the file's order for the same values
            d = dict(next((c for c in before if c == d), d))
            cds.append(d)
            return len(cds) - 1
        self.nodes = []
        for v in nodes:
            n = {'Position': FloatList_(v['Position']), 'Rotation': FloatList_(v['Rotation'])}
            for k in NODE_KEYS[2:8]:            # the six racing-line U values
                n[k] = float(v.get(k, tr.get('DefaultNodeRacingLineU', 0.5)))
            if v.get('Width') is not None and float(v['Width']) != tr.get('DefaultNodeWidth', 32.0):
                n['Width'] = float(v['Width'])
            ci = cd_index(self.node_cd, v, NODE_CD_KEYS)
            if ci is not None:
                n['CustomDataIndex'] = ci
            self.nodes.append(n)
        self.links = []
        for v in links:
            l = {'Node1Index': int(v['Node1Index']), 'Node2Index': int(v['Node2Index'])}
            if v.get('StraightawayLength') is not None and \
                    float(v['StraightawayLength']) != tr.get('DefaultLinkStraightawayLength', 0.0):
                l['StraightawayLength'] = float(v['StraightawayLength'])
            if v.get('AvoidanceBitfield') is not None and \
                    int(v['AvoidanceBitfield']) != tr.get('DefaultLinkAvoidanceBitfield', -1):
                l['AvoidanceBitfield'] = int(v['AvoidanceBitfield'])
            ci = cd_index(self.link_cd, v, LINK_CD_KEYS)
            if ci is not None:
                l['CustomDataIndex'] = ci
            self.links.append(l)
        self.sections = []
        for v in sections:
            s = {'BranchName': v.get('BranchName') or ''}
            ci = cd_index(self.section_cd, v, SECTION_CD_KEYS)
            if ci is not None:
                s['CustomDataIndex'] = ci
            self.sections.append(s)
        if markups is not None:
            self.markups = [{'ID': int(m['ID']), 'Type': int(m['Type']), 'Position': FloatList_(m['Position']),
                             'Distance': float(m['Distance'])} for m in markups]
        # field order: like the file's record at the same index, else key order
        for name, keys in (('nodes', NODE_KEYS), ('links', LINK_KEYS), ('sections', SECTION_KEYS),
                           ('markups', MARKUP_KEYS)):
            rows, prev = getattr(self, name), old[name]
            for i, r in enumerate(rows):
                if i < len(prev):
                    lay = [k for k in prev[i] if k in r] + [k for k in keys if k in r and k not in prev[i]]
                    lay += [k for k in r if k not in lay]
                    rows[i] = {k: r[k] for k in lay}
                else:
                    rows[i] = self._ordered(r, keys)
        self.commit()


def section_runs_of(links):
    """Link runs between branch/merge nodes, in the order the game hands out
    section records (links: dicts with Node1Index/Node2Index)."""
    out, inn = {}, {}
    for li, l in enumerate(links):
        out.setdefault(l['Node1Index'], []).append(li)
        inn.setdefault(l['Node2Index'], []).append(li)
    owner = [None] * len(links)      # run index of each link
    runs = []

    def boundary(node):
        # a run stops at any node that isn't "one in, one out"
        return len(out.get(node, ())) != 1 or len(inn.get(node, ())) != 1
    for li in range(len(links)):
        if owner[li] is not None:
            continue
        # walk back to the start of this link's run
        front = li
        seen = {li}
        while not boundary(links[front]['Node1Index']):
            prev = inn[links[front]['Node1Index']][0]
            if prev in seen:        # a loop with no branch: starts at li
                front = li
                break
            seen.add(prev)
            front = prev
        # then forward to its end, claiming the links
        run, cur = [], front
        while True:
            run.append(cur)
            owner[cur] = len(runs)
            end = links[cur]['Node2Index']
            if boundary(end):
                break
            cur = out[end][0]
            if cur == front or owner[cur] is not None:
                break
        runs.append(run)
    return list(enumerate(runs))


def FloatList_(v):
    """A c2oct FloatList (plain list if c2oct isn't importable)."""
    try:
        from c2oct import FloatList
        return FloatList(float(x) for x in v)
    except ImportError:
        return [float(x) for x in v]


def node_frame(prev_pos, pos, next_pos, up_hint=(0.0, 1.0, 0.0)):
    """Rotation rows (across, up, forward) for a node from its neighbours:
    forward along the road, up from the hint, across = up x forward (the
    shipping frames are right-handed with this layout)."""
    import numpy as np
    p = np.asarray(pos, float)
    # forward: average of the incoming and outgoing directions
    f = np.zeros(3)
    if next_pos is not None:
        f += np.asarray(next_pos, float) - p
    if prev_pos is not None:
        f += p - np.asarray(prev_pos, float)
    if np.linalg.norm(f) < 1e-6:
        f = np.array([0.0, 0.0, 1.0])
    f /= np.linalg.norm(f)
    # up: the hint with its forward part removed
    u = np.asarray(up_hint, float)
    u = u - f * (u @ f)
    if np.linalg.norm(u) < 1e-6:
        u = np.array([0.0, 1.0, 0.0]) - f * f[1]
    u /= np.linalg.norm(u)
    a = np.cross(u, f)
    return [float(x) for x in np.concatenate([a, u, f])]


# ----------------------------------------------------------------------------
# starting grid (Meridian StartingGrid node, sdaemon FUN_0056c9a0)
# ----------------------------------------------------------------------------

def grid_slot(slot, racers, row_count=6, h_spacing=6.0, v_spacing=8.0, stagger=0.0,
              custom23=False, custom32=False):
    """Offset of grid slot `slot` from the grid locator in its local frame:
    (across, back) where the world position is
    origin + across * row0 + back * row2 of the locator rotation
    (row0 = across, row2 = forward, as DirCos3x3 rows).
    Defaults are the node's serialized defaults (RowCount 6, spacing 6/8)."""
    # cars per row: 12 / RowCount (e.g. 6 rows -> 2 per row), capped by the racer count
    per_row = max(1, int(12 / row_count)) if row_count else 1
    cols = max(1, min(per_row, racers))
    row, col = divmod(slot, cols)
    if custom23 or custom32:
        # alternating rows of 2 and 3 (or 3 and 2): 5 cars per pair of rows
        first = 3 - (1 if custom23 else 0)
        k = slot % 5
        if k < first:
            row, col = (slot // 5) * 2, k
        else:
            row, col = (slot // 5) * 2 + 1, k - first
        if custom23:
            stagger = -stagger
    back = -row * v_spacing
    # centre the row, shift alternate rows by half the stagger
    across = (cols - 1) * h_spacing * 0.5
    across += stagger * 0.5 if row % 2 == 0 else -stagger * 0.5
    across -= col * h_spacing
    return across, back


def grid_positions(origin, rot_rows, racers, **kw):
    """World positions of every grid slot. rot_rows: 3 rows of the locator
    rotation (across, up, forward)."""
    out = []
    a, _, f = rot_rows
    for s in range(racers):
        x, z = grid_slot(s, racers, **kw)
        out.append(tuple(origin[k] + a[k] * x + f[k] * z for k in range(3)))
    return out


def polyline_length(points):
    """Total length of a polyline."""
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
