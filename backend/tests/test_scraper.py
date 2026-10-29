import cv2
import httpx
import numpy as np
import pytest

from floored import http
from floored.geo.matching import find_addresses, name_similarity, normalize_name
from floored.scraper import classify
from floored.scraper.crawler import CrawlConfig, crawl
from floored.scraper.extract import parse_level, parse_page
from floored.scraper.fetch import Fetcher
from synth import draw_plan


@pytest.mark.parametrize(
    "text,level",
    [
        ("Memorial Union 3rd Floor", 3),
        ("Second Floor Plan", 2),
        ("Basement", -1),
        ("level-2.pdf", 2),
        ("engineering_fl4.png", 4),
        ("Ground Floor", 1),
        ("Floor B2", -2),
        ("Main hall", None),
    ],
)
def test_parse_level(text, level):
    got = parse_level(text)
    assert (got[0] if got else None) == level


def test_names_and_addresses():
    assert normalize_name("Engineering Hall - 3rd Floor Plan") == "engineering hall"
    assert name_similarity("MU", "Memorial Union") >= 0.85
    assert name_similarity("Engr. Hall", "Engineering Hall") > 0.95
    assert name_similarity("Chemistry Building", "Memorial Union") < 0.6
    assert find_addresses("Visit us at 800 Langdon St, Madison, WI 53706 today") == ["800 Langdon St, Madison, WI 53706"]


def test_parse_page_scores_plan_links():
    html = """
    <html><head><title>Building Floor Plans | Facilities</title></head><body>
    <nav><a href="/about">About</a></nav>
    <h2>Memorial Union</h2><p>800 Langdon St, Madison, WI 53706</p>
    <ul><li><a href="/plans/mu-1.pdf">First Floor</a></li>
        <li><a href="/plans/mu-2.pdf">Second Floor</a></li></ul>
    <h2>Engineering Hall</h2>
    <a href="/plans/eh_fl3.png">Floor 3</a>
    <img src="/img/logo.png" alt="logo">
    <a href="/buildings/chemistry">Chemistry building</a>
    <a href="https://other.org/x">elsewhere</a>
    </body></html>"""
    info = parse_page("https://fac.example.edu/plans", html, ["fac.example.edu"])
    by_url = {a.url.rsplit("/", 1)[-1]: a for a in info.assets}
    assert set(by_url) == {"mu-1.pdf", "mu-2.pdf", "eh_fl3.png"}
    assert by_url["mu-2.pdf"].level[0] == 2
    assert by_url["mu-1.pdf"].building_guess == "Memorial Union"
    assert by_url["eh_fl3.png"].building_guess == "Engineering Hall"
    assert by_url["mu-1.pdf"].address_guess.startswith("800 Langdon St")
    links = dict(info.links)
    assert "https://fac.example.edu/buildings/chemistry" in links
    assert not any("other.org" in u for u in links)


def test_classifier_separates_plans_from_photos():
    png, _ = draw_plan()
    plan = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    rng = np.random.default_rng(1)
    photo = cv2.GaussianBlur(rng.integers(0, 255, (600, 900, 3), dtype=np.uint8), (15, 15), 0)
    assert classify.plan_likeness(plan) > 0.6
    assert classify.plan_likeness(photo) < 0.4


def _site():
    png, _ = draw_plan()
    _, noise = cv2.imencode(".jpg", np.random.default_rng(2).integers(0, 255, (500, 700, 3), dtype=np.uint8))
    pages = {
        "/": '<a href="/facilities">Facilities</a> <a href="/news">News</a>',
        "/facilities": '<title>Floor plans</title><h2>Memorial Union</h2><a href="/plans/mu_floor2.png">Floor 2</a>'
        '<a href="/plans/team-photo.jpg">Floor plan team photo</a>',
        "/news": "<p>nothing</p>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if path in pages:
            return httpx.Response(200, html=pages[path])
        if path == "/plans/mu_floor2.png":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        if path == "/plans/team-photo.jpg":
            return httpx.Response(200, content=noise.tobytes(), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    return handler


def test_crawl_finds_plan_and_skips_photo(settings):
    http.set_transport(httpx.MockTransport(_site()))
    found = list(crawl(CrawlConfig(["https://uni.example.edu/"], []), fetcher=Fetcher(delay_s=0)))
    assert [f.asset_url.rsplit("/", 1)[-1] for f in found] == ["mu_floor2.png"]
    plan = found[0]
    assert plan.level == 2
    assert plan.building_guess == "Memorial Union"


def test_robots_respected(settings):
    http.set_transport(httpx.MockTransport(_site()))
    assert not Fetcher(delay_s=0).allowed("https://uni.example.edu/private/x")
