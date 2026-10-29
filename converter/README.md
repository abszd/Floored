# floorplan-converter

Turns a floor plan image into rooms: one polygon per room with its label and
type, the building outline, wall centre lines and door openings. It is the
"convert" step of Floored, and a rebuild of the converter from the Locked-In
college project.

```python
from floorplan_converter import convert

result = convert(open("plan.png", "rb").read())
result.outline                  # building footprint ring, original-image pixels
for room in result.rooms:
    room.polygon, room.label, room.kind, room.area_px
result.to_dict()                # plain dict (see "Output format")
```

```bash
pip install -e converter            # or: pip install -e "converter[pdf,dev]"
floorplan-convert plan.png > rooms.json
floorplan-convert plan.pdf --page 1 --debug-dir debug/
```

## What changed from the old converter

The old pipeline inpainted OCR boxes, flood-filled the plan and split it with a
distance-transform watershed. That is where its four known problems came from:

| Problem | Cause | Now |
|---|---|---|
| Text not fully removed | Only OCR boxes were inpainted; anything OCR missed stayed in as "walls". | Text is found twice: by OCR (RapidOCR) and by a shape detector that groups small, thin components into lines of characters. Both are removed from the linework before any wall is looked at, and the OCR text becomes room labels. |
| Small objects never removed | No step for it. | Furniture, fixtures, door swings, arrows, dimension lines, stair treads and symbols are removed by what they are: free-standing small closed outlines and light-line groups (furniture), circular arcs with their leaf (door swings), V-headed and filled-head lines (callout arrows), tick-marked lines outside the building (dimensions). |
| Failed on many drawing styles | Fixed thresholds tuned for one style. | Line weights, wall thickness and door width are measured from each drawing. Double-line and hatched walls are filled solid; inverted (blueprint) and colour-filled plans are detected; scans get a paper model that ignores uneven lighting. |
| Big rooms split into 2-3 | Watershed seeds every bulge of a room. | Rooms are the free space between walls once doorways are closed, so a room is never cut where there is no wall. Doorways are closed from wall geometry (facing wall ends, corners, rays), with door width learned from the plan. A neck split is only made where a room is joined to another through an opening much narrower than both. |

Benchmark on synthetic plans with known rooms, 7 drawing styles × 5 plans
(`python bench/benchmark.py --seeds 0-4 --legacy`):

BENCHMARK_TABLE

F1 counts a room as found when a predicted room overlaps it with IoU ≥ 0.5
(one-to-one matching). Split is the share of true rooms cut into two or more
predicted rooms; merge the share of predictions that swallowed two or more
true rooms. Times are per plan with four plans running in parallel; a single
plan takes about 3-4 s on one core.

## Output format

Everything is in pixels of the **original input image** (before any internal
resizing), x to the right and y down. Polygons use the pixel-corner convention:
the image spans `[0, width] × [0, height]`, and a room drawn over pixels 10-19
spans x = 10.0 to 20.0. Rings are not closed (the first point is not repeated).

`result.to_dict()`:

| Key | Type | |
|---|---|---|
| `image_size` | `[w, h]` | Size of the input image. |
| `outline` | ring or `null` | Exterior of the largest building part. Outdoor areas (decks, courtyards) are left out. This is what to fit to a map footprint. |
| `footprint` | `[{polygon, holes}]` | Every building part (separate wings come out as separate parts). |
| `rooms` | list | `{id, polygon, holes, label, name, number, kind, area_px, confidence, centroid}`. `label` is the OCR text found in the room ("Main Lounge 2218"), split into `name` and `number`. `kind` is one of `room`, `corridor`, `stair`, `elevator`, `restroom`, `outdoor`, `unknown`. `confidence` (0-1) is the share of the room's edge made of drawn walls rather than closed doorways. `centroid` is a point well inside the room, for a label. |
| `walls` | `[[[x, y], ...]]` | Wall centre lines; `wall_thickness_px` holds each one's thickness. |
| `doors` | `[[[x, y], [x, y]]]` | Lines across each closed doorway; `door_widths_px` and `door_kinds` (`door`, or `opening` where a room was split at a narrow neck) run alongside. |
| `texts` | list | Every text read: `{text, quad, confidence, room_id}`. |
| `scale_hint` | float or `null` | Estimated pixels per metre, from the typical door width (0.9 m). Only given when enough doors were seen. |
| `meta` | dict | Converter version, working scale, detected style, wall and door size, timings, and `crop` when the plan was cut out of a larger sheet. |
| `debug_images` | `{name: png bytes}` | With `debug=True`: `text`, `walls`, `rooms`, `polygons`. Left out by `to_json_dict()`. |

