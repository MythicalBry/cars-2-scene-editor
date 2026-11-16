"""Collect mode's collect line and gates, as the game reads them (no Blender).

A Collect race (survival) follows one long open path per track in the world's
.dbl, `<ns>:<world>_collect_CollectLineTrackA` (B, C): all three laps one after
the other, each lap starting where the line passes that track's lap gate (the
CollectGate with LapStartGate set). The game (sdaemon.exe, CollectGameInf
Update 0x506300 / gate pass 0x50c3a0) works it like this:

* it walks the line in 10 m steps; a gate is crossed between two samples on
  opposite sides of the gate's vertical plane (through the middle of its two
  posts, along them) when the nearer sample is within 0.6 x the post spacing
  of the gate's middle (height counted double);
* crossing the lap gate sets the lap the following crossings belong to, from
  how far along the line it is: before 1/6 lap 1, before 1/2 lap 2, else lap 3;
* every other crossing of a gate (not an alternate-route one) gets two
  Objects\\Survival_GateTarget rings for that lap, 0.1 m either side of the
  crossing along the line, 3.5 m above the ground, turned to the gate;
* a gate's order in its lap is its lap progress on the race spline
  TrackA/B/C (the game's lap-progress lookup, 0x004f5420), not its name: "A 1:4" is
  only a label.

Points are game space (Y up). Everything here works on plain lists / numpy.
"""
import math

import numpy as np

STEP = 10.0                 # the game samples the line every 10 m
RADIUS = 0.6                # crossing radius: 0.6 x post spacing (0.5 * 1.2)
TARGET_HEIGHT = 3.5         # rings above the ground
TARGET_OFFSET = 0.1         # each ring this far along the line from the crossing
TARGET_SCALE = (2.0, 2.0, 1.0)
LEVEL = 15.0                # a lap gate crossing on another level (a bridge above) doesn't count
LAPS = 3


def _arr(points):
    return np.asarray(points, np.float64).reshape(-1, 3)


def gate_frame(p1, p2):
    """(middle, unit direction post 1 -> post 2 ... as the game: p1 - p2, flat),
    span, plane normal) of a gate."""
    p1, p2 = np.asarray(p1, float), np.asarray(p2, float)
    mid = (p1 + p2) * 0.5
    d = p1 - p2
    flat = np.array([d[0], 0.0, d[2]])
    span = float(np.linalg.norm(d))
    n = np.array([-d[2], 0.0, d[0]])             # cross(d, up), as the game builds the plane
    ln = np.linalg.norm(n)
    n = n / ln if ln > 1e-9 else np.array([0.0, 0.0, 1.0])
    fl = np.linalg.norm(flat)
    return mid, (flat / fl if fl > 1e-9 else np.array([1.0, 0.0, 0.0])), span, n


def lap_bounds(points, lap_gates, laps=LAPS):
    """Indices of `points` (the line's control points) where laps 2, 3 ... begin:
    the segments that pass between the posts of one of the track's lap gates.
    Crossings in the first and last 2% of the line are the start and the
    finish, not lap changes. -> sorted list (at most laps - 1 entries)."""
    P = _arr(points)
    n = len(P)
    if n < 4 or not lap_gates:
        return []
    hits = []
    for p1, p2 in lap_gates:
        a = np.array([p1[0], p1[2]], float)
        b = np.array([p2[0], p2[2]], float)
        gy = (p1[1] + p2[1]) * 0.5
        s = b - a
        p = P[:-1][:, [0, 2]]
        r = P[1:][:, [0, 2]] - p
        den = r[:, 0] * s[1] - r[:, 1] * s[0]
        ok = np.abs(den) > 1e-9
        den = np.where(ok, den, 1.0)
        w = a - p
        t = (w[:, 0] * s[1] - w[:, 1] * s[0]) / den
        u = (w[:, 0] * r[:, 1] - w[:, 1] * r[:, 0]) / den
        sel = ok & (t >= 0) & (t < 1) & (u >= -0.25) & (u <= 1.25) & (np.abs(P[:-1, 1] - gy) < LEVEL)
        hits += [int(j) + 1 for j in np.nonzero(sel)[0]]
    lo, hi = int(n * 0.02), n - 1 - int(n * 0.02)
    out = []
    for j in sorted(set(hits)):
        if lo < j < hi and all(abs(j - k) > n * 0.1 for k in out):
            out.append(j)
    if len(out) < laps - 1:
        # a custom line that passes beside its lap gate rather than between the posts: its
        # closest approaches to the gate's middle (within a gate width or 25 m) instead
        near = []
        for p1, p2 in lap_gates:
            mid = (np.asarray(p1, float) + np.asarray(p2, float)) / 2.0
            reach = max(25.0, float(np.linalg.norm(np.subtract(p1, p2))))
            d = np.linalg.norm(P - mid, axis=1)
            near += [(float(d[j]), j) for j in range(lo + 1, hi)
                     if d[j] <= d[j - 1] and d[j] <= d[j + 1] and d[j] < reach]
        for _d, j in sorted(near):
            if len(out) >= laps - 1:
                break
            if all(abs(j - k) > n * 0.1 for k in out):
                out.append(j)
        out.sort()
    return out[:laps - 1]


