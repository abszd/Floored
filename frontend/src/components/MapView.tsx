import { useEffect, useRef, useState } from "react";
import mapboxgl, { type GeoJSONSource, type Map as MBMap, type MapMouseEvent } from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import type { Feature, FeatureCollection, Geometry } from "geojson";

export const MAPBOX_TOKEN = (import.meta.env.VITE_MAPBOX_TOKEN as string | undefined) || "";
mapboxgl.accessToken = MAPBOX_TOKEN;

export type Basemap = "streets" | "satellite" | "dark";
export const FLOOR_HEIGHT_M = 4;

const STYLES: Record<Basemap, string> = {
  streets: "mapbox://styles/mapbox/standard",
  dark: "mapbox://styles/mapbox/standard",
  satellite: "mapbox://styles/mapbox/standard-satellite",
};

// Used without a Mapbox token, or when Mapbox can't be reached, so buildings
// and rooms still show on a plain background.
const FALLBACK_STYLE: mapboxgl.StyleSpecification = {
  version: 8,
  glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
  sources: {},
  layers: [{ id: "background", type: "background", paint: { "background-color": "#eef2f6" } }],
};

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

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

export interface Hover {
  kind: "room" | "building";
  x: number;
  y: number;
  props: Record<string, unknown>;
}

interface Props {
  buildings: FeatureCollection | undefined;
  /** Rooms of the floor being viewed. */
  rooms: FeatureCollection | undefined;
  /** Outlines of every floor of the selected building, for the 3D stack. */
  stack: FeatureCollection | undefined;
  /** Height above ground of the viewed floor, in metres (3D mode). */
  elevation: number;
  /** Height of the glass shell drawn around the floors below the viewed one (3D mode). */
  shellHeight: number;
  selectedBuildingId: string | null;
  selectedRoomUid: string | null;
  basemap: Basemap;
  threeD: boolean;
  hideRooms?: boolean;
  picking?: boolean;
  onSelectBuilding: (id: string | null) => void;
  onSelectRoom: (click: RoomClick | null) => void;
  onHover?: (h: Hover | null) => void;
  onPick?: (lngLat: [number, number]) => void;
  onReady?: (map: MBMap) => void;
}

const ROOM_LAYERS_2D = ["rooms-fill", "rooms-line", "rooms-outline", "rooms-walls", "rooms-doors", "rooms-label"];
const ROOM_LAYERS_3D = ["rooms-3d", "stack-3d", "shell-3d"];

