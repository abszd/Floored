"""From ink to a clean wall mask.

Steps, in order:

1. Drop text pixels and specks.
2. *Solidify*: fill the inside of walls drawn as two parallel lines or as a
   hatched outline, so every style ends up with solid walls of real thickness.
3. Measure stroke widths along the skeleton: the typical wall thickness drives
   most later size thresholds.
4. Remove small isolated objects (furniture, symbols, stray marks) unless they
   are thick, wall-like pieces sitting next to the wall network (piers between
   doors, column stubs).
5. Keep only the drawing's main plan: drop page frames, title blocks, legends,
   north arrows and anything else that is far from the building.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

from . import graph
from . import skeleton as skel


@dataclass
class Walls:
    mask: np.ndarray  # bool: every kept line (walls, partitions, and minor linework)
    barrier: np.ndarray  # bool: lines that bound rooms (heavy walls + long straight thin walls)
    strong: np.ndarray  # bool: heavy walls only (equals barrier when line weights are uniform)
    width: float  # typical thickness of the room-bounding lines, px
    thin: float  # typical thin-line width, px
    tau: float  # line-weight threshold between minor lines and walls (0: single weight)
    door_guess: float  # first estimate of the door width, px
    plan_bbox: tuple  # x0, y0, x1, y1 of the kept plan
    double_line: bool  # walls were drawn as parallel line pairs (and have been filled)
    removed: np.ndarray  # bool: ink removed as clutter (furniture, symbols, marks, arrows)
    coverage: np.ndarray  # float32 ink coverage used for sub-pixel width measurements

    @property
    def thick_plan(self) -> bool:
        return self.tau > 0


def extract(ink: np.ndarray, contrast: np.ndarray, text_mask: np.ndarray, text_h: float) -> Walls:
    base = ink & ~text_mask
    base = _drop_specks(base, 4)

    solid, double_line = _solidify(base, text_h)
    coverage = _coverage(base, solid, contrast)

    width0, _ = _stroke_widths(solid)
    prune_len = max(2, int(round(0.5 * width0)) + 1)

    kept, removed = _remove_clutter(solid, width0, text_h)
    kept, bbox = _main_plan(kept, width0)

    g = graph.build(kept, coverage, prune_len)
    # Door swings: quarter circles drawn from a jamb, with the door leaf as a radius.
    # They are annotation whatever their line weight, and their radius is the door width.
    swing_mask, radii = _door_swings(g, kept, width0, float(np.hypot(*kept.shape)))
    if radii:
        removed |= swing_mask & kept
        kept = _drop_specks(kept & ~swing_mask, max(4, int(width0 * width0)))
        g = graph.build(kept, coverage, prune_len)
    tau, thin = _line_weights(g)
    from .gaps import estimate_door

    if tau > 0:
        # Measure doors on the heavy lines alone: door leaves and swings join the jambs,
        # and furniture and stair treads leave small gaps that would swamp the count.
        heavy = _drop_specks(kept & ~_branch_lut(g, [b.width < tau for b in g.branches])[g.owner + 2],
                             max(4, int(width0 * width0)))
        door = estimate_door(heavy, graph.build(heavy, coverage, prune_len), width0, text_h)
        st = _light_stats(g, tau, door)
        if st["straight"] >= 0.85 and st["curved"] <= 0.05:
            # The light lines are long straight runs: a lighter class of walls (interior
            # walls in a wayfinding map), not annotation. Treat all lines as walls.
            tau, thin = 0.0, 1.0
    if tau <= 0:
        door = estimate_door(kept, g, width0, text_h)
    if len(radii) >= 3:
        door = float(np.median(radii))

    # Callout and leader arrows never enclose anything, and their tips would be taken
    # for wall ends by the door closing.
    for _ in range(2):
        dt = cv2.distanceTransform(kept.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        drop = [i for i, b in enumerate(g.branches) if (b.a_free or b.b_free) and _is_arrow(b, dt, width0)]
        drop = sorted(set(drop) | set(_open_arrows(g, door)))
        if not drop:
            break
        erase = _branch_lut(g, np.isin(np.arange(len(g.branches)), drop))[g.owner + 2] & kept
        kept = _drop_specks(kept & ~erase, max(4, int(width0 * width0)))
        g = graph.build(kept, coverage, prune_len)

    if tau > 0 and g.branches:
        # Free-standing groups of light lines (a table with its chairs, a bed, a desk
        # cluster) are furniture: drop them whole, however large the group is.
        light_px = _branch_lut(g, [b.width < tau for b in g.branches])[g.owner + 2]
        heavy_px = kept & ~light_px & (g.owner != -1)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(kept.astype(np.uint8), connectivity=8)
        has_heavy = np.bincount(lab[heavy_px], minlength=n) >= max(4, int(width0 * width0))
        diag = np.hypot(stats[:, 2], stats[:, 3])
        furn = (~has_heavy) & (diag <= 4.0 * door)
        furn[0] = False
        if furn.any():
            drop_px = furn[lab]
            removed |= drop_px
            kept = kept & ~drop_px
            g = graph.build(kept, coverage, prune_len)

    strong = barrier = kept
    widths = np.array([b.width for b in g.branches]) if g.branches else np.zeros(0)
    lengths = np.array([b.length for b in g.branches]) if g.branches else np.zeros(0)
    if tau > 0 and len(g.branches):
        light = widths < tau
        w_min = float(np.sqrt(tau * thin))  # closer to the wall weight than to the light one
        structural = np.array([_structural(b, door, w_min) for b in g.branches])
        strong = _drop_specks(kept & ~_branch_lut(g, light)[g.owner + 2], max(4, int(width0 * width0)))
        barrier = _drop_specks(kept & ~_branch_lut(g, light & ~structural)[g.owner + 2], max(4, int(width0 * width0)))
        sel = ~(light & ~structural) & (lengths >= 2 * widths)
    else:
        sel = lengths >= 2 * widths
    width = _weighted_percentile(widths[sel], lengths[sel], 50) if sel.any() else width0
    removed |= solid & ~kept
    return Walls(kept, barrier, strong, max(float(width), 1.0), thin, tau, door, bbox, double_line, removed, coverage)


def _door_swings(g, mask: np.ndarray, wall_w: float, size: float):
    """Find door swing arcs and their leaves.

    Branches are cut at sharp corners (an arc meets its jamb and its leaf at right
    angles), and every curved piece is fitted with a circle. A swing is a circular arc
    of 50-200 degrees with a door-like radius; its leaf is the straight line drawn in
    from the circle's centre (the hinge) to one end of the arc.

    Returns (mask of swing and leaf pixels to erase, list of radii).
    """
    erase = np.zeros(mask.shape, np.uint8)
    radii = []
    r_lo, r_hi = max(8.0, 3.0 * wall_w), 0.08 * size
    H, W = mask.shape
    for b in g.branches:
        if b.length < 1.2 * r_lo or len(b.pts) < 8 or b.width > 1.2 * wall_w:
            continue
        pts = b.pts
        s = 5
        cuts = [0] + _corners(pts, s, 55.0) + [len(pts) - 1]
        for k0, k1 in zip(cuts[:-1], cuts[1:]):
            # Leave out the few points bent by the corner itself.
            seg = pts[k0 + (s // 2 if k0 else 0):k1 + 1 - (s // 2 if k1 < len(pts) - 1 else 0)]
            if len(seg) < max(8, 0.5 * r_lo):
                continue
            fit = _fit_circle(seg)
            if fit is None:
                continue
            c, r, resid = fit
            if not (r_lo <= r <= r_hi) or resid > max(1.0, min(0.05 * r, 2.0)):
                continue
            # The middle of the piece must bend like the whole: a straight run whose ends
            # curl into junctions also fits a wide circle, but its middle is straight.
            q = len(seg) // 4
            mid = _fit_circle(seg[q:len(seg) - q]) if len(seg) - 2 * q >= 6 else None
            if mid is None or not (0.6 * r <= mid[1] <= 1.6 * r):
                continue
            ang = np.unwrap(np.arctan2(seg[:, 1] - c[1], seg[:, 0] - c[0]))
            span = np.degrees(abs(ang[-1] - ang[0]))
            if not (50 <= span <= 200):
                continue
            t = max(3, int(np.ceil(1.5 * min(b.width, wall_w))) + 2)
            inner = seg[max(1, len(seg) // 20):len(seg) - max(1, len(seg) // 20)]
            cv2.polylines(erase, [np.round(inner).astype(np.int32).reshape(-1, 1, 2)], False, 1, t)
            radii.append(r)
            # The leaf: an inked radius from the hinge to one end of the arc (the corner
            # where the arc was cut, when it was cut there).
            for e in (pts[k0], pts[k1]):
                v = e - c
                n = int(np.hypot(*v))
                if n < 4:
                    continue
                ts = np.linspace(0.25, 0.95, max(8, n))
                xs = np.clip(np.round(c[0] + v[0] * ts).astype(int), 0, W - 1)
                ys = np.clip(np.round(c[1] + v[1] * ts).astype(int), 0, H - 1)
                if mask[ys, xs].mean() >= 0.85:
                    # All the way to the hinge: a stub left there would turn the jamb's
                    # end sideways and hide the doorway from the gap closing.
                    p0 = np.round(c).astype(int)
                    cv2.line(erase, (int(p0[0]), int(p0[1])), (int(round(e[0])), int(round(e[1]))), 1, t)
                    break
    return erase.astype(bool), radii


def _corners(pts: np.ndarray, s: int, min_turn: float) -> list:
    """Indices of sharp turns along an ordered point chain, measured over ``s`` points on
    either side so that a right angle drawn as two 45-degree steps still counts."""
    n = len(pts)
    if n < 2 * s + 1:
        return []
    d1 = pts[s:n - s] - pts[:n - 2 * s]
    d2 = pts[2 * s:] - pts[s:n - s]
    t = np.degrees(np.arctan2(d2[:, 1], d2[:, 0]) - np.arctan2(d1[:, 1], d1[:, 0]))
    t = np.abs((t + 180.0) % 360.0 - 180.0)
    out = []
    for i in np.argsort(-t, kind="stable"):
        if t[i] < min_turn:
            break
        if all(abs(int(i) - j) > s for j in out):
            out.append(int(i))
    return sorted(i + s for i in out)


def _fit_circle(pts: np.ndarray):
    """Least-squares circle through points: (centre, radius, rms residual) or None."""
    x, y = pts[:, 0].astype(np.float64), pts[:, 1].astype(np.float64)
    A = np.stack([x, y, np.ones_like(x)], axis=1)
    rhs = -(x * x + y * y)
    try:
        (D, E, F), *_ = np.linalg.lstsq(A, rhs, rcond=None)
    except np.linalg.LinAlgError:
        return None
    c = np.array([-D / 2.0, -E / 2.0])
    r2 = c @ c - F
    if r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    resid = float(np.sqrt(np.mean((np.hypot(x - c[0], y - c[1]) - r) ** 2)))
    return c, r, resid


def _branch_lut(g, flags) -> np.ndarray:
    """Lookup table indexed by ``g.owner + 2`` (owner is -2 for junction pixels, -1 for none)."""
    return np.concatenate([[False, False], np.asarray(flags, bool).reshape(-1)])


def _structural(b, door: float, w_min: float = 0.0) -> bool:
    """Light lines that still bound rooms: long straight or right-angled runs (partitions,
    glazing) and short straight pieces of wall left between doorways and corners.
    Curves (door swings), short pieces joined at both ends (stair treads, door leaves
    with their swing), short pieces drawn at the annotation weight (furniture edges,
    below ``w_min``) and short loose dashes are minor linework."""
    if b.length >= 1.5 * door:
        return _rectilinear(b, door, 0.4 * door)
    if b.width < w_min:
        return False
    if b.a_free != b.b_free:
        return b.length >= 0.3 * door and _rectilinear(b, door, 0.3 * door, max_segments=1)
    if b.a_free and b.b_free:
        return b.length >= 1.0 * door and _rectilinear(b, door, 0.5 * door, max_segments=1)
    return False


def _rectilinear(b, door: float, min_seg: float, max_segments: int = 0) -> bool:
    """Straight segments of at least ``min_seg`` px meeting at right angles."""
    poly = cv2.approxPolyDP(b.pts.astype(np.float32).reshape(-1, 1, 2), max(1.5, 0.03 * door), False).reshape(-1, 2)
    seg = np.diff(poly, axis=0)
    seglen = np.hypot(seg[:, 0], seg[:, 1])
    if len(seglen) == 0 or seglen.min() < min_seg or (max_segments and len(seglen) > max_segments):
        return False
    ang = np.degrees(np.arctan2(seg[:, 1], seg[:, 0]))
    for a0, a1 in zip(ang[:-1], ang[1:]):
        turn = abs((a1 - a0 + 180) % 360 - 180)
        if not (turn < 15 or abs(turn - 90) < 15):
            return False
    return True


def _open_arrows(g, door: float) -> list:
    """Arrows drawn with an open V head: a shaft ending in a junction from which two short
    loose barbs sweep back on either side. Returns the shaft and barb branch indices."""
    barbs = [i for i, b in enumerate(g.branches)
             if b.a_free != b.b_free and 2.0 <= b.length <= 0.8 * door
             and _rectilinear(b, door, 0.0, max_segments=1)]
    if len(barbs) < 2:
        return []
    root = np.array([g.branches[i].pts[-1] if g.branches[i].a_free else g.branches[i].pts[0] for i in barbs])
    tree = cKDTree(root)
    node_ends = []  # (point, branch, at_a) for every end sitting on a junction
    for i, b in enumerate(g.branches):
        if not b.a_free:
            node_ends.append((b.pts[0], i, True))
        if not b.b_free:
            node_ends.append((b.pts[-1], i, False))
    ntree = cKDTree(np.array([e[0] for e in node_ends]))
    out = set()
    seen = set()
    for k, i in enumerate(barbs):
        group = [barbs[j] for j in tree.query_ball_point(root[k], 3.0)]
        key = tuple(sorted(group))
        if len(group) < 2 or key in seen:
            continue
        seen.add(key)
        blen = max(g.branches[j].length for j in group)
        node = root[k]
        for e in ntree.query_ball_point(node, 3.0):
            _, s, at_a = node_ends[e]
            sb = g.branches[s]
            if s in group or sb.length < 1.5 * blen:
                continue
            d, _ = graph.end_direction(sb, at_a, min(sb.length, 3.0 * blen))
            if d is None:
                continue
            back = -d
            sides = []
            for j in group:
                bj = g.branches[j]
                tip = bj.pts[0] if bj.a_free else bj.pts[-1]
                v = tip - node
                n = float(np.hypot(*v))
                if n < 1.0:
                    continue
                v /= n
                ang = np.degrees(np.arccos(np.clip(v @ back, -1, 1)))
                if 12 <= ang <= 70:
                    sides.append((np.sign(back[0] * v[1] - back[1] * v[0]), j, bj.length))
            left = [x for x in sides if x[0] > 0]
            right = [x for x in sides if x[0] < 0]
            if left and right:
                l, r = max(left, key=lambda x: x[2]), max(right, key=lambda x: x[2])
                if min(l[2], r[2]) >= 0.5 * max(l[2], r[2]):
                    out.update((s, l[1], r[1]))
                    break
    return sorted(out)


def _line_weights(g):
    """Find the annotation line weight, if the drawing has one distinct from the walls.

    Returns (tau, thin): branches lighter than ``tau`` are minor linework candidates
    (tau = 0 when all lines share one weight) and ``thin`` is that light weight.

    Widths are pooled into a length-weighted density over log width. The lightest
    common weight is the light class and the first clearly heavier common weight the
    walls. The threshold goes in the heaviest clear dip below the walls, so that door
    leaves drawn a little heavier than furniture still fall on the light side. Whether the light class is annotation or a
    lighter class of walls is decided later from its shapes (see ``_light_is_wall``).
    """
    sel = [b for b in g.branches if b.length >= max(4.0, 2.0 * b.width)]
    if len(sel) < 5:
        return 0.0, 1.0
    ws = np.array([b.width for b in sel])
    ls = np.array([b.length for b in sel])
    grid = np.linspace(np.log(0.4), np.log(40.0), 240)
    lw = np.log(np.maximum(ws, 0.4))
    dens = (ls[None, :] * np.exp(-0.5 * ((grid[:, None] - lw[None, :]) / 0.09) ** 2)).sum(axis=1)
    dens /= dens.sum()

    def mass(i):
        return float(dens[np.abs(grid - grid[i]) <= 0.25].sum())

    peaks = [i for i in range(1, len(grid) - 1) if dens[i] >= dens[i - 1] and dens[i] > dens[i + 1]]
    peaks = [i for i in peaks if mass(i) >= 0.02]
    if len(peaks) < 2:
        return 0.0, float(np.exp(grid[peaks[0]])) if peaks else 1.0
    p = peaks[0]
    for q in peaks[1:]:
        if mass(q) >= 0.12 and grid[q] - grid[p] >= np.log(1.5):
            light = [i for i in peaks if i < q and grid[q] - grid[i] >= np.log(1.5)]
            top = light[-1]
            hi = max(top, int(np.searchsorted(grid, grid[q] - np.log(1.25))))
            # The heaviest real dip below the walls: a local minimum well below the
            # density on both sides of it.
            v = top + int(np.argmin(dens[top:hi + 1]))
            for m in range(hi, top, -1):
                if dens[m] > dens[m - 1] or dens[m] > dens[m + 1]:
                    continue
                if dens[m] <= 0.5 * float(dens[top:m].max()) and dens[m] <= 0.5 * float(dens[m:q + 1].max()):
                    v = m
                    break
            thin = max(light, key=mass)
            return float(np.exp(grid[v])), float(np.exp(grid[thin]))
    return 0.0, float(np.exp(grid[p]))


def _light_stats(g, tau: float, door: float) -> dict:
    """Shape statistics of the light lines: length shares that are long straight runs,
    curves, and loose pieces."""
    tot = straight = curved = loose = 0.0
    for b in g.branches:
        if b.width >= tau or b.length < max(4.0, 2.0 * b.width):
            continue
        tot += b.length
        st = _straight(b)
        if not st:
            curved += b.length
        elif b.length >= 1.5 * door:
            straight += b.length
        if b.a_free and b.b_free:
            loose += b.length
    tot = max(tot, 1e-9)
    return {"length": tot, "straight": straight / tot, "curved": curved / tot, "loose": loose / tot}


def _straight(b) -> bool:
    """One straight run, or a few joined at right angles."""
    poly = cv2.approxPolyDP(b.pts.astype(np.float32).reshape(-1, 1, 2), max(1.5, 0.5 * b.width), False).reshape(-1, 2)
    seg = np.diff(poly, axis=0)
    if len(seg) == 0 or len(seg) > 3:
        return False
    ang = np.degrees(np.arctan2(seg[:, 1], seg[:, 0]))
    turns = [abs((a1 - a0 + 180) % 360 - 180) for a0, a1 in zip(ang[:-1], ang[1:])]
    return all(t < 15 or abs(t - 90) < 15 for t in turns)


def _is_arrow(b, dt: np.ndarray, wall_w: float) -> bool:
    """A line ending in a filled triangle: callout and leader arrows."""
    if b.length < 4 * wall_w:
        return False
    for at_a, free in ((True, b.a_free), (False, b.b_free)):
        if not free:
            continue
        pts = b.pts if at_a else b.pts[::-1]
        xs = np.clip(pts[:, 0].astype(int), 0, dt.shape[1] - 1)
        ys = np.clip(pts[:, 1].astype(int), 0, dt.shape[0] - 1)
        r = dt[ys, xs]
        dist = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        shaft = float(np.median(r[dist > dist[-1] * 0.4])) if (dist > dist[-1] * 0.4).any() else 0.0
        if shaft <= 0 or 2 * shaft > 1.3 * wall_w:
            continue
        head = r[dist <= max(6.0 * shaft, 4.0)]
        if head.size and head.max() >= 1.9 * shaft and head.max() >= shaft + 1.5:
            return True
    return False


def _coverage(base: np.ndarray, solid: np.ndarray, contrast: np.ndarray) -> np.ndarray:
    """Fractional ink coverage: 1 for solid ink, partial for anti-aliased edge pixels."""
    vals = contrast[base]
    full = float(np.percentile(vals, 75)) if vals.size else 1.0
    full = max(full, 0.2)
    cov = np.clip(contrast / full, 0.0, 1.0).astype(np.float32)
    cov[solid & ~base] = 1.0  # filled wall interiors
    cov[~solid] = 0.0
    return cov


def _drop_specks(mask: np.ndarray, min_area: int) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    return keep[lab]


def _free_components(mask: np.ndarray):
    """Label non-ink regions (4-connected) with area, inscribed radius and border contact."""
    free = (~mask).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    dt = cv2.distanceTransform(free, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    idx = np.arange(n)
    rmax = np.asarray(ndi.maximum(dt, lab, idx))
    x, y, bw, bh, area = (stats[:, i] for i in range(5))
    H, W = mask.shape
    border = (x == 0) | (y == 0) | (x + bw >= W) | (y + bh >= H)
    return n, lab, area, rmax, border, dt


def _solidify(mask: np.ndarray, text_h: float):
    """Fill wall interiors drawn as double lines or hatching. Returns (mask, double_line)."""
    H, W = mask.shape
    n, lab, area, rmax, border, _ = _free_components(mask)
    if n <= 1:
        return mask, False
    area = area.astype(np.float64)
    elong = area / np.maximum(4.0 * rmax ** 2, 1.0)
    ys, xs = np.nonzero(mask)
    span = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min())) if xs.size else float(np.hypot(H, W))
    # Wall interiors of double-line walls: thin channels that run a long way (they follow
    # the wall network), unlike stair treads or furniture which are thin but short.
    r0 = max(3.0, 0.012 * min(H, W))
    if text_h:
        # Wall thickness is on the order of the label text height; rooms are much wider.
        r0 = min(r0, max(2.5, 0.45 * text_h))
    length = area / np.maximum(2.0 * rmax, 1.0)
    chan = (~border) & (rmax <= r0) & (rmax >= 0.9) & (length >= 0.12 * span)
    chan[0] = False
    double_line = bool(chan.any() and area[chan].sum() >= 0.05 * float(mask.sum()))
    fill = np.zeros(n, bool)
    r_fill = 0.0
    if double_line:
        r_fill = 1.3 * float(_weighted_percentile(rmax[chan], area[chan], 70))
        cand = (~border) & (rmax <= r_fill) & (elong >= 2.0)
        cand[0] = False
        # Long channels are the wall network itself, never a stack of treads.
        short = cand & (elong < 8.0) & (length < 0.12 * span)
        fill |= cand & ~(short & _stacked(lab, short, rmax, area))
    # The gaps between hatching lines, and other slivers and specks too narrow to be
    # anything but the inside of a wall. (Stair treads and furniture leave wider cells.)
    fill |= (~border) & (rmax <= 2.0)
    r_tiny = max(2.0, 0.12 * text_h) if text_h else 2.5
    fill |= (~border) & (rmax <= r_tiny) & (elong <= 3.5)
    fill |= (~border) & (area <= 12)
    fill[0] = False
    return mask | fill[lab], double_line


def _stacked(lab: np.ndarray, cand: np.ndarray, rmax: np.ndarray, area: np.ndarray, gap: int = 3) -> np.ndarray:
    """Candidate channels that sit in a stack of similar channels a line's width apart:
    the gaps between stair treads (or a hatched band), not a wall's inside."""
    out = np.zeros(len(cand), bool)
    ids = np.nonzero(cand)[0]
    if ids.size < 3:
        return out
    clab = np.where(cand[lab], lab, 0)
    k = np.ones((2 * gap + 1, 2 * gap + 1), np.uint8)
    objs = ndi.find_objects(clab)
    H, W = lab.shape
    for i in ids:
        sl = objs[i - 1] if i - 1 < len(objs) else None
        if sl is None:
            continue
        y0, y1 = max(0, sl[0].start - gap), min(H, sl[0].stop + gap)
        x0, x1 = max(0, sl[1].start - gap), min(W, sl[1].stop + gap)
        sub = clab[y0:y1, x0:x1]
        ring = cv2.dilate((sub == i).astype(np.uint8), k).astype(bool)
        nb = np.unique(sub[ring])
        nb = nb[(nb > 0) & (nb != i)]
        similar = [j for j in nb if 0.67 <= rmax[j] / max(rmax[i], 1e-6) <= 1.5
                   and 0.5 <= area[j] / max(area[i], 1.0) <= 2.0]
        if len(similar) >= 2:
            out[i] = True
    return out


