"""Address and building-name geocoding through Nominatim (OpenStreetMap)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import http
from ..config import get_settings

# OSM classes/types that describe a building rather than a street or area.
_BUILDING_CLASSES = {"building", "amenity", "tourism", "historic", "leisure", "office", "shop"}


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
        return self.footprint is not None and self.footprint.get("type") in ("Polygon", "MultiPolygon")

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


def search(query: str, *, limit: int = 8, viewbox: tuple[float, float, float, float] | None = None) -> list[Place]:
    """Free-text search. Results with a building polygon are ranked first."""
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
    places = [_parse(item) for item in data]
    places.sort(key=lambda p: (not p.is_building, p.category not in _BUILDING_CLASSES, -p.importance))
    return places


def reverse(lat: float, lon: float) -> Place | None:
    s = get_settings()
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "polygon_geojson": 1, "addressdetails": 1, "zoom": 18}
    data = http.get_json(f"{s.nominatim_url}/reverse", params, min_interval=1.0)
    if not data or "lat" not in data:
        return None
    return _parse(data)
