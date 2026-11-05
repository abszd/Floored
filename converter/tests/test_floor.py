"""Reading which floor a plan shows: wording, lone marks, room numbers, PDFs."""

import time

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from floorplan_converter import convert, parse_floor, read_floor, synthetic
from floorplan_converter.cli import main as cli_main
from floorplan_converter.floor import floor_from_texts
from floorplan_converter.text import TextItem, get_backend

HAS_OCR = get_backend("auto").name != "none"
needs_ocr = pytest.mark.skipif(not HAS_OCR, reason="no OCR engine installed")


@pytest.mark.parametrize("text, level, label", [
    ("Second Floor Plan", 2.0, "Second Floor"),
    ("SECONDFLOORPLAN", 2.0, "Second Floor"),
    ("MEMORIAL UNION - 3RD FLOOR", 3.0, "3rd Floor"),
    ("FLOOR PLAN - LEVEL 02", 2.0, "Level 02"),
    ("Level O1", 1.0, "Level O1"),
    ("Level -1", -1.0, "Level -1"),
    ("Level - 1", 1.0, "Level - 1"),
    ("Level B2", -2.0, "Level B2"),
    ("Lower Level", -1.0, "Lower Level"),
    ("Lower Level 2", -2.0, "Lower Level 2"),
    ("Basement", -1.0, "Basement"),
    ("Sub-Basement", -2.0, "Sub-Basement"),
    ("Ground Floor", 0.0, "Ground Floor"),
    ("Main Level", 1.0, "Main Level"),
    ("Mezzanine", 1.5, "Mezzanine"),
    ("2nd Floor Mezzanine", 2.5, "2nd Floor Mezzanine"),
    ("Roof Plan", None, "Roof Plan"),
    ("Floor Two", 2.0, "Floor Two"),
    ("12th floor", 12.0, "12th Floor"),
    ("3F", 3.0, "3F"),
])
def test_parse_floor(text, level, label):
    assert parse_floor(text) == (level, label)


@pytest.mark.parametrize("text", ["Floor Plan", "Floor drain", "Great Hall", "Roof Deck", "Storage 2",
                                  "Level 340", "Leveling 2", "Classroom 2210"])
def test_parse_floor_ignores(text):
    assert parse_floor(text) is None


# ---------------------------------------------------------------------------
# Deciding from all the text on a page (no OCR: text items made up here)
# ---------------------------------------------------------------------------


def T(text, x, y, h=20.0, conf=0.9):
    w = 0.6 * h * max(1, len(text))
    return TextItem(np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64), text, conf)


