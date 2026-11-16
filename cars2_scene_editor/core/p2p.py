"""Point-to-point lap splines: a real race finish away from the start line.

How the game counts laps (sdaemon.exe, Cars 2 Arcade):

* The lap spline is the world's .dbl B-spline TrackA / TrackB / TrackC (the
  current track's letter; else 'lane1'). Every car's LapTracker projects the
  car onto its control polygon (CurveBSpline, NearestDistance, 0x00dcd4c0): it stays on
  the current segment while the car is inside it (projection 0..1, at most
  20 m away), else tries the next one, else searches every segment for the
  nearest. Its position is the arc length there / total length (0..1).
* A lap counts when that position wraps forward (above 0.95 to below 0.05);
  wrapping back removes one. Laps start at -1, so the first wrap starts lap 1.
  A car finishes when int(laps + position) reaches the lap count (the arcade
  setting Number of Laps per Game): RacerFinished, the placement stinger, the
  car's real finish time.

The wrap is a jump of the projection, not a place: it happens wherever a car
leaves a segment of the last 5% and the nearest segment belongs to the first
5%. So a spline whose last 5% has a short road piece before the grid's front
row AND one before the finish, and whose first 5% has the road after them,
makes a race of one lap start at the grid and end at the finish, with the
game's own finish. The pieces are joined by legs high in the sky that no car
comes near, and a long dummy loop in the sky makes the total long enough
that those legs fit in 5%.

Layout (positions along the spline):

    0       post_B: B -> a little past B             (the finish wrap lands here)
            sky leg to the front row
            post_A: the front row -> the start line -> the route ... (the start wrap lands here)
    > 5%    ... the route -> just before B           (must pass 5%: a car leaving the
                                                       route for pre_B with its position
                                                       still under 5% would count a lap back)
    middle  dummy loop in the sky
    > 0.95  pre_B: the last metres before B, then pre_A: under the grid to the front row

The total length is chosen so both ends fit in their 5% while the route
still crosses 5%: end pieces / 0.045 < length < (start pieces + route) / 0.06.

Distances along the route stay true (arc length / total), so race positions
and AI gaps work. With the lap count set to 1 (the game's track settings),
the race starts and finishes like any other race.
"""
import math

import numpy as np

UP = np.array([0.0, 1.0, 0.0])          # game space: Y up
SKY = 300.0                             # height of the joining legs above the road
DUMMY_HEIGHT = 3000.0                   # the dummy loop's height
WRAP = 0.05                             # LapTracker's wrap window (5% at each end)


def _norm(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else v


def _flat(v):
    v = np.array(v, float)
    v[1] = 0.0
    return _norm(v)


def _length(pts):
    pts = np.asarray(pts, float)
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) if len(pts) > 1 else 0.0


class Route:
    """A closed polygon (the original lap) with arc-length lookups."""

    def __init__(self, pts):
        self.p = np.asarray(pts, float)
        if len(self.p) > 2 and np.allclose(self.p[0], self.p[-1]):
            self.p = self.p[:-1]
        self.n = len(self.p)
        nxt = np.roll(self.p, -1, axis=0)
        self.seg = np.linalg.norm(nxt - self.p, axis=1)
        self.cum = np.concatenate([[0.0], np.cumsum(self.seg)])
        self.L = float(self.cum[-1])

    def project(self, q):
        """Arc length of the nearest point of the polygon to q."""
        q = np.asarray(q, float)
        best = (1e30, 0.0)
        for i in range(self.n):
            a, b = self.p[i], self.p[(i + 1) % self.n]
            ab = b - a
            den = float(ab @ ab)
            if den < 1e-9:
                continue
            t = min(1.0, max(0.0, float((q - a) @ ab) / den))
            d = float(np.linalg.norm(q - (a + t * ab)))
            if d < best[0]:
                best = (d, float(self.cum[i] + t * self.seg[i]))
        return best[1]

    def point(self, s):
        s = s % self.L
        i = int(np.searchsorted(self.cum, s, side='right') - 1)
        i = min(max(i, 0), self.n - 1)
        t = (s - self.cum[i]) / self.seg[i] if self.seg[i] > 0 else 0.0
        return self.p[i] + t * (self.p[(i + 1) % self.n] - self.p[i])

    def tangent(self, s):
        return _flat(self.point(s + 2.0) - self.point(s - 2.0))

    def piece(self, s0, s1):
        """Polygon points from arc s0 to s1 (s1 > s0, wraps around)."""
        out = [self.point(s0)]
        k = s0
        while True:
            i = int(np.searchsorted(self.cum, k % self.L, side='right'))
            nxt = self.cum[i] + (k - k % self.L)
            if nxt >= s1 - 1e-6:
                break
            out.append(self.point(nxt))
            k = nxt + 1e-6
        out.append(self.point(s1))
        return out


