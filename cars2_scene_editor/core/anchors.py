"""Where a choreography anchor really is: follow the Meridian graph from the
transform an anchor fetches to something placed in the world.

A ChoreographyTransformReference ('ANC_00') reads a TransformSource. Shipping
worlds feed it in several ways, and only the first is a plain locator:

* LocatorNode / InstanceNode: the bound scene object.
* ActorTransform: the transform of an actor, fetched from
  - a NodeGroup's handle ('<actor> / Handle'): that actor's scene object;
  - a CarsPlayerHandle, a walk-in trigger's 'Who', a message 'Sender', a
    spawned agent ...: a racer, i.e. the player's car;
  - an ActorVariable: whatever SetActor wrote into it.
* TransformVariable / TransformList / markup lists: whatever SetTransform /
  list writes put in them (the first entry of a list).
* IndexTransformFromList: an entry of its list (the first).
* ImpulseTransformSplitter: the transform the impulse carries, i.e. what fired
  it: a list loop's entries, or a racer (lap / finish messages, triggers).

resolve() returns ('object', name), ('player', reason) or None (unresolved).
Used by the choreography preview (preview.py) to put the package's anchor
where the game does.
"""

# FloatFetch: a TransformCombiner builds a transform from floats a TransformSplitter took apart
FETCH = ('TransformFetch', 'ActorFetch', 'InstanceFetch', 'TransformListFetch', 'ActorListFetch', 'FloatFetch')
WRITE = ('TransformWrite', 'ActorWrite', 'TransformListWrite', 'ActorListWrite')
# nodes whose output is "a racer" in the game: the preview puts those on the player's start
RACER_OWNERS = ('CarsPlayerHandle', 'MessageHandler', 'SpawnAgentNode', 'SpawnActorNode', 'ProximityNode',
                'ImpulseHandleSplitterNode')
RACER_LEAVES = ('ActorInside.Who', 'Who', 'Sender', 'Racer', 'Player', 'Instigator')


class Resolver:
    def __init__(self, g):
        self.g = g
        # port -> owner, also for the proxies of merged graphs (IsProxy nodes
        # carry a 'Group' instead of IsRootObject, so port_owners skips them)
        self.owner = dict(g.port_owners())
        for j, n in enumerate(g.nodes):
            name = n.get('AuthoredName')
            if not name or n.get('IsRootObject') or not n.get('IsProxy') or ' / ' in name:
                continue
            pre = name + ' / '
            for key, v in n.items():
                if key in ('IsProxy', 'Group', 'NumInLinks', 'NumOutLinks') or not isinstance(v, int) or \
                        isinstance(v, bool) or not 0 <= v < len(g.nodes):
                    continue
                if str(g.nodes[v].get('AuthoredName', '')).startswith(pre):
                    self.owner.setdefault(v, j)

    def _own(self, i):
        return self.owner.get(i, i)

    def _ins(self, i, types):
        """Origins of the links of `types` into node / port i."""
        return [self.g.links[li]['Origin'] for li in self.g.in_links.get(i, [])
                if self.g.links[li].get('Type') in types]

    def _ports(self, o):
        n = self.g.nodes[o]
        return [v for k, v in n.items() if k not in ('IsProxy', 'Group', 'Parent', 'NumInLinks', 'NumOutLinks',
                                                       'IsRootObject', 'SceneNodeIndex')
                and isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(self.g.nodes)
                and self.owner.get(v) == o]

    def _ports_in(self, o, types):
        """Origins feeding any port of node o (and o itself), in field order."""
        out = self._ins(o, types)
        for p in self._ports(o):
            out += self._ins(p, types)
        return out

    def resolve(self, src, depth=0, seen=None):
        seen = set() if seen is None else seen
        if src is None or depth > 24 or src in seen:
            return None
        seen.add(src)
        g = self.g
        n = g.nodes[src]
        o = self._own(src)
        on = g.nodes[o]
        ty = on.get('Type', '')
        # a placed thing
        if on.get('BoundObjectName') and ty in ('LocatorNode', 'InstanceNode', 'ActorTransform', 'SplineNode'):
            return ('object', on['BoundObjectName'])
        # an actor's own handle: its NodeGroup names the actor (scene object / .ap section)
        if n.get('Type') == 'ActorSource' and isinstance(n.get('Parent'), int) and \
                g.nodes[n['Parent']].get('Type') == 'NodeGroup' and n.get('LeafName') in ('this', 'Handle', None):
            return ('object', g.nodes[n['Parent']].get('AuthoredName', ''))
        if ty in RACER_OWNERS or n.get('LeafName', '') in RACER_LEAVES or \
                any(n.get('AuthoredName', '').endswith('/ ' + x) or n.get('AuthoredName', '').endswith('/ %s~' % x)
                    for x in ('Who', 'Sender')):
            return ('player', on.get('AuthoredName', ty))
        if ty == 'ImpulseTransformSplitter':
            return self._impulse_origin(o, depth, seen)
        # variables and lists: what was written into them; everything else
        # (ActorTransform, IndexTransformFromList, loops ...): their fetch ports
        pushes = [x for p in self._ports(o) if not g.nodes[p].get('AuthoredName', '').endswith('/ Remove')
                  for x in self._ins(p, WRITE)]           # list PushHead / PushTail
        for s in self._ins(src, WRITE) + self._ins(o, WRITE) + pushes + self._ports_in(o, FETCH) +                 self._ins(src, FETCH):
            w = self._own(s)
            # a write node: follow what it fetched
            cands = self._ports_in(w, FETCH) if g.nodes[w].get('Type', '').startswith('Set') else [s]
            for c in cands:
                r = self.resolve(c, depth + 1, seen)
                if r is not None:
                    return r
        # actors the world fills at run time (Featured Cars, an empty ActorVariable ...) are racers
        if ty.startswith('Actor') or ty in ('IndexActorFromList', 'SetActor', 'SetActorList'):
            return ('player', on.get('AuthoredName', ty))
        return None

    def _impulse_origin(self, o, depth, seen):
        """What an ImpulseTransformSplitter's impulse carries: walk the impulses
        back to a list loop (its list's entries) or a racer event."""
        g = self.g
        stack, guard = [o], set()
        while stack and len(guard) < 200:
            x = stack.pop()
            if x in guard:
                continue
            guard.add(x)
            for p in [x] + self._ports(x):
                for li in g.in_links.get(p, []):
                    l = g.links[li]
                    if l.get('Type') != 'Impulse':
                        continue
                    src = self._own(l['Origin'])
                    st = g.nodes[src].get('Type', '')
                    if st in ('TransformLoop', 'ActorLoop'):
                        for c in self._ports_in(src, FETCH):
                            r = self.resolve(c, depth + 1, seen)
                            if r is not None:
                                return r
                    if st in RACER_OWNERS or st.startswith('Trigger') or 'WalkIn' in st or st == 'Catalyst':
                        return ('player', g.nodes[src].get('AuthoredName', st))
                    stack.append(src)
        return ('player', 'impulse')


def resolve(g, src):
    return Resolver(g).resolve(src)
