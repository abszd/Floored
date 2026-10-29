"""Fit a floor plan onto its building's real outline.

The converter gives the plan's exterior outline in pixels; OpenStreetMap gives
the building footprint in lon/lat. We search for the rotation, scale and
offset that make the two overlap best (intersection over union), starting
from each of the four ways the plans' principal axes can line up with the
footprint's, then polishing with Nelder–Mead.

Plans are drawn to scale, so the x and y scales are kept close to each other
by a penalty rather than fixed equal: scanned or re-exported images are often
stretched a little, and allowing a few percent of anisotropy fixes that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from ..converters.base import ConversionResult
from ..geo.projection import LocalFrame
from .transform import Placement

ANISOTROPY_WEIGHT = 4.0
# Small preference for north-up when the footprint is symmetric and IoU can't
# tell the orientations apart.
NORTH_UP_WEIGHT = 0.01


@dataclass
class FitResult:
    placement: Placement
    iou: float
    # Other orientations considered, best first: (iou, rotation_deg).
    alternatives: list[tuple[float, float]] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        """True when another orientation fits nearly as well (square-ish buildings)."""
        return bool(self.alternatives) and self.alternatives[0][0] > self.iou - 0.03

    def to_dict(self) -> dict[str, Any]:
        return {
            "placement": self.placement.to_dict(),
            "iou": self.iou,
            "ambiguous": self.ambiguous,
            "alternatives": [{"iou": i, "rotation_deg": r} for i, r in self.alternatives],
        }


def plan_outline(conv: ConversionResult) -> Polygon | None:
    """The plan's exterior in pixels: the converter's outline, else the union of the rooms."""
    if conv.outline and len(conv.outline) >= 3:
        poly = Polygon(conv.outline)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.area > 0:
            return _largest(poly)
    rooms = [Polygon(r.polygon) for r in conv.rooms if len(r.polygon) >= 3]
    rooms = [p if p.is_valid else p.buffer(0) for p in rooms]
    rooms = [p for p in rooms if not p.is_empty]
    if not rooms:
        return None
    w, h = conv.image_size
    # Rooms are separated by walls; close those gaps before taking the outline.
    gap = 0.01 * max(w, h)
    merged = unary_union([p.buffer(gap, join_style=2) for p in rooms]).buffer(-gap, join_style=2)
    return Polygon(_largest(merged).exterior) if not merged.is_empty else None


def _largest(geom: Any) -> Polygon:
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def _principal_angle(poly: Polygon) -> float:
    """Angle (degrees, CCW from +x) of the long side of the minimum rotated rectangle."""
    rect = poly.minimum_rotated_rectangle
    xs, ys = rect.exterior.coords.xy
    edges = [(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(2)]
    dx, dy = max(edges, key=lambda e: math.hypot(*e))
    return math.degrees(math.atan2(dy, dx))


def _iou(a: Polygon, b: Polygon) -> float:
    if a.is_empty or not a.is_valid:
        a = a.buffer(0)
    inter = a.intersection(b).area
    union = a.union(b).area
    return inter / union if union > 0 else 0.0


def _angle_from_north(bearing: float) -> float:
    b = bearing % 360
    return min(b, 360 - b)


def fit_to_footprint(
    conv: ConversionResult,
    footprint_geojson: dict[str, Any],
    *,
    allow_mirror: bool = False,
) -> FitResult | None:
    outline_px = plan_outline(conv)
    if outline_px is None:
        return None
    fp_ll = _largest(shape(footprint_geojson))
    if fp_ll.is_empty:
        return None

    w, h = conv.image_size
    c = fp_ll.centroid
    frame = LocalFrame(c.x, c.y)
    fp = Polygon([frame.to_local(x, y) for x, y in fp_ll.exterior.coords])
    if not fp.is_valid:
        fp = _largest(fp.buffer(0))

    # Plan in "image metres before scaling": centred on the image, y up.
    plan = affinity.scale(affinity.translate(outline_px, -w / 2, -h / 2), 1, -1, origin=(0, 0))
    scale0 = math.sqrt(fp.area / plan.area)
    phi_fp = _principal_angle(fp)
    phi_plan = _principal_angle(plan)
    pc = plan.centroid

    def build(params: np.ndarray, mirror: bool) -> Polygon:
        phi, lsx, lsy, tx, ty = params
        g = affinity.scale(plan, (-1 if mirror else 1) * math.exp(lsx), math.exp(lsy), origin=(0, 0))
        g = affinity.rotate(g, phi, origin=(0, 0))
        return affinity.translate(g, tx, ty)

    def cost(params: np.ndarray, mirror: bool) -> float:
        _, lsx, lsy, _, _ = params
        iou = _iou(build(params, mirror), fp)
        bearing = -params[0]
        return (1 - iou) + ANISOTROPY_WEIGHT * (lsx - lsy) ** 2 + NORTH_UP_WEIGHT * _angle_from_north(bearing) / 180

    results: list[tuple[float, float, np.ndarray, bool]] = []
    for mirror in (False, True) if allow_mirror else (False,):
        base = (-phi_plan if not mirror else phi_plan - 180)
        for k in range(4):
            phi = phi_fp + base + 90 * k
            # Translate so the plan's centroid lands on the footprint's.
            g = affinity.scale(pc, (-1 if mirror else 1) * scale0, scale0, origin=(0, 0))
            g = affinity.rotate(g, phi, origin=(0, 0))
            cx, cy = g.x, g.y
            x0 = np.array([phi, math.log(scale0), math.log(scale0), -cx, -cy])
            res = minimize(
                cost,
                x0,
                args=(mirror,),
                method="Nelder-Mead",
                options={"xatol": 1e-3, "fatol": 1e-5, "maxiter": 1500, "initial_simplex": _simplex(x0, scale0, fp)},
            )
            iou = _iou(build(res.x, mirror), fp)
            results.append((res.fun, iou, res.x, mirror))

    results.sort(key=lambda r: r[0])
    _, best_iou, best, mirror = results[0]
    phi, lsx, lsy, tx, ty = best
    # The image centre is the origin of `plan`, so it maps to (tx, ty).
    lon, lat = frame.to_lonlat(tx, ty)
    placement = Placement(
        center_lon=lon,
        center_lat=lat,
        width_m=math.exp(lsx) * w,
        height_m=math.exp(lsy) * h,
        rotation_deg=(-phi) % 360,
        mirror=mirror,
    )
    alternatives = [(round(r[1], 4), round((-r[2][0]) % 360, 2)) for r in results[1:]]
    alternatives.sort(key=lambda a: -a[0])
    return FitResult(placement=placement, iou=round(best_iou, 4), alternatives=alternatives)


def _simplex(x0: np.ndarray, scale0: float, fp: Polygon) -> np.ndarray:
    minx, miny, maxx, maxy = fp.bounds
    step = 0.08 * max(maxx - minx, maxy - miny)
    deltas = [8.0, 0.08, 0.08, step, step]
    simplex = [x0]
    for i, d in enumerate(deltas):
        v = x0.copy()
        v[i] += d
        simplex.append(v)
    return np.array(simplex)


def default_placement(
    conv: ConversionResult,
    lon: float,
    lat: float,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    footprint_geojson: dict[str, Any] | None = None,
) -> Placement:
    """A north-up first guess when there is no outline to fit against."""
    w, h = conv.image_size
    if conv.scale_hint:
        return Placement(lon, lat, w / conv.scale_hint, h / conv.scale_hint)
    span = None
    if footprint_geojson:
        g = _largest(shape(footprint_geojson))
        c = g.centroid
        lon, lat = c.x, c.y
        bbox = g.bounds
    if bbox:
        p = Placement.from_bounds(*bbox)
        span = max(p.width_m, p.height_m)
    span = span or 60.0
    # Size the plan's outline (not the whole image, which has margins) to the span.
    outline = plan_outline(conv)
    frac = 1.0
    if outline is not None:
        minx, miny, maxx, maxy = outline.bounds
        frac = max((maxx - minx) / w, (maxy - miny) / h) or 1.0
    m_per_px = span / (frac * max(w, h))
    return Placement(lon, lat, w * m_per_px, h * m_per_px)
