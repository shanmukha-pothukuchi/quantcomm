// Leaflet helpers (Leaflet is loaded globally from cdnjs in dashboard.html).
import { h } from "./core.js";

export const COLORS = Object.freeze({
  flood: "#3fd6c6",
  cover: "#8b95a3",
  warn: "#9b8cff",
  track: "#f3f5f7",
  exposed: "#ff5d6c",
  ok: "#5be38f",
  aoi: "#ff8a4c",
});

export const ALERT_COLORS = Object.freeze({
  "Coastal Flood Warning": "#4ba3ff",
  "Coastal Flood Watch": "#3fd6c6",
  "Coastal Flood Advisory": "#e9b949",
  "Flood Warning": "#ff8a4c",
  "Flash Flood Warning": "#ff5d6c",
  "Storm Surge Warning": "#9b8cff",
});
export const ALERT_OTHER = "#8b95a3";

// Esri Dark Gray Canvas: keyless with attribution. Labels are a separate layer drawn above our data.
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
const BASE_URL = `${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
const LABELS_URL = `${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`;
const TILE_ATTR = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";
const MAX_ZOOM = 16;

/** @returns {{map:any, control:any}} */
export function createMap(el, bbox, { collapsed = window.innerWidth < 760 } = {}) {
  if (typeof window.L === "undefined") throw new Error("Map library failed to load (check your connection).");
  const L = window.L;
  const map = L.map(el, { zoomControl: true, scrollWheelZoom: false, preferCanvas: true, maxZoom: MAX_ZOOM });
  L.tileLayer(BASE_URL, { attribution: TILE_ATTR, maxZoom: MAX_ZOOM }).addTo(map);
  map.createPane("labels");
  map.getPane("labels").style.zIndex = 650;
  map.getPane("labels").style.pointerEvents = "none";
  L.tileLayer(LABELS_URL, { pane: "labels", maxZoom: MAX_ZOOM }).addTo(map);
  if (bbox) map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [10, 10] });
  const control = L.control.layers(null, null, { collapsed }).addTo(map);
  map.on("focus", () => map.scrollWheelZoom.enable());
  map.on("blur", () => map.scrollWheelZoom.disable());
  return { map, control };
}

/** NHC best track: point fixes joined into a line in time order, each fix a small dot. */
export function trackLayer(geojson) {
  const L = window.L;
  const pts = (geojson.features || []).filter((f) => f.geometry && f.geometry.type === "Point")
    .sort((a, b) => String(a.properties.dtg_utc || "").localeCompare(String(b.properties.dtg_utc || "")));
  const group = L.featureGroup();
  if (pts.length > 1) L.polyline(pts.map((f) => [f.geometry.coordinates[1], f.geometry.coordinates[0]]), { color: COLORS.track, weight: 2, opacity: 0.85 }).addTo(group);
  for (const f of pts) {
    const p = f.properties;
    L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], { radius: 3, color: COLORS.track, fillOpacity: 1, weight: 1 })
      .bindTooltip(tipText(p.STORMNAME, p.dtg_utc ? new Date(p.dtg_utc).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "", p.INTENSITY ? `${p.INTENSITY} kt` : "", p.STORMTYPE))
      .addTo(group);
  }
  const lines = (geojson.features || []).filter((f) => f.geometry && f.geometry.type !== "Point");
  if (lines.length) L.geoJSON({ type: "FeatureCollection", features: lines }, { style: { color: COLORS.track, weight: 2 } }).addTo(group);
  return group;
}

/** Escape text for Leaflet tooltips, which take HTML strings. */
export function tipText(...parts) {
  const div = document.createElement("div");
  div.textContent = parts.filter((p) => p !== null && p !== undefined && p !== "").join(" · ");
  return div.innerHTML;
}

export function legend(items) {
  return h("div.map-legend", items.map(([color, label, shape]) =>
    h("span", h(`i${shape === "dot" ? ".dot" : shape === "line" ? ".line" : ""}`, { style: { background: color, borderColor: color } }), label)));
}
