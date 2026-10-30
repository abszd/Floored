"""Command line: ``floorplan-convert plan.png > rooms.json``.

    floorplan-convert plan.png                      # JSON to stdout
    floorplan-convert plan.pdf --page 2 -o out.json
    floorplan-convert plan.png --debug-dir debug/   # also write the debug images
    floorplan-convert plan.png --geojson --bounds 43.07,43.08,-89.41,-89.40
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, convert, to_geojson


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="floorplan-convert", description="Turn a floor plan image into rooms.")
    ap.add_argument("image", help="PNG, JPEG, TIFF, WebP, BMP or PDF")
    ap.add_argument("-o", "--output", help="write JSON here instead of stdout")
    ap.add_argument("--page", type=int, default=0, help="PDF page (0-based)")
    ap.add_argument("--ocr", default="auto", choices=["auto", "rapidocr", "tesseract", "none"])
    ap.add_argument("--extent", default="interior", choices=["interior", "centerline"],
                    help="room extent: floor inside the walls, or grown to the wall centre lines")
    ap.add_argument("--debug-dir", help="write the intermediate images (PNG) to this folder")
    ap.add_argument("--geojson", action="store_true", help="print GeoJSON instead of the pixel result")
    ap.add_argument("--bounds", help="south,north,west,east in degrees; places the outline in this box")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    res = convert(args.image, pdf_page=args.page, ocr=args.ocr, room_extent=args.extent,
                  debug=bool(args.debug_dir))
    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)
        for name, png in res.debug_images.items():
            with open(os.path.join(args.debug_dir, f"{name}.png"), "wb") as fh:
                fh.write(png)

    if args.geojson:
        bounds = None
        if args.bounds:
            s, n, w, e = (float(v) for v in args.bounds.split(","))
            bounds = {"south": s, "north": n, "west": w, "east": e}
        data = to_geojson(res, bounds=bounds, include=("outline", "rooms", "doors"))
    else:
        data = res.to_json_dict()
    text = json.dumps(data, indent=1)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text + "\n")
    print(f"{len(res.rooms)} rooms, {len(res.doors)} doors in {res.meta['timings_s'].get('total', 0):.1f}s",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
