import { useEffect, useRef } from "react";
import maplibregl, { type GeoJSONSource, type Map as MLMap, type MapLayerMouseEvent } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Feature, FeatureCollection, Geometry } from "geojson";

const STYLE = (import.meta.env.VITE_MAP_STYLE as string | undefined) ?? "https://tiles.openfreemap.org/styles/positron";
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };
// Used when the basemap can't be reached, so buildings and rooms still show.
const FALLBACK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
  sources: {},
  layers: [{ id: "background", type: "background", paint: { "background-color": "#eef2f6" } }],
};

export const KIND_COLORS: Record<string, string> = {
  room: "#a5b4fc",
  corridor: "#e2e8f0",
  stair: "#fcd34d",
  elevator: "#c4b5fd",
  restroom: "#7dd3fc",
  unknown: "#c7d2fe",
};

export interface RoomClick {
  feature: Feature<Geometry, Record<string, unknown>>;
  lngLat: [number, number];
}

interface Props {
  buildings: FeatureCollection | undefined;
  rooms: FeatureCollection | undefined;
  selectedBuildingId: string | null;
  selectedRoomUid: string | null;
  hideRooms?: boolean;
  picking?: boolean;
  initialView?: { center: [number, number]; zoom: number };
  onSelectBuilding: (id: string | null) => void;
  onSelectRoom: (click: RoomClick | null) => void;
  onPick?: (lngLat: [number, number]) => void;
  onReady?: (map: MLMap) => void;
}

