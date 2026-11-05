"""HTTP API. Run with ``uvicorn floored.api.app:app`` or ``floored serve``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import editing, http, jobs, pipeline, storage
from ..config import get_settings
from ..converters import ConversionResult, available_converters
from ..converters.base import ROOM_KINDS
from ..db import get_session, init_db
from ..geo import footprints, geocoder, matching
from ..models import Building, Candidate, Draft, Floor, Job, Source
from ..placement.fit import fit_to_footprint
from ..placement.transform import Placement
from ..scraper import documents
from . import serialize as ser

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    jobs.recover()
    yield


app = FastAPI(title="Floored", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# scope="function" commits before the response goes out. With the default the
# commit runs after it, so a client that creates something and immediately uses
# it ("add website" then "crawl") could get a 404 for the thing it just made.
DB = Depends(get_session, scope="function")


def _get(s: Session, model: Any, id_: str) -> Any:
    obj = s.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__.lower()} not found")
    return obj


# ------------------------------------------------------------------ misc


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "converters": available_converters(), "default_converter": get_settings().converter}


@app.get("/api/geocode")
def geocode(q: str = Query(min_length=2), hint: str | None = None) -> list[dict[str, Any]]:
    """Places matching ``q``; with a ``hint`` ("Houghton, MI"), results there come first."""
    try:
        places = geocoder.search(q)
        if hint and hint.lower() not in q.lower():
            area = next(iter(geocoder.search(hint, limit=1)), None)
            if area is not None:
                places.sort(key=lambda p: not matching._in_area(p.lat, p.lon, area))
        return [p.to_dict() for p in places]
    except Exception as exc:
        raise HTTPException(502, _geocoder_error(exc)) from exc


@app.get("/api/suggest")
def suggest(q: str = "", hint: str | None = None, lat: float | None = None, lon: float | None = None) -> list[dict[str, Any]]:
    """Suggestions for an address box as the user types, nearest to ``hint`` (a town) or lat/lon first."""
    near = (lat, lon) if lat is not None and lon is not None else None
    try:
        if near is None and hint:
            area = next(iter(geocoder.search(hint, limit=1)), None)
            near = (area.lat, area.lon) if area else None
        places = geocoder.suggest(q, near=near)
    except Exception as exc:
        log.warning("suggestions failed for %r: %s", q, exc)
        return []  # typing should never show an error; the search button still works
    return [p.to_dict() for p in places]


class LookupIn(BaseModel):
    name: str | None = None
    address: str | None = None
    hint: str | None = None
    # A point the user picked from the address suggestions: the building there is used.
    lat: float | None = None
    lon: float | None = None
    # Leave this building out of the "already have it" check (when moving it).
    exclude_building_id: str | None = None


@app.post("/api/lookup")
def lookup(body: LookupIn, s: Session = DB) -> dict[str, Any]:
    """Which building a name and/or address points to, without creating anything.

    Lets the UI show the building on a map before the user commits to it.
    """
    if body.lat is not None and body.lon is not None:
        return _lookup_point(body, s)
    if not (body.name or body.address):
        raise HTTPException(422, "give a name or an address")
    try:
        geocoder.search(body.address or body.name, limit=1)  # surfaces an outage as an outage
    except Exception as exc:
        raise HTTPException(502, _geocoder_error(exc)) from exc
    known = [b for b in pipeline.known_buildings(s) if b.id != body.exclude_building_id]
    m = matching.resolve(body.name, body.address, body.hint, known)
    if m is None:
        raise HTTPException(404, "Couldn't find that on OpenStreetMap. Try adding the town, or click the building on the map.")
    existing = s.get(Building, m.building_id) if m.building_id else None
    return {
        "confidence": m.confidence,
        "reason": m.reason,
        "building": ser.building_detail(existing) if existing else None,
        "place": m.place,
    }


def _geocoder_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 429):
        return (
            f"OpenStreetMap's geocoder refused the request ({exc.response.status_code}). "
            "Set FLOORED_USER_AGENT to something that identifies you, e.g. \"Floored (you@example.com)\", "
            "restart the server, and don't run several crawls at once."
        )
    return f"Geocoder unavailable: {exc}"


@app.get("/api/footprint")
def footprint(lat: float, lon: float) -> dict[str, Any] | None:
    """The OSM building at a point, for picking a building by clicking the map."""
    try:
        found = footprints.footprint_at(lat, lon)
    except Exception as exc:
        raise HTTPException(502, f"OpenStreetMap unavailable: {exc}") from exc
    return found


@app.get("/api/search")
def search(q: str = Query(min_length=1), s: Session = DB) -> dict[str, Any]:
    like = f"%{q}%"
    buildings = s.scalars(
        select(Building).where(or_(Building.name.ilike(like), Building.address.ilike(like))).limit(10)
    ).all()
    alias_hits = [b for b in s.scalars(select(Building)) if any(q.lower() in a.lower() for a in b.aliases or [])]
    seen = {b.id for b in buildings}
    buildings = list(buildings) + [b for b in alias_hits if b.id not in seen]

    rooms = []
    ql = q.lower()
    for f in s.scalars(select(Floor).where(Floor.geojson.is_not(None))):
        for feat in f.geojson["features"]:
            p = feat["properties"]
            if p.get("feature") == "room" and p.get("label") and ql in p["label"].lower():
                rooms.append(
                    {
                        "building_id": f.building_id,
                        "building_name": f.building.name,
                        "floor_id": f.id,
                        "floor_name": f.name,
                        "level": f.level,
                        "room_id": p["room_id"],
                        "label": p["label"],
                        "kind": p.get("kind"),
                    }
                )
                if len(rooms) >= 20:
                    break
    return {"buildings": [ser.building_summary(b) for b in buildings[:10]], "rooms": rooms}


# ------------------------------------------------------------------ buildings


class PlaceIn(BaseModel):
    lat: float
    lon: float
    name: str | None = None
    display_name: str | None = None
    osm_id: str | None = None
    footprint: dict[str, Any] | None = None


class BuildingIn(BaseModel):
    name: str | None = None
    address: str | None = None
    # Town or region the address is in, when the address alone may be ambiguous.
    hint: str | None = None
    place: PlaceIn | None = None


class BuildingPatch(BaseModel):
    name: str | None = None
    address: str | None = None
    aliases: list[str] | None = None
    footprint: dict[str, Any] | None = None


@app.get("/api/buildings")
def list_buildings(s: Session = DB) -> list[dict[str, Any]]:
    return [ser.building_summary(b) for b in s.scalars(select(Building).order_by(Building.name))]


@app.get("/api/buildings.geojson")
def buildings_geojson(s: Session = DB) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [ser.building_feature(b) for b in s.scalars(select(Building))]}


@app.post("/api/buildings", status_code=201)
def create_building(body: BuildingIn, s: Session = DB) -> dict[str, Any]:
    place = body.place.model_dump() if body.place else None
    if place is None:
        if not body.address:
            raise HTTPException(422, "give an address or a place")
        try:
            geocoder.search(body.address, limit=1)  # surfaces an outage as an outage, not "not found"
        except Exception as exc:
            raise HTTPException(502, _geocoder_error(exc)) from exc
        m = matching.resolve(body.name, body.address, body.hint, pipeline.known_buildings(s))
        if m is None:
            raise HTTPException(404, "Couldn't find that address on OpenStreetMap. Click the building on the map instead.")
        if m.building_id:
            return ser.building_detail(s.get(Building, m.building_id))
        place = m.place
    if not place.get("footprint"):
        try:
            fp = footprints.footprint_at(place["lat"], place["lon"])
        except Exception:
            fp = None
        if fp and fp["contains"]:
            place["footprint"], place["osm_id"] = fp["footprint"], fp["osm_id"]
            place["name"] = place.get("name") or fp.get("name")
    b = pipeline.building_from_place(s, place, name=body.name)
    if body.address and not b.address:
        b.address = body.address
    s.flush()
    return ser.building_detail(b)


@app.get("/api/buildings/{building_id}")
def get_building(building_id: str, s: Session = DB) -> dict[str, Any]:
    return ser.building_detail(_get(s, Building, building_id))


@app.patch("/api/buildings/{building_id}")
def patch_building(building_id: str, body: BuildingPatch, s: Session = DB) -> dict[str, Any]:
    b = _get(s, Building, building_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    s.flush()
    return ser.building_detail(b)


def _lookup_point(body: LookupIn, s: Session) -> dict[str, Any]:
    """The OSM building at a picked suggestion, or the bare point when there is none."""
    lat, lon = body.lat, body.lon
    try:
        found = footprints.buildings_near(lat, lon, 25)
    except Exception:
        found = []
    b = found[0] if found and (found[0]["contains"] or found[0]["distance_m"] < 8) else None
    label = body.name or (body.address or "").split(",")[0] or None
    if b is None:
        place = {"name": label or "Unnamed building", "display_name": body.address or label, "lat": lat, "lon": lon,
                 "osm_id": None, "footprint": None}
        return {"confidence": 0.5, "reason": "picked point, no OSM building outline there", "building": None, "place": place}
    existing = s.scalar(select(Building).where(Building.osm_id == b["osm_id"], Building.id != (body.exclude_building_id or "")))
    if existing is not None:
        return {"confidence": 1.0, "reason": "already on your map", "building": ser.building_detail(existing), "place": None}
    place = matching._from_outline(b, label, body.address)
    return {"confidence": 0.95, "reason": f"OSM building at {body.address or 'the picked point'}", "building": None, "place": place}


class RelocateIn(BaseModel):
    place: PlaceIn
    address: str | None = None


@app.post("/api/buildings/{building_id}/relocate")
def relocate_building(building_id: str, body: RelocateIn, s: Session = DB) -> dict[str, Any]:
    """Move a building to another OSM building (usually after its address was corrected).

    The rooms found in each floor plan are kept; only where they sit on the
    map is worked out again, against the new outline.
    """
    b = _get(s, Building, building_id)
    place = body.place.model_dump()
    if place.get("osm_id"):
        other = s.scalar(select(Building).where(Building.osm_id == place["osm_id"], Building.id != b.id))
        if other is not None:
            raise HTTPException(409, f"That outline already belongs to “{other.name}”. Move its floors there, or remove it first.")
    if not place.get("footprint"):
        try:
            fp = footprints.footprint_at(place["lat"], place["lon"])
        except Exception:
            fp = None
        if fp and fp["contains"]:
            place["footprint"], place["osm_id"] = fp["footprint"], fp["osm_id"]
    pipeline.move_building(b, place, address=body.address)
    s.flush()
    return ser.building_detail(b)


class PointIn(BaseModel):
    lat: float
    lon: float


def _replace_outline(b: Building, footprint: dict[str, Any] | None) -> None:
    b.footprint = footprint
    for floor in b.floors:
        if floor.conversion is not None:
            pipeline.autoplace(floor, b)


@app.post("/api/buildings/{building_id}/outline/add")
def add_outline_part(building_id: str, body: PointIn, s: Session = DB) -> dict[str, Any]:
    """Add the OSM building at a point to this building's outline (wings mapped as separate shapes)."""
    b = _get(s, Building, building_id)
    try:
        found = footprints.buildings_near(body.lat, body.lon, 25)
    except Exception as exc:
        raise HTTPException(502, f"OpenStreetMap unavailable: {exc}") from exc
    part = next((x for x in found if x["contains"]), None)
    if part is None:
        raise HTTPException(404, "There's no OpenStreetMap building outline at that spot.")
    other = s.scalar(select(Building).where(Building.osm_id == part["osm_id"], Building.id != b.id))
    if other is not None:
        raise HTTPException(409, f"That outline is “{other.name}”, a separate building on your map. Remove it first to join it to this one.")
    _replace_outline(b, footprints.combine(b.footprint, part["footprint"]))
    if not b.osm_id:
        b.osm_id = part["osm_id"]
    s.flush()
    return ser.building_detail(b)


