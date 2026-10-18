// Live tab: active NWS flood alerts, NHC storms, pre-event bank / Capital One exposure, satellite passes.
import { api, fmt, h, hbars, kpis, missingNote, note, panel, section, source, syntheticBanner, table } from "./core.js";
import { ALERT_COLORS, ALERT_OTHER, COLORS, createMap, legend, tipText } from "./map.js";

const TOP_BANK_BARS = 12;
const DEFAULT_CENTER_BBOX = [-82, 33.5, -74, 39.5];
const CONUS = [-125, 24, -66, 50]; // [min_lon, min_lat, max_lon, max_lat]

export async function render() {
  const [live, alerts] = await Promise.all([api("/api/live"), api("/api/live/alerts").catch(() => null)]);
  const s = live.summary;
  return [
    h("header.view-head", h("h1", "Live ", h("span.muted", "· active flood alerts and exposure")),
      h("p.lede", "Active NWS flood-type alerts and NHC storms, turned into pre-event exposure for every bank and for Capital One, plus when the next Sentinel-1 pass lands.")),
    s.available ? h("div", kpis([
      { label: "Alerts fetched", value: fmt.dt(s.alerts_fetched_utc), sub: "re-fetched when older than 15 min" },
      { label: "Active flood-type alerts (CONUS)", value: fmt.int(s.n_alerts_conus), tone: "teal" },
      { label: "Banks with branches under an alert", value: fmt.int(s.banks.n_banks_with_branches_in_alert) },
      { label: "Deposits under alert", value: fmt.bnFromK(s.banks.deposits_in_alert_k_total), tone: "amber" },
    ]), source(s.source)) : missingNote(s),
    h("div.split", mapBlock(alerts, live.nessie), sideBlock(alerts, live.nhc)),
    exposureBlock(live),
    satelliteBlock(live.satellite, s),
  ];
}

function mapBlock(alerts, nessie) {
  const feats = (alerts && alerts.features) || [];
  if (!alerts) return panel(note("info", "Alert polygons not generated yet. Run ", h("code", "scripts/run_live.py"), "."));
  if (!feats.length) return panel(note("info", "No active flood-type alerts with geometry right now."));
  const events = [...new Set(feats.map((f) => f.properties.event))];
  const el = h("div.map.map-tall");
  requestAnimationFrame(() => {
    try {
      const L = window.L;
      const { map, control } = createMap(el, null, { collapsed: true });
      const order = Object.keys(ALERT_COLORS);
      const rank = (ev) => (order.includes(ev) ? order.indexOf(ev) : order.length);
      const all = [];
      for (const ev of [...events].sort((a, b) => rank(a) - rank(b))) {
        const col = ALERT_COLORS[ev] || ALERT_OTHER;
        const lyr = L.geoJSON({ type: "FeatureCollection", features: feats.filter((f) => f.properties.event === ev) }, {
          style: { color: col, weight: 1.2, fillColor: col, fillOpacity: 0.3 },
          onEachFeature: (ft, l) => l.bindTooltip(tipText(ft.properties.event, ft.properties.areaDesc, ft.properties.ends ? `ends ${fmt.dt(ft.properties.ends)}` : "")),
        }).addTo(map);
        control.addOverlay(lyr, ev);
        all.push(lyr);
      }
      if (nessie.available) {
        control.addOverlay(L.layerGroup(nessie.locations.map((r) => L.circleMarker([r.lat, r.lon], {
          radius: 3, color: r.in_alert ? COLORS.exposed : COLORS.ok, fillOpacity: 0.9, weight: 1,
        }).bindTooltip(tipText(`Capital One ${r.kind}: ${r.name}`, r.in_alert ? "UNDER ALERT" : "clear")))).addTo(map), "Capital One locations");
      }
      // fit to CONUS alerts (the exposure scope); Alaska/territory alerts would zoom the map out to the globe
      const conus = all.flatMap((g) => g.getLayers()).filter((l) => {
        const c = l.getBounds().getCenter();
        return c.lng > CONUS[0] && c.lat > CONUS[1] && c.lng < CONUS[2] && c.lat < CONUS[3];
      });
      const bounds = L.featureGroup(conus.length ? conus : all).getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [12, 12] });
      else map.fitBounds([[DEFAULT_CENTER_BBOX[1], DEFAULT_CENTER_BBOX[0]], [DEFAULT_CENTER_BBOX[3], DEFAULT_CENTER_BBOX[2]]]);
    } catch (err) {
      el.replaceWith(note("error", `Map failed: ${err.message}`));
    }
  });
  return panel(el, legend([...events.map((ev) => [ALERT_COLORS[ev] || ALERT_OTHER, ev]),
    [COLORS.exposed, "Capital One under alert", "dot"], [COLORS.ok, "Capital One clear", "dot"]]),
  source("data/processed/live/alerts.geojson"));
}

