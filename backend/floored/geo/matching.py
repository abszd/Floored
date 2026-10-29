"""Match a scraped floor plan to a building.

Scraped pages rarely carry a clean address. Usually there is a building name
("Memorial Union", "MU", "Engineering Hall - 3rd Floor") and sometimes a
street address somewhere in the page text. We first try buildings we already
know, then geocode, and score how much we trust the result.
"""

from __future__ import annotations

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


def resolve(
    building_name: str | None,
    address: str | None,
    location_hint: str | None,
    known: Iterable[KnownBuilding] = (),
) -> Match | None:
    """Best guess for which building a plan belongs to, or ``None``."""
    known = list(known)
    if building_name:
        m = match_known(building_name, known)
        if m:
            return m

    queries: list[tuple[str, float]] = []
    if address:
        q = address if not location_hint or location_hint.lower() in address.lower() else f"{address}, {location_hint}"
        queries.append((q, 0.15))  # an explicit address is worth a bonus
    if building_name and normalize_name(building_name):
        name = re.sub(r"\s+", " ", _NOISE.sub(" ", building_name)).strip(" -–,")
        queries.append((f"{name}, {location_hint}" if location_hint else name, 0.0))

    best: tuple[float, geocoder.Place, str] | None = None
    for q, bonus in queries:
        try:
            places = geocoder.search(q, limit=5)
        except Exception:
            continue
        for p in places:
            s = min(1.0, _place_score(p, building_name) + bonus)
            if best is None or s > best[0]:
                best = (s, p, q)
        if best and best[0] >= 0.9:
            break
    if best is None:
        return None

    score, place, query = best
    by_osm = {b.osm_id: b for b in known if b.osm_id}
    if place.osm_ref and place.osm_ref in by_osm:
        b = by_osm[place.osm_ref]
        return Match(confidence=round(max(score, 0.9), 3), reason=f"geocoded to known building “{b.name}”", building_id=b.id)

    info = place.to_dict()
    if not place.is_building:
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
    if building_name and not info.get("name"):
        info["name"] = building_name
    return Match(confidence=round(score, 3), reason=f"geocoded “{query}” to {place.display_name}", place=info)
