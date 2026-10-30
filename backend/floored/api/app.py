"""HTTP API. Run with ``uvicorn floored.api.app:app`` or ``floored serve``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import http, jobs, pipeline, storage
from ..config import get_settings
from ..converters import ConversionResult, available_converters
from ..converters.base import ROOM_KINDS
from ..db import get_session, init_db
from ..geo import footprints, geocoder
from ..models import Building, Candidate, Floor, Job, Source
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

DB = Depends(get_session)


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
def geocode(q: str = Query(min_length=2)) -> list[dict[str, Any]]:
    try:
        return [p.to_dict() for p in geocoder.search(q)]
    except Exception as exc:
        raise HTTPException(502, f"geocoder unavailable: {exc}") from exc


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
            results = geocoder.search(body.address, limit=3)
        except Exception as exc:
            raise HTTPException(502, f"geocoder unavailable: {exc}") from exc
        if not results:
            raise HTTPException(404, "address not found")
        place = results[0].to_dict()
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
    job_id = jobs.submit("process_floor", {"floor_id": floor_id})
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
    f = _get(s, Floor, floor_id)
    for k, v in body.model_dump(exclude_unset=True).items():
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
    fit = pipeline.autoplace(f, f.building)
    s.flush()
    return {"floor": ser.floor_detail(f), "fit": fit}


@app.post("/api/floors/{floor_id}/copy-placement")
def copy_placement(floor_id: str, from_floor_id: str = Query(...), s: Session = DB) -> dict[str, Any]:
    """Line a floor up with another floor of the same building."""
    f, other = _get(s, Floor, floor_id), _get(s, Floor, from_floor_id)
    if not other.placement:
        raise HTTPException(409, "the other floor is not placed")
    p = Placement.from_dict(other.placement)
    if f.image_width and other.image_width and (f.image_width, f.image_height) != (other.image_width, other.image_height):
        # Same scale in metres per pixel, so different-sized exports still line up.
        p.width_m = p.width_m * f.image_width / other.image_width
        p.height_m = p.height_m * f.image_height / other.image_height
    f.placement = p.to_dict()
    f.status = "placed"
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


@app.post("/api/floors/{floor_id}/reprocess")
def reprocess(floor_id: str, converter: str | None = None, keep_placement: bool = True, s: Session = DB) -> dict[str, Any]:
    _get(s, Floor, floor_id)
    return {"job_id": jobs.submit("process_floor", {"floor_id": floor_id, "converter": converter, "keep_placement": keep_placement})}


@app.patch("/api/floors/{floor_id}/rooms/{room_id}")
def patch_room(floor_id: str, room_id: str, body: RoomPatch, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    conv = dict(f.conversion or {})
    rooms = [dict(r) for r in conv.get("rooms") or []]
    for r in rooms:
        if r["id"] == room_id:
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


@app.delete("/api/floors/{floor_id}/rooms/{room_id}")
def delete_room(floor_id: str, room_id: str, s: Session = DB) -> dict[str, Any]:
    f = _get(s, Floor, floor_id)
    conv = dict(f.conversion or {})
    before = len(conv.get("rooms") or [])
    conv["rooms"] = [r for r in conv.get("rooms") or [] if r["id"] != room_id]
    if len(conv["rooms"]) == before:
        raise HTTPException(404, "room not found")
    f.conversion = conv
    pipeline.refresh_geojson(f)
    s.flush()
    return ser.floor_detail(f)


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
    building_id: str | None = None


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


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: str, s: Session = DB) -> None:
    s.delete(_get(s, Source, source_id))


@app.post("/api/sources/{source_id}/crawl")
def crawl_source(source_id: str, s: Session = DB) -> dict[str, Any]:
    _get(s, Source, source_id)
    return {"job_id": jobs.submit("crawl", {"source_id": source_id})}


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
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    if body.building_id:
        c.status, c.match_confidence, c.match_reason = "matched", 1.0, "chosen by hand"
    s.flush()
    return ser.candidate_out(c)


@app.post("/api/candidates/{candidate_id}/approve")
def approve(candidate_id: str, body: ApproveIn, s: Session = DB) -> dict[str, Any]:
    c = _get(s, Candidate, candidate_id)
    if not (body.building_id or c.building_id):
        raise HTTPException(409, "choose a building for this plan first")
    c.status = "approved"
    s.commit()
    return {"job_id": jobs.submit("approve_candidate", {"candidate_id": candidate_id, **body.model_dump()})}


@app.post("/api/candidates/{candidate_id}/reject")
def reject(candidate_id: str, s: Session = DB) -> dict[str, Any]:
    c = _get(s, Candidate, candidate_id)
    c.status = "rejected"
    s.flush()
    return ser.candidate_out(c)


@app.post("/api/candidates/rematch")
def rematch(ids: list[str], s: Session = DB) -> dict[str, Any]:
    return {"job_id": jobs.submit("match_candidates", {"candidate_ids": ids})}


# ------------------------------------------------------------------ jobs & media


@app.get("/api/jobs")
def list_jobs(active: bool = False, limit: int = 30, s: Session = DB) -> list[dict[str, Any]]:
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if active:
        q = q.where(Job.status.in_(("queued", "running")))
    return [ser.job_out(j) for j in s.scalars(q)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, s: Session = DB) -> dict[str, Any]:
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
