"""Score the converter against the fixes people made in the Floored editor.

    cd backend && python -m floored.feedback export ../fixes      # from the app's data
    python bench/feedback.py ../fixes                             # here
    python bench/feedback.py ../fixes --no-rerun -v               # the export alone, fast

The export holds, for every floor someone fixed, the image the converter saw, what
it produced then (``machine.json``) and the floor as people left it
(``final.json``). Here the converter runs again on each image and both its output
then and now are scored against the fixed floor, with the matching used by
``bench/benchmark.py``, inside the part of the image the floor kept (people crop
plans and cut sheets of several floors apart):

* ``F1``, ``mIoU``, ``split``, ``merge``: rooms, as in the benchmark. Splits are
  rooms people had to merge, merges rooms they had to cut.
* ``outline``: IoU of the building outline with the fixed one.
* ``sheet``: for floors cut out of a sheet of several, the share now found as a
  floor of their own (a ``meta["sheets"]`` entry covering it, box IoU >= 0.7).

The fixes themselves say where the converter goes wrong, and are counted by kind.
Floors people moved on the map also give the placement error: how far the plan's
outline sat from where people put it, in metres, then and (when the backend is
importable) with the current ``fit.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from benchmark import rasterize, score  # noqa: E402

# What each kind of fix says about the converter.
MEANING = {
    "merge_rooms": "rooms split that should be one (extra lines, colouring, furniture)",
    "split_room": "rooms run together (missed walls or doorways)",
    "add_room": "rooms missed",
    "delete_room": "rooms that are not rooms (outside, furniture, sheet clutter)",
    "delete_lines": "lines taken for walls that are not",
    "crop": "the building's extent was wrong (titles, frames, legends kept)",
    "split_floor": "several floors on one sheet were not told apart",
    "set_kind": "room kind (stair, restroom, ...) wrong",
    "rename": "room label read wrong",
    "set_level": "floor read wrong",
    "move": "placement on the map was off",
    "autofit": "placement was redone by snapping to the outline",
    "line_up": "placement copied from another floor",
}


def _load(path: Path):
    return json.loads(path.read_text())


def _rooms_in(conv: dict, dx: float = 0.0, dy: float = 0.0) -> list:
    out = []
    for r in conv.get("rooms") or []:
        if r.get("kind") == "outdoor" or len(r.get("polygon") or []) < 3:
            continue
        shift = np.array([dx, dy])
        out.append((np.asarray(r["polygon"], float) + shift, [np.asarray(h, float) + shift for h in r.get("holes") or []]))
    return out


def _mask(ring, shape, dx=0.0, dy=0.0) -> np.ndarray:
    m = np.zeros(shape, np.uint8)
    if ring:
        cv2.fillPoly(m, [np.round(np.asarray(ring, float) + (dx, dy)).astype(np.int32)], 1)
    return m.astype(bool)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def _box_iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _rooms_score(gt: list, pred: list, shape, box) -> dict:
    x0, y0, x1, y1 = box
    g = rasterize(gt, shape)[y0:y1, x0:x1]
    p = rasterize(pred, shape)[y0:y1, x0:x1]
    return score(g, p)


# Placement maths, as in backend/floored/placement/transform.py (kept separate so the
# converter does not depend on the backend).
_R = 6378137.0


def _to_local(lon, lat, lon0, lat0):
    kx = math.radians(1) * _R * math.cos(math.radians(lat0))
    ky = math.radians(1) * _R
    return (lon - lon0) * kx, (lat - lat0) * ky


def _pixel_to_lonlat(p: dict, x, y, w, h):
    u = (x / w - 0.5) * p["width_m"]
    v = (0.5 - y / h) * p["height_m"]
    if p.get("mirror"):
        u = -u
    t = math.radians(p.get("rotation_deg", 0.0))
    ex, ny = u * math.cos(t) + v * math.sin(t), -u * math.sin(t) + v * math.cos(t)
    kx = math.radians(1) * _R * math.cos(math.radians(p["center_lat"]))
    return p["center_lon"] + ex / kx, p["center_lat"] + ny / (math.radians(1) * _R)


def placement_error(a: dict, size_a, b: dict, size_b, ring, offset=(0.0, 0.0)) -> float:
    """Mean distance in metres between where placements ``a`` (an image of ``size_a``)
    and ``b`` put the points of ``ring`` (pixels of b's image; ``offset`` moves them
    into a's image)."""
    if not (a and b and ring):
        return float("nan")
    d = []
    for x, y in ring:
        la = _pixel_to_lonlat(a, x + offset[0], y + offset[1], *size_a)
        lb = _pixel_to_lonlat(b, x, y, *size_b)
        d.append(math.hypot(*_to_local(la[0], la[1], lb[0], lb[1])))
    return float(np.mean(d))


def _refit(final: dict):
    """The placement the current fit.py gives the fixed floor, if the backend imports."""
    fp = (final.get("building") or {}).get("footprint")
    if not fp:
        return None
    try:
        sys.path.insert(0, str(HERE.parents[1] / "backend"))
        from floored.converters.base import ConversionResult
        from floored.placement.fit import fit_to_footprint
    except Exception:  # the backend is optional here
        return None
    fit = fit_to_footprint(ConversionResult.from_dict(final["conversion"]), fp)
    return fit.placement.to_dict() if fit is not None else None


def evaluate(folder: Path, d: Path, rerun: bool = True, ocr: str = "auto") -> dict:
    machine, final = _load(d / "machine.json"), _load(d / "final.json")
    image = d / machine["image"]
    edits = [json.loads(line) for line in (d / "edits.jsonl").read_text().splitlines() if line.strip()]
    mconv = machine["conversion"]
    mw, mh = (machine.get("image_size") or mconv["image_size"])[:2]
    shape = (int(mh), int(mw))
    box = [int(round(v)) for v in (final.get("origin") or {}).get("box") or [0, 0, mw, mh]]
    box = [max(0, box[0]), max(0, box[1]), min(mw, box[2]), min(mh, box[3])]
    dx, dy = box[0], box[1]
    fconv = final["conversion"]
    gt = _rooms_in(fconv, dx, dy)
    row = {"floor": d.name, "ops": Counter(e["op"] for e in edits), "rooms": len(gt),
           "cut_from_sheet": machine.get("auto_split") or any(e["op"] == "split_floor" for e in edits)}
    fixed_outline = _mask(fconv.get("outline"), shape, dx, dy)
    row["then"] = {**_rooms_score(gt, _rooms_in(mconv), shape, box),
                   "outline": _iou(_mask(mconv.get("outline"), shape), fixed_outline)}
    if rerun:
        from floorplan_converter import convert

        res = convert(str(image), ocr=ocr).to_dict()
        outlines = [s["outline"] for s in (res["meta"].get("sheets") or [])] or [res.get("outline")]
        row["now"] = {**_rooms_score(gt, _rooms_in(res), shape, box),
                      "outline": max(_iou(_mask(o, shape), fixed_outline) for o in outlines)}
        if row["cut_from_sheet"]:
            row["now"]["sheet"] = any(_box_iou(s["bbox"], box) >= 0.7 for s in res["meta"].get("sheets") or [])
    if any(e["op"] in ("move", "autofit", "line_up") for e in edits) and final.get("placement"):
        fw, fh = final.get("image_size") or fconv["image_size"]
        ring = fconv.get("outline") or [[0, 0], [fw, 0], [fw, fh], [0, fh]]
        row["placement_then_m"] = placement_error(machine.get("placement"), (mw, mh), final["placement"], (fw, fh),
                                                  ring, (dx, dy))
        refit = _refit(final)
        if refit is not None:
            row["placement_now_m"] = placement_error(refit, (fw, fh), final["placement"], (fw, fh), ring)
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="what `python -m floored.feedback export` wrote")
    ap.add_argument("--no-rerun", action="store_true", help="score the output from then only")
    ap.add_argument("--ocr", default="auto")
    ap.add_argument("--json", help="write per-floor rows to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every floor")
    args = ap.parse_args(argv)

    index = _load(args.folder / "index.json")
    rows = []
    for f in index["floors"]:
        d = args.folder / f["floor_id"]
        if not (d / "machine.json").exists():
            continue
        try:
            rows.append(evaluate(args.folder, d, rerun=not args.no_rerun, ocr=args.ocr))
        except Exception as exc:  # one bad floor must not stop the report
            print(f"{f['floor_id']}: {exc}", file=sys.stderr)
            continue
        if args.verbose:
            r = rows[-1]
            line = f"{r['floor']}  rooms {r['rooms']:3d}  then F1 {r['then']['f1']:.2f}"
            if "now" in r:
                line += f"  now F1 {r['now']['f1']:.2f}"
            line += "  fixes " + ", ".join(f"{k} {v}" for k, v in sorted(r["ops"].items()))
            print(line)
    if not rows:
        print("no floors to score")
        return
    print(f"\n{len(rows)} floors, {sum(r['rooms'] for r in rows)} rooms after fixes\n")
    cols = ["then"] + (["now"] if "now" in rows[0] else [])
    print(f"{'':6s} {'F1':>6s} {'mIoU':>6s} {'split':>6s} {'merge':>6s} {'outline':>8s}")
    n_gt = max(1, sum(r["then"]["n_gt"] for r in rows))
    for c in cols:
        print(f"{c:6s} {np.mean([r[c]['f1'] for r in rows]):6.3f} {np.mean([r[c]['miou'] for r in rows]):6.3f} "
              f"{sum(r[c]['split'] for r in rows) / n_gt:6.3f} {sum(r[c]['merge'] for r in rows) / n_gt:6.3f} "
              f"{np.mean([r[c]['outline'] for r in rows]):8.3f}")
    cut = [r for r in rows if r["cut_from_sheet"] and "now" in r]
    if cut:
        print(f"\nFloors cut out of a sheet of several: {len(cut)}, now found on their own: "
              f"{sum(r['now']['sheet'] for r in cut)}")
    ops = Counter()
    floors_with = Counter()
    for r in rows:
        ops.update(r["ops"])
        floors_with.update(r["ops"].keys())
    print("\nFixes people made (count, floors):")
    for op, n in ops.most_common():
        print(f"  {op:14s} {n:5d} {floors_with[op]:5d}  {MEANING.get(op, '')}")
    moved = [r for r in rows if "placement_then_m" in r and np.isfinite(r["placement_then_m"])]
    if moved:
        print(f"\nPlacement, {len(moved)} floors moved by hand: autofit was off by "
              f"{np.median([r['placement_then_m'] for r in moved]):.1f} m (median)", end="")
        now = [r["placement_now_m"] for r in moved if "placement_now_m" in r]
        print(f", the current fit by {np.median(now):.1f} m" if now else "")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=1, default=lambda o: dict(o) if isinstance(o, Counter) else str(o))


if __name__ == "__main__":
    main()