/** The base map plus building and room layers. Interaction is reported up; state lives in the page. */
export default function MapView(props: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MBMap | null>(null);
  const [styleReady, setStyleReady] = useState(0);
  const latest = useRef(props);
  latest.current = props;
  const usingFallback = useRef(!MAPBOX_TOKEN);

  useEffect(() => {
    const map = new mapboxgl.Map({
      container: container.current!,
      style: usingFallback.current ? FALLBACK_STYLE : STYLES[props.basemap],
      center: [-98.5, 39.8],
      zoom: 3.5,
      maxPitch: 75,
      attributionControl: false,
      config: props.basemap === "dark" ? { basemap: { lightPreset: "night" } } : undefined,
    });
    mapRef.current = map;
    map.addControl(new mapboxgl.AttributionControl({ compact: true }), "bottom-left");
    map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new mapboxgl.GeolocateControl({}), "bottom-right");
    map.addControl(new mapboxgl.ScaleControl({ unit: "metric" }), "bottom-left");

    map.on("error", (e) => {
      if (!usingFallback.current && !map.isStyleLoaded()) {
        usingFallback.current = true;
        console.warn("Mapbox basemap unavailable, using a plain background", e.error);
        map.setStyle(FALLBACK_STYLE);
      }
    });

    // Our layers have to be re-added whenever the style is replaced.
    map.on("style.load", () => {
      if (!usingFallback.current && latest.current.basemap === "dark") {
        map.setConfigProperty("basemap", "lightPreset", "night");
      }
      addLayers(map, usingFallback.current);
      setStyleReady((n) => n + 1);
      latest.current.onReady?.(map);
    });

    let hoverBuilding: string | number | null = null;
    let hoverRoom: string | number | null = null;
    const setHover = (src: string, id: string | number | null, prev: string | number | null) => {
      if (prev !== null && map.getSource(src)) map.setFeatureState({ source: src, id: prev }, { hover: false });
      if (id !== null && map.getSource(src)) map.setFeatureState({ source: src, id }, { hover: true });
    };

    map.on("mousemove", (e: MapMouseEvent) => {
      const p = latest.current;
      if (p.picking) return;
      const layers = ["rooms-fill", "rooms-3d", "buildings-fill", "buildings-point"].filter((l) => map.getLayer(l));
      const hits = layers.length ? map.queryRenderedFeatures(e.point, { layers }) : [];
      const room = p.hideRooms ? undefined : hits.find((h) => h.layer?.id === "rooms-fill" || h.layer?.id === "rooms-3d");
      const building = room ? undefined : hits.find((h) => h.layer?.id.startsWith("buildings"));
      const roomId = room?.id ?? null;
      const buildingId = building?.id ?? null;
      if (roomId !== hoverRoom) setHover("rooms", roomId, hoverRoom);
      if (buildingId !== hoverBuilding) setHover("buildings", buildingId, hoverBuilding);
      hoverRoom = roomId;
      hoverBuilding = buildingId;
      map.getCanvas().style.cursor = room || building ? "pointer" : "";
      if (room) p.onHover?.({ kind: "room", x: e.point.x, y: e.point.y, props: room.properties ?? {} });
      else if (building && building.properties?.id !== p.selectedBuildingId)
        p.onHover?.({ kind: "building", x: e.point.x, y: e.point.y, props: building.properties ?? {} });
      else p.onHover?.(null);
    });
    map.on("mouseout", () => latest.current.onHover?.(null));

    map.on("click", (e) => {
      const p = latest.current;
      if (p.picking) {
        p.onPick?.([e.lngLat.lng, e.lngLat.lat]);
        return;
      }
      const layers = ["rooms-fill", "rooms-3d", "buildings-fill", "buildings-point"].filter((l) => map.getLayer(l));
      const hits = map.queryRenderedFeatures(e.point, { layers });
      const room = hits.find((h) => h.layer?.id === "rooms-fill" || h.layer?.id === "rooms-3d");
      if (room && !p.hideRooms) {
        p.onSelectRoom({ feature: room as unknown as RoomClick["feature"], lngLat: [e.lngLat.lng, e.lngLat.lat] });
        return;
      }
      const b = hits.find((h) => h.layer?.id.startsWith("buildings"));
      p.onSelectRoom(null);
      if (b) p.onSelectBuilding(String(b.properties?.id ?? b.id));
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Basemap switching: light presets change in place; satellite needs a new style.
  const currentStyle = useRef(props.basemap === "satellite" ? "satellite" : "standard");
  useEffect(() => {
    const map = mapRef.current;
    if (!map || usingFallback.current) return;
    const want = props.basemap === "satellite" ? "satellite" : "standard";
    if (want !== currentStyle.current) {
      currentStyle.current = want;
      map.setStyle(STYLES[props.basemap]);
      return;
    }
    if (want === "standard" && map.isStyleLoaded()) {
      map.setConfigProperty("basemap", "lightPreset", props.basemap === "dark" ? "night" : "day");
    }
  }, [props.basemap]);

  // Push data into sources whenever it (or the style) changes.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;
    const p = latest.current;
    (map.getSource("buildings") as GeoJSONSource | undefined)?.setData(p.buildings ?? EMPTY);
    const rooms = p.rooms ?? EMPTY;
    (map.getSource("rooms") as GeoJSONSource | undefined)?.setData({
      ...rooms,
      features: rooms.features.map((f) => ({
        ...f,
        properties: { ...f.properties, uid: String(f.id ?? ""), base: p.elevation, top: p.elevation + 0.35 },
      })),
    });
    (map.getSource("stack") as GeoJSONSource | undefined)?.setData(p.stack ?? EMPTY);
  }, [styleReady, props.buildings, props.rooms, props.stack, props.elevation]);

  // 2D/3D and editor visibility.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;
    const show = (ids: string[], on: boolean) =>
      ids.forEach((l) => map.getLayer(l) && map.setLayoutProperty(l, "visibility", on ? "visible" : "none"));
    show(ROOM_LAYERS_2D, !props.hideRooms && !props.threeD);
    show(ROOM_LAYERS_3D, !props.hideRooms && props.threeD);
    if (map.getLayer("shell-3d")) {
      map.setFilter("shell-3d", ["==", ["get", "id"], props.selectedBuildingId ?? ""]);
      map.setPaintProperty("shell-3d", "fill-extrusion-height", props.shellHeight);
    }
    // Mapbox's own 3D buildings would hide the floor we're looking at.
    if (!usingFallback.current && currentStyle.current === "standard") {
      try {
        map.setConfigProperty("basemap", "show3dObjects", !props.selectedBuildingId);
      } catch {
        /* style without that option */
      }
    }
    map.easeTo({ pitch: props.threeD ? 55 : 0, duration: 600 });
  }, [styleReady, props.threeD, props.hideRooms, props.selectedBuildingId, props.shellHeight]);

  // Selection highlight lives in feature-state so it survives data refreshes.
  const prevBuilding = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady || !map.getSource("buildings")) return;
    if (prevBuilding.current) map.setFeatureState({ source: "buildings", id: prevBuilding.current }, { selected: false });
    if (props.selectedBuildingId) map.setFeatureState({ source: "buildings", id: props.selectedBuildingId }, { selected: true });
    prevBuilding.current = props.selectedBuildingId;
  }, [styleReady, props.selectedBuildingId, props.buildings]);

  const prevRoom = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady || !map.getSource("rooms")) return;
    if (prevRoom.current) map.setFeatureState({ source: "rooms", id: prevRoom.current }, { selected: false });
    if (props.selectedRoomUid) map.setFeatureState({ source: "rooms", id: props.selectedRoomUid }, { selected: true });
    prevRoom.current = props.selectedRoomUid;
  }, [styleReady, props.selectedRoomUid, props.rooms]);

  useEffect(() => {
    const map = mapRef.current;
    if (map) map.getCanvas().style.cursor = props.picking ? "crosshair" : "";
  }, [props.picking]);

  return <div ref={container} className="absolute inset-0" />;
}

