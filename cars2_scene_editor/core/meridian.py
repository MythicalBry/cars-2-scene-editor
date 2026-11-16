"""Meridian (.mer) graph editing.

File layout (Cars 2):
    MeridianNodePool   MNode#i  - flat, global node list
    MeridianLinkPool   MLink#i  - {Type, Uuid, Origin, Target}
    MeridianNodeAssociationPool (some worlds) - {Type, Owner, Collector}

Both pools are split into namespace blocks (NameSpaceBegin ... NameSpaceEnd).
The first block is the world's own graph (its name is the `MeridianNameSpace`
used by the .ap file); later blocks are merged reference graphs.

Node references (Parent, Group, Begin, Playing, link Origin/Target, ...) are
*global* indices into MeridianNodePool (the engine subtracts the block's first
index), and links never
cross blocks.  So adding nodes to the world block means every reference in the
later blocks has to move by the number of nodes added; `insert_nodes` does
that.  Which int fields are references was derived from every shipping Cars 2
world: inside a later block, a reference field always holds an index inside
that block, while the only int value fields that can land in that range are
listed in VALUE_FIELDS / TYPED_VALUE_FIELDS.

What Meridian is: the game's visual-scripting graph. Nodes are things
(locators, triggers, choreographies, spawners, counters ...) and their
"ports" (sources, events, handlers); links carry data or impulses between
ports, e.g. "level loaded (Catalyst) -> Impulse -> play choreography".
Each node's ports are separate nodes named '<Owner> / <Port>'.
"""
import os

# int fields that hold values, not node indices (never shifted)
VALUE_FIELDS = {'IsRootObject', 'NumInLinks', 'NumOutLinks', 'IsProxy', 'SceneNodeIndex',
                'Serialize', 'GraphVersion'}
TYPED_VALUE_FIELDS = {
    ('IntFetch', 'Value'), ('IntVariable', 'Value'),
    ('CarsPlayerHandle', 'Player'),
    ('G2G_Input_Impulse_Channel', 'Index'), ('G2G_Input_Impulse_Float_Channel', 'Index'),
    ('G2G_Input_Impulse_Spline_Channel', 'Index'), ('G2G_Output_Impulse_Channel', 'Index'),
    ('G2G_Output_Impulse_Channel_Targeted', 'Index'),
}

# on/off settings every Choreography node carries
CHOREO_FLAGS = ('loop', 'visible_before', 'visible_after', 'enableAsyncLoad', 'enableMusicSync',
                'hide_gameplay_elements', 'use_master', 'fullScreen_CHO')


def new_uuid():
    """A fresh random 16-byte UUID (every node and link has one)."""
    return os.urandom(16)


def uuid_hex(b):
    """UUID bytes -> upper-case hex (the form the .ap file uses)."""
    return b.hex().upper() if isinstance(b, (bytes, bytearray)) else ''


def choreo_path(name):
    """'ExplosionFireyB' -> 'choreographies/ExplosionFireyB/ExplosionFireyB';
    full paths are kept as typed."""
    name = name.strip().replace('\\', '/')
    if name.lower().endswith('.oct'):
        name = name[:-4]
    if '/' in name:
        return name
    return 'choreographies/%s/%s' % (name, name)


