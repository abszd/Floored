"""Synthetic floorplans with known ground truth.

Real floorplans come in many drawing conventions, and the converter has to cope
with all of them. This module draws random but plausible buildings in several
of those conventions and keeps the true room layout, so the converter can be
measured objectively (see ``bench/benchmark.py``) and regression-tested.

Every plan is generated from a seed, so results are reproducible::

    plan = generate(seed=3, style="solid")
    cv2.imwrite("plan.png", plan.image)
    plan.labels   # int32 array, 0 = not a room, -1 = ignore (door openings), k = room k

Styles
------
``solid``      Real-estate style: solid black walls, door swings, furniture, area labels.
``cad``        Architectural CAD export: walls as double thin lines, dimension strings,
               door swings, furniture, title block and page frame.
``hatched``    CAD with hatched wall poche.
``gray_fill``  Campus wayfinding (the Memorial Union style): gray rooms, lighter corridors,
               dark walls, colored icon badges, callouts with leader arrows outside.
``color_fill`` Wayfinding map with a pastel color per room type and thin walls.
``blueprint``  White linework on a blue background.
``scan``       Solid walls on a photocopied or scanned page: tilt, blur, noise, shading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

STYLES = ("solid", "cad", "hatched", "gray_fill", "color_fill", "blueprint", "scan")

ROOM_NAMES = {
    "room": ["OFFICE", "Office", "CONFERENCE", "Meeting", "Lab", "STORAGE", "Bedroom", "Living",
             "Kitchen", "Study", "Classroom", "Studio", "Lounge", "Reading Room", "Copy", "Dining"],
    "corridor": ["CORRIDOR", "Hall", "Corridor"],
    "stair": ["STAIR", "Stairs", "STAIR B"],
    "elevator": ["ELEV", "Elevator", "LIFT"],
    "restroom": ["WOMEN", "MEN", "Restroom", "WC", "Toilet"],
}


@dataclass
class SynthRoom:
    id: int
    kind: str
    name: str
    bbox: tuple  # x0, y0, x1, y1 in pixels of the final image


@dataclass
class SynthPlan:
    image: np.ndarray  # BGR uint8
    labels: np.ndarray  # int32: 0 outside / walls, -1 ignore, k > 0 room id
    rooms: list
    footprint: np.ndarray  # uint8 mask of the building including exterior walls
    style: str
    px_per_m: float
    wall_px: float
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class _Layout:
    """Rooms as unions of axis-aligned rectangles on an integer pixel grid."""

    def __init__(self, rng, ppm):
        self.rng = rng
        self.ppm = ppm
        self.rects = []  # (x0, y0, x1, y1, room_index)
        self.kinds = []

    def add_room(self, kind):
        self.kinds.append(kind)
        return len(self.kinds) - 1

    def add_rect(self, rect, room):
        x0, y0, x1, y1 = (int(round(v)) for v in rect)
        if x1 - x0 > 2 and y1 - y0 > 2:
            self.rects.append((x0, y0, x1, y1, room))


def _split_band(rng, lay, x0, y0, x1, y1, ppm, horizontal, allow_big=True):
    """Fill a band next to a corridor with a row of rooms (some subdivided)."""
    length = (x1 - x0) if horizontal else (y1 - y0)
    depth = (y1 - y0) if horizontal else (x1 - x0)
    pos = 0.0
    while pos < length - 1:
        remaining = length - pos
        if allow_big and rng.random() < 0.12 and remaining > 12 * ppm:
            w = rng.uniform(10, 18) * ppm  # a big hall
        else:
            w = rng.uniform(3.0, 7.5) * ppm
        if remaining - w < 2.8 * ppm:
            w = remaining
        a, b = pos, min(length, pos + w)
        if horizontal:
            cell = (x0 + a, y0, x0 + b, y1)
        else:
            cell = (x0, y0 + a, x1, y0 + b)
        _fill_cell(rng, lay, cell, ppm, depth, w)
        pos = b


def _fill_cell(rng, lay, cell, ppm, depth, width):
    x0, y0, x1, y1 = cell
    w, h = x1 - x0, y1 - y0
    r = rng.random()
    if depth > 8 * ppm and width < 9 * ppm and r < 0.45:
        # Two rooms back to back.
        if w > h:
            m = x0 + w * rng.uniform(0.4, 0.6)
            lay.add_rect((x0, y0, m, y1), lay.add_room("room"))
            lay.add_rect((m, y0, x1, y1), lay.add_room("room"))
        else:
            m = y0 + h * rng.uniform(0.4, 0.6)
            lay.add_rect((x0, y0, x1, m), lay.add_room("room"))
            lay.add_rect((x0, m, x1, y1), lay.add_room("room"))
        return
    if width < 6 * ppm and r < 0.6 and min(w, h) > 2.2 * ppm and max(w, h) > 4.5 * ppm:
        kind = rng.choice(["stair", "restroom", "elevator", "room"], p=[0.3, 0.35, 0.15, 0.2])
        if kind == "elevator":
            # Elevator shaft next to a small storage room.
            s = min(w, h, 2.4 * ppm)
            if w > h:
                lay.add_rect((x0, y0, x0 + s, y0 + s), lay.add_room("elevator"))
                lay.add_rect((x0, y0 + s, x0 + s, y1), lay.add_room("room"))
                lay.add_rect((x0 + s, y0, x1, y1), lay.add_room("room"))
            else:
                lay.add_rect((x0, y0, x0 + s, y0 + s), lay.add_room("elevator"))
                lay.add_rect((x0 + s, y0, x1, y0 + s), lay.add_room("room"))
                lay.add_rect((x0, y0 + s, x1, y1), lay.add_room("room"))
            return
        lay.add_rect(cell, lay.add_room(kind))
        return
    lay.add_rect(cell, lay.add_room("room"))


def _make_layout(rng, ppm):
    lay = _Layout(rng, ppm)
    W = rng.uniform(22, 48) * ppm
    H = rng.uniform(14, 26) * ppm
    corridor_w = rng.uniform(1.8, 3.0) * ppm
    # Main block: rooms / corridor / rooms, corridor along the long axis.
    horizontal = True
    cy = H * rng.uniform(0.38, 0.62)
    corr = lay.add_room("corridor")
    lay.add_rect((0, cy - corridor_w / 2, W, cy + corridor_w / 2), corr)
    _split_band(rng, lay, 0, 0, W, cy - corridor_w / 2, ppm, horizontal)
    _split_band(rng, lay, 0, cy + corridor_w / 2, W, H, ppm, horizontal)
    footprint_rects = [(0, 0, W, H)]

    shape = rng.choice(["rect", "L", "T", "wing"], p=[0.3, 0.3, 0.2, 0.2])
    if shape != "rect":
        # A wing hanging off the main block with its own corridor that joins the main one.
        ww = rng.uniform(9, 16) * ppm
        wh = rng.uniform(8, 18) * ppm
        if shape == "L":
            wx = 0 if rng.random() < 0.5 else W - ww
        elif shape == "T":
            wx = W / 2 - ww / 2
        else:
            wx = rng.uniform(0.15, 0.6) * W
        top = rng.random() < 0.5
        wy0, wy1 = (-wh, 0) if top else (H, H + wh)
        wc = wx + ww * rng.uniform(0.35, 0.65)
        # The wing corridor extends the main corridor's room.
        lay.add_rect((wc - corridor_w / 2, wy0, wc + corridor_w / 2, wy1), corr)
        if rng.random() < 0.35:
            lay.add_rect((wx, wy0, wc - corridor_w / 2, wy1), lay.add_room("room"))  # big wing hall
        else:
            _split_band(rng, lay, wx, wy0, wc - corridor_w / 2, wy1, ppm, False, allow_big=False)
        _split_band(rng, lay, wc + corridor_w / 2, wy0, wx + ww, wy1, ppm, False, allow_big=False)
        footprint_rects.append((wx, wy0, wx + ww, wy1))
        # The wing corridor has to cut through the main block's rooms to reach the corridor.
        if top:
            lay.add_rect((wc - corridor_w / 2, 0, wc + corridor_w / 2, cy), corr)
        else:
            lay.add_rect((wc - corridor_w / 2, cy, wc + corridor_w / 2, H), corr)
    return lay, footprint_rects


def _merge_big_rooms(rng, labels, kinds, n_merge):
    """Merge a few pairs of adjacent ordinary rooms into one bigger (often L-shaped) room."""
    for _ in range(n_merge):
        ids = [i for i, k in enumerate(kinds) if k == "room" and np.any(labels == i + 1)]
        if len(ids) < 4:
            return
        a = int(rng.choice(ids)) + 1
        ma = labels == a
        ring = cv2.dilate(ma.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~ma
        neigh = [int(v) for v in np.unique(labels[ring]) if v > 0 and kinds[int(v) - 1] == "room"]
        if not neigh:
            continue
        b = int(rng.choice(neigh))
        labels[labels == b] = a


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _shared_runs(labels, a, b):
    """Straight runs of boundary between rooms a and b: list of (orientation, fixed, start, end)."""
    runs = []
    # Vertical boundaries: a at (y, x), b at (y, x+1) or the reverse.
    left, right = labels[:, :-1], labels[:, 1:]
    m = ((left == a) & (right == b)) | ((left == b) & (right == a))
    for x in np.unique(np.nonzero(m)[1]):
        ys = np.nonzero(m[:, x])[0]
        runs += [("v", int(x) + 0.5, s, e) for s, e in _contiguous(ys)]
    top, bottom = labels[:-1, :], labels[1:, :]
    m = ((top == a) & (bottom == b)) | ((top == b) & (bottom == a))
    for y in np.unique(np.nonzero(m)[0]):
        xs = np.nonzero(m[y, :])[0]
        runs += [("h", int(y) + 0.5, s, e) for s, e in _contiguous(xs)]
    return runs


def _contiguous(vals):
    if len(vals) == 0:
        return []
    out = []
    start = prev = int(vals[0])
    for v in vals[1:]:
        v = int(v)
        if v != prev + 1:
            out.append((start, prev))
            start = v
        prev = v
    out.append((start, prev))
    return out


def _put_text(img, text, center, scale, color, thickness=1, font=cv2.FONT_HERSHEY_SIMPLEX):
    (tw, th), base = cv2.getTextSize(text, font, scale, thickness)
    org = (int(center[0] - tw / 2), int(center[1] + th / 2))
    cv2.putText(img, text, org, font, scale, color, thickness, cv2.LINE_AA)
    return (org[0], org[1] - th, org[0] + tw, org[1] + base)


def _draw_furniture(rng, img, interior, rect, ppm, color, thick, kind):
    """Draw furniture inside a room interior rectangle (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    if w < 2 * ppm or h < 2 * ppm:
        return
    lt = cv2.LINE_AA
    if kind == "restroom":
        # Toilet stalls along one wall: partitions touching the wall + toilet bowls.
        n = int(max(1, min(4, w // (1.0 * ppm))))
        sw = w / max(n, 1)
        for i in range(n):
            cx = int(x0 + sw * (i + 0.5))
            cv2.ellipse(img, (cx, int(y0 + 0.45 * ppm)), (int(0.18 * ppm), int(0.28 * ppm)), 0, 0, 360, color, thick, lt)
            cv2.rectangle(img, (cx - int(0.2 * ppm), int(y0)), (cx + int(0.2 * ppm), int(y0 + 0.15 * ppm)), color, thick, lt)
            if i:
                xx = int(x0 + sw * i)
                cv2.line(img, (xx, int(y0)), (xx, int(y0 + 1.4 * ppm)), color, thick, lt)
        return
    if kind != "room":
        return
    r = rng.random()
    if r < 0.3 and w > 3 * ppm and h > 3 * ppm:
        # Table with chairs in the middle.
        tw, th = min(w * 0.5, 2.4 * ppm), min(h * 0.3, 1.0 * ppm)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        cv2.rectangle(img, (int(cx - tw / 2), int(cy - th / 2)), (int(cx + tw / 2), int(cy + th / 2)), color, thick, lt)
        for k in range(int(tw // (0.7 * ppm))):
            px = int(cx - tw / 2 + (k + 0.5) * 0.7 * ppm)
            for sgn in (-1, 1):
                py = int(cy + sgn * (th / 2 + 0.3 * ppm))
                cv2.rectangle(img, (px - int(0.2 * ppm), py - int(0.15 * ppm)), (px + int(0.2 * ppm), py + int(0.15 * ppm)), color, thick, lt)
    elif r < 0.55:
        # Desk or counter against a wall (touches the wall).
        d = 0.65 * ppm
        side = rng.integers(4)
        if side == 0:
            cv2.rectangle(img, (int(x0), int(y0)), (int(x0 + w * 0.6), int(y0 + d)), color, thick, lt)
        elif side == 1:
            cv2.rectangle(img, (int(x1 - d), int(y0)), (int(x1), int(y0 + h * 0.6)), color, thick, lt)
        elif side == 2:
            cv2.rectangle(img, (int(x0), int(y1 - d)), (int(x0 + w * 0.7), int(y1)), color, thick, lt)
        else:
            # L-shaped kitchen counter in a corner.
            cv2.rectangle(img, (int(x0), int(y0)), (int(x0 + w * 0.6), int(y0 + d)), color, thick, lt)
            cv2.rectangle(img, (int(x0), int(y0)), (int(x0 + d), int(y0 + h * 0.6)), color, thick, lt)
            cv2.circle(img, (int(x0 + w * 0.3), int(y0 + d / 2)), int(d * 0.3), color, thick, lt)
    elif r < 0.75 and w > 2.6 * ppm and h > 2.6 * ppm:
        # Bed with pillows.
        bw, bh = 1.6 * ppm, 2.0 * ppm
        bx = x0 + rng.uniform(0.1, 0.5) * (w - bw)
        cv2.rectangle(img, (int(bx), int(y0)), (int(bx + bw), int(y0 + bh)), color, thick, lt)
        cv2.rectangle(img, (int(bx + 0.1 * ppm), int(y0 + 0.1 * ppm)), (int(bx + 0.75 * ppm), int(y0 + 0.45 * ppm)), color, thick, lt)
        cv2.rectangle(img, (int(bx + 0.85 * ppm), int(y0 + 0.1 * ppm)), (int(bx + 1.5 * ppm), int(y0 + 0.45 * ppm)), color, thick, lt)
        cv2.line(img, (int(bx), int(y0 + 0.6 * ppm)), (int(bx + bw), int(y0 + 0.6 * ppm)), color, thick, lt)
    elif r < 0.9:
        # Round table and a sofa.
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        cv2.circle(img, (cx, cy), int(min(w, h) * 0.15), color, thick, lt)
        sx = int(x0 + 0.2 * ppm)
        cv2.rectangle(img, (sx, int(cy - 0.9 * ppm)), (sx + int(0.8 * ppm), int(cy + 0.9 * ppm)), color, thick, lt)


def _draw_stairs(img, rect, ppm, color, thick):
    x0, y0, x1, y1 = (int(v) for v in rect)
    w, h = x1 - x0, y1 - y0
    step = max(3, int(0.28 * ppm))
    if h >= w:
        for y in range(y0 + step, y1 - 1, step):
            cv2.line(img, (x0, y), (x1, y), color, thick)
        cv2.line(img, ((x0 + x1) // 2, y0), ((x0 + x1) // 2, y1), color, thick)
        cv2.arrowedLine(img, ((x0 + x1) // 2 + step, y1 - step), ((x0 + x1) // 2 + step, y0 + step), color, thick, tipLength=0.1)
    else:
        for x in range(x0 + step, x1 - 1, step):
            cv2.line(img, (x, y0), (x, y1), color, thick)
        cv2.line(img, (x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2), color, thick)


def _wall_mask(labels, footprint, t_int, t_ext):
    """Interior walls centred on room boundaries, exterior walls just inside the footprint."""
    lab = labels.astype(np.float32)
    k = np.ones((t_int, t_int), np.uint8)
    interior = (cv2.dilate(lab, k) != cv2.erode(lab, k)) & (footprint > 0)
    dist_out = cv2.distanceTransform(footprint, cv2.DIST_L2, 5)
    exterior = (footprint > 0) & (dist_out <= t_ext)
    return interior | exterior


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate(seed=0, style=None):
    """Generate one synthetic floorplan. ``style`` is one of STYLES (random when None)."""
    rng = np.random.default_rng(seed)
    if style is None:
        style = str(rng.choice(STYLES))
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}")

    ppm = float(rng.uniform(16, 34))  # pixels per metre
    lay, fp_rects = _make_layout(rng, ppm)

    # Place everything on a canvas with margins.
    xs = [r[0] for r in lay.rects + fp_rects] + [r[2] for r in lay.rects + fp_rects]
    ys = [r[1] for r in lay.rects + fp_rects] + [r[3] for r in lay.rects + fp_rects]
    margin = int(rng.uniform(4, 9) * ppm)
    ox, oy = margin - min(xs), margin - min(ys)
    W = int(max(xs) - min(xs) + 2 * margin)
    H = int(max(ys) - min(ys) + 2 * margin)

    labels = np.zeros((H, W), np.int32)
    for x0, y0, x1, y1, room in lay.rects:
        labels[int(y0 + oy):int(y1 + oy), int(x0 + ox):int(x1 + ox)] = room + 1
    footprint = np.zeros((H, W), np.uint8)
    for x0, y0, x1, y1 in fp_rects:
        footprint[int(y0 + oy):int(y1 + oy), int(x0 + ox):int(x1 + ox)] = 1
    labels[footprint == 0] = 0
    # Any footprint pixel left unassigned joins its nearest room.
    if np.any((footprint > 0) & (labels == 0)):
        from scipy import ndimage as ndi

        idx = ndi.distance_transform_edt(labels == 0, return_distances=False, return_indices=True)
        filled = labels[idx[0], idx[1]]
        labels = np.where(footprint > 0, filled, 0).astype(np.int32)
    kinds = list(lay.kinds)
    _merge_big_rooms(rng, labels, kinds, int(rng.integers(1, 4)))

    t_int = int(max(2, round(rng.uniform(0.12, 0.22) * ppm))) | 1
    t_ext = int(max(t_int + 1, round(rng.uniform(0.25, 0.4) * ppm)))
    if style in ("color_fill",):
        t_int = max(3, t_int - 2) | 1
        t_ext = t_int + 1
    walls = _wall_mask(labels, footprint, t_int, t_ext)

    # Doors: connect rooms in a spanning tree, preferring the corridor, plus a few extras.
    ids = [int(v) for v in np.unique(labels) if v > 0]
    adj = {}
    for a in ids:
        ma = labels == a
        ring = cv2.dilate(ma.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~ma
        adj[a] = [int(v) for v in np.unique(labels[ring]) if v > 0]
    corridor_ids = [i + 1 for i, k in enumerate(kinds) if k == "corridor" and (i + 1) in adj]
    start = corridor_ids[0] if corridor_ids else ids[0]
    connected = {start}
    frontier = [start]
    edges = []
    while frontier:
        a = frontier.pop(0)
        nb = list(adj[a])
        rng.shuffle(nb)
        for b in nb:
            if b not in connected:
                connected.add(b)
                frontier.append(b)
                edges.append((a, b))
    for a in ids:
        for b in adj[a]:
            if a < b and (a, b) not in edges and (b, a) not in edges and rng.random() < 0.15:
                edges.append((a, b))

    doors = []  # (orientation, fixed coord, start, end)
    ignore = np.zeros((H, W), bool)
    for a, b in edges:
        runs = _shared_runs(labels, a, b)
        runs = [r for r in runs if r[3] - r[2] > 1.4 * ppm]
        if not runs:
            continue
        o, fixed, s, e = runs[int(rng.integers(len(runs)))]
        dw = (rng.uniform(1.4, 1.8) if rng.random() < 0.12 else rng.uniform(0.8, 1.0)) * ppm
        dw = min(dw, (e - s) - 0.5 * ppm)
        c = rng.uniform(s + 0.25 * ppm + dw / 2, e - 0.25 * ppm - dw / 2)
        d0, d1 = int(c - dw / 2), int(c + dw / 2)
        half = t_int // 2 + 2
        if o == "v":
            x = int(fixed)
            walls[d0:d1, x - half:x + half + 1] = False
            ignore[d0:d1, x - half:x + half + 1] = True
        else:
            y = int(fixed)
            walls[y - half:y + half + 1, d0:d1] = False
            ignore[y - half:y + half + 1, d0:d1] = True
        doors.append((o, fixed, d0, d1, False))

    # Exterior entrances into the corridor (or any room on the boundary).
    fp_dist = cv2.distanceTransform(footprint, cv2.DIST_L2, 5)
    n_ent = int(rng.integers(1, 3))
    for _ in range(n_ent):
        target = corridor_ids[0] if corridor_ids and rng.random() < 0.8 else int(rng.choice(ids))
        cand = np.argwhere((labels == target) & (fp_dist <= 1.5))
        if len(cand) == 0:
            continue
        y, x = cand[int(rng.integers(len(cand)))]
        dw = rng.uniform(1.0, 1.8) * ppm
        # Orientation from which side of the footprint we are on.
        horiz_edge = (y == 0 or footprint[max(0, y - 1), x] == 0 or footprint[min(H - 1, y + 1), x] == 0)
        if horiz_edge:
            seg = slice(int(x - dw / 2), int(x + dw / 2))
            band = slice(max(0, y - t_ext - 2), min(H, y + t_ext + 3))
            if np.all(labels[y, seg] == target):
                walls[band, seg] = False
                ignore[band, seg] = True
                doors.append(("h", y, seg.start, seg.stop, True))
        else:
            seg = slice(int(y - dw / 2), int(y + dw / 2))
            band = slice(max(0, x - t_ext - 2), min(W, x + t_ext + 3))
            if np.all(labels[seg, x] == target):
                walls[seg, band] = False
                ignore[seg, band] = True
                doors.append(("v", x, seg.start, seg.stop, True))

    # Windows: runs on the exterior wall away from corners and doors.
    windows = []
    ext_wall = (footprint > 0) & (fp_dist <= t_ext) & walls
    if style not in ("gray_fill", "color_fill"):
        n_win = int(rng.integers(3, 10))
        for _ in range(n_win):
            cand = np.argwhere(ext_wall & (fp_dist <= 1.5))
            if len(cand) == 0:
                break
            y, x = cand[int(rng.integers(len(cand)))]
            ww = rng.uniform(1.0, 2.2) * ppm
            if footprint[max(0, y - 1), x] == 0 or footprint[min(H - 1, y + 1), x] == 0:
                seg = slice(int(x - ww / 2), int(x + ww / 2))
                band = slice(max(0, y - t_ext - 1), min(H, y + t_ext + 1))
                if np.all(ext_wall[y, seg]) and np.all(walls[band, seg].any(axis=0)):
                    walls[band, seg] &= ~ext_wall[band, seg]
                    windows.append(("h", y, seg.start, seg.stop))
            else:
                seg = slice(int(y - ww / 2), int(y + ww / 2))
                band = slice(max(0, x - t_ext - 1), min(W, x + t_ext + 1))
                if np.all(ext_wall[seg, x]) and np.all(walls[seg, band].any(axis=1)):
                    walls[seg, band] &= ~ext_wall[seg, band]
                    windows.append(("v", x, seg.start, seg.stop))

    gt = np.where(walls, 0, labels).astype(np.int32)
    gt[(ignore & ~walls) & (footprint > 0)] = -1
    # Window openings are part of the exterior wall, not of the room.
    gt[ext_wall & ~walls] = 0

    names = {}
    img = _render(rng, style, labels, kinds, walls, footprint, doors, windows, ppm, t_int, t_ext, names)

    rooms = []
    for rid in ids:
        if not np.any(gt == rid):
            continue
        ys_, xs_ = np.nonzero(gt == rid)
        rooms.append(SynthRoom(rid, kinds[rid - 1], names.get(rid, ""),
                               (int(xs_.min()), int(ys_.min()), int(xs_.max()), int(ys_.max()))))

    plan = SynthPlan(img, gt, rooms, footprint, style, ppm, float(t_int), {"doors": doors, "windows": windows, "t_ext": t_ext})
    if style == "scan":
        plan = _scan_effects(rng, plan)
    return plan


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _render(rng, style, labels, kinds, walls, footprint, doors, windows, ppm, t_int, t_ext, names):
    """Draw the plan; the label written in each room goes into ``names`` (room id -> text)."""
    H, W = labels.shape
    ink = (0, 0, 0)
    thin = max(1, int(round(ppm / 30)))
    lt = cv2.LINE_AA
    paper = (255, 255, 255)
    if style == "blueprint":
        paper = (int(rng.integers(100, 140)), int(rng.integers(55, 80)), int(rng.integers(15, 35)))
        ink = (245, 240, 235)
    img = np.full((H, W, 3), paper, np.uint8)
    w8 = walls.astype(np.uint8)
    ids = [int(v) for v in np.unique(labels) if v > 0]

    # --- fills ---
    if style == "gray_fill":
        room_gray = int(rng.integers(205, 222))
        corr_gray = int(rng.integers(232, 244))
        for rid in ids:
            g = corr_gray if kinds[rid - 1] == "corridor" else room_gray
            img[(labels == rid) & (footprint > 0)] = (g, g, g)
    elif style == "color_fill":
        palette = {
            "room": [(196, 222, 246), (205, 236, 210), (240, 220, 200), (220, 205, 240), (190, 230, 240)],
            "corridor": [(245, 245, 245)],
            "stair": [(150, 190, 240)],
            "elevator": [(150, 190, 240)],
            "restroom": [(240, 205, 180)],
        }
        for rid in ids:
            opts = palette[kinds[rid - 1]]
            img[(labels == rid) & (footprint > 0)] = opts[int(rng.integers(len(opts)))]

    # --- walls ---
    if style in ("solid", "scan"):
        img[walls] = (20, 20, 20) if style == "solid" else (35, 35, 35)
    elif style == "gray_fill":
        img[walls] = (55, 55, 55)
    elif style == "color_fill":
        c = int(rng.integers(60, 110))
        img[walls] = (c, c, c)
    elif style in ("cad", "blueprint", "hatched"):
        if style == "cad" and rng.random() < 0.5:
            img[walls] = (175, 175, 175)  # gray poche
        if style == "hatched":
            hatch = np.zeros((H, W), np.uint8)
            sp = max(4, int(0.12 * ppm))
            for k in range(-H, W, sp):
                cv2.line(hatch, (k, 0), (k + H, H), 1, 1)
            img[(hatch > 0) & walls] = ink
        contours, _ = cv2.findContours(w8, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(img, contours, -1, ink, thin, lt)

    # --- windows ---
    for o, fixed, s, e in windows:
        fixed = int(fixed)
        for off in (-t_ext // 3, t_ext // 3) if style != "solid" else (-t_ext // 2, 0, t_ext // 2):
            if o == "h":
                yy = fixed + (t_ext // 2 if fixed < H / 2 else -t_ext // 2) + off
                cv2.line(img, (s, yy), (e, yy), ink if style != "solid" else (60, 60, 60), thin)
            else:
                xx = fixed + (t_ext // 2 if fixed < W / 2 else -t_ext // 2) + off
                cv2.line(img, (xx, s), (xx, e), ink if style != "solid" else (60, 60, 60), thin)

    # --- doors: swing arcs and leaves ---
    if style in ("solid", "cad", "hatched", "blueprint", "scan"):
        for o, fixed, s, e, ext in doors:
            r = e - s
            fixed = int(fixed)
            if rng.random() < 0.15:
                continue
            sgn = 1 if rng.random() < 0.5 else -1
            if o == "v":
                hinge = (fixed, s)
                leaf_end = (fixed + sgn * r, s)
                cv2.line(img, hinge, leaf_end, ink, thin, lt)
                a0, a1 = (0, 90) if sgn > 0 else (90, 180)
                cv2.ellipse(img, hinge, (r, r), 0, a0, a1, ink, max(1, thin - 0), lt)
            else:
                hinge = (s, fixed)
                leaf_end = (s, fixed + sgn * r)
                cv2.line(img, hinge, leaf_end, ink, thin, lt)
                a0, a1 = (0, 90) if sgn > 0 else (270, 360)
                cv2.ellipse(img, hinge, (r, r), 0, a0, a1, ink, thin, lt)

    # --- room contents ---
    furn_color = ink if style != "solid" else (90, 90, 90)
    text_color = ink if style not in ("gray_fill", "color_fill") else (40, 40, 40)
    font = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_PLAIN, cv2.FONT_HERSHEY_COMPLEX][int(rng.integers(4))]
    tscale = float(np.clip(ppm / 55 * rng.uniform(0.8, 1.2), 0.3, 1.0))
    tthick = 1 if tscale < 0.6 else 2
    number = int(rng.integers(1, 4)) * 100
    for rid in ids:
        m = (labels == rid) & ~walls
        ys, xs = np.nonzero(m)
        if len(xs) == 0:
            continue
        kind = kinds[rid - 1]
        # Largest inscribed rectangle approximation: the bbox of the eroded mask core.
        dist = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3)
        cy, cx = np.unravel_index(np.argmax(dist), dist.shape)
        rad = dist[cy, cx]
        rect = (cx - rad * 0.95, cy - rad * 0.95, cx + rad * 0.95, cy + rad * 0.95)
        # Interior bbox (for stairs etc. rooms are rectangles anyway)
        bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max(), ys.max()
        if kind == "stair" and style != "color_fill":
            _draw_stairs(img, (bx0, by0, bx1, by1), ppm, furn_color if style != "gray_fill" else (70, 70, 70), thin)
        elif kind == "elevator" and style not in ("gray_fill", "color_fill"):
            cv2.line(img, (bx0, by0), (bx1, by1), furn_color, thin, lt)
            cv2.line(img, (bx0, by1), (bx1, by0), furn_color, thin, lt)
        elif style in ("solid", "cad", "hatched", "blueprint", "scan") and rng.random() < 0.75:
            full = (bx0, by0, bx1, by1) if (m[by0:by1, bx0:bx1].mean() > 0.97) else rect
            _draw_furniture(rng, img, m, full, ppm, furn_color, thin, kind)
        if style in ("gray_fill",) and kind == "room" and rad > 1.5 * ppm and rng.random() < 0.25:
            # Columns in big rooms.
            s = max(3, int(0.3 * ppm))
            cv2.rectangle(img, (int(cx - s), int(cy + rad * 0.5)), (int(cx + s), int(cy + rad * 0.5 + 2 * s)), (55, 55, 55), -1)

        # Labels
        if rad < 0.9 * ppm or (kind == "corridor" and rng.random() < 0.5):
            continue
        name = ROOM_NAMES[kind][int(rng.integers(len(ROOM_NAMES[kind])))]
        number += int(rng.integers(1, 4))
        names[rid] = name
        lines = [name]
        if style in ("gray_fill", "color_fill"):
            lines.append(f"({number})")
        elif style == "solid" and kind == "room":
            area = m.sum() / ppm ** 2
            lines.append(f"{area:.1f} m2" if rng.random() < 0.5 else f"{int(area * 3.28)}'x{int(rad / ppm * 6.5)}'")
        elif kind == "room":
            lines.append(str(number))
        for li, txt in enumerate(lines):
            sc = tscale * (0.85 if li else 1.0)
            _put_text(img, txt, (cx, cy + (li - (len(lines) - 1) / 2) * tscale * 32), sc, text_color, tthick, font)

    # --- outside decorations ---
    if style in ("cad", "hatched", "blueprint", "solid"):
        _dimension_lines(rng, img, footprint, ppm, ink, thin, font, tscale)
    if style in ("cad", "hatched", "blueprint"):
        cv2.rectangle(img, (6, 6), (W - 7, H - 7), ink, thin + 1)
        tb_w, tb_h = int(W * 0.28), int(min(H * 0.12, 3.5 * ppm))
        cv2.rectangle(img, (W - 7 - tb_w, H - 7 - tb_h), (W - 7, H - 7), ink, thin)
        cv2.line(img, (W - 7 - tb_w, H - 7 - tb_h // 2), (W - 7, H - 7 - tb_h // 2), ink, thin)
        _put_text(img, "FLOOR PLAN - LEVEL 1", (W - 7 - tb_w / 2, H - 7 - tb_h * 0.75), tscale * 0.9, ink, tthick, font)
        _put_text(img, "SCALE 1:100   DWG A-101", (W - 7 - tb_w / 2, H - 7 - tb_h * 0.25), tscale * 0.7, ink, 1, font)
        _north_arrow(img, (int(14 + 1.5 * ppm), int(14 + 1.5 * ppm)), int(1.2 * ppm), ink, thin)
    if style == "gray_fill":
        _wayfinding_decor(rng, img, labels, footprint, walls, ppm, font, tscale)
    if style == "color_fill":
        # Legend in a corner.
        y = 20
        for name, col in (("Office", (196, 222, 246)), ("Restroom", (240, 205, 180)), ("Stairs", (150, 190, 240))):
            cv2.rectangle(img, (20, y), (44, y + 18), col, -1)
            cv2.rectangle(img, (20, y), (44, y + 18), (80, 80, 80), 1)
            _put_text(img, name, (95, y + 9), 0.5, (40, 40, 40), 1)
            y += 26
    return img


def _dimension_lines(rng, img, footprint, ppm, ink, thin, font, tscale):
    ys, xs = np.nonzero(footprint)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    off = int(1.6 * ppm)
    y = y0 - off
    if y > 10:
        cv2.line(img, (x0, y), (x1, y), ink, thin)
        for x in np.linspace(x0, x1, int(rng.integers(3, 6))):
            x = int(x)
            cv2.line(img, (x - 4, y + 4), (x + 4, y - 4), ink, thin)
            cv2.line(img, (x, y - 6), (x, y + int(0.8 * ppm)), ink, thin)
        _put_text(img, f"{(x1 - x0) / ppm:.2f}", ((x0 + x1) / 2, y - 0.5 * ppm), tscale * 0.8, ink, 1, font)
    x = x1 + off
    if x < img.shape[1] - 10:
        cv2.line(img, (x, y0), (x, y1), ink, thin)
        for yy in np.linspace(y0, y1, int(rng.integers(3, 6))):
            yy = int(yy)
            cv2.line(img, (x - 4, yy + 4), (x + 4, yy - 4), ink, thin)
            cv2.line(img, (x - int(0.8 * ppm), yy), (x + 6, yy), ink, thin)


def _north_arrow(img, c, r, ink, thin):
    cv2.circle(img, c, r, ink, thin, cv2.LINE_AA)
    pts = np.array([[c[0], c[1] - r], [c[0] - r // 3, c[1] + r // 2], [c[0], c[1] + r // 5], [c[0] + r // 3, c[1] + r // 2]], np.int32)
    cv2.fillPoly(img, [pts], ink, cv2.LINE_AA)
    _put_text(img, "N", (c[0], c[1] - r - 12), 0.5, ink, 1)


def _wayfinding_decor(rng, img, labels, footprint, walls, ppm, font, tscale):
    """Memorial Union style extras: icon badges, callouts with leader arrows, wing captions."""
    H, W = labels.shape
    ys, xs = np.nonzero(footprint)
    fx0, fx1, fy0, fy1 = xs.min(), xs.max(), ys.min(), ys.max()
    s = max(8, int(0.45 * ppm))
    for _ in range(int(rng.integers(2, 6))):
        free = np.argwhere((footprint > 0) & ~walls)
        y, x = free[int(rng.integers(len(free)))]
        col, letter = [((40, 30, 210), "E"), ((30, 150, 240), "W"), ((30, 150, 240), "M"), ((30, 150, 240), "S")][int(rng.integers(4))]
        cv2.rectangle(img, (int(x - s), int(y - s)), (int(x + s), int(y + s)), col, -1)
        _put_text(img, letter, (x, y), s / 22, (255, 255, 255), 2)
    # Callouts outside the building with an arrow into a room.
    for _ in range(int(rng.integers(1, 4))):
        side = rng.integers(2)
        tx = int(rng.uniform(fx0, fx1))
        ty = int(fy0 - 2.2 * ppm) if side == 0 else int(fy1 + 2.2 * ppm)
        if not (12 < ty < H - 12):
            continue
        _put_text(img, "Lounge (2210)", (tx, ty + (-12 if side == 0 else 12)), tscale * 0.8, (30, 30, 30), 1, font)
        target_y = int(fy0 + 1.5 * ppm) if side == 0 else int(fy1 - 1.5 * ppm)
        cv2.arrowedLine(img, (tx, ty), (tx, target_y), (30, 30, 30), max(2, int(ppm / 12)), cv2.LINE_AA, tipLength=0.25)
    # Wing captions and dashed guide lines in light gray.
    _put_text(img, "EAST WING", (W * 0.75, 14 + 0.6 * ppm), tscale * 1.2, (120, 120, 120), 1, font)
    gx = int(rng.uniform(0.3, 0.7) * W)
    for y in range(0, H, 24):
        if not footprint[min(H - 1, y):min(H, y + 13), gx].any():
            cv2.line(img, (gx, y), (gx, y + 12), (205, 205, 205), 2)


def _scan_effects(rng, plan):
    img = plan.image.astype(np.float32)
    H, W = img.shape[:2]
    # Paper tint and uneven illumination.
    tint = np.array([rng.uniform(0.88, 0.97), rng.uniform(0.92, 1.0), rng.uniform(0.95, 1.0)], np.float32)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    shade = 1.0 - 0.18 * ((xx / W - rng.uniform(0, 1)) ** 2 + (yy / H - rng.uniform(0, 1)) ** 2)
    img = img * tint[None, None, :] * shade[..., None]
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.6, 1.2))
    img += rng.normal(0, rng.uniform(4, 10), img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    # Small rotation, applied identically to the ground truth.
    ang = rng.uniform(-3.0, 3.0)
    M = cv2.getRotationMatrix2D((W / 2, H / 2), ang, 1.0)
    img = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=(235, 240, 240))
    labels = cv2.warpAffine(plan.labels, M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
    footprint = cv2.warpAffine(plan.footprint, M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(55, 80))])
    img = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    plan.image, plan.labels, plan.footprint = img, labels, footprint
    plan.meta["rotation_deg"] = ang
    return plan
