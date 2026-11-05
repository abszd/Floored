"""Where the building drawings are on a sheet.

A sheet holds more than the plan: a border frame, a title block, the building's
name in big letters, a legend, a key plan, a north arrow, and sometimes several
floors side by side. The linework is cut into groups (components that sit close
together) and each group is judged:

* sheet furniture: frames drawn around the drawing, tables (title blocks,
  legends, schedules: boxes split by lines that run their full width) and
  text-filled boxes are never the building;
* the plan: the group with the most linework that is not furniture, plus the
  groups comparable to it (wings drawn apart);
* separate floors: comparable groups that the text says are on different floors
  (their titles or room numbers), or that repeat the same outline.

Big text (titles) is removed before any of this, so a building name printed in
heavy letters is never mistaken for the building.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy import ndimage as ndi


@dataclass
class Group:
    id: int
    bbox: tuple  # x0, y0, x1, y1 (exclusive)
    length: float  # length of its lines, px
    fill: float  # area its lines enclose, px²
    weight: float  # heavy stroke half-width (90th percentile of the distance transform)
    table: float = 0.0  # share of its inner lines that run its full width or height
    text: float = 0.0  # share of its box covered by text
    furniture: bool = False

    @property
    def diag(self) -> float:
        return float(np.hypot(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]))


@dataclass
class Layout:
    labels: np.ndarray  # int32 group label per pixel of the dilated grouping (0: none)
    groups: dict  # id -> Group
    main: int  # id of the main group (0 when there is no linework)
    comparable: list = field(default_factory=list)  # ids of groups comparable to the main one (main first)


def remove_frames(mask: np.ndarray) -> np.ndarray:
    """Drop page and viewport frames: rectangles around the drawing whose pixels hug
    their own box, with the lines attached to them from the inside (title blocks,
    legend boxes). A frame must enclose other linework that is not attached to it, so
    a rectangular building (its walls attached to the outside wall) is never taken
    for one."""
    mask = mask.copy()
    H, W = mask.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 2:
        return mask
    order = np.argsort(-stats[1:, cv2.CC_STAT_AREA]) + 1
    for i in order[:12]:
        x, y, bw, bh = (int(v) for v in stats[i, :4])
        if bw < 0.3 * W or bh < 0.3 * H:
            continue
        sub = lab[y:y + bh, x:x + bw] == i
        band = max(3, int(0.04 * min(bw, bh)))
        edge = np.zeros_like(sub)
        edge[:band] = edge[-band:] = True
        edge[:, :band] = edge[:, -band:] = True
        on_edge = (sub & edge).sum()
        # All four sides drawn, most of its pixels on them.
        sides = [sub[:band].any(axis=0).mean(), sub[-band:].any(axis=0).mean(),
                 sub[:, :band].any(axis=1).mean(), sub[:, -band:].any(axis=1).mean()]
        if on_edge <= 0.6 * sub.sum() or min(sides) < 0.9:
            continue
        # Something else inside it (the drawing it frames), kept clear of the frame on
        # every side. A building's inner walls run up to its outer wall instead.
        inner = stats[1:, :4]
        inside = ((inner[:, 0] > x + band) & (inner[:, 1] > y + band)
                  & (inner[:, 0] + inner[:, 2] < x + bw - band) & (inner[:, 1] + inner[:, 3] < y + bh - band))
        inside[i - 1] = False
        enclosed = stats[1:, cv2.CC_STAT_AREA][inside].sum()
        if enclosed < 0.5 * (sub.sum() - on_edge) or enclosed < 0.05 * sub.sum():
            continue
        # Lines attached to the frame from the inside (a title block) may come close;
        # what is not attached must keep a margin.
        ib = inner[inside]
        margin = min(ib[:, 0].min() - x, ib[:, 1].min() - y,
                     x + bw - (ib[:, 0] + ib[:, 2]).max(), y + bh - (ib[:, 1] + ib[:, 3]).max())
        need = max(6, int(0.025 * min(bw, bh)))
        if margin < need:
            continue
        # What it frames: the biggest cluster of linework inside (leftover title-block
        # text and stray marks aside), and it must be a drawing, not a stray mark.
        ids = np.nonzero(inside)[0] + 1
        lab_sub = lab[y:y + bh, x:x + bw]
        enc = np.isin(lab_sub, ids)
        ker = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * need + 1, 2 * need + 1))
        # Clusters do not reach across the frame's own lines (into title-block cells).
        walls_of = cv2.dilate(sub.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        grown = cv2.dilate(enc.astype(np.uint8), ker).astype(bool) & ~walls_of
        _, cl = cv2.connectedComponents((grown | enc).astype(np.uint8), connectivity=8)
        pairs = np.unique(np.stack([lab_sub[enc], cl[enc]]), axis=1)
        lens = {}
        for j, c in pairs.T:
            sx, sy, sw, sh = stats[j, :4]
            lens[c] = lens.get(c, 0.0) + _length(lab[sy:sy + sh, sx:sx + sw] == j)
        best = max(lens, key=lens.get)
        if lens[best] < 0.5 * (bw + bh):
            continue
        cys, cxs = np.nonzero(cl == best)
        dx0, dy0, dx1, dy1 = cxs.min() + need, cys.min() + need, cxs.max() + 1 - need, cys.max() + 1 - need
        # Lines attached to it from the inside stay off to one side of that drawing (a
        # title block, a legend box). A building's inner walls surround what is not
        # attached to its outer wall instead.
        att = sub & ~edge
        total = att.sum()
        if total > 0.02 * on_edge:
            ys_a, xs_a = np.nonzero(att)
            sides_ok = [(xs_a >= dx1).mean(), (xs_a < dx0).mean(), (ys_a >= dy1).mean(), (ys_a < dy0).mean()]
            if max(sides_ok) < 0.97:
                continue
        region = mask[y:y + bh, x:x + bw]
        frame = sub & edge
        region[frame] = False
        touch = cv2.dilate(frame.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        _, lab2 = cv2.connectedComponents((region & sub).astype(np.uint8), connectivity=8)
        hit = np.unique(lab2[touch & (lab2 > 0)])
        if hit.size:
            region[np.isin(lab2, hit)] = False
    return mask


def group(mask: np.ndarray, width: float, text: np.ndarray = None) -> Layout:
    """Cut the linework into groups, measure them and pick the plan among them."""
    H, W = mask.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return Layout(np.zeros((H, W), np.int32), {}, 0)
    area = stats[:, cv2.CC_STAT_AREA].astype(np.float64)
    area[0] = 0
    big = int(np.argmax(area / max(width, 1.0)))
    gap = int(max(3 * width, 0.05 * float(np.hypot(stats[big, 2], stats[big, 3]))))
    f = 2 if max(H, W) > 1500 else 1
    m = mask.astype(np.uint8)
    if f > 1:
        m = cv2.resize(m, (W // f, H // f), interpolation=cv2.INTER_NEAREST)
    r = max(1, gap // f)
    # Dilation by r, done with a distance transform: a big kernel is slow.
    grouped = (cv2.distanceTransform(1 - m, cv2.DIST_L2, 3) <= r).astype(np.uint8)
    if f > 1:
        grouped = cv2.resize(grouped, (W, H), interpolation=cv2.INTER_NEAREST)
    _, glab = cv2.connectedComponents(grouped, connectivity=8)
    gl = np.where(mask, glab, 0)
    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    objs = ndi.find_objects(gl)
    objs_d = ndi.find_objects(glab)
    groups = {}
    for k, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        sub = gl[sl] == k
        # Enclosed area measured on the grouped (dilated) shape, so doorways in the outer
        # wall do not let a building's floor leak out.
        fill = _hole_area(glab[objs_d[k - 1]] == k)
        g = Group(k, (sl[1].start, sl[0].start, sl[1].stop, sl[0].stop), _length(sub), fill,
                  float(np.percentile(dt[sl][sub], 90)))
        g.table = _table_share(sub, g.weight)
        if text is not None:
            g.text = float(text[sl].mean())
        g.furniture = _is_furniture(g, sub)
        groups[k] = g
    lay = Layout(glab.astype(np.int32), groups, 0)
    _pick(lay)
    return lay


def _hole_area(sub: np.ndarray) -> float:
    """Pixels enclosed by ``sub`` (background not reaching the edge of the box)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats((~sub).astype(np.uint8), connectivity=4)
    if n <= 1:
        return 0.0
    x, y, w, h, a = (stats[1:, i] for i in range(5))
    inner = (x > 0) & (y > 0) & (x + w < sub.shape[1]) & (y + h < sub.shape[0])
    return float(a[inner].sum())


