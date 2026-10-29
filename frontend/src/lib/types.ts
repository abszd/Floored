import type { FeatureCollection, Geometry } from "geojson";

export type FloorStatus = "pending" | "processing" | "placed" | "needs_review" | "error";
export type RoomKind = "room" | "corridor" | "stair" | "elevator" | "restroom" | "outdoor" | "unknown";

export interface Placement {
  center_lon: number;
  center_lat: number;
  width_m: number;
  height_m: number;
  rotation_deg: number;
  mirror: boolean;
}

export interface FloorSummary {
  id: string;
  building_id: string;
  name: string;
  level: number;
  status: FloorStatus;
  error: string | null;
  fit_score: number | null;
  room_count: number;
  image_url: string | null;
  source_url: string | null;
  updated_at: string;
}

export interface Floor extends FloorSummary {
  converter: string | null;
  image_size: [number, number] | null;
  placement: Placement | null;
  corners: [number, number][] | null;
  debug_images: Record<string, string>;
  geojson: FeatureCollection | null;
}

export interface BuildingSummary {
  id: string;
  name: string;
  address: string | null;
  lat: number;
  lon: number;
  osm_id: string | null;
  aliases: string[];
  has_footprint: boolean;
  floor_count: number;
  placed_count: number;
  review_count: number;
}

export interface Building extends BuildingSummary {
  footprint: Geometry | null;
  floors: FloorSummary[];
}

export interface Place {
  name: string;
  display_name: string;
  lat: number;
  lon: number;
  osm_id: string | null;
  category: string | null;
  kind: string | null;
  footprint: Geometry | null;
}

export interface Source {
  id: string;
  name: string;
  seed_urls: string[];
  allowed_domains: string[];
  location_hint: string | null;
  max_pages: number;
  max_depth: number;
  last_crawled_at: string | null;
  candidate_counts: Record<string, number>;
}

export type CandidateStatus = "new" | "matched" | "unmatched" | "approved" | "processed" | "rejected" | "error";

export interface Candidate {
  id: string;
  source_id: string | null;
  asset_url: string;
  page_index: number;
  page_url: string | null;
  page_title: string | null;
  context: string | null;
  image_url: string | null;
  plan_score: number;
  building_guess: string | null;
  address_guess: string | null;
  level_guess: number | null;
  level_label: string | null;
  building_id: string | null;
  building_name: string | null;
  match_confidence: number;
  match_reason: string | null;
  status: CandidateStatus;
  floor_id: string | null;
  error: string | null;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  progress: number;
  message: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
  finished_at: string | null;
}

export interface SearchResult {
  buildings: BuildingSummary[];
  rooms: {
    building_id: string;
    building_name: string;
    floor_id: string;
    floor_name: string;
    level: number;
    room_id: string;
    label: string;
    kind: RoomKind | null;
  }[];
}
