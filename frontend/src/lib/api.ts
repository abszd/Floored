import type {
  Building,
  BuildingSummary,
  Candidate,
  Floor,
  Job,
  Place,
  Placement,
  RoomKind,
  SearchResult,
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
  geocode: (q: string) => request<Place[]>(`/api/geocode?q=${encodeURIComponent(q)}`),
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
  deleteRoom: (floorId: string, roomId: string) =>
    request<Floor>(`/api/floors/${floorId}/rooms/${roomId}`, { method: "DELETE" }),

  sources: () => request<Source[]>("/api/sources"),
  createSource: (body: Omit<Source, "id" | "last_crawled_at" | "candidate_counts">) =>
    request<Source>("/api/sources", json("POST", body)),
  deleteSource: (id: string) => request<void>(`/api/sources/${id}`, { method: "DELETE" }),
  crawl: (id: string) => request<{ job_id: string }>(`/api/sources/${id}/crawl`, json("POST")),

  candidates: (status?: string) =>
    request<Candidate[]>(`/api/candidates${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  updateCandidate: (id: string, body: Partial<Pick<Candidate, "building_guess" | "address_guess" | "level_guess" | "building_id">>) =>
    request<Candidate>(`/api/candidates/${id}`, json("PATCH", body)),
  approve: (id: string, body: { building_id?: string; level?: number } = {}) =>
    request<{ job_id: string }>(`/api/candidates/${id}/approve`, json("POST", body)),
  reject: (id: string) => request<Candidate>(`/api/candidates/${id}/reject`, json("POST")),
  rematch: (ids: string[]) => request<{ job_id: string }>("/api/candidates/rematch", json("POST", ids)),

  jobs: (active = false) => request<Job[]>(`/api/jobs${active ? "?active=true" : ""}`),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
};