function addLayers(map: MBMap, fallback: boolean) {
  // Standard styles place custom layers in "slots"; the fallback style has none.
  const slot = (s: "middle" | "top") => (fallback ? {} : { slot: s });
  map.addSource("buildings", { type: "geojson", data: EMPTY, promoteId: "id" });
  map.addSource("rooms", { type: "geojson", data: EMPTY, promoteId: "uid" });
  map.addSource("stack", { type: "geojson", data: EMPTY });

  const status: mapboxgl.ExpressionSpecification = [
    "case",
    [">", ["get", "placed_count"], 0],
    "#4f46e5",
    [">", ["get", "review_count"], 0],
    "#f59e0b",
    "#64748b",
  ];
  const selected = ["boolean", ["feature-state", "selected"], false];
  const hover = ["boolean", ["feature-state", "hover"], false];
  const kindColor = ["match", ["get", "kind"], ...Object.entries(KIND_COLORS).flat(), KIND_COLORS.unknown];

  map.addLayer({
    id: "buildings-fill",
    type: "fill",
    source: "buildings",
    ...slot("middle"),
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: {
      "fill-color": status,
      "fill-opacity": ["case", selected, 0.04, hover, 0.4, 0.22] as never,
      "fill-emissive-strength": 1,
    },
  });
  map.addLayer({
    id: "buildings-line",
    type: "line",
    source: "buildings",
    ...slot("middle"),
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: {
      "line-color": status,
      "line-width": ["case", selected, 3, hover, 2.5, 1.5] as never,
      "line-emissive-strength": 1,
    },
  });
  map.addLayer({
    id: "buildings-point",
    type: "circle",
    source: "buildings",
    ...slot("middle"),
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-color": status,
      "circle-radius": ["case", hover, 9, 7] as never,
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 2,
      "circle-emissive-strength": 1,
    },
  });

  // ---- 2D floor
  map.addLayer({
    id: "rooms-fill",
    type: "fill",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "room"],
    paint: {
      "fill-color": kindColor as never,
      "fill-opacity": ["case", selected, 0.95, hover, 0.85, 0.65] as never,
      "fill-emissive-strength": 1,
    },
  });
  map.addLayer({
    id: "rooms-line",
    type: "line",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "room"],
    paint: {
      "line-color": ["case", selected, "#312e81", "#6366f1"] as never,
      "line-width": ["case", selected, 2.5, 0.8] as never,
      "line-emissive-strength": 1,
    },
  });
  map.addLayer({
    id: "rooms-outline",
    type: "line",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "outline"],
    paint: { "line-color": "#1e1b4b", "line-width": 2.5, "line-emissive-strength": 1 },
  });
  map.addLayer({
    id: "rooms-walls",
    type: "line",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "wall"],
    paint: { "line-color": "#1e293b", "line-width": 1.5, "line-emissive-strength": 1 },
  });
  map.addLayer({
    id: "rooms-doors",
    type: "line",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "door"],
    paint: { "line-color": "#f8fafc", "line-width": 2, "line-emissive-strength": 1 },
  });

  // ---- 3D: a glass shell for the building, faint slabs for other floors, the viewed floor in colour.
  map.addLayer({
    id: "shell-3d",
    type: "fill-extrusion",
    source: "buildings",
    ...slot("middle"),
    filter: ["==", ["get", "id"], ""],
    layout: { visibility: "none" },
    paint: { "fill-extrusion-color": "#6366f1", "fill-extrusion-opacity": 0.08, "fill-extrusion-height": 0 },
  });
  map.addLayer({
    id: "stack-3d",
    type: "fill-extrusion",
    source: "stack",
    ...slot("middle"),
    layout: { visibility: "none" },
    paint: {
      "fill-extrusion-color": "#94a3b8",
      "fill-extrusion-base": ["get", "base"],
      "fill-extrusion-height": ["get", "top"],
      "fill-extrusion-opacity": 0.25,
    },
  });
  map.addLayer({
    id: "rooms-3d",
    type: "fill-extrusion",
    source: "rooms",
    ...slot("middle"),
    filter: ["==", ["get", "feature"], "room"],
    layout: { visibility: "none" },
    paint: {
      "fill-extrusion-color": ["case", selected, "#4f46e5", hover, "#818cf8", kindColor] as never,
      "fill-extrusion-base": ["get", "base"],
      "fill-extrusion-height": ["get", "top"],
      "fill-extrusion-opacity": 0.92,
      "fill-extrusion-emissive-strength": 0.8,
    },
  });

  map.addLayer({
    id: "rooms-label",
    type: "symbol",
    source: "rooms",
    ...slot("top"),
    minzoom: 17.5,
    filter: ["all", ["==", ["get", "feature"], "room"], ["has", "label"]],
    layout: {
      "text-field": ["get", "label"],
      "text-size": 12,
      "text-font": fallback ? ["Noto Sans Regular"] : ["DIN Pro Medium", "Arial Unicode MS Regular"],
      "text-max-width": 8,
    },
    paint: { "text-color": "#1e1b4b", "text-halo-color": "#fff", "text-halo-width": 1.2 },
  });
  map.addLayer({
    id: "buildings-label",
    type: "symbol",
    source: "buildings",
    ...slot("top"),
    minzoom: 14,
    layout: {
      "text-field": ["get", "name"],
      "text-size": 12,
      "text-font": fallback ? ["Noto Sans Bold"] : ["DIN Pro Bold", "Arial Unicode MS Bold"],
      "text-offset": [0, 1.2],
      "text-optional": true,
    },
    paint: { "text-color": "#334155", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });
}
