"""Lossless Python codec for Avalanche Tupperware files (.oct/.bent/.mer/.tfx).

Unlike the JSON route through `offsetting oct`, this keeps every value's exact
type (int vs float, empty typed arrays, binary) and reproduces the game's own
encoder choices, so decode -> encode is byte-identical on shipped Cars 2 files.

Tree model: dict (insertion ordered). Keys are 'Key' or 'Key#Name'.
A key repeated inside one container is stored as 'Key~1', 'Key~2', ...
(same convention as octread.py); the suffix is dropped on encode.
Values:
  dict            container
  str             string            StrList   string array
  int             int32             IntList   int array
  float           float32           FloatList float array
  bytes           binary blob

File layout:
    0x00  8-byte magic (also tells the byte order: PC little, consoles big)
    0x0C  size of the string table, 0x10 size of the node data
    0x14  node count (Cars 3 only; 0 in Cars 2)
    0x3C  string table: every key/name/string value once, zero-terminated
    then  nodes, one after another, each:
            u16 header  bits 0-1 kind (1 container, 2 array, 3 single value)
                        bit 2    has a '#Name' part
                        bits 3-5 data type (1 string, 2 float, 3 int, 4 binary)
                        bits 6-7 byte width of array lengths - 1
                        bits 8-9 byte width of each int - 1
                        bits 10+ nesting level (how deep in the tree)
            u16 key string index (+ u16 name index when bit 2 is set)
            payload
The tree is rebuilt from the nesting levels: a node belongs to the closest
earlier container with a smaller level.
"""
import struct

MAGIC_LE = bytes([0x29, 0x76, 0x01, 0x45, 0xcd, 0xcc, 0x8c, 0x3f])
MAGIC_BE = bytes([0x45, 0x01, 0x76, 0x29, 0x3f, 0x8c, 0xcc, 0xcd])


# Typed lists: a plain Python list doesn't say whether [] was a string,
# int or float array, so these subclasses carry the type through edits.
class StrList(list): pass
class IntList(list): pass
class FloatList(list): pass


class RootDict(dict):
    """Root container; remembers the root node's key ('' for .oct, 'MeridianOct' for .mer)."""
    root_key = ''


def _is(val, name):
    """Typed-list check by class name, so lists made by another loaded copy of
    this module (an add-on reload) are still recognised."""
    return isinstance(val, list) and any(c.__name__ == name for c in type(val).__mro__)


def _width(v, signed):
    """Smallest byte count (1..4) that holds v - the game's encoder always
    picks the smallest, and byte-identical output needs the same choice."""
    for n in (1, 2, 3, 4):
        if signed:
            if -(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1)):
                return n
        elif 0 <= v < (1 << (8 * n)):
            return n
    raise ValueError('value %d does not fit in 4 bytes' % v)


def decode(raw):
    """File bytes -> (tree, endian). endian is '<' (PC) or '>' (console)."""
    if raw[:8] == MAGIC_LE: e = '<'
    elif raw[:8] == MAGIC_BE: e = '>'
    else: raise ValueError('bad magic')
    bo = 'little' if e == '<' else 'big'
    st_size, dt_size = struct.unpack_from(e + 'II', raw, 12)
    p = 0x3C
    strings = [s.decode('latin1') for s in raw[p:p + st_size].split(b'\0')[:-1]]
    p += st_size
    end = p + dt_size

    def rint(p, n, signed):
        """Read an n-byte integer at p -> (value, next position)."""
        return int.from_bytes(raw[p:p + n], bo, signed=signed), p + n
    # first pass: a flat list of (level, key, value)
    nodes = []
    while p < end:
        h = struct.unpack_from(e + 'H', raw, p)[0]; p += 2
        typ = h & 3; hasname = (h >> 2) & 1; dtype = (h >> 3) & 7
        lens = ((h >> 6) & 3) + 1; ints = ((h >> 8) & 3) + 1; level = h >> 10
        key = strings[struct.unpack_from(e + 'H', raw, p)[0]]; p += 2
        if hasname:
            key += '#' + strings[struct.unpack_from(e + 'H', raw, p)[0]]; p += 2
        if typ == 1:
            val = {}                                                        # container
        elif dtype == 1 and typ == 3:
            val = strings[struct.unpack_from(e + 'H', raw, p)[0]]; p += 2   # string
        elif dtype == 1 and typ == 2:
            n, p = rint(p, lens, False)                                     # string array
            val = StrList(strings[i] for i in struct.unpack_from(e + '%dH' % n, raw, p)); p += 2 * n
        elif dtype == 2 and typ == 3:
            val = struct.unpack_from(e + 'f', raw, p)[0]; p += 4            # float
        elif dtype == 2 and typ == 2:
            n, p = rint(p, lens, False)                                     # float array
            val = FloatList(struct.unpack_from(e + '%df' % n, raw, p)); p += 4 * n
        elif dtype == 3 and typ == 3:
            val, p = rint(p, ints, True)                                    # int
        elif dtype == 3 and typ == 2:
            n, p = rint(p, lens, False); val = IntList()                    # int array
            for _ in range(n):
                v, p = rint(p, ints, True); val.append(v)
        elif dtype == 4:
            n, p = rint(p, lens, False)                                     # binary blob
            val = bytes(raw[p:p + n]); p += n
        else:
            raise ValueError('unhandled node typ=%d dtype=%d' % (typ, dtype))
        nodes.append((level, key, val))
    # second pass: nest the nodes by level
    root_level, root_key, _ = nodes[0]
    root = RootDict()
    root.root_key = root_key
    stack = [(root_level, root)]       # open containers, innermost last
    for level, key, val in nodes[1:]:
        while stack[-1][0] >= level:   # close containers at this depth or deeper
            stack.pop()
        parent = stack[-1][1]
        # repeated keys get ~1, ~2 ... so the dict can hold them all
        k, i = key, 1
        while k in parent:
            k = '%s~%d' % (key, i); i += 1
        parent[k] = val
        if isinstance(val, dict):
            stack.append((level, val))
    return root, e