def _length(sub: np.ndarray) -> float:
    """Line length from the outlines (half their perimeter), so a solid blob counts its
    size, not its area."""
    cnts, _ = cv2.findContours(sub.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    return 0.5 * float(sum(cv2.arcLength(c, True) for c in cnts))


def _table_share(sub: np.ndarray, weight: float):
    """Share of the inner lines (away from the group's own border) that run straight
    across its whole width or height: rows and columns of a table."""
    h, w = sub.shape
    if h < 8 or w < 8:
        return 0.0
    u8 = sub.astype(np.uint8)
    rows = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(0.9 * w)), 1)))
    cols = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.9 * h)))))
    band = int(max(3, 3 * weight))
    inner = np.zeros_like(sub)
    inner[band:h - band, band:w - band] = True
    lines = sub & inner
    if lines.sum() < 20:
        return 0.0
    full = (rows.astype(bool) | cols.astype(bool)) & inner
    # A table has several rows or columns inside its border; a small building's
    # corridor walls can make two or three.
    n_rows = cv2.connectedComponents((rows.astype(bool) & inner).astype(np.uint8))[0] - 1
    n_cols = cv2.connectedComponents((cols.astype(bool) & inner).astype(np.uint8))[0] - 1
    if n_rows + n_cols < 4:
        return 0.0
    return float(full.sum() / lines.sum())


