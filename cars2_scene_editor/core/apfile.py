"""World actor-property files (<world>.ap).

Plain INI text: one [Section] per placed actor (named after its scene node),
with ordered key=value lines.  Components are switched on with
`AddComponent_<Name>=True` and bound to Meridian by UUID, e.g.

    AddComponent_WalkInTrigger=True
    WalkInTrigger_Uuid=AB13B8D17FD27E4EB5C8A190B1724549   (a NodeGroup in the .mer)
    AddComponent_MExploder=True                            (breakable / bashable)
    MExploder_Uuid=...
    exp/piece_name=CACTUS_BASHABLE_

Keys can repeat inside a section, so sections are kept as ordered lists.

Data shape used everywhere in the add-on:

    [(section_name, [(key, value), ...]), ...]

`value` is None for a line without '=' (kept so the file round-trips
byte-for-byte). The text is latin-1 because the game files are 8-bit ANSI.
"""


def parse(text):
    """-> [(section_name, [(key, value), ...])]; lines before the first
    section (if any) are kept under the name None."""
    sections = []
    cur = None          # the (name, key/value list) currently being filled
    for raw in text.split('\n'):
        line = raw.rstrip('\r')          # files use CRLF or LF
        if line.startswith('[') and line.endswith(']'):
            # a new section header: [Name]
            cur = (line[1:-1], [])
            sections.append(cur)
            continue
        if not line.strip():
            continue                     # blank lines carry no data (dumps() re-adds them)
        if cur is None:
            # key=value lines before any [Section]
            cur = (None, [])
            sections.append(cur)
        if '=' in line:
            k, v = line.split('=', 1)    # only the first '=' separates; values may contain '='
        else:
            k, v = line, None
        cur[1].append((k, v))
    return sections


def load(path):
    """Read and parse an .ap file."""
    with open(path, 'rb') as f:
        return parse(f.read().decode('latin1'))


def dumps(sections):
    """Sections -> .ap text (the inverse of parse)."""
    # every section (the last one too) is followed by a blank line
    out = []
    for name, kvs in sections:
        if name is not None:
            out.append('[%s]' % name)
        for k, v in kvs:
            out.append(k if v is None else '%s=%s' % (k, v))
        out.append('')
    return '\n'.join(out) + '\n'


def save(sections, path):
    """Write sections to an .ap file."""
    with open(path, 'wb') as f:
        f.write(dumps(sections).encode('latin1'))


def get(kvs, key, default=None):
    """First value of `key` in a section's key/value list (or `default`)."""
    for k, v in kvs:
        if k == key:
            return v
    return default


def components(kvs):
    """Names of the components switched on in a section: every
    AddComponent_<Name>=True line gives <Name>."""
    return [k[len('AddComponent_'):] for k, v in kvs
            if k.startswith('AddComponent_') and str(v).lower() == 'true']
