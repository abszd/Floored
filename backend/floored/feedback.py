"""Export what the machine produced next to what people made of it.

    python -m floored.feedback export DIR [--all]

writes, for every floor someone has fixed (every converted floor with ``--all``)::

    DIR/index.json                 one entry per floor, plus the building-match fixes
    DIR/<floor_id>/image.<ext>     the image the converter saw
    DIR/<floor_id>/final.png       the floor's image now, when it was cropped or split off
    DIR/<floor_id>/machine.json    the converter's output and the automatic placement
    DIR/<floor_id>/final.json      the floor as it is now, after people's fixes
    DIR/<floor_id>/edits.jsonl     each fix, oldest first

Coordinates in machine.json are pixels of image.<ext>; in final.json they are
pixels of the floor's current image. ``final.json["origin"]["box"]`` is where
that image sits in image.<ext> (``[0, 0, w, h]`` when it was never cropped),
so the two can be compared. Placements are ``placement.transform.Placement``
dicts, and ``final.json["building"]["footprint"]`` is the outline they fit to.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import storage
from .db import init_db, session_scope
from .models import Correction, Floor


def _ext(path: str) -> str:
    suffix = Path(path).suffix
    return suffix if suffix and suffix != ".img" else ".png"


def _dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, default=str))


def export(out: Path, *, everything: bool = False) -> dict[str, Any]:
    init_db()
    out.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {"floors": [], "matches": []}
    with session_scope() as s:
        fixes: dict[str | None, list[Correction]] = {}
        for c in s.scalars(select(Correction).order_by(Correction.at)):
            fixes.setdefault(c.floor_id, []).append(c)
            # A floor split off another starts with that split.
            for new in (c.params or {}).get("new_floors") or []:
                fixes.setdefault(new, []).append(c)
        floors = {f.id: f for f in s.scalars(select(Floor))}
        for f in floors.values():
            edits = fixes.get(f.id, [])
            if f.conversion is None or not (edits or everything):
                continue
            origin = (f.conversion.get("meta") or {}).get("origin") or {
                "floor": f.id,
                "box": [0, 0, f.image_width, f.image_height],
            }
            # A floor split off another has no machine output of its own: it's the sheet's.
            source = f if f.machine else floors.get(origin["floor"])
            machine = source.machine if source is not None else None
            if not machine:
                continue
            d = out / f.id
            d.mkdir(exist_ok=True)
            image = machine.get("image_path") or f.image_path
            try:
                shutil.copyfile(storage.media_path(image), d / f"image{_ext(image)}")
                if f.image_path != image:
                    shutil.copyfile(storage.media_path(f.image_path), d / "final.png")
            except OSError as exc:
                print(f"skipping {f.id}: {exc}", file=sys.stderr)
                continue
            _dump(d / "machine.json", {**machine, "image": f"image{_ext(image)}", "floor": source.id})
            b = f.building
            _dump(
                d / "final.json",
                {
                    "floor_id": f.id,
                    "image": "final.png" if f.image_path != image else f"image{_ext(image)}",
                    "image_size": [f.image_width, f.image_height],
                    "origin": origin,
                    "conversion": f.conversion,
                    "placement": f.placement,
                    "fit_score": f.fit_score,
                    "status": f.status,
                    "level": f.level,
                    "name": f.name,
                    "source_url": f.source_url,
                    "building": {"id": b.id, "name": b.name, "address": b.address, "footprint": b.footprint},
                },
            )
            with (d / "edits.jsonl").open("w") as fh:
                for c in edits:
                    fh.write(json.dumps({"op": c.op, "at": c.at, "params": c.params}, default=str) + "\n")
            index["floors"].append(
                {
                    "floor_id": f.id,
                    "building": b.name,
                    "level": f.level,
                    "name": f.name,
                    "machine_floor": source.id,
                    "auto_split": bool(machine.get("auto_split")),
                    "edits": len(edits),
                    "ops": sorted({c.op for c in edits}),
                    "status": f.status,
                }
            )
        index["matches"] = [{"at": c.at, "building_id": c.building_id, **c.params} for c in fixes.get(None, [])]
    _dump(out / "index.json", index)
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m floored.feedback", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write the machine output and people's fixes to a folder")
    ex.add_argument("dir", type=Path)
    ex.add_argument("--all", action="store_true", help="every converted floor, not only the fixed ones")
    args = parser.parse_args(argv)
    index = export(args.dir, everything=args.all)
    print(f"Exported {len(index['floors'])} floors and {len(index['matches'])} building matches to {args.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
