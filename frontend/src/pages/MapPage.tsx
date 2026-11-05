import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import mapboxgl, { type Map as MBMap } from "mapbox-gl";
import type { Feature, FeatureCollection, Geometry, Position } from "geojson";
import { Building2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Plus, Radar, X } from "lucide-react";
import clsx from "clsx";
import { api } from "../lib/api";
import { levelShort } from "../lib/placement";
import { isDark } from "../lib/theme";
import type { Building } from "../lib/types";
import MapView, { FLOOR_HEIGHT_M, type Basemap, type Hover, type RoomClick } from "../components/MapView";
import { HoverTip, MapControls, TokenNotice } from "../components/MapChrome";
import BuildingPanel from "../components/BuildingPanel";
import PlacementEditor from "../components/PlacementEditor";
import RoomCard, { MergeRoomsCard } from "../components/RoomCard";
import SearchBox from "../components/SearchBox";
import SplitFloorDialog from "../components/SplitFloorDialog";
import { DrawHint, FloorToolbar, useDrawLayer, type Tool } from "../components/FloorTools";
import AddBuildingDialog from "../components/AddBuildingDialog";
import { Empty, Spinner } from "../components/ui";
import { useToast } from "../components/Toast";

function eachPosition(g: Geometry, fn: (p: Position) => void) {
  if (g.type === "Point") fn(g.coordinates);
  else if (g.type === "Polygon") g.coordinates.forEach((r) => r.forEach(fn));
  else if (g.type === "MultiPolygon") g.coordinates.forEach((p) => p.forEach((r) => r.forEach(fn)));
}

function boundsOf(geoms: Geometry[]): mapboxgl.LngLatBounds | null {
  let b: mapboxgl.LngLatBounds | null = null;
  for (const g of geoms)
    eachPosition(g, (p) => {
      const ll: [number, number] = [p[0], p[1]];
      b = b ? b.extend(ll) : new mapboxgl.LngLatBounds(ll, ll);
    });
  return b;
}

function stored<T extends string>(key: string, fallback: T): T {
  try {
    return (localStorage.getItem(key) as T) || fallback;
  } catch {
    return fallback;
  }
}

function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}

const isSmall = () => window.matchMedia("(max-width: 767px)").matches;

