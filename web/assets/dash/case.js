// Storm case study tab: Sentinel-1 flood map, passes, NWS sanity check, Capital One exposure,
// SYNTHETIC stress test and the satellite catalog for one storm (default Isaias 2026).
import { api, fmt, h, hbars, kpis, missingNote, note, panel, section, source, syntheticBanner, table } from "./core.js";
import { COLORS, createMap, legend, tipText, trackLayer } from "./map.js";

const BACKDROPS = [["none", "None"], ["preview_pre.png", "Pre-storm"], ["preview_post.png", "Post-storm"], ["preview_change.png", "Change (flood)"]];
const TOP_COUNTIES = 15;

export async function render(state) {
  const storms = await api("/api/storms");
  const done = storms.filter((s) => s.done);
  const key = state.storm && storms.some((s) => s.key === state.storm) ? state.storm
    : (done.find((s) => s.key === "isaias") || done[0] || storms[0]).key;
  const data = await api(`/api/storms/${encodeURIComponent(key)}`);

  const picker = h("div.storm-picker", { role: "tablist", "aria-label": "Storm" }, storms.map((s) =>
    h("button", {
      type: "button", role: "tab", "aria-selected": String(s.key === key), disabled: !s.done,
      title: s.done ? null : "Flood map not generated yet",
      onclick: () => state.go({ storm: s.key }),
    }, `${s.name} ${s.year}`)));

  return [
    picker,
    h("header.view-head", h("h1", `${data.name} ${data.year}`, h("span.muted", " · Sentinel-1 flood map")),
      data.notes ? h("p.lede", data.notes) : null),
    floodSummary(data.flood),
    mapBlock(data),
    passesBlock(data.flood),
    nwsBlock(data.nws),
    nessieBlock(data.nessie),
    stressBlock(data.stress),
    catalogBlock(data.catalog),
  ];
}

function floodSummary(f) {
  if (!f.available) return missingNote(f);
  return h("div",
    kpis([
      { label: "Landfall (NHC)", value: fmt.dt(f.landfall_utc) },
      { label: "Event time = first post-storm SAR published", value: fmt.dt(f.event_time_utc), tone: "teal" },
      { label: "Landfall → SAR publication", value: f.hours_landfall_to_event === null ? "—" : `${fmt.num(f.hours_landfall_to_event, 0)} h` },
      { label: "Flooded area", value: `${fmt.num(f.flooded_km2, 1)} km²`, tone: "teal" },
      { label: "Post-storm SAR coverage of AOI", value: fmt.pct(f.coverage_pct, 0) },
      { label: "Passes used (orbits)", value: `${f.n_passes} (${(f.orbits || []).join(", ")})` },
      { label: "Mosaic complete", value: fmt.dt(f.mosaic_complete_utc) },
      { label: "Resolution", value: f.resolution_m ? `${f.resolution_m} m` : "—" },
    ]),
    source(f.source, "event time is the Copernicus publication time of the first post-landfall pass; nothing published later feeds the strict signal"));
}

function mapBlock(data) {
  const f = data.flood.available ? data.flood : {};
  const bbox = f.aoi_bbox || data.aoi_bbox;
  const el = h("div.map", { id: "case-map" });
  const seg = h("div.seg", { role: "radiogroup", "aria-label": "Radar backdrop" });
  const wrap = section("Flood map", seg, el, legend([
    [COLORS.flood, "Flood polygons"], [COLORS.cover, "SAR coverage per pass", "line"], [COLORS.warn, "NWS flood warnings", "line"],
    [COLORS.track, "NHC best track", "line"], [COLORS.aoi, "Area of interest", "line"],
    [COLORS.exposed, `Capital One ≤ ${data.nessie.buffer_m ?? 500} m from flood`, "dot"], [COLORS.ok, "Capital One, not exposed", "dot"],
  ]));
  requestAnimationFrame(() => drawMap(el, seg, data, bbox, f).catch((err) => el.replaceWith(note("error", `Map failed: ${err.message}`))));
  return wrap;
}