/** The base map plus building and room layers. Interaction is reported up; state lives in the page. */
export default function MapView(props: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const loaded = useRef(false);
  const latest = useRef(props);
  latest.current = props;
  const hoverRoom = useRef<string | number | null>(null);

  useEffect(() => {
    const map = new maplibregl.Map({
      container: container.current!,
      style: STYLE,
      center: props.initialView?.center ?? [-98.5, 39.8],
      zoom: props.initialView?.zoom ?? 3.5,
      maxPitch: 70,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    let usedFallback = false;
    map.on("error", (e) => {
      if (!loaded.current && !usedFallback && !map.isStyleLoaded()) {
        usedFallback = true;
        console.warn("Basemap unavailable, using a plain background", e.error);
        map.setStyle(FALLBACK_STYLE);
      }
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new maplibregl.GeolocateControl({}), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    map.on("load", () => {
      loaded.current = true;
      map.addSource("buildings", { type: "geojson", data: EMPTY, promoteId: "id" });
      map.addSource("rooms", { type: "geojson", data: EMPTY, promoteId: "uid" });

      const status: maplibregl.ExpressionSpecification = [
        "case",
        [">", ["get", "placed_count"], 0],
        "#4f46e5",
        [">", ["get", "review_count"], 0],
        "#f59e0b",
        "#64748b",
      ];
      map.addLayer({
        id: "buildings-fill",
        type: "fill",
        source: "buildings",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: {
          "fill-color": status,
          "fill-opacity": [
            "case",
            ["boolean", ["feature-state", "selected"], false],
            0.06,
            ["boolean", ["feature-state", "hover"], false],
            0.35,
            0.2,
          ],
        },
      });
      map.addLayer({
        id: "buildings-line",
        type: "line",
        source: "buildings",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: {
          "line-color": status,
          "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3, 1.5],
        },
      });
      map.addLayer({
        id: "buildings-point",
        type: "circle",
        source: "buildings",
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-color": status,
          "circle-radius": 7,
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 2,
        },
      });

      map.addLayer({
        id: "rooms-fill",
        type: "fill",
        source: "rooms",
        filter: ["==", ["get", "feature"], "room"],
        paint: {
          "fill-color": ["match", ["get", "kind"], ...Object.entries(KIND_COLORS).flat(), KIND_COLORS.unknown] as never,
          "fill-opacity": [
            "case",
            ["boolean", ["feature-state", "selected"], false],
            0.95,
            ["boolean", ["feature-state", "hover"], false],
            0.85,
            0.6,
          ],
        },
      });
      map.addLayer({
        id: "rooms-line",
        type: "line",
        source: "rooms",
        filter: ["==", ["get", "feature"], "room"],
        paint: {
          "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#312e81", "#6366f1"],
          "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 2.5, 0.8],
        },
      });
      map.addLayer({
        id: "rooms-outline",
        type: "line",
        source: "rooms",
        filter: ["==", ["get", "feature"], "outline"],
        paint: { "line-color": "#1e1b4b", "line-width": 2.5 },
      });
      map.addLayer({
        id: "rooms-walls",
        type: "line",
        source: "rooms",
        filter: ["==", ["get", "feature"], "wall"],
        paint: { "line-color": "#1e293b", "line-width": 1.5 },
      });
      map.addLayer({
        id: "rooms-doors",
        type: "line",
        source: "rooms",
        filter: ["==", ["get", "feature"], "door"],
        paint: { "line-color": "#f8fafc", "line-width": 2 },
      });
      map.addLayer({
        id: "rooms-label",
        type: "symbol",
        source: "rooms",
        minzoom: 17.5,
        filter: ["all", ["==", ["get", "feature"], "room"], ["has", "label"]],
        layout: {
          "text-field": ["get", "label"],
          "text-size": 12,
          "text-font": ["Noto Sans Regular"],
          "text-max-width": 8,
        },
        paint: { "text-color": "#1e1b4b", "text-halo-color": "#fff", "text-halo-width": 1.2 },
      });
      map.addLayer({
        id: "buildings-label",
        type: "symbol",
        source: "buildings",
        minzoom: 14,
        layout: {
          "text-field": ["get", "name"],
          "text-size": 12,
          "text-font": ["Noto Sans Bold"],
          "text-offset": [0, 1.2],
          "text-optional": true,
        },
        paint: { "text-color": "#334155", "text-halo-color": "#fff", "text-halo-width": 1.5 },
      });

      syncData();
      latest.current.onReady?.(map);
    });

    let hoverBuilding: string | number | null = null;
    const setHover = (src: string, id: string | number | null, prev: string | number | null) => {
      if (prev !== null) map.setFeatureState({ source: src, id: prev }, { hover: false });
      if (id !== null) map.setFeatureState({ source: src, id }, { hover: true });
    };

    map.on("mousemove", "buildings-fill", (e: MapLayerMouseEvent) => {
      const id = e.features?.[0]?.id ?? null;
      setHover("buildings", id, hoverBuilding);
      hoverBuilding = id;
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", "buildings-fill", () => {
      setHover("buildings", null, hoverBuilding);
      hoverBuilding = null;
      map.getCanvas().style.cursor = "";
    });
    map.on("mousemove", "rooms-fill", (e: MapLayerMouseEvent) => {
      const id = e.features?.[0]?.id ?? null;
      setHover("rooms", id, hoverRoom.current);
      hoverRoom.current = id;
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", "rooms-fill", () => {
      setHover("rooms", null, hoverRoom.current);
      hoverRoom.current = null;
      map.getCanvas().style.cursor = "";
    });

    map.on("click", (e) => {
      const p = latest.current;
      if (p.picking) {
        p.onPick?.([e.lngLat.lng, e.lngLat.lat]);
        return;
      }
      const layers = ["rooms-fill", "buildings-fill", "buildings-point"].filter((l) => map.getLayer(l));
      const hits = map.queryRenderedFeatures(e.point, { layers });
      const room = hits.find((h) => h.layer.id === "rooms-fill");
      if (room && !p.hideRooms) {
        p.onSelectRoom({ feature: room as unknown as RoomClick["feature"], lngLat: [e.lngLat.lng, e.lngLat.lat] });
        return;
      }
      const b = hits.find((h) => h.layer.id.startsWith("buildings"));
      if (b) {
        p.onSelectRoom(null);
        p.onSelectBuilding(String(b.properties?.id ?? b.id));
        return;
      }
      p.onSelectRoom(null);
    });

    return () => {
      loaded.current = false;
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function syncData() {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    const p = latest.current;
    (map.getSource("buildings") as GeoJSONSource).setData(p.buildings ?? EMPTY);
    const rooms = p.rooms ?? EMPTY;
    (map.getSource("rooms") as GeoJSONSource).setData({
      ...rooms,
      features: rooms.features.map((f) => ({ ...f, properties: { ...f.properties, uid: String(f.id ?? "") } })),
    });
  }

  useEffect(syncData, [props.buildings, props.rooms]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    for (const l of ["rooms-fill", "rooms-line", "rooms-outline", "rooms-walls", "rooms-doors", "rooms-label"]) {
      if (map.getLayer(l)) map.setLayoutProperty(l, "visibility", props.hideRooms ? "none" : "visible");
    }
  }, [props.hideRooms]);

  // Selection highlight lives in feature-state so it survives data refreshes.
  const prevBuilding = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    if (prevBuilding.current) map.setFeatureState({ source: "buildings", id: prevBuilding.current }, { selected: false });
    if (props.selectedBuildingId) map.setFeatureState({ source: "buildings", id: props.selectedBuildingId }, { selected: true });
    prevBuilding.current = props.selectedBuildingId;
  }, [props.selectedBuildingId, props.buildings]);

  const prevRoom = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    if (prevRoom.current) map.setFeatureState({ source: "rooms", id: prevRoom.current }, { selected: false });
    if (props.selectedRoomUid) map.setFeatureState({ source: "rooms", id: props.selectedRoomUid }, { selected: true });
    prevRoom.current = props.selectedRoomUid;
  }, [props.selectedRoomUid, props.rooms]);

  useEffect(() => {
    const map = mapRef.current;
    if (map) map.getCanvas().style.cursor = props.picking ? "crosshair" : "";
  }, [props.picking]);

  return <div ref={container} className="absolute inset-0" />;
}
