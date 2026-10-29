"""The converter's output: a plain, documented, JSON-friendly structure.

All coordinates are in pixels of the ORIGINAL input image, x to the right and y
down, using the pixel-corner convention (the image spans [0, width] x [0, height]).
See README.md ("Output format") for the full contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

ROOM_KINDS = ("room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown")


def _pts(a) -> list:
    return [[round(float(x), 2), round(float(y), 2)] for x, y in a]


@dataclass
class Room:
    id: str
    polygon: list  # [[x, y], ...] exterior ring, not closed (first != last)
    holes: list = field(default_factory=list)  # [[[x, y], ...], ...]
    label: Optional[str] = None  # full OCR text found in the room, e.g. "Main Lounge (2218)"
    name: Optional[str] = None  # label without the room number, e.g. "Main Lounge"
    number: Optional[str] = None  # room number, e.g. "2218"
    kind: str = "room"  # one of ROOM_KINDS
    area_px: float = 0.0  # floor area in original-image square pixels
    confidence: Optional[float] = None  # 0..1, how well walls (not guesses) bound the room
    centroid: Optional[list] = None  # [x, y], a point inside the room (good label anchor)


@dataclass
class Door:
    polyline: list  # [[x, y], [x, y]] across the opening
    width_px: float  # opening width
    kind: str = "door"  # "door" (closed gap) or "opening" (split at a constriction)


@dataclass
class Wall:
    polyline: list  # [[x, y], ...] wall centre line
    thickness_px: float


@dataclass
class TextLabel:
    text: str
    quad: list  # 4 corners [[x, y], ...]
    confidence: float
    room_id: Optional[str] = None


@dataclass
class ConversionResult:
    image_size: list  # [width, height] of the original image
    outline: Optional[list]  # exterior footprint ring of the main building part, or None
    rooms: list  # [Room]
    footprint: list = field(default_factory=list)  # every footprint part: [{"polygon", "holes"}]
    walls: list = field(default_factory=list)  # [Wall]
    doors: list = field(default_factory=list)  # [Door]
    texts: list = field(default_factory=list)  # [TextLabel], every OCR'd text
    scale_hint: Optional[float] = None  # estimated pixels per metre (from door widths), or None
    debug_images: dict = field(default_factory=dict)  # name -> PNG bytes (only with debug=True)
    meta: dict = field(default_factory=dict)

    def to_dict(self, include_debug: bool = True) -> dict:
        d = {
            "image_size": list(self.image_size),
            "outline": self.outline,
            "rooms": [asdict(r) for r in self.rooms],
            "footprint": self.footprint,
            "walls": [asdict(w) for w in self.walls],
            "doors": [asdict(x) for x in self.doors],
            "texts": [asdict(t) for t in self.texts],
            "scale_hint": self.scale_hint,
            "meta": self.meta,
        }
        if include_debug:
            d["debug_images"] = dict(self.debug_images)
        return d

    def to_json_dict(self) -> dict:
        """Like to_dict() but without binary debug images (safe for json.dumps)."""
        return self.to_dict(include_debug=False)

    def room(self, room_id: str) -> Optional[Room]:
        return next((r for r in self.rooms if r.id == room_id), None)
