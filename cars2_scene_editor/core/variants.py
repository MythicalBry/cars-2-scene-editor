"""Track variants (A / B / C) and modes (Race / Collect / Takedown) of a
world, from its Meridian graph.

One world file holds every route raced in it (oilrig: OILRIG_A and OILRIG_B,
see xml dlc_trackinfo1.xml). When a race starts, the game broadcasts the
messages TrackA / TrackB / TrackC, then the mode's (TrackARace; CollectMode and
TrackACollect; TakedownMode and TrackATakedown), and the world's graph shows and
hides the objects that belong to that route and mode: bridges up or down,
detour walls, barriers, sky domes ...

This module replays that logic without the game: Catalyst nodes fire first (the
state when the world loads), then the route's and the mode's messages. Impulses run depth
first in link order, like Meridian's synchronous FireImpulse. A toggle acts on
the objects fetched into the handler it was fired at (ActorFetch /
InstanceFetch links: sdaemon FUN_00d1dcb0 fires once per fetched element) or,
without such links, on the objects carried down the chain (a CollisionToggle
passes its objects to the VisibilityToggle it fires).

Not modelled: timers, conditions on game state (player counts, comparisons
pass every branch), choreographies.
"""
import re

import meridian

FETCH_LINKS = ('ActorFetch', 'InstanceFetch')
# fields that hold numbers, not node indices
_NOT_REFS = ('IsRootObject', 'NumInLinks', 'NumOutLinks', 'GraphVersion', 'SceneNodeIndex', 'Parent',
             'Index', 'IsOpen', 'IsOn', 'Serialize', 'loop')
_STEP_LIMIT = 200000