@app.post("/api/buildings/{building_id}/outline/remove")
def remove_outline_part(building_id: str, body: PointIn, s: Session = DB) -> dict[str, Any]:
    """Take the separate part of the outline at a point away."""
    b = _get(s, Building, building_id)
    if not b.footprint:
        raise HTTPException(409, "This building has no outline.")
    try:
        _replace_outline(b, footprints.remove_part(b.footprint, body.lat, body.lon))
    except LookupError as exc:
        raise HTTPException(404, "Click inside one of the building's separate parts to remove it.") from exc
    s.flush()
    return ser.building_detail(b)


@app.post("/api/buildings/{building_id}/spread-levels")
def spread_levels(building_id: str, s: Session = DB) -> dict[str, Any]:
    """Put plans that share a level onto levels of their own, so they stop overlapping."""
    b = _get(s, Building, building_id)
    moved = pipeline.spread_levels(b)
    s.flush()
    return {"moved": moved, "building": ser.building_detail(b)}


@app.delete("/api/buildings/{building_id}", status_code=204)
def delete_building(building_id: str, s: Session = DB) -> None:
    s.delete(_get(s, Building, building_id))


@app.post("/api/buildings/{building_id}/floors", status_code=201)
def add_floor(
    building_id: str,
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    level: float | None = Form(None),
    name: str | None = Form(None),
    page: int = Form(1),
    s: Session = DB,
) -> dict[str, Any]:
    b = _get(s, Building, building_id)
    if file is not None:
        raw, ctype, src = file.file.read(), file.content_type or "", None
        fname = file.filename or ""
    elif url:
        try:
            resp = http.client().get(url)
            resp.raise_for_status()
        except Exception as exc:
            raise HTTPException(400, f"could not download {url}: {exc}") from exc
        raw, ctype, src, fname = resp.content, resp.headers.get("content-type", ""), url, url
    else:
        raise HTTPException(422, "upload a file or give a url")
    try:
        pages = documents.load(raw, ctype, fname)
    except Exception as exc:
        raise HTTPException(400, f"unreadable file: {exc}") from exc
    if not pages:
        raise HTTPException(400, "unreadable image")
    pg = pages[min(max(page, 1), len(pages)) - 1]
    floor = pipeline.create_floor(s, b, pg.png, level=level, name=name, source_url=src)
    floor_id = floor.id
    s.commit()
    job_id = jobs.submit("process_floor", {"floor_id": floor_id}, label=f"Converting {b.name} · {floor.name}")
    s.expire_all()
    return {"floor": ser.floor_summary(_get(s, Floor, floor_id)), "job_id": job_id}