def _weighted_percentile(values, weights, q):
    order = np.argsort(values)
    v, wt = values[order], weights[order].astype(np.float64)
    c = np.cumsum(wt)
    if c[-1] <= 0:
        return float(np.percentile(values, q))
    return float(v[np.searchsorted(c, c[-1] * q / 100.0)])


def stroke_width_map(mask: np.ndarray):
    """Skeleton of the mask and the stroke width at each skeleton pixel."""
    sk = skel.skeletonize(mask)
    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    width = np.where(sk, 2.0 * dt - 1.0, 0.0)
    return sk, np.maximum(width, 0.0), dt


def _stroke_widths(mask: np.ndarray):
    """Typical wall thickness and thin-line width, weighted by skeleton length on big structures."""
    sk, width, _ = stroke_width_map(mask)
    if not sk.any():
        return 3.0, 1.0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    diag = np.hypot(stats[:, 2], stats[:, 3])
    diag[0] = 0
    big = diag >= 0.25 * diag.max()
    sel = sk & big[lab]
    vals = width[sel] if sel.any() else width[sk]
    vals = vals[vals > 0]
    if vals.size == 0:
        return 3.0, 1.0
    # Walls dominate the big structures' skeleton length; thin marks pull the lower tail.
    wall = float(np.percentile(vals, 65))
    thin = float(np.percentile(vals, 15))
    return max(wall, 1.0), max(thin, 1.0)


