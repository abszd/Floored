"""Score finding the building on a whole sheet, and telling floors apart.

    python bench/sheets.py                       # every kind, 5 sheets each
    python bench/sheets.py --kinds two_floors --seeds 0-9 -v

Each sheet puts a synthetic plan (see ``floorplan_converter.synthetic``) on a page
with the things that used to be taken for the building:

* ``title_block``: a page frame with a title block, the building name in heavy type
  and a logo.
* ``big_title``: the building name in very large heavy type over the plan, and a
  legend of coloured swatches beside it.
* ``key_plan``: a small filled key plan of the building in a boxed corner.
* ``two_floors``: two floors of one building side by side or stacked, each titled.

Reported per kind:

* ``found``: share of sheets whose outline is the building's (IoU >= 0.9 with the
  footprint of the plan; for two floors, each floor with its own sheet entry).
* ``IoU``: mean outline IoU (per floor for two floors).
* ``floors``: share of two-floor sheets returned as two sheets with the right levels.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

os.environ.setdefault("OMP_NUM_THREADS", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from floorplan_converter import convert, synthetic  # noqa: E402

KINDS = ("title_block", "big_title", "key_plan", "two_floors")
ORDINAL = {0: "GROUND", 1: "FIRST", 2: "SECOND", 3: "THIRD", 4: "FOURTH"}
NAMES = ["MEMORIAL UNION", "BASCOM HALL", "VAN VLECK HALL", "HELEN C. WHITE LIBRARY"]


@dataclass
class Sheet:
    image: np.ndarray  # BGR
    kind: str
    # One per floor drawn: {"bbox": (x0, y0, x1, y1), "footprint": sheet-sized mask, "floor": level}
    plans: list = field(default_factory=list)


def _font(size):
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except TypeError:  # Pillow < 10.1 has one bitmap size
        return ImageFont.load_default()


def _colours(img):
    border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
    bg = tuple(int(v) for v in np.median(border, axis=0))
    ink = (240, 240, 240) if np.mean(bg) < 128 else (20, 20, 20)
    return bg[::-1], ink[::-1]  # RGB


def _place(plans, plan, x, y, shape):
    H, W = shape
    fp = np.zeros((H, W), np.uint8)
    h, w = plan.footprint.shape
    fp[y:y + h, x:x + w] = plan.footprint
    ys, xs = np.nonzero(fp)
    plans.append({"bbox": (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                  "footprint": fp, "floor": plan.meta["floor"]})


def _paste(sheet, plan, x, y):
    sheet.paste(Image.fromarray(cv2.cvtColor(plan.image, cv2.COLOR_BGR2RGB)), (x, y))


def make(seed: int, kind: str, style: str | None = None) -> Sheet:
    """A sheet of ``kind`` (see KINDS) around the synthetic plan of ``seed``."""
    rng = np.random.default_rng(1000 + seed)
    style = style or ["cad", "solid", "hatched", "gray_fill", "color_fill"][seed % 5]
    p = synthetic.generate(seed, style)
    bg, ink = _colours(p.image)
    h, w = p.image.shape[:2]
    plans = []

    if kind == "two_floors":
        s2 = seed + 100
        while True:  # another floor of the same building: same scale, another number
            q = synthetic.generate(s2, style)
            if (q.meta["floor"] != p.meta["floor"] and abs(q.px_per_m / p.px_per_m - 1) < 0.15
                    and abs(q.wall_px - p.wall_px) <= 1):
                break
            s2 += 1
        pair = sorted([p, q], key=lambda r: r.meta["floor"])
        side = rng.random() < 0.6
        if side:
            W = int(sum(r.image.shape[1] for r in pair) * 1.15) + 80
            H = int(max(r.image.shape[0] for r in pair) * 1.35) + 60
        else:
            W = int(max(r.image.shape[1] for r in pair) * 1.25) + 60
            H = int(sum(r.image.shape[0] for r in pair) * 1.25) + 120
        sheet = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(sheet)
        x, y = 40, 40
        fs = int(0.045 * max(h, w))
        for r in pair:
            rh, rw = r.image.shape[:2]
            _paste(sheet, r, x, y)
            _place(plans, r, x, y, (H, W))
            d.text((x + rw // 4, y + rh + 8), f"{ORDINAL[r.meta['floor']]} FLOOR PLAN", fill=ink, font=_font(fs))
            if side:
                x += rw + int(0.1 * rw)
            else:
                y += rh + int(0.15 * rh) + fs
        return Sheet(cv2.cvtColor(np.asarray(sheet), cv2.COLOR_RGB2BGR), kind, plans)

    W, H = int(w * rng.uniform(1.5, 2.0)), int(h * rng.uniform(1.4, 1.8))
    sheet = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(sheet)
    px, py = int(0.05 * W), int(0.12 * H)
    _paste(sheet, p, px, py)
    _place(plans, p, px, py, (H, W))
    lw = max(2, int(0.003 * max(W, H)))
    name = NAMES[seed % len(NAMES)]
    title = f"{ORDINAL[p.meta['floor']]} FLOOR PLAN"

    if kind == "title_block":
        d.rectangle([10, 10, W - 11, H - 11], outline=ink, width=lw)
        tb_x = max(int(0.72 * W), px + w + 20)
        d.rectangle([tb_x, 10, W - 11, H - 11], outline=ink, width=lw)
        for r in (0.12, 0.3, 0.55, 0.7, 0.8, 0.9):
            yy = int(10 + r * (H - 20))
            d.line([tb_x, yy, W - 11, yy], fill=ink, width=lw)
        d.line([(tb_x + W - 11) // 2, int(10 + 0.8 * (H - 20)), (tb_x + W - 11) // 2, H - 11], fill=ink, width=lw)
        tw = W - 11 - tb_x
        fs = min(tw / max(8, len(name)) * 1.6, 0.06 * H)
        d.text((tb_x + 12, int(0.16 * H)), name, fill=ink, font=_font(fs), stroke_width=max(1, int(fs / 14)), stroke_fill=ink)
        d.text((tb_x + 12, int(0.38 * H)), title, fill=ink, font=_font(fs * 0.7))
        d.text((tb_x + 12, int(0.6 * H)), "SCALE 1/8\" = 1'-0\"", fill=ink, font=_font(fs * 0.45))
        d.text((tb_x + 12, int(0.73 * H)), "DATE 03/14/2019", fill=ink, font=_font(fs * 0.45))
        d.text((tb_x + 12, int(0.83 * H)), "A-101", fill=ink, font=_font(fs * 0.9), stroke_width=2, stroke_fill=ink)
        cx, cy = tb_x + int(0.5 * tw), int(0.05 * H) + 10
        d.ellipse([cx - 25, cy - 25, cx + 25, cy + 25], fill=ink)
    elif kind == "big_title":
        fs = 0.075 * H
        d.text((px, int(0.015 * H)), name, fill=ink, font=_font(fs), stroke_width=max(2, int(fs / 10)), stroke_fill=ink)
        d.text((px, py + h + 10), title, fill=ink, font=_font(0.035 * H))
        lx = px + w + 30
        if lx < W - 150:
            for k, col in enumerate([(246, 222, 196), (180, 205, 240), (240, 190, 150), (200, 240, 200)]):
                yy = py + 40 * k
                d.rectangle([lx, yy, lx + 30, yy + 24], fill=col, outline=ink, width=2)
                d.text((lx + 40, yy), ["Offices", "Restrooms", "Stairs", "Lounge"][k], fill=ink, font=_font(20))
    elif kind == "key_plan":
        d.text((px, py + h + 10), title, fill=ink, font=_font(0.035 * H))
        small = cv2.resize(p.footprint, None, fx=0.2, fy=0.2, interpolation=cv2.INTER_NEAREST)
        cnts, _ = cv2.findContours(small, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        kx, ky = W - small.shape[1] - 60, H - small.shape[0] - 80
        d.rectangle([kx - 20, ky - 40, W - 30, H - 30], outline=ink, width=lw)
        d.text((kx, ky - 34), "KEY PLAN", fill=ink, font=_font(22))
        for c in cnts:
            pts = [(int(x + kx), int(y + ky)) for x, y in c.reshape(-1, 2)]
            d.polygon(pts, outline=ink, width=lw + 1)
            d.polygon(pts[: max(3, len(pts) // 2)], fill=ink)
    else:
        raise ValueError(f"unknown sheet kind {kind!r}")
    return Sheet(cv2.cvtColor(np.asarray(sheet), cv2.COLOR_RGB2BGR), kind, plans)


def ring_iou(ring, footprint: np.ndarray) -> float:
    """IoU of a polygon (image pixels) with a mask."""
    m = np.zeros(footprint.shape, np.uint8)
    if ring:
        cv2.fillPoly(m, [np.round(np.asarray(ring)).astype(np.int32)], 1)
    t = footprint > 0
    union = (m.astype(bool) | t).sum()
    return float((m.astype(bool) & t).sum() / union) if union else 0.0


def run_one(job):
    kind, seed, ocr = job
    sh = make(seed, kind)
    t = time.perf_counter()
    res = convert(sh.image, ocr=ocr)
    dt = time.perf_counter() - t
    sheets = res.meta.get("sheets") or []
    if len(sh.plans) == 1:
        ious = [ring_iou(res.outline, sh.plans[0]["footprint"])]
        floors_ok = None
    else:
        # Each floor against the sheet entry that covers it best.
        ious = [max([ring_iou(s["outline"], p["footprint"]) for s in sheets] or [0.0]) for p in sh.plans]
        floors_ok = (len(sheets) == len(sh.plans)
                     and [s["level"] for s in sheets] == [p["floor"] for p in sh.plans])
    return {"kind": kind, "seed": seed, "time_s": dt, "iou": ious, "sheets": len(sheets),
            "levels": [s["level"] for s in sheets], "truth": [p["floor"] for p in sh.plans], "floors_ok": floors_ok}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--seeds", default="0-4")
    ap.add_argument("--ocr", default="auto")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--save", help="write each sheet image to this folder")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every sheet")
    args = ap.parse_args(argv)
    from benchmark import _seeds

    jobs = [(k, s, args.ocr) for k in args.kinds.split(",") for s in _seeds(args.seeds)]
    if args.save:
        os.makedirs(args.save, exist_ok=True)
        for k, s, _ in jobs:
            cv2.imwrite(os.path.join(args.save, f"{k}_{s}.png"), make(s, k).image)
    if args.workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(args.workers) as ex:
            rows = list(ex.map(run_one, jobs))
    else:
        rows = [run_one(j) for j in jobs]
    if args.verbose:
        for r in rows:
            print(f"{r['kind']:12s} seed {r['seed']:3d}  IoU {' '.join(f'{v:.2f}' for v in r['iou'])}  "
                  f"sheets {r['sheets']} levels {r['levels']} truth {r['truth']}  {r['time_s']:.1f}s")
    print(f"{'kind':12s} {'sheets':>6s} {'found':>6s} {'IoU':>6s} {'floors':>7s} {'time':>6s}")
    for k in list(dict.fromkeys(r["kind"] for r in rows)) + ["all"]:
        sel = [r for r in rows if k == "all" or r["kind"] == k]
        ious = [v for r in sel for v in r["iou"]]
        multi = [r["floors_ok"] for r in sel if r["floors_ok"] is not None]
        print(f"{k:12s} {len(sel):6d} {np.mean([min(r['iou']) >= 0.9 for r in sel]):6.2f} {np.mean(ious):6.3f} "
              f"{(f'{np.mean(multi):7.2f}' if multi else '      -')} {np.mean([r['time_s'] for r in sel]):5.1f}s")


if __name__ == "__main__":
    main()
