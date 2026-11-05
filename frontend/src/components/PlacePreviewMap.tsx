import { useEffect, useRef, useState } from "react";
import mapboxgl, { type GeoJSONSource, type Map as MBMap } from "mapbox-gl";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import { FALLBACK_STYLE, MAPBOX_TOKEN } from "./MapView";

interface Spot {
  lat: number;
  lon: number;
  footprint?: Geometry | null;
}

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

function features(spot: Spot | null | undefined, role: string): Feature[] {
  if (!spot) return [];
  const point: Feature = { type: "Feature", properties: { role }, geometry: { type: "Point", coordinates: [spot.lon, spot.lat] } };
  return spot.footprint ? [{ type: "Feature", properties: { role }, geometry: spot.footprint }, point] : [point];
}

function extend(bounds: mapboxgl.LngLatBounds, g: Geometry) {
  if (g.type === "Point") bounds.extend(g.coordinates as [number, number]);
  else if (g.type === "Polygon") g.coordinates[0].forEach((c) => bounds.extend(c as [number, number]));
  else if (g.type === "MultiPolygon") g.coordinates.forEach((poly) => poly[0].forEach((c) => bounds.extend(c as [number, number])));
}

/** A small map showing where a building is (and, when moving it, where it was). */
export default function PlacePreviewMap({ place, current, className = "h-48" }: { place: Spot | null; current?: Spot | null; className?: string }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MBMap | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const map = new mapboxgl.Map({
      container: container.current!,
      style: MAPBOX_TOKEN ? "mapbox://styles/mapbox/standard" : FALLBACK_STYLE,
      center: [-98.5, 39.8],
      zoom: 3,
      attributionControl: false,
      interactive: true,
    });
    mapRef.current = map;
    map.addControl(new mapboxgl.AttributionControl({ compact: true }), "bottom-left");
    map.on("style.load", () => {
      map.addSource("spots", { type: "geojson", data: EMPTY });
      const slot = MAPBOX_TOKEN ? { slot: "top" } : {};
      map.addLayer({
        id: "spot-fill",
        type: "fill",
        source: "spots",
        filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
        paint: { "fill-color": ["match", ["get", "role"], "new", "#6366f1", "#94a3b8"], "fill-opacity": 0.3 },
        ...slot,
      });
      map.addLayer({
        id: "spot-line",
        type: "line",
        source: "spots",
        filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
        paint: {
          "line-color": ["match", ["get", "role"], "new", "#4338ca", "#64748b"],
          "line-width": 2,
          "line-dasharray": ["match", ["get", "role"], "new", ["literal", [1, 0]], ["literal", [2, 2]]],
        },
        ...slot,
      });
      map.addLayer({
        id: "spot-point",
        type: "circle",
        source: "spots",
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-radius": ["match", ["get", "role"], "new", 6, 4],
          "circle-color": ["match", ["get", "role"], "new", "#4f46e5", "#94a3b8"],
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 2,
        },
        ...slot,
      });
      setReady(true);
    });
    map.on("error", (e) => {
      if (MAPBOX_TOKEN && !map.isStyleLoaded()) {
        console.warn("Mapbox basemap unavailable for the preview", e.error);
        map.setStyle(FALLBACK_STYLE);
      }
    });
    // Follow the box's size (cards grow and shrink with the picture-size slider).
    const ro = new ResizeObserver(() => map.resize());
    ro.observe(container.current!);
    return () => {
      ro.disconnect();
      map.remove();
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const data: FeatureCollection = { type: "FeatureCollection", features: [...features(current, "current"), ...features(place, "new")] };
    (map.getSource("spots") as GeoJSONSource | undefined)?.setData(data);
    // Frame the new place when there is one; the current outline is context only.
    const focus = place ? features(place, "new") : features(current, "current");
    if (!focus.length) return;
    const bounds = new mapboxgl.LngLatBounds();
    focus.forEach((f) => extend(bounds, f.geometry));
    const ne = bounds.getNorthEast(), sw = bounds.getSouthWest();
    const span = Math.max(ne.lng - sw.lng, ne.lat - sw.lat);
    if (span < 0.002) map.jumpTo({ center: bounds.getCenter(), zoom: 17 });
    else map.fitBounds(bounds, { padding: 40, maxZoom: 17, duration: 0 });
  }, [place, current, ready]);

  return <div ref={container} className={`w-full overflow-hidden rounded-lg border border-slate-200 ${className}`} />;
}
