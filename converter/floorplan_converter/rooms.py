"""Room segmentation: enclosed free space, cleaned up into real rooms.

The old pipeline seeded a watershed at every local maximum of the distance
transform, which is why large rooms came out as two or three pieces: a big or
irregular room simply has several maxima. Here a room is a connected region of
free space bounded by walls and closed doorways, and the only operations that
change regions are evidence-driven:

* merge pieces cut off by non-wall lines (furniture outlines, door swings,
  stair treads) when they are too small to be rooms,
* group the strips between stair treads into one stair,
* split a region only at a genuine constriction: a door-width neck that is much
  narrower than the spaces on both sides of it (an opening nobody drew a door
  in). Big rooms and L-shaped rooms have no such neck, so they stay whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.morphology import h_maxima
from skimage.segmentation import watershed


@dataclass
class Segmentation:
    labels: np.ndarray  # int32, 0 = not a room
    footprint: np.ndarray  # bool, building area incl. walls (outdoor areas removed)
    outdoor: np.ndarray  # bool, page-coloured areas enclosed by the drawing (decks, courtyards)
    stairs: set = field(default_factory=set)  # labels found as tread groups
    cuts: list = field(default_factory=list)  # (p, q) segments where a region was split at a neck
    min_area: float = 0.0


def footprint_from_walls(lines: np.ndarray, virtual: np.ndarray, width: float, gap: float) -> np.ndarray:
    """Building area: everything enclosed by the drawing's lines once doorways are closed.

    All kept lines count here (not just heavy walls), so windows and glazing drawn as
    thin lines still close the building. Openings in the outer wall up to ``gap`` px
    wide (an entrance the door closing missed) are bridged by a closing with a square
    element, which keeps right-angle corners square.
    """
    # Only the building's own line work: a title block or legend box drawn near the plan
    # must not be bridged onto it.
    closed = _main_components(lines | virtual, 0.1)
    fp = ndi.binary_fill_holes(closed)
    r = int(max(3, np.ceil(gap / 2.0)))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * r + 1, 2 * r + 1))
    bridged = ndi.binary_fill_holes(cv2.morphologyEx(closed.astype(np.uint8), cv2.MORPH_CLOSE, k).astype(bool))
    fp = _main_components(fp | bridged)
    # Remove spikes left by lines that stick out of the building.
    r = max(1, int(round(width)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    opened = cv2.morphologyEx(fp.astype(np.uint8), cv2.MORPH_OPEN, k).astype(bool)
    return _main_components(opened) if opened.any() else fp


def _main_components(mask: np.ndarray, rel: float = 0.1) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 2:
        return mask.astype(bool)
    area = stats[:, cv2.CC_STAT_AREA].astype(np.float64)
    area[0] = 0
    keep = area >= rel * area.max()
    keep[0] = False
    return keep[lab]


def _hull_area(mask: np.ndarray) -> float:
    pts = cv2.findNonZero(mask.astype(np.uint8))
    if pts is None or len(pts) < 3:
        return 0.0
    return float(cv2.contourArea(cv2.convexHull(pts)))


def label_free_space(footprint: np.ndarray, barrier: np.ndarray, min_px: int) -> np.ndarray:
    free = footprint & ~barrier
    n, lab, stats, _ = cv2.connectedComponentsWithStats(free.astype(np.uint8), connectivity=4)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_px
    keep[0] = False
    remap = np.zeros(n, np.int32)
    remap[keep] = np.arange(1, keep.sum() + 1)
    return remap[lab]


# ---------------------------------------------------------------------------
# Region graph helpers
# ---------------------------------------------------------------------------


def _contacts(labels: np.ndarray, rid: int, sl, reach: int, weak: np.ndarray = None):
    """Neighbouring labels within ``reach`` px of region ``rid``.

    Returns {label: (contact_px, weak_contact_px)}; ``weak`` marks separator pixels
    that are not real walls (thin lines, colour edges, closed doorways).
    """
    H, W = labels.shape
    y0, y1 = max(0, sl[0].start - reach - 1), min(H, sl[0].stop + reach + 1)
    x0, x1 = max(0, sl[1].start - reach - 1), min(W, sl[1].stop + reach + 1)
    sub = labels[y0:y1, x0:x1]
    m = (sub == rid).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * reach + 1, 2 * reach + 1))
    ring = cv2.dilate(m, k).astype(bool) & (sub != rid)
    vals = sub[ring]
    out = {}
    if vals.size == 0:
        return out
    near = np.unique(vals[vals > 0])
    if weak is not None:
        # Separator between rid and the neighbour: the ring pixels that are not labelled.
        sep = ring & (sub == 0)
        wsub = weak[y0:y1, x0:x1]
        weak_frac = float(wsub[sep].mean()) if sep.any() else 1.0
    else:
        weak_frac = 1.0
    for v in near:
        c = int((vals == v).sum())
        out[int(v)] = (c, c * weak_frac)
    return out


def _relabel(labels: np.ndarray) -> np.ndarray:
    ids = np.unique(labels)
    ids = ids[ids > 0]
    lut = np.zeros(int(labels.max()) + 1, np.int32)
    lut[ids] = np.arange(1, len(ids) + 1)
    return lut[labels]


# ---------------------------------------------------------------------------
# Clean-up steps
# ---------------------------------------------------------------------------


def group_stairs(labels: np.ndarray, door: float, wall_w: float) -> tuple:
    """Merge the strips between stair treads into one region. Returns (labels, stair ids)."""
    objs = ndi.find_objects(labels)
    strips = {}
    for rid, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        m = (labels[sl] == rid).astype(np.uint8)
        area = int(m.sum())
        if area < 6:
            continue
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        (cx, cy), (w, h), ang = cv2.minAreaRect(max(cnts, key=cv2.contourArea))
        short, long_ = min(w, h), max(w, h)
        if long_ <= 0 or short > 0.34 * long_ or short > 0.7 * door or long_ > 3.5 * door:
            continue
        if area < 0.6 * w * h:  # strips are full rectangles
            continue
        axis = ang if w >= h else ang + 90.0
        strips[rid] = (sl, short, long_, axis % 180.0)
    if len(strips) < 4:
        return labels, set()
    parent = {r: r for r in strips}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    reach = int(max(2, np.ceil(wall_w)) + 1)
    for rid, (sl, short, long_, axis) in strips.items():
        for nb, (c, _) in _contacts(labels, rid, sl, reach).items():
            if nb not in strips or c < 0.5 * short:
                continue
            _, s2, l2, a2 = strips[nb]
            da = abs(axis - a2)
            da = min(da, 180 - da)
            if da < 12 and 0.7 < long_ / l2 < 1.43:
                parent[find(rid)] = find(nb)
    groups = {}
    for r in strips:
        groups.setdefault(find(r), []).append(r)
    stairs = set()
    out = labels.copy()
    for root, members in groups.items():
        if len(members) < 4:
            continue
        lut_ids = np.array(members)
        out[np.isin(labels, lut_ids)] = root
        stairs.add(root)
    return out, stairs


def merge_small(labels: np.ndarray, min_area: float, wall_w: float, weak: np.ndarray, keep_ids=()) -> np.ndarray:
    """Merge regions smaller than ``min_area`` into the neighbour they share the most
    non-wall boundary with; drop them when only real walls separate them."""
    labels = labels.copy()
    reach = int(max(2, np.ceil(wall_w)) + 1)
    for _ in range(4):
        areas = np.bincount(labels.ravel())
        objs = ndi.find_objects(labels)
        order = [r for r in np.argsort(areas) if r > 0 and areas[r] > 0 and areas[r] < min_area and r not in keep_ids]
        if not order:
            break
        changed = False
        for rid in order:
            sl = objs[rid - 1] if rid - 1 < len(objs) else None
            if sl is None or areas[rid] == 0:
                continue
            cont = _contacts(labels, rid, sl, reach, weak)
            if cont:
                best = max(cont.items(), key=lambda kv: (kv[1][1], kv[1][0]))
                nb, (c, cw) = best
                if cw >= 0.3 * c and c >= 2:
                    labels[labels == rid] = nb
                    areas[nb] += areas[rid]
                    areas[rid] = 0
                    changed = True
                    continue
            if areas[rid] < 0.35 * min_area:
                labels[labels == rid] = 0
                areas[rid] = 0
                changed = True
        if not changed:
            break
    return labels


def merge_enclosed(labels: np.ndarray, max_area: float, wall_w: float, weak: np.ndarray, keep_ids=()) -> np.ndarray:
    """Merge areas fenced off mostly by light linework (a bed or table outline, a
    counter, a door swing) into the room around them. Only small areas qualify, and
    only when they are much smaller than that room."""
    labels = labels.copy()
    reach = int(max(2, np.ceil(wall_w)) + 1)
    areas = np.bincount(labels.ravel())
    objs = ndi.find_objects(labels)
    for rid in [r for r in np.argsort(areas) if r > 0 and 0 < areas[r] <= max_area and r not in keep_ids]:
        sl = objs[rid - 1] if rid - 1 < len(objs) else None
        if sl is None or areas[rid] == 0:
            continue
        cont = _contacts(labels, rid, sl, reach, weak)
        if not cont:
            continue
        nb, (c, cw) = max(cont.items(), key=lambda kv: kv[1][0])
        if c >= 2 and cw >= 0.6 * c and areas[rid] <= 0.35 * areas[nb]:
            labels[labels == rid] = nb
            areas[nb] += areas[rid]
            areas[rid] = 0
    return labels


def fill_region_holes(labels: np.ndarray, barrier: np.ndarray, max_hole: float) -> np.ndarray:
    """Fill small holes inside a room (columns, removed furniture, leftover marks)."""
    out = labels.copy()
    for rid, sl in enumerate(ndi.find_objects(labels), start=1):
        if sl is None:
            continue
        m = labels[sl] == rid
        filled = ndi.binary_fill_holes(m)
        holes = filled & ~m
        if not holes.any():
            continue
        hl, n = ndi.label(holes)
        sizes = np.bincount(hl.ravel())
        sub_lab = labels[sl]
        for h in range(1, n + 1):
            if sizes[h] > max_hole:
                continue
            hm = hl == h
            if (sub_lab[hm] > 0).any() and (sub_lab[hm] != rid).any():
                continue  # another room sits inside: keep it
            out[sl][hm] = rid
    return out


def close_thin_separators(labels: np.ndarray, barrier: np.ndarray, wall_w: float) -> np.ndarray:
    """After merges, absorb the thin lines that separated the merged pieces."""
    out = labels.copy()
    r = int(max(1, np.ceil(0.6 * wall_w)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    for rid, sl in enumerate(ndi.find_objects(labels), start=1):
        if sl is None:
            continue
        m = (labels[sl] == rid).astype(np.uint8)
        closed = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k).astype(bool)
        add = closed & (labels[sl] == 0) & barrier[sl]
        out[sl][add] = rid
    return out


def split_necks(labels: np.ndarray, min_area: float, door: float, ratio: float, max_neck: float) -> tuple:
    """Split regions at door-like constrictions much narrower than both sides.

    Returns (labels, cuts) where cuts are (p, q) endpoints of the cut lines.
    """
    out = labels.copy()
    next_id = int(labels.max()) + 1
    cuts = []
    h = max(1.0, 0.18 * door)
    for rid, sl in enumerate(ndi.find_objects(labels), start=1):
        if sl is None:
            continue
        m = labels[sl] == rid
        area = int(m.sum())
        if area < 2.2 * min_area:
            continue
        mp = np.pad(m, 1)
        dist = ndi.distance_transform_edt(mp)
        peaks = h_maxima(dist, h)
        mk, nmk = ndi.label(peaks)
        if nmk < 2:
            continue
        ws = watershed(-dist, mk, mask=mp)
        # Peak (inscribed radius) of each basin, and the pass height between basins.
        peak = np.asarray(ndi.maximum(dist, ws, np.arange(nmk + 1)))
        passes = {}
        for a_sl, b_sl in (((slice(None), slice(None, -1)), (slice(None), slice(1, None))),
                           ((slice(None, -1), slice(None)), (slice(1, None), slice(None)))):
            la, lb = ws[a_sl], ws[b_sl]
            diff = (la != lb) & (la > 0) & (lb > 0)
            if not diff.any():
                continue
            da, db = dist[a_sl][diff], dist[b_sl][diff]
            va, vb = la[diff], lb[diff]
            hgt = np.minimum(da, db)
            for x, y, hh in zip(np.minimum(va, vb), np.maximum(va, vb), hgt):
                key = (int(x), int(y))
                if hh > passes.get(key, -1):
                    passes[key] = float(hh)
        if not passes:
            continue
        parent = list(range(nmk + 1))
        gpeak = peak.astype(float).copy()
        garea = np.bincount(ws.ravel(), minlength=nmk + 1).astype(float)

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for (a, b), s in sorted(passes.items(), key=lambda kv: -kv[1]):
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            neck = 2 * s
            small_side = min(gpeak[ra], gpeak[rb])
            is_neck = (neck <= max_neck and s <= ratio * small_side
                       and min(garea[ra], garea[rb]) >= 0.8 * min_area)
            if is_neck:
                continue
            parent[ra] = rb
            gpeak[rb] = max(gpeak[ra], gpeak[rb])
            garea[rb] += garea[ra]
        roots = sorted({find(i) for i in range(1, nmk + 1)})
        if len(roots) < 2:
            continue
        group = np.array([find(i) if i > 0 else 0 for i in range(nmk + 1)])
        gl = group[ws][1:-1, 1:-1]
        # Keep the largest part on the original id; give the others new ids.
        sizes = {r: int((gl == r).sum()) for r in roots}
        keep_root = max(sizes, key=sizes.get)
        sub = out[sl]
        for r in roots:
            sel = (gl == r) & m
            if r == keep_root:
                continue
            sub[sel] = next_id
            next_id += 1
        # Record the cut lines (boundary pixels between groups).
        bnd = np.zeros_like(m)
        bnd[:, 1:] |= (gl[:, 1:] != gl[:, :-1]) & m[:, 1:] & m[:, :-1]
        bnd[1:, :] |= (gl[1:, :] != gl[:-1, :]) & m[1:, :] & m[:-1, :]
        if bnd.any():
            bl, nb = ndi.label(bnd, structure=np.ones((3, 3)))
            for i in range(1, nb + 1):
                ys, xs = np.nonzero(bl == i)
                pts = np.stack([xs + sl[1].start, ys + sl[0].start], axis=1).astype(float)
                c = pts.mean(axis=0)
                u, s_, vt = np.linalg.svd(pts - c, full_matrices=False)
                d = vt[0]
                proj = (pts - c) @ d
                cuts.append((c + d * proj.min(), c + d * proj.max()))
            sub[bnd & (sub > 0)] = 0  # leave a hairline between the two parts
    return out, cuts
