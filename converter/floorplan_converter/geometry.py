"""Raster masks to clean polygons: tracing, simplification and right-angle snapping."""

from __future__ import annotations

import cv2
import numpy as np


def dominant_angle(mask: np.ndarray) -> float:
    """The plan's main wall direction in degrees, in [-45, 45). 0 for an unrotated plan.

    Finds the rotation that makes the projections of the mask's boundary pixels onto
    the x and y axes sharpest (walls line up into few rows and columns).
    """
    m = mask.astype(np.uint8)
    edge = m & ~cv2.erode(m, np.ones((3, 3), np.uint8))
    ys, xs = np.nonzero(edge)
    if xs.size < 50:
        return 0.0
    if xs.size > 40000:
        idx = np.random.default_rng(0).choice(xs.size, 40000, replace=False)
        xs, ys = xs[idx], ys[idx]
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    pts -= pts.mean(axis=0)

    def sharpness(deg):
        t = np.radians(deg)
        c, s_ = np.cos(t), np.sin(t)
        u = pts[:, 0] * c + pts[:, 1] * s_
        v = -pts[:, 0] * s_ + pts[:, 1] * c
        hu = np.bincount(np.round(u - u.min()).astype(np.int64))
        hv = np.bincount(np.round(v - v.min()).astype(np.int64))
        return float((hu.astype(np.float64) ** 2).sum() + (hv.astype(np.float64) ** 2).sum())

    coarse = np.arange(-45.0, 45.0, 1.0)
    best = float(coarse[int(np.argmax([sharpness(a) for a in coarse]))])
    fine = np.arange(best - 1.0, best + 1.0001, 0.1)
    best = float(fine[int(np.argmax([sharpness(a) for a in fine]))])
    if best >= 45.0:
        best -= 90.0
    if best < -45.0:
        best += 90.0
    return best


def mask_polygons(mask: np.ndarray, epsilon: float, min_hole_area: float = 0.0) -> list:
    """Trace a binary mask into [(outer, [holes])] with (x, y) float vertices.

    Coordinates use the pixel-corner convention: pixel (0, 0) covers [0, 1] x [0, 1],
    so a 10 x 10 px square traces to a polygon of area 100.
    """
    m = mask.astype(np.uint8)
    if not m.any():
        return []
    cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    hier = hier[0]
    out = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1:
            continue  # holes are collected with their parent
        outer = _simplify(c.reshape(-1, 2).astype(np.float64), epsilon)
        if outer is None:
            continue
        holes = []
        j = hier[i][2]
        while j != -1:
            hpts = cnts[j].reshape(-1, 2).astype(np.float64)
            if len(hpts) >= 3 and polygon_area(hpts) >= max(min_hole_area, 1.0):
                h = _simplify(hpts, epsilon)
                if h is not None:
                    holes.append(h)
            j = hier[j][0]
        poly = _grow_half_pixel(outer, holes)
        if poly is not None:
            out.append(poly)
    return out


def _grow_half_pixel(outer: np.ndarray, holes: list):
    """Contours run through boundary pixel centres; push edges out to the pixel edges
    and shift to the corner convention."""
    try:
        from shapely.geometry import Polygon
    except ImportError:  # pragma: no cover
        return outer + 0.5, [h + 0.5 for h in holes]
    try:
        poly = Polygon(outer, holes)
        if not poly.is_valid:
            poly = poly.buffer(0)
        grown = poly.buffer(0.5, join_style=2, mitre_limit=2.0)
    except Exception:
        return outer + 0.5, [h + 0.5 for h in holes]
    if grown.is_empty:
        return None
    if grown.geom_type == "MultiPolygon":
        grown = max(grown.geoms, key=lambda g: g.area)
    ext = np.asarray(grown.exterior.coords)[:-1] + 0.5
    ints = [np.asarray(r.coords)[:-1] + 0.5 for r in grown.interiors]
    return ext, ints


def _simplify(pts: np.ndarray, epsilon: float):
    if len(pts) < 3:
        return None
    approx = cv2.approxPolyDP(pts.astype(np.float32).reshape(-1, 1, 2), max(0.5, epsilon), True)
    approx = approx.reshape(-1, 2).astype(np.float64)
    if len(approx) < 3:
        return None
    return approx


