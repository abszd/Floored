"""Closing door openings (and other short breaks in walls) with virtual wall segments.

Rooms are found as enclosed free space, so every doorway must be closed first.
Instead of a blanket morphological closing (which also swallows narrow rooms and
glues corridors shut) this matches wall *ends*:

* two wall ends that face each other along the same line (a door in a straight
  wall, or a run of dashed wall), and
* a wall end that points at another wall a short distance away (a door next to a
  corner or a perpendicular wall).

The typical door width is measured from the drawing itself (the most common
distance between facing wall ends), so no scale information is needed. Openings
up to ``max_gap_doors`` door widths are closed; wider openings are left for the
room splitter, which only cuts at real constrictions.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy.spatial import cKDTree

from . import graph


@dataclass
class Gap:
    p: np.ndarray  # (x, y)
    q: np.ndarray
    thickness: float
    kind: str  # "pair" (facing wall ends), "corner" (facing wall runs that end at corners),
    #            "window" (a drawn-in span) or "ray" (wall end to a wall)

    @property
    def length(self) -> float:
        return float(np.hypot(*(self.q - self.p)))


@dataclass
class GapResult:
    mask: np.ndarray  # bool: virtual wall pixels
    gaps: list
    door_width: float  # typical door width in px (estimated)


@dataclass
class _End:
    tip: np.ndarray
    d: np.ndarray
    width: float
    branch: int


def find_ends(mask: np.ndarray, g, wall_w: float, min_width: float = 0.0) -> list:
    """Free wall ends with outward direction, measured at the true end of the wall."""
    H, W = mask.shape
    ends = []
    span = max(6.0, 3.0 * wall_w)
    for i, b in enumerate(g.branches):
        if b.length < max(3.0, 0.8 * wall_w):
            continue
        for at_a, free in ((True, b.a_free), (False, b.b_free)):
            if not free:
                continue
            d, p = graph.end_direction(b, at_a, span)
            if d is None:
                continue
            if b.width < min_width:
                continue
            # The skeleton stops short of the wall's end face; walk out to it.
            tip = p.astype(np.float64)
            for _ in range(int(2 * max(wall_w, b.width)) + 3):
                nxt = tip + d * 0.5
                x, y = int(round(nxt[0])), int(round(nxt[1]))
                if not (0 <= x < W and 0 <= y < H) or not mask[y, x]:
                    break
                tip = nxt
            ends.append(_End(tip, d, b.width, i))
    return ends


def find_corner_ends(mask: np.ndarray, g, wall_w: float, min_run: float) -> list:
    """Ends of straight wall runs that stop at a corner or a junction rather than at a
    free end. An opening whose sides are wall corners (a corridor that opens through
    the exterior wall, a door at an L) has no free wall ends to pair up, but the
    straight runs on either side still face each other across it."""
    H, W = mask.shape
    out = []
    for i, b in enumerate(g.branches):
        if b.length < min_run:
            continue
        poly = cv2.approxPolyDP(b.pts.astype(np.float32).reshape(-1, 1, 2), max(1.5, 0.5 * b.width), False)
        poly = poly.reshape(-1, 2).astype(np.float64)
        if len(poly) < 2:
            continue
        seg = np.diff(poly, axis=0)
        seglen = np.hypot(seg[:, 0], seg[:, 1])
        for k in range(len(seg)):
            if seglen[k] < min_run:
                continue
            u = seg[k] / seglen[k]
            # The run's start (pointing back) and end (pointing forward), unless that end
            # is the branch's free end, which ``find_ends`` already covers.
            for at_start in (True, False):
                if at_start:
                    if k == 0 and b.a_free:
                        continue
                    p, d = poly[k], -u
                    if k > 0 and _turn(seg[k - 1], seg[k]) < 60:
                        continue
                else:
                    if k == len(seg) - 1 and b.b_free:
                        continue
                    p, d = poly[k + 1], u
                    if k < len(seg) - 1 and _turn(seg[k], seg[k + 1]) < 60:
                        continue
                tip = p.copy()
                for _ in range(int(3 * wall_w) + 4):
                    nxt = tip + d * 0.5
                    x, y = int(round(nxt[0])), int(round(nxt[1]))
                    if not (0 <= x < W and 0 <= y < H) or not mask[y, x]:
                        break
                    tip = nxt
                out.append(_End(tip, d, b.width, i))
    return out


def _turn(a, b) -> float:
    ang = np.degrees(np.arctan2(b[1], b[0]) - np.arctan2(a[1], a[0]))
    return abs((ang + 180) % 360 - 180)


def _facing_distances(ends: list, mask: np.ndarray, g_hi: float) -> list:
    if len(ends) < 2:
        return []
    tips = np.array([e.tip for e in ends])
    dirs = np.array([e.d for e in ends])
    out = []
    cos_tol = np.cos(np.deg2rad(32))
    for i, j in cKDTree(tips).query_pairs(g_hi, output_type="ndarray"):
        if ends[i].branch == ends[j].branch:
            continue
        v = tips[j] - tips[i]
        dist = float(np.hypot(*v))
        if dist < 1.0:
            continue
        u = v / dist
        if dirs[i] @ u < cos_tol or -dirs[j] @ u < cos_tol or dirs[i] @ dirs[j] > -0.8:
            continue
        perp = abs(dirs[i][0] * v[1] - dirs[i][1] * v[0])
        if perp > max(0.9 * max(ends[i].width, ends[j].width), 2.5):
            continue
        if _segment_clear(mask, tips[i], tips[j], 1.5) > 0.12:
            continue
        out.append(dist)
    return out


def estimate_door(mask: np.ndarray, g, wall_w: float, text_h: float) -> float:
    """Typical door width: the most common distance between facing wall ends."""
    g_hi = max(16.0 * wall_w, 4.0 * (text_h or 0.0), 24.0)
    d = np.array([x for x in _facing_distances(find_ends(mask, g, wall_w), mask, g_hi) if x >= 2.0 * wall_w])
    if d.size >= 3:
        door = _mode(d, wall_w)
    else:
        door = max(6.0 * wall_w, 1.1 * (text_h or 0.0), 12.0)
    return float(np.clip(door, 4.0 * wall_w, g_hi / 2.0))


def _segment_clear(mask: np.ndarray, p: np.ndarray, q: np.ndarray, skip: float) -> float:
    """Fraction of the segment p-q (ignoring ``skip`` px at both ends) that crosses walls."""
    L = float(np.hypot(*(q - p)))
    if L <= 2 * skip + 1:
        return 0.0
    n = int(L) + 1
    t = np.linspace(skip / L, 1 - skip / L, max(2, n))
    pts = p[None, :] + (q - p)[None, :] * t[:, None]
    xs = np.clip(np.round(pts[:, 0]).astype(int), 0, mask.shape[1] - 1)
    ys = np.clip(np.round(pts[:, 1]).astype(int), 0, mask.shape[0] - 1)
    return float(mask[ys, xs].mean())


def _ray(mask: np.ndarray, p: np.ndarray, d: np.ndarray, max_len: float):
    """March from p along d; return the first wall pixel hit and its distance."""
    H, W = mask.shape
    step = 0.5
    t = 0.5
    # Leave the wall the ray starts in first.
    while t <= max_len:
        x, y = p + d * t
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < W and 0 <= yi < H) or not mask[yi, xi]:
            break
        t += step
    t0 = t
    t += 1.0
    while t <= max_len + t0:
        x, y = p + d * t
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < W and 0 <= yi < H):
            return None, None
        if mask[yi, xi]:
            return np.array([x, y]), t
        t += step
    return None, None


def close_gaps(mask: np.ndarray, g, wall_w: float, text_h: float, max_gap_doors: float = 2.2,
               ray_min_width: float = 0.0, support: np.ndarray = None, door_hint: float = 0.0) -> GapResult:
    """Close doorways in ``mask``. ``support`` holds thinner linework: a wide gap whose
    span is drawn in (a window, a glazed wall) is closed even beyond door width. Ends of
    lines thinner than ``ray_min_width`` are only extended across door-sized gaps.
    ``door_hint`` is an earlier door width estimate (from the heavy walls alone); the
    estimate here only refines it."""
    H, W = mask.shape
    ends = find_ends(mask, g, wall_w)
    virtual = np.zeros((H, W), np.uint8)
    if len(ends) < 1:
        return GapResult(virtual.astype(bool), [], 0.0)
    tips = np.array([e.tip for e in ends])
    dirs = np.array([e.d for e in ends])

    g_hi = max(16.0 * wall_w, 4.0 * (text_h or 0.0), 24.0)
    cos_tol = np.cos(np.deg2rad(32))

    def facing(i, j):
        v = tips[j] - tips[i]
        dist = float(np.hypot(*v))
        if dist < 1.0:
            return None
        u = v / dist
        ci = float(dirs[i] @ u)
        cj = float(-dirs[j] @ u)
        if ci < cos_tol or cj < cos_tol or float(dirs[i] @ dirs[j]) > -0.8:
            return None
        # Lateral offset of each tip from the other's centre line.
        perp_i = abs(dirs[i][0] * v[1] - dirs[i][1] * v[0])
        perp_j = abs(dirs[j][0] * v[1] - dirs[j][1] * v[0])
        # A few degrees of slack: jambs are rarely drawn on exactly one line.
        tol = max(0.9 * max(ends[i].width, ends[j].width), 2.5) + 0.05 * dist
        if max(perp_i, perp_j) > tol:
            return None
        return dist, ci, cj

    g_win = 3.0 * g_hi
    tree = cKDTree(tips)
    pairs = tree.query_pairs(g_win, output_type="ndarray")
    pair_cands = []
    for i, j in pairs:
        if ends[i].branch == ends[j].branch:
            continue
        f = facing(i, j)
        if f is None:
            continue
        dist, ci, cj = f
        if _segment_clear(mask, tips[i], tips[j], 1.5) > 0.12:
            continue
        drawn = support is not None and _segment_clear(support, tips[i], tips[j], 1.0) >= 0.6
        pair_cands.append((dist, i, j, ci, cj, drawn))

    # Typical door width: the most common facing distance (ignoring tiny breaks and
    # spans that are drawn in, which are windows rather than doors).
    dists = np.array([c[0] for c in pair_cands if c[0] >= 2.0 * wall_w and not c[5] and c[0] <= g_hi])
    heavy = np.array([c[0] for c in pair_cands if c[0] >= 2.0 * wall_w and not c[5] and c[0] <= g_hi
                      and min(ends[c[1]].width, ends[c[2]].width) >= ray_min_width])
    if heavy.size >= 3:
        dists = heavy  # gaps between wall-weight lines are the reliable door samples
    if door_hint > 0:
        dists = dists[(dists >= 0.6 * door_hint) & (dists <= 1.7 * door_hint)]
    if dists.size >= 3:
        door = _mode(dists, wall_w)
    elif door_hint > 0:
        door = door_hint
    else:
        door = max(6.0 * wall_w, 1.1 * (text_h or 0.0), 12.0)
    door = float(np.clip(door, 4.0 * wall_w, g_hi / 2.0))
    g_max = min(max_gap_doors * door, g_hi)
    g_ray = min(1.5 * door, g_hi)

    cands = []
    # Openings between wall corners: straight heavy runs facing each other across a
    # door-sized gap (kept short, so corridor mouths are not shut).
    corners = [e for e in find_corner_ends(mask, g, wall_w, max(2.0 * wall_w, 0.5 * door))
               if e.width >= ray_min_width]
    n_free = len(ends)
    if corners:
        ends = ends + corners
        tips = np.array([e.tip for e in ends])
        dirs = np.array([e.d for e in ends])
        atree = cKDTree(tips)
        lo, hi = max(2.0 * wall_w, 0.4 * door), 1.4 * door
        for a, nb in enumerate(atree.query_ball_point(tips[n_free:], hi)):
            i = n_free + a
            for j in nb:
                if j == i or (j >= n_free and j < i) or np.allclose(tips[i], tips[j]):
                    continue
                if j < n_free and ends[j].width < ray_min_width:
                    continue
                f = facing(i, j)
                if f is None or not (lo <= f[0] <= hi):
                    continue
                if _segment_clear(mask, tips[i], tips[j], 1.5) > 0.05:
                    continue
                cands.append((1.2 * f[0] * (1.0 + 0.6 * (2.0 - f[1] - f[2])), "corner", i, j, None))
    for dist, i, j, ci, cj, drawn in pair_cands:
        if dist <= g_max or drawn:
            cost = dist * (1.0 + 0.6 * (2.0 - ci - cj)) * (0.5 if drawn else 1.0)
            cands.append((cost, "window" if drawn else "pair", i, j, None))
    for i, e in enumerate(ends[:n_free]):
        # Light line ends (thinner than ``ray_min_width``) only reach across a narrow
        # opening: a door leaf or furniture edge must not wall off half a room.
        reach = g_ray if e.width >= ray_min_width else min(g_ray, 1.0 * door)
        hit, t = _ray(mask, e.tip, e.d, reach)
        if hit is None:
            continue
        cands.append((1.3 * t, "ray", i, -1, hit))
    cands.sort(key=lambda c: c[0])

    used = np.zeros(len(ends), bool)
    gaps = []
    for cost, kind, i, j, hit in cands:
        if used[i] or (j >= 0 and used[j]):
            continue
        p = ends[i].tip
        q = ends[j].tip if j >= 0 else hit
        thick = max(2.0, min(ends[i].width, ends[j].width if j >= 0 else ends[i].width))
        # Do not cross a virtual wall drawn earlier (two doors sharing a jamb, etc.).
        if _segment_clear(virtual.astype(bool), p, q, 2.0) > 0.2:
            continue
        cv2.line(virtual, tuple(int(round(v)) for v in p), tuple(int(round(v)) for v in q), 1,
                 max(2, int(round(thick))), cv2.LINE_8)
        used[i] = True
        if j >= 0:
            used[j] = True
        gaps.append(Gap(p.copy(), q.copy(), thick, kind))
    return GapResult(virtual.astype(bool) & ~mask, gaps, door)


def _mode(values: np.ndarray, wall_w: float) -> float:
    """Densest value of a 1-D sample (kernel density with a bandwidth tied to wall width)."""
    bw = max(1.5, 0.12 * float(np.median(values)))
    grid = np.linspace(values.min(), values.max(), 200)
    dens = np.exp(-0.5 * ((grid[:, None] - values[None, :]) / bw) ** 2).sum(axis=1)
    return float(grid[int(np.argmax(dens))])
