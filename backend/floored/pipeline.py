"""The end-to-end flow: scraped image -> building match -> rooms -> placed GeoJSON."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import jobs, storage
from .config import get_settings
from .converters import ConversionResult, get_converter
from .db import session_scope
from .geo import matching
from .models import Building, Candidate, Floor, Source
from .placement.fit import default_placement, fit_to_footprint
from .placement.geojson import floor_geojson
from .placement.transform import Placement
from .scraper.crawler import CrawlConfig, crawl

log = logging.getLogger(__name__)

# Below this footprint overlap a placement is shown but flagged for review.
GOOD_FIT_IOU = 0.75


# ---------------------------------------------------------------- buildings


def building_from_place(s: Session, place: dict[str, Any], name: str | None = None) -> Building:
    """Get or create the building for a geocoder result."""
    osm_id = place.get("osm_id")
    if osm_id:
        existing = s.scalar(select(Building).where(Building.osm_id == osm_id))
        if existing:
            if name and name != existing.name and name not in existing.aliases:
                existing.aliases = [*existing.aliases, name]
            return existing
    footprint = place.get("footprint")
    lat, lon = place["lat"], place["lon"]
    if footprint:
        from shapely.geometry import shape

        c = shape(footprint).centroid
        lon, lat = c.x, c.y
    b = Building(
        name=name or place.get("name") or place.get("display_name", "Unnamed building").split(",")[0],
        address=place.get("display_name"),
        lat=lat,
        lon=lon,
        footprint=footprint,
        osm_id=osm_id,
        aliases=[],
    )
    s.add(b)
    s.flush()
    return b


def known_buildings(s: Session) -> list[matching.KnownBuilding]:
    return [
        matching.KnownBuilding(id=b.id, name=b.name, aliases=b.aliases or [], osm_id=b.osm_id, address=b.address)
        for b in s.scalars(select(Building))
    ]


# ---------------------------------------------------------------- floors


def _conversion(floor: Floor) -> ConversionResult:
    assert floor.conversion is not None
    return ConversionResult.from_dict(floor.conversion)


def refresh_geojson(floor: Floor) -> None:
    if floor.conversion is None or floor.placement is None:
        floor.geojson = None
        return
    floor.geojson = floor_geojson(
        _conversion(floor),
        Placement.from_dict(floor.placement),
        floor_id=floor.id,
        building_id=floor.building_id,
        level=floor.level,
    )


def autoplace(floor: Floor, building: Building) -> dict[str, Any]:
    """Fit the plan onto the building footprint, or fall back to a sized guess."""
    conv = _conversion(floor)
    if building.footprint:
        fit = fit_to_footprint(conv, building.footprint)
        if fit is not None:
            floor.placement = fit.placement.to_dict()
            floor.fit_score = fit.iou
            ok = fit.iou >= GOOD_FIT_IOU and not fit.ambiguous
            floor.status = "placed" if ok else "needs_review"
            refresh_geojson(floor)
            return fit.to_dict()
    floor.placement = default_placement(
        conv, building.lon, building.lat, footprint_geojson=building.footprint
    ).to_dict()
    floor.fit_score = None
    floor.status = "needs_review"
    refresh_geojson(floor)
    return {"placement": floor.placement, "iou": None, "ambiguous": True, "alternatives": []}


def process_floor(floor_id: str, converter: str | None = None, keep_placement: bool = False) -> dict[str, Any]:
    """Run the converter on a floor's image and place the result."""
    with session_scope() as s:
        floor = s.get(Floor, floor_id)
        if floor is None:
            raise LookupError(f"floor {floor_id} not found")
        if not floor.image_path:
            raise ValueError("floor has no image")
        floor.status = "processing"
        floor.error = None
        image_path = floor.image_path
    try:
        conv_impl = get_converter(converter)
        conv = conv_impl.convert(storage.read_bytes(image_path))
    except Exception as exc:
        with session_scope() as s:
            floor = s.get(Floor, floor_id)
            floor.status = "error"
            floor.error = f"conversion failed: {exc}"
        raise

    data = conv.to_dict()
    debug = {name: storage.save_bytes(png, f"debug/{floor_id}", f"-{name}.png") for name, png in conv.debug_images.items()}
    data["meta"] = {**data.get("meta", {}), "debug_images": debug}
    with session_scope() as s:
        floor = s.get(Floor, floor_id)
        floor.conversion = data
        floor.converter = conv_impl.name
        floor.image_width, floor.image_height = conv.image_size
        if keep_placement and floor.placement:
            floor.status = "placed"
            refresh_geojson(floor)
            fit = None
        else:
            fit = autoplace(floor, floor.building)
        return {"floor_id": floor_id, "rooms": len(conv.rooms), "fit": fit, "status": floor.status}


def create_floor(
    s: Session,
    building: Building,
    image: bytes,
    *,
    level: float | None,
    name: str | None = None,
    source_url: str | None = None,
) -> Floor:
    level = 1.0 if level is None else float(level)
    floor = None
    if source_url:
        floor = s.scalar(
            select(Floor).where(Floor.building_id == building.id, Floor.source_url == source_url, Floor.level == level)
        )
    if floor is None:
        floor = Floor(building_id=building.id, level=level, name=name or level_name(level))
        s.add(floor)
    floor.image_path = storage.save_bytes(image, "plans", ".png" if image[:4] == b"\x89PNG" else ".img")
    floor.source_url = source_url
    floor.status = "pending"
    s.flush()
    return floor