class _Sim:
    def __init__(self, g):
        self.g = g
        n = len(g.nodes)
        # port (Handler / Event ...) -> the node it belongs to
        self.owner = {}
        for i, node in enumerate(g.nodes):
            for k, v in node.items():
                if k not in _NOT_REFS and isinstance(v, int) and 0 <= v < n and v != i:
                    self.owner.setdefault(v, i)
        self.handlers = {}          # message name -> MessageHandler nodes
        self.inputs = {}            # G2G channel name -> input channel nodes
        # spawn groups -> the placeholders of the agents they spawn (a scene object
        # bound to a SpawnAgentNode / SpawnActorNode only exists once spawned)
        self.group_agents = {}
        self.spawnable = set()
        for i, node in enumerate(g.nodes):
            t = node.get('Type')
            if t == 'MessageHandler':
                self.handlers.setdefault(node.get('MessageName', ''), []).append(i)
            elif t == 'G2G_Input_Impulse_Channel':
                self.inputs.setdefault(node.get('MessageName', ''), []).append(i)
            elif t in ('SpawnAgentNode', 'SpawnActorNode') and node.get('BoundObjectName'):
                self.spawnable.add(node['BoundObjectName'])
                if isinstance(node.get('Group'), int):
                    self.group_agents.setdefault(node['Group'], []).append(i)
        self.reset()

    def reset(self):
        self.visible = {}           # object name -> shown
        self.gates = {}             # Gate / Switch node -> open
        self.steps = 0
        self.active = set()         # nodes on the current chain (loops stop here)
        self.count = {}             # node -> times it ran in this run
        self.game_type = None       # mission worlds: 'ArenaCombatGame' / 'CTFGame' / 'HunterGame'
        self.spawned = set()        # placeholders spawned in this run

    # -- helpers ---------------------------------------------------------------
    def _name(self, src):
        """'<object> / Handle' source -> the object's (scene node) name."""
        o = self.owner.get(src, src)
        node = self.g.nodes[o]
        return node.get('BoundObjectName') or node.get('AuthoredName', '').split(' / ')[0]

    def _fetched(self, port):
        return [self._name(self.g.links[li]['Origin']) for li in self.g.in_links.get(port, [])
                if self.g.links[li].get('Type') in FETCH_LINKS]

    def _fire(self, src, objs):
        """Every impulse link out of `src`, in order."""
        for li in self.g.out_links.get(src, []):
            l = self.g.links[li]
            if l.get('Type') == 'Impulse':
                self._arrive(l['Target'], objs)

    def _fire_node(self, i, objs, only=None):
        """A node's outputs: its own links, then its events' (EventSurge: Begin, End)."""
        node = self.g.nodes[i]
        if only is None:
            self._fire(i, objs)
        for k, v in node.items():
            if k in _NOT_REFS or not isinstance(v, int) or not 0 <= v < len(self.g.nodes) or v == i:
                continue
            if only is not None and k != only:
                continue
            t = self.g.nodes[v].get('Type')
            if t == 'Event':
                self._fire(v, objs)
            elif t == 'EventSurge':
                for e in (self.g.nodes[v].get('Begin'), self.g.nodes[v].get('End')):
                    if e is not None:
                        self._fire(e, objs)

    # -- one impulse arriving at node / port `t` -------------------------------------
    def _arrive(self, t, objs):
        self.steps += 1
        if self.steps > _STEP_LIMIT:
            return
        g = self.g
        fetched = self._fetched(t)
        if fetched:
            objs = fetched
        o = self.owner.get(t, t)
        node = g.nodes[o]
        ty = node.get('Type')
        # repeating timers / ping-pong loops: never re-enter a node on the chain,
        # and let no node run more than a few times per run
        n = self.count.get(o, 0)
        if o in self.active or n >= 8:
            return
        self.count[o] = n + 1
        self.active.add(o)
        try:
            self._run(t, o, node, ty, objs)
        finally:
            self.active.discard(o)

    def _run(self, t, o, node, ty, objs):
        g = self.g
        leaf = g.nodes[t].get('AuthoredName', '').split(' / ')[-1] if t != o else ''
        if ty in ('VisibilityToggle', 'CollisionToggle'):
            if ty == 'VisibilityToggle':
                for name in objs or ():
                    if leaf == 'Turn On':
                        self.visible[name] = True
                    elif leaf == 'Turn Off':
                        self.visible[name] = False
                    elif leaf == 'Toggle':
                        self.visible[name] = not self.visible.get(name, True)
            self._fire_node(o, objs)
        elif ty in ('Gate', 'Switch'):
            on_key = 'IsOpen' if ty == 'Gate' else 'IsOn'
            state = self.gates.get(o, bool(node.get(on_key, 0)))
            if t == o:                                      # the impulse input
                if state:
                    self._fire_node(o, objs, only='Out')
                elif ty == 'Gate':
                    self._fire_node(o, objs, only='Blocked')
            elif leaf in ('Open', 'Turn On'):
                self.gates[o] = True
            elif leaf in ('Close', 'Turn Off'):
                self.gates[o] = False
            elif leaf == 'Toggle':
                self.gates[o] = not state
        elif ty in ('SpawnGroupNode', 'SpawnAgentNode', 'SpawnActorNode'):
            agents = self.group_agents.get(o, []) if ty == 'SpawnGroupNode' else [o]
            names = {g.nodes[a].get('BoundObjectName') for a in agents} - {None}
            if leaf in ('Kill All', 'Kill Spawn'):
                self.spawned -= names
            elif t == o or leaf == 'Spawn':
                self.spawned |= names
            self._fire_node(o, objs)
        elif ty == 'MissionGameType':
            # the mission's mode picks one branch (all of them when none is set)
            for out in ('ArenaCombatGame', 'CTFGame', 'HunterGame'):
                if self.game_type in (None, out):
                    self._fire_node(o, objs, only=out)
        elif ty == 'ConsoleType':
            # Arcade runs the DX (PC) build: its flag picks the main out, else FalseOut
            if node.get('DX'):
                self._fire(o, objs)
            else:
                self._fire_node(o, objs, only='FalseOut')
        elif ty == 'MessageSender':
            self.send(node.get('MessageName', ''), objs)
            self._fire_node(o, objs)
        elif ty == 'G2G_Output_Impulse_Channel':
            for i in self.inputs.get(node.get('MessageName', ''), ()):
                self._fire_node(i, objs)
        elif ty in ('Choreography', 'Delay') and t != o and leaf in ('Stop', 'Restart'):
            pass
        else:
            self._fire_node(o, objs)

    def send(self, message, objs=None):
        for h in self.handlers.get(message, ()):
            if h not in self.active:
                self.active.add(h)
                try:
                    self._fire_node(h, objs)
                finally:
                    self.active.discard(h)

    def start(self):
        for i in self.g.of_type('Catalyst'):
            self._fire_node(i, None)


MODES = (
    # (mode, messages sent after Track<letter>: '%s' is the letter)
    ('Race', ('Track%sRace',)),
    ('Collect', ('CollectMode', 'Track%sCollect')),
    ('Takedown', ('TakedownMode', 'Track%sTakedown')),
)


# mission worlds (mi_*): MissionGameType branches and the markup lists each mode reads
MISSION_MODES = (
    ('Arena', 'ArenaCombatGame', r'Arena'),
    ('Bomb', 'CTFGame', r'Bomb|HiddenBomb'),
    ('Hunter', 'HunterGame', r'Hunter'),
)
MISSION = 'M'                       # the one "route" of a mission world


