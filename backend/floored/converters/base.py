"""The contract between Floored and whatever turns a floor plan image into rooms.

Every coordinate is in pixels of the original input image, with y pointing
down. This mirrors ``floorplan_converter.ConversionResult.to_dict()`` from the
top-level ``converter/`` package, so either converter can be swapped in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Point = list[float]
Ring = list[Point]

ROOM_KINDS = ("room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown")


@dataclass
class Room:
    id: str
    polygon: Ring
    holes: list[Ring] = field(default_factory=list)
    label: str | None = None
    kind: str = "unknown"
    area_px: float = 0.0
    confidence: float | None = None


@dataclass
class ConversionResult:
    image_size: tuple[int, int]
    rooms: list[Room]
    outline: Ring | None = None
    walls: list[list[Point]] = field(default_factory=list)
    doors: list[list[Point]] = field(default_factory=list)
    scale_hint: float | None = None  # pixels per metre, if a scale bar was read
    debug_images: dict[str, bytes] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_debug: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "image_size": list(self.image_size),
            "outline": self.outline,
            "rooms": [
                {
                    "id": r.id,
                    "polygon": r.polygon,
                    "holes": r.holes,
                    "label": r.label,
                    "kind": r.kind,
                    "area_px": r.area_px,
                    "confidence": r.confidence,
                }
                for r in self.rooms
            ],
            "walls": self.walls,
            "doors": self.doors,
            "scale_hint": self.scale_hint,
            "meta": self.meta,
        }
        if include_debug:
            out["debug_images"] = self.debug_images
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConversionResult:
        rooms = [
            Room(
                id=str(r.get("id", i)),
                polygon=[list(map(float, p)) for p in r["polygon"]],
                holes=[[list(map(float, p)) for p in h] for h in (r.get("holes") or [])],
                label=r.get("label"),
                kind=r.get("kind") if r.get("kind") in ROOM_KINDS else "unknown",
                area_px=float(r.get("area_px") or 0.0),
                confidence=r.get("confidence"),
            )
            for i, r in enumerate(d.get("rooms") or [])
            if r.get("polygon") and len(r["polygon"]) >= 3
        ]
        outline = d.get("outline")
        w, h = d["image_size"]
        return cls(
            image_size=(int(w), int(h)),
            rooms=rooms,
            outline=[list(map(float, p)) for p in outline] if outline else None,
            walls=d.get("walls") or [],
            doors=d.get("doors") or [],
            scale_hint=d.get("scale_hint"),
            debug_images=d.get("debug_images") or {},
            meta=d.get("meta") or {},
        )


class Converter(Protocol):
    name: str

    def convert(self, image: bytes) -> ConversionResult: ...
