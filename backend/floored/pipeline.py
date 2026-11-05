"""The end-to-end flow: scraped image -> building match -> rooms -> placed GeoJSON."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import jobs, storage
from .config import get_settings
from .converters import ConversionResult, get_converter
from .db import session_scope
from .geo import matching
from .models import Building, Candidate, Correction, Draft, Floor, Source
from .placement.fit import default_placement, fit_to_footprint
from .placement.geojson import floor_geojson
from .placement.transform import Placement
from .scraper.crawler import CrawlConfig, crawl
from .scraper.fetch import Fetcher

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


def move_building(b: Building, place: dict[str, Any], *, address: str | None = None) -> None:
    """Point a building at a new location and outline, and re-place its converted floors."""
    footprint = place.get("footprint")
    lat, lon = place["lat"], place["lon"]
    if footprint:
        from shapely.geometry import shape

        c = shape(footprint).centroid
        lon, lat = c.x, c.y
    b.lat, b.lon = lat, lon
    b.footprint = footprint
    b.osm_id = place.get("osm_id")
    b.address = address or place.get("display_name") or b.address
    for floor in b.floors:
        if floor.conversion is not None:
            autoplace(floor, b)


def known_buildings(s: Session) -> list[matching.KnownBuilding]:
    return [
        matching.KnownBuilding(
            id=b.id, name=b.name, aliases=b.aliases or [], osm_id=b.osm_id, address=b.address, lat=b.lat, lon=b.lon
        )
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


def merge_rooms(floor: Floor, room_ids: list[str], *, label: str | None = None, kind: str | None = None) -> str:
    """Join several rooms into one (the converter split a room, or a suite should be one).

    The rooms are unioned in image pixels; the wall gap between neighbours is
    closed, and walls that end up inside the new room are dropped. Returns
    the id of the merged room (the first one's).
    """
    from collections import Counter

    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    conv = dict(floor.conversion or {})
    rooms = [dict(r) for r in conv.get("rooms") or []]
    by_id = {str(r["id"]): r for r in rooms}
    ids = list(dict.fromkeys(str(i) for i in room_ids))
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise LookupError(f"room {missing[0]} not found")
    if len(ids) < 2:
        raise ValueError("pick at least two rooms to merge")
    picked = [by_id[i] for i in ids]
    polys = [Polygon(r["polygon"], r.get("holes") or []).buffer(0) for r in picked]
    w, h = conv.get("image_size") or [1000, 1000]
    merged = unary_union(polys)
    # Close the wall between neighbouring rooms, growing the gap we bridge until it's one shape.
    for frac in (0.004, 0.008, 0.015, 0.03):
        if merged.geom_type == "Polygon":
            break
        d = frac * max(w, h)
        merged = unary_union(polys).buffer(d, join_style=2).buffer(-d, join_style=2)
    if merged.geom_type != "Polygon":
        merged = merged.convex_hull  # far apart; the user asked for one room all the same
    merged = merged.simplify(0.5)

    first = picked[0]
    labels = [r.get("label") for r in picked if r.get("label")]
    kinds = Counter(r.get("kind") or "unknown" for r in picked)
    new_room = {
        **first,
        "polygon": [list(c) for c in merged.exterior.coords[:-1]],
        "holes": [[list(c) for c in ring.coords[:-1]] for ring in merged.interiors],
        "label": label if label is not None else (labels[0] if labels else None),
        "kind": kind or kinds.most_common(1)[0][0],
        "area_px": float(merged.area),
        "confidence": None,
    }
    drop = set(ids[1:])
    conv["rooms"] = [new_room if str(r["id"]) == ids[0] else r for r in rooms if str(r["id"]) not in drop]
    inner = merged.buffer(-1.5)
    conv["walls"] = [
        line for line in conv.get("walls") or [] if len(line) < 2 or inner.is_empty or not inner.contains(LineString(line))
    ]
    floor.conversion = conv
    refresh_geojson(floor)
    return ids[0]


def process_floor(
    floor_id: str, converter: str | None = None, keep_placement: bool = False, *, split: bool = True
) -> dict[str, Any]:
    """Run the converter on a floor's image and place the result.

    A sheet the converter is sure holds several floors is cut into one floor
    each, unless ``split`` is off (the image is already one part of a sheet).
    """
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
        if (floor.conversion or {}).get("rooms"):
            snapshot(floor, "Convert again")
        floor.conversion = data
        floor.converter = conv_impl.name
        floor.image_width, floor.image_height = conv.image_size
        if keep_placement and floor.placement:
            _floor_from_plan_text(floor, (data.get("meta") or {}).get("floor"))
            floor.status = "placed"
            refresh_geojson(floor)
            remember_machine(floor, None)
            return {"floor_id": floor_id, "rooms": len(conv.rooms), "fit": None, "status": floor.status}
        # The machine's own output, before it is split or placed.
        whole = {"conversion": floor.conversion, "image_path": floor.image_path, "image_size": [floor.image_width, floor.image_height]}
        parts = auto_split(s, floor) if split else []
        if not parts:
            _floor_from_plan_text(floor, (data.get("meta") or {}).get("floor"))
            fit = autoplace(floor, floor.building)
            remember_machine(floor, fit)
            return {"floor_id": floor_id, "rooms": len(conv.rooms), "fit": fit, "status": floor.status}
        # Parts of the sheet the converter found no rooms in are converted on their own.
        for f in parts:
            remember_machine(f, {"iou": f.fit_score})
            # Each part's machine output is the whole sheet's; final.json says which box it is.
            f.machine = {**f.machine, **whole, "auto_split": True}
        empty = [(f.id, f"Converting {f.building.name} · {f.name}") for f in parts if not (f.conversion or {}).get("rooms")]
        for f in parts:
            if f.id in dict(empty):
                f.status = "pending"
        out = {"floor_id": floor_id, "rooms": len(conv.rooms), "split_into": [f.id for f in parts], "status": floor.status}
    for fid, label in empty:
        jobs.submit("process_floor", {"floor_id": fid, "split": False}, label=label)
    return out


def free_level(taken: set[float], wanted: float | None) -> float:
    """``wanted`` if no other plan has it, else the next level above the building's top floor."""
    if wanted is not None and float(wanted) not in taken:
        return float(wanted)
    if not taken:
        return 1.0
    return float(int(max(taken)) + 1)


def _floor_from_plan_text(floor: Floor, info: dict[str, Any] | None) -> None:
    """Settle a guessed level with what the plan itself says, when that level is free."""
    if not floor.level_note or not info or info.get("level") is None:
        return
    if info.get("confidence", 0) < 0.5:
        return
    level = float(info["level"])
    if any(f.level == level for f in floor.building.floors if f.id != floor.id):
        floor.level_note = f"The plan says {info.get('label') or level_name(level).lower()}, but another plan has that level; check which floor this is."
        return
    floor.level = level
    floor.name = info.get("label") or level_name(level)
    floor.level_note = None


def create_floor(
    s: Session,
    building: Building,
    image: bytes,
    *,
    level: float | None,
    name: str | None = None,
    source_url: str | None = None,
) -> Floor:
    """A floor for a plan image. Two plans never share a level: a clash gets the next free one."""
    floor = None
    if source_url:
        floor = s.scalar(select(Floor).where(Floor.building_id == building.id, Floor.source_url == source_url))
    if floor is None:
        s.flush()
        taken = {f.level for f in s.scalars(select(Floor).where(Floor.building_id == building.id))}
        lvl = free_level(taken, level)
        note = None
        if level is None:
            note = "The plan didn't say which floor it is; check the level."
        elif lvl != float(level):
            note = f"Also labelled level {level_name(float(level)).lower()}, which another plan has; check which floor this is."
        floor = Floor(building_id=building.id, level=lvl, name=(name if lvl == level else None) or level_name(lvl), level_note=note)
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


# ---------------------------------------------------------------- temp buildings

# Candidates still waiting for a human (or a confident match).
REVIEWABLE = ("new", "matched", "unmatched", "error")


def _draft_key(c: Candidate) -> str:
    name = matching.normalize_name(c.building_guess or "")
    if name:
        return f"name:{name}"
    if c.address_guess:
        return "addr:" + re.sub(r"\W+", " ", c.address_guess.lower()).strip()
    return f"file:{c.asset_url}"


def _draft_name(c: Candidate) -> str:
    name = matching.clean_name(c.building_guess)
    if name:
        return name[:300]
    if c.address_guess:
        return c.address_guess[:300]
    if c.page_title:
        return c.page_title[:300]
    return c.asset_url.rstrip("/").rsplit("/", 1)[-1][:300] or "Unnamed building"


def assign_draft(s: Session, c: Candidate) -> Draft:
    """Put a candidate in the temp building for its name (or address, or file), making one if needed."""
    key = _draft_key(c)
    d = s.scalar(
        select(Draft).where(Draft.source_id == c.source_id, Draft.key == key).order_by(Draft.created_at.desc())
    )
    if d is None:
        d = Draft(source_id=c.source_id, key=key, name=_draft_name(c), address=c.address_guess)
        s.add(d)
        s.flush()
    elif not d.address and c.address_guess:
        d.address = c.address_guess
    c.draft_id = d.id
    if d.building_id:
        # Plans for a building the user already confirmed go straight to it.
        c.building_id, c.status = d.building_id, "matched"
        c.match_confidence, c.match_reason = 0.95, "same building as plans you already confirmed"
    return d


def new_draft_for(s: Session, c: Candidate) -> Draft:
    """Take a candidate out of its group into a temp building of its own."""
    d = Draft(source_id=c.source_id, key=f"own:{c.id}", name=_draft_name(c), address=c.address_guess, match_state="done")
    s.add(d)
    s.flush()
    c.draft_id, c.building_id, c.status = d.id, None, "unmatched"
    c.match_confidence, c.match_reason = 0.0, "moved to its own building"
    return d


def ensure_drafts(s: Session) -> None:
    """Group plans found before temp buildings existed."""
    loose = s.scalars(select(Candidate).where(Candidate.draft_id.is_(None), Candidate.status.in_(REVIEWABLE))).all()
    for c in loose:
        building_id, conf, reason = c.building_id, c.match_confidence, c.match_reason
        d = assign_draft(s, c)
        if building_id and not d.suggestion and not d.building_id:
            d.suggestion = {"confidence": conf, "reason": reason, "building_id": building_id, "place": None, "chosen": False}
        if c.status != "new":
            d.match_state = "done"
    s.flush()


def match_draft(draft_id: str) -> None:
    """Ask the geocoder where a temp building is, and keep the answer as its suggestion."""
    with session_scope() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return
        if d.building_id or (d.suggestion or {}).get("chosen"):
            d.match_state = "done"  # the user already said where it is
            return
        hint = d.source.location_hint if d.source else None
        known = known_buildings(s)
        name, address = d.name, d.address
    try:
        m = matching.resolve(name, address, hint, known)
    except Exception as exc:  # geocoder outage etc.
        log.warning("matching draft %s failed: %s", draft_id, exc)
        m = None
    with session_scope() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return
        d.match_state = "done"
        d.suggestion = (
            None
            if m is None
            else {"confidence": m.confidence, "reason": m.reason, "building_id": m.building_id, "place": m.place, "chosen": False}
        )
        for c in d.candidates:
            if c.status not in REVIEWABLE:
                continue
            c.building_id = m.building_id if m else None
            c.status = "matched" if m else "unmatched"
            c.match_confidence = m.confidence if m else 0.0
            c.match_reason = m.reason if m else "no building found for this name or address"


def confirm_draft(
    s: Session,
    d: Draft,
    *,
    building_id: str | None = None,
    place: dict[str, Any] | None = None,
    name: str | None = None,
    address: str | None = None,
    min_plan_score: float = 0.0,
    chosen: bool = True,
) -> tuple[Building, list[str]]:
    """Make a temp building real (or point it at one we have) and mark its plans for conversion.

    Returns the building and the ids of the candidates to convert; plans
    scoring under ``min_plan_score`` stay in review, attached to the building.
    """
    if name:
        d.name = name.strip()[:300] or d.name
    if address:
        d.address = address
    if building_id:
        b = s.get(Building, building_id)
        if b is None:
            raise LookupError("building not found")
    elif place:
        b = building_from_place(s, place, name=d.name)
        if not b.address:
            b.address = d.address or place.get("display_name")
    else:
        raise ValueError("say which building, or where it is")
    d.building_id = b.id
    d.match_state = "done"
    d.suggestion = {
        "confidence": 1.0 if chosen else (d.suggestion or {}).get("confidence", 0.9),
        "reason": "chosen by you" if chosen else (d.suggestion or {}).get("reason", ""),
        "building_id": b.id,
        "place": None,
        "chosen": chosen,
    }
    s.flush()
    taken = {f.level for f in s.scalars(select(Floor).where(Floor.building_id == b.id))}
    group = sorted(
        (c for c in d.candidates if c.status in REVIEWABLE and c.plan_score >= min_plan_score),
        key=lambda c: (c.level_guess is None, c.level_guess or 0, c.asset_url, c.page_index),
    )
    for c in group:
        if c.floor_id:
            continue
        lvl = free_level(taken, c.level_guess)
        if c.level_guess is None:
            c.level_note = "The plan didn't say which floor it is; check the level."
        elif lvl != c.level_guess:
            c.level_note = (
                f"Labelled {c.level_label or level_name(c.level_guess).lower()}, like another plan; check which floor this is."
            )
            c.level_label = None  # its printed label belongs to the level it can't have
        c.level_guess = lvl
        taken.add(lvl)
    ids = []
    for c in d.candidates:
        if c.status not in REVIEWABLE:
            continue
        c.building_id = b.id
        if chosen:
            c.match_confidence, c.match_reason = 1.0, "chosen by hand"
        if c.plan_score >= min_plan_score:
            c.status = "approved"
            ids.append(c.id)
        else:
            c.status = "matched"
    s.flush()
    return b, ids


def reset_source(s: Session, src: Source, *, remove_from_map: bool = False) -> dict[str, int]:
    """Forget what a website's crawls found, so the next crawl finds it all again.

    Plans waiting for review (or rejected) and their temp buildings go. Plans
    already on the map stay, and the next crawl skips them, unless
    ``remove_from_map``: then their floors go too, and buildings left with
    no floors at all.
    """
    plans = floors = buildings = 0
    touched: set[str] = set()
    for c in list(src.candidates):
        on_map = c.floor_id is not None and c.status in ("approved", "processed")
        if on_map and not remove_from_map:
            continue
        if c.floor_id:
            f = s.get(Floor, c.floor_id)
            if f is not None:
                touched.add(f.building_id)
                s.delete(f)
                floors += 1
        s.delete(c)
        plans += 1
    s.flush()
    for d in list(src.drafts):
        if not d.candidates:
            s.delete(d)
    s.flush()
    for bid in touched:
        b = s.get(Building, bid)
        if b is not None and not b.floors:
            s.delete(b)
            buildings += 1
    src.last_crawled_at = None
    s.flush()
    return {"plans": plans, "floors": floors, "buildings": buildings}


def convert_label(c: Candidate) -> str:
    building = c.draft.name if c.draft else (c.building_guess or "Plan")
    return f"Converting {building}" + (f" · {c.level_label}" if c.level_label else "")


def submit_conversions(candidate_ids: list[str]) -> list[str]:
    """One background job per plan, so each conversion shows in the activity list."""
    with session_scope() as s:
        labels = {cid: convert_label(s.get(Candidate, cid)) for cid in candidate_ids}
    return [jobs.submit("approve_candidate", {"candidate_id": cid}, label=labels[cid]) for cid in candidate_ids]


def match_candidate(candidate_id: str) -> None:
    """Find the building for one plan, by way of its temp building."""
    with session_scope() as s:
        c = s.get(Candidate, candidate_id)
        d = c.draft or assign_draft(s, c)
        draft_id = d.id
    match_draft(draft_id)


def set_floor_level(floor: Floor, level: float) -> Floor | None:
    """Move a floor to a level. If another plan is there, the two swap. Returns the other floor."""
    level = float(level)
    other = next((f for f in floor.building.floors if f.id != floor.id and f.level == level), None)
    old = floor.level
    if other is not None:
        other.level = old
        if other.name == level_name(level):
            other.name = level_name(old)
        other.level_note = None
        refresh_geojson(other)
    if floor.name == level_name(old):
        floor.name = level_name(level)
    floor.level = level
    floor.level_note = None
    refresh_geojson(floor)
    return other


def spread_levels(b: Building) -> int:
    """Give every plan of a building its own level (for buildings saved before that was enforced)."""
    taken: set[float] = set()
    moved = 0
    for f in sorted(b.floors, key=lambda f: (f.level, f.created_at)):
        if f.level in taken:
            new = free_level(taken, f.level)
            f.level_note = f"Was on level {level_name(f.level).lower()} with another plan; check which floor this is."
            if f.name == level_name(f.level):
                f.name = level_name(new)
            f.level = new
            refresh_geojson(f)
            moved += 1
        taken.add(f.level)
    return moved


# ---------------------------------------------------------------- editing


def log_fix(s: Session, floor: Floor | None, op: str, *, building_id: str | None = None, **params: Any) -> None:
    """Note a fix a person made, so the converter and matching can learn from it (see floored.feedback)."""
    s.add(
        Correction(
            floor_id=floor.id if floor is not None else None,
            building_id=building_id or (floor.building_id if floor is not None else None),
            op=op,
            params=params,
        )
    )


def remember_machine(floor: Floor, fit: dict[str, Any] | None) -> None:
    """Keep what the converter and the fit produced, untouched by later edits."""
    floor.machine = {
        "conversion": floor.conversion,
        "placement": floor.placement,
        "fit_iou": (fit or {}).get("iou"),
        "ambiguous": (fit or {}).get("ambiguous"),
        "converter": floor.converter,
        "image_path": floor.image_path,
        "image_size": [floor.image_width, floor.image_height],
        "at": datetime.now(timezone.utc).isoformat(),
    }

HISTORY_LIMIT = 12
_SNAP_FIELDS = ("conversion", "placement", "status", "fit_score", "image_path", "image_width", "image_height", "level", "name", "level_note")


def snapshot(floor: Floor, label: str, *, created: list[str] | None = None) -> None:
    """Remember the floor as it is now, so the next change can be undone."""
    snap: dict[str, Any] = {k: getattr(floor, k) for k in _SNAP_FIELDS}
    if snap["status"] in ("processing", "pending"):
        snap["status"] = "needs_review" if floor.placement else "pending"
    snap["label"] = label
    snap["at"] = datetime.now(timezone.utc).isoformat()
    snap["created"] = list(created or [])
    floor.history = [*(floor.history or []), snap][-HISTORY_LIMIT:]


def undo(s: Session, floor: Floor) -> str:
    """Put the floor back as it was before its last change. Returns what was undone."""
    hist = list(floor.history or [])
    if not hist:
        raise ValueError("nothing to undo")
    snap = hist.pop()
    for fid in snap.get("created") or []:
        other = s.get(Floor, fid)
        if other is not None and other.building_id == floor.building_id:
            s.delete(other)
    s.flush()
    for k in _SNAP_FIELDS:
        if k in snap:
            setattr(floor, k, snap[k])
    floor.history = hist
    refresh_geojson(floor)
    return snap.get("label") or "last change"


def line_up(floor: Floor, other: Floor) -> None:
    """Put ``floor`` where ``other`` is, at the same metres per pixel."""
    p = Placement.from_dict(other.placement)
    if floor.image_width and other.image_width and (floor.image_width, floor.image_height) != (other.image_width, other.image_height):
        # Same scale in metres per pixel, so different-sized exports still line up.
        p.width_m = p.width_m * floor.image_width / other.image_width
        p.height_m = p.height_m * floor.image_height / other.image_height
    floor.placement = p.to_dict()
    floor.status = "placed"
    refresh_geojson(floor)


def split_floor(
    s: Session,
    floor: Floor,
    regions: list[dict[str, Any]],
    *,
    refit: bool = True,
    label: str | None = None,
) -> list[Floor]:
    """Cut a plan image into one floor per region (one region is a crop).

    Each region is ``{"bbox": [x0, y0, x1, y1] in image pixels, "level"?, "name"?, "outline"?}``.
    The first region stays this floor; the rest become new floors of the same
    building on their own levels. Rooms keep their numbers and types. With
    ``refit`` each piece is fitted to the building outline again; otherwise it
    stays where it was on the map (new floors stack on the first).
    """
    from . import editing

    if not regions:
        raise ValueError("draw at least one area")
    if floor.conversion is None or not floor.image_path:
        raise ValueError("the plan hasn't been converted yet")
    conv = floor.conversion
    w, h = conv.get("image_size") or [floor.image_width, floor.image_height]
    boxes = [editing.clamp_box(r["bbox"], w, h) for r in regions]
    image = storage.read_bytes(floor.image_path)
    old_place = Placement.from_dict(floor.placement) if floor.placement else None
    b = floor.building
    snap_label = label or ("Crop" if len(regions) == 1 else f"Split into {len(regions)} floors")
    before: dict[str, Any] = {k: getattr(floor, k) for k in _SNAP_FIELDS}
    taken = {f.level for f in b.floors if f.id != floor.id}

    out: list[Floor] = []
    anchor: Placement | None = None
    for i, (r, bx) in enumerate(zip(regions, boxes)):
        sub = editing.crop_conversion(conv, bx)
        # Where this part sits in the image the converter saw, so fixes can be compared with its output.
        org = (conv.get("meta") or {}).get("origin") or {"floor": floor.id, "box": [0, 0, w, h]}
        ox, oy = org["box"][0], org["box"][1]
        sub["meta"]["origin"] = {"floor": org["floor"], "box": [ox + bx[0], oy + bx[1], ox + bx[2], oy + bx[3]]}
        ring = r.get("outline")
        if ring and len(ring) >= 3:
            sub["outline"] = [[float(x) - bx[0], float(y) - bx[1]] for x, y in ring]
        wanted = r.get("level")
        if i == 0:
            target = floor
            lvl = floor.level if wanted is None else free_level(taken, float(wanted))
        else:
            lvl = free_level(taken, None if wanted is None else float(wanted))
            target = Floor(building=b, level=lvl, name=level_name(lvl), converter=floor.converter)
            target.source_url = f"{floor.source_url}#part={i + 1}" if floor.source_url else None
            s.add(target)
        taken.add(lvl)
        if wanted is None and i > 0:
            target.level_note = "Cut from a sheet with several floors; check which floor this is."
        elif wanted is not None and lvl != float(wanted):
            target.level_note = f"Labelled {level_name(float(wanted)).lower()}, which another plan has; check which floor this is."
        elif wanted is not None:
            target.level_note = None
        target.name = r.get("name") or (target.name if i == 0 and lvl == floor.level else level_name(lvl))
        target.level = lvl
        target.image_path = storage.save_bytes(editing.crop_image(image, bx), "plans", ".png")
        target.image_width, target.image_height = bx[2] - bx[0], bx[3] - bx[1]
        target.conversion = sub
        target.error = None
        s.flush()

        cropped = editing.crop_placement(old_place, bx, w, h) if old_place else None
        if i == 0:
            anchor = cropped
        if refit and b.footprint:
            autoplace(target, b)
        elif cropped is not None and anchor is not None:
            p = cropped if i == 0 else Placement(
                anchor.center_lon, anchor.center_lat, cropped.width_m, cropped.height_m, anchor.rotation_deg, anchor.mirror
            )
            target.placement = p.to_dict()
            target.status = floor.status if i == 0 and floor.status in ("placed", "needs_review") else "needs_review"
            refresh_geojson(target)
        else:
            autoplace(target, b)
        out.append(target)

    created = [f.id for f in out[1:]]
    before.update(label=snap_label, at=datetime.now(timezone.utc).isoformat(), created=created)
    floor.history = [*(floor.history or []), before][-HISTORY_LIMIT:]
    return out


def sheet_regions(conv: dict[str, Any]) -> list[dict[str, Any]]:
    """The converter's guess at separate floors on one sheet, as split regions."""
    out = []
    for sh in (conv.get("meta") or {}).get("sheets") or []:
        if not sh.get("bbox") or len(sh["bbox"]) != 4:
            continue
        info = sh.get("floor") or {}
        level = sh.get("level", info.get("level"))
        level_ok = level is not None and info.get("confidence", 1.0 if sh.get("level") is not None else 0) >= 0.5
        out.append(
            {
                "bbox": [float(v) for v in sh["bbox"]],
                "level": float(level) if level_ok else None,
                "name": (sh.get("label") or info.get("label")) if level_ok else None,
                "outline": sh.get("outline"),
                "confidence": float(sh.get("confidence") or 0),
            }
        )
    return out if len(out) >= 2 else []


def auto_split(s: Session, floor: Floor) -> list[Floor]:
    """Split a sheet the converter is sure holds several floors. Unsure ones are left as a suggestion."""
    regions = sheet_regions(floor.conversion or {})
    if not regions or min(r["confidence"] for r in regions) < 0.5:
        return []
    if not floor.level_note:
        regions[0] = {**regions[0], "level": None, "name": None}  # the level was given by the page; keep it
    return split_floor(s, floor, regions, refit=True, label="Split the sheet into floors")


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
        if c.level_note and not floor.level_note:
            floor.level_note = c.level_note
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
    draft_ids: list[str] = []
    last: dict[str, Any] = {}

    def crawl_progress(fraction: float, message: str, detail: dict[str, Any]) -> None:
        last.clear()
        last.update(detail)
        progress(0.85 * fraction, message, {**detail, "buildings_found": len(draft_ids)})

    from .converters import external

    reader = external.read_floor if external.is_available() else None
    # "use_saved": answer from copies an earlier crawl saved, sending the site no requests.
    fetcher = Fetcher(use_saved=payload.get("use_saved"))
    for found in crawl(
        cfg,
        fetcher=fetcher,
        skip_assets=skip,
        progress=crawl_progress,
        should_stop=lambda: progress.stop_requested,
        floor_reader=reader,
    ):
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
            d = assign_draft(s, c)
            new_ids.append(c.id)
            if d.id not in draft_ids:
                draft_ids.append(d.id)
    stopped = progress.stop_requested
    with session_scope() as s:
        s.get(Source, source_id).last_crawled_at = datetime.now(timezone.utc)

    # One lookup per building rather than per floor; confident ones go straight on the map.
    threshold = get_settings().auto_approve_confidence
    to_convert: list[str] = []
    auto_buildings = 0
    for i, did in enumerate(draft_ids):
        with session_scope() as s:
            name = s.get(Draft, did).name
        progress(
            0.85 + 0.15 * i / len(draft_ids),
            f"Finding building {i + 1} of {len(draft_ids)}: {name}",
            {**last, "phase": "matching", "buildings_found": len(draft_ids), "buildings_matched": i, "current": name,
             "current_url": None},
        )
        match_draft(did)
        with session_scope() as s:
            d = s.get(Draft, did)
            sugg = d.suggestion or {}
            ids: list[str] = []
            if d.building_id:
                ids = [c.id for c in d.candidates if c.id in new_ids and c.status in REVIEWABLE and c.plan_score >= 0.6]
                for c in d.candidates:
                    if c.id in ids:
                        c.status = "approved"
            elif sugg.get("confidence", 0) >= threshold and (sugg.get("building_id") or sugg.get("place")):
                _, ids = confirm_draft(
                    s, d, building_id=sugg.get("building_id"), place=sugg.get("place"), min_plan_score=0.6, chosen=False
                )
                auto_buildings += 1
            to_convert += ids
    submit_conversions(to_convert)

    with session_scope() as s:
        to_review = sum(
            1 for did in draft_ids if any(c.status in REVIEWABLE for c in s.get(Draft, did).candidates)
        )
    progress(
        1.0,
        f"Found {len(new_ids)} plans in {len(draft_ids)} buildings",
        {**last, "phase": "finished", "buildings_found": len(draft_ids), "buildings_matched": len(draft_ids),
         "current": None, "current_url": None},
    )
    return {
        "found": len(new_ids),
        "buildings": len(draft_ids),
        "auto_buildings": auto_buildings,
        "auto_processed": len(to_convert),
        "to_review": to_review,
        "stopped": stopped,
        "from_saved": fetcher.from_saved,
        "downloaded": fetcher.downloaded,
    }


@jobs.handler("process_floor")
def _process_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    progress(0.1, "Finding rooms")
    return process_floor(
        payload["floor_id"], payload.get("converter"), payload.get("keep_placement", False), split=payload.get("split", True)
    )


@jobs.handler("approve_candidate")
def _approve_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    progress(0.1, "Finding rooms")
    floor_id = approve_candidate(payload["candidate_id"], payload.get("building_id"), payload.get("level"))
    return {"floor_id": floor_id}


@jobs.handler("match_candidates")
def _match_job(payload: dict[str, Any], progress: jobs.Progress) -> dict[str, Any]:
    draft_ids = list(payload.get("draft_ids") or [])
    with session_scope() as s:
        for cid in payload.get("candidate_ids") or []:
            c = s.get(Candidate, cid)
            if c is None:
                continue
            d = c.draft or assign_draft(s, c)
            if d.id not in draft_ids:
                draft_ids.append(d.id)
        for did in draft_ids:
            d = s.get(Draft, did)
            if d and d.suggestion and d.suggestion.get("chosen") and not d.building_id:
                d.suggestion = None  # asked to look again
            if d:
                d.match_state = "pending"
    for i, did in enumerate(draft_ids):
        progress(i / max(len(draft_ids), 1), f"Finding building {i + 1} of {len(draft_ids)}")
        match_draft(did)
    return {"matched": len(draft_ids)}
