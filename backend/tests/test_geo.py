"""Geocoding fallbacks and building matching against fake OSM services."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from floored import http, jobs
from floored.geo import geocoder, matching
from floored.geo.geocoder import query_variants

# Michigan Tech's Memorial Union Building, roughly.
MUB = [(-88.5490, 47.1170), (-88.5480, 47.1170), (-88.5480, 47.1176), (-88.5490, 47.1176), (-88.5490, 47.1170)]
CAMPUS = [(-88.56, 47.11), (-88.54, 47.11), (-88.54, 47.12), (-88.56, 47.12), (-88.56, 47.11)]


def _way(id_, ring, **tags):
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in ring]}


class FakeOSM:
    """Nominatim, Photon and Overpass, answering from small tables."""

    def __init__(self, nominatim=None, photon=None, overpass=None):
        self.nominatim = nominatim or {}
        self.photon = photon or {}
        self.overpass = overpass or (lambda q: [])
        self.queries: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "nominatim.openstreetmap.org":
            q = req.url.params.get("q", "")
            self.queries.append(q)
            return httpx.Response(200, json=self.nominatim.get(q, []))
        if req.url.host == "photon.komoot.io":
            q = req.url.params.get("q", "")
            self.queries.append(f"photon:{q}")
            return httpx.Response(200, json={"type": "FeatureCollection", "features": self.photon.get(q, [])})
        if req.url.host == "overpass-api.de":
            query = parse_qs(req.content.decode())["data"][0]
            return httpx.Response(200, json={"elements": self.overpass(query)})
        return httpx.Response(404)


@pytest.fixture()
def osm(settings):
    def install(**tables):
        fake = FakeOSM(**tables)
        http.set_transport(httpx.MockTransport(fake))
        return fake

    return install


def _street():
    return {
        "lat": "47.1172", "lon": "-88.5486", "name": "Townsend Drive", "category": "highway", "type": "secondary",
        "display_name": "Townsend Drive, Houghton, Michigan, 49931, United States", "osm_type": "way", "osm_id": 9,
    }


def test_query_variants_simplify_an_address():
    assert query_variants("Memorial Union, 1400 Townsend Dr, Suite 2, Houghton, MI 49931, USA") == [
        "Memorial Union, 1400 Townsend Dr, Houghton, MI",
        "1400 Townsend Dr, Houghton, MI",
        "Townsend Dr, Houghton, MI",
    ]
    assert query_variants("Memorial Union Building") == []


def test_search_retries_simpler_address_then_photon(osm):
    fake = osm(
        photon={
            "1400 Townsend Drive, Houghton, MI 49931": [
                {
                    "geometry": {"type": "Point", "coordinates": [-88.5485, 47.1173]},
                    "properties": {"osm_type": "W", "osm_id": 77, "osm_key": "building", "osm_value": "university",
                                   "name": "Memorial Union Building", "housenumber": "1400", "street": "Townsend Drive",
                                   "city": "Houghton", "state": "Michigan"},
                }
            ]
        }
    )
    places = geocoder.search("1400 Townsend Drive, Houghton, MI 49931")
    assert fake.queries == [
        "1400 Townsend Drive, Houghton, MI 49931",
        "1400 Townsend Drive, Houghton, MI",
        "Townsend Drive, Houghton, MI",
        "photon:1400 Townsend Drive, Houghton, MI 49931",
    ]
    assert places[0].name == "Memorial Union Building"
    assert places[0].osm_ref == "way/77"
    assert places[0].lat == pytest.approx(47.1173)


def test_campus_outline_is_not_a_building():
    campus = geocoder.Place("Michigan Tech", "", 47.115, -88.55, category="amenity", kind="university",
                            footprint={"type": "Polygon", "coordinates": [CAMPUS]})
    hall = geocoder.Place("MUB", "", 47.117, -88.548, category="amenity", kind="community_centre",
                          footprint={"type": "Polygon", "coordinates": [MUB]})
    # A university-tagged outline could be either; the OSM building at that point decides.
    ambiguous = geocoder.Place("MUB", "", 47.117, -88.548, category="amenity", kind="university",
                               footprint={"type": "Polygon", "coordinates": [MUB]})
    tagged = geocoder.Place("MUB", "", 47.117, -88.548, category="building", kind="university",
                            footprint={"type": "Polygon", "coordinates": [CAMPUS]})
    assert not campus.is_building
    assert hall.is_building
    assert not ambiguous.is_building
    assert tagged.is_building


def test_resolve_finds_building_by_house_number(osm):
    def overpass(q):
        if "addr:housenumber" in q:
            assert '"1400"' in q and '"Townsend"' in q
            return [_way(500, MUB, building="university", name="Memorial Union Building",
                         **{"addr:housenumber": "1400", "addr:street": "Townsend Drive"})]
        return []

    osm(nominatim={"1400 Townsend Drive, Houghton, MI 49931": [_street()]}, overpass=overpass)
    m = matching.resolve(None, "1400 Townsend Drive, Houghton, MI 49931", "Houghton, MI")
    assert m is not None and m.is_new
    assert m.confidence >= 0.85  # good enough to process without review
    assert m.place["osm_id"] == "way/500"
    assert m.place["name"] == "Memorial Union Building"
    assert m.place["footprint"]["type"] == "Polygon"


def test_resolve_finds_named_building_in_hinted_town(osm):
    town = {"lat": "47.12", "lon": "-88.57", "name": "Houghton", "category": "boundary", "type": "administrative",
            "display_name": "Houghton, Michigan", "osm_type": "relation", "osm_id": 1,
            "boundingbox": ["47.10", "47.13", "-88.61", "-88.53"]}

    def overpass(q):
        if '"name"~"Memorial.*Union"' in q:
            return [_way(500, MUB, building="university", name="Memorial Union Building")]
        return []

    osm(nominatim={"Houghton, MI": [town]}, overpass=overpass)
    m = matching.resolve("Memorial Union Building", None, "Houghton, MI")
    assert m is not None and m.place["osm_id"] == "way/500"
    assert m.confidence >= 0.85


def test_two_buildings_with_the_same_name_need_review(osm):
    town = {"lat": "47.12", "lon": "-88.57", "name": "Houghton", "category": "boundary", "type": "administrative",
            "display_name": "Houghton, Michigan", "osm_type": "relation", "osm_id": 1,
            "boundingbox": ["47.10", "47.13", "-88.61", "-88.53"]}
    other = [(x + 0.01, y) for x, y in MUB]

    def overpass(q):
        if '"name"~"Library"' in q:
            return [_way(1, MUB, building="yes", name="Library"), _way(2, other, building="yes", name="Library")]
        return []

    osm(nominatim={"Houghton, MI": [town]}, overpass=overpass)
    m = matching.resolve("Library", None, "Houghton, MI")
    assert m is not None and m.confidence < 0.85


def test_add_building_by_address(osm):
    def overpass(q):
        if "addr:housenumber" in q:
            return [_way(500, MUB, building="university", name="Memorial Union Building",
                         **{"addr:housenumber": "1400", "addr:street": "Townsend Drive"})]
        return []

    osm(nominatim={"1400 Townsend Drive, Houghton, MI 49931": [_street()]}, overpass=overpass)
    jobs.synchronous = True
    from floored.api.app import app

    try:
        with TestClient(app) as client:
            r = client.post("/api/buildings", json={"address": "1400 Townsend Drive, Houghton, MI 49931"})
            assert r.status_code == 201, r.text
            b = r.json()
            assert b["name"] == "Memorial Union Building"
            assert b["footprint"]["type"] == "Polygon"
            # The same address again finds the building we already have.
            again = client.post("/api/buildings", json={"address": "1400 Townsend Drive, Houghton, MI 49931"}).json()
            assert again["id"] == b["id"]
            missing = client.post("/api/buildings", json={"address": "1 Nowhere Lane, Atlantis"})
            assert missing.status_code == 404
    finally:
        jobs.synchronous = False


def test_blocked_geocoder_explains_itself(settings):
    http.set_transport(httpx.MockTransport(lambda req: httpx.Response(403)))
    from floored.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/geocode", params={"q": "Memorial Union"})
    assert r.status_code == 502
    assert "FLOORED_USER_AGENT" in r.json()["detail"]
