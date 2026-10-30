"""Turn a ConversionResult (pixel space) into GeoJSON (longitude / latitude).

Three ways to place the plan on the map, from crude to exact:

* ``bounds``: a lat/lon box (the old app's ``{min_lat, max_lat, min_long, max_long}``).
  The building outline's bounding box is stretched to fill it (``fit="outline"``),
  or the whole image is (``fit="image"``).
* ``control_points``: two or more ``((x, y), (lon, lat))`` pairs. Two points give a
  similarity transform (move, rotate, uniform scale); three or more a least-squares affine.
* ``transform``: an explicit 2x3 affine ``[[a, b, c], [d, e, f]]`` with
  ``lon = a*x + b*y + c`` and ``lat = d*x + e*y + f``.

With none of these, coordinates stay in image pixels (useful for debugging).
"""

from __future__ import annotations

import numpy as np

from .result import ConversionResult


def fit_affine(control_points) -> np.ndarray:
    """Least-squares pixel -> lon/lat transform from ((x, y), (lon, lat)) pairs."""
    src = np.array([p for p, _ in control_points], np.float64)
    dst = np.array([q for _, q in control_points], np.float64)
    if len(src) < 2:
        raise ValueError("need at least two control points")
    if len(src) == 2:
        # Similarity (no shear). Image y points down; flip it so the fit can be a
        # proper rotation + scale in a right-handed frame.
        s = src * np.array([1.0, -1.0])
        a = np.zeros((4, 4))
        b = np.zeros(4)
        for i, ((x, y), (u, v)) in enumerate(zip(s, dst)):
            a[2 * i] = [x, -y, 1, 0]
            a[2 * i + 1] = [y, x, 0, 1]
            b[2 * i], b[2 * i + 1] = u, v
        p, q, tx, ty = np.linalg.solve(a, b)
        # [u, v] = [[p, -q], [q, p]] @ [x, -y] + [tx, ty]
        return np.array([[p, q, tx], [q, -p, ty]])
    A = np.hstack([src, np.ones((len(src), 1))])
    sol, *_ = np.linalg.lstsq(A, dst, rcond=None)
    return sol.T  # (2, 3)


def bounds_transform(result: ConversionResult, bounds: dict, fit: str = "outline") -> np.ndarray:
    def get(*names):
        for n in names:
            if n in bounds:
                return float(bounds[n])
        raise KeyError(names[0])

    min_lat, max_lat = get("min_lat", "south"), get("max_lat", "north")
    min_lon, max_lon = get("min_long", "min_lon", "min_lng", "west"), get("max_long", "max_lon", "max_lng", "east")
    if fit == "outline" and result.outline:
        pts = np.array(result.outline)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
    else:
        x0, y0 = 0.0, 0.0
        x1, y1 = float(result.image_size[0]), float(result.image_size[1])
    sx = (max_lon - min_lon) / max(x1 - x0, 1e-9)
    sy = (max_lat - min_lat) / max(y1 - y0, 1e-9)
    return np.array([[sx, 0.0, min_lon - sx * x0], [0.0, -sy, max_lat + sy * y0]])


def _apply(T, ring):
    a = np.asarray(ring, np.float64)
    if T is None:
        out = a
    else:
        out = a @ T[:, :2].T + T[:, 2]
    return [[float(x), float(y)] for x, y in out]


def _close(ring):
    return ring + [ring[0]] if ring and ring[0] != ring[-1] else ring


def _orient(ring, ccw: bool, flip_y: bool):
    a = np.asarray(ring, np.float64)
    area = 0.5 * float(np.dot(a[:, 0], np.roll(a[:, 1], -1)) - np.dot(a[:, 1], np.roll(a[:, 0], -1)))
    if flip_y:
        area = -area
    return ring if (area > 0) == ccw else ring[::-1]


def _polygon(T, outer, holes):
    flip = T is None  # in pixel space y points down
    rings = [_close(_orient(_apply(T, outer), True, flip))]
    rings += [_close(_orient(_apply(T, h), False, flip)) for h in holes if len(h) >= 3]
    return {"type": "Polygon", "coordinates": rings}


def to_geojson(result: ConversionResult, bounds: dict = None, control_points=None, transform=None,
               fit: str = "outline", include=("outline", "rooms"), floor: str = None) -> dict:
    """GeoJSON FeatureCollection of the building outline and rooms (and optionally doors).

    ``include`` may contain "outline", "rooms", "doors" and "walls".
    """
    if transform is not None:
        T = np.asarray(transform, np.float64).reshape(2, 3)
    elif control_points is not None:
        T = fit_affine(control_points)
    elif bounds is not None:
        T = bounds_transform(result, bounds, fit)
    else:
        T = None
    m2 = None
    if result.scale_hint:
        m2 = 1.0 / result.scale_hint ** 2
    feats = []
    if "outline" in include:
        for i, part in enumerate(result.footprint):
            props = {"feature": "building", "part": i}
            if floor is not None:
                props["floor"] = floor
            feats.append({"type": "Feature", "geometry": _polygon(T, part["polygon"], part["holes"]), "properties": props})
    if "rooms" in include:
        for r in result.rooms:
            props = {
                "feature": "room",
                "id": r.id,
                "label": r.label,
                "name": r.name,
                "number": r.number,
                "kind": r.kind,
                "area_px": r.area_px,
                "area_m2": round(r.area_px * m2, 1) if m2 else None,
                "confidence": r.confidence,
            }
            if floor is not None:
                props["floor"] = floor
            feats.append({"type": "Feature", "id": r.id, "geometry": _polygon(T, r.polygon, r.holes), "properties": props})
    if "doors" in include:
        for i, d in enumerate(result.doors):
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": _apply(T, d.polyline)},
                          "properties": {"feature": d.kind, "width_px": d.width_px}})
    if "walls" in include:
        for w in result.walls:
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": _apply(T, w.polyline)},
                          "properties": {"feature": "wall", "thickness_px": w.thickness_px}})
    return {"type": "FeatureCollection", "features": feats}