class Graph:
    """Editable Meridian graph: nodes and links as lists, written back by apply()."""

    def __init__(self, tree):
        self.tree = tree
        self.nodes = list(tree['MeridianNodePool'].values())
        self.links = list(tree['MeridianLinkPool'].values())
        assoc = tree.get('MeridianNodeAssociationPool')
        self.assocs = list(assoc.values()) if assoc is not None else None
        self._reindex()

    # -- structure -------------------------------------------------------
    def _reindex(self):
        """Rebuild the lookups (UUID -> node, node -> links) after any change."""
        self.uuid_index = {}
        for i, n in enumerate(self.nodes):
            u = n.get('Uuid')
            if isinstance(u, (bytes, bytearray)):
                self.uuid_index[bytes(u)] = i
        self.in_links = {}
        self.out_links = {}
        for li, l in enumerate(self.links):
            if 'Origin' in l:
                self.out_links.setdefault(l['Origin'], []).append(li)
                self.in_links.setdefault(l['Target'], []).append(li)

    @staticmethod
    def blocks(items):
        """[(namespace name, begin index, end index)] of a node or link list."""
        out, start, name = [], None, None
        for i, n in enumerate(items):
            t = n.get('Type')
            if t == 'NameSpaceBegin':
                start, name = i, n.get('Name')
            elif t == 'NameSpaceEnd' and start is not None:
                out.append((name, start, i))
                start = None
        return out

    @property
    def namespace(self):
        """The world's own namespace name (first block)."""
        b = self.blocks(self.nodes)
        return b[0][0] if b else ''

    def find_uuid(self, u):
        """Node index with UUID u (bytes or hex text), or None."""
        if isinstance(u, str):
            try:
                u = bytes.fromhex(u)
            except ValueError:
                return None
        return self.uuid_index.get(bytes(u))

    def owned(self, i):
        """Nodes that belong to node i (its ports): Parent == i, or authored
        under 'Name / ...', searched near i (the exporter writes them first)."""
        name = self.nodes[i].get('AuthoredName', None)
        out = []
        lo = max(0, i - 64)
        for j in range(lo, min(len(self.nodes), i + 64)):
            if j == i:
                continue
            n = self.nodes[j]
            if n.get('Parent') == i or (name and n.get('AuthoredName', '').startswith(name + ' / ')):
                out.append(j)
        return out

    # -- queries -----------------------------------------------------------
    def of_type(self, ty):
        """Indices of every node of a type (e.g. 'Choreography')."""
        return [i for i, n in enumerate(self.nodes) if n.get('Type') == ty]

    def link_sources(self, target, ltype=None):
        """Nodes linked into `target` (optionally only links of one type)."""
        return [self.links[li]['Origin'] for li in self.in_links.get(target, [])
                if ltype is None or self.links[li]['Type'] == ltype]

    def link_targets(self, origin, ltype=None):
        """Nodes `origin` links to (optionally only links of one type)."""
        return [self.links[li]['Target'] for li in self.out_links.get(origin, [])
                if ltype is None or self.links[li]['Type'] == ltype]

    def choreo_anchors(self, ci):
        """[(ref_node_index, anchor_name, transform_source_index or None)]"""
        out = []
        for j in self.owned_refs(ci):
            n = self.nodes[j]
            src = self.link_sources(n.get('In', -1), 'TransformFetch') if 'In' in n else []
            out.append((j, n.get('Name', ''), src[0] if src else None))
        return out

    def owned_refs(self, ci):
        """The ChoreographyTransformReference nodes (anchor slots) of choreography ci."""
        return [j for j, n in enumerate(self.nodes)
                if n.get('Type') == 'ChoreographyTransformReference' and n.get('Parent') == ci]

    def locator_of_transform_source(self, ts):
        """LocatorNode / InstanceNode / ActorTransform that owns TransformSource ts."""
        # the owner is written right after its ports and points back with 'Transform'
        for j in range(ts + 1, min(len(self.nodes), ts + 8)):
            if self.nodes[j].get('Transform') == ts:
                return j
        return None

    def port_owners(self):
        """port node -> owning node ('Owner / Port' authored names, linked by a field)."""
        if getattr(self, '_owners', None) is None:
            self._owners = {}
            for j, n in enumerate(self.nodes):
                name = n.get('AuthoredName')
                if not name or not n.get('IsRootObject'):
                    continue
                pre = name + ' / '
                # any int field of the owner that points at a node named 'Owner / ...'
                for key, v in n.items():
                    if isinstance(v, int) and not isinstance(v, bool) and key not in VALUE_FIELDS \
                            and 0 <= v < len(self.nodes) and \
                            str(self.nodes[v].get('AuthoredName', '')).startswith(pre):
                        self._owners[v] = j
        return self._owners

    def ports_of(self, j):
        """The port nodes owned by node j."""
        return [p for p, o in self.port_owners().items() if o == j]

    def markup_lists(self):
        """Named location lists the game reads through DataMarkupNode (GetMarkup):
        [{'markup': node, 'name': MarkupName, 'kind': 'Transform'|'Actor',
          'sources': [TransformSource/ActorSource feeding it], 'entries': {source: entry port}}]
        Traced back from the markup's DataList through list writes; a locator
        joins a list by a TransformFetch link from its TransformSource to an
        entry port (a SetTransformList's Read port in shipping worlds)."""
        out = []
        owners = self.port_owners()
        # nodes that pass lists along / link types that carry list data
        hubs = ('TransformList', 'SetTransformList', 'ActorList', 'SetActorList', 'TransformMarkup',
                'ActorMarkup', 'TransformCombiner')
        data = ('TransformFetch', 'TransformListWrite', 'TransformListFetch', 'ActorFetch',
                'ActorListWrite', 'ActorListFetch')
        for mi in self.of_type('TransformMarkup') + self.of_type('ActorMarkup'):
            n = self.nodes[mi]
            start = n.get('DataList')
            if start is None:
                continue
            # walk the links backwards from the markup's list until we reach
            # the locators (TransformSource / ActorSource of a placed thing)
            seen, stack = {start}, [start]
            sources, entries = [], {}
            while stack:
                x = stack.pop()
                for li in self.in_links.get(x, []):
                    l = self.links[li]
                    if l['Type'] not in data:
                        continue
                    o = l['Origin']
                    on = self.nodes[o]
                    if on.get('Type') in ('TransformSource', 'ActorSource') and \
                            self.nodes[owners.get(o, o)].get('Type') in ('LocatorNode', 'InstanceNode', 'ActorTransform',
                                                                         'SpawnActorNode', 'SpawnAgentNode'):
                        if o not in entries:
                            sources.append(o)
                            entries[o] = x
                        continue
                    if o not in seen:
                        seen.add(o)
                        stack.append(o)
                # list-combining nodes: continue through all their ports
                own = owners.get(x, x)
                if self.nodes[own].get('Type') in hubs:
                    for p in self.ports_of(own):
                        if p not in seen:
                            seen.add(p)
                            stack.append(p)
            out.append({'markup': mi, 'name': n.get('MarkupName', ''), 'kind': n['Type'][:-6],
                        'sources': sources, 'entries': entries})
        return out

    def owner_of_source(self, ts):
        """The node owning source port ts (by port name, else by position)."""
        o = self.port_owners().get(ts)
        return o if o is not None else self.locator_of_transform_source(ts)

    def trigger_ports(self, group_index):
        """WalkInTrigger NodeGroup -> dict(inside=EventSurge, begin=Event, end=Event, who, activate...)."""
        out = {}
        for j in self.owned(group_index):
            n = self.nodes[j]
            leaf = n.get('LeafName', '')
            if n.get('Type') == 'EventSurge' and leaf == 'ActorInside':
                out['inside'] = j
                out['begin'] = n.get('Begin')
                out['end'] = n.get('End')
            elif leaf == 'ActorInside.Who':
                out['who'] = j
            elif leaf in ('Activate', 'Deactivate', 'Toggle'):
                out[leaf.lower()] = j
        return out

    # -- editing -----------------------------------------------------------
    def _is_ref(self, node, key, value, lo, hi):
        """Is field `key` of `node` a node reference inside block (lo, hi)?"""
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if key in VALUE_FIELDS or (node.get('Type'), key) in TYPED_VALUE_FIELDS:
            return False
        return lo < value < hi

    def insert_nodes(self, new_nodes):
        """Append nodes to the end of the world namespace block.

        new_nodes may refer to each other with *local* indices given as
        ('@', k) tuples (k = position in new_nodes); they are resolved here.
        Returns the global indices assigned."""
        blocks = self.blocks(self.nodes)
        if not blocks:
            raise ValueError('no NameSpaceBegin/End in this .mer')
        _, s0, e0 = blocks[0]
        k = len(new_nodes)
        at = e0  # insert before the world block's NameSpaceEnd
        # shift every reference in later blocks
        for (_, s, e) in blocks[1:]:
            for j in range(s, e + 1):
                n = self.nodes[j]
                for key, v in list(n.items()):
                    if self._is_ref(n, key, v, s, e):
                        n[key] = v + k
        # links and associations pointing past the insertion point move too
        for l in self.links:
            if 'Origin' in l:
                if l['Origin'] >= at:
                    l['Origin'] += k
                if l['Target'] >= at:
                    l['Target'] += k
        if self.assocs is not None:
            for a in self.assocs:
                for key in ('Owner', 'Collector'):
                    if isinstance(a.get(key), int) and a[key] >= at:
                        a[key] += k
        # resolve local refs
        assigned = list(range(at, at + k))

        def res(v):
            if isinstance(v, tuple) and len(v) == 2 and v[0] == '@':
                return assigned[v[1]]
            return v
        for n in new_nodes:
            for key in list(n):
                n[key] = res(n[key])
        self.nodes[at:at] = new_nodes
        self._reindex()
        return assigned

    def add_link(self, ltype, origin, target, element_fetch=False):
        """Add a link (e.g. 'Impulse', 'TransformFetch') in the world block,
        keeping the nodes' link counters right. Returns the link index."""
        blocks = self.blocks(self.links)
        at = blocks[0][2]           # before the world block's NameSpaceEnd
        l = {'Type': ltype, 'Uuid': new_uuid(), 'Origin': origin, 'Target': target}
        if element_fetch:
            l['IsElementFetch'] = 1
        self.links.insert(at, l)
        self._bump(origin, 'NumOutLinks', +1)
        self._bump(target, 'NumInLinks', +1)
        self._reindex()
        return at

    def remove_link(self, li):
        """Delete link li and update the counters."""
        l = self.links.pop(li)
        self._bump(l['Origin'], 'NumOutLinks', -1)
        self._bump(l['Target'], 'NumInLinks', -1)
        self._reindex()

    def _bump(self, i, key, d):
        """Change a node's NumInLinks / NumOutLinks by d (the field is left
        out when it reaches 0, like the shipping files)."""
        n = self.nodes[i]
        v = n.get(key, 0) + d
        if v > 0:
            if key in n:
                n[key] = v
            else:
                # NumOutLinks precedes NumInLinks when both exist; just append
                n[key] = v
        elif key in n:
            del n[key]

    # -- builders ----------------------------------------------------------
    def unique_authored(self, base):
        """base, or base2, base3 ... - an AuthoredName not used yet."""
        names = {n.get('AuthoredName') for n in self.nodes}
        if base not in names:
            return base
        i = 2
        while '%s%d' % (base, i) in names:
            i += 1
        return '%s%d' % (base, i)

    def add_choreography(self, choreography_name, authored=None, flags=None):
        """Builds Event/Event/EventSurge/Handler/Handler/Choreography exactly
        like the shipping exporter does. Returns the Choreography's index."""
        authored = self.unique_authored(authored or 'Choreography')
        flags = dict(flags or {})
        ch = {'Type': 'Choreography', 'Uuid': new_uuid(), 'Playing': ('@', 2), 'Stop': ('@', 3),
              'Restart': ('@', 4), 'choreography_name': choreo_path(choreography_name)}
        for f in CHOREO_FLAGS:
            ch[f] = int(bool(flags.get(f, 0)))
        ch['IsRootObject'] = 1
        ch['AuthoredName'] = authored
        # ports first (0..4), the Choreography itself last (5)
        nodes = [
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Playing / Begin~'},
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Playing / End~'},
            {'Type': 'EventSurge', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Playing',
             'Begin': ('@', 0), 'End': ('@', 1)},
            {'Type': 'Handler', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Stop'},
            {'Type': 'Handler', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Restart'},
            ch,
        ]
        idx = self.insert_nodes(nodes)
        return idx[5]

    def add_choreo_anchor(self, ci, anchor_name, transform_source):
        """ChoreographyTransformReference(Name=anchor) on choreography ci, fed
        from transform_source (a locator's TransformSource) by a TransformFetch link."""
        authored = self.unique_authored('Choreography Transform Reference')
        nodes = [
            {'Type': 'TransformFetch', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Transform'},
            {'Type': 'ChoreographyTransformReference', 'Uuid': new_uuid(), 'Parent': ci,
             'In': ('@', 0), 'Name': anchor_name, 'AuthoredName': authored},
        ]
        fetch, ref = self.insert_nodes(nodes)
        # insert_nodes appended before ci? no - ci is inside the world block and
        # unaffected, new nodes land after it.
        if transform_source is not None:
            self.add_link('TransformFetch', transform_source, fetch)
        return ref

    def set_anchor_source(self, ref_index, transform_source):
        """Re-point an anchor slot at another locator (or none)."""
        fetch = self.nodes[ref_index]['In']
        for li in list(self.in_links.get(fetch, [])):
            if self.links[li]['Type'] == 'TransformFetch':
                self.remove_link(li)
        if transform_source is not None:
            self.add_link('TransformFetch', transform_source, fetch)

    def add_locator(self, name, scene_node_index):
        """LocatorNode + its five source ports. Returns (locator, transform_source)."""
        authored = name
        nodes = [
            {'Type': 'InstanceSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Handle'},
            {'Type': 'FloatSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Heading'},
            {'Type': 'Vector3dSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Scale'},
            {'Type': 'Vector3dSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Euler'},
            {'Type': 'TransformSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Transform'},
            {'Type': 'LocatorNode', 'Uuid': new_uuid(), 'SceneNodeIndex': scene_node_index,
             'Handle': ('@', 0), 'Heading': ('@', 1), 'Scale': ('@', 2), 'Euler': ('@', 3),
             'Transform': ('@', 4), 'IsRootObject': 1, 'AuthoredName': authored,
             'BoundObjectName': name},
        ]
        idx = self.insert_nodes(nodes)
        return idx[5], idx[4]

    def add_transform_variable(self, authored='Transform Variable'):
        """TransformVariable with its Pre / Post Change events. Readers link
        from the variable itself (TransformFetch); a SetTransform's Write port
        writes it (TransformWrite). Returns its index."""
        a = self.unique_authored(authored)
        nodes = [
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': a + ' / Pre Change'},
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': a + ' / Post Change'},
            {'Type': 'TransformVariable', 'Uuid': new_uuid(), 'PreChange': ('@', 0), 'PostChange': ('@', 1),
             'IsRootObject': 1, 'AuthoredName': a},
        ]
        return self.insert_nodes(nodes)[2]

    def add_set_transform(self, source, variable, authored='Transform Write'):
        """SetTransform copying `source` (a TransformSource / variable) into
        `variable` when an impulse reaches it, then passing the impulse on.
        Returns its index."""
        a = self.unique_authored(authored)
        nodes = [
            {'Type': 'TransformWrite', 'Uuid': new_uuid(), 'AuthoredName': a + ' / Write'},
            {'Type': 'TransformFetch', 'Uuid': new_uuid(), 'AuthoredName': a + ' / Read'},
            {'Type': 'SetTransform', 'Uuid': new_uuid(), 'Write': ('@', 0), 'Fetch': ('@', 1),
             'IsRootObject': 1, 'AuthoredName': a},
        ]
        wr, rd, st = self.insert_nodes(nodes)
        self.add_link('TransformFetch', source, rd)
        self.add_link('TransformWrite', wr, variable)
        return st

    def add_message_handler(self, message, authored='Message Handler - Impulse'):
        """MessageHandler for `message` (fires an impulse when it arrives). Returns its index."""
        a = self.unique_authored(authored)
        nodes = [
            {'Type': 'ActorSource', 'Uuid': new_uuid(), 'AuthoredName': a + ' / Sender'},
            {'Type': 'MessageHandler', 'Uuid': new_uuid(), 'Sender': ('@', 0), 'MessageName': message,
             'IsRootObject': 1, 'AuthoredName': a},
        ]
        return self.insert_nodes(nodes)[1]

    def add_race_finish(self, locator_ts, radius, message='EndTutorial', authored='Race Finish'):
        """End the race when a human player gets within `radius` of a locator
        (point-to-point tracks).

        Built from shipping pieces: TransformToVector3d (the locator's
        position) -> ProximityNode.ComparePos; CarsPlayerHandle 0..3 ->
        ProximityNode.Actor (players only, AI cars never trigger it; air's
        luggage-chatter proximity is built the same way); Inside / Begin ->
        MessageSender. The game's RaceManager handles the message EndTutorial
        in every race (registered in its constructor; the tutorials end their
        race with it): SetRaceState(5), the race-over state that shows the
        results. Returns the ProximityNode's index."""
        from c2oct import FloatList
        a = self.unique_authored(authored)
        ttv = self.insert_nodes([
            {'Type': 'Vector3dSource', 'Uuid': new_uuid(), 'AuthoredName': a + ' Position / Position'},
            {'Type': 'TransformToVector3d', 'Uuid': new_uuid(), 'Output': ('@', 0), 'CastingOption': 'Position',
             'IsRootObject': 1, 'AuthoredName': a + ' Position'},
        ])
        handles = []
        for player in (3, 2, 1, 0):
            h = self.insert_nodes([
                {'Type': 'ActorSource', 'Uuid': new_uuid(), 'AuthoredName': '%s Player %d / Handle' % (a, player + 1)},
                {'Type': 'CarsPlayerHandle', 'Uuid': new_uuid(), 'Handle': ('@', 0), 'Player': player,
                 'IsRootObject': 1, 'AuthoredName': '%s Player %d' % (a, player + 1)},
            ])
            handles.append(h[0])
        sender = self.insert_nodes([
            {'Type': 'MessageSender', 'Uuid': new_uuid(), 'MessageName': message, 'MessageTarget': '',
             'MessageData': '', 'IsRootObject': 1, 'AuthoredName': a + ' Message'},
        ])[0]
        px = a + ' Proximity'
        prox = self.insert_nodes([
            {'Type': 'Vector3dFetch', 'Uuid': new_uuid(), 'Value': FloatList([0.0, 0.0, 0.0]),
             'AuthoredName': px + ' / ComparePos'},                                              # 0
            {'Type': 'ActorListFetch', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Actor'},     # 1
            {'Type': 'FloatFetch', 'Uuid': new_uuid(), 'Value': float(radius),
             'AuthoredName': px + ' / Distance'},                                                # 2
            {'Type': 'FloatFetch', 'Uuid': new_uuid(), 'Value': 0.0, 'AuthoredName': px + ' / MinDistance'},  # 3
            {'Type': 'Handler', 'Uuid': new_uuid(), 'AuthoredName': px + ' / On'},               # 4
            {'Type': 'Handler', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Off'},              # 5
            {'Type': 'Handler', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Toggle'},           # 6
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Inside / Begin~'},    # 7
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Inside / End~'},      # 8
            {'Type': 'EventSurge', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Inside',
             'Begin': ('@', 7), 'End': ('@', 8)},                                                # 9
            {'Type': 'FloatSource', 'Uuid': new_uuid(), 'AuthoredName': px + ' / DistanceAway'},  # 10
            {'Type': 'FloatSource', 'Uuid': new_uuid(), 'AuthoredName': px + ' / ScaledDistanceAway'},  # 11
            {'Type': 'ActorSource', 'Uuid': new_uuid(), 'AuthoredName': px + ' / Who'},          # 12
            {'Type': 'ProximityNode', 'Uuid': new_uuid(), 'ComparePos': ('@', 0), 'Actor': ('@', 1),
             'Distance': ('@', 2), 'MinDistance': ('@', 3), 'On': ('@', 4), 'Off': ('@', 5),
             'Toggle': ('@', 6), 'ActorInside': ('@', 9), 'DistanceAway': ('@', 10),
             'ScaledDistanceAway': ('@', 11), 'Who': ('@', 12), 'InitialState': 1,
             'IsRootObject': 1, 'AuthoredName': px},                                             # 13
        ])
        # by UUID from here on: every insertion above shifted nothing before
        # it, but resolve fresh anyway
        u = lambda i: bytes(self.nodes[i]['Uuid'])
        ttv_u, sender_u = u(ttv[1]), u(sender)
        pos_u, cmp_u, act_u, begin_u = u(ttv[0]), u(prox[0]), u(prox[1]), u(prox[7])
        handle_u = [u(h) for h in handles]
        ts_u = u(locator_ts)
        prox_u = u(prox[13])
        ix = lambda b: self.uuid_index[b]
        self.add_link('TransformFetch', ix(ts_u), ix(ttv_u))
        self.add_link('Vector3dFetch', ix(pos_u), ix(cmp_u))
        for h in handle_u:
            self.add_link('ActorFetch', ix(h), ix(act_u))
        self.add_link('Impulse', ix(begin_u), ix(sender_u))
        return ix(prox_u)

    def add_catalyst(self, authored='Catalyst - Impulse'):
        """Catalyst: fires one Impulse when the graph starts (level load)."""
        n = {'Type': 'Catalyst', 'Uuid': new_uuid(), 'Serialize': 0, 'IsRootObject': 1,
             'AuthoredName': self.unique_authored(authored)}
        return self.insert_nodes([n])[0]

    def start_catalysts(self, ci):
        """Catalysts linked straight to node ci."""
        return [o for o in self.link_sources(ci, 'Impulse') if self.nodes[o].get('Type') == 'Catalyst']

    def choreo_actor_refs(self, ci):
        """Names of the actor slots already bound on choreography ci."""
        return [n.get('Name', '') for n in self.nodes
                if n.get('Type') == 'ChoreographyActorReference' and n.get('Parent') == ci]

    def add_actor_group(self, name):
        """NodeGroup of an .ap actor with its Destroyed/Died events and Handle,
        laid out like the shipping actor groups. Returns (group, handle)."""
        nodes = [
            {'Type': 'NodeGroup', 'Uuid': new_uuid(), 'IsRootObject': 1, 'AuthoredName': name},
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': name + ' / Destroyed', 'Parent': ('@', 0),
             'LeafName': 'Died'},
            {'Type': 'Event', 'Uuid': new_uuid(), 'AuthoredName': name + ' / Died', 'Parent': ('@', 0),
             'LeafName': 'DiedButNotReset'},
            {'Type': 'ActorSource', 'Uuid': new_uuid(), 'AuthoredName': name + ' / Handle', 'Parent': ('@', 0),
             'LeafName': 'this'},
        ]
        idx = self.insert_nodes(nodes)
        return idx[0], idx[3]

    def add_choreo_actor(self, ci, slot, handle):
        """ChoreographyActorReference(Name=slot) on choreography ci, given the
        actor from `handle` (an ActorSource) by an ActorFetch link."""
        authored = self.unique_authored('Choreography Actor Reference')
        nodes = [
            {'Type': 'ActorFetch', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Actor'},
            {'Type': 'ActorSource', 'Uuid': new_uuid(), 'AuthoredName': authored + ' / Actor'},
            {'Type': 'ChoreographyActorReference', 'Uuid': new_uuid(), 'Parent': ci, 'In': ('@', 0),
             'Who': ('@', 1), 'Name': slot, 'AuthoredName': authored},
        ]
        fetch, _, ref = self.insert_nodes(nodes)
        self.add_link('ActorFetch', handle, fetch)
        return ref

    def connect_impulse(self, origin_event, target):
        """Fire `target` when `origin_event` happens."""
        self.add_link('Impulse', origin_event, target)

    # -- output ------------------------------------------------------------
    def apply(self):
        """Write nodes/links back into the tree (keys MNode#i / MLink#i)."""
        def pool(prefix, items):
            return {'%s#%d' % (prefix, i): n for i, n in enumerate(items)}
        self.tree['MeridianNodePool'] = pool('MNode', self.nodes)
        self.tree['MeridianLinkPool'] = pool('MLink', self.links)
        if self.assocs is not None:
            self.tree['MeridianNodeAssociationPool'] = pool('MNodeAssociation', self.assocs)
        return self.tree


def validate(graph):
    """Cheap structural checks after editing; returns a list of problems."""
    problems = []
    N = graph.nodes
    # references must stay inside their own namespace block
    for name, s, e in Graph.blocks(N):
        for j in range(s + 1, e):
            n = N[j]
            for key, v in n.items():
                if key in ('Parent', 'Group', 'Begin', 'End', 'Playing', 'Stop', 'Restart', 'In',
                           'Handle', 'Transform') and isinstance(v, int):
                    if not (s < v < e):
                        problems.append('%s: node %d field %s=%d outside its block %d..%d' % (name, j, key, v, s, e))
    # links must not cross blocks
    for (name, s, e), (_, ls, le) in zip(Graph.blocks(N), Graph.blocks(graph.links)):
        for li in range(ls + 1, le):
            l = graph.links[li]
            if 'Origin' in l and not (s < l['Origin'] < e and s < l['Target'] < e):
                problems.append('%s: link %d %s->%s crosses its block' % (name, li, l['Origin'], l['Target']))
    return problems
