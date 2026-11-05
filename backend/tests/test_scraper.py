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


def _plan_image():
    png, _ = draw_plan()
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)


def _photo(h, w, seed=3):
    """Smooth continuous-tone shading with sensor noise, like a photo."""
    rng = np.random.default_rng(seed)
    base = cv2.resize(rng.integers(0, 255, (h // 40, w // 40, 3)).astype(np.uint8), (w, h), interpolation=cv2.INTER_CUBIC)
    return np.clip(base.astype(np.int16) + rng.integers(-6, 7, (h, w, 3)), 0, 255).astype(np.uint8)


def _flyer():
    """An event flyer: a photo of people on a white page with a frame and text."""
    page = np.full((1100, 850, 3), 255, np.uint8)
    page[150:650, 175:675] = _photo(500, 500)
    cv2.rectangle(page, (40, 40), (810, 1060), (0, 0, 0), 3)
    for i in range(6):
        cv2.putText(page, "Student activities fair - join us Thursday", (60, 740 + 50 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2)
    return page


def _facade():
    """A building photographed head-on: a grid of windows under a sky gradient."""
    rng = np.random.default_rng(4)
    img = np.zeros((600, 900, 3), np.uint8)
    for y in range(600):
        img[y] = (230 - y // 6, 200 - y // 8, 160 - y // 10)
    for x in range(80, 820, 90):
        for y in range(200, 560, 80):
            cv2.rectangle(img, (x, y), (x + 50, y + 50), (60, 70, 80), -1)
    img = np.clip(img.astype(np.int16) + rng.integers(-8, 9, img.shape), 0, 255).astype(np.uint8)
    return cv2.GaussianBlur(img, (3, 3), 0)


def _tilted(img, deg):
    h, w = img.shape[:2]
    return cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1), (w, h), borderValue=(255, 255, 255))


def _jpeg(img, q=70):
    return cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1], cv2.IMREAD_COLOR)


@pytest.mark.parametrize(
    "make",
    [_plan_image, lambda: _jpeg(_plan_image()), lambda: _tilted(_plan_image(), 3.5),
     lambda: cv2.resize(_plan_image(), (450, 300), interpolation=cv2.INTER_AREA),
     lambda: 255 - _plan_image()],  # white lines on black, like a blueprint
    ids=["plan", "jpeg", "tilted", "small", "inverted"],
)
def test_classifier_accepts_plans(make):
    assert classify.plan_likeness(make()) > 0.6


@pytest.mark.parametrize(
    "make",
    [lambda: _photo(600, 900), _flyer, lambda: _jpeg(_flyer()), _facade],
    ids=["photo", "flyer", "flyer-jpeg", "facade"],
)
def test_classifier_rejects_photos(make):
    assert classify.plan_likeness(make()) < 0.3


def test_photo_file_names_rank_below_plans():
    html = """<title>Floor Plans | Memorial Union Building | Michigan Tech</title>
    <h1>Floor Plans</h1>
    <img src="/mub/IMG_2041.jpg" alt="Students at the activities fair">
    <img src="/mub/events-hero.jpg">
    <a href="/mub/mub-basement.pdf"><img src="/mub/mub-basement.jpg" alt="Basement"></a>"""
    info = parse_page("https://www.mtu.edu/mub/", html, ["mtu.edu"])
    score = {a.url.rsplit("/", 1)[-1]: a.score for a in info.assets}
    assert score["IMG_2041.jpg"] < 0.3 and score["events-hero.jpg"] < 0.3  # not downloaded
    assert score["mub-basement.pdf"] >= 0.6
    plan = next(a for a in info.assets if a.url.endswith(".pdf"))
    assert plan.building_guess == "Memorial Union Building"
    assert plan.level == (-1.0, "Basement")


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


def test_crawl_stays_in_the_seed_section_first(settings):
    visited = []
    pages = {
        "/facilities/plans/": '<a href="/news/student-center-gala">Student center gala</a>'
        '<a href="/facilities/plans/union-building">Union building</a>',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        visited.append(request.url.path)
        return httpx.Response(200, html=pages.get(request.url.path, "<p>nothing</p>"))

    http.set_transport(httpx.MockTransport(handler))
    cfg = CrawlConfig(["https://uni.example.edu/facilities/plans/"], [], max_pages=2)
    list(crawl(cfg, fetcher=Fetcher(delay_s=0)))
    assert visited == ["/facilities/plans/", "/facilities/plans/union-building"]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    from floored import config, db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY, kind VARCHAR(64), status VARCHAR(16), progress FLOAT, "
                "message TEXT, payload JSON, result JSON, created_at DATETIME, started_at DATETIME, finished_at DATETIME)")
    con.execute("INSERT INTO jobs (id, kind, status, progress, payload) VALUES ('a', 'crawl', 'done', 1, '{}')")
    con.commit()
    con.close()
    config.override_settings(config.Settings(data_dir=tmp_path / "data", database_url=f"sqlite:///{path}"))
    db.reset_engine()
    try:
        db.init_db()
        cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(jobs)")}
        assert {"detail", "stop_requested"} <= cols
        assert "draft_id" in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(candidates)")}
    finally:
        db.reset_engine()