def build(lap_pts, grid_pos, grid_fwd, grid_depth, finish_pos, start_line=None,
          pre_finish=15.0, post_finish=80.0, front_gap=8.0, behind_grid=60.0, total=None):
    """Points of the point-to-point lap spline (closed; game space).

    lap_pts: the original lap polygon (the B-spline's control points) whose
    road the race follows. grid_pos / grid_fwd: the pole position and the
    grid's facing; grid_depth: how far the grid reaches back (rows x spacing).
    finish_pos: where the race ends (snapped onto the route). start_line: an
    optional point the cars drive through from the grid before joining the
    route (where the grid sits off the lap line). Returns (points, info).
    """
    R = Route(lap_pts)
    gp = np.asarray(grid_pos, float)
    gf = _flat(grid_fwd)
    A = gp + gf * front_gap                     # the start wrap: just ahead of the front row
    # where the cars join the route: from the start line (or the front row) onward
    join_from = np.asarray(start_line, float) if start_line is not None else A
    s_join = R.project(join_from)
    s_B = R.project(finish_pos)
    if s_B <= s_join:
        s_B += R.L
    if s_B - s_join < 200.0:
        raise ValueError('the finish point must be well along the route from the start')
    B = R.point(s_B)
    fB = R.tangent(s_B)

    route = R.piece(s_join, s_B - pre_finish)
    pB0 = np.asarray(route[-1])
    post_B = R.piece(s_B, s_B + post_finish)
    pB1 = np.asarray(post_B[-1])
    to_A = [pB1 - _flat(pB1 - np.asarray(post_B[-2])) * 60.0 + UP * SKY,   # up and back past B
            A + gf * 60.0 + UP * SKY]                                    # down from ahead onto the front row
    post_A = [A] + ([np.asarray(start_line, float)] if start_line is not None else []) + route
    # last 5%: just before B, then under the grid to the front row, then back to 0 (B)
    lead_in = [pB0 + R.tangent(s_B - pre_finish) * 60.0 + UP * SKY]       # down from ahead onto pre_B
    pre_B = R.piece(s_B - pre_finish, s_B)
    back = gp - gf * (grid_depth + behind_grid)
    to_grid = [B - fB * 60.0 + UP * SKY,                                 # up and back from B
               back + gf * 60.0 + UP * SKY]                              # down from ahead onto the grid's back
    pre_A = [back, A]
    close = [A - gf * 60.0 + UP * SKY,                                   # up and back from the front row
             B + fB * 60.0 + UP * SKY]                                   # down from ahead onto B (point 0)

    start_before = _length(post_B + to_A + [A])
    route_len = _length(post_A)
    end_len = _length(lead_in + pre_B + to_grid + pre_A + close + [B])
    lo, hi = end_len / (WRAP * 0.9), (start_before + route_len) / (WRAP * 1.2)
    if total is None:
        total = (lo + hi) / 2.0
    if not lo <= total <= hi or start_before / total >= WRAP * 0.9:
        raise ValueError('route too short for a point-to-point lap: %.0f m of route for %.0f m of joining legs '
                         '(move the finish further along, or the start and finish closer together)'
                         % (route_len, end_len + start_before))

    # the dummy loop: from above the route's end up to the dummy height,
    # zigzag, then over to above pre_B's lead-in
    c = (pB0 + np.asarray(lead_in[0])) / 2.0
    rise = [pB0 - R.tangent(s_B - pre_finish) * 60.0 + UP * SKY, c + UP * DUMMY_HEIGHT]
    fixed = start_before + route_len + _length([pB0] + rise) + end_len
    remain = max(total - fixed, 1000.0)
    span = 8000.0
    n = max(2, int(math.ceil(remain / span)))
    width = remain / n
    zig = []
    side = np.array([1.0, 0.0, 0.0])
    ahead = np.array([0.0, 0.0, 1.0])
    for k in range(n):
        o = c + UP * DUMMY_HEIGHT + ahead * (k * 50.0)
        zig.append(o + side * (width / 2.0 if k % 2 == 0 else -width / 2.0))
    zig.append(c + UP * DUMMY_HEIGHT + ahead * (n * 50.0))
    start_part = post_B + to_A + post_A
    middle = rise + zig
    end_part = lead_in + pre_B + to_grid + pre_A + close
    pts = start_part + middle + end_part
    pts = [tuple(float(v) for v in p) for p in pts]
    # drop consecutive duplicates
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) > 1e-3:
            out.append(p)
    L = _length(out + [out[0]])
    info = {'length': L, 'route': route_len, 'start_share': start_before / L,
            'route_end': (start_before + route_len) / L, 'end_share': end_len / L,
            'finish': tuple(float(v) for v in B), 'start': tuple(float(v) for v in A)}
    return out, info