`kind: "outdoor"` marks unpainted areas inside a colour-filled plan: terraces,
decks and courtyards drawn within the building's outline. They are rooms of a
sort, but not part of the floor.

### Putting it on a map

`to_geojson(result, ...)` returns a FeatureCollection in lon/lat, from one of:

* `bounds={"south", "north", "west", "east"}`: stretch the outline's bounding box to a box (the old app's resizer);
* `control_points=[((x, y), (lon, lat)), ...]`: two points give a similarity transform (move, rotate, scale), three or more a least-squares affine;
* `transform=[[a, b, c], [d, e, f]]`: an explicit affine.

The Floored backend does its own placement by fitting `outline` to the
building's OpenStreetMap footprint.

## Options

Pass as keyword arguments (`convert(img, debug=True, ocr="none")`) or as a
`ConverterOptions`:

| Option | Default | |
|---|---|---|
| `ocr` | `"auto"` | `auto`, `rapidocr`, `tesseract` or `none`. Without OCR, text is still removed but rooms have no labels. |
| `room_extent` | `"interior"` | `interior`: floor inside the walls. `centerline`: rooms grown to the wall centre lines, tiling the building with no gaps. |
| `auto_crop` | `True` | When the plan covers only part of the sheet, run again on a crop around it for more working resolution. |
| `max_working_size` / `min_working_size` | 2600 / 1400 | Longest side the image is processed at. |
| `pdf_page`, `pdf_dpi` | 0, 200 | PDF input (needs `pypdfium2` or PyMuPDF). |
| `max_gap_doors` | 2.2 | Widest opening closed as a doorway, in door widths. |
| `neck_ratio` | 0.62 | How narrow an unclosed opening must be, relative to the rooms on either side, to split there. |
| `min_room_fraction` | 0.0012 | Smaller regions are merged into a neighbour. |
| `orthogonalize`, `simplify_factor` | True, 0.45 | Polygon clean-up. |
| `debug` | False | Include the debug images. |

## How it works

1. **Load and style** (`loading.py`, `style.py`): decode bytes, paths, arrays,
   PIL images or PDF pages; composite transparency onto white; resize to the
   working size. Ink is found by local contrast with hysteresis, so faint
   scans and coloured fills both work. Inverted (white-on-blue) plans are
   flipped.
2. **Find the plan** (`walls.plan_bbox`): when the plan is a small part of the
   sheet, crop around it and start again at a higher scale.
3. **Text** (`text.py`): OCR words plus shape-detected character runs, removed
   from the linework.
4. **Walls** (`walls.py`, `graph.py`): fill double-line and hatched walls,
   skeletonise into a branch graph and measure every branch's width from ink
   coverage (sub-pixel). Branches are cut where the width steps, so a door
   leaf drawn on from a wall end is its own branch. Line weights are pooled to
   find the annotation weight; light lines are kept only where they behave like
   walls (long straight runs, short stubs between doorways). Door swings,
   arrows, dimension chains and furniture are removed.
5. **Doorways** (`gaps.py`): free wall ends are paired across door-sized gaps,
   corner-to-corner openings are closed, and remaining ends are extended as
   rays to the nearest wall. The door width is learned from the swings and the
   gaps themselves.
6. **Rooms** (`rooms.py`, `fills.py`): the building footprint is the filled
   main wall network; rooms are its free space between walls and closed
   doorways. In colour-filled plans fill boundaries separate rooms too. Small
   pieces are merged, furniture islands absorbed, and rooms joined through a
   narrow neck are split there.
7. **Labels and kinds** (`classify.py`): texts are assigned to the room they
   fall in; names and numbers are separated; kinds come from the label (and
   stair treads).
8. **Polygons** (`geometry.py`): traced, simplified and snapped to the plan's
   dominant axes, then mapped back to original-image pixels.

## Development

```bash
pip install -e "converter[pdf,dev]"
cd converter
pytest -q                                       # about a minute
python bench/benchmark.py --seeds 0-2 -v        # quality per style
python bench/benchmark.py --styles scan --seeds 0-9 --json scan.json
```

`floorplan_converter/synthetic.py` draws random plans in seven styles (solid,
CAD, hatched, gray-filled, colour-filled, blueprint, scanned) with exact room
masks, which is what the benchmark and tests score against. `bench/legacy.py`
is a port of the old converter, used only for the comparison.

Known weak spots: stairs drawn as many separate treads are not always grouped
into one stair room; very small plans with hairline interior walls (some
wayfinding maps) can merge rooms; curved walls are approximated by polygons.
