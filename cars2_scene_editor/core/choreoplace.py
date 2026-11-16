"""Where (and how often) the game plays each choreography: replay a world's
Meridian logic for one route and mode, carrying transforms the way the game
does, and record the anchor transform every time a Choreography node fires.

Static tracing (anchors.py) is not enough. Many anchors read a variable that
the logic fills just before playing: the lap gates and banners of the
VictoryCam graph take the transform an impulse brings in from the world graph
through a G2G channel ('LapGate' Gates / Start / Lap / Finish), which the world
fires once per gate locator (a TransformLoop over StartGateA1Scale ...) or with
the start / finish locator, separately for each route.

Rules, as the engine runs them (see variants.py for the impulse walking):

* An impulse may carry a transform. Sources of one: a TransformLoop's Each
  output (the current list entry), a G2G output channel's TransformFetch (it
  replaces what came in), a racer event (MessageHandler / trigger: the racer).
* G2G_Output_Impulse_Channel -> every G2G_Input_Impulse_Channel with the same
  MessageName and Index.
* ImpulseTransformSplitter: its Transform output is the incoming transform.
* SetTransform: the Read port's value goes into what Write is linked to (a
  TransformVariable, or a TransformList's PushHead / PushTail / Remove).
  SetTransformList copies a list.
* TransformLoop: Each fires once per entry of its List (Value = the entry),
  then After (Empty when there are none).
* Index/RandomTransformFromList: the list's first entry.
* Choreography: an impulse at the node plays it; the anchor's transform is
  evaluated at that moment.

Racers (the player's car) are reported as PLAYER. Values are object names
(the scene node / BoundObjectName).
"""
import heapq

import meridian
import variants

import anchors

PLAYER = '<player>'
# after the route and mode: what the game sends while a race runs
RACE_FLOW = ('EventStart', 'NewQuickStart', 'QuickStartBegin', 'StartCountdown', 'QuickStartLightThree',
             'QuickStartLightTwo', 'QuickStartLightOne', 'QuickStartLightGo', 'QuickStartRaceGo',
             'CollectIntroStart', 'CollectStartRace', 'RacerFinishedLap', 'RacerFinishedLap', 'RacerFinished')
_LIMIT = 300000
_PER_NODE = 256
HORIZON = 75.0                  # seconds of race the timed replay covers (spawn_times)


