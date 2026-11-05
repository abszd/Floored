"""Hand edits to a converted plan, all in image pixels.

These work on the stored conversion dict (see ``converters.base``) and never
re-run the converter, so room numbers and other fixes the user made survive:
cropping a plan to its building, cutting a sheet that holds several floors
into one plan per floor, splitting a room with a line and drawing a room the
converter missed.
"""

from __future__ import annotations

import io
import math
from typing import Any
from uuid import uuid4

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import split as shapely_split
from shapely.ops import unary_union

from .placement.transform import Placement

Box = tuple[float, float, float, float]


def clamp_box(b: Box | list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = b
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, y0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
    x1, y1 = min(w, int(math.ceil(x1))), min(h, int(math.ceil(y1)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("the area is too small")
    return x0, y0, x1, y1


def _poly(r: dict[str, Any]) -> Polygon:
    p = Polygon(r["polygon"], [h for h in r.get("holes") or [] if len(h) >= 3])
    return p if p.is_valid else p.buffer(0)


def _ring(coords: Any) -> list[list[float]]:
    return [[round(x, 2), round(y, 2)] for x, y in list(coords)[:-1]]


def _largest(g: Any) -> Polygon | None:
    if g.is_empty:
        return None
    if g.geom_type == "Polygon":
        return g
    polys = [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon"]
    return max(polys, key=lambda p: p.area) if polys else None


def _room_with(r: dict[str, Any], poly: Polygon, **changes: Any) -> dict[str, Any]:
    return {
        **r,
        "polygon": _ring(poly.exterior.coords),
        "holes": [_ring(i.coords) for i in poly.interiors],
        "area_px": round(float(poly.area), 1),
        **changes,
    }


def _clip_lines(lines: list[list[list[float]]], clip: Polygon, dx: float, dy: float) -> list[list[list[float]]]:
    out = []
    for line in lines:
        if len(line) < 2:
            continue
        g = LineString(line).intersection(clip)
        parts = [g] if g.geom_type == "LineString" else [p for p in getattr(g, "geoms", []) if p.geom_type == "LineString"]
        for p in parts:
            if not p.is_empty and p.length > 0:
                out.append([[round(x - dx, 2), round(y - dy, 2)] for x, y in p.coords])
    return out


def crop_conversion(conv: dict[str, Any], b: Box) -> dict[str, Any]:
    """The part of a conversion inside box ``b``, moved so the box's corner is (0, 0).

    A room belongs to the box its centre is in (walls between floors drawn
    close together can make rooms poke over the edge) and is trimmed to it.
    """
    x0, y0, x1, y1 = b
    clip = box(x0, y0, x1, y1)
    rooms = []
    for r in conv.get("rooms") or []:
        if len(r.get("polygon") or []) < 3:
            continue
        p = _poly(r)
        if p.is_empty or not clip.contains(p.representative_point()):
            continue
        p = _largest(p.intersection(clip))
        if p is None or p.area < 1:
            continue
        rooms.append(_room_with(r, affinity.translate(p, -x0, -y0)))

    outline = None
    if conv.get("outline") and len(conv["outline"]) >= 3:
        o = Polygon(conv["outline"])
        o = _largest((o if o.is_valid else o.buffer(0)).intersection(clip))
        # The converter's outline is only kept if it still covers the rooms in the box
        # (it may have been the title block, or another floor on the sheet).
        if o is not None and o.area > 0:
            if rooms:
                inside = unary_union([affinity.translate(_poly(r), x0, y0) for r in rooms])
                keep = inside.intersection(o).area >= 0.6 * inside.area
            else:
                keep = True
            if keep:
                outline = _ring(affinity.translate(o, -x0, -y0).exterior.coords)

    meta = dict(conv.get("meta") or {})
    meta.pop("debug_images", None)
    meta.pop("sheets", None)
    meta["crop"] = [x0, y0, x1, y1]
    return {
        **conv,
        "image_size": [x1 - x0, y1 - y0],
        "outline": outline,
        "rooms": rooms,
        "walls": _clip_lines(conv.get("walls") or [], clip, x0, y0),
        "doors": _clip_lines(conv.get("doors") or [], clip, x0, y0),
        "meta": meta,
    }


def crop_image(image: bytes, b: Box) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(image)) as im:
        im.load()
        out = io.BytesIO()
        im.crop(tuple(int(v) for v in b)).save(out, "PNG")
        return out.getvalue()


def crop_placement(p: Placement, b: Box, w: int, h: int) -> Placement:
    """Where the cropped image sits so its content stays exactly where it was on the map."""
    x0, y0, x1, y1 = b
    lon, lat = p.pixel_to_lonlat((x0 + x1) / 2, (y0 + y1) / 2, w, h)
    return Placement(lon, lat, p.width_m * (x1 - x0) / w, p.height_m * (y1 - y0) / h, p.rotation_deg, p.mirror)


def split_room(conv: dict[str, Any], room_id: str, line: list[list[float]]) -> tuple[dict[str, Any], list[str]]:
    """Cut a room in two (or more) along a line drawn across it. The biggest piece keeps the room."""
    rooms = [dict(r) for r in conv.get("rooms") or []]
    idx = next((i for i, r in enumerate(rooms) if str(r["id"]) == str(room_id)), None)
    if idx is None:
        raise LookupError(f"room {room_id} not found")
    if len(line) < 2:
        raise ValueError("draw a line across the room")
    room = rooms[idx]
    poly = _poly(room)
    cut = LineString(line)
    # Let a line that stops just short of the walls still cut all the way through.
    minx, miny, maxx, maxy = poly.bounds
    reach = max(maxx - minx, maxy - miny)
    cut = _extend(cut, reach)
    pieces = [g for g in shapely_split(poly, cut).geoms if g.geom_type == "Polygon" and g.area > 1]
    if len(pieces) < 2:
        raise ValueError("the line doesn't cross the room")
    pieces.sort(key=lambda g: -g.area)
    new_rooms = [_room_with(room, pieces[0], confidence=None)]
    for g in pieces[1:]:
        new_rooms.append(_room_with(room, g, id=f"r{uuid4().hex[:8]}", label=None, confidence=None))
    rooms[idx : idx + 1] = new_rooms
    walls = list(conv.get("walls") or [])
    inner = cut.intersection(poly)
    for part in [inner] if inner.geom_type == "LineString" else getattr(inner, "geoms", []):
        if part.geom_type == "LineString" and part.length > 0:
            walls.append([[round(x, 2), round(y, 2)] for x, y in part.coords])
    return {**conv, "rooms": rooms, "walls": walls}, [r["id"] for r in new_rooms]


def _extend(line: LineString, by: float) -> LineString:
    coords = list(line.coords)

    def push(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        dx, dy = a[0] - b[0], a[1] - b[1]
        n = math.hypot(dx, dy) or 1.0
        return a[0] + dx / n * by * 0.05, a[1] + dy / n * by * 0.05

    coords[0] = push(coords[0], coords[1])
    coords[-1] = push(coords[-1], coords[-2])
    return LineString(coords)


def add_room(
    conv: dict[str, Any], ring: list[list[float]], *, label: str | None = None, kind: str = "room"
) -> tuple[dict[str, Any], str]:
    """A room the converter missed, drawn by hand. Rooms it overlaps give up that area."""
    poly = Polygon(ring)
    poly = _largest(poly if poly.is_valid else poly.buffer(0))
    if poly is None or poly.area < 4:
        raise ValueError("draw at least three corners around the room")
    rooms = []
    for r in conv.get("rooms") or []:
        p = _poly(r)
        if p.intersects(poly) and p.intersection(poly).area > 0.5:
            rest = _largest(p.difference(poly))
            if rest is None or rest.area < 0.05 * p.area:
                continue  # drawn over the whole room: replaced
            r = _room_with(r, rest)
        rooms.append(r)
    rid = f"r{uuid4().hex[:8]}"
    rooms.append(
        {
            "id": rid,
            "polygon": _ring(poly.exterior.coords),
            "holes": [],
            "label": label,
            "kind": kind,
            "area_px": round(float(poly.area), 1),
            "confidence": None,
        }
    )
    return {**conv, "rooms": rooms}, rid


def room_at(conv: dict[str, Any], x: float, y: float) -> str | None:
    pt = Point(x, y)
    for r in conv.get("rooms") or []:
        if len(r.get("polygon") or []) >= 3 and _poly(r).contains(pt):
            return str(r["id"])
    return None


def lines_to_pixels(p: Placement, coords: list[list[float]], w: int, h: int) -> list[list[float]]:
    return [list(p.lonlat_to_pixel(lon, lat, w, h)) for lon, lat in coords]

