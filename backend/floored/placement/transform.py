"""How a floor plan image sits on the map.

A ``Placement`` says where the centre of the image goes, which compass
bearing the top of the image faces, and how many metres the full image spans
in each direction. It is the replacement for the old two-corner bounding box
"resizer": it adds rotation and keeps the maths in metres, so a plan can be
fitted to the real building outline and dragged, rotated and resized in the
UI without distortion.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from ..geo.projection import LocalFrame


@dataclass
class Placement:
    center_lon: float
    center_lat: float
    width_m: float
    height_m: float
    # Compass bearing of the image's "up" direction: 0 = north, 90 = east.
    rotation_deg: float = 0.0
    mirror: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Placement:
        return cls(
            center_lon=float(d["center_lon"]),
            center_lat=float(d["center_lat"]),
            width_m=float(d["width_m"]),
            height_m=float(d["height_m"]),
            rotation_deg=float(d.get("rotation_deg", 0.0)),
            mirror=bool(d.get("mirror", False)),
        )

    @classmethod
    def from_bounds(cls, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> Placement:
        """North-up placement filling a lon/lat box (what the old resizer did)."""
        cx, cy = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
        frame = LocalFrame(cx, cy)
        x0, y0 = frame.to_local(min_lon, min_lat)
        x1, y1 = frame.to_local(max_lon, max_lat)
        return cls(cx, cy, abs(x1 - x0), abs(y1 - y0))

    @property
    def frame(self) -> LocalFrame:
        return LocalFrame(self.center_lon, self.center_lat)

    def pixel_to_local(self, x: float, y: float, w: int, h: int) -> tuple[float, float]:
        """Image pixel -> metres east/north of the placement centre."""
        u = (x / w - 0.5) * self.width_m
        v = (0.5 - y / h) * self.height_m
        if self.mirror:
            u = -u
        t = math.radians(self.rotation_deg)
        c, s = math.cos(t), math.sin(t)
        return u * c + v * s, -u * s + v * c

    def local_to_pixel(self, ex: float, ny: float, w: int, h: int) -> tuple[float, float]:
        t = math.radians(self.rotation_deg)
        c, s = math.cos(t), math.sin(t)
        u, v = ex * c - ny * s, ex * s + ny * c
        if self.mirror:
            u = -u
        return (u / self.width_m + 0.5) * w, (0.5 - v / self.height_m) * h

    def pixel_to_lonlat(self, x: float, y: float, w: int, h: int) -> tuple[float, float]:
        return self.frame.to_lonlat(*self.pixel_to_local(x, y, w, h))

    def lonlat_to_pixel(self, lon: float, lat: float, w: int, h: int) -> tuple[float, float]:
        return self.local_to_pixel(*self.frame.to_local(lon, lat), w, h)

    def ring_to_lonlat(self, ring: Iterable[Iterable[float]], w: int, h: int) -> list[list[float]]:
        out = [list(self.pixel_to_lonlat(p[0], p[1], w, h)) for p in ring]
        if out and out[0] != out[-1]:
            out.append(list(out[0]))
        return out

    def corners(self, w: int, h: int) -> list[list[float]]:
        """Image corners as lon/lat, in the order MapLibre's image source wants: TL, TR, BR, BL."""
        return [list(self.pixel_to_lonlat(x, y, w, h)) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]

    def metres_per_pixel(self, w: int, h: int) -> tuple[float, float]:
        return self.width_m / w, self.height_m / h