class PlaceSim:
    def __init__(self, g):
        self.g = g
        self.res = anchors.Resolver(g)
        self.owner = self.res.owner
        n = len(g.nodes)
        self.handlers, self.inputs = {}, {}
        for i, node in enumerate(g.nodes):
            t = node.get('Type')
            if t == 'MessageHandler':
                self.handlers.setdefault(node.get('MessageName', ''), []).append(i)
            elif t == 'G2G_Input_Impulse_Channel':
                self.inputs.setdefault((node.get('MessageName', ''), node.get('Index')), []).append(i)
        self.choreo_anchor = {ci: [src for _r, _a, src in g.choreo_anchors(ci)] for ci in g.of_type('Choreography')}
        # spawn group -> the start catalysts of its (proxy) graph: each spawn runs them
        self.group_catalysts = {}
        for i, node in enumerate(g.nodes):
            if node.get('Type') == 'Catalyst' and isinstance(node.get('Group'), int):
                self.group_catalysts.setdefault(node['Group'], []).append(i)
        # spawn group -> its SpawnAgentNodes / SpawnActorNodes, and each one's Position port
        self.group_agents, self.agent_pos = {}, {}
        for i in g.of_type('SpawnAgentNode') + g.of_type('SpawnActorNode'):
            if isinstance(g.nodes[i].get('Group'), int):
                self.group_agents.setdefault(g.nodes[i]['Group'], []).append(i)
            for j in g.owned(i):
                if g.nodes[j].get('AuthoredName', '').endswith('/ Position'):
                    self.agent_pos[i] = j
        # ResetCollection -> the stateful nodes it resets (Count, Gate ...)
        self.collects = {}
        for a in g.assocs or ():
            if a.get('Type') == 'Collection' and isinstance(a.get('Collector'), int):
                self.collects.setdefault(a['Collector'], []).append(a.get('Owner'))
        self.timed = False
        self.reset()

    def reset(self):
        self.vars, self.lists = {}, {}
        self.split, self.loop = {}, {}
        self.spawn_xf = {}          # spawn group -> where its agent was last spawned
        self.spawn_all = {}         # spawn group -> every place it spawned an agent
        self.agent_spawns = {}      # SpawnAgentNode -> every place it was spawned at
        self.gates = {}
        self.plays = {}             # choreography node -> [anchor value per play]
        self.steps = 0
        self.active, self.count = set(), {}
        self.deferred, self.depth = [], 0
        self.unfinished = {}        # Count -> the transform its last hit carried (not done yet)
        self.game_type = None
        # timed replay (spawn_times): a clock and the events waiting on it
        self.now, self.queue, self.seq = 0.0, [], 0
        self.hits, self.chance, self.gen = {}, {}, {}
        self.spawn_times = {}       # spawn node -> [(place, seconds)]

    # -- data ------------------------------------------------------------------
    def _own(self, i):
        return self.owner.get(i, i)

    def _ins(self, i, types):
        return [self.g.links[li]['Origin'] for li in self.g.in_links.get(i, [])
                if self.g.links[li].get('Type') in types]

    def _port(self, o, field):
        v = self.g.nodes[o].get(field)
        return v if isinstance(v, int) and 0 <= v < len(self.g.nodes) else None

    def list_of(self, src, depth=0):
        """Entries of the list a TransformListFetch reads."""
        if src is None or depth > 8:
            return []
        o = self._own(src)
        if o in self.lists:
            return list(self.lists[o])
        ty = self.g.nodes[o].get('Type', '')
        if ty in ('TransformList', 'TransformListVariable'):
            return []
        for s in self._ins(o, ('TransformListFetch',)) + self._ins(src, ('TransformListFetch',)):
            r = self.list_of(s, depth + 1)
            if r:
                return r
        return []

    def entries(self, port, depth=0):
        """Entries a list port reads: whole lists (TransformListFetch) and single
        transforms linked straight into it (TransformFetch, in link order)."""
        out = []
        if port is None:
            return out
        for li in self.g.in_links.get(port, []):
            l = self.g.links[li]
            if l.get('Type') == 'TransformListFetch':
                out += self.list_of(l['Origin'])
            elif l.get('Type') == 'TransformFetch':
                v = self.value(l['Origin'], depth + 1)
                if v is not None:
                    out.append(v)
        return out

    def value(self, src, depth=0):
        """Current transform (object name / PLAYER / None) of a TransformSource."""
        if src is None or depth > 12:
            return None
        g = self.g
        o = self._own(src)
        on = g.nodes[o]
        ty = on.get('Type', '')
        if on.get('BoundObjectName') and ty in ('LocatorNode', 'InstanceNode', 'SplineNode'):
            return on['BoundObjectName']
        if ty == 'TransformVariable':
            if o in self.vars:
                return self.vars[o]
        elif ty == 'ImpulseTransformSplitter':
            # inside a spawned graph, an event of the spawned agent (Exploded ...)
            # carries the agent, which stands where it was spawned
            v = self.split.get(o)
            if v is None and isinstance(on.get('Group'), int):
                v = self.spawn_xf.get(on['Group'])
            if v is not None:
                return v
        elif ty == 'TransformLoop':
            return self.loop.get(o)
        elif ty in ('IndexTransformFromList', 'RandomTransformFromList'):
            entries = self.entries(self._port(o, 'List'), depth + 1)
            if entries:
                return entries[0]
        elif ty == 'ActorTransform':
            r = self.res.resolve(src)
            return PLAYER if r is None or r[0] == 'player' else r[1]
        # a fetch chain (TransformFetch into a variable / port): follow it
        for s in self._ins(src, ('TransformFetch',)) + (self._ins(o, ('TransformFetch',)) if o != src else []):
            v = self.value(s, depth + 1)
            if v is not None:
                return v
        r = self.res.resolve(src)
        if r is None:
            return None
        return PLAYER if r[0] == 'player' else r[1]

    def fetched(self, port):
        """Value of the TransformFetch feeding `port` (None without one)."""
        for s in self._ins(port, ('TransformFetch',)):
            v = self.value(s)
            if v is not None:
                return v
        return None

    # -- impulses -------------------------------------------------------------
    def _fire(self, src, xf):
        for li in self.g.out_links.get(src, []):
            l = self.g.links[li]
            if l.get('Type') == 'Impulse':
                self._arrive(l['Target'], xf)

    def _fire_node(self, i, xf, only=None):
        node = self.g.nodes[i]
        if only is None:
            self._fire(i, xf)
        for k, v in node.items():
            if k in variants._NOT_REFS or k in ('IsProxy', 'Group') or not isinstance(v, int) or \
                    isinstance(v, bool) or not 0 <= v < len(self.g.nodes) or v == i:
                continue
            if only is not None and k != only:
                continue
            t = self.g.nodes[v].get('Type')
            if t == 'Event':
                self._fire(v, xf)
            elif t == 'EventSurge':
                for e in (self.g.nodes[v].get('Begin'), self.g.nodes[v].get('End')):
                    if e is not None:
                        self._fire(e, xf)

    def _arrive(self, t, xf):
        self.steps += 1
        if self.steps > _LIMIT:
            return
        o = self._own(t)
        n = self.count.get(o, 0)
        if o in self.active and self.g.nodes[o].get('Type') == 'Count':
            # a Count hit again from what it set off (the lap gates' Count
            # spawns a gate, whose start hits the Count again until every gate
            # stands): taken once the current chain has finished
            self.deferred.append((t, xf))
            return
        # each node runs at most _PER_NODE times per replay: enough for a loop
        # over every entry of a long list (weapon spawn lists run past 100),
        # never forever
        if o in self.active or n >= _PER_NODE:
            return
        self.count[o] = n + 1
        self.active.add(o)
        self.depth += 1
        try:
            self._run(t, o, xf)
        finally:
            self.active.discard(o)
            self.depth -= 1
        if self.depth == 0:
            while self.deferred and self.steps <= _LIMIT:
                self._arrive(*self.deferred.pop(0))

    def _run(self, t, o, xf):
        g = self.g
        node = g.nodes[o]
        ty = node.get('Type', '')
        leaf = g.nodes[t].get('AuthoredName', '').split(' / ')[-1] if t != o else ''
        if self.timed and self._run_timed(t, o, xf, ty, leaf):
            return
        if ty == 'Choreography':
            if t == o or leaf == 'Restart':
                self.plays.setdefault(o, []).append([self.value(s) for s in self.choreo_anchor.get(o, [])])
            self._fire_node(o, xf)
        elif ty == 'TransformLoop':
            entries = self.entries(self._port(o, 'List'))
            for e in entries:
                self.loop[o] = e
                self._fire_node(o, e, only='EachOut')
            self._fire_node(o, xf, only='After' if entries else 'Empty')
        elif ty == 'ImpulseTransformSplitter':
            self.split[o] = xf
            self._fire_node(o, xf)
        elif ty in ('SetTransform', 'SetTransformList'):
            rd = self._port(o, 'Fetch')
            if ty == 'SetTransform':
                v = self.fetched(rd) if rd is not None else None
            else:
                v = self.entries(rd)
            wr = self._port(o, 'Write')
            for li in g.out_links.get(wr, []) if wr is not None else []:
                tgt = g.links[li]['Target']
                to = self._own(tgt)
                tleaf = g.nodes[tgt].get('AuthoredName', '').split(' / ')[-1]
                tt = g.nodes[to].get('Type', '')
                if tt == 'TransformVariable':
                    self.vars[to] = v
                elif tt == 'TransformList':
                    lst = self.lists.setdefault(to, [])
                    if ty == 'SetTransformList':
                        lst[:] = list(v)
                    elif tleaf == 'Remove':
                        if v in lst:
                            lst.remove(v)
                    elif v is not None:
                        if tleaf in ('Add to Top', 'Push Head', 'PushHead'):
                            lst.insert(0, v)
                        else:
                            lst.append(v)
            self._fire_node(o, xf)
        elif ty.startswith('G2G_Output') and ty.endswith('Channel'):
            v = self.fetched(o)
            v = xf if v is None else v
            for i in self.inputs.get((node.get('MessageName', ''), node.get('Index')), ()):
                self._fire_node(i, v)
        elif ty == 'SpawnGroupNode':
            if t == o:              # spawn: the new instance's graph starts
                if xf is not None:
                    self.spawn_xf[o] = xf
                    if xf not in self.spawn_all.setdefault(o, []):
                        self.spawn_all[o].append(xf)
                # each agent of the group stands where its Position port points
                # (usually the transform the spawn impulse carries)
                for a in self.group_agents.get(o, ()):
                    v = self.fetched(self.agent_pos[a]) if a in self.agent_pos else None
                    v = xf if v is None else v
                    if v is not None and v not in self.agent_spawns.setdefault(a, []):
                        self.agent_spawns[a].append(v)
                    if v is not None:
                        self.spawn_times.setdefault(a, []).append((v, self.now))
                for c in self.group_catalysts.get(o, ()):
                    self._fire_node(c, xf)
            self._fire_node(o, xf)
        elif ty == 'MessageSender':
            self.send(node.get('MessageName', ''), xf)
            self._fire_node(o, xf)
        elif ty in ('Gate', 'Switch'):
            key = 'IsOpen' if ty == 'Gate' else 'IsOn'
            state = self.gates.get(o, bool(node.get(key, 0)))
            if t == o:
                if state:
                    self._fire_node(o, xf, only='Out')
                elif ty == 'Gate':
                    self._fire_node(o, xf, only='Blocked')
            elif leaf in ('Open', 'Turn On'):
                self.gates[o] = True
            elif leaf in ('Close', 'Turn Off'):
                self.gates[o] = False
            elif leaf == 'Toggle':
                self.gates[o] = not state
            if self.timed and ty == 'Switch' and self.gates.get(o, state) != state:
                self._surge(self._port(o, 'Out'), self.gates[o], xf)
            elif ty == 'Switch' and self.gates.get(o, state) and not state:
                # untimed: a Pulse the switch turns on fires (once)
                for li in self.g.out_links.get(self._port(o, 'Out'), []) if self._port(o, 'Out') is not None else []:
                    l = self.g.links[li]
                    p = self._own(l['Target'])
                    if l.get('Type') == 'Surge' and self.g.nodes[p].get('Type') == 'Pulse':
                        self._fire_node(p, xf)
        elif ty == 'ConsoleType':
            if node.get('DX'):
                self._fire(o, xf)
            else:
                self._fire_node(o, xf, only='FalseOut')
        elif ty == 'MissionGameType':
            for out in ('ArenaCombatGame', 'CTFGame', 'HunterGame'):
                if self.game_type in (None, out):
                    self._fire_node(o, xf, only=out)
        elif ty in ('Choreography', 'Delay') and leaf == 'Stop':
            pass
        elif ty == 'Count':
            self._count(o, xf, leaf)
        else:
            self._fire_node(o, xf)

    def int_value(self, port, depth=0):
        """An int port's value: what an IntFetch brings in (a Count's hits, an
        AB Arithmetic Int's result), else its own stored value."""
        if port is None or depth > 8:
            return 0
        for s in self._ins(port, ('IntFetch',)):
            o = self._own(s)
            on = self.g.nodes[o]
            ty = on.get('Type', '')
            if ty == 'Count' and on.get('CurHits') == s:
                return self.hits.get(o, 0)
            if ty == 'ABArithmeticInt' and on.get('Result') == s:
                a = self.int_value(self._port(o, 'A'), depth + 1)
                b = self.int_value(self._port(o, 'B'), depth + 1)
                return {0: a + b, 1: a - b, 2: a * b, 3: a // b if b else 0}.get(on.get('Operator', 0), a + b)
            return self.int_value(s, depth + 1)
        v = self.g.nodes[port].get('Value', 0)
        return int(v) if isinstance(v, (int, float)) else 0

    def _count(self, o, xf, leaf):
        """Count: Fewer below Hits Needed, Out on it, More past it."""
        if leaf == 'Reset':
            self.hits[o] = 0
            return
        h = self.hits[o] = self.hits.get(o, 0) + 1
        need = self.int_value(self._port(o, 'HitsNeeded')) or 1
        if h < need:
            self.unfinished[o] = xf
            self._fire_node(o, xf, only='Fewer')
        elif h == need:
            self.unfinished.pop(o, None)
            self._fire(o, xf)
        else:
            self._fire_node(o, xf, only='More')

    def finish_counts(self):
        """Counts a short replay hits fewer times than a whole race would
        (mi_air's wild karts wait for several bomb hits): what follows them
        happens once, as if they had got there."""
        for _ in range(4):
            pending, self.unfinished = self.unfinished, {}
            if not pending:
                break
            for o, xf in pending.items():
                self.hits[o] = max(self.hits.get(o, 0), self.int_value(self._port(o, 'HitsNeeded')) or 1)
                self._fire(o, xf)

    def send(self, message, xf=None):
        for h in self.handlers.get(message, ()):
            if h in self.active:
                continue
            self.active.add(h)
            try:
                # the sender is (nearly always) a racer
                self._fire_node(h, PLAYER if xf is None else xf)
            finally:
                self.active.discard(h)

    def start(self):
        for i in self.g.of_type('Catalyst'):
            if not isinstance(self.g.nodes[i].get('Group'), int):   # spawned graphs start when spawned
                self._fire_node(i, None)
        if self.timed:
            for i in self.g.of_type('Switch'):
                if self.g.nodes[i].get('IsOn') and not isinstance(self.g.nodes[i].get('Group'), int):
                    self._surge(self._port(i, 'Out'), True, None)

    # -- the timed replay (spawn_times) -----------------------------------------
    # Delays wait, a Pulse fires every Frequency seconds while the surge
    # driving it (a Switch's Out) is on, a Count counts, a Chance takes its
    # branches in proportion (deterministically). mi_air's baggage trains are
    # a driver, then a kart every 0.5 s until a Count turns the Pulse off.
    def _float(self, port):
        v = self.g.nodes[port].get('Value', 0.0) if port is not None else 0.0
        return float(v) if isinstance(v, (int, float)) else 0.0

    def _at(self, when, ev):
        self.seq += 1
        heapq.heappush(self.queue, (when, self.seq, ev))

    def _surge(self, surge, on, xf):
        """A surge turned on / off: start or stop the Pulses it drives."""
        if surge is None:
            return
        for li in self.g.out_links.get(surge, []):
            l = self.g.links[li]
            p = self._own(l['Target'])
            if l.get('Type') == 'Surge' and self.g.nodes[p].get('Type') == 'Pulse':
                self.gen[p] = self.gen.get(p, 0) + 1
                if on:
                    self._at(self.now + max(self._float(self._port(p, 'Frequency')), 0.05),
                             ('pulse', p, self.gen[p], xf))

    def _run_timed(self, t, o, xf, ty, leaf):
        if ty == 'Delay':
            if leaf in ('Kill All', 'Clear', 'Stop'):
                self.gen[o] = self.gen.get(o, 0) + 1      # forget what is waiting
                return True
            surge = self._port(o, 'Delaying')
            if surge is not None and self.g.nodes[surge].get('Begin') is not None:
                self._fire(self.g.nodes[surge]['Begin'], xf)
            self._at(self.now + max(self._float(self._port(o, 'Duration')), 0.0),
                     ('delay', o, self.gen.get(o, 0), xf))
            return True
        if ty == 'Count':
            self._count(o, xf, leaf)
            return True
        if ty == 'Chance':
            acc = self.chance.get(o, 0.5) + min(max(self._float(self._port(o, 'Percent')), 0.0), 1.0)
            self.chance[o] = acc - 1.0 if acc >= 1.0 else acc
            self._fire_node(o, xf, only='True' if acc >= 1.0 else 'False')
            return True
        if ty == 'ResetCollection':
            for x in self.collects.get(o, ()):
                self.hits.pop(x, None)
                self.gates.pop(x, None)
            self._fire_node(o, xf)
            return True
        return False

    def event_messages(self, group, depth=14):
        """Messages whose handlers lead (through impulses) to spawn group
        `group`: what starts a spawner the race flow never reaches (mi_air's
        conveyor boxes start when a bomb blows a base: BlueBase_D3_EXPLODE_5Bomb)."""
        g = self.g
        seen, out, todo = {group}, [], [(group, 0)]
        while todo:
            i, d = todo.pop()
            for j in [i] + list(g.owned(i)):
                for li in g.in_links.get(j, []):
                    l = g.links[li]
                    if l.get('Type') not in ('Impulse', 'Surge'):
                        continue
                    o = self._own(l['Origin'])
                    n = g.nodes[o]
                    if n.get('Type') == 'MessageHandler':
                        if n.get('MessageName') and n['MessageName'] not in out:
                            out.append(n['MessageName'])
                    elif o not in seen and d < depth:
                        seen.add(o)
                        todo.append((o, d + 1))
        return out

    def run_clock(self, horizon=HORIZON):
        """Run what waits on the clock, up to `horizon` seconds."""
        while self.queue and self.steps <= _LIMIT:
            when, _s, ev = heapq.heappop(self.queue)
            if when > horizon:
                break
            kind, o, gen, xf = ev
            if self.gen.get(o, 0) != gen:
                continue
            self.now = when
            if kind == 'delay':
                surge = self._port(o, 'Delaying')
                if surge is not None and self.g.nodes[surge].get('End') is not None:
                    self._fire(self.g.nodes[surge]['End'], xf)
                if self._port(o, 'Delayed') is not None:
                    self._fire(self.g.nodes[o]['Delayed'], xf)
            elif kind == 'pulse':
                self._fire(o, xf)
                self._at(when + max(self._float(self._port(o, 'Frequency')), 0.05), ev)
            elif kind == 'send':
                self.send(o, xf)


def places(mer_tree):
    """{route letter or variants.MISSION: {mode: {choreography node: [anchor, ...]}}}
    where anchor is the object name its first anchor had each time it played
    (PLAYER for a racer, None when unknown). Routes and modes as in
    variants.resolve; a world without them gets one run under ('', '')."""
    return simulate(mer_tree)[0]


def _runs(g):
    """[(route, mode, messages after the catalysts, mission game type)]."""
    names = variants.variant_names(g)
    runs = []
    if names:
        for v in names:
            for mode, msgs in variants.MODES:
                runs.append((v, mode, ['Track' + v] + [m % v if '%s' in m else m for m in msgs], None))
    elif g.of_type('MissionGameType'):
        # mission content (mi_air's CTF baggage train and conveyor boxes, the
        # wild karts) starts from RestartRace, handled by the game's mission
        # classes (MissionGameInf, RestartRace)
        for mode, game_type, _pat in variants.MISSION_MODES:
            runs.append((variants.MISSION, mode, ['RestartRace'], game_type))
    else:
        runs.append(('', '', [], None))
    return runs


EVENT_AT = 15.0                 # when an event replay sends its message (seconds): after
                                # the mission intro (mi_air keeps its conveyors shut for 9.25 s)


def _replay(sim, msgs, game_type, event=None, horizon=HORIZON):
    """One replay: the catalysts, the route / mode messages, the race flow,
    then (timed) the clock; `event` is a message sent EVENT_AT seconds in."""
    sim.reset()
    sim.game_type = game_type
    sim.start()
    for m in list(msgs) + list(RACE_FLOW):
        sim.send(m)
    if event is not None:
        if sim.timed:
            sim._at(EVENT_AT, ('send', event, 0, None))
        else:
            sim.send(event)
    if sim.timed:
        sim.run_clock(horizon)
    else:
        sim.finish_counts()


_MODE_WORDS = {'Arena': ('arena',), 'Bomb': ('bomb', 'ctf'), 'Hunter': ('hunter',)}


def _event_replays(sim, g, reached, route, mode):
    """[(message, [spawn nodes])]: in a mission world, the spawners the race
    flow didn't reach, with each message that can start them. A message naming
    another mode (the game's 'BlueBase_D0_EXPLODE_3Bomb' is Bomb's) is left
    out. Race worlds get none: a spawner nothing reaches there stays unspawned
    (london_a's cones)."""
    if route != variants.MISSION:
        return []
    others = [w for m, ws in _MODE_WORDS.items() if m != mode for w in ws]
    mine = _MODE_WORDS.get(mode, ())
    by_msg = {}
    for grp, agents in sim.group_agents.items():
        if any(a in reached for a in agents):
            continue
        for m in sim.event_messages(grp):
            low = m.lower()
            if any(w in low for w in others) and not any(w in low for w in mine):
                continue
            by_msg.setdefault(m, []).extend(agents)
    return sorted(by_msg.items())


def spawn_times(mer_tree, horizon=HORIZON):
    """{route: {mode: {spawn node: [(object name, seconds, event), ...]}}}:
    when each agent / actor spawns in the first `horizon` seconds of the race,
    replaying delays, pulses and counts on a clock (routes and modes as
    simulate()). event is '' for the race itself, else the message that starts
    the spawner (sent EVENT_AT seconds in: a base blown up, a flag taken ...)."""
    g = meridian.Graph(mer_tree)
    sim = PlaceSim(g)
    sim.timed = True
    out = {}
    for v, mode, msgs, game_type in _runs(g):
        _replay(sim, msgs, game_type, horizon=horizon)
        res = {a: [(x, round(t, 3), '') for x, t in l if isinstance(x, str) and x != PLAYER]
               for a, l in sim.spawn_times.items()}
        for m, agents in _event_replays(sim, g, {a for a, l in res.items() if l}, v, mode):
            _replay(sim, msgs, game_type, m, horizon)
            for a in agents:
                if not res.get(a):
                    l = [(x, round(t, 3), m) for x, t in sim.spawn_times.get(a, ()) if isinstance(x, str) and x != PLAYER]
                    if l:
                        res[a] = l
        out.setdefault(v, {})[mode] = res
    return out


def simulate(mer_tree):
    """(places(), agents): agents is {route: {mode: {spawn node (agent / actor): [object
    name, ...]}}}, every place the logic spawns each agent at in that run (the
    world's bashables, pickups and breakables: see objplace.py)."""
    g = meridian.Graph(mer_tree)
    sim = PlaceSim(g)
    out, agents = {}, {}
    for v, mode, msgs, game_type in _runs(g):
        _replay(sim, msgs, game_type)
        res = {ci: [a[0] if a else None for a in plays] for ci, plays in sim.plays.items()}
        # played from live race data the replay can't produce (lap counters,
        # proximity ...): the anchor as the logic left it
        for ci, srcs in sim.choreo_anchor.items():
            if ci not in res and srcs:
                grp = g.nodes[ci].get('Group')
                if isinstance(grp, int) and sim.spawn_all.get(grp):
                    res[ci] = list(sim.spawn_all[grp])     # one per spawned agent
                    continue
                at = sim.value(srcs[0])
                if at is not None:
                    res[ci] = [at]
        out.setdefault(v, {})[mode] = res
        spawned = {a: [x for x in l if x is not None] for a, l in sim.agent_spawns.items()}
        # spawners only gameplay events start (see spawn_times): replay each event
        for m, nodes in _event_replays(sim, g, {a for a, l in spawned.items() if l}, v, mode):
            _replay(sim, msgs, game_type, m)
            for a in nodes:
                if not spawned.get(a) and sim.agent_spawns.get(a):
                    spawned[a] = [x for x in sim.agent_spawns[a] if x is not None]
        agents.setdefault(v, {})[mode] = spawned
    return out, agents
