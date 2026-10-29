"""Local metric frame around a reference point.

A building is small enough that an equirectangular projection centred on it
is accurate to well under a centimetre, and it keeps the maths transparent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6378137.0


@dataclass(frozen=True)
class LocalFrame:
    lon0: float
    lat0: float

    @property
    def _kx(self) -> float:
        return math.radians(1) * EARTH_RADIUS_M * math.cos(math.radians(self.lat0))

    @property
    def _ky(self) -> float:
        return math.radians(1) * EARTH_RADIUS_M

    def to_local(self, lon: float, lat: float) -> tuple[float, float]:
        """lon/lat -> metres east, metres north."""
        return (lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self.lon0 + x / self._kx, self.lat0 + y / self._ky
