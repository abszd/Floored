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


HOUGHTON = {"lat": "47.12", "lon": "-88.57", "name": "Houghton", "category": "boundary", "type": "administrative",
            "display_name": "Houghton, Michigan", "osm_type": "relation", "osm_id": 1,
            "boundingbox": ["47.10", "47.13", "-88.61", "-88.53"]}
MADISON_UNION = matching.KnownBuilding(id="wi", name="Memorial Union", osm_id="way/123", lat=43.0761, lon=-89.4003)


def _mub_overpass(q):
    if "addr:housenumber" in q:
        return [_way(500, MUB, building="university", name="Memorial Union Building",
                     **{"addr:housenumber": "1400", "addr:street": "Townsend Drive"})]
    if '"name"~"Memorial.*Union"' in q:
        return [_way(500, MUB, building="university", name="Memorial Union Building")]
    return []


def test_same_name_in_another_state_is_not_a_match(osm):
    osm(nominatim={"Houghton, MI": [HOUGHTON], "1400 Townsend Drive, Houghton, MI": [_street()]}, overpass=_mub_overpass)
    m = matching.resolve("Memorial Union", "1400 Townsend Drive", "Houghton, MI", [MADISON_UNION])
    assert m is not None and m.building_id is None
    assert m.place["osm_id"] == "way/500"
    # Without any location evidence the known building still wins.
    assert matching.resolve("Memorial Union", None, None, [MADISON_UNION]).building_id == "wi"


def test_results_outside_the_hinted_town_are_dropped(osm):
    madison = {"lat": "43.0761", "lon": "-89.4003", "name": "Memorial Union", "category": "building", "type": "university",
               "display_name": "Memorial Union, Madison, Wisconsin", "osm_type": "way", "osm_id": 123}
    osm(nominatim={"Houghton, MI": [HOUGHTON], "Memorial Union, Houghton, MI": [madison]})
    assert matching.resolve("Memorial Union", None, "Houghton, MI") is None


def test_lookup_previews_without_creating(osm):
    osm(nominatim={"Houghton, MI": [HOUGHTON], "1400 Townsend Drive, Houghton, MI": [_street()]}, overpass=_mub_overpass)
    from floored.api.app import app

    with TestClient(app) as client:
        r = client.post("/api/lookup", json={"name": "Memorial Union", "address": "1400 Townsend Drive", "hint": "Houghton, MI"})
        assert r.status_code == 200, r.text
        found = r.json()
        assert found["building"] is None
        assert found["place"]["osm_id"] == "way/500" and found["place"]["footprint"]["type"] == "Polygon"
        assert client.get("/api/buildings").json() == []


def test_suggestions_come_from_photon_near_the_hint(osm):
    fake = osm(
        nominatim={"Houghton, MI": [HOUGHTON]},
        photon={
            "1400 towns": [
                {"geometry": {"type": "Point", "coordinates": [-88.5485, 47.1173]},
                 "properties": {"osm_type": "N", "osm_id": 5, "osm_key": "place", "osm_value": "house",
                                "housenumber": "1400", "street": "Townsend Drive", "city": "Houghton",
                                "state": "Michigan", "postcode": "49931", "country": "United States"}}
            ]
        },
    )
    from floored.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/suggest", params={"q": "1400 towns", "hint": "Houghton, MI"})
        assert r.status_code == 200
        [s] = r.json()
        assert s["display_name"] == "1400 Townsend Drive, Houghton, Michigan, 49931, United States"
        assert client.get("/api/suggest", params={"q": "14"}).json() == []  # too short to bother
    assert "photon:1400 towns" in fake.queries


def test_suggestions_never_error(settings):
    http.set_transport(httpx.MockTransport(lambda req: httpx.Response(500)))
    from floored.api.app import app

    with TestClient(app) as client:
        assert client.get("/api/suggest", params={"q": "1400 Townsend"}).json() == []


def test_lookup_at_a_picked_suggestion_uses_the_building_there(osm):
    osm(overpass=lambda q: [_way(500, MUB, building="university", name="Memorial Union Building")] if "around:25" in q else [])
    from floored.api.app import app

    with TestClient(app) as client:
        r = client.post("/api/lookup", json={"lat": 47.1173, "lon": -88.5485, "address": "1400 Townsend Drive, Houghton, Michigan"})
        assert r.status_code == 200, r.text
        place = r.json()["place"]
        assert place["osm_id"] == "way/500" and place["name"] == "Memorial Union Building"
        assert place["footprint"]["type"] == "Polygon"
        # Somewhere with no building: the bare point, labelled by the address.
        bare = client.post("/api/lookup", json={"lat": 40.0, "lon": -88.0, "address": "5 Field Road, Nowhere"}).json()["place"]
        assert bare["footprint"] is None and bare["name"] == "5 Field Road"


def test_relation_buildings_and_multi_part_outlines(osm):
    from shapely.geometry import shape

    from floored.geo import footprints

    a = [(-88.5490, 47.1150), (-88.5480, 47.1150), (-88.5480, 47.1156), (-88.5490, 47.1156)]
    b = [(-88.5470, 47.1150), (-88.5460, 47.1150), (-88.5460, 47.1156), (-88.5470, 47.1156)]
    hole = [(-88.5488, 47.1152), (-88.5484, 47.1152), (-88.5484, 47.1154), (-88.5488, 47.1154)]

    def member(ring, role):
        pts = ring + ring[:1]
        return {"type": "way", "role": role, "geometry": [{"lon": x, "lat": y} for x, y in pts]}

    rel = {"type": "relation", "id": 77, "tags": {"building": "yes", "type": "multipolygon", "name": "Twin Halls"},
           "members": [member(a, "outer"), member(b, "outer"), member(hole, "inner")]}
    osm(overpass=lambda q: [rel] if "rel" in q else [])
    found = footprints.buildings_near(47.1155, -88.5485, 60)
    assert found and found[0]["osm_id"] == "relation/77" and found[0]["name"] == "Twin Halls"
    g = shape(found[0]["footprint"])
    assert g.geom_type == "MultiPolygon" and len(g.geoms) == 2
    assert len(max(g.geoms, key=lambda p: len(p.interiors)).interiors) == 1  # the courtyard

    # Wings mapped as separate ways: touching ones fuse, apart ones stay parts.
    touching = [(-88.5480, 47.1150), (-88.5475, 47.1150), (-88.5475, 47.1156), (-88.5480, 47.1156)]
    poly = lambda r: {"type": "Polygon", "coordinates": [[list(p) for p in r + r[:1]]]}
    assert footprints.combine(poly(a), poly(touching))["type"] == "Polygon"
    two = footprints.combine(poly(a), poly(b))
    assert two["type"] == "MultiPolygon"
    one = footprints.remove_part(two, 47.1153, -88.5465)
    assert one["type"] == "Polygon"
    assert footprints.remove_part(one, 47.1153, -88.5485) is None
