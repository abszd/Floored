import type {
  Activity,
  Draft,
  LookupResult,
  Building,
  BuildingSummary,
  Candidate,
  Floor,
  Job,
  Place,
  Placement,
  RoomKind,
  SearchResult,
  SheetRegion,
  FloorSummary,
  Source,
} from "./types";
import type { FeatureCollection } from "geojson";

const BASE = ((import.meta.env.VITE_API_URL as string | undefined) ?? "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const mediaUrl = (path: string | null | undefined) => (path ? BASE + path : undefined);

export const api = {
  health: () => request<{ converters: string[]; default_converter: string }>("/api/health"),
  geocode: (q: string, hint?: string | null) =>
    request<Place[]>(`/api/geocode?q=${encodeURIComponent(q)}${hint ? `&hint=${encodeURIComponent(hint)}` : ""}`),
  suggest: (q: string, near: { hint?: string | null; lat?: number; lon?: number } = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q });
    if (near.hint) params.set("hint", near.hint);
    if (near.lat != null && near.lon != null) {
      params.set("lat", String(near.lat));
      params.set("lon", String(near.lon));
    }
    return request<Place[]>(`/api/suggest?${params}`, { signal });
  },
  lookup: (body: {
    name?: string | null;
    address?: string | null;
    hint?: string | null;
    lat?: number;
    lon?: number;
    exclude_building_id?: string;
  }) =>
    request<LookupResult>("/api/lookup", json("POST", body)),
  relocateBuilding: (id: string, body: { place: Place; address?: string }) =>
    request<Building>(`/api/buildings/${id}/relocate`, json("POST", body)),
  footprintAt: (lat: number, lon: number) =>
    request<{ osm_id: string; name: string | null; footprint: Place["footprint"]; contains: boolean } | null>(
      `/api/footprint?lat=${lat}&lon=${lon}`,
    ),
  search: (q: string) => request<SearchResult>(`/api/search?q=${encodeURIComponent(q)}`),

  buildings: () => request<BuildingSummary[]>("/api/buildings"),
  buildingsGeojson: () => request<FeatureCollection>("/api/buildings.geojson"),
  building: (id: string) => request<Building>(`/api/buildings/${id}`),
  createBuilding: (body: { name?: string; address?: string; place?: Partial<Place> & { lat: number; lon: number } }) =>
    request<Building>("/api/buildings", json("POST", body)),
  updateBuilding: (id: string, body: Partial<Pick<Building, "name" | "address" | "aliases">>) =>
    request<Building>(`/api/buildings/${id}`, json("PATCH", body)),
  addOutlinePart: (id: string, lat: number, lon: number) =>
    request<Building>(`/api/buildings/${id}/outline/add`, json("POST", { lat, lon })),
  removeOutlinePart: (id: string, lat: number, lon: number) =>
    request<Building>(`/api/buildings/${id}/outline/remove`, json("POST", { lat, lon })),
  spreadLevels: (id: string) => request<{ moved: number; building: Building }>(`/api/buildings/${id}/spread-levels`, json("POST")),
  deleteBuilding: (id: string) => request<void>(`/api/buildings/${id}`, { method: "DELETE" }),

  addFloor: (buildingId: string, data: { file?: File; url?: string; level?: number; name?: string; page?: number }) => {
    const form = new FormData();
    if (data.file) form.append("file", data.file);
    if (data.url) form.append("url", data.url);
    if (data.level !== undefined) form.append("level", String(data.level));
    if (data.name) form.append("name", data.name);
    if (data.page) form.append("page", String(data.page));
    return request<{ floor: Floor; job_id: string }>(`/api/buildings/${buildingId}/floors`, {
      method: "POST",
      body: form,
    });
  },
  floor: (id: string) => request<Floor>(`/api/floors/${id}`),
  updateFloor: (id: string, body: { name?: string; level?: number }) =>
    request<Floor>(`/api/floors/${id}`, json("PATCH", body)),
  deleteFloor: (id: string) => request<void>(`/api/floors/${id}`, { method: "DELETE" }),
  savePlacement: (id: string, p: Placement) => request<Floor>(`/api/floors/${id}/placement`, json("PUT", p)),
  autofit: (id: string) =>
    request<{ floor: Floor; fit: { iou: number | null; ambiguous: boolean } }>(`/api/floors/${id}/autofit`, json("POST")),
  copyPlacement: (id: string, fromId: string) =>
    request<Floor>(`/api/floors/${id}/copy-placement?from_floor_id=${fromId}`, json("POST")),
  reprocess: (id: string, converter?: string) =>
    request<{ job_id: string }>(
      `/api/floors/${id}/reprocess${converter ? `?converter=${encodeURIComponent(converter)}` : ""}`,
      json("POST"),
    ),
  updateRoom: (floorId: string, roomId: string, body: { label?: string | null; kind?: RoomKind }) =>
    request<Floor>(`/api/floors/${floorId}/rooms/${roomId}`, json("PATCH", body)),
  mergeRooms: (floorId: string, body: { room_ids: string[]; label?: string | null; kind?: RoomKind }) =>
    request<{ room_id: string; floor: Floor }>(`/api/floors/${floorId}/rooms/merge`, json("POST", body)),
  deleteRoom: (floorId: string, roomId: string) =>
    request<Floor>(`/api/floors/${floorId}/rooms/${roomId}`, { method: "DELETE" }),
  deleteFeatures: (floorId: string, body: { room_ids?: string[]; walls?: number[]; doors?: number[] }) =>
    request<{ removed: { rooms: number; walls: number; doors: number }; floor: Floor }>(`/api/floors/${floorId}/delete`, json("POST", body)),
  splitRoom: (floorId: string, roomId: string, line: [number, number][]) =>
    request<{ room_ids: string[]; floor: Floor }>(`/api/floors/${floorId}/rooms/${roomId}/split`, json("POST", { line })),
  addRoom: (floorId: string, body: { ring: [number, number][]; label?: string | null; kind?: RoomKind }) =>
    request<{ room_id: string; floor: Floor }>(`/api/floors/${floorId}/rooms`, json("POST", body)),
  splitFloor: (floorId: string, body: { regions: SheetRegion[]; refit?: boolean; reconvert?: boolean }) =>
    request<{ floor: Floor; floors: FloorSummary[]; job_ids: string[] }>(`/api/floors/${floorId}/split`, json("POST", body)),
  duplicateFloor: (floorId: string, body: { level?: number; name?: string } = {}) =>
    request<Floor>(`/api/floors/${floorId}/duplicate`, json("POST", body)),
  lineUpAll: (floorId: string) => request<{ moved: number }>(`/api/floors/${floorId}/line-up-all`, json("POST")),
  undoFloor: (floorId: string) => request<{ undone: string; floor: Floor }>(`/api/floors/${floorId}/undo`, json("POST")),

  sources: () => request<Source[]>("/api/sources"),
  createSource: (body: Omit<Source, "id" | "last_crawled_at" | "candidate_counts" | "saved">) =>
    request<Source>("/api/sources", json("POST", body)),
  updateSource: (id: string, body: Partial<Omit<Source, "id" | "last_crawled_at" | "candidate_counts" | "saved">>) =>
    request<Source>(`/api/sources/${id}`, json("PATCH", body)),
  deleteSource: (id: string) => request<void>(`/api/sources/${id}`, { method: "DELETE" }),
  crawl: (id: string, opts: { use_saved?: boolean; fresh?: boolean; remove_from_map?: boolean } = {}) =>
    request<{ job_id: string; reset: { plans: number; floors: number; buildings: number } | null }>(
      `/api/sources/${id}/crawl`,
      json("POST", opts),
    ),
  clearSaved: (id: string) => request<void>(`/api/sources/${id}/saved`, { method: "DELETE" }),

  candidates: (status?: string) =>
    request<Candidate[]>(`/api/candidates${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  updateCandidate: (
    id: string,
    body: Partial<Pick<Candidate, "building_guess" | "address_guess" | "level_guess" | "level_label" | "building_id" | "draft_id">>,
  ) =>
    request<Candidate>(`/api/candidates/${id}`, json("PATCH", body)),
  approve: (id: string, body: { building_id?: string; level?: number } = {}) =>
    request<{ job_id: string }>(`/api/candidates/${id}/approve`, json("POST", body)),
  reject: (id: string) => request<Candidate>(`/api/candidates/${id}/reject`, json("POST")),
  rematch: (ids: string[]) => request<{ job_id: string }>("/api/candidates/rematch", json("POST", ids)),

  drafts: (sourceId?: string) => request<Draft[]>(`/api/drafts${sourceId ? `?source_id=${sourceId}` : ""}`),
  updateDraft: (
    id: string,
    body: { name?: string; address?: string; choice?: { building_id?: string; place?: Place } | null },
  ) => request<Draft>(`/api/drafts/${id}`, json("PATCH", body)),
  confirmDraft: (id: string, body: { building_id?: string; place?: Place; name?: string; address?: string } = {}) =>
    request<{ building: BuildingSummary; job_ids: string[] }>(`/api/drafts/${id}/confirm`, json("POST", body)),
  rejectDraft: (id: string) => request<{ rejected: number }>(`/api/drafts/${id}/reject`, json("POST")),
  rematchDraft: (id: string) => request<{ job_id: string }>(`/api/drafts/${id}/rematch`, json("POST")),

  activity: () => request<Activity>("/api/activity"),
  jobs: (active = false) => request<Job[]>(`/api/jobs${active ? "?active=true" : ""}`),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  stopJob: (id: string) => request<Job>(`/api/jobs/${id}/stop`, json("POST")),
};