def labels(numbers, x0=100, y0=100):
    """Room labels spread over a plan: a name and a number per room."""
    out = []
    for k, num in enumerate(numbers):
        x, y = x0 + 220 * (k % 4), y0 + 120 * (k // 4)
        out += [T("Office", x, y), T(f"({num})", x, y + 24)]
    return out


def test_room_numbers_alone():
    f = floor_from_texts(labels([2218, 2220, 2206, 2324, 2190, 2214]))
    assert f["level"] == 2.0 and f["source"] == "room_numbers" and f["label"] is None
    assert 0.5 <= f["confidence"] <= 0.8


def test_big_mark_beside_plan_with_room_numbers():
    f = floor_from_texts(labels([4195, 4214, 4187, 4310, 4304]) + [T("4", 1200, 150, h=90, conf=0.4)])
    assert f["level"] == 4.0 and f["source"] == "mark+room_numbers" and f["confidence"] >= 0.8


def test_mark_among_the_labels_is_not_a_floor():
    items = [T("Office", x, y) for x in (100, 500, 900) for y in (100, 400, 700)]
    assert floor_from_texts(items + [T("10", 480, 380, h=70)]) is None


def test_grid_bubbles_are_not_floors():
    bubbles = [T(s, 100 + 200 * k, 20, h=60) for k, s in enumerate("123456")]
    bubbles += [T(s, 20, 150 + 200 * k, h=60) for k, s in enumerate("ABCD")]
    words = [T("Office", 300 + 150 * k, 400) for k in range(5)]
    assert floor_from_texts(bubbles + words) is None


def test_title_with_building_name():
    items = labels([2218, 2220, 2206, 2324]) + [T("MEMORIAL UNION - SECOND FLOOR PLAN", 100, 900, h=45)]
    f = floor_from_texts(items)
    assert f["level"] == 2.0 and f["label"] == "Second Floor"
    assert f["source"] == "title+room_numbers" and f["confidence"] >= 0.85
    assert f["building"] == "MEMORIAL UNION"
    assert f["text"] == "MEMORIAL UNION - SECOND FLOOR PLAN"


def test_title_split_over_two_lines():
    f = floor_from_texts(labels(["B104", "B105", "B152"]) + [T("Lower", 1200, 300, h=60), T("Level", 1200, 370, h=60)])
    assert f["level"] == -1.0 and f["label"] == "Lower Level" and f["source"] == "title+room_numbers"


def test_letters_on_room_numbers():
    # B on its own is the basement...
    assert floor_from_texts(labels(["B104", "B105", "B152", "B147A"]))["level"] == -1.0
    # ...but A, B and C together are wings, and the digits give the floor.
    assert floor_from_texts(labels(["A210", "B210", "C305", "A215", "B220"]))["level"] == 2.0


def test_numbers_that_are_not_rooms():
    items = [T("Updated May 2019", 100, 900), T("SCALE 1:100", 100, 930), T("000", 300, 300),
             T("000", 500, 300), T("12'-6\"", 100, 500), T("1125 sq ft", 600, 600)]
    assert floor_from_texts(items + labels([310, 312, 315]))["level"] == 3.0
    assert floor_from_texts(items) is None


def test_room_numbers_outvote_a_lone_mark_but_not_a_title():
    rooms = labels([2218, 2220, 2206, 2324, 2190, 2214, 2310, 2312])
    f = floor_from_texts(rooms + [T("5", 1200, 150, h=90, conf=0.4)])
    assert f["level"] == 2.0 and f["source"] == "room_numbers"
    f = floor_from_texts(rooms + [T("THIRD FLOOR PLAN", 100, 900, h=45)])
    assert f["level"] == 3.0 and f["source"] == "title" and f["confidence"] < 0.7


def test_key_plan_listing_every_floor():
    keys = [T(s, 1200, 600 + 30 * k) for k, s in enumerate(["First Floor", "Second Floor", "Third Floor"])]
    f = floor_from_texts(keys)
    assert f["confidence"] < 0.5  # it lists them all; which one this is, it cannot say
    f = floor_from_texts(keys + [T("SECOND FLOOR", 100, 900, h=50)])
    assert f["level"] == 2.0 and f["confidence"] >= 0.8


def test_nothing_to_read():
    assert floor_from_texts([]) is None
    assert floor_from_texts([T("Office", 100, 100), T("Kitchen", 300, 100)]) is None


# ---------------------------------------------------------------------------
# Images, PDFs and the converter
# ---------------------------------------------------------------------------


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no scalable default font
        pytest.skip("needs Pillow 10.1+")


def _sheet(plan_img, title=None, mark=None, building=None):
    """Put a plan on a big sheet with a title under it or a floor number beside it."""
    h, w = plan_img.shape[:2]
    H, W = 3 * h, 3 * w
    sheet = Image.new("RGB", (W, H), "white")
    sheet.paste(Image.fromarray(cv2.cvtColor(plan_img, cv2.COLOR_BGR2RGB)), (W // 10, H // 6))
    d = ImageDraw.Draw(sheet)
    if building:
        d.text((W // 10, int(H * 0.78)), building, fill=(30, 30, 30), font=_font(int(h * 0.07)))
    if title:
        d.text((W // 10, int(H * 0.86)), title, fill=(30, 30, 30), font=_font(int(h * 0.06)))
    if mark:
        d.text((int(W * 0.75), H // 5), mark, fill=(170, 170, 170), font=_font(int(h * 0.3)))
    return cv2.cvtColor(np.asarray(sheet), cv2.COLOR_RGB2BGR)


@needs_ocr
@pytest.mark.parametrize("style, seed", [("cad", 0), ("blueprint", 1), ("gray_fill", 4), ("color_fill", 0)])
def test_read_floor_on_synthetic_plans(style, seed):
    plan = synthetic.generate(seed, style)
    t = time.perf_counter()
    f = read_floor(plan.image)
    assert time.perf_counter() - t < 5.0
    assert f is not None and f["level"] == plan.meta["floor"]


@needs_ocr
def test_title_and_building_on_a_sheet():
    plan = synthetic.generate(4, "gray_fill")  # room numbers 2xx
    img = _sheet(plan.image, title="SECOND FLOOR PLAN", building="Bascom Hall")
    f = read_floor(cv2.imencode(".png", img)[1].tobytes())
    assert f["level"] == 2.0 and f["label"] == "Second Floor" and f["building"] == "Bascom Hall"


@needs_ocr
def test_convert_reads_a_mark_outside_its_crop():
    plan = synthetic.generate(0, "gray_fill")  # room numbers 3xx
    img = _sheet(plan.image, mark=str(plan.meta["floor"]))
    res = convert(img)
    assert res.meta.get("crop") is not None  # the plan was cut out of the sheet...
    f = res.meta["floor"]
    assert f["level"] == plan.meta["floor"] and f["source"] == "mark+room_numbers"  # ...the mark still read
    assert convert(plan.image, read_floor=False).meta.get("floor") is None


def _pdf(lines):
    """A one-page PDF with real text: [(text, size, x, y)] in points."""
    content = "".join(f"BT /F1 {size} Tf {x} {y} Td ({s}) Tj ET\n" for s, size, x, y in lines).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] /Contents 4 0 R "
             b"/Resources << /Font << /F1 5 0 R >> >> >>"),
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for k, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % k + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_pdf_text_layer_is_read_without_ocr(tmp_path):
    pytest.importorskip("pypdfium2")
    pdf = _pdf([("Van Vleck Hall", 30, 60, 80), ("Third Floor", 24, 60, 50),
                ("Office 301", 8, 200, 400), ("Lab 305", 8, 300, 400), ("Lounge 312", 8, 400, 300)])
    f = read_floor(pdf, ocr="none")
    assert f["level"] == 3.0 and f["label"] == "Third Floor" and f["building"] == "Van Vleck Hall"
    path = tmp_path / "plan.pdf"
    path.write_bytes(pdf)
    assert read_floor(str(path), ocr="none")["level"] == 3.0


def test_cli_floor(tmp_path, capsys):
    pytest.importorskip("pypdfium2")
    path = tmp_path / "plan.pdf"
    path.write_bytes(_pdf([("Lower Level", 30, 60, 60), ("B104", 8, 200, 400), ("B110", 8, 300, 400)]))
    assert cli_main([str(path), "--floor", "--ocr", "none"]) == 0
    assert '"level": -1.0' in capsys.readouterr().out