def _is_furniture(g: Group, sub: np.ndarray) -> bool:
    if g.table >= 0.6:
        return True  # title block, legend or schedule table
    if g.text >= 0.2:
        return True  # a box of text
    return False


def _pick(lay: Layout) -> None:
    cands = [g for g in lay.groups.values() if not g.furniture]
    if not cands:
        cands = list(lay.groups.values())
    if not cands:
        return
    main = max(cands, key=lambda g: g.length)
    lay.main = main.id
    lay.comparable = [main.id] + [g.id for g in cands if g.id != main.id and comparable(g, main)]


def comparable(g: Group, main: Group) -> bool:
    """A group drawn like the main one: comparable linework, enclosing comparable floor
    area, drawn at the same wall weight (another wing, or another floor)."""
    return g.length >= 0.2 * main.length and g.fill >= 0.1 * main.fill and g.weight >= 0.7 * main.weight


def focus_box(focus, shape, extra: float = 0.0) -> tuple:
    """``focus`` (x0, y0, x1, y1) grown by a small margin and clipped to the image."""
    x0, y0, x1, y1 = focus
    pad = 0.03 * float(np.hypot(x1 - x0, y1 - y0)) + extra
    H, W = shape[:2]
    return (max(0, int(x0 - pad)), max(0, int(y0 - pad)),
            min(W, int(np.ceil(x1 + pad))), min(H, int(np.ceil(y1 + pad))))


def focused(lay: Layout, mask: np.ndarray, focus) -> list:
    """Groups inside ``focus`` (x0, y0, x1, y1): the plan the caller picked on the sheet."""
    x0, y0, x1, y1 = focus_box(focus, mask.shape)
    gl = np.where(mask, lay.labels, 0)
    total = np.bincount(gl.ravel(), minlength=lay.labels.max() + 1)
    sub = gl[y0:y1, x0:x1]
    inside = np.bincount(sub.ravel(), minlength=total.size)
    keep = [k for k, g in lay.groups.items() if not g.furniture and total[k] and inside[k] >= 0.6 * total[k]]
    return sorted(keep, key=lambda k: -lay.groups[k].length)


def union_bbox(groups) -> tuple:
    xs0, ys0, xs1, ys1 = zip(*(g.bbox for g in groups))
    return min(xs0), min(ys0), max(xs1), max(ys1)


def shape_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """IoU of two filled outlines scaled to the same box (floors of one building repeat
    their outline; wings rarely do)."""
    ha, wa = a.shape
    hb, wb = b.shape
    ra, rb = wa / max(ha, 1), wb / max(hb, 1)
    if max(ra, rb) / max(min(ra, rb), 1e-6) > 1.25:
        return 0.0
    s = 64
    pa = cv2.resize(a.astype(np.uint8), (s, s), interpolation=cv2.INTER_AREA) > 0
    pb = cv2.resize(b.astype(np.uint8), (s, s), interpolation=cv2.INTER_AREA) > 0
    union = (pa | pb).sum()
    return float((pa & pb).sum() / union) if union else 0.0


# ---------------------------------------------------------------------------
# The plans on a sheet
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    bbox: tuple  # x0, y0, x1, y1 of its linework, in the pixels of the image analysed
    groups: list  # group ids
    floor: dict = None  # what its own text says (floor.floor_from_texts), or None
    confidence: float = 1.0  # that it is a floor of its own (1.0 when the sheet has one plan)


