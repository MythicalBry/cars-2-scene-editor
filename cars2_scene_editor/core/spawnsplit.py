"""Per-track spawn points: give one track its own copy of a shared spawn point.

In a world that holds several tracks (Italy A/B/C, air, oilrig), the same
locator can feed a spawn loop that runs on every track: a TransformLoop over
a list the logic fills from the locators (directly into the loop's List port,
or through a SetTransformList), fired by a load-time Catalyst or by each
track's own handlers (TrackARace, TrackBRace ...). Moving that locator moves
the object on all of them.

Splitting re-points the list entry at a TransformVariable and sets the
variable per track before the loop runs:

    Track<R>... handler --Impulse--> SetTransform(locator for R -> variable) --> (what the handler fired)

* a handler of one track (MessageName Track<R>, Track<R>Race / Collect /
  Takedown) gets a SetTransform for its track spliced in after it;
* a track-independent start (a Catalyst at level load) can't know the track
  yet: its link into the chain moves to a new Track<R> handler per track,
  each with its SetTransform. Those objects then spawn when the game sends
  the track message instead of at load.

Tracks without their own copy keep the original locator. Everything is
matched by UUID: inserting nodes shifts indices.
"""
import meridian

_NOT_PORT = ('NumInLinks', 'NumOutLinks', 'Index', 'Flags', 'Group', 'IsProxy', 'IsRootObject', 'Parent',
             'Serialize', 'SceneNodeIndex')
MODES = ('', 'Race', 'Collect', 'Takedown')


def _uuid(g, i):
    return bytes(g.nodes[i]['Uuid'])


def _idx(g, u):
    return g.uuid_index.get(bytes(u))


def _owners(g):
    """port node -> owning node (fields of a node that point at other nodes)."""
    own = {}
    for i, n in enumerate(g.nodes):
        for k, v in n.items():
            if k in _NOT_PORT or isinstance(v, bool) or not isinstance(v, int):
                continue
            if 0 <= v < len(g.nodes) and v != i:
                own.setdefault(v, i)
        if isinstance(n.get('Parent'), int):
            own.setdefault(i, n['Parent'])
    return own


def track_of(message, routes):
    """The track a handler's message belongs to ('TrackARace' -> 'A'), or None."""
    for r in routes:
        for m in MODES:
            if message == 'Track%s%s' % (r, m):
                return r
    return None


def consumers(g, ts):
    """[(link type, target port)] the locator's TransformSource feeds."""
    return [(g.links[li].get('Type'), g.links[li]['Target']) for li in g.out_links.get(ts, [])
            if g.links[li].get('Type') in ('TransformFetch', 'TransformListFetch')]


def _ancestors(g, own, start):
    """Nodes (and ports) whose impulses reach `start`: an impulse into a
    node's port (its Handler) counts as into the node."""
    ports = {}
    for p, o in own.items():
        ports.setdefault(o, []).append(p)
    seen = set()
    todo = [start]
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        seen.add(i)
        for t in [i] + ports.get(i, []):
            for li in g.in_links.get(t, []):
                l = g.links[li]
                if l.get('Type') != 'Impulse':
                    continue
                o = l['Origin']
                todo.append(own.get(o, o))
    return seen


def _root_links(g, own, consumer):
    """[(root node, link index)]: the impulse links leaving a start of the
    logic (MessageHandler / Catalyst) towards `consumer`."""
    anc = _ancestors(g, own, consumer)
    out = []
    for i in anc:
        if g.nodes[i].get('Type') not in ('MessageHandler', 'Catalyst'):
            continue
        for li in g.out_links.get(i, []):
            l = g.links[li]
            if l.get('Type') == 'Impulse' and (l['Target'] in anc or own.get(l['Target']) in anc):
                out.append((i, li))
    return out


def split(g, locator_ts, values, routes, report=None, name='Spawn'):
    """Give each track its own value for a shared spawn point.

    locator_ts: UUID of the shared locator's TransformSource.
    values: {track letter: UUID of the TransformSource to use on that track}
    (tracks missing here keep the original). routes: every track of the world.
    Returns the number of chains changed."""
    report = report if report is not None else []
    ts = _idx(g, locator_ts)
    if ts is None:
        report.append('%s: spawn point not in the graph' % name)
        return 0
    feeds = consumers(g, ts)
    if not feeds:
        report.append('%s: the spawn point feeds no list' % name)
        return 0
    own = _owners(g)
    cons_u = [(lt, _uuid(g, p), _uuid(g, own.get(p, p))) for lt, p in feeds]
    var = g.add_transform_variable('%s per track' % name)
    var_u = _uuid(g, var)
    # the list entry reads the variable instead of the locator
    for lt, pu, _cu in cons_u:
        ts, p = _idx(g, locator_ts), _idx(g, pu)
        li = next(li for li in g.out_links.get(ts, []) if g.links[li]['Target'] == p and g.links[li].get('Type') == lt)
        # re-point the link in place: the list keeps its entry order
        v = _idx(g, var_u)
        g.links[li]['Origin'] = v
        g._bump(ts, 'NumOutLinks', -1)
        g._bump(v, 'NumOutLinks', +1)
        g._reindex()

    def source(r):
        return values.get(r, locator_ts)

    changed = 0
    for _lt, _pu, cu in cons_u:
        own = _owners(g)
        roots = [(_uuid(g, r), _uuid(g, g.links[li]['Target']), g.nodes[r].get('MessageName'),
                  g.nodes[r].get('Type')) for r, li in _root_links(g, own, _idx(g, cu))]
        if not roots:
            report.append('%s: nothing starts the spawn loop' % name)
        for ru, yu, msg, ty in roots:
            r_track = track_of(msg or '', routes) if ty == 'MessageHandler' else None
            root, y = _idx(g, ru), _idx(g, yu)
            li = next((li for li in g.out_links.get(root, []) if g.links[li]['Target'] == y
                       and g.links[li].get('Type') == 'Impulse'), None)
            if li is None:
                continue
            g.remove_link(li)
            if r_track is not None:
                st = g.add_set_transform(_idx(g, source(r_track)), _idx(g, var_u), '%s %s' % (name, r_track))
                g.add_link('Impulse', _idx(g, ru), st)
                g.add_link('Impulse', st, _idx(g, yu))
            else:
                # started at load: wait for the track message to know which copy
                for r in routes:
                    h = g.add_message_handler('Track' + r, '%s Track%s' % (name, r))
                    st = g.add_set_transform(_idx(g, source(r)), _idx(g, var_u), '%s %s' % (name, r))
                    g.add_link('Impulse', h, st)
                    g.add_link('Impulse', st, _idx(g, yu))
                report.append('%s: spawns when the track starts (Track%s) instead of at load'
                              % (name, '/'.join(routes)))
            changed += 1
    return changed
