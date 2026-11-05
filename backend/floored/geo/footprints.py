"""Building outlines from OpenStreetMap via Overpass."""

from __future__ import annotations

import re
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

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


def _relation_polygon(element: dict[str, Any]) -> Polygon | MultiPolygon | None:
    """A multipolygon relation's outline: outer rings minus inner ones, from member way geometry.

    Relations are how OSM maps buildings with courtyards or several separate
    parts; member ways may each be a piece of a ring.
    """
    rings: dict[str, list[list[tuple[float, float]]]] = {"outer": [], "inner": []}
    for m in element.get("members") or []:
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        role = "inner" if m.get("role") == "inner" else "outer"
        rings[role].append([(n["lon"], n["lat"]) for n in m["geometry"]])
    if not rings["outer"]:
        return None
    outer = unary_union(list(polygonize(linemerge(rings["outer"]))))
    if outer.is_empty:
        return None
    if rings["inner"]:
        inner = unary_union(list(polygonize(linemerge(rings["inner"]))))
        outer = outer.difference(inner)
    if not outer.is_valid:
        outer = outer.buffer(0)
    if outer.is_empty or outer.area <= 0 or outer.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return outer


def _query(body: str) -> list[dict[str, Any]]:
    data = http.post_form_json(get_settings().overpass_url, {"data": f"[out:json][timeout:25];{body}"}, min_interval=2.0)
    return data.get("elements", [])


def _buildings(elements: list[dict[str, Any]], lat: float, lon: float) -> list[dict[str, Any]]:
    """Building ways as dicts, the ones containing (lat, lon) first, then nearest first."""
    pt = Point(lon, lat)
    out = []
    seen = set()
    for el in elements:
        kind = el.get("type")
        if kind not in ("way", "relation") or (kind, el["id"]) in seen:
            continue
        seen.add((kind, el["id"]))
        poly = _way_polygon(el) if kind == "way" else _relation_polygon(el)
        if poly is None:
            continue
        tags = el.get("tags") or {}
        out.append(
            {
                "osm_id": f"{kind}/{el['id']}",
                "name": tags.get("name"),
                "footprint": mapping(poly),
                "tags": tags,
                "contains": poly.contains(pt),
                "_dist": poly.distance(pt),
            }
        )
    out.sort(key=lambda b: (not b["contains"], b["_dist"]))
    for b in out:
        b["distance_m"] = round(b.pop("_dist") * 111_320.0, 1)
    return out


def buildings_near(lat: float, lon: float, radius_m: float = 60) -> list[dict[str, Any]]:
    """Building ways within ``radius_m`` of a point, nearest first.

    Each item is ``{"osm_id", "name", "footprint", "tags", "contains", "distance_m"}``
    where ``contains`` says whether the point lies inside the outline.
    """
    around = f"(around:{radius_m:.0f},{lat},{lon})"
    return _buildings(_query(f"(way{around}[building];rel{around}[building][type=multipolygon];);out tags geom;"), lat, lon)


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def building_at_address(housenumber: str, street: str, lat: float, lon: float, radius_m: float = 1500) -> dict[str, Any] | None:
    """The building carrying this street address near (lat, lon).

    OSM puts addresses either on the building itself or on a point inside it,
    so both are searched; the street name only has to share its main word.
    """
    words = [w for w in re.findall(r"[A-Za-z]{3,}", street) if w.lower() not in _STREET_TYPES]
    if not words:
        return None
    street_re = _quote(re.escape(max(words, key=len)))
    hn = _quote(housenumber.strip())
    around = f"(around:{radius_m:.0f},{lat},{lon})"
    elements = _query(
        f'(way{around}[building]["addr:housenumber"="{hn}"]["addr:street"~"{street_re}",i];'
        f'rel{around}[building]["addr:housenumber"="{hn}"]["addr:street"~"{street_re}",i];'
        f'node{around}["addr:housenumber"="{hn}"]["addr:street"~"{street_re}",i];);out tags geom;'
    )
    found = _buildings(elements, lat, lon)
    if found:
        return found[0]
    for el in elements:
        if el.get("type") == "node":
            inside = [b for b in buildings_near(el["lat"], el["lon"], 30) if b["contains"]]
            if inside:
                return inside[0]
    return None


def buildings_named(name: str, lat: float, lon: float, radius_m: float = 2000) -> list[dict[str, Any]]:
    """Buildings near (lat, lon) whose name (or alternative name) contains the main words of ``name``."""
    words = [w for w in re.findall(r"[A-Za-z0-9]{2,}", name) if w.lower() not in _NAME_FILLER][:3]
    if not words:
        return []
    pattern = _quote(".*".join(re.escape(w) for w in words))
    around = f"(around:{radius_m:.0f},{lat},{lon})"
    keys = ("name", "alt_name", "official_name", "short_name")
    body = "(" + "".join(f'{t}{around}[building]["{k}"~"{pattern}",i];' for k in keys for t in ("way", "rel")) + ");out tags geom;"
    return _buildings(_query(body), lat, lon)


_STREET_TYPES = {
    "street", "avenue", "ave", "road", "drive", "lane", "way", "place", "court", "boulevard", "blvd",
    "parkway", "pkwy", "highway", "hwy", "circle", "terrace", "square", "north", "south", "east", "west",
}
_NAME_FILLER = {"the", "of", "and", "building", "bldg", "hall", "center", "centre"}


def footprint_at(lat: float, lon: float) -> dict[str, Any] | None:
    """The building containing (or nearest to) a point, or ``None``."""
    found = buildings_near(lat, lon)
    return found[0] if found else None


def combine(*footprints: dict[str, Any] | None) -> dict[str, Any] | None:
    """One outline from several (a building mapped as separate wings). Touching parts become one polygon."""
    from shapely.geometry import shape

    geoms = [shape(f) for f in footprints if f]
    if not geoms:
        return None
    merged = unary_union([g if g.is_valid else g.buffer(0) for g in geoms])
    # Wings drawn a hair apart still read as one building.
    closed = merged.buffer(1e-6, join_style=2).buffer(-1e-6, join_style=2)
    return mapping(closed if not closed.is_empty else merged)


def remove_part(footprint: dict[str, Any], lat: float, lon: float) -> dict[str, Any] | None:
    """The outline without the separate part at (lat, lon); ``None`` when nothing would be left."""
    from shapely.geometry import shape

    g = shape(footprint)
    parts = list(g.geoms) if isinstance(g, MultiPolygon) else [g]
    pt = Point(lon, lat)
    keep = [p for p in parts if not p.buffer(1e-6).contains(pt)]
    if len(keep) == len(parts):
        raise LookupError("no part of the outline there")
    if not keep:
        return None
    return mapping(keep[0] if len(keep) == 1 else MultiPolygon(keep))
