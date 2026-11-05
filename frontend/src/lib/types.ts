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
  /** Why the level may be wrong (it was guessed, or another plan had it). */
  level_note: string | null;
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
  /** Separate pieces in the building's outline (wings mapped apart in OSM). */
  outline_parts: number;
  /** Plans that share a level with another plan (and so would overlap). */
  shared_levels: number;
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
  address?: Record<string, string>;
}

/** What a name or address points to: a building we already have, or a new OSM place. */
export interface LookupResult {
  confidence: number;
  reason: string;
  building: Building | null;
  place: Place | null;
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
  location_hint: string | null;
  level_guess: number | null;
  level_label: string | null;
  building_id: string | null;
  building_name: string | null;
  match_confidence: number;
  match_reason: string | null;
  status: CandidateStatus;
  floor_id: string | null;
  error: string | null;
  draft_id: string | null;
}

/** Where we think a temp building is: one already on the map, or a place from OpenStreetMap. */
export interface DraftSuggestion {
  confidence: number;
  reason: string;
  chosen: boolean;
  place: Place | null;
  building: Building | null;
}

/** Found plans grouped by the building they seem to show, before it is on the map. */
export interface Draft {
  id: string;
  source_id: string | null;
  source_name: string | null;
  location_hint: string | null;
  name: string;
  address: string | null;
  match_state: "pending" | "done";
  suggestion: DraftSuggestion | null;
  /** Set once confirmed; leftover plans can still be added to it. */
  building: Building | null;
  page_urls: string[];
  candidates: Candidate[];
}

/** Live numbers a crawl reports while it runs. */
export interface CrawlDetail {
  phase?: "reading" | "checking" | "matching" | "finished";
  pages_read?: number;
  max_pages?: number;
  pages_waiting?: number;
  files_found?: number;
  files_checked?: number;
  files_total?: number;
  plans_found?: number;
  buildings_found?: number;
  buildings_matched?: number;
  current?: string | null;
  current_url?: string | null;
}

export interface Job {
  id: string;
  kind: string;
  label: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: number;
  message: string | null;
  detail: CrawlDetail;
  stop_requested: boolean;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Activity {
  running: Job[];
  queued: Job[];
  recent: Job[];
  workers: number;
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