def load(path):
    """Read a Tupperware file -> tree."""
    with open(path, 'rb') as f:
        return decode(f.read())[0]


def _flatten(tree):
    """Tree -> [(level, key, value)] in file order (inverse of decode's nesting).
    The '~n' suffixes for repeated keys are removed here."""
    out = [(0, getattr(tree, 'root_key', ''), {})]
    def walk(d, level):
        for k, v in d.items():
            out.append((level, k.split('~', 1)[0], v))
            if isinstance(v, dict):
                walk(v, level + 1)
    walk(tree, 1)
    return out


def node_count_field(raw):
    """Header u32 at offset 20: total node count in Cars 3 files, 0 in Cars 2."""
    return struct.unpack_from('<I' if raw[:8] == MAGIC_LE else '>I', raw, 20)[0]


def encode(tree, endian='<', cars3=False):
    """cars3=True writes the node count into header offset 20 (Cars 3 / DTW
    files); Cars 2 files leave it 0."""
    e = endian
    nodes = _flatten(tree)
    strings, sidx = [], {}
    def s(x):
        # intern a string: index in the string table (added on first use)
        if x not in sidx:
            sidx[x] = len(strings); strings.append(x)
        return sidx[x]
    s('')  # the game's encoder always interns the empty string first
    body = bytearray()
    bo = 'little' if e == '<' else 'big'
    for level, key, val in nodes:
        if '#' in key:
            k, name = key.split('#', 1); ks = [s(k), s(name)]
        else:
            ks = [s(key)]
        lens = ints = 1
        payload = b''
        # pick the node kind / data type from the Python type
        if isinstance(val, dict):
            typ, dtype = 1, 0
        elif isinstance(val, bool):
            raise TypeError('bool is not an OCT type; use int 0/1 (%s)' % key)
        elif isinstance(val, str):
            typ, dtype = 3, 1; payload = struct.pack(e + 'H', s(val))
        elif _is(val, 'StrList'):
            typ, dtype = 2, 1; lens = _width(len(val), False)
            payload = len(val).to_bytes(lens, bo) + b''.join(struct.pack(e + 'H', s(x)) for x in val)
        elif isinstance(val, float):
            typ, dtype = 3, 2; payload = struct.pack(e + 'f', val)
        elif _is(val, 'FloatList'):
            typ, dtype = 2, 2; lens = _width(len(val), False)
            payload = len(val).to_bytes(lens, bo) + struct.pack(e + '%df' % len(val), *val)
        elif isinstance(val, int):
            typ, dtype = 3, 3; ints = _width(val, True); payload = val.to_bytes(ints, bo, signed=True)
        elif _is(val, 'IntList'):
            # every element uses the width of the widest one
            typ, dtype = 2, 3; lens = _width(len(val), False)
            ints = max([_width(x, True) for x in val] or [1])
            payload = len(val).to_bytes(lens, bo) + b''.join(x.to_bytes(ints, bo, signed=True) for x in val)
        elif isinstance(val, (bytes, bytearray)):
            typ, dtype = 3, 4; lens = _width(len(val), False)
            payload = len(val).to_bytes(lens, bo) + bytes(val)
        elif isinstance(val, list):
            raise TypeError('plain list for %r: use StrList/IntList/FloatList' % key)
        else:
            raise TypeError('unsupported value for %r: %r' % (key, type(val)))
        # node header (bit layout in the module docstring)
        h = typ | ((len(ks) == 2) << 2) | (dtype << 3) | ((lens - 1) << 6) | ((ints - 1) << 8) | (level << 10)
        body += struct.pack(e + 'H', h) + b''.join(struct.pack(e + 'H', x) for x in ks) + payload
    st = b''.join(x.encode('latin1') + b'\0' for x in strings)
    # magic, 0, string table size, node data size, node count (Cars 3), 36 bytes padding
    head = (MAGIC_LE if e == '<' else MAGIC_BE) + struct.pack(e + 'IIII', 0, len(st), len(body),
                                                                 len(nodes) if cars3 else 0) + bytes(36)
    return head + st + bytes(body)


def save(tree, path, endian='<', cars3=False):
    """Encode a tree and write it to a file."""
    with open(path, 'wb') as f:
        f.write(encode(tree, endian, cars3))
