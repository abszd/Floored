"""Address and building-name geocoding through OpenStreetMap.

Nominatim is tried first because it returns building outlines. It is strict
about spelling, so a full address with a unit number or ZIP code it doesn't
know can come back empty; we then retry simpler forms of the query, and
finally ask Photon, a more forgiving geocoder over the same OSM data.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import shape

from .. import http
from ..config import get_settings

log = logging.getLogger(__name__)

# OSM classes/types that describe a building rather than a street or area.
_BUILDING_CLASSES = {"building", "amenity", "tourism", "historic", "leisure", "office", "shop"}
# Types whose polygon is usually a whole site (a campus, a park), not one building.
_SITE_KINDS = {"university", "college", "school", "kindergarten", "campus", "hospital", "park", "zoo", "theme_park"}
# Above this a polygon that isn't tagged as a building is taken to be a site.
_MAX_UNTAGGED_BUILDING_M2 = 40_000

_UNIT = re.compile(r"\b(?:suite|ste|room|rm|unit|apt|floor|fl)\.?\s*#?\s*[\w-]+|#\s*[\w-]+", re.I)
_POSTCODE = re.compile(r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b")
_COUNTRY = re.compile(r",\s*(?:usa|u\.s\.a\.|us|united states(?: of america)?|canada)\s*$", re.I)
_HOUSE_NUMBER = re.compile(r"^\d{1,6}[a-z]?\s")


def _area_m2(geojson: dict[str, Any]) -> float:
    try:
        geom = shape(geojson)
    except Exception:
        return 0.0
    lat = geom.centroid.y
    return geom.area * (111_320.0**2) * math.cos(math.radians(lat))


@dataclass
class Place:
    name: str
    display_name: str
    lat: float
    lon: float
    osm_type: str | None = None
    osm_id: str | None = None
    category: str | None = None
    kind: str | None = None
    importance: float = 0.0
    bbox: tuple[float, float, float, float] | None = None  # min_lon, min_lat, max_lon, max_lat
    footprint: dict[str, Any] | None = None
    address: dict[str, str] = field(default_factory=dict)

    @property
    def is_building(self) -> bool:
        """Whether ``footprint`` is the outline of one building (not a campus or a park)."""
        if self.footprint is None or self.footprint.get("type") not in ("Polygon", "MultiPolygon"):
            return False
        if self.category == "building":
            return True
        return self.kind not in _SITE_KINDS and _area_m2(self.footprint) < _MAX_UNTAGGED_BUILDING_M2

    @property
    def osm_ref(self) -> str | None:
        return f"{self.osm_type}/{self.osm_id}" if self.osm_type and self.osm_id else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "lat": self.lat,
            "lon": self.lon,
            "osm_id": self.osm_ref,
            "category": self.category,
            "kind": self.kind,
            "bbox": self.bbox,
            "footprint": self.footprint,
            "address": self.address,
        }


def _parse(item: dict[str, Any]) -> Place:
    bb = item.get("boundingbox")
    bbox = (float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])) if bb else None
    geo = item.get("geojson")
    return Place(
        name=item.get("name") or item.get("display_name", "").split(",")[0],
        display_name=item.get("display_name", ""),
        lat=float(item["lat"]),
        lon=float(item["lon"]),
        osm_type=item.get("osm_type"),
        osm_id=str(item["osm_id"]) if item.get("osm_id") is not None else None,
        category=item.get("category") or item.get("class"),
        kind=item.get("type"),
        importance=float(item.get("importance") or 0.0),
        bbox=bbox,
        footprint=geo if geo and geo.get("type") in ("Polygon", "MultiPolygon") else None,
        address=item.get("address") or {},
    )


def query_variants(query: str) -> list[str]:
    """Simpler spellings of an address, most specific first, for retries.

    "Memorial Union, 1400 Townsend Dr, Suite 2, Houghton, MI 49931, USA"
    becomes, in turn, the address without the unit, ZIP and country; without
    the building name in front of the street; and the street without its
    house number (often missing from OSM).
    """
    out: list[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s*,\s*(?:,\s*)*", ", ", re.sub(r"\s+", " ", q)).strip(" ,")
        if q and q.lower() != query.strip().lower() and q.lower() not in (o.lower() for o in out):
            out.append(q)

    base = _COUNTRY.sub("", _POSTCODE.sub("", _UNIT.sub("", query)))
    add(base)
    parts = [p.strip() for p in base.split(",") if p.strip()]
    street = next((i for i, p in enumerate(parts) if _HOUSE_NUMBER.match(p)), None)
    if street:
        parts = parts[street:]
        add(", ".join(parts))
    if street is not None and len(parts) > 1:
        add(", ".join([_HOUSE_NUMBER.sub("", parts[0]), *parts[1:]]))
    return out[:3]


def search(query: str, *, limit: int = 8, viewbox: tuple[float, float, float, float] | None = None) -> list[Place]:
    """Free-text search for an address or a building name.

    Results with a building outline are ranked first. Raises only when every
    geocoder failed outright; an empty list means nothing was found.
    """
    error: Exception | None = None
    for q in [query, *query_variants(query)]:
        try:
            places = _nominatim(q, limit, viewbox)
        except Exception as exc:  # outage, rate limit, blocked User-Agent
            log.warning("nominatim failed for %r: %s", q, exc)
            error = error or exc
            break
        if places:
            return places
    if get_settings().photon_url:
        try:
            places = _photon(query, limit, viewbox)
            if places:
                return places
        except Exception as exc:
            log.warning("photon failed for %r: %s", query, exc)
    if error:
        raise error
    return []


def _rank(places: list[Place]) -> list[Place]:
    return sorted(places, key=lambda p: (not p.is_building, p.category not in _BUILDING_CLASSES, -p.importance))


def _nominatim(query: str, limit: int, viewbox: tuple[float, float, float, float] | None) -> list[Place]:
    s = get_settings()
    params: dict[str, Any] = {
        "q": query,
        "format": "jsonv2",
        "polygon_geojson": 1,
        "addressdetails": 1,
        "limit": limit,
    }
    if viewbox:
        params["viewbox"] = ",".join(str(v) for v in viewbox)
    data = http.get_json(f"{s.nominatim_url}/search", params, min_interval=1.0)
    return _rank([_parse(item) for item in data])


_PHOTON_TYPES = {"N": "node", "W": "way", "R": "relation"}


def suggest(query: str, *, near: tuple[float, float] | None = None, limit: int = 6) -> list[Place]:
    """Address and place suggestions while the user is still typing.

    Photon is built for search-as-you-type (Nominatim's usage policy forbids
    it). ``near`` is a (lat, lon) that results close to are preferred, not
    limited to.
    """
    if not get_settings().photon_url or len(query.strip()) < 3:
        return []
    return _photon(query, limit, None, near=near, min_interval=0.2, rank=False)


def _photon(
    query: str,
    limit: int,
    viewbox: tuple[float, float, float, float] | None,
    *,
    near: tuple[float, float] | None = None,
    min_interval: float = 1.0,
    rank: bool = True,
) -> list[Place]:
    """Photon returns points only; the building outline is looked up afterwards."""
    params: dict[str, Any] = {"q": query, "limit": limit, "lang": "en"}
    if viewbox:
        params["bbox"] = ",".join(str(v) for v in viewbox)
    if near:
        params["lat"], params["lon"] = round(near[0], 3), round(near[1], 3)
    data = http.get_json(f"{get_settings().photon_url}/api", params, min_interval=min_interval)
    places = []
    for f in data.get("features", []):
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        street = " ".join(filter(None, [props.get("housenumber"), props.get("street")]))
        parts = [props.get("name"), street, props.get("city"), props.get("state"), props.get("postcode"), props.get("country")]
        ext = props.get("extent")  # [min_lon, max_lat, max_lon, min_lat]
        places.append(
            Place(
                name=props.get("name") or street,
                display_name=", ".join(dict.fromkeys(p for p in parts if p)),
                lat=float(coords[1]),
                lon=float(coords[0]),
                osm_type=_PHOTON_TYPES.get(props.get("osm_type", ""), None),
                osm_id=str(props["osm_id"]) if props.get("osm_id") is not None else None,
                category=props.get("osm_key"),
                kind=props.get("osm_value"),
                bbox=(ext[0], ext[3], ext[2], ext[1]) if ext and len(ext) == 4 else None,
                address={k: str(props[k]) for k in ("housenumber", "street", "city", "state", "postcode", "country") if props.get(k)},
            )
        )
    return _rank(places) if rank else places


def reverse(lat: float, lon: float) -> Place | None:
    s = get_settings()
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "polygon_geojson": 1, "addressdetails": 1, "zoom": 18}
    data = http.get_json(f"{s.nominatim_url}/reverse", params, min_interval=1.0)
    if not data or "lat" not in data:
        return None
    return _parse(data)