def find(bgr: np.ndarray, ink: np.ndarray, backend, max_text_size: int = 1600, page_text=None):
    """The building drawings on a sheet, in reading order (rows top to bottom, left to
    right within a row): one Plan per floor, wings of one floor kept together.
    ``page_text`` is text already known (a PDF's own, in these pixels): it is used
    instead of reading the sheet when deciding which floor each drawing shows.

    Returns (plans, layout, text items read on the page or [], text boxes found on
    ``bgr`` itself or None when they were found on a smaller copy)."""
    from . import walls

    H, W = ink.shape
    s = min(1.0, max_text_size / max(H, W))
    small = cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else bgr
    try:
        boxes = backend.boxes(small)
    except Exception:  # OCR must never take the conversion down
        boxes = []
    same = boxes if s >= 1 else None
    text = np.zeros((H, W), np.uint8)
    for q in boxes:
        q = np.asarray(q, np.float64) / s
        hq = float(min(np.linalg.norm(q[0] - q[3]), np.linalg.norm(q[0] - q[1])))
        if hq > 0.12 * max(H, W):
            continue
        c = q.mean(axis=0)
        q = c + (q - c) * (1 + 0.3 * hq / max(1.0, np.linalg.norm(q - c, axis=1).mean()))
        cv2.fillPoly(text, [np.round(q).astype(np.int32)], 1)
    text = text.astype(bool)

    base = walls._drop_specks(ink & ~text, 4)
    width0, _ = walls._stroke_widths(base)
    kept, _ = walls._remove_clutter(base, width0, 0.0)
    kept = remove_frames(kept)
    lay = group(kept, width0, text)
    if not lay.groups:
        return [Plan((0, 0, W, H), [])], lay, [], same
    comp = lay.comparable
    if len(comp) == 1:
        return [Plan(lay.groups[comp[0]].bbox, comp)], lay, [], same

    # Several drawings: wings of one floor, or floors? Read the text around each.
    items = list(page_text or [])
    if not items and boxes:
        try:
            items = backend.read(small, boxes)
        except Exception:
            items = []
        for it in items:
            it.quad = np.asarray(it.quad, np.float64) / s
    plans = _split_floors(lay, comp, items)
    plans.sort(key=lambda p: (p.bbox[1], p.bbox[0]))
    rows = []
    for p in plans:  # rows: plans whose vertical extents overlap by half
        for row in rows:
            q = row[0]
            ov = min(p.bbox[3], q.bbox[3]) - max(p.bbox[1], q.bbox[1])
            if ov >= 0.5 * min(p.bbox[3] - p.bbox[1], q.bbox[3] - q.bbox[1]):
                row.append(p)
                break
        else:
            rows.append([p])
    return [p for row in rows for p in sorted(row, key=lambda p: p.bbox[0])], lay, items, same


def _split_floors(lay: Layout, comp: list, items: list) -> list:
    from . import floor as floor_mod

    groups = [lay.groups[k] for k in comp]
    # Text belongs to the drawing it is in or nearest to (titles sit under or over it).
    near = {k: [] for k in comp}
    for it in items:
        c = np.asarray(it.quad, np.float64).reshape(-1, 2).mean(axis=0)
        best, best_d = None, np.inf
        for g in groups:
            x0, y0, x1, y1 = g.bbox
            d = float(np.hypot(max(x0 - c[0], 0, c[0] - x1), max(y0 - c[1], 0, c[1] - y1)))
            if d < best_d:
                best, best_d = g, d
        if best is not None and best_d <= 0.35 * best.diag:
            near[best.id].append(it)
    read = {k: floor_mod.floor_from_texts(near[k]) for k in comp}
    known = {k: f["level"] for k, f in read.items()
             if f is not None and f["level"] is not None and f["confidence"] >= 0.5}

    def plan_of(ks, conf):
        its = [it for k in ks for it in near[k]]
        return Plan(union_bbox([lay.groups[k] for k in ks]), list(ks), floor_mod.floor_from_texts(its), conf)

    if len(set(known.values())) >= 2:
        # The text says different floors: one plan per floor; drawings that say nothing
        # join the nearest one (a wing of it).
        levels = sorted(set(known.values()))
        members = {lv: [k for k in comp if known.get(k) == lv] for lv in levels}
        for k in comp:
            if k in known:
                continue
            lv = min(levels, key=lambda lv: min(_gap(lay.groups[k], lay.groups[j]) for j in members[lv]))
            members[lv].append(k)
        out = []
        for lv in levels:
            conf = float(np.mean([read[k]["confidence"] for k in members[lv] if k in known]))
            out.append(plan_of(members[lv], round(min(0.95, 0.5 + 0.5 * conf), 2)))
        return out
    if len(known) == len(comp):
        return [plan_of(comp, 1.0)]  # all on the same floor: wings
    # Nothing tells them apart: the same outline drawn again is another floor.
    shapes = {k: ndi.binary_fill_holes(lay.labels[_slice(lay.groups[k])] == k) for k in comp}
    sims = [shape_similarity(shapes[a], shapes[b]) for i, a in enumerate(comp) for b in comp[i + 1:]]
    sizes = [lay.groups[k].fill for k in comp]
    if sims and min(sims) >= 0.8 and min(sizes) >= 0.7 * max(sizes):
        return [plan_of([k], 0.55) for k in comp]
    return [plan_of(comp, 1.0)]


def _slice(g: Group):
    x0, y0, x1, y1 = g.bbox
    return slice(y0, y1), slice(x0, x1)


def _gap(a: Group, b: Group) -> float:
    dx = max(a.bbox[0] - b.bbox[2], b.bbox[0] - a.bbox[2], 0)
    dy = max(a.bbox[1] - b.bbox[3], b.bbox[1] - a.bbox[3], 0)
    return float(np.hypot(dx, dy))
