"""Building outlines from OpenStreetMap via Overpass."""

from __future__ import annotations

from typing import Any

from shapely.geometry import Point, Polygon, mapping

from .. import http
from ..config import get_settings


def _way_polygon(element: dict[str, Any]) -> Polygon | None:
    pts = [(n["lon"], n["lat"]) for n in element.get("geometry") or []]
    if len(pts) < 4:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 0 or poly.geom_type != "Polygon":
        return None
    return poly


def buildings_near(lat: float, lon: float, radius_m: float = 60) -> list[dict[str, Any]]:
    """Building ways within ``radius_m`` of a point, nearest first.

    Each item is ``{"osm_id", "name", "footprint", "tags", "contains"}`` where
    ``contains`` says whether the point lies inside the outline.
    """
    query = f"""
    [out:json][timeout:25];
    way(around:{radius_m:.0f},{lat},{lon})[building];
    out tags geom;
    """
    data = http.post_form_json(get_settings().overpass_url, {"data": query}, min_interval=2.0)
    pt = Point(lon, lat)
    out = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        poly = _way_polygon(el)
        if poly is None:
            continue
        tags = el.get("tags") or {}
        out.append(
            {
                "osm_id": f"way/{el['id']}",
                "name": tags.get("name"),
                "footprint": mapping(poly),
                "tags": tags,
                "contains": poly.contains(pt),
                "_dist": poly.distance(pt),
            }
        )
    out.sort(key=lambda b: (not b["contains"], b["_dist"]))
    for b in out:
        b.pop("_dist")
    return out


def footprint_at(lat: float, lon: float) -> dict[str, Any] | None:
    """The building containing (or nearest to) a point, or ``None``."""
    found = buildings_near(lat, lon)
    return found[0] if found else None