function sideBlock(alerts, nhc) {
  const rows = ((alerts && alerts.features) || []).map((f) => f.properties).sort((a, b) => String(a.event).localeCompare(String(b.event)));
  return panel(
    h("h3", "Alerts"),
    table(rows, [
      { key: "event", label: "Event" }, { key: "severity", label: "Severity" }, { key: "areaDesc", label: "Area" },
      { key: "ends", label: "Ends", fmt: (v) => fmt.dt(v, false) },
    ], { maxHeight: 300, empty: "No active alerts." }),
    h("h3", "NHC active storms"),
    !nhc.available ? missingNote(nhc) : h("div",
      nhc.active_storms.length ? table(nhc.active_storms, [
        { key: "name", label: "Name" }, { key: "classification", label: "Class" }, { key: "intensity", label: "Intensity (kt)", num: true },
        { key: "basin", label: "Basin" }, { key: "lastUpdate", label: "Updated", fmt: (v) => fmt.dt(v, false) },
      ]) : h("p.muted", `No active NHC storms at ${fmt.dt(nhc.fetched_utc)}.`),
      nhc.active_storms.length && !nhc.atlantic.length ? h("p.muted", "No active Atlantic storm; the mid-Atlantic event is a non-tropical coastal flood (NWS alerts only).") : null,
      source(nhc.source)));
}

function exposureBlock(live) {
  const b = live.banks, n = live.nessie, p = live.portfolio;
  const banks = !b.available ? missingNote(b) : h("div",
    hbars(b.rows.slice(0, TOP_BANK_BARS).map((r) => ({ label: r.bank_name, value: r.deposits_in_alert_k / 1e6, display: fmt.bnFromK(r.deposits_in_alert_k, 2) })),
      { title: `Deposits in branches under an active flood alert (top ${Math.min(TOP_BANK_BARS, b.rows.length)} of ${fmt.int(b.n)} banks)` }),
    table(b.rows, [
      { key: "bank_name", label: "Bank" }, { key: "n_branches_in_alert", label: "Branches under alert", num: true, fmt: fmt.int },
      { key: "deposits_in_alert_k", label: "Deposits under alert", num: true, fmt: (v) => fmt.bnFromK(v, 2) },
      { key: "exposure_pct", label: "% of bank deposits", num: true, fmt: (v) => fmt.pct(v, 2) },
      { key: "alert_events", label: "Alert types" },
    ], { maxHeight: 320 }),
    source(b.source, "exposure_pct = deposits under alert ÷ the bank's national deposits"));
  const cap1 = !n.available ? missingNote(n) : h("div",
    kpis([{ label: "Locations", value: fmt.int(n.n) }, { label: "Under an alert", value: fmt.int(n.n_in_alert), tone: n.n_in_alert ? "red" : "green" }]),
    n.n_in_alert ? table(n.locations.filter((r) => r.in_alert), [
      { key: "kind", label: "Kind" }, { key: "name", label: "Name" }, { key: "city", label: "City" }, { key: "state", label: "State" }, { key: "alert_events", label: "Alerts" },
    ]) : h("p.muted", "All Capital One branches and ATMs are inland (Arlington / DC / Maryland suburbs); the coastal flood zones stop short of them. Honest result, not a bug."),
    source(n.source));
  const port = !p.available ? missingNote(p) : h("div", syntheticBanner(),
    kpis([{ label: "Customers under an alert", value: fmt.int(p.n_in_alert) }, { label: "Loan balance under alert", value: fmt.usd(p.loan_in_alert) }]),
    source(p.source));
  return section("Pre-event exposure under the current alerts",
    h("div.split", panel(h("h3", "Banks (FDIC Summary of Deposits branches inside alert polygons)"), banks),
      panel(h("h3", "Capital One (Nessie) locations"), cap1, h("h3", "Synthetic portfolio"), port)));
}

function satelliteBlock(sat, s) {
  if (!sat.available) return section("Satellite status", missingNote(sat));
  const refresh = (s.available && s.isaias_refresh) || {};
  return section("Satellite status",
    h("p.muted", `Checked ${fmt.dt(sat.checked_utc)}. Next expected pass = last pass + 12 days per relative orbit (Sentinel-1 repeat cycle).`),
    refresh.refresh_recommended ? note("warn", `A Sentinel-1 pass newer than the processed Isaias map is available (${(refresh.new_passes || []).length} new). Re-run: `, h("code", refresh.command || "scripts/run_storm.py --storm isaias"))
      : refresh.processed_through_utc ? note("ok", `Isaias flood map is current through ${fmt.dt(refresh.processed_through_utc)}; no newer pass published yet.`) : null,
    table(sat.orbits, [
      { key: "area", label: "Area" }, { key: "orbit", label: "Orbit", num: true }, { key: "direction", label: "Direction" }, { key: "platform", label: "Platform" },
      { key: "last_pass_utc", label: "Last pass", fmt: (v) => fmt.dt(v, false) }, { key: "next_expected_utc", label: "Next expected", fmt: (v) => fmt.dt(v, false) },
      { key: "coverage_pct", label: "Coverage", num: true, fmt: (v) => fmt.pct(v, 0) }, { key: "scenes_30d", label: "Scenes / 30 d", num: true },
    ], { maxHeight: 360 }),
    source(sat.source));
}