# ------------------------------------------------------------------ floors


class FloorPatch(BaseModel):
    name: str | None = None
    level: float | None = None


class RoomPatch(BaseModel):
    label: str | None = None
    kind: Literal["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"] | None = None


class PlacementIn(BaseModel):
    center_lon: float
    center_lat: float
    width_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    rotation_deg: float = 0.0
    mirror: bool = False


@app.get("/api/floors/{floor_id}")
def get_floor(floor_id: str, s: Session = DB) -> dict[str, Any]:
    return ser.floor_detail(_get(s, Floor, floor_id))


@app.get("/api/floors/{floor_id}/geojson")
def get_floor_geojson(floor_id: str, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    if not f.geojson:
        raise HTTPException(409, "floor has not been placed yet")
    return f.geojson


@app.get("/api/buildings/{building_id}/levels/{level}.geojson")
def level_geojson(building_id: str, level: float, s: Session = DB) -> dict[str, Any]:
    feats = []
    for f in _get(s, Building, building_id).floors:
        if f.level == level and f.geojson:
            feats.extend(f.geojson["features"])
    return {"type": "FeatureCollection", "features": feats}


@app.patch("/api/floors/{floor_id}")
def patch_floor(floor_id: str, body: FloorPatch, s: Session = DB) -> dict[str, Any]:
    """Rename a floor or change its level; moving onto a taken level swaps the two floors."""
    f = _get(s, Floor, floor_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("level") is not None and changes["level"] != f.level:
        pipeline.log_fix(s, f, "set_level", before=f.level, after=changes["level"], note=f.level_note)
        pipeline.set_floor_level(f, changes.pop("level"))
    if changes.get("name") and changes["name"] != f.name:
        pipeline.log_fix(s, f, "rename_floor", before=f.name, after=changes["name"])
    changes.pop("level", None)
    for k, v in changes.items():
        setattr(f, k, v)
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


@app.delete("/api/floors/{floor_id}", status_code=204)
def delete_floor(floor_id: str, s: Session = DB) -> None:
    s.delete(_get(s, Floor, floor_id))


@app.put("/api/floors/{floor_id}/placement")
def put_placement(floor_id: str, body: PlacementIn, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    if f.conversion is None:
        raise HTTPException(409, "floor has not been converted yet")
    pipeline.snapshot(f, "Move the plan")
    pipeline.log_fix(s, f, "move", before=f.placement, after=body.model_dump())
    f.placement = body.model_dump()
    f.status = "placed"
    if f.building.footprint and f.image_width:
        # Keep the overlap score current so the list shows how good it is.
        from shapely.geometry import Polygon, shape

        from ..placement.fit import plan_outline

        conv = ConversionResult.from_dict(f.conversion)
        outline = plan_outline(conv)
        if outline is not None:
            p = Placement.from_dict(f.placement)
            ring = p.ring_to_lonlat(outline.exterior.coords, f.image_width, f.image_height)
            fp = shape(f.building.footprint)
            poly = Polygon(ring)
            if poly.is_valid and fp.is_valid:
                f.fit_score = round(poly.intersection(fp).area / poly.union(fp).area, 4)
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


@app.post("/api/floors/{floor_id}/autofit")
def autofit(floor_id: str, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    if f.conversion is None:
        raise HTTPException(409, "floor has not been converted yet")
    pipeline.snapshot(f, "Snap to outline")
    pipeline.log_fix(s, f, "autofit", before=f.placement)
    fit = pipeline.autoplace(f, f.building)
    s.flush()
    return {"floor": ser.floor_detail(f), "fit": fit}


@app.post("/api/floors/{floor_id}/copy-placement")
def copy_placement(floor_id: str, from_floor_id: str = Query(...), s: Session = DB) -> dict[str, Any]:
    """Line a floor up with another floor of the same building."""
    f, other = _get(s, Floor, floor_id), _get(s, Floor, from_floor_id)
    if not other.placement:
        raise HTTPException(409, "the other floor is not placed")
    pipeline.snapshot(f, f"Line up with {other.name}")
    pipeline.log_fix(s, f, "line_up", source_floor=other.id, before=f.placement)
    pipeline.line_up(f, other)
    s.flush()
    return ser.floor_detail(f)


@app.post("/api/floors/{floor_id}/line-up-all")
def line_up_all(floor_id: str, s: Session = DB) -> dict[str, Any]:
    """Put every other floor of the building where this one is."""
    f = _get(s, Floor, floor_id)
    if not f.placement:
        raise HTTPException(409, "place this floor first")
    moved = 0
    for other in f.building.floors:
        if other.id != f.id and other.conversion is not None:
            pipeline.snapshot(other, f"Line up with {f.name}")
            pipeline.log_fix(s, other, "line_up", source_floor=f.id, before=other.placement)
            pipeline.line_up(other, f)
            moved += 1
    s.flush()
    return {"moved": moved}


@app.post("/api/floors/{floor_id}/reprocess")
def reprocess(floor_id: str, converter: str | None = None, keep_placement: bool = True, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    pipeline.log_fix(s, f, "reconvert", converter=converter, keep_placement=keep_placement)
    s.commit()
    return {
        "job_id": jobs.submit(
            "process_floor",
            {"floor_id": floor_id, "converter": converter, "keep_placement": keep_placement},
            label=f"Re-converting {f.building.name} · {f.name}",
        )
    }


@app.patch("/api/floors/{floor_id}/rooms/{room_id}")
def patch_room(floor_id: str, room_id: str, body: RoomPatch, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    conv = dict(f.conversion or {})
    rooms = [dict(r) for r in conv.get("rooms") or []]
    for r in rooms:
        if r["id"] == room_id:
            pipeline.snapshot(f, "Rename room" if "label" in body.model_fields_set else "Change room type")
            if "label" in body.model_fields_set and body.label != r.get("label"):
                pipeline.log_fix(s, f, "rename", room_id=room_id, before=r.get("label"), after=body.label)
            if body.kind is not None and body.kind != r.get("kind"):
                pipeline.log_fix(s, f, "set_kind", room_id=room_id, before=r.get("kind"), after=body.kind)
            for k, v in body.model_dump(exclude_unset=True).items():
                r[k] = v
            break
    else:
        raise HTTPException(404, "room not found")
    conv["rooms"] = rooms
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


class MergeIn(BaseModel):
    room_ids: list[str] = Field(min_length=2)
    label: str | None = None
    kind: Literal["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"] | None = None


@app.post("/api/floors/{floor_id}/rooms/merge")
def merge_rooms(floor_id: str, body: MergeIn, s: Session = DB) -> dict[str, Any]:
    """Combine rooms the converter split (or that should be one) into a single room."""
    f = _get(s, Floor, floor_id)
    pipeline.snapshot(f, f"Merge {len(body.room_ids)} rooms")
    pipeline.log_fix(s, f, "merge_rooms", room_ids=body.room_ids, label=body.label, kind=body.kind)
    try:
        room_id = pipeline.merge_rooms(f, body.room_ids, label=body.label, kind=body.kind)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    s.flush()
    return {"room_id": room_id, "floor": ser.floor_detail(f)}


@app.delete("/api/floors/{floor_id}/rooms/{room_id}")
def delete_room(floor_id: str, room_id: str, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    conv = dict(f.conversion or {})
    before = len(conv.get("rooms") or [])
    conv["rooms"] = [r for r in conv.get("rooms") or [] if r["id"] != room_id]
    if len(conv["rooms"]) == before:
        raise HTTPException(404, "room not found")
    pipeline.snapshot(f, "Remove room")
    pipeline.log_fix(s, f, "delete_room", room_ids=[room_id])
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


class DeleteIn(BaseModel):
    room_ids: list[str] = []
    # Walls and doors by their position in the plan (the number in their map id, "wall3").
    walls: list[int] = []
    doors: list[int] = []


@app.post("/api/floors/{floor_id}/delete")
def delete_features(floor_id: str, body: DeleteIn, s: Session = DB) -> dict[str, Any]:
    """Remove many rooms, walls and doors at once (an area picked on the map)."""
    f = _get(s, Floor, floor_id)
    conv = dict(f.conversion or {})
    rooms, walls, doors = set(body.room_ids), set(body.walls), set(body.doors)
    before = (len(conv.get("rooms") or []), len(conv.get("walls") or []), len(conv.get("doors") or []))
    conv["rooms"] = [r for r in conv.get("rooms") or [] if str(r["id"]) not in rooms]
    conv["walls"] = [w for i, w in enumerate(conv.get("walls") or []) if i not in walls]
    conv["doors"] = [d for i, d in enumerate(conv.get("doors") or []) if i not in doors]
    removed = {
        "rooms": before[0] - len(conv["rooms"]),
        "walls": before[1] - len(conv["walls"]),
        "doors": before[2] - len(conv["doors"]),
    }
    if not any(removed.values()):
        raise HTTPException(404, "nothing to remove")
    parts = [f"{n} {k if n != 1 else k[:-1]}" for k, n in removed.items() if n]
    pipeline.snapshot(f, "Remove " + " and ".join(parts))
    if body.room_ids:
        pipeline.log_fix(s, f, "delete_room", room_ids=body.room_ids)
    if body.walls or body.doors:
        pipeline.log_fix(
            s, f, "delete_lines",
            walls=[(f.conversion or {}).get("walls", [])[i] for i in body.walls if i < len((f.conversion or {}).get("walls") or [])],
            doors=[(f.conversion or {}).get("doors", [])[i] for i in body.doors if i < len((f.conversion or {}).get("doors") or [])],
        )
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return {"removed": removed, "floor": ser.floor_detail(f)}


class RegionIn(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    level: float | None = None
    name: str | None = None
    outline: list[list[float]] | None = None


class SplitIn(BaseModel):
    regions: list[RegionIn] = Field(min_length=1, max_length=20)
    refit: bool = True
    # Run the converter again on every part (parts it found no rooms in always are).
    reconvert: bool = False
    # Crop these floors the same way too (other pages of the same file, at the same size).
    also: list[str] = []


@app.post("/api/floors/{floor_id}/split")
def split_floor(floor_id: str, body: SplitIn, s: Session = DB) -> dict[str, Any]:
    """Crop a plan to the building (one area), or cut a sheet with several floors into one floor each."""
    f = _get(s, Floor, floor_id)
    try:
        regions = [r.model_dump(exclude_none=True) for r in body.regions]
        size = list((f.conversion or {}).get("image_size") or [f.image_width, f.image_height])
        floors = pipeline.split_floor(s, f, regions, refit=body.refit)
        pipeline.log_fix(
            s, f, "crop" if len(regions) == 1 else "split_floor",
            regions=regions, image_size=size, new_floors=[x.id for x in floors[1:]], reconvert=body.reconvert,
        )
        extra: list[Floor] = []
        if len(regions) == 1:
            for oid in body.also:
                o = s.get(Floor, oid)
                if o is None or o.building_id != f.building_id or [o.image_width, o.image_height] != size:
                    continue
                pipeline.split_floor(s, o, [{"bbox": regions[0]["bbox"]}], refit=body.refit, label="Crop like another page")
                pipeline.log_fix(s, o, "crop", regions=[{"bbox": regions[0]["bbox"]}], image_size=size, new_floors=[], copied_from=f.id)
                extra.append(o)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    redo = [x for x in floors + extra if body.reconvert or not (x.conversion or {}).get("rooms")]
    for x in redo:
        x.status = "pending"
    s.commit()
    job_ids = [
        jobs.submit("process_floor", {"floor_id": x.id, "split": False}, label=f"Converting {f.building.name} · {x.name}")
        for x in redo
    ]
    s.expire_all()
    return {
        "floor": ser.floor_detail(f),
        "floors": [ser.floor_summary(x) for x in floors],
        "also_cropped": [x.id for x in extra],
        "job_ids": job_ids,
    }


class LineIn(BaseModel):
    # Lon/lat on the map, or image pixels with ``pixels``.
    line: list[list[float]] = Field(min_length=2)
    pixels: bool = False


def _to_pixels(f: Floor, coords: list[list[float]], pixels: bool) -> list[list[float]]:
    if pixels:
        return coords
    if not f.placement or not f.image_width:
        raise HTTPException(409, "the plan isn't on the map yet")
    return editing.lines_to_pixels(Placement.from_dict(f.placement), coords, f.image_width, f.image_height)


@app.post("/api/floors/{floor_id}/rooms/{room_id}/split")
def split_room(floor_id: str, room_id: str, body: LineIn, s: Session = DB) -> dict[str, Any]:
    """Cut a room in two along a line (the converter missed a wall)."""
    f = _get(s, Floor, floor_id)
    line = _to_pixels(f, body.line, body.pixels)
    try:
        conv, ids = editing.split_room(f.conversion or {}, room_id, line)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    pipeline.snapshot(f, "Split room")
    pipeline.log_fix(s, f, "split_room", room_id=room_id, line=line, new_rooms=ids)
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return {"room_ids": ids, "floor": ser.floor_detail(f)}


class NewRoomIn(BaseModel):
    ring: list[list[float]] = Field(min_length=3)
    pixels: bool = False
    label: str | None = None
    kind: Literal["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"] = "room"


@app.post("/api/floors/{floor_id}/rooms", status_code=201)
def add_room(floor_id: str, body: NewRoomIn, s: Session = DB) -> dict[str, Any]:
    """Draw a room the converter missed."""
    f = _get(s, Floor, floor_id)
    if f.conversion is None:
        raise HTTPException(409, "floor has not been converted yet")
    ring = _to_pixels(f, body.ring, body.pixels)
    try:
        conv, rid = editing.add_room(f.conversion, ring, label=body.label, kind=body.kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    pipeline.snapshot(f, "Draw room")
    pipeline.log_fix(s, f, "add_room", room_id=rid, ring=ring, label=body.label, kind=body.kind)
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return {"room_id": rid, "floor": ser.floor_detail(f)}


class DuplicateIn(BaseModel):
    level: float | None = None
    name: str | None = None


@app.post("/api/floors/{floor_id}/duplicate", status_code=201)
def duplicate_floor(floor_id: str, body: DuplicateIn, s: Session = DB) -> dict[str, Any]:
    """Copy a plan, with its rooms and placement, to another level (towers with identical floors)."""
    f = _get(s, Floor, floor_id)
    taken = {x.level for x in f.building.floors}
    wanted = body.level if body.level is not None else f.level + 1
    lvl = pipeline.free_level(taken, wanted)
    copy = Floor(
        building=f.building,
        level=lvl,
        name=body.name or pipeline.level_name(lvl),
        level_note=None if lvl == wanted else f"{pipeline.level_name(wanted)} was taken; check which floor this is.",
        status=f.status,
        image_path=f.image_path,
        image_width=f.image_width,
        image_height=f.image_height,
        converter=f.converter,
        conversion=f.conversion,
        placement=f.placement,
        fit_score=f.fit_score,
    )
    s.add(copy)
    s.flush()
    pipeline.log_fix(s, copy, "copy_floor", source_floor=f.id)
    pipeline.refresh_geojson(copy)
    s.flush()
    return ser.floor_detail(copy)


@app.post("/api/floors/{floor_id}/undo")
def undo_floor(floor_id: str, s: Session = DB) -> dict[str, Any]:
    """Put the floor back as it was before its last change."""
    f = _get(s, Floor, floor_id)
    try:
        label = pipeline.undo(s, f)
        pipeline.log_fix(s, f, "undo", undone=label)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    s.flush()
    return {"undone": label, "floor": ser.floor_detail(f)}


# ------------------------------------------------------------------ sources & candidates


class SourceIn(BaseModel):
    name: str
    seed_urls: list[str] = Field(min_length=1)
    allowed_domains: list[str] = []
    location_hint: str | None = None
    max_pages: int = Field(200, ge=1, le=5000)
    max_depth: int = Field(3, ge=0, le=10)


class ApproveIn(BaseModel):
    building_id: str | None = None
    level: float | None = None


class CandidatePatch(BaseModel):
    building_guess: str | None = None
    address_guess: str | None = None
    level_guess: float | None = None
    level_label: str | None = None
    building_id: str | None = None
    # Move the plan to another temp building, or "new" for one of its own.
    draft_id: str | None = None


@app.get("/api/sources")
def list_sources(s: Session = DB) -> list[dict[str, Any]]:
    return [ser.source_out(x) for x in s.scalars(select(Source).order_by(Source.created_at.desc()))]


@app.post("/api/sources", status_code=201)
def create_source(body: SourceIn, s: Session = DB) -> dict[str, Any]:
    for u in body.seed_urls:
        if not u.startswith(("http://", "https://")):
            raise HTTPException(422, f"not a web address: {u}")
    src = Source(**body.model_dump())
    s.add(src)
    s.flush()
    return ser.source_out(src)


class SourcePatch(BaseModel):
    name: str | None = None
    seed_urls: list[str] | None = Field(None, min_length=1)
    allowed_domains: list[str] | None = None
    location_hint: str | None = None
    max_pages: int | None = Field(None, ge=1, le=5000)
    max_depth: int | None = Field(None, ge=0, le=10)


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: str, body: SourcePatch, s: Session = DB) -> dict[str, Any]:
    src = _get(s, Source, source_id)
    changes = body.model_dump(exclude_unset=True)
    for u in changes.get("seed_urls") or []:
        if not u.startswith(("http://", "https://")):
            raise HTTPException(422, f"not a web address: {u}")
    if "location_hint" in changes:
        changes["location_hint"] = (changes["location_hint"] or "").strip() or None
    for k, v in changes.items():
        setattr(src, k, v)
    s.flush()
    return ser.source_out(src)


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: str, s: Session = DB) -> None:
    s.delete(_get(s, Source, source_id))


@app.delete("/api/sources/{source_id}/saved", status_code=204)
def clear_saved(source_id: str, s: Session = DB) -> None:
    """Delete the pages and files saved from this website, so the next crawl downloads them again."""
    from ..scraper import cache

    cache.clear(ser._source_domains(_get(s, Source, source_id)))


class CrawlIn(BaseModel):
    # Use pages and files saved by an earlier crawl instead of asking the site again.
    use_saved: bool = False
    # Start over: forget the plans earlier crawls found (see pipeline.reset_source).
    fresh: bool = False
    remove_from_map: bool = False


@app.post("/api/sources/{source_id}/crawl")
def crawl_source(source_id: str, body: CrawlIn | None = None, s: Session = DB) -> dict[str, Any]:
    body = body or CrawlIn()
    src = _get(s, Source, source_id)
    for j in s.scalars(select(Job).where(Job.kind == "crawl", Job.status.in_(("queued", "running")))):
        if (j.payload or {}).get("source_id") == source_id:
            if body.fresh:
                raise HTTPException(409, "this website is being crawled; stop that first")
            return {"job_id": j.id}  # already crawling it
    reset = pipeline.reset_source(s, src, remove_from_map=body.remove_from_map) if body.fresh else None
    s.commit()
    label = f"{'Re-running' if body.fresh else 'Crawling'} {src.name}{' from saved copies' if body.use_saved else ''}"
    job_id = jobs.submit("crawl", {"source_id": source_id, "use_saved": body.use_saved}, label=label)
    return {"job_id": job_id, "reset": reset}


@app.get("/api/candidates")
def list_candidates(
    status: str | None = None, source_id: str | None = None, limit: int = 200, s: Session = DB
) -> list[dict[str, Any]]:
    q = select(Candidate).order_by(Candidate.plan_score.desc(), Candidate.created_at.desc()).limit(limit)
    if status:
        q = q.where(Candidate.status.in_(status.split(",")))
    if source_id:
        q = q.where(Candidate.source_id == source_id)
    cands = s.scalars(q).all()
    names = {b.id: b.name for b in s.scalars(select(Building).where(Building.id.in_({c.building_id for c in cands if c.building_id})))}
    out = []
    for c in cands:
        d = ser.candidate_out(c)
        d["building_name"] = names.get(c.building_id)
        out.append(d)
    return out


@app.patch("/api/candidates/{candidate_id}")
def patch_candidate(candidate_id: str, body: CandidatePatch, s: Session = DB) -> dict[str, Any]:
    c = _get(s, Candidate, candidate_id)
    changes = body.model_dump(exclude_unset=True)
    draft_id = changes.pop("draft_id", None)
    for k, v in changes.items():
        setattr(c, k, v)
    if "level_guess" in changes and "level_label" not in changes:
        c.level_label = pipeline.level_name(c.level_guess) if c.level_guess is not None else None
    if body.building_id:
        c.status, c.match_confidence, c.match_reason = "matched", 1.0, "chosen by hand"
    if draft_id == "new":
        pipeline.new_draft_for(s, c)
    elif draft_id:
        d = _get(s, Draft, draft_id)
        c.draft_id = d.id
        c.building_id = d.building_id or (d.suggestion or {}).get("building_id")
        c.status = "matched" if c.building_id else "unmatched"
    s.flush()
    return ser.candidate_out(c)


@app.post("/api/candidates/{candidate_id}/approve")
def approve(candidate_id: str, body: ApproveIn, s: Session = DB) -> dict[str, Any]:
    c = _get(s, Candidate, candidate_id)
    if not (body.building_id or c.building_id):
        raise HTTPException(409, "choose a building for this plan first")
    c.status = "approved"
    label = pipeline.convert_label(c)
    s.commit()
    return {"job_id": jobs.submit("approve_candidate", {"candidate_id": candidate_id, **body.model_dump()}, label=label)}


@app.post("/api/candidates/{candidate_id}/reject")
def reject(candidate_id: str, s: Session = DB) -> dict[str, Any]:
    c = _get(s, Candidate, candidate_id)
    c.status = "rejected"
    s.flush()
    return ser.candidate_out(c)


@app.post("/api/candidates/rematch")
def rematch(ids: list[str], s: Session = DB) -> dict[str, Any]:
    return {"job_id": jobs.submit("match_candidates", {"candidate_ids": ids})}


# ------------------------------------------------------------------ temp buildings


class ChoiceIn(BaseModel):
    building_id: str | None = None
    place: PlaceIn | None = None


class DraftPatch(BaseModel):
    name: str | None = None
    address: str | None = None
    # Where the user says it is; kept so the card remembers it before they confirm.
    choice: ChoiceIn | None = None


class ConfirmIn(BaseModel):
    building_id: str | None = None
    place: PlaceIn | None = None
    name: str | None = None
    address: str | None = None


def _drafts_query(source_id: str | None = None):
    open_ids = select(Candidate.draft_id).where(Candidate.status.in_(pipeline.REVIEWABLE), Candidate.draft_id.is_not(None))
    q = select(Draft).where(Draft.id.in_(open_ids)).order_by(Draft.created_at, Draft.name)
    return q.where(Draft.source_id == source_id) if source_id else q


@app.get("/api/drafts")
def list_drafts(source_id: str | None = None, s: Session = DB) -> list[dict[str, Any]]:
    """Temp buildings: found plans grouped by the building they seem to show, waiting for an address."""
    pipeline.ensure_drafts(s)
    return [ser.draft_out(d) for d in s.scalars(_drafts_query(source_id))]


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str, s: Session = DB) -> dict[str, Any]:
    return ser.draft_out(_get(s, Draft, draft_id))


def _with_outline(place: dict[str, Any]) -> dict[str, Any]:
    if not place.get("footprint"):
        try:
            fp = footprints.footprint_at(place["lat"], place["lon"])
        except Exception:
            fp = None
        if fp and fp["contains"]:
            place["footprint"], place["osm_id"] = fp["footprint"], fp["osm_id"]
            place["name"] = place.get("name") or fp.get("name")
    return place


@app.patch("/api/drafts/{draft_id}")
def patch_draft(draft_id: str, body: DraftPatch, s: Session = DB) -> dict[str, Any]:
    d = _get(s, Draft, draft_id)
    if body.name is not None and body.name.strip():
        d.name = body.name.strip()[:300]
    if body.address is not None:
        d.address = body.address.strip() or None
    if body.choice is not None:
        if body.choice.building_id:
            _get(s, Building, body.choice.building_id)
            d.suggestion = {"confidence": 1.0, "reason": "chosen by you", "building_id": body.choice.building_id,
                            "place": None, "chosen": True}
        elif body.choice.place:
            d.suggestion = {"confidence": 1.0, "reason": "chosen by you", "building_id": None,
                            "place": body.choice.place.model_dump(), "chosen": True}
        else:
            d.suggestion = None
        d.building_id = None if d.building_id != body.choice.building_id else d.building_id
        d.match_state = "done"
    s.flush()
    return ser.draft_out(d)


@app.post("/api/drafts/{draft_id}/confirm")
def confirm_draft(draft_id: str, body: ConfirmIn, s: Session = DB) -> dict[str, Any]:
    """Make the temp building real (or use one already on the map) and convert its plans."""
    d = _get(s, Draft, draft_id)
    sugg = d.suggestion or {}
    building_id, place = body.building_id, body.place.model_dump() if body.place else None
    if not (building_id or place):
        building_id = d.building_id or sugg.get("building_id")
        place = None if building_id else sugg.get("place")
    if not (building_id or place):
        raise HTTPException(409, "Say where this building is first: type its address or pick it on the map.")
    if place:
        place = _with_outline(dict(place))
        if place.get("osm_id"):
            other = s.scalar(select(Building).where(Building.osm_id == place["osm_id"]))
            building_id, place = (other.id, None) if other else (None, place)
    try:
        b, ids = pipeline.confirm_draft(s, d, building_id=building_id, place=place, name=body.name, address=body.address)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    # How the building match was settled, to tune matching: kept the suggestion or chose another.
    suggested = sugg.get("building_id") or (sugg.get("place") or {}).get("osm_id")
    pipeline.log_fix(
        s, None, "match", building_id=b.id, draft=d.name, address=d.address, suggested=suggested,
        confidence=sugg.get("confidence"), reason=sugg.get("reason"), chosen=b.osm_id or b.id,
        kept=bool(suggested) and suggested in (b.id, b.osm_id), name=body.name, address_given=body.address,
    )
    building = ser.building_summary(b)
    s.commit()
    job_ids = pipeline.submit_conversions(ids)
    return {"building": building, "job_ids": job_ids}


@app.post("/api/drafts/{draft_id}/reject")
def reject_draft(draft_id: str, s: Session = DB) -> dict[str, Any]:
    d = _get(s, Draft, draft_id)
    n = 0
    for c in d.candidates:
        if c.status in pipeline.REVIEWABLE:
            c.status = "rejected"
            n += 1
    return {"rejected": n}


@app.post("/api/drafts/{draft_id}/rematch")
def rematch_draft(draft_id: str, s: Session = DB) -> dict[str, Any]:
    d = _get(s, Draft, draft_id)
    d.match_state = "pending"
    s.commit()
    return {"job_id": jobs.submit("match_candidates", {"draft_ids": [draft_id]}, label=f"Finding {d.name}")}


# ------------------------------------------------------------------ jobs & media


@app.get("/api/jobs")
def list_jobs(active: bool = False, limit: int = 30, s: Session = DB) -> list[dict[str, Any]]:
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if active:
        q = q.where(Job.status.in_(("queued", "running")))
    return [ser.job_out(j) for j in s.scalars(q)]


@app.get("/api/activity")
def activity(s: Session = DB) -> dict[str, Any]:
    """Everything the background workers are doing, waiting to do, and recently did."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    running = s.scalars(select(Job).where(Job.status == "running").order_by(Job.started_at)).all()
    queued = s.scalars(select(Job).where(Job.status == "queued").order_by(Job.created_at)).all()
    recent = s.scalars(
        select(Job)
        .where(Job.status.in_(("done", "failed", "cancelled")), Job.finished_at >= since)
        .order_by(Job.finished_at.desc())
        .limit(15)
    ).all()
    sources = {x.id: x.name for x in s.scalars(select(Source))}

    def out(j: Job) -> dict[str, Any]:
        label = None
        if j.kind == "crawl" and not (j.payload or {}).get("label"):
            label = f"Crawling {sources.get((j.payload or {}).get('source_id'), 'a website')}"
        return ser.job_out(j, label)

    return {
        "running": [out(j) for j in running],
        "queued": [out(j) for j in queued],
        "recent": [out(j) for j in recent],
        "workers": get_settings().workers,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, s: Session = DB) -> dict[str, Any]:
    return ser.job_out(_get(s, Job, job_id))


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, s: Session = DB) -> dict[str, Any]:
    """Stop a crawl early (what it found so far is kept) or cancel a job that hasn't started."""
    _get(s, Job, job_id)
    if not jobs.request_stop(job_id):
        raise HTTPException(409, "that job has already finished")
    s.expire_all()
    return ser.job_out(_get(s, Job, job_id))


@app.get("/media/{path:path}")
def media(path: str) -> FileResponse:
    try:
        p = storage.media_path(path)
    except ValueError as exc:
        raise HTTPException(404) from exc
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})


# Serve the built frontend when it's there (production: one process, one port).
_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = _dist / full_path
        if full_path and candidate.is_file() and _dist in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
