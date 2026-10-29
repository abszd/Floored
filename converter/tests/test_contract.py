"""The output contract the Floored backend relies on, and the input formats."""

import io
import json

import cv2
import numpy as np
import pytest

from floorplan_converter import ROOM_KINDS, convert, synthetic
from floorplan_converter.cli import main as cli_main


@pytest.fixture(scope="module")
def plan():
    return synthetic.generate(1, "solid")


@pytest.fixture(scope="module")
def result(plan):
    return convert(cv2.imencode(".png", plan.image)[1].tobytes(), debug=True)


def _is_ring(r):
    return isinstance(r, list) and len(r) >= 3 and all(len(p) == 2 and all(isinstance(v, float) for v in p) for p in r)


def test_to_dict_shape(plan, result):
    d = result.to_dict()
    h, w = plan.image.shape[:2]
    assert d["image_size"] == [w, h]
    assert _is_ring(d["outline"])
    assert d["rooms"]
    for r in d["rooms"]:
        assert {"id", "polygon", "holes", "label", "kind", "area_px", "confidence"} <= set(r)
        assert _is_ring(r["polygon"]) and all(_is_ring(hh) for hh in r["holes"])
        assert r["kind"] in ROOM_KINDS
        assert r["area_px"] > 0
        xs = [p[0] for p in r["polygon"]]
        ys = [p[1] for p in r["polygon"]]
        assert 0 <= min(xs) and max(xs) <= w and 0 <= min(ys) and max(ys) <= h
    # Walls and doors are plain polylines, with their widths and kinds alongside.
    assert d["walls"] and all(len(line) >= 2 and len(line[0]) == 2 for line in d["walls"])
    assert len(d["wall_thickness_px"]) == len(d["walls"])
    assert d["doors"] and all(len(line) == 2 for line in d["doors"])
    assert len(d["door_kinds"]) == len(d["doors"]) == len(d["door_widths_px"])
    assert d["scale_hint"] is None or d["scale_hint"] > 0
    assert set(d["debug_images"]) >= {"walls", "rooms"}
    assert all(png[:8] == b"\x89PNG\r\n\x1a\n" for png in d["debug_images"].values())
    json.dumps(result.to_json_dict())


def test_scale_hint_close_to_truth(plan, result):
    assert result.scale_hint is not None
    assert 0.6 * plan.px_per_m <= result.scale_hint <= 1.6 * plan.px_per_m


def test_input_formats_agree(plan, result, tmp_path):
    path = tmp_path / "plan.png"
    cv2.imwrite(str(path), plan.image)
    gray = cv2.cvtColor(plan.image, cv2.COLOR_BGR2GRAY)
    for src in (str(path), gray):
        r = convert(src, ocr="none")
        assert r.image_size == result.image_size
        assert abs(len(r.rooms) - len(result.rooms)) <= 3


def test_transparent_png_is_composited_on_white(plan):
    bgra = np.zeros(plan.image.shape[:2] + (4,), np.uint8)
    ink = cv2.cvtColor(plan.image, cv2.COLOR_BGR2GRAY) < 128
    bgra[ink, 3] = 255  # black ink, everything else fully transparent black
    r = convert(cv2.imencode(".png", bgra)[1].tobytes(), ocr="none")
    assert len(r.rooms) >= 10


def test_plan_on_a_large_sheet_is_cropped_and_mapped_back(plan, result):
    h, w = plan.image.shape[:2]
    dx, dy = 1500, 1100
    sheet = np.full((h + 2 * dy, w + 2 * dx, 3), 255, np.uint8)
    sheet[dy:dy + h, dx:dx + w] = plan.image
    r = convert(sheet)
    assert r.meta.get("crop") is not None
    assert r.image_size == [sheet.shape[1], sheet.shape[0]]
    a = np.array(result.outline)
    b = np.array(r.outline) - [dx, dy]
    assert np.allclose(a.min(axis=0), b.min(axis=0), atol=6)
    assert np.allclose(a.max(axis=0), b.max(axis=0), atol=6)


def test_blank_page_gives_no_rooms():
    r = convert(np.full((600, 800, 3), 255, np.uint8))
    assert r.rooms == [] and r.outline is None


def test_pdf_input(plan):
    pytest.importorskip("pypdfium2")
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(plan.image[..., ::-1]).save(buf, format="PDF", resolution=100)
    r = convert(buf.getvalue(), pdf_dpi=100, ocr="none")
    h, w = plan.image.shape[:2]
    assert abs(r.image_size[0] - w) <= 2 and abs(r.image_size[1] - h) <= 2
    assert len(r.rooms) >= 10


def test_cli_writes_json(plan, tmp_path):
    src = tmp_path / "plan.png"
    out = tmp_path / "rooms.json"
    cv2.imwrite(str(src), plan.image)
    assert cli_main([str(src), "-o", str(out), "--ocr", "none", "--debug-dir", str(tmp_path / "dbg")]) == 0
    d = json.loads(out.read_text())
    assert d["rooms"] and d["outline"]
    assert (tmp_path / "dbg" / "rooms.png").exists()
