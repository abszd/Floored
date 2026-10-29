import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import maplibregl, { type Map as MLMap } from "maplibre-gl";
import type { FeatureCollection, Geometry, Position } from "geojson";
import { Building2, ChevronLeft, ChevronRight, Plus, X } from "lucide-react";
import { api } from "../lib/api";
import { levelShort } from "../lib/placement";
import type { Building } from "../lib/types";
import MapView, { type RoomClick } from "../components/MapView";
import BuildingPanel from "../components/BuildingPanel";
import PlacementEditor from "../components/PlacementEditor";
import RoomCard from "../components/RoomCard";
import SearchBox from "../components/SearchBox";
import AddBuildingDialog from "../components/AddBuildingDialog";
import { Empty, Spinner } from "../components/ui";
import { useToast } from "../components/Toast";

function eachPosition(g: Geometry, fn: (p: Position) => void) {
  if (g.type === "Point") fn(g.coordinates);
  else if (g.type === "Polygon") g.coordinates.forEach((r) => r.forEach(fn));
  else if (g.type === "MultiPolygon") g.coordinates.forEach((p) => p.forEach((r) => r.forEach(fn)));
}

function boundsOf(geoms: Geometry[]): maplibregl.LngLatBounds | null {
  let b: maplibregl.LngLatBounds | null = null;
  for (const g of geoms)
    eachPosition(g, (p) => {
      const ll: [number, number] = [p[0], p[1]];
      b = b ? b.extend(ll) : new maplibregl.LngLatBounds(ll, ll);
    });
  return b;
}

