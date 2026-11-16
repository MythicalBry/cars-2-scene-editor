"""Header namespace of an Avalanche SubNetwork (Cars 2 / Toy Story 3 era Tupperware).

HeaderStrings        unique name strings
HeaderStringIndices  name of id k+1  ->  HeaderStrings[HeaderStringIndices[k]]
HeaderLayout         flat (parent_id, child_count) pairs in depth-first pre-order.
                     Root is the virtual id 0; each pair gives its parent the next
                     child_count fresh ids (ids start at 1).
Every DataNode / ProcessorNode 'Header' is such an id, i.e. a path like
Anchor/Main_Ctrl/RotateFront/ClipProcessor.

In plain words: a SubNetwork (an animation/scene network inside an .oct)
names its nodes with slash-separated paths, but stores them compactly as a
tree: each name string once, plus a list saying "node P has N children".
These helpers turn that compact form into full paths and back.

Worked example
    paths (id order):  1 'A', 2 'B', 3 'A/x', 4 'A/y'
    layout:            (0, 2)  -> root gets ids 1, 2
                       (1, 2)  -> id 1 ('A') gets ids 3, 4
"""

def decode_paths(strings, indices, layout):
    """-> list of paths, paths[id-1] for header id."""
    n = len(indices)
    parent = [None] * (n + 1)       # parent[id] for ids 1..n (index 0 unused)
    nxt = 1                         # next id to hand out
    for i in range(0, len(layout), 2):
        p, c = layout[i], layout[i + 1]
        for _ in range(c):
            parent[nxt] = p
            nxt += 1
    if nxt != n + 1:
        raise ValueError('layout assigns %d ids, table has %d' % (nxt - 1, n))
    # build each path from its parent's; parents always have smaller ids
    # because ids are handed out depth-first
    paths = [None] * (n + 1)
    for h in range(1, n + 1):
        name = strings[indices[h - 1]]
        p = parent[h]
        paths[h] = name if p == 0 else paths[p] + '/' + name
    return paths[1:]


def encode_paths(paths):
    """Inverse of decode_paths. `paths` in id order (paths[0] is id 1).

    Ids must be consistent with the depth-first allocation rule; use
    allocate() to produce a valid ordering from an arbitrary set of paths.
    Returns (strings, indices, layout)."""
    pid = {p: i + 1 for i, p in enumerate(paths)}      # path -> id
    kids = {0: []}                                       # id -> child ids
    for i, p in enumerate(paths):
        par = pid[p.rsplit('/', 1)[0]] if '/' in p else 0
        kids.setdefault(par, []).append(i + 1)
    layout = []
    expect = [1]        # the next id the depth-first walk must see (list = mutable in closure)
    def visit(node):
        ch = kids.get(node)
        if not ch:
            return
        # the children of a node must be exactly the next block of ids
        if ch != list(range(expect[0], expect[0] + len(ch))):
            raise ValueError('ids of children of %r are not the next fresh ids' % node)
        expect[0] += len(ch)
        layout.extend((node, len(ch)))
        for c in ch:
            visit(c)
    visit(0)
    # name table: each distinct last path component once
    strings, sidx, indices = [], {}, []
    for p in paths:
        name = p.rsplit('/', 1)[-1]
        if name not in sidx:
            sidx[name] = len(strings); strings.append(name)
        indices.append(sidx[name])
    return strings, indices, layout


def allocate(paths):
    """Assign header ids to an arbitrary collection of paths (parents added
    automatically). Sibling order = first-seen order. Returns ordered path list
    (id = index + 1)."""
    children = {'': []}     # parent path -> child paths ('' = root)
    seen = set()
    for p in paths:
        parts = p.split('/')
        # make sure every prefix ('A', 'A/x', 'A/x/y') exists as a node
        for i in range(len(parts)):
            q = '/'.join(parts[:i + 1])
            if q in seen:
                continue
            seen.add(q)
            par = '/'.join(parts[:i])
            children.setdefault(par, []).append(q)
    out = []
    # depth-first: a node's children get consecutive ids, then each child's
    # subtree follows (matches the layout rule in the module docstring)
    def visit(node):
        ch = children.get(node, [])
        out.extend(ch)
        for c in ch:
            visit(c)
    visit('')
    return out