def level_name(level: float) -> str:
    n = int(level)
    if n < 0:
        return "Basement" if n == -1 else f"Basement {-n}"
    if n == 0:
        return "Ground floor"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} floor"


# ---------------------------------------------------------------- candidates


def match_candidate(candidate_id: str) -> None:
    with session_scope() as s:
        c = s.get(Candidate, candidate_id)
        hint = c.source.location_hint if c.source else None
        known = known_buildings(s)
        name, address = c.building_guess, c.address_guess
    try:
        m = matching.resolve(name, address, hint, known)
    except Exception as exc:  # geocoder outage etc.
        log.warning("matching %s failed: %s", candidate_id, exc)
        m = None
    with session_scope() as s:
        c = s.get(Candidate, candidate_id)
        if m is None:
            c.status = "unmatched"
            c.match_confidence = 0
            c.match_reason = "no building found for this name or address"
            return
        if m.is_new:
            b = building_from_place(s, m.place, name=None)
            if name and matching.name_similarity(name, b.name) < 0.88 and name not in (b.aliases or []):
                b.aliases = [*(b.aliases or []), name]
            c.building_id = b.id
        else:
            c.building_id = m.building_id
        c.match_confidence = m.confidence
        c.match_reason = m.reason
        c.status = "matched"


def approve_candidate(candidate_id: str, building_id: str | None = None, level: float | None = None) -> str:
    """Turn a candidate into a floor and process it. Returns the floor id."""
    with session_scope() as s:
        c = s.get(Candidate, candidate_id)
        if building_id:
            c.building_id = building_id
        if level is not None:
            c.level_guess = level
        if not c.building_id:
            raise ValueError("candidate has no building; choose one first")
        b = s.get(Building, c.building_id)
        floor = create_floor(
            s,
            b,
            storage.read_bytes(c.image_path),
            level=c.level_guess,
            name=c.level_label,
            source_url=f"{c.asset_url}#page={c.page_index + 1}" if c.page_index else c.asset_url,
        )
        c.floor_id = floor.id
        c.status = "approved"
        floor_id = floor.id
    try:
        process_floor(floor_id)
    except Exception as exc:
        with session_scope() as s:
            c = s.get(Candidate, candidate_id)
            c.status = "error"
            c.error = str(exc)[:500]
        raise
    with session_scope() as s:
        s.get(Candidate, candidate_id).status = "processed"
    return floor_id


# ---------------------------------------------------------------- jobs


@jobs.handler("crawl")
def _crawl_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    source_id = payload["source_id"]
    with session_scope() as s:
        src = s.get(Source, source_id)
        cfg = CrawlConfig(src.seed_urls, src.allowed_domains or [], src.max_pages, src.max_depth)
        skip = {c.asset_url for c in src.candidates}
    new_ids: list[str] = []
    for found in crawl(cfg, skip_assets=skip, progress=progress):
        with session_scope() as s:
            c = Candidate(
                source_id=source_id,
                asset_url=found.asset_url,
                page_index=found.page_index,
                page_url=found.page_url,
                page_title=found.page_title,
                context=found.context,
                image_path=storage.save_bytes(found.png, "candidates", ".png"),
                plan_score=found.plan_score,
                building_guess=found.building_guess,
                address_guess=found.address_guess,
                level_guess=found.level,
                level_label=found.level_label,
            )
            s.add(c)
            s.flush()
            new_ids.append(c.id)
    with session_scope() as s:
        s.get(Source, source_id).last_crawled_at = datetime.now(timezone.utc)

    threshold = get_settings().auto_approve_confidence
    processed = 0
    for i, cid in enumerate(new_ids):
        progress(0.9 + 0.1 * i / max(len(new_ids), 1), f"Matching plan {i + 1} of {len(new_ids)} to a building")
        match_candidate(cid)
        with session_scope() as s:
            c = s.get(Candidate, cid)
            auto = c.status == "matched" and c.match_confidence >= threshold and c.plan_score >= 0.6
        if auto:
            try:
                approve_candidate(cid)
                processed += 1
            except Exception as exc:
                log.warning("auto-processing %s failed: %s", cid, exc)
    return {"found": len(new_ids), "auto_processed": processed}


@jobs.handler("process_floor")
def _process_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    progress(0.1, "Finding rooms")
    return process_floor(payload["floor_id"], payload.get("converter"), payload.get("keep_placement", False))


@jobs.handler("approve_candidate")
def _approve_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    progress(0.1, "Finding rooms")
    floor_id = approve_candidate(payload["candidate_id"], payload.get("building_id"), payload.get("level"))
    return {"floor_id": floor_id}


@jobs.handler("match_candidates")
def _match_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    ids = payload["candidate_ids"]
    for i, cid in enumerate(ids):
        progress(i / max(len(ids), 1), f"Matching {i + 1} of {len(ids)}")
        match_candidate(cid)
    return {"matched": len(ids)}