def variant_names(g):
    """The routes the graph handles: ['A', 'B']. A world's own routes have a
    plain TrackA / TrackB handler; shared templates also answer the other
    letters' mode messages (radiator_b has TrackARace), so those only count
    when no plain one exists."""
    plain, any_ = set(), set()
    for n in g.nodes:
        if n.get('Type') == 'MessageHandler':
            m = re.fullmatch(r'Track([A-Z])(Race|Collect|Takedown)?', n.get('MessageName', ''))
            if m:
                any_.add(m.group(1))
                if not m.group(2):
                    plain.add(m.group(1))
    return sorted(plain or any_)


def _mode_content(g, sim):
    """({'Collect' | 'Takedown': object names}, {object name: route letters})

    Every race world references a Collect and an Attack (Takedown) graph
    (OilRig_Collect, Radiator_B_ATTACK ...); the objects bound in them (gates,
    weapon pickups, attack spawns, results cameras) only matter in that mode.
    An object in both stays in both. Markup lists named CollectTrackA,
    TakedownTrackB, ResetTrackA ... tie their locators to one route."""
    mode_objs = {'Collect': set(), 'Takedown': set()}
    for name, a, b in g.blocks(g.nodes):
        low = name.lower()
        mode = 'Collect' if 'collect' in low else 'Takedown' if ('attack' in low or 'takedown' in low) else None
        if mode is None:
            continue
        for i in range(a, b):
            o = g.nodes[i].get('BoundObjectName')
            if o:
                mode_objs[mode].add(o)
    both = mode_objs['Collect'] & mode_objs['Takedown']
    for k in mode_objs:
        mode_objs[k] -= both
    letter_objs = {}
    for ml in g.markup_lists():
        m = re.fullmatch(r'(?:Collect|Takedown|Reset|Race)Track([A-Z])', ml['name'])
        if m:
            for src in ml['sources']:
                letter_objs.setdefault(sim._name(src), set()).add(m.group(1))
    return mode_objs, letter_objs


def _mission(g):
    """{MISSION: {mode: {object name: shown}}} of a mission world: the game
    type (Arena / Bomb = CTF / Hunter) picks the MissionGameType branches the
    load-time logic runs, and the restart / spawn lists named for a mode
    (ArenaRedRestartLocations, BombSpawnLocators, HunterRestartLocations ...)
    hold that mode's locators."""
    sim = _Sim(g)
    listed = {}
    for ml in g.markup_lists():
        for mode, _gt, pat in MISSION_MODES:
            if re.match(pat, ml['name']):
                for src in ml['sources']:
                    listed.setdefault(sim._name(src), set()).add(mode)
    runs = {}
    for mode, game_type, _pat in MISSION_MODES:
        sim.reset()
        sim.game_type = game_type
        sim.start()
        vis = dict(sim.visible)
        for name in sim.spawnable:
            vis.setdefault(name, name in sim.spawned)
        for name, modes in listed.items():
            if mode not in modes:
                vis[name] = False
            else:
                vis.setdefault(name, True)
        runs[(MISSION, mode)] = vis
    return runs


def _differing(runs):
    """{route: {mode: {object: shown}}}, keeping the objects some run hides:
    whatever differs between routes and modes, and also what every one of them
    hides when the world loads (a spy base's damaged states D1-D4, unspawned
    placeholders), which would otherwise pile up in every preview."""
    keys = set().union(*[set(d) for d in runs.values()])
    differ = {k for k in keys if any(d.get(k, True) is False for d in runs.values())}
    out = {}
    for (v, mode), d in runs.items():
        out.setdefault(v, {})[mode] = {k: s for k, s in d.items() if k in differ}
    return out


def resolve(mer_tree):
    """{route letter: {mode: {object name: shown}}} for every route and mode
    (Race / Collect / Takedown) of the world, listing only objects whose
    visibility differs between them. Mission worlds have one route, MISSION,
    with the modes Arena / Bomb / Hunter. Empty when the world has neither."""
    g = meridian.Graph(mer_tree)
    names = variant_names(g)
    if not names:
        return _differing(_mission(g)) if g.of_type('MissionGameType') else {}
    sim = _Sim(g)
    mode_objs, letter_objs = _mode_content(g, sim)
    runs = {}
    for v in names:
        for mode, msgs in MODES:
            sim.reset()
            sim.start()
            sim.send('Track' + v)
            for msg in msgs:
                sim.send(msg % v if '%s' in msg else msg)
            vis = dict(sim.visible)
            for name in sim.spawnable:
                vis.setdefault(name, name in sim.spawned)
            # each mode's own content: the Collect / Attack sub-graphs' objects
            for m, objs in mode_objs.items():
                for name in objs:
                    if m != mode:
                        vis[name] = False
                    else:
                        vis.setdefault(name, True)
            # locators listed for one route only (CollectTrackB, TakedownTrackA ...)
            for name, letters in letter_objs.items():
                if v not in letters:
                    vis[name] = False
            runs[(v, mode)] = vis
    return _differing(runs)
