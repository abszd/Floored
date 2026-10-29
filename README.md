# Floored

Find building floor plans on the web, turn them into rooms, and put them on an
interactive map in the right building.

Floored grew out of the *Locked-In* college project (`abszd/locked-in-backend`),
which converted an uploaded floor plan into room polygons and stretched them
over a hand-drawn bounding box. Floored keeps that idea and builds the rest of
the product around it:

- **Discover**: crawl a website (a campus facilities site, say) for floor plan
  images and PDFs, read the page around each one for the building name, street
  address and floor level, and match it to a real building.
- **Convert**: find the rooms in each plan. The converter is pluggable; the
  rebuilt one lives in `converter/` and a port of the original algorithm is
  kept as a fallback.
- **Place**: fit the plan onto the building's real outline from OpenStreetMap
  (rotation, scale and position are solved automatically), with a drag-to-adjust
  editor for the cases that need a human.
- **Explore**: a map of every building, with a floor switcher, clickable rooms,
  search across building and room names, and quick fixes for room labels and types.

## How it fits together

```
 website ──crawl──▶ candidate plan ──match──▶ building (OSM outline)
                         │                          │
                         └──────convert──▶ rooms (pixels)
                                                    │
                                   fit to outline ──▶ placement ──▶ rooms (GeoJSON) ──▶ map
```

| Piece | Where | What it does |
|---|---|---|
| Crawler | `backend/floored/scraper/` | Best-first crawl that follows links mentioning plans, buildings or facilities first; respects `robots.txt` and rate limits; renders PDF pages; scores each image with a plan-likeness heuristic (mostly white, colourless, long axis-aligned lines). |
| Matching | `backend/floored/geo/matching.py` | Cleans names ("Engr. Hall – 3rd Floor Plan" → "engineering hall"), matches known buildings and aliases (including acronyms like "MU"), otherwise geocodes the address or name + location hint through Nominatim and scores the result. Confident matches are processed automatically; the rest wait in the review queue. |
| Converter contract | `backend/floored/converters/base.py` | `image bytes → {image_size, outline, rooms[{id, polygon, holes, label, kind, area_px}], walls, doors, scale_hint}` in original-image pixels. `FLOORED_CONVERTER=auto` uses `converter/` when installed, else the legacy port. |
| Placement | `backend/floored/placement/` | A placement is *centre, compass bearing, width and height in metres*: the old two-corner "GeoJSON resizer" plus rotation. `fit.py` tries the four axis alignments between the plan outline and the OSM footprint and polishes each with Nelder–Mead to maximise overlap (IoU), with a small penalty on non-uniform scale. Symmetric buildings are flagged for review because they fit equally well turned 180°. |
| API | `backend/floored/api/app.py` | FastAPI; SQLite by default; background jobs for crawling and conversion. |
| UI | `frontend/` | React + MapLibre GL (no map API key needed). |

## Running it

Requires Python 3.10+ and Node 18+.

```bash
# API
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../converter        # optional: the rebuilt converter
floored serve                      # http://127.0.0.1:8000

# UI (in another terminal)
cd frontend
npm install
npm run dev                        # http://localhost:5173, proxies /api to :8000
```

For a single-process deployment, run `npm run build` in `frontend/`; `floored serve`
then serves the built UI as well.

Other commands:

```bash
floored crawl https://facilities.example.edu/floorplans --hint "Madison, WI"
floored convert plan.png --converter legacy > rooms.json
cd backend && pytest
```

### Configuration

Environment variables (all optional):

| Variable | Default | |
|---|---|---|
| `FLOORED_DATA_DIR` | `./data` | Database, images and HTTP cache. |
| `FLOORED_DATABASE_URL` | SQLite in the data dir | Any SQLAlchemy URL. |
| `FLOORED_CONVERTER` | `auto` | `auto`, `floorplan_converter` or `legacy`. |
| `FLOORED_USER_AGENT` | `FlooredBot/0.1 (+repo url)` | Nominatim's usage policy requires a real contact; set this to yours. |
| `FLOORED_NOMINATIM_URL` / `FLOORED_OVERPASS_URL` | public OSM servers | Point these at your own instances for bulk use. |
| `FLOORED_AUTO_APPROVE` | `0.85` | Match confidence above which a found plan is processed without review. |
| `FLOORED_CRAWL_DELAY` | `1.0` | Seconds between requests to the same site. |
| `FLOORED_WORKERS` | `2` | Background job threads. |
| `VITE_MAP_STYLE` (frontend) | OpenFreeMap Positron | Any MapLibre style URL. |

## Using it

1. **Discover → Add**: paste a page that links to floor plans and say where the
   buildings are ("Madison, WI"). The crawl runs in the background.
2. Plans matched with high confidence go straight onto the map. The rest appear
   under **To review**: check the building and level, pick the right building if
   needed, and approve.
3. On the **Map**, click a building, switch floors with the elevator-style
   buttons, and click rooms to see or fix their label and type.
4. Floors marked **Check placement** open the placement editor: snap to the
   building outline, rotate by 90° if the plan is turned around, or drag the
   handles. **Line up with another floor** copies a good placement to the rest
   of the building.

Please be considerate when crawling: keep the page cap modest, keep the default
delay, and only crawl sites whose plans you are allowed to use.
