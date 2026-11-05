"""Match a scraped floor plan to a building.

Scraped pages rarely carry a clean address. Usually there is a building name
("Memorial Union", "MU", "Engineering Hall - 3rd Floor") and sometimes a
street address somewhere in the page text. We first try buildings we already
know, then geocode, and score how much we trust the result.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from rapidfuzz import fuzz

from . import footprints, geocoder

_ABBREVIATIONS = {
    "bldg": "building",
    "bld": "building",
    "ctr": "center",
    "cntr": "center",
    "centre": "center",
    "hl": "hall",
    "lib": "library",
    "univ": "university",
    "sci": "science",
    "eng": "engineering",
    "engr": "engineering",
    "st": "street",
    "ave": "avenue",
    "av": "avenue",
    "blvd": "boulevard",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "pl": "place",
    "ct": "court",
    "hwy": "highway",
    "pkwy": "parkway",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}

# Words that describe the drawing rather than the building.
_NOISE = re.compile(
    r"\b(floor\s*plans?|floorplans?|plans?|maps?|layout|pdf|png|jpe?g|"
    r"(?:\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth|ground|lower|upper|main|basement|sub-?basement)"
    r"\s*(?:floor|level|flr|fl)|(?:floor|level|flr|fl)\s*[-#]?\s*[0-9a-z]{1,2}|basement)\b",
    re.I,
)

_STREET_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z0-9][\w.'-]*\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Way|Place|Pl|Court|Ct|"
    r"Parkway|Pkwy|Highway|Hwy|Mall|Circle|Cir|Terrace|Ter|Square|Sq)\b\.?"
    r"(?:\s*,?\s*(?:[A-Z][\w.'-]*\s*){1,3},?\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)?",
)


def normalize_name(name: str) -> str:
    s = _NOISE.sub(" ", name.lower())
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    words = [_ABBREVIATIONS.get(w, w) for w in s.split()]
    return " ".join(words).strip()


def clean_name(name: str | None) -> str:
    """A scraped building name without the words about the drawing ("Engineering Hall")."""
    if not name or not normalize_name(name):
        return ""
    return re.sub(r"\s+", " ", _NOISE.sub(" ", name)).strip(" -–,")


def find_addresses(text: str) -> list[str]:
    """Street addresses mentioned in free text, in order of appearance."""
    seen, out = set(), []
    for m in _STREET_ADDRESS.finditer(text or ""):
        addr = re.sub(r"\s+", " ", m.group(0)).strip(" ,.")
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    # token_set handles "Memorial Union" vs "Memorial Union Building";
    # partial handles short forms embedded in longer titles.
    score = max(fuzz.token_set_ratio(na, nb), 0.9 * fuzz.partial_ratio(na, nb))
    # Acronym match: "MU" vs "Memorial Union".
    for short, long_ in ((na, nb), (nb, na)):
        if " " not in short and 2 <= len(short) <= 5:
            initials = "".join(w[0] for w in long_.split() if w not in ("of", "the", "and", "&"))
            if short == initials:
                score = max(score, 85)
    return score / 100.0


@dataclass
class KnownBuilding:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    osm_id: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass
class Match:
    confidence: float
    reason: str
    building_id: str | None = None
    # Set when the match is a place we don't have yet.
    place: dict[str, Any] | None = None

    @property
    def is_new(self) -> bool:
        return self.building_id is None and self.place is not None


def match_known(name: str, known: Iterable[KnownBuilding]) -> Match | None:
    best: tuple[float, KnownBuilding] | None = None
    for b in known:
        sim = max([name_similarity(name, b.name)] + [name_similarity(name, a) for a in b.aliases])
        if best is None or sim > best[0]:
            best = (sim, b)
    if best and best[0] >= 0.88:
        sim, b = best
        return Match(confidence=round(min(0.98, sim), 3), reason=f"name matches known building “{b.name}”", building_id=b.id)
    return None


def _place_score(place: geocoder.Place, wanted_name: str | None) -> float:
    score = 0.45 if place.is_building else 0.15
    if place.category in ("building", "amenity"):
        score += 0.1
    if wanted_name:
        score += 0.45 * max(name_similarity(wanted_name, place.name), name_similarity(wanted_name, place.display_name))
    else:
        score += 0.2
    return min(score, 1.0)


def _search(query: str) -> list[geocoder.Place]:
    try:
        return geocoder.search(query, limit=5)
    except Exception:
        return []


def _split_address(address: str) -> tuple[str, str] | None:
    """("1400 Townsend Drive, Houghton, MI") -> ("1400", "Townsend Drive")."""
    m = re.match(r"\s*(\d{1,6}[a-z]?)\s+([^,]+)", address, re.I)
    return (m.group(1), m.group(2).strip()) if m else None


def _osm_names(b: dict[str, Any]) -> list[str]:
    tags = b.get("tags") or {}
    return [tags[k] for k in ("name", "alt_name", "official_name", "short_name") if tags.get(k)] or ([b["name"]] if b.get("name") else [])


def _from_outline(b: dict[str, Any], fallback_name: str | None, address: str | None) -> dict[str, Any]:
    from shapely.geometry import shape

    c = shape(b["footprint"]).centroid
    name = b.get("name") or fallback_name or address or "Unnamed building"
    return {
        "name": name,
        "display_name": ", ".join(dict.fromkeys(filter(None, [b.get("name"), address]))) or name,
        "lat": c.y,
        "lon": c.x,
        "osm_id": b["osm_id"],
        "footprint": b["footprint"],
    }


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, (lat2 - lat1) * 111_320)


def _in_area(lat: float, lon: float, area: geocoder.Place, margin_m: float = 10_000) -> bool:
    """Whether a point lies in the hinted town or region (with some slack around it)."""
    if area.bbox:
        min_lon, min_lat, max_lon, max_lat = area.bbox
        dlat = margin_m / 111_320
        dlon = margin_m / (111_320 * max(math.cos(math.radians(lat)), 0.1))
        return min_lat - dlat <= lat <= max_lat + dlat and min_lon - dlon <= lon <= max_lon + dlon
    return _distance_m(lat, lon, area.lat, area.lon) <= margin_m + 15_000


def _search_radius(place: geocoder.Place) -> float:
    """How far around a city or neighbourhood hint to look for a named building."""
    if not place.bbox:
        return 3000.0
    min_lon, min_lat, max_lon, max_lat = place.bbox
    half_diag = 0.5 * math.hypot((max_lon - min_lon) * 111_320 * math.cos(math.radians(place.lat)), (max_lat - min_lat) * 111_320)
    return float(min(max(half_diag, 1000.0), 8000.0))


def resolve(
    building_name: str | None,
    address: str | None,
    location_hint: str | None,
    known: Iterable[KnownBuilding] = (),
) -> Match | None:
    """Best guess for which building a plan belongs to, or ``None``.

    Evidence is gathered in order of strength: a building we already have by
    name; the OSM building carrying the page's street address; an OSM
    building with that name near the address or inside the hinted area; and
    finally whatever the geocoder makes of the name or address.
    """
    known = list(known)
    name = clean_name(building_name)

    # Where the plan is: the hinted town, then the page's street address within it.
    area: geocoder.Place | None = None
    if location_hint:
        hint_places = _search(location_hint)
        area = hint_places[0] if hint_places else None
    anchor: geocoder.Place | None = None
    address_places: list[geocoder.Place] = []
    if address:
        q = address if not location_hint or location_hint.lower() in address.lower() else f"{address}, {location_hint}"
        address_places = [p for p in _search(q) if area is None or _in_area(p.lat, p.lon, area)]
        anchor = address_places[0] if address_places else None

    def plausible(lat: float | None, lon: float | None) -> bool:
        """A same-named building elsewhere (Memorial Union in Madison, not Houghton) is not this one."""
        if lat is None or lon is None:
            return True
        if anchor is not None:
            return _distance_m(lat, lon, anchor.lat, anchor.lon) <= 2_000
        return area is None or _in_area(lat, lon, area)

    if building_name:
        m = match_known(building_name, [b for b in known if plausible(b.lat, b.lon)])
        if m:
            return m
    by_osm = {b.osm_id: b for b in known if b.osm_id}

    # (score, place info for a new building, reason, geocoder place still needing an outline)
    options: list[tuple[float, dict[str, Any], str, geocoder.Place | None]] = []

    if anchor is not None:
        for p in address_places[:3]:
            options.append((min(1.0, _place_score(p, name or None) + 0.15), p.to_dict(), f"geocoded “{address}” to {p.display_name}", p))
        parts = _split_address(address or "")
        if parts:
            try:
                b = footprints.building_at_address(*parts, anchor.lat, anchor.lon)
            except Exception:
                b = None
            if b:
                sim = max((name_similarity(name, n) for n in _osm_names(b)), default=0.0) if name else 0.5
                options.append((0.85 + 0.1 * sim, _from_outline(b, name, address), f"OSM building at {address}", None))

    if name:
        center, radius = anchor, 800.0
        if center is None and area is not None:
            center, radius = area, _search_radius(area)
        if center is not None:
            try:
                named = footprints.buildings_named(name, center.lat, center.lon, radius)
            except Exception:
                named = []
            scored = sorted(
                ((max(name_similarity(name, n) for n in _osm_names(b)), b) for b in named if _osm_names(b)),
                key=lambda t: -t[0],
            )
            if scored and scored[0][0] >= 0.75:
                sim, b = scored[0]
                score = 0.5 + 0.45 * sim
                if len(scored) > 1 and scored[1][0] >= sim - 0.05:
                    score -= 0.15  # two buildings with the same name nearby
                elif anchor is not None and b["distance_m"] < 400:
                    score += 0.03
                where = "near the address" if anchor is not None else f"in {location_hint}"
                options.append((min(score, 0.97), _from_outline(b, name, address), f"OSM building “{b.get('name')}” {where}", None))

        q = f"{name}, {location_hint}" if location_hint else name
        for p in _search(q):
            options.append((_place_score(p, name), p.to_dict(), f"geocoded “{q}” to {p.display_name}", p))

    options = [o for o in options if plausible(o[1].get("lat"), o[1].get("lon"))]
    if not options:
        return None
    score, info, reason, place = max(options, key=lambda o: o[0])
    if info.get("osm_id") in by_osm:
        b = by_osm[info["osm_id"]]
        return Match(confidence=round(max(score, 0.9), 3), reason=f"{reason}: known building “{b.name}”", building_id=b.id)

    if place is not None and not place.is_building:
        try:
            fp = footprints.footprint_at(place.lat, place.lon)
        except Exception:
            fp = None
        if fp:
            info["footprint"] = fp["footprint"]
            info["osm_id"] = fp["osm_id"]
            info["name"] = fp.get("name") or info["name"]
            if fp["osm_id"] in by_osm:
                b = by_osm[fp["osm_id"]]
                return Match(confidence=round(score, 3), reason=f"point lies in known building “{b.name}”", building_id=b.id)
            score = min(1.0, score + (0.2 if fp["contains"] else 0.1))
        else:
            info["footprint"] = None  # a campus or park outline is worse than none
    if name and not info.get("name"):
        info["name"] = name
    return Match(confidence=round(score, 3), reason=reason, place=info)