export default function MapPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const buildingId = params.get("b");
  const levelParam = params.get("l");
  const editFloorId = params.get("edit");
  const [map, setMap] = useState<MBMap | null>(null);
  // Selected rooms; several when shift-clicking rooms to merge them. The last one is "the" room.
  const [selection, setSelection] = useState<RoomClick[]>([]);
  const room = selection.length ? selection[selection.length - 1] : null;
  // Walls and doors picked with an area selection, by map id ("<floor>:wall3").
  const [wallSel, setWallSel] = useState<string[]>([]);
  const setRoom = useCallback((r: RoomClick | null) => {
    setSelection(r ? [r] : []);
    setWallSel([]);
  }, []);
  const uidOf = (r: RoomClick) => String(r.feature.id ?? r.feature.properties.uid);
  const floorOf = (r: RoomClick) => String(r.feature.properties.floor_id);
  const addRooms = useCallback((clicks: RoomClick[], toggle: boolean) => {
    setSelection((sel) => {
      // Rooms can only be merged within one floor plan; a room from another starts over.
      let next = clicks.length && sel.length && floorOf(sel[0]) !== floorOf(clicks[0]) ? [] : [...sel];
      for (const c of clicks) {
        const i = next.findIndex((r) => uidOf(r) === uidOf(c));
        if (i >= 0) {
          if (toggle) next = next.filter((_, j) => j !== i);
        } else if (!next.length || floorOf(next[0]) === floorOf(c)) next.push(c);
      }
      return next;
    });
  }, []);
  const addWalls = useCallback((ids: string[]) => {
    setWallSel((sel) => {
      const floor = (id: string) => id.split(":")[0];
      const base = sel.length && ids.length && floor(sel[0]) !== floor(ids[0]) ? [] : sel;
      return [...new Set([...base, ...ids])];
    });
  }, []);
  // Editing tools on the map: area select, draw a room, split a room.
  const [tool, setTool] = useState<Tool | null>(null);
  const [drawPts, setDrawPts] = useState<[number, number][]>([]);
  const [splitTarget, setSplitTarget] = useState<{ floorId: string; roomId: string } | null>(null);
  const [cropFloorId, setCropFloorId] = useState<string | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [adding, setAdding] = useState(false);
  // Clicking the map: to add a new building, or to add/remove a part of the open building's outline.
  const [pickMode, setPickMode] = useState<null | "new" | "add" | "remove">(null);
  const picking = pickMode !== null;
  const setPicking = (v: boolean) => setPickMode(v ? "new" : null);
  const [pendingRoom, setPendingRoom] = useState<{ floorId: string; roomId: string; stay?: boolean } | null>(null);
  const [basemap, setBasemap] = useState<Basemap>(() => stored("floored.basemap", isDark() ? "dark" : "streets"));
  const [threeD, setThreeD] = useState(() => stored<string>("floored.3d", "0") === "1");

  const update = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) v === null ? next.delete(k) : next.set(k, v);
      setParams(next);
    },
    [params, setParams],
  );

  const buildingsFc = useQuery({ queryKey: ["buildings-geojson"], queryFn: api.buildingsGeojson });
  const buildings = useQuery({ queryKey: ["buildings"], queryFn: api.buildings });
  const building = useQuery({ queryKey: ["building", buildingId], queryFn: () => api.building(buildingId!), enabled: !!buildingId });
  const floors = useMemo(() => building.data?.floors ?? [], [building.data]);

  // Default to the lowest placed floor at or above ground.
  const level = useMemo(() => {
    if (levelParam !== null) return Number(levelParam);
    const shown = floors.filter((f) => f.status === "placed" || f.status === "needs_review");
    const pool = shown.length ? shown : floors;
    if (!pool.length) return null;
    const above = pool.filter((f) => f.level >= 0).sort((a, b) => a.level - b.level);
    return (above[0] ?? pool[0]).level;
  }, [levelParam, floors]);

  const levels = useMemo(() => [...new Set(floors.map((f) => f.level))].sort((a, b) => b - a), [floors]);
  const minLevel = levels.length ? levels[levels.length - 1] : 0;
  const elevationOf = useCallback((l: number) => (l - minLevel) * FLOOR_HEIGHT_M, [minLevel]);

  // Every floor of the building: the viewed one for rooms, the rest for the 3D stack.
  const floorDetails = useQuery({
    queryKey: ["floors", floors.map((f) => `${f.id}:${f.updated_at}`).join(",")],
    queryFn: () => Promise.all(floors.map((f) => api.floor(f.id))),
    enabled: floors.length > 0,
    // Keep showing the old rooms while edited ones load, instead of an empty map.
    placeholderData: (prev) => (prev && prev.every((f) => floors.some((x) => x.id === f.id)) ? prev : undefined),
  });
  const rooms = useMemo<FeatureCollection | undefined>(() => {
    if (!floorDetails.data) return undefined;
    return {
      type: "FeatureCollection",
      features: floorDetails.data.filter((f) => f.level === level).flatMap((f) => f.geojson?.features ?? []),
    };
  }, [floorDetails.data, level]);
  const stack = useMemo<FeatureCollection | undefined>(() => {
    if (!floorDetails.data) return undefined;
    const features: Feature[] = [];
    // Floors above the one being viewed are left out, like a cutaway, so they don't hide it.
    for (const f of floorDetails.data) {
      if (level !== null && f.level >= level) continue;
      const feats = f.geojson?.features ?? [];
      const outline = feats.filter((x) => x.properties?.feature === "outline");
      const base = elevationOf(f.level);
      for (const x of outline.length ? outline : feats.filter((x) => x.properties?.feature === "room")) {
        features.push({ ...x, properties: { base, top: base + 0.25 } });
      }
    }
    return { type: "FeatureCollection", features };
  }, [floorDetails.data, level, elevationOf]);

  // Poll while something in this building is still converting.
  useEffect(() => {
    const busy = floors.some((f) => f.status === "pending" || f.status === "processing");
    if (!busy) return;
    const t = setInterval(() => qc.invalidateQueries({ queryKey: ["building", buildingId] }), 2000);
    return () => clearInterval(t);
  }, [floors, buildingId, qc]);

  const sidebarPadding = useCallback(
    () => (isSmall() ? { top: 60, bottom: window.innerHeight * 0.5, left: 40, right: 40 } : { top: 80, bottom: 80, left: collapsed ? 80 : 440, right: 100 }),
    [collapsed],
  );

  // Frame the data set on first load, and the building when one is chosen.
  useEffect(() => {
    if (!map || buildingId || !buildingsFc.data?.features.length) return;
    const b = boundsOf(buildingsFc.data.features.map((f) => f.geometry));
    if (b) map.fitBounds(b, { padding: sidebarPadding(), maxZoom: 16, duration: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, buildingsFc.data === undefined]);

  useEffect(() => {
    if (!map || !building.data || editFloorId) return;
    const g: Geometry = building.data.footprint ?? { type: "Point", coordinates: [building.data.lon, building.data.lat] };
    const b = boundsOf([g]);
    if (b) map.fitBounds(b, { padding: sidebarPadding(), maxZoom: 19, duration: 900, pitch: threeD ? 55 : 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, building.data?.id]);

  // After choosing a room in search, select it once its level has loaded.
  useEffect(() => {
    if (!pendingRoom || !rooms || !map) return;
    const f = rooms.features.find((x) => x.id === `${pendingRoom.floorId}:${pendingRoom.roomId}`);
    if (!f) return;
    const c = boundsOf([f.geometry])?.getCenter();
    if (c) {
      setRoom({ feature: f as RoomClick["feature"], lngLat: [c.lng, c.lat] });
      if (!pendingRoom.stay) map.flyTo({ center: c, zoom: 19.5 });
    }
    setPendingRoom(null);
  }, [pendingRoom, rooms, map]);

  // Keyboard: Page Up/Down or [ and ] move between floors; Escape closes things.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest("input, textarea, select")) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey && !editFloorId) {
        if (levelFloor?.undo && !undo.isPending) {
          e.preventDefault();
          undo.mutate(levelFloor.id);
        }
        return;
      }
      if (tool === "draw" || tool === "split") {
        if (e.key === "Backspace") {
          e.preventDefault();
          setDrawPts((p) => p.slice(0, -1));
          return;
        }
        if (e.key === "Enter" && tool === "draw") {
          finishDrawing();
          return;
        }
      }
      if (e.key === "Escape") {
        if (tool) stopTool();
        else if (room || wallSel.length) setRoom(null);
        else if (picking) setPicking(false);
        return;
      }
      if (level === null || editFloorId || !levels.length) return;
      const i = levels.indexOf(level);
      const up = e.key === "PageUp" || e.key === "]";
      const down = e.key === "PageDown" || e.key === "[";
      if (!up && !down) return;
      const next = levels[up ? Math.max(0, i - 1) : Math.min(levels.length - 1, i + 1)];
      if (next !== level) {
        e.preventDefault();
        setRoom(null);
        update({ l: String(next) });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const outlinePick = useMutation({
    mutationFn: ([lon, lat]: [number, number]) =>
      pickMode === "remove" ? api.removeOutlinePart(buildingId!, lat, lon) : api.addOutlinePart(buildingId!, lat, lon),
    onSuccess: (b: Building) => {
      qc.invalidateQueries();
      toast.success(pickMode === "remove" ? "Part removed from the outline" : "Part added to the outline", b.floors.length ? "Floor plans were fitted to the new outline." : undefined);
      setPickMode(null);
    },
    onError: (e: Error) => toast.error("Couldn't change the outline", e.message),
  });

  const pick = useMutation({
    mutationFn: async ([lon, lat]: [number, number]) => {
      const fp = await api.footprintAt(lat, lon).catch(() => null);
      return api.createBuilding({
        name: fp?.name ?? undefined,
        place: { lat, lon, osm_id: fp?.contains ? fp.osm_id : null, footprint: fp?.contains ? fp.footprint : null, name: fp?.name ?? "New building" },
      });
    },
    onSuccess: (b: Building) => {
      qc.invalidateQueries();
      setPicking(false);
      toast.success(`Added ${b.name}`, b.has_footprint ? undefined : "No OpenStreetMap outline found at that spot.");
      update({ b: b.id, l: null, edit: null });
    },
    onError: (e: Error) => toast.error("Couldn't add building", e.message),
  });

  // The plan shown at the current level (each level has one).
  const levelFloor = floorDetails.data?.find((f) => f.level === level) ?? null;
  const stopTool = () => {
    setTool(null);
    setDrawPts([]);
    setSplitTarget(null);
  };
  const chooseTool = (t: Tool | null) => {
    setDrawPts([]);
    if (t === "split") {
      if (!room) return;
      setSplitTarget({ floorId: floorOf(room), roomId: String(room.feature.properties.room_id) });
    } else setSplitTarget(null);
    if (t === "draw") setRoom(null);
    setTool(t);
  };
  const undo = useMutation({
    mutationFn: (floorId: string) => api.undoFloor(floorId),
    onSuccess: async ({ undone }) => {
      setRoom(null);
      await qc.invalidateQueries();
      toast.success(`Undid “${undone}”`);
    },
    onError: (e: Error) => toast.error("Couldn't undo", e.message),
  });
  const drawRoom = useMutation({
    mutationFn: (ring: [number, number][]) => api.addRoom(levelFloor!.id, { ring }),
    onSuccess: async ({ room_id, floor }) => {
      stopTool();
      await qc.invalidateQueries();
      toast.success("Room added", "Give it a number.");
      setPendingRoom({ floorId: floor.id, roomId: room_id, stay: true });
    },
    onError: (e: Error) => toast.error("Couldn't add the room", e.message),
  });
  const splitRoom = useMutation({
    mutationFn: (line: [number, number][]) => api.splitRoom(splitTarget!.floorId, splitTarget!.roomId, line),
    onSuccess: async ({ room_ids, floor }) => {
      stopTool();
      await qc.invalidateQueries();
      toast.success(`Split into ${room_ids.length} rooms`, "Ctrl+Z puts it back.");
      setPendingRoom({ floorId: floor.id, roomId: room_ids[room_ids.length - 1], stay: true });
    },
    onError: (e: Error) => {
      setDrawPts([]);
      toast.error("Couldn't split the room", e.message);
    },
  });
  const finishDrawing = () => {
    if (tool === "draw" && drawPts.length >= 3 && levelFloor && !drawRoom.isPending) drawRoom.mutate(drawPts);
  };
  const onToolClick = (ll: [number, number]) => {
    if (tool === "split") {
      const pts = [...drawPts, ll];
      if (pts.length >= 2) {
        setDrawPts(pts);
        splitRoom.mutate(pts);
      } else setDrawPts(pts);
      return;
    }
    // Clicking near the first corner closes the room.
    if (drawPts.length >= 3 && map) {
      const a = map.project(drawPts[0]);
      const b = map.project(ll);
      if (Math.hypot(a.x - b.x, a.y - b.y) < 12) return finishDrawing();
    }
    setDrawPts((p) => [...p, ll]);
  };
  useDrawLayer(map, editFloorId ? null : tool, drawPts);
  // Leaving the building or opening the placement editor puts the tools away.
  useEffect(() => {
    if (editFloorId || !buildingId) stopTool();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editFloorId, buildingId, level]);

  const editFloor = floorDetails.data?.find((f) => f.id === editFloorId) ?? null;
  const noBuildings = buildings.data?.length === 0;
  const selectRoomFromSearch = (r: { building_id: string; level: number; floor_id: string; room_id: string }) => {
    update({ b: r.building_id, l: String(r.level) });
    setPendingRoom({ floorId: r.floor_id, roomId: r.room_id });
  };
  const changeLevel = (l: number) => {
    setRoom(null);
    update({ l: String(l) });
  };

  return (
    <div className="relative h-full overflow-hidden">
      <MapView
        buildings={buildingsFc.data}
        rooms={rooms}
        stack={stack}
        elevation={level === null ? 0 : elevationOf(level)}
        shellHeight={level === null ? 0 : elevationOf(level)}
        selectedBuildingId={buildingId}
        selectedRoomUids={selection.map(uidOf)}
        selectedWallUids={wallSel}
        areaSelect={tool === "select"}
        basemap={basemap}
        threeD={threeD && !editFloorId}
        hideRooms={!!editFloorId}
        picking={picking || tool === "draw" || tool === "split"}
        onReady={setMap}
        onHover={setHover}
        onSelectBuilding={(id) => {
          if (editFloorId) return;
          setRoom(null);
          update({ b: id, l: null });
        }}
        onSelectRoom={(r, additive) => (r && additive ? addRooms([r], true) : setRoom(r))}
        onSelectRooms={(clicks, walls) => {
          addRooms(clicks, false);
          addWalls(walls);
        }}
        onPick={(ll) => (tool === "draw" || tool === "split" ? onToolClick(ll) : pickMode === "new" ? pick.mutate(ll) : outlinePick.mutate(ll))}
      />
      <HoverTip hover={hover} />

      {picking && (
        <div className="panel absolute left-1/2 top-4 z-20 flex -translate-x-1/2 items-center gap-3 px-4 py-2.5 text-sm">
          {pick.isPending || outlinePick.isPending ? <Spinner /> : <Building2 className="h-4 w-4 text-indigo-600" />}
          {pickMode === "add"
            ? "Click the shape on the map that belongs to this building"
            : pickMode === "remove"
              ? "Click the part of the outline to take away"
              : "Click a building on the map to add it"}
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setPicking(false)}>
            Cancel
          </button>
        </div>
      )}

      {/* Sidebar: a left panel on desktop, a bottom sheet on phones */}
      <aside
        className={clsx(
          "panel absolute z-10 flex flex-col overflow-hidden transition-transform duration-300",
          "inset-x-2 bottom-2 max-h-[55vh] md:inset-x-auto md:bottom-3 md:left-3 md:top-3 md:max-h-none md:w-[400px]",
          collapsed && "translate-y-[calc(100%-56px)] md:-translate-x-[calc(100%+12px)] md:translate-y-0",
        )}
      >
        <button className="flex justify-center py-1.5 text-slate-300 md:hidden" onClick={() => setCollapsed((c) => !c)} aria-label="Toggle panel">
          <span className="h-1.5 w-10 rounded-full bg-slate-200" />
        </button>
        {editFloorId && map && building.data && editFloor ? (
          <PlacementEditor key={editFloor.id} map={map} floor={editFloor} building={building.data} onClose={() => update({ edit: null })} />
        ) : buildingId ? (
          building.data ? (
            <>
              <div className="border-b border-slate-100 p-3">
                <SearchBox onBuilding={(id) => update({ b: id, l: null })} onRoom={selectRoomFromSearch} />
              </div>
              <BuildingPanel
                key={building.data.id}
                building={building.data}
                level={level}
                onLevel={changeLevel}
                onBack={() => {
                  setRoom(null);
                  update({ b: null, l: null, edit: null });
                }}
                onEditPlacement={(fid) => {
                  const f = floors.find((x) => x.id === fid);
                  update({ edit: fid, l: f ? String(f.level) : null });
                }}
                onPickOutline={(mode) => setPickMode(mode)}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center p-10">
              <Spinner className="h-6 w-6 text-slate-400" />
            </div>
          )
        ) : (
          <>
            <div className="border-b border-slate-100 p-3">
              <SearchBox onBuilding={(id) => update({ b: id, l: null })} onRoom={selectRoomFromSearch} />
            </div>
            <div className="flex items-center justify-between px-4 pb-2 pt-4">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Buildings · {buildings.data?.length ?? 0}</h2>
              <button className="btn-primary px-2.5 py-1.5 text-xs" onClick={() => setAdding(true)}>
                <Plus className="h-3.5 w-3.5" /> Add building
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-2 pb-3">
              {buildings.isLoading && <Spinner className="m-4 text-slate-400" />}
              {noBuildings && (
                <Empty icon={<Building2 className="h-5 w-5" />} title="No buildings yet">
                  Add one by address, or let Discover find floor plans for you.
                </Empty>
              )}
              {buildings.data?.map((b) => (
                <button
                  key={b.id}
                  onClick={() => update({ b: b.id, l: null })}
                  className="group mb-1 flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition hover:bg-slate-50"
                >
                  <div
                    className={clsx(
                      "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl",
                      b.placed_count ? "bg-indigo-50 text-indigo-600" : b.review_count ? "bg-amber-50 text-amber-600" : "bg-slate-100 text-slate-400",
                    )}
                  >
                    <Building2 className="h-4 w-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{b.name}</div>
                    <div className="truncate text-xs text-slate-500">
                      {b.floor_count ? `${b.floor_count} floor${b.floor_count > 1 ? "s" : ""}` : "No floors yet"}
                      {b.review_count > 0 && <span className="text-amber-600"> · {b.review_count} to check</span>}
                    </div>
                  </div>
                  <ChevronRight className="h-4 w-4 text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-slate-500" />
                </button>
              ))}
            </div>
          </>
        )}
      </aside>

      <button
        onClick={() => setCollapsed((c) => !c)}
        className={clsx(
          "panel absolute top-1/2 z-10 hidden -translate-y-1/2 rounded-l-none rounded-r-lg border-l-0 px-0.5 py-3 text-slate-500 transition-all duration-300 hover:text-slate-800 md:block",
          collapsed ? "left-0" : "left-[412px]",
        )}
        aria-label={collapsed ? "Show panel" : "Hide panel"}
      >
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
      </button>

      {/* First run: nothing on the map yet */}
      {noBuildings && !adding && !picking && (
        <div className="pointer-events-none absolute inset-0 z-[5] hidden items-center justify-center md:flex md:pl-[400px]">
          <div className="panel pointer-events-auto w-[420px] p-6">
            <div className="mb-1 text-lg font-semibold">Put your first floor plans on the map</div>
            <p className="mb-5 text-sm text-slate-500">
              Point Floored at a website with floor plans and it will find them, work out which building each belongs to, and line
              them up with the real building. Or start with one building yourself.
            </p>
            <div className="grid grid-cols-2 gap-3">
              <button className="btn-primary flex-col items-start gap-1 p-4 text-left" onClick={() => navigate("/discover?add=1")}>
                <Radar className="h-5 w-5" />
                <span className="font-semibold">Search a website</span>
                <span className="text-xs font-normal text-indigo-100">Crawl for plans automatically</span>
              </button>
              <button className="btn-secondary flex-col items-start gap-1 p-4 text-left" onClick={() => setAdding(true)}>
                <Building2 className="h-5 w-5 text-indigo-600" />
                <span className="font-semibold">Add a building</span>
                <span className="text-xs font-normal text-slate-500">Then upload its plans</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Floor switcher, like an elevator panel */}
      {building.data && levels.length > 0 && !editFloorId && (
        <div className="group/levels absolute right-3 top-3 z-10 flex flex-col items-center gap-1">
          <div className="panel flex flex-col p-1">
            <button
              className="flex h-6 w-10 items-center justify-center text-slate-400 hover:text-slate-700 disabled:opacity-30"
              disabled={level === levels[0]}
              onClick={() => level !== null && changeLevel(levels[Math.max(0, levels.indexOf(level) - 1)])}
              aria-label="Floor up"
            >
              <ChevronUp className="h-4 w-4" />
            </button>
            {levels.map((l) => {
              const fl = floors.filter((f) => f.level === l);
              const review = fl.some((f) => f.status === "needs_review");
              const check = fl.some((f) => f.level_note) || fl.length > 1;
              return (
                <div key={l} className="relative">
                  <button
                    onClick={() => changeLevel(l)}
                    className={clsx(
                      "relative h-10 w-10 rounded-lg text-sm font-semibold transition",
                      l === level ? "bg-indigo-600 text-white shadow" : "text-slate-600 hover:bg-slate-100",
                    )}
                    title={[fl.map((f) => f.name).join(", "), ...fl.map((f) => f.level_note).filter(Boolean)].join(" · ")}
                  >
                    {levelShort(l)}
                    {review && <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-amber-500" />}
                    {check && <span className="absolute bottom-1 right-1 text-[9px] font-bold text-amber-500">?</span>}
                  </button>
                  {/* Floor names, shown beside the panel: the current one always, the rest on hover. */}
                  <span
                    className={clsx(
                      "pointer-events-none absolute right-full top-1/2 mr-2 -translate-y-1/2 whitespace-nowrap rounded-md px-2 py-0.5 text-xs shadow-sm transition",
                      l === level ? "bg-indigo-600 text-white opacity-100" : "bg-white text-slate-700 opacity-0 group-hover/levels:opacity-100",
                    )}
                  >
                    {fl.map((f) => f.name).join(" + ")}
                    {check && " (check level)"}
                  </span>
                </div>
              );
            })}
            <button
              className="flex h-6 w-10 items-center justify-center text-slate-400 hover:text-slate-700 disabled:opacity-30"
              disabled={level === levels[levels.length - 1]}
              onClick={() => level !== null && changeLevel(levels[Math.min(levels.length - 1, levels.indexOf(level) + 1)])}
              aria-label="Floor down"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      <div className="absolute bottom-[136px] right-2.5 z-10 hidden md:block">
        <MapControls
          basemap={basemap}
          onBasemap={(b) => {
            setBasemap(b);
            remember("floored.basemap", b);
          }}
          threeD={threeD}
          onThreeD={(v) => {
            setThreeD(v);
            remember("floored.3d", v ? "1" : "0");
          }}
        />
      </div>
      <div className="pointer-events-none absolute inset-0 z-[4] hidden items-center justify-center md:flex md:pl-[400px]">
        <TokenNotice />
      </div>

      {(room || wallSel.length > 0) && !editFloorId && (
        <div className="absolute right-16 top-3 z-10 md:bottom-6 md:top-auto">
          {selection.length > 1 || wallSel.length > 0 ? (
            <MergeRoomsCard
              floorId={room ? floorOf(room) : wallSel[0].split(":")[0]}
              rooms={selection}
              walls={wallSel}
              onRemove={(r) => addRooms([r], true)}
              onMerged={(floorId, roomId) => {
                setRoom(null);
                setPendingRoom({ floorId, roomId, stay: true });
              }}
              onDeleted={() => setRoom(null)}
              onDropWalls={() => setWallSel([])}
              onClose={() => setRoom(null)}
            />
          ) : (
            room && <RoomCard room={room} buildingName={building.data?.name} onClose={() => setRoom(null)} onSplit={() => chooseTool("split")} />
          )}
        </div>
      )}

      {/* Editing tools for the plan on this level */}
      {building.data && levelFloor?.geojson && !editFloorId && !picking && (
        <div className={clsx("absolute top-3 z-10 hidden -translate-x-1/2 flex-col items-center gap-2 md:flex", collapsed ? "left-1/2" : "left-[calc(50%+206px)]")}>
          <FloorToolbar
            tool={tool}
            onTool={chooseTool}
            canSplit={!!room && selection.length === 1}
            undoLabel={levelFloor.undo}
            onUndo={() => undo.mutate(levelFloor.id)}
            onCrop={() => setCropFloorId(levelFloor.id)}
          />
          {tool && (
            <DrawHint
              tool={tool}
              points={drawPts.length}
              busy={drawRoom.isPending || splitRoom.isPending}
              onDone={finishDrawing}
              onBack={() => setDrawPts((p) => p.slice(0, -1))}
              onCancel={stopTool}
            />
          )}
        </div>
      )}
      {cropFloorId && <SplitFloorDialog floorId={cropFloorId} onClose={() => setCropFloorId(null)} />}

      {editFloorId && !editFloor && (
        <div className="panel absolute left-1/2 top-4 z-10 -translate-x-1/2 px-4 py-2 text-sm">
          <Spinner className="mr-2 inline" /> Loading floor…
          <button className="btn-ghost ml-2 px-1.5 py-0.5" onClick={() => update({ edit: null })}>
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {adding && (
        <AddBuildingDialog
          onClose={() => setAdding(false)}
          onCreated={(b) => {
            setAdding(false);
            update({ b: b.id, l: null });
          }}
          onPickOnMap={() => {
            setAdding(false);
            setPicking(true);
          }}
        />
      )}
    </div>
  );
}
