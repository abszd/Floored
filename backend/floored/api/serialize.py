"""Shape database records for the API."""

from __future__ import annotations

from typing import Any

from ..models import Building, Candidate, Floor, Job, Source
from ..placement.transform import Placement


def media_url(rel: str | None) -> str | None:
    return f"/media/{rel}" if rel else None


def floor_summary(f: Floor) -> dict[str, Any]:
    return {
        "id": f.id,
        "building_id": f.building_id,
        "name": f.name,
        "level": f.level,
        "status": f.status,
        "error": f.error,
        "fit_score": f.fit_score,
        "room_count": len((f.conversion or {}).get("rooms") or []),
        "image_url": media_url(f.image_path),
        "source_url": f.source_url,
        "updated_at": f.updated_at,
    }


def floor_detail(f: Floor) -> dict[str, Any]:
    out = floor_summary(f)
    conv = f.conversion or {}
    debug = (conv.get("meta") or {}).get("debug_images") or {}
    out.update(
        {
            "converter": f.converter,
            "image_size": [f.image_width, f.image_height] if f.image_width else None,
            "placement": f.placement,
            "corners": (
                Placement.from_dict(f.placement).corners(f.image_width, f.image_height)
                if f.placement and f.image_width
                else None
            ),
            "debug_images": {k: media_url(v) for k, v in debug.items()},
            "geojson": f.geojson,
        }
    )
    return out


def building_summary(b: Building) -> dict[str, Any]:
    floors = b.floors
    return {
        "id": b.id,
        "name": b.name,
        "address": b.address,
        "lat": b.lat,
        "lon": b.lon,
        "osm_id": b.osm_id,
        "aliases": b.aliases or [],
        "has_footprint": b.footprint is not None,
        "floor_count": len(floors),
        "placed_count": sum(1 for f in floors if f.status == "placed"),
        "review_count": sum(1 for f in floors if f.status == "needs_review"),
    }


def building_detail(b: Building) -> dict[str, Any]:
    out = building_summary(b)
    out["footprint"] = b.footprint
    out["floors"] = [floor_summary(f) for f in sorted(b.floors, key=lambda f: f.level)]
    return out


def building_feature(b: Building) -> dict[str, Any]:
    geom = b.footprint or {"type": "Point", "coordinates": [b.lon, b.lat]}
    s = building_summary(b)
    return {
        "type": "Feature",
        "id": b.id,
        "geometry": geom,
        "properties": {k: s[k] for k in ("id", "name", "address", "floor_count", "placed_count", "review_count")},
    }


def source_out(src: Source) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for c in src.candidates:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {
        "id": src.id,
        "name": src.name,
        "seed_urls": src.seed_urls,
        "allowed_domains": src.allowed_domains,
        "location_hint": src.location_hint,
        "max_pages": src.max_pages,
        "max_depth": src.max_depth,
        "last_crawled_at": src.last_crawled_at,
        "candidate_counts": counts,
    }


def candidate_out(c: Candidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "source_id": c.source_id,
        "asset_url": c.asset_url,
        "page_index": c.page_index,
        "page_url": c.page_url,
        "page_title": c.page_title,
        "context": c.context,
        "image_url": media_url(c.image_path),
        "plan_score": c.plan_score,
        "building_guess": c.building_guess,
        "address_guess": c.address_guess,
        "level_guess": c.level_guess,
        "level_label": c.level_label,
        "building_id": c.building_id,
        "building_name": None,
        "match_confidence": c.match_confidence,
        "match_reason": c.match_reason,
        "status": c.status,
        "floor_id": c.floor_id,
        "error": c.error,
        "created_at": c.created_at,
    }


def job_out(j: Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "kind": j.kind,
        "status": j.status,
        "progress": j.progress,
        "message": j.message,
        "payload": j.payload,
        "result": {k: v for k, v in (j.result or {}).items() if k != "traceback"},
        "created_at": j.created_at,
        "finished_at": j.finished_at,
    }