def split(points, bounds):
    """The line cut at `bounds` into laps; neighbours share the cut point."""
    P = [tuple(p) for p in points]
    cuts = [0] + list(bounds) + [len(P) - 1]
    return [P[cuts[k]:cuts[k + 1] + 1] for k in range(len(cuts) - 1)]


def join(pieces, tol=1e-3):
    """Lap pieces back into one line (a shared cut point once)."""
    out = []
    for piece in pieces:
        piece = [tuple(p) for p in piece]
        if out and piece and max(abs(a - b) for a, b in zip(out[-1], piece[0])) <= tol:
            piece = piece[1:]
        out += piece
    return out


def resample(points, step=STEP):
    """Points every `step` metres along the polyline (the game's walk)."""
    P = _arr(points)
    if len(P) < 2:
        return P, 0.0
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    t = np.arange(0.0, total, step)
    if len(t) == 0 or t[-1] < total:
        t = np.append(t, total)
    out = np.empty((len(t), 3))
    for k in range(3):
        out[:, k] = np.interp(t, s, P[:, k])
    return out, total


def crossings(points, gates):
    """The game's gate pass over a collect line. gates: [{'p1', 'p2', 'start': bool,
    'alternate': bool}] (game space). -> [(gate index, lap 0..2, crossing point,
    unit line direction there)] in line order."""
    S, total = resample(points)
    if len(S) < 2 or total <= 0:
        return []
    frames = [gate_frame(g['p1'], g['p2']) for g in gates]
    out = []
    lap = 0
    dist = 0.0
    for k in range(1, len(S)):
        a, b = S[k - 1], S[k]
        dist += float(np.linalg.norm(b - a))
        for gi, (g, (mid, _d, span, n)) in enumerate(zip(gates, frames)):
            if g.get('alternate'):
                continue
            da, db = a - mid, b - mid
            da[1] *= 2.0
            db[1] *= 2.0
            near = min(float(da @ da), float(db @ db))
            if near >= (span * RADIUS) ** 2:
                continue
            sa, sb = float(n @ (a - mid)), float(n @ (b - mid))
            side_a = 0 if abs(sa) <= 1e-5 else (1 if sa > 0 else -1)
            side_b = 0 if abs(sb) <= 1e-5 else (1 if sb > 0 else -1)
            if side_a == side_b:
                continue
            if g.get('start'):
                f = dist / total
                lap = 0 if f < 0.166 else (1 if f < 0.5 else 2)
                continue
            den = sa - sb
            t = sa / den if abs(den) > 1e-12 else 0.0
            x = a + (b - a) * t
            dvec = b - a
            ln = np.linalg.norm(dvec)
            out.append((gi, lap, x, dvec / ln if ln > 1e-9 else np.array([1.0, 0.0, 0.0])))
    return out


def target_heading(p1, p2):
    """Yaw (radians about Y) the game turns a gate's rings by: atan2 of the gate's
    post-to-post direction (x over z). The quaternion it builds takes -1/2 of it."""
    d = np.asarray(p1, float) - np.asarray(p2, float)
    return math.atan2(float(d[0]), float(d[2]))


def lap_progress(race_points, p):
    """0..1 lap progress of point p on the closed race spline (TrackA/B/C):
    where a gate sorts within its lap."""
    P = _arr(race_points)
    if len(P) < 2:
        return 0.0
    if np.linalg.norm(P[0] - P[-1]) > 1e-4:
        P = np.vstack([P, P[:1]])
    q = np.asarray(p, float)
    a, b = P[:-1], P[1:]
    d = b - a
    L2 = np.einsum('ij,ij->i', d, d)
    t = np.clip(np.einsum('ij,ij->i', q - a, d) / np.where(L2 > 1e-12, L2, 1.0), 0.0, 1.0)
    proj = a + d * t[:, None]
    dist = np.linalg.norm(proj - q, axis=1)
    k = int(np.argmin(dist))
    seg = np.sqrt(L2)
    acc = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(acc[-1])
    return float((acc[k] + seg[k] * t[k]) / total) if total > 0 else 0.0