async function drawMap(el, seg, data, bbox, f) {
  const L = window.L;
  const { map, control } = createMap(el, bbox);
  if (bbox) L.rectangle([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { color: COLORS.aoi, weight: 1, dashArray: "4", fill: false }).addTo(map);

  let overlay = null;
  const setBackdrop = (file) => {
    if (overlay) map.removeLayer(overlay);
    overlay = null;
    if (file !== "none" && f.preview_bounds && (f.previews || []).includes(file)) {
      overlay = L.imageOverlay(`/media/${encodeURIComponent(data.key)}/${file}`, f.preview_bounds, { opacity: 0.85 }).addTo(map);
      overlay.bringToBack();
    }
    for (const b of seg.children) b.setAttribute("aria-checked", String(b.dataset.file === file));
  };
  for (const [file, label] of BACKDROPS) {
    const available = file === "none" || (f.previews || []).includes(file);
    seg.append(h("button", { type: "button", role: "radio", "data-file": file, disabled: !available, onclick: () => setBackdrop(file) }, label));
  }
  setBackdrop((f.previews || []).includes("preview_change.png") ? "preview_change.png" : "none");

  const layer = async (name) => {
    try { return await api(`/api/storms/${encodeURIComponent(data.key)}/layers/${name}`); } catch (_) { return null; }
  };
  const [polys, cover, track] = await Promise.all([layer("polygons"), layer("coverage"), layer("track")]);
  if (cover) control.addOverlay(L.geoJSON(cover, { style: { color: COLORS.cover, weight: 2, fill: false, dashArray: "6 3" } }).addTo(map), "SAR coverage");
  if (polys) control.addOverlay(L.geoJSON(polys, {
    style: { color: COLORS.flood, weight: 0.5, fillColor: COLORS.flood, fillOpacity: 0.6 },
    onEachFeature: (ft, lyr) => lyr.bindTooltip(tipText(ft.properties.pass_id, ft.properties.area_m2 ? `${fmt.int(ft.properties.area_m2)} m²` : "")),
  }).addTo(map), "Flood polygons");
  if (data.nws.available && data.nws.warnings_geojson) control.addOverlay(L.geoJSON(data.nws.warnings_geojson, {
    style: { color: COLORS.warn, weight: 1.5, fill: false },
    onEachFeature: (ft, lyr) => lyr.bindTooltip(tipText(ft.properties.event, ft.properties.areaDesc || ft.properties.area)),
  }).addTo(map), "NWS warnings");
  if (track) control.addOverlay(trackLayer(track).addTo(map), "NHC best track");
  if (data.nessie.available) {
    const g = L.layerGroup(data.nessie.locations.map((r) => L.circleMarker([r.lat, r.lon], {
      radius: r.kind === "atm" ? 4 : 5, color: r.exposed ? COLORS.exposed : COLORS.ok, fillOpacity: 0.9, weight: 1,
    }).bindTooltip(tipText(`${r.kind}: ${r.name}`, r.exposed ? "EXPOSED" : "not exposed")))).addTo(map);
    control.addOverlay(g, "Capital One branches / ATMs");
  }
}

function passesBlock(f) {
  if (!f.available || !f.passes.length) return null;
  const sens = f.sensitivity.length ? panel(
    h("h3", "Threshold sensitivity"),
    h("p.muted", "km² before the cluster filter, summed over frames. Thresholds are tuned on training storms only (Validation tab), never on the storm shown."),
    table(f.sensitivity, [
      { key: "threshold_db", label: "Change threshold (dB)", num: true },
      { key: "km2_darker", label: "km² darker", num: true, fmt: (v) => fmt.num(v, 1) },
      { key: "km2_darker_and_post_dark", label: "…and post below floor", num: true, fmt: (v) => fmt.num(v, 1) },
      { key: "used", label: "Used", fmt: (v) => (v ? "✓" : "") },
    ])) : null;
  const diag = (f.previews || []).includes("diagnostic_diff.png") ? panel(
    h("img.diag", { src: `/media/${encodeURIComponent(f.storm_key)}/diagnostic_diff.png`, loading: "lazy",
      alt: "Post minus pre VV difference and pixels darker than −1.5 dB, with NWS warnings and the NHC track" }),
    h("p.muted", "post − pre VV difference (left) and pixels darker than −1.5 dB (right), with NWS warning polygons (red) and the NHC track (black)")) : null;
  return section("Sentinel-1 passes used",
    h("p.muted", "Each pass keeps its own publication time."),
    table(f.passes, [
      { key: "pass", label: "Pass" }, { key: "orbit", label: "Orbit", num: true }, { key: "direction", label: "Direction" },
      { key: "acquired_utc", label: "Acquired", fmt: (v) => fmt.dt(v, false) },
      { key: "published_utc", label: "Published", fmt: (v) => fmt.dt(v, false) },
      { key: "hours_after_landfall", label: "h after landfall", num: true, fmt: (v) => fmt.num(v, 0) },
      { key: "frames", label: "Frames", num: true }, { key: "coverage_pct", label: "AOI cov.", num: true, fmt: (v) => fmt.pct(v, 0) },
      { key: "flooded_km2", label: "Flooded km²", num: true, fmt: (v) => fmt.num(v, 2) }, { key: "polygons", label: "Polygons", num: true },
    ]),
    source(f.source), sens, diag);
}

function nwsBlock(n) {
  if (!n.available) return section("Sanity check against NWS warnings and FEMA declarations", missingNote(n));
  const top = n.counties.slice(0, TOP_COUNTIES);
  return section("Sanity check against NWS warnings and FEMA declarations",
    kpis([
      { label: "NWS Flood / Flash Flood Warnings", value: fmt.int(n.n_warnings) },
      { label: "Flood area inside warning polygons", value: fmt.pct(n.pct_flood_area_inside_warnings, 0) },
      { label: "Warning area detected as flooded", value: fmt.pct(n.pct_warning_area_flooded, 2) },
      { label: "Verdict", value: String(n.verdict || "—").split(":")[0] },
    ]),
    n.warnings_available === false || !n.n_warnings ? h("p.muted", "api.weather.gov keeps only recent alert history, so historical storms have no warning polygons here.") : null,
    top.length ? h("div.two-col",
      panel(hbars(top.map((c) => ({ label: c.county, value: c.flooded_km2, display: `${fmt.num(c.flooded_km2, 2)} km²`, tone: c.fema_declared ? "amber" : "teal" })),
        { title: `Flooded area by county (top ${top.length}) · amber = FEMA-declared` })),
      panel(table(n.counties, [
        { key: "county", label: "County" }, { key: "flooded_km2", label: "Flooded km²", num: true, fmt: (v) => fmt.num(v, 2) },
        { key: "fema_declared", label: "FEMA declared", fmt: fmt.bool },
      ], { maxHeight: 340 }))) : null,
    source(n.source));
}

function nessieBlock(n) {
  if (!n.available) return section("Capital One branches and ATMs (Nessie)", missingNote(n));
  return section("Capital One branches and ATMs (Nessie)",
    kpis([
      { label: "Locations pulled from Nessie", value: fmt.int(n.n) },
      { label: "Inside this storm's AOI", value: fmt.int(n.n_in_aoi) },
      { label: `Exposed (≤ ${n.buffer_m} m from flood)`, value: fmt.int(n.n_exposed), tone: n.n_exposed ? "red" : "green" },
    ]),
    n.n_exposed === 0 ? note("ok", "Zero direct exposure. Capital One's Nessie branch and ATM footprint is entirely in Maryland, Virginia and DC, " +
      "so Gulf and Atlantic storms do not touch it. The Live tab shows the mid-Atlantic coastal flood alerts that do; the synthetic portfolio below models customer-level exposure.") : null,
    h("details.more-data", h("summary", "Location table"), table(n.locations, [
      { key: "kind", label: "Kind" }, { key: "name", label: "Name" }, { key: "city", label: "City" }, { key: "state", label: "State" },
      { key: "in_aoi", label: "In AOI", fmt: fmt.bool }, { key: "sar_covered", label: "SAR covered", fmt: fmt.bool },
      { key: "dist_to_flood_m", label: "Distance to flood (m)", num: true, fmt: (v) => fmt.int(v) },
      { key: "exposed", label: "Exposed", fmt: fmt.bool },
    ], { maxHeight: 360 })),
    source(n.source));
}

function stressBlock(s) {
  const title = "Synthetic Capital One portfolio stress test";
  if (!s.available) return section(title, missingNote(s));
  const groups = s.groups || [];
  const fl = groups.find((g) => g.group_label === "Flooded tract");
  const nf = groups.find((g) => g.group_label === "Not flooded");
  return section(title,
    syntheticBanner(),
    h("p.muted", `${fmt.int(s.n_customers)} synthetic customers generated; ${fmt.int(s.n_pushed)} created in Nessie (tagged ${s.nickname_prefix}).`),
    kpis([
      { label: "Customers in flooded tracts", value: fmt.int(fl ? fl.n : 0) },
      { label: "Expected loss · flooded", value: fl ? fmt.usd(fl.expected_loss) : "—", sub: fl ? `${fmt.num(fl.loss_rate_pct, 2)} % of EAD` : null },
      { label: "Expected loss · not flooded", value: nf ? fmt.usd(nf.expected_loss) : "—", sub: nf ? `${fmt.num(nf.loss_rate_pct, 2)} % of EAD` : null },
      { label: "Incremental loss from flooding", value: fmt.usd(s.incremental_expected_loss), tone: "teal" },
    ]),
    !fl ? note("info", "No synthetic customer sits in a tract with ≥ 1 % mapped flooding, so the stress uplift is not triggered and incremental expected loss is $0. That is the model's honest output for this map.") : null,
    groups.length ? table(groups, [
      { key: "group_label", label: "Group" }, { key: "n", label: "n", num: true, fmt: fmt.int },
      { key: "ead", label: "EAD", num: true, fmt: fmt.usd }, { key: "expected_loss", label: "Expected loss", num: true, fmt: fmt.usd },
      { key: "loss_rate_pct", label: "Loss rate", num: true, fmt: (v) => fmt.pct(v, 2) },
      { key: "pd_12m", label: "Mean PD (12m)", num: true, fmt: (v) => fmt.num(v, 3) },
      { key: "withdrawal", label: "Withdrawal shock", num: true, fmt: fmt.usd },
      { key: "missed", label: "Missed-bill rate", num: true, fmt: (v) => fmt.num(v, 2) },
    ]) : null,
    h("details.more-data", h("summary", "Model parameters (config/model.yaml)"),
      table(Object.entries(s.params || {}).map(([parameter, value]) => ({ parameter, value: JSON.stringify(value) })),
        [{ key: "parameter", label: "Parameter" }, { key: "value", label: "Value" }])),
    source(s.source));
}

function catalogBlock(c) {
  if (!c.available) return null;
  return h("details.more-data.block", h("summary", `Sentinel-1 acquisitions over the AOI (${c.rows.length})`),
    table(c.rows, [
      { key: "scene_id", label: "Scene" }, { key: "acquired_utc", label: "Acquired", fmt: (v) => fmt.dt(v, false) },
      { key: "published_utc", label: "Published", fmt: (v) => fmt.dt(v, false) }, { key: "platform", label: "Platform" },
      { key: "relative_orbit", label: "Orbit", num: true }, { key: "pass_direction", label: "Direction" },
      { key: "aoi_coverage_pct", label: "AOI cov.", num: true, fmt: (v) => fmt.pct(v, 1) },
      { key: "is_post_landfall", label: "Post-landfall", fmt: fmt.bool },
    ], { maxHeight: 400 }),
    source(c.source));
}
