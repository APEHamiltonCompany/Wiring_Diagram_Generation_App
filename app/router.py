"""
router.py - orthogonal line router for the wiring diagram (StyleGuide 9.1).

Lines are routed on a 10px grid:
* Every block is an obstacle grown by 10px (9.1.1 non-available space). A line
  may run ON that boundary (exactly 10px away) but never inside it.
* Two lines never share a grid segment, so parallel lines are always at least
  10px apart. Lines may cross at right angles (draw.io draws an arc jump), but
  a line never bends on a point another line passes through.
* A line leaves its source straight out of one face and enters its target
  straight into one face, never at a corner. Pipettor ports are entered from
  the left only.
* The result is a list of waypoints for <Array as="points">. The ends stay
  floating (source/target only, no exitX/entryX), so lines can still be moved
  along the block face in draw.io.
"""
from __future__ import annotations

import heapq
import math

G = 10          # grid / lane spacing (px)
CLEAR = 10      # clearance around blocks (px)

E, W, S, N = (1, 0), (-1, 0), (0, 1), (0, -1)
DIRS = (E, W, S, N)

STEP_COST = 1.0
BEND_COST = 6.0
CROSS_COST = 4.0
CENTER_COST = 0.15      # per grid step away from the middle of a face
JOG_COST = 20.0         # two bends 10px apart (a small zig-zag)
FACE_COST = 50.0
SOFT_COST = 40.0        # diagnostic route: using a lane another line holds
SOFT_BLOCK = 15.0       # diagnostic route: one grid step inside a block's clearance


class Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h


class Terminal:
    """One end of a line.

    rect   : the shape the line attaches to (a block or a pipettor port)
    owner  : the obstacle rect that contains it (pipettor body for a port)
    faces  : faces the line may use ("E", "W", "S", "N")
    prefer : faces to use when possible; the other faces cost FACE_COST
    """
    __slots__ = ("rect", "owner", "faces", "prefer")

    def __init__(self, rect, owner=None, faces=("E", "W", "S", "N"), prefer=None):
        self.rect, self.owner, self.faces = rect, owner or rect, faces
        self.prefer = prefer


def _ceil_g(v):
    return int(math.ceil(v / G - 1e-9))


def _floor_g(v):
    return int(math.floor(v / G + 1e-9))


