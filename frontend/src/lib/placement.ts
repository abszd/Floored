// Mirror of backend/floored/placement/transform.py, so the editor can move the
// plan image live without a round trip.
import type { Placement } from "./types";

const R = 6378137;
const DEG = Math.PI / 180;

export type LngLat = [number, number];

function k(lat0: number) {
  return { kx: DEG * R * Math.cos(lat0 * DEG), ky: DEG * R };
}

export function toLocal(p: Placement, lon: number, lat: number): [number, number] {
  const { kx, ky } = k(p.center_lat);
  return [(lon - p.center_lon) * kx, (lat - p.center_lat) * ky];
}

export function toLonLat(p: Placement, x: number, y: number): LngLat {
  const { kx, ky } = k(p.center_lat);
  return [p.center_lon + x / kx, p.center_lat + y / ky];
}

/** Rotate (u, v) in image-aligned metres to (east, north). */
function rotate(p: Placement, u: number, v: number): [number, number] {
  const t = p.rotation_deg * DEG;
  const c = Math.cos(t);
  const s = Math.sin(t);
  return [u * c + v * s, -u * s + v * c];
}

function unrotate(p: Placement, e: number, n: number): [number, number] {
  const t = p.rotation_deg * DEG;
  const c = Math.cos(t);
  const s = Math.sin(t);
  return [e * c - n * s, e * s + n * c];
}

/** Point at fractional image position (fx, fy in 0..1, y down). */
export function at(p: Placement, fx: number, fy: number): LngLat {
  let u = (fx - 0.5) * p.width_m;
  const v = (0.5 - fy) * p.height_m;
  if (p.mirror) u = -u;
  const [e, n] = rotate(p, u, v);
  return toLonLat(p, e, n);
}

/** TL, TR, BR, BL — the order MapLibre's image source expects. */
export function corners(p: Placement): [LngLat, LngLat, LngLat, LngLat] {
  return [at(p, 0, 0), at(p, 1, 0), at(p, 1, 1), at(p, 0, 1)];
}

export const CORNER_FRACTIONS: [number, number][] = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];

/** Where the rotate handle sits: above the top edge. */
export function rotateHandle(p: Placement): LngLat {
  const extra = 0.12 * Math.max(p.width_m, p.height_m);
  const [e, n] = rotate(p, 0, p.height_m / 2 + extra);
  return toLonLat(p, e, n);
}

export function withCenter(p: Placement, lon: number, lat: number): Placement {
  return { ...p, center_lon: lon, center_lat: lat };
}

export function withRotationToward(p: Placement, lon: number, lat: number): Placement {
  const [e, n] = toLocal(p, lon, lat);
  const bearing = (Math.atan2(e, n) / DEG + 360) % 360;
  return { ...p, rotation_deg: Math.round(bearing * 10) / 10 };
}

/** Drag a corner. Uniform keeps the aspect ratio; otherwise each axis stretches on its own. */
export function withCornerAt(p: Placement, corner: number, lon: number, lat: number, uniform: boolean): Placement {
  const [e, n] = toLocal(p, lon, lat);
  let [u, v] = unrotate(p, e, n);
  if (p.mirror) u = -u;
  const [fx, fy] = CORNER_FRACTIONS[corner];
  const su = fx - 0.5; // -0.5 or 0.5
  const sv = 0.5 - fy;
  if (uniform) {
    const before = Math.hypot(su * p.width_m, sv * p.height_m);
    const after = Math.max(Math.hypot(u, v), 0.5);
    const f = after / before;
    return { ...p, width_m: p.width_m * f, height_m: p.height_m * f };
  }
  return {
    ...p,
    width_m: Math.max(Math.abs(u / su), 1),
    height_m: Math.max(Math.abs(v / sv), 1),
  };
}

export function scaled(p: Placement, f: number): Placement {
  return { ...p, width_m: p.width_m * f, height_m: p.height_m * f };
}

export function levelShort(level: number): string {
  if (level < 0) return level === -1 ? "B" : `B${-level}`;
  if (level === 0) return "G";
  return Number.isInteger(level) ? String(level) : level.toFixed(1);
}