export default function MapPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const buildingId = params.get("b");
  const levelParam = params.get("l");
  const editFloorId = params.get("edit");
  const [map, setMap] = useState<MLMap | null>(null);
  const [room, setRoom] = useState<RoomClick | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [adding, setAdding] = useState(false);
  const [picking, setPicking] = useState(false);
  const [pendingRoom, setPendingRoom] = useState<{ floorId: string; roomId: string } | null>(null);

  const update = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) v === null ? next.delete(k) : next.set(k, v);
      setParams(next, { replace: false });
    },
    [params, setParams],
  );

  const buildingsFc = useQuery({ queryKey: ["buildings-geojson"], queryFn: api.buildingsGeojson });
  const buildings = useQuery({ queryKey: ["buildings"], queryFn: api.buildings });
  const building = useQuery({ queryKey: ["building", buildingId], queryFn: () => api.building(buildingId!), enabled: !!buildingId });

  // Default to the lowest placed floor at or above ground.
  const level = useMemo(() => {
    if (levelParam !== null) return Number(levelParam);
    const floors = building.data?.floors ?? [];
    const shown = floors.filter((f) => f.status === "placed" || f.status === "needs_review");
    const pool = shown.length ? shown : floors;
    if (!pool.length) return null;
    const above = pool.filter((f) => f.level >= 0).sort((a, b) => a.level - b.level);
    return (above[0] ?? pool[0]).level;
  }, [levelParam, building.data]);

  const floorsAtLevel = useMemo(() => (building.data?.floors ?? []).filter((f) => f.level === level), [building.data, level]);
  const floorDetails = useQuery({
    queryKey: ["floors", floorsAtLevel.map((f) => `${f.id}:${f.updated_at}`).join(",")],
    queryFn: () => Promise.all(floorsAtLevel.map((f) => api.floor(f.id))),
    enabled: floorsAtLevel.length > 0,
  });
  const rooms = useMemo<FeatureCollection | undefined>(() => {
    if (!floorDetails.data) return undefined;
    return { type: "FeatureCollection", features: floorDetails.data.flatMap((f) => f.geojson?.features ?? []) };
  }, [floorDetails.data]);

  // Poll while something in this building is still converting.
  useEffect(() => {
    const busy = building.data?.floors.some((f) => f.status === "pending" || f.status === "processing");
    if (!busy) return;
    const t = setInterval(() => qc.invalidateQueries({ queryKey: ["building", buildingId] }), 2000);
    return () => clearInterval(t);
  }, [building.data, buildingId, qc]);

  // Frame the data set on first load, and the building when one is chosen.
  useEffect(() => {
    if (!map || buildingId || !buildingsFc.data?.features.length) return;
    const b = boundsOf(buildingsFc.data.features.map((f) => f.geometry));
    if (b) map.fitBounds(b, { padding: 80, maxZoom: 16, duration: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, buildingsFc.data === undefined]);

  useEffect(() => {
    if (!map || !building.data || editFloorId) return;
    const g: Geometry = building.data.footprint ?? { type: "Point", coordinates: [building.data.lon, building.data.lat] };
    const b = boundsOf([g]);
    if (b) map.fitBounds(b, { padding: { top: 80, bottom: 80, left: collapsed ? 80 : 420, right: 80 }, maxZoom: 19, duration: 800 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, building.data?.id]);

  // After choosing a room in search, select it once its level has loaded.
  useEffect(() => {
    if (!pendingRoom || !rooms || !map) return;
    const f = rooms.features.find((x) => x.id === `${pendingRoom.floorId}:${pendingRoom.roomId}`);
    if (!f) return;
    const b = boundsOf([f.geometry]);
    const c = b?.getCenter();
    if (c) {
      setRoom({ feature: f as RoomClick["feature"], lngLat: [c.lng, c.lat] });
      map.flyTo({ center: c, zoom: 19.5 });
    }
    setPendingRoom(null);
  }, [pendingRoom, rooms, map]);

  const pick = useMutation({
    mutationFn: async ([lon, lat]: [number, number]) => {
      const fp = await api.footprintAt(lat, lon);
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

  const editingFloor = floorDetails.data?.find((f) => f.id === editFloorId) ?? null;
  const editFloorQuery = useQuery({ queryKey: ["floor", editFloorId], queryFn: () => api.floor(editFloorId!), enabled: !!editFloorId && !editingFloor });
  const editFloor = editingFloor ?? editFloorQuery.data ?? null;
  const levels = useMemo(() => [...new Set((building.data?.floors ?? []).map((f) => f.level))].sort((a, b) => b - a), [building.data]);

  return (
    <div className="relative h-full overflow-hidden">
      <MapView
        buildings={buildingsFc.data}
        rooms={rooms}
        selectedBuildingId={buildingId}
        selectedRoomUid={room ? String(room.feature.id ?? room.feature.properties.uid) : null}
        hideRooms={!!editFloorId}
        picking={picking}
        onReady={setMap}
        onSelectBuilding={(id) => {
          if (editFloorId) return;
          setRoom(null);
          update({ b: id, l: null });
        }}
        onSelectRoom={setRoom}
        onPick={(ll) => pick.mutate(ll)}
      />

      {picking && (
        <div className="panel absolute left-1/2 top-4 z-10 flex -translate-x-1/2 items-center gap-3 px-4 py-2.5 text-sm">
          {pick.isPending ? <Spinner /> : <Building2 className="h-4 w-4 text-indigo-600" />}
          Click a building on the map to add it
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setPicking(false)}>
            Cancel
          </button>
        </div>
      )}

      {/* Sidebar */}
      <aside
        className={`panel absolute bottom-3 left-3 top-3 z-10 flex w-[380px] max-w-[calc(100vw-24px)] flex-col overflow-hidden transition-transform duration-300 ${collapsed ? "-translate-x-[calc(100%+12px)]" : ""}`}
      >
        {editFloorId && map && building.data && editFloor ? (
          <PlacementEditor key={editFloor.id} map={map} floor={editFloor} building={building.data} onClose={() => update({ edit: null })} />
        ) : buildingId ? (
          building.data ? (
            <>
              <div className="border-b border-slate-100 p-3">
                <SearchBox
                  onBuilding={(id) => update({ b: id, l: null })}
                  onRoom={(r) => {
                    update({ b: r.building_id, l: String(r.level) });
                    setPendingRoom({ floorId: r.floor_id, roomId: r.room_id });
                  }}
                />
              </div>
              <BuildingPanel
                key={building.data.id}
                building={building.data}
                level={level}
                onLevel={(l) => {
                  setRoom(null);
                  update({ l: String(l) });
                }}
                onBack={() => {
                  setRoom(null);
                  update({ b: null, l: null, edit: null });
                }}
                onEditPlacement={(fid) => {
                  const f = building.data!.floors.find((x) => x.id === fid);
                  update({ edit: fid, l: f ? String(f.level) : null });
                }}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center">
              <Spinner className="h-6 w-6 text-slate-400" />
            </div>
          )
        ) : (
          <>
            <div className="border-b border-slate-100 p-3">
              <SearchBox
                onBuilding={(id) => update({ b: id, l: null })}
                onRoom={(r) => {
                  update({ b: r.building_id, l: String(r.level) });
                  setPendingRoom({ floorId: r.floor_id, roomId: r.room_id });
                }}
              />
            </div>
            <div className="flex items-center justify-between px-4 pb-2 pt-4">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Buildings · {buildings.data?.length ?? 0}</h2>
              <button className="btn-primary px-2.5 py-1.5 text-xs" onClick={() => setAdding(true)}>
                <Plus className="h-3.5 w-3.5" /> Add building
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-2 pb-3">
              {buildings.data?.length === 0 && (
                <Empty icon={<Building2 className="h-5 w-5" />} title="No buildings yet">
                  Add one by address, or let the Discover page find floor plans for you.
                </Empty>
              )}
              {buildings.data?.map((b) => (
                <button key={b.id} onClick={() => update({ b: b.id, l: null })} className="mb-1 flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left hover:bg-slate-50">
                  <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${b.placed_count ? "bg-indigo-50 text-indigo-600" : b.review_count ? "bg-amber-50 text-amber-600" : "bg-slate-100 text-slate-400"}`}>
                    <Building2 className="h-4 w-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{b.name}</div>
                    <div className="truncate text-xs text-slate-500">
                      {b.floor_count ? `${b.floor_count} floor${b.floor_count > 1 ? "s" : ""}` : "No floors yet"}
                      {b.review_count > 0 && <span className="text-amber-600"> · {b.review_count} to check</span>}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </>
        )}
      </aside>

      <button
        onClick={() => setCollapsed((c) => !c)}
        className={`panel absolute top-1/2 z-10 -translate-y-1/2 rounded-l-none rounded-r-lg border-l-0 px-0.5 py-3 text-slate-500 transition-all duration-300 hover:text-slate-800 ${collapsed ? "left-0" : "left-[392px]"}`}
        aria-label={collapsed ? "Show panel" : "Hide panel"}
      >
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
      </button>

      {/* Level switcher, like an elevator panel */}
      {building.data && levels.length > 0 && !editFloorId && (
        <div className="panel absolute right-3 top-3 z-10 flex flex-col overflow-hidden p-1">
          {levels.map((l) => (
            <button
              key={l}
              onClick={() => {
                setRoom(null);
                update({ l: String(l) });
              }}
              className={`h-9 w-9 rounded-lg text-sm font-semibold transition ${l === level ? "bg-indigo-600 text-white" : "text-slate-600 hover:bg-slate-100"}`}
              title={building.data!.floors.find((f) => f.level === l)?.name}
            >
              {levelShort(l)}
            </button>
          ))}
        </div>
      )}

      {room && !editFloorId && (
        <div className="absolute bottom-6 right-16 z-10">
          <RoomCard room={room} buildingName={building.data?.name} onClose={() => setRoom(null)} />
        </div>
      )}

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