class Router:
    def __init__(self, obstacles, bounds, vlines=()):
        """obstacles: list of Rect; bounds: (x0, y0, x1, y1) routing area;
        vlines: x of vertical separators (no vertical segment closer than 10px)."""
        self.obstacles = list(obstacles)
        x0, y0, x1, y1 = bounds
        self.ix0, self.iy0 = _ceil_g(x0), _ceil_g(y0)
        self.ix1, self.iy1 = _floor_g(x1), _floor_g(y1)
        self.no_vert = {ix for x in vlines
                        for ix in range(_floor_g(x - CLEAR), _ceil_g(x + CLEAR) + 1)
                        if abs(ix * G - x) < CLEAR - 1e-9}
        self.blocked = set()
        for r in self.obstacles:
            self._block(r)
        self.lines = {}
        self._rebuild()
        cells = (self.ix1 - self.ix0 + 1) * (self.iy1 - self.iy0 + 1)
        self.max_expand = max(400000, cells * 10)

    def _rebuild(self):
        self.seg_used = {}          # frozenset({a, b}) of grid nodes -> line id
        self.bend_nodes = {}        # nodes where a line bends / ends (exclusive) -> line id
        self.pass_nodes = {}        # node -> {"h"/"v": line id} straight passes
        for lid, nodes in self.lines.items():
            self._mark(nodes, lid)

    # ---- obstacles ---------------------------------------------------
    def _block(self, r):
        # strictly inside the grown rect is blocked; the boundary itself is free
        lo_x, hi_x = r.x - CLEAR, r.x2 + CLEAR
        lo_y, hi_y = r.y - CLEAR, r.y2 + CLEAR
        for ix in range(_floor_g(lo_x) + 1 if lo_x % G == 0 else _ceil_g(lo_x),
                        (_ceil_g(hi_x) - 1 if hi_x % G == 0 else _floor_g(hi_x)) + 1):
            for iy in range(_floor_g(lo_y) + 1 if lo_y % G == 0 else _ceil_g(lo_y),
                            (_ceil_g(hi_y) - 1 if hi_y % G == 0 else _floor_g(hi_y)) + 1):
                self.blocked.add((ix, iy))

    def in_bounds(self, n):
        return self.ix0 <= n[0] <= self.ix1 and self.iy0 <= n[1] <= self.iy1

    def free(self, n):
        return self.in_bounds(n) and n not in self.blocked

    # ---- terminals ---------------------------------------------------
    def _face_points(self, t):
        """(stub-end node, outward dir, stub length px, centre penalty) per usable spot."""
        r, o = t.rect, t.owner
        out = []
        small_v = r.h <= 30
        small_h = r.w <= 30
        mv = 5 if small_v else 10
        mh = 5 if small_h else 10
        for f in t.faces:
            fc = FACE_COST if t.prefer and f not in t.prefer else 0.0
            if f in ("E", "W"):
                lo, hi = r.y + mv, r.y2 - mv
                ys = list(range(_ceil_g(lo), _floor_g(hi) + 1))
                if not ys:
                    ys = [int(round((r.y + r.h / 2) / G))]
                mid = (r.y + r.h / 2) / G
                if f == "E":
                    face_x = r.x2
                    ex = _ceil_g(max(face_x, o.x2) + CLEAR)
                    d = E
                else:
                    face_x = r.x
                    ex = _floor_g(min(face_x, o.x) - CLEAR)
                    d = W
                for iy in ys:
                    out.append(((ex, iy), d, abs(ex * G - face_x), abs(iy - mid) * CENTER_COST + fc))
            else:
                lo, hi = r.x + mh, r.x2 - mh
                xs = list(range(_ceil_g(lo), _floor_g(hi) + 1))
                if not xs:
                    xs = [int(round((r.x + r.w / 2) / G))]
                mid = (r.x + r.w / 2) / G
                if f == "S":
                    face_y = r.y2
                    ey = _ceil_g(max(face_y, o.y2) + CLEAR)
                    d = S
                else:
                    face_y = r.y
                    ey = _floor_g(min(face_y, o.y) - CLEAR)
                    d = N
                for ix in xs:
                    out.append(((ix, ey), d, abs(ey * G - face_y), abs(ix - mid) * CENTER_COST + fc))
        return out

    def _node_ok_as_end(self, n, d):
        return (self.free(n) and n not in self.bend_nodes and n not in self.pass_nodes
                and not (d[0] == 0 and n[0] in self.no_vert))

    # ---- search ------------------------------------------------------
    def route(self, src: Terminal, tgt: Terminal, max_expand=None, lid=None):
        found = self._search(src, tgt, max_expand or self.max_expand)
        if found is None:
            return None
        nodes, d0, d1 = found
        self.commit(nodes, lid)
        return self._waypoints(nodes, d0, d1)

    def blockers(self, src: Terminal, tgt: Terminal, max_expand=None):
        """Diagnostic route where blocks and used lanes are expensive, not forbidden.
        Returns (line ids in the way, grid nodes inside blocks) or None."""
        found = self._search(src, tgt, max_expand or self.max_expand, soft=True)
        if found is None:
            return None
        nodes = found[0]
        lids, inside = set(), []
        for n in (nodes[0], nodes[-1]):
            if n in self.bend_nodes:
                lids.add(self.bend_nodes[n])
            lids.update(self.pass_nodes.get(n, {}).values())
        for a, b in zip(nodes, nodes[1:]):
            k = frozenset((a, b))
            if k in self.seg_used:
                lids.add(self.seg_used[k])
            if b in self.bend_nodes:
                lids.add(self.bend_nodes[b])
            if b in self.blocked:
                inside.append(b)
        for i in range(1, len(nodes) - 1):
            a, b, c = nodes[i - 1], nodes[i], nodes[i + 1]
            ps = self.pass_nodes.get(b)
            if not ps:
                continue
            bend = (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1])
            ori = "h" if a[1] == b[1] else "v"
            for o, owner in ps.items():
                if bend or o == ori:
                    lids.add(owner)
        lids.discard(None)
        return lids, inside

    def _end_cost(self, n, d, soft):
        if self._node_ok_as_end(n, d):
            return 0.0
        if soft and self.in_bounds(n) and not (d[0] == 0 and n[0] in self.no_vert):
            return SOFT_COST
        return None

    def _search(self, src, tgt, max_expand, soft=False):
        starts = []
        for n, d, ln, pen in self._face_points(src):
            ec = self._end_cost(n, d, soft)
            if ec is not None:
                starts.append((n, d, ln / G + pen + ec))
        goals = {}
        for n, d, ln, pen in self._face_points(tgt):
            ec = self._end_cost(n, d, soft)
            if ec is not None:
                inward = (-d[0], -d[1])
                c = ln / G + pen + ec
                if n not in goals or c < goals[n][1]:
                    goals[n] = (inward, c)
        if not starts or not goals:
            return None
        gx = [n[0] for n in goals]
        gy = [n[1] for n in goals]
        gx0, gx1, gy0, gy1 = min(gx), max(gx), min(gy), max(gy)

        def h(n):
            dx = 0 if gx0 <= n[0] <= gx1 else min(abs(n[0] - gx0), abs(n[0] - gx1))
            dy = 0 if gy0 <= n[1] <= gy1 else min(abs(n[1] - gy0), abs(n[1] - gy1))
            return dx + dy

        DONE = ("done",)
        best = {}
        prev = {}
        heap = []
        cnt = 0
        for n, d, c in starts:
            st = (n, d, False)
            if c < best.get(st, 1e18):
                best[st] = c
                prev[st] = None
                heapq.heappush(heap, (c + h(n), cnt, c, st))
                cnt += 1
        expanded = 0
        end_state = None
        while heap:
            f, _k, g, st = heapq.heappop(heap)
            if st == DONE:
                end_state = prev[DONE]
                break
            if g > best.get(st, 1e18) + 1e-9:
                continue
            expanded += 1
            if expanded > max_expand:
                break
            n, d, fresh = st
            # finishing here?
            if n in goals:
                inward, c_end = goals[n]
                if d == inward:
                    tot = g + c_end
                elif (d[0] == -inward[0] and d[1] == -inward[1]):
                    tot = None
                else:
                    tot = g + c_end + BEND_COST + (JOG_COST if fresh else 0.0)
                if tot is not None and tot < best.get(DONE, 1e18):
                    best[DONE] = tot
                    prev[DONE] = st
                    heapq.heappush(heap, (tot, cnt, tot, DONE))
                    cnt += 1
            for nd in DIRS:
                if nd[0] == -d[0] and nd[1] == -d[1]:
                    continue
                bend = nd != d
                pen = 0.0
                if bend and (n in self.pass_nodes or (n in self.bend_nodes)):
                    if not soft:
                        continue
                    pen += SOFT_COST
                if nd[0] == 0 and n[0] in self.no_vert:
                    continue
                m = (n[0] + nd[0], n[1] + nd[1])
                if not self.free(m):
                    if not soft or not self.in_bounds(m):
                        continue
                    pen += SOFT_BLOCK
                if frozenset((n, m)) in self.seg_used or m in self.bend_nodes:
                    if not soft:
                        continue
                    pen += SOFT_COST
                cost = STEP_COST + pen + (BEND_COST if bend else 0.0)
                if bend and fresh:
                    cost += JOG_COST
                ori = "h" if nd[1] == 0 else "v"
                passes = self.pass_nodes.get(m)
                if passes:
                    if ori in passes:
                        if not soft:
                            continue
                        cost += SOFT_COST
                    cost += CROSS_COST
                ns = (m, nd, bend)
                ng = g + cost
                if ng < best.get(ns, 1e18) - 1e-9:
                    best[ns] = ng
                    prev[ns] = st
                    heapq.heappush(heap, (ng + h(m), cnt, ng, ns))
                    cnt += 1
        if end_state is None:
            return None
        # rebuild node list
        path = []
        st = end_state
        while st is not None:
            path.append(st)
            st = prev[st]
        path.reverse()
        nodes = [s[0] for s in path]
        d0 = path[0][1]
        d1 = goals[nodes[-1]][0]
        return nodes, d0, d1

    # ---- bookkeeping ---------------------------------------------------
    def commit(self, nodes, lid=None):
        if lid is None:
            lid = ("anon", len(self.lines))
        self.lines[lid] = nodes
        self._mark(nodes, lid)

    def uncommit(self, lid):
        nodes = self.lines.pop(lid, None)
        if nodes is not None:
            self._rebuild()
        return nodes

    def _mark(self, nodes, lid):
        for a, b in zip(nodes, nodes[1:]):
            self.seg_used[frozenset((a, b))] = lid
        self.bend_nodes[nodes[0]] = lid
        self.bend_nodes[nodes[-1]] = lid
        for i in range(1, len(nodes) - 1):
            a, b, c = nodes[i - 1], nodes[i], nodes[i + 1]
            if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
                self.bend_nodes[b] = lid
            else:
                self.pass_nodes.setdefault(b, {})["h" if a[1] == b[1] else "v"] = lid

    @staticmethod
    def _waypoints(nodes, d0, d1):
        """Bend points plus the two stub ends. A stub end is dropped when the next
        point is straight out from the same face (draw.io then still leaves the
        face at the same height/position)."""
        pts = [nodes[0]]
        for i in range(1, len(nodes) - 1):
            a, b, c = nodes[i - 1], nodes[i], nodes[i + 1]
            if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
                pts.append(b)
        pts.append(nodes[-1])
        # dedupe consecutive duplicates (single-node paths)
        out = []
        for p in pts:
            if not out or out[-1] != p:
                out.append(p)
        if len(out) >= 2:
            a, b = out[0], out[1]
            if (d0[1] == 0 and a[1] == b[1]) or (d0[0] == 0 and a[0] == b[0]):
                out.pop(0)
        if len(out) >= 2:
            a, b = out[-1], out[-2]
            if (d1[1] == 0 and a[1] == b[1]) or (d1[0] == 0 and a[0] == b[0]):
                out.pop()
        return [(ix * G, iy * G) for ix, iy in out]