def _area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def polygon_area(pts: np.ndarray) -> float:
    return abs(_area(np.asarray(pts, np.float64)))


def orthogonalize(pts: np.ndarray, angle_deg: float, tol_deg: float = 9.0, min_len: float = 0.0) -> np.ndarray:
    """Snap nearly axis-aligned edges (relative to ``angle_deg``) to exact right angles.

    Curved or diagonal stretches are left as they are.
    """
    pts = np.asarray(pts, np.float64)
    n = len(pts)
    if n < 4:
        return pts
    th = np.radians(angle_deg)
    c, s = np.cos(th), np.sin(th)
    R = np.array([[c, s], [-s, c]])  # rotate by -angle
    P = pts @ R.T
    edges = []  # (kind, value or (p, q), length)
    for i in range(n):
        p, q = P[i], P[(i + 1) % n]
        d = q - p
        L = float(np.hypot(*d))
        if L < 1e-9:
            continue
        a = np.degrees(np.arctan2(d[1], d[0])) % 180.0
        if min(a, 180 - a) <= tol_deg and L >= min_len:
            edges.append(["h", (p[1] + q[1]) / 2.0, L, p, q])
        elif abs(a - 90.0) <= tol_deg and L >= min_len:
            edges.append(["v", (p[0] + q[0]) / 2.0, L, p, q])
        else:
            edges.append(["o", None, L, p, q])
    # Merge consecutive edges of the same snapped orientation.
    merged = []
    for e in edges:
        if merged and e[0] in "hv" and merged[-1][0] == e[0]:
            m = merged[-1]
            m[1] = (m[1] * m[2] + e[1] * e[2]) / (m[2] + e[2])
            m[2] += e[2]
            m[4] = e[4]
        else:
            merged.append(list(e))
    if len(merged) >= 2 and merged[0][0] in "hv" and merged[0][0] == merged[-1][0]:
        a, b = merged[-1], merged[0]
        b[1] = (a[1] * a[2] + b[1] * b[2]) / (a[2] + b[2])
        b[2] += a[2]
        b[3] = a[3]
        merged.pop()
    if len(merged) < 3:
        return pts

    def line(e):
        if e[0] == "h":
            return np.array([0.0, 1.0]), e[1]  # n . x = c  ->  y = c
        if e[0] == "v":
            return np.array([1.0, 0.0]), e[1]
        p, q = e[3], e[4]
        d = q - p
        nrm = np.array([-d[1], d[0]]) / np.hypot(*d)
        return nrm, float(nrm @ p)

    out = []
    m = len(merged)
    for i in range(m):
        e0, e1 = merged[i - 1], merged[i]
        n0, c0 = line(e0)
        n1, c1 = line(e1)
        A = np.array([n0, n1])
        det = np.linalg.det(A)
        if abs(det) < 1e-6:
            out.append(e1[3])
            continue
        out.append(np.linalg.solve(A, np.array([c0, c1])))
    Q = np.array(out) @ R  # rotate back
    return Q


def clean_polygon(outer: np.ndarray, holes: list, angle: float, ortho: bool, min_len: float):
    """Orthogonalize and validate; fall back to the input when snapping misbehaves."""
    if not ortho:
        return outer, holes
    try:
        from shapely.geometry import Polygon
    except ImportError:  # pragma: no cover
        Polygon = None
    o2 = orthogonalize(outer, angle, min_len=min_len)
    h2 = [orthogonalize(h, angle, min_len=min_len) for h in holes]
    a0 = polygon_area(outer)
    a1 = polygon_area(o2)
    if a0 <= 0 or abs(a1 - a0) > 0.08 * a0 + 4 * min_len * min_len:
        return outer, holes
    if Polygon is not None:
        try:
            poly = Polygon(o2, [h for h in h2 if len(h) >= 3])
            if not poly.is_valid:
                return outer, holes
        except Exception:
            return outer, holes
    return o2, h2