def _remove_clutter(mask: np.ndarray, width: float, text_h: float):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask, np.zeros_like(mask)
    diag = np.hypot(stats[:, 2], stats[:, 3]).astype(np.float64)
    diag[0] = 0
    main_diag = diag.max()
    e_small = max(3.0 * (text_h or 10.0), 0.04 * main_diag, 6.0 * width)
    big = diag >= e_small
    big[0] = False
    keep = big.copy()
    # Thick pieces close to the kept structure (wall piers between doors, short stubs).
    small = np.nonzero(~big)[0]
    small = small[small > 0]
    if small.size:
        dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
        maxdt = np.asarray(ndi.maximum(dt, lab, small))
        thick = (2 * maxdt - 1) >= 0.8 * width
        if thick.any():
            near_dist = cv2.distanceTransform((~big[lab]).astype(np.uint8), cv2.DIST_L2, 3)
            mind = np.asarray(ndi.minimum(near_dist, lab, small))
            reach = max(4.0 * width, 1.2 * (text_h or 10.0))
            ok = thick & (mind <= reach)
            keep[small[ok]] = True
    kept = keep[lab]
    return kept, mask & ~kept


def _main_plan(mask: np.ndarray, width: float):
    """Keep the cluster of wall components that forms the plan; drop page frames and
    anything far from the building (legends, title blocks, north arrows)."""
    H, W = mask.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask, (0, 0, W, H)
    for i in range(1, n):
        x, y, bw, bh = (int(v) for v in stats[i, :4])
        if bw < 0.8 * W or bh < 0.8 * H:
            continue
        # A page frame: nearly the whole page, with most pixels hugging its bbox edges.
        sub = lab[y:y + bh, x:x + bw] == i
        band = max(3, int(0.04 * min(bw, bh)))
        edge = np.zeros_like(sub)
        edge[:band] = edge[-band:] = True
        edge[:, :band] = edge[:, -band:] = True
        if (sub & edge).sum() > 0.6 * sub.sum():
            region = mask[y:y + bh, x:x + bw]
            frame = sub & edge
            region[frame] = False
            # Lines attached to the frame from the inside (title block, legend boxes)
            # belong to the sheet, not the building.
            touch = cv2.dilate(frame.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
            n2, lab2 = cv2.connectedComponents((region & sub).astype(np.uint8), connectivity=8)
            hit = np.unique(lab2[touch & (lab2 > 0)])
            if hit.size:
                region[np.isin(lab2, hit)] = False
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask, (0, 0, W, H)
    bw, bh, area = stats[:, 2].astype(np.float64), stats[:, 3].astype(np.float64), stats[:, 4].astype(np.float64)
    score = area / max(width, 1.0)  # ~ skeleton length
    score[0] = 0
    main = int(np.argmax(score))
    # Group components that sit close together; the plan is the group holding the main one.
    gap = int(max(3 * width, 0.05 * float(np.hypot(bw[main], bh[main]))))
    f = 2 if max(H, W) > 1500 else 1
    m = mask.astype(np.uint8)
    if f > 1:
        m = cv2.resize(m, (W // f, H // f), interpolation=cv2.INTER_NEAREST)
    r = max(1, gap // f)
    grouped = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    if f > 1:
        grouped = cv2.resize(grouped, (W, H), interpolation=cv2.INTER_NEAREST)
    _, glab = cv2.connectedComponents(grouped, connectivity=8)
    ys, xs = np.nonzero(lab == main)
    out = mask & (glab == glab[ys[0], xs[0]])
    # Dimension lines: long, thin, separate components lying outside the building's extent.
    mx0, my0, mw, mh = (float(v) for v in stats[main, :4])
    for i in range(1, n):
        if i == main:
            continue
        x, y, bw_i, bh_i = (float(v) for v in stats[i, :4])
        if max(bw_i, bh_i) < 10 * max(1.0, min(bw_i, bh_i)):
            continue
        inside_x = x >= mx0 + 0.02 * mw and x + bw_i <= mx0 + 0.98 * mw
        inside_y = y >= my0 + 0.02 * mh and y + bh_i <= my0 + 0.98 * mh
        if not (inside_x and inside_y):
            out &= lab != i
    ys, xs = np.nonzero(out)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if xs.size else (0, 0, W, H)
    return out, bbox
