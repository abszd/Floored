"""Fit a floor plan onto its building's real outline.

The converter gives the plan's exterior outline in pixels; OpenStreetMap gives
the building footprint in lon/lat. We search for the rotation, scale and
offset that make the two overlap best (intersection over union), starting
from each of the four ways the plans' principal axes can line up with the
footprint's, then polishing with Nelder–Mead.

Plans are drawn to scale, so the x and y scales are kept close to each other
by a penalty rather than fixed equal: scanned or re-exported images are often
stretched a little, and allowing a few percent of anisotropy fixes that.

When another floor of the building is already placed (by a person, or by a
clear fit), its rotation and metres per pixel are a ``prior``: one more start,
and the tie-break between orientations the outline alone cannot tell apart
(square and symmetric buildings), in place of the plain north-up preference.
Floors of one building are almost always drawn the same way round.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, shape
import shapely
from shapely.ops import unary_union

from ..converters.base import ConversionResult
from ..geo.projection import LocalFrame
from .transform import Placement

ANISOTROPY_WEIGHT = 4.0
# Small preference for north-up when the footprint is symmetric and IoU can't
# tell the orientations apart.
NORTH_UP_WEIGHT = 0.01
# Preference for the rotation of the building's other floors, when there is one.
PRIOR_WEIGHT = 0.02
# A fit within this many degrees of the prior counts as agreeing with it.
PRIOR_AGREES_DEG = 10.0


@dataclass
class FitResult:
    placement: Placement
    iou: float
    # Other orientations considered, best first: (iou, rotation_deg).
    alternatives: list[tuple[float, float]] = field(default_factory=list)
    # The fit turned the plan the way the building's other floors are.
    agrees_with_prior: bool = False

    @property
    def ambiguous(self) -> bool:
        """True when another orientation fits nearly as well (square-ish buildings)
        and no other floor of the building settles it."""
        close = bool(self.alternatives) and self.alternatives[0][0] > self.iou - 0.03
        return close and not self.agrees_with_prior

    def to_dict(self) -> dict[str, Any]:
        return {
            "placement": self.placement.to_dict(),
            "iou": self.iou,
            "ambiguous": self.ambiguous,
            "agrees_with_prior": self.agrees_with_prior,
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


def _angle_between(a: float, b: float) -> float:
    return _angle_from_north(a - b)


def fit_to_footprint(
    conv: ConversionResult,
    footprint_geojson: dict[str, Any],
    *,
    allow_mirror: bool = False,
    prior: tuple[float, float] | None = None,
) -> FitResult | None:
    """Fit the plan outline onto the footprint. ``prior`` is (rotation_deg, metres per
    pixel) of another floor of the building that is already placed."""
    outline_px = plan_outline(conv)
    if outline_px is None:
        return None
    fp_ll = shape(footprint_geojson)
    if fp_ll.is_empty:
        return None

    w, h = conv.image_size
    c = fp_ll.centroid
    frame = LocalFrame(c.x, c.y)
    # A building in several parts (wings, a relation) is fitted as a whole.
    fp = shapely.transform(fp_ll, lambda c: np.column_stack(frame.to_local(c[:, 0], c[:, 1])))
    if not fp.is_valid:
        fp = fp.buffer(0)

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
        if prior is not None:
            turn = PRIOR_WEIGHT * _angle_between(bearing, prior[0]) / 180
        else:
            turn = NORTH_UP_WEIGHT * _angle_from_north(bearing) / 180
        return (1 - iou) + ANISOTROPY_WEIGHT * (lsx - lsy) ** 2 + turn

    results: list[tuple[float, float, np.ndarray, bool]] = []
    for mirror in (False, True) if allow_mirror else (False,):
        base = (-phi_plan if not mirror else phi_plan - 180)
        starts = [(phi_fp + base + 90 * k, scale0) for k in range(4)]
        if prior is not None and not mirror:
            starts.append((-prior[0], prior[1] if prior[1] and prior[1] > 0 else scale0))
        for phi, sc in starts:
            # Translate so the plan's centroid lands on the footprint's.
            g = affinity.scale(pc, (-1 if mirror else 1) * sc, sc, origin=(0, 0))
            g = affinity.rotate(g, phi, origin=(0, 0))
            cx, cy = g.x, g.y
            x0 = np.array([phi, math.log(sc), math.log(sc), -cx, -cy])
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
    # Other orientations: one per distinct rotation (the prior's start may land on one
    # of the others).
    alternatives: list[tuple[float, float]] = []
    for r in results[1:]:
        rot = round((-r[2][0]) % 360, 2)
        if _angle_between(rot, placement.rotation_deg) < 5 or any(_angle_between(rot, a[1]) < 5 for a in alternatives):
            continue
        alternatives.append((round(r[1], 4), rot))
    alternatives.sort(key=lambda a: -a[0])
    agrees = prior is not None and _angle_between(placement.rotation_deg, prior[0]) <= PRIOR_AGREES_DEG
    return FitResult(placement=placement, iou=round(best_iou, 4), alternatives=alternatives, agrees_with_prior=agrees)


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
        g = shape(footprint_geojson)
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
