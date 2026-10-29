"""Turn a placed conversion into GeoJSON for the map."""

from __future__ import annotations

from typing import Any

from ..converters.base import ConversionResult
from .transform import Placement


def floor_geojson(
    conv: ConversionResult,
    placement: Placement,
    *,
    floor_id: str | None = None,
    building_id: str | None = None,
    level: float | None = None,
) -> dict[str, Any]:
    w, h = conv.image_size
    mx, my = placement.metres_per_pixel(w, h)
    common = {"floor_id": floor_id, "building_id": building_id, "level": level}
    features: list[dict[str, Any]] = []

    if conv.outline:
        features.append(
            {
                "type": "Feature",
                "id": f"{floor_id}:outline" if floor_id else "outline",
                "geometry": {"type": "Polygon", "coordinates": [placement.ring_to_lonlat(conv.outline, w, h)]},
                "properties": {**common, "feature": "outline"},
            }
        )

    for room in conv.rooms:
        rings = [placement.ring_to_lonlat(room.polygon, w, h)]
        rings += [placement.ring_to_lonlat(hole, w, h) for hole in room.holes if len(hole) >= 3]
        features.append(
            {
                "type": "Feature",
                "id": f"{floor_id}:{room.id}" if floor_id else room.id,
                "geometry": {"type": "Polygon", "coordinates": rings},
                "properties": {
                    **common,
                    "feature": "room",
                    "room_id": room.id,
                    "label": room.label,
                    "kind": room.kind,
                    "area_m2": round(room.area_px * mx * my, 1),
                    "confidence": room.confidence,
                },
            }
        )

    for kind, lines in (("wall", conv.walls), ("door", conv.doors)):
        for i, line in enumerate(lines):
            if len(line) < 2:
                continue
            features.append(
                {
                    "type": "Feature",
                    "id": f"{floor_id}:{kind}{i}" if floor_id else f"{kind}{i}",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(placement.pixel_to_lonlat(p[0], p[1], w, h)) for p in line],
                    },
                    "properties": {**common, "feature": kind},
                }
            )

    return {"type": "FeatureCollection", "features": features}
