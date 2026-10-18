// "Show research" popup for the Markets page: the data behind one analyst's call, panel by panel.
// Panels come from /api/research/{ticker}?panels=...; big geometries load from the layer endpoints.
// Leaflet is loaded from cdnjs the first time a map is drawn.
const LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min";
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
const BASE_URL = `${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
const LABELS_URL = `${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`;
const TILE_ATTR = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";
const CONUS = [[24.5, -125], [49.5, -66.5]];
const C = Object.freeze({ flood: "#3fd6c6", exposed: "#f0565a", ok: "#22c38e", track: "#ecedef", aoi: "#ff8a4c", store: "#4ba3ff", alert: "#8b95a3" });
const ALERT_COLORS = Object.freeze({
  "Coastal Flood Warning": "#4ba3ff", "Coastal Flood Watch": "#3fd6c6", "Coastal Flood Advisory": "#e9b949",
  "Flood Warning": "#ff8a4c", "Flash Flood Warning": "#f0565a", "Storm Surge Warning": "#9b8cff",
});
const IMAGES = [["none", "Map only"], ["preview_pre.png", "Before storm"], ["preview_post.png", "After storm"], ["preview_change.png", "Change (flood)"]];

const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const pct = (v, d = 2, signed = false) => (v == null ? "—" : (signed && v > 0 ? "+" : "") + Number(v).toFixed(d) + "%");
function money(v) {
  if (v == null) return "—";
  const a = Math.abs(v), [k, u] = a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "K"] : [1, ""];
  return "$" + (v / k).toFixed(u ? 1 : 0) + u;
}
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" }) + " UTC" : "—");
const kpis = (items) => `<div class="rs-kpis">${items.map(([l, v]) => `<div class="rs-kpi"><span>${esc(l)}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
const missing = (m) => `<div class="rs-note">${esc(m.label)} has not been generated yet (expected <code>${esc(m.expected)}</code>)${m.hint ? `. Run <code>${esc(m.hint)}</code>.` : "."}</div>`;
const legend = (items) => `<div class="rs-legend">${items.map(([c, l, dot]) => `<span><i class="${dot ? "dot" : ""}" style="background:${c}"></i>${esc(l)}</span>`).join("")}</div>`;

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => null);
  if (!r.ok || !body || !body.success) throw new Error((body && body.error) || `Request failed (HTTP ${r.status})`);
  return body.data;
}

// ------------------------------------------------------------------ maps --
let leafletReady = null;
function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (leafletReady) return leafletReady;
  leafletReady = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = `${LEAFLET}.css`;
    document.head.appendChild(css);
    const js = document.createElement("script");
    js.src = `${LEAFLET}.js`;
    js.onload = () => resolve(window.L);
    js.onerror = () => { leafletReady = null; reject(new Error("Map library failed to load (check your connection).")); };
    document.head.appendChild(js);
  });
  return leafletReady;
}
async function makeMap(el, bounds) {
  const L = await loadLeaflet();
  const map = L.map(el, { scrollWheelZoom: false, preferCanvas: true, maxZoom: 16 });
  L.tileLayer(BASE_URL, { attribution: TILE_ATTR, maxZoom: 16 }).addTo(map);
  map.createPane("labels");
  Object.assign(map.getPane("labels").style, { zIndex: 650, pointerEvents: "none" });
  L.tileLayer(LABELS_URL, { pane: "labels", maxZoom: 16 }).addTo(map);
  map.fitBounds(bounds, { padding: [12, 12] });
  map.on("focus", () => map.scrollWheelZoom.enable());
  map.on("blur", () => map.scrollWheelZoom.disable());
  return { L, map };
}
const tip = (...parts) => esc(parts.filter((p) => p != null && p !== "").join(" · "));
function pointBounds(pts, fallback) {
  const ll = pts.filter((p) => p.lat != null && p.lon != null).map((p) => [p.lat, p.lon]);
  if (!ll.length) return fallback;
  const lat = ll.map((p) => p[0]), lon = ll.map((p) => p[1]);
  return [[Math.min(...lat) - 0.05, Math.min(...lon) - 0.05], [Math.max(...lat) + 0.05, Math.max(...lon) + 0.05]];
}
async function addAlerts(L, map) {
  const gj = await api("/api/live/alerts");
  if (!gj || !gj.features || !gj.features.length) return 0;
  L.geoJSON(gj, {
    style: (f) => { const c = ALERT_COLORS[f.properties.event] || C.alert; return { color: c, weight: 1, fillColor: c, fillOpacity: 0.18 }; },
    onEachFeature: (f, layer) => layer.bindTooltip(tip(f.properties.event, f.properties.areaDesc)),
  }).addTo(map);
  return gj.features.length;
}
function addLocations(L, map, locs, inside) {
  for (const p of locs) {
    if (p.lat == null || p.lon == null) continue;
    const hit = inside(p);
    L.circleMarker([p.lat, p.lon], { radius: p.kind === "atm" ? 6 : 4, color: hit ? C.exposed : C.ok, weight: p.kind === "atm" ? 2 : 1,
      fillColor: hit ? C.exposed : C.ok, fillOpacity: p.kind === "atm" ? 0.25 : 0.8 })
      .bindTooltip(tip(p.kind === "atm" ? "ATM" : "Branch", p.name, p.city, p.state, hit ? "inside alert / flood buffer" : ""))
      .addTo(map);
  }
}
function drawLater(el, fn) {
  requestAnimationFrame(() => fn().catch((e) => { el.outerHTML = `<div class="rs-note">Map failed: ${esc(e.message)}</div>`; }));
}

// --------------------------------------------------------------- panels --
function branchMap(p, { id }) {
  const n = p.nessie, a = p.alerts;
  if (!n.available) return missing(n);
  const atms = n.locations.filter((l) => l.kind === "atm").length;
  const html = kpis([["Branches + ATMs (Nessie)", `${n.n - atms} + ${atms}`], ["Inside a live flood alert", `${n.n_in_alert} of ${n.n}`],
    ["Live US flood alerts", a.available ? num(a.n_alerts_conus) : "—"], ["Alerts fetched", a.available ? when(a.generated_utc) : "—"]]) +
    `<div class="rs-map" id="${id}-map"></div>` +
    legend([[C.exposed, "Branch/ATM inside an alert", true], [C.ok, "Branch/ATM, no alert", true], ...Object.entries(ALERT_COLORS).map(([k, c]) => [c, k])]) +
    `<div class="rs-src">${esc(n.source)} · ring markers are ATMs</div>`;
  return { html, after: () => drawLater(document.getElementById(`${id}-map`), async () => {
    const { L, map } = await makeMap(document.getElementById(`${id}-map`), pointBounds(n.locations, CONUS));
    await addAlerts(L, map);
    addLocations(L, map, n.locations, (l) => l.in_alert);
  }) };
}

function customerStress(p) {
  const s = p.stress, lv = p.live;
  let html = `<p class="why">Generated customers, not real Capital One data <span class="rs-synth">SYNTHETIC</span></p>`;
  if (!s.available) html += missing(s);
  else {
    html += kpis([["Customers in the portfolio", num(s.n_customers)], ["Created in Nessie", num(s.n_pushed)],
      ["Baseline expected loss", money(s.baseline_expected_loss)], ["Extra loss from Isaias", money(s.incremental_expected_loss)]]);
    if (s.groups && s.groups.length) {
      html += `<div class="rs-tablewrap"><table class="rs-table"><thead><tr><th>Group</th><th class="n">Customers</th><th class="n">Exposure at default</th>
        <th class="n">Expected loss</th><th class="n">Loss rate</th><th class="n">Avg 12-mo PD</th></tr></thead><tbody>${s.groups.map((g) => `<tr>
        <td>${esc(g.group_label)}</td><td class="n">${num(g.n)}</td><td class="n">${money(g.ead)}</td><td class="n">${money(g.expected_loss)}</td>
        <td class="n">${pct(g.loss_rate_pct)}</td><td class="n">${pct(g.pd_12m * 100)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    html += `<div class="rs-src">${esc(s.source)} · expected loss = EAD × PD × LGD (config/model.yaml)</div>`;
  }
  if (lv.available) html += kpis([["Customers under live alerts", `${lv.n_in_alert} of ${lv.n}`], ["Loan balance under live alerts", money(lv.loan_in_alert)]]);
  else html += missing(lv);
  return html;
}

function floodMap(p, { id }) {
  const f = p.flood;
  if (!f.available) return missing(f);
  const imgs = IMAGES.filter(([k]) => k === "none" || (f.previews || []).includes(k));
  const html = kpis([["Landfall → first radar image", f.hours_landfall_to_event == null ? "—" : `${num(f.hours_landfall_to_event)} h`],
    ["Radar image published", when(f.event_time_utc)], ["Flooded area detected", `${num(f.flooded_km2, 1)} km²`],
    ["Storm area covered by radar", pct(f.coverage_pct, 0)]]) +
    `<div class="rs-seg" role="group" aria-label="Satellite image">${imgs.map(([k, l]) => `<button type="button" data-img="${k}" aria-pressed="${k === "preview_change.png"}">${esc(l)}</button>`).join("")}</div>` +
    `<div class="rs-map" id="${id}-map"></div>` +
    legend([[C.flood, "Detected flood"], [C.track, "NHC storm track"], [C.aoi, "Study area"],
      ...(p.nessie ? [[C.exposed, `Capital One ≤ ${p.nessie.buffer_m ?? 500} m from flood`, true], [C.ok, "Capital One, not exposed", true]] : [])]) +
    `<div class="rs-src">Sentinel-1 VV change (post − pre ≤ ${esc(f.diff_db_threshold ?? "−3")} dB), orbits ${esc((f.orbits || []).join(", "))} · ${esc(f.source)}</div>`;
  return { html, after: () => drawLater(document.getElementById(`${id}-map`), async () => {
    const el = document.getElementById(`${id}-map`), bb = f.aoi_bbox;
    const { L, map } = await makeMap(el, bb ? [[bb[1], bb[0]], [bb[3], bb[2]]] : CONUS);
    if (bb) L.rectangle([[bb[1], bb[0]], [bb[3], bb[2]]], { color: C.aoi, weight: 1, dashArray: "4", fill: false }).addTo(map);
    let overlay = null;
    const show = (file) => {
      if (overlay) map.removeLayer(overlay);
      overlay = file !== "none" && f.preview_bounds ? L.imageOverlay(`/media/${encodeURIComponent(p.storm)}/${file}`, f.preview_bounds, { opacity: 0.85 }).addTo(map) : null;
      if (overlay) overlay.bringToBack();
      el.parentElement.querySelectorAll("[data-img]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.img === file)));
    };
    el.parentElement.querySelectorAll("[data-img]").forEach((b) => b.addEventListener("click", () => show(b.dataset.img)));
    show(imgs.some(([k]) => k === "preview_change.png") ? "preview_change.png" : "none");
    const [poly, track] = await Promise.all([api(`/api/storms/${encodeURIComponent(p.storm)}/layers/polygons`), api(`/api/storms/${encodeURIComponent(p.storm)}/layers/track`)]);
    if (poly) L.geoJSON(poly, { style: { color: C.flood, weight: 1, fillColor: C.flood, fillOpacity: 0.6 } }).addTo(map);
    if (track) {
      const pts = track.features.filter((x) => x.geometry && x.geometry.type === "Point")
        .sort((a, b) => String(a.properties.dtg_utc || "").localeCompare(String(b.properties.dtg_utc || "")));
      if (pts.length > 1) L.polyline(pts.map((x) => [x.geometry.coordinates[1], x.geometry.coordinates[0]]), { color: C.track, weight: 2, opacity: 0.8 }).addTo(map);
    }
    if (p.nessie && p.nessie.available) addLocations(L, map, p.nessie.locations, (l) => l.exposed);
  }) };
}

function alertExposure(p, { ticker }) {
  const b = p.banks;
  if (!b.available) return missing(b);
  const mine = new Set(b.ticker_rows.map((r) => r.bank_name));
  const extra = b.ticker_rows.filter((r) => !b.rows.some((x) => x.bank_name === r.bank_name));
  const row = (r) => `<tr class="${mine.has(r.bank_name) ? "me" : ""}"><td>${esc(r.bank_name)}${r.ticker ? ` <span class="dim">${esc(r.ticker)}</span>` : ""}</td>
    <td class="n">${num(r.n_branches_in_alert)}</td><td class="n">${money(r.deposits_in_alert_k * 1e3)}</td><td class="n">${pct(r.exposure_pct)}</td>
    <td>${esc((r.alert_events || "").replace(/;/g, ", "))}</td></tr>`;
  const head = b.ticker_rows.length ? `${esc(ticker)} is highlighted.` : `${esc(ticker)} has no branch inside a live alert polygon.`;
  return `<p class="why">${b.n} banks have at least one branch inside a live NWS flood alert. ${head}</p>
    <div class="rs-tablewrap"><table class="rs-table"><thead><tr><th>Bank</th><th class="n">Branches under alert</th><th class="n">Deposits under alert</th>
    <th class="n">Share of bank's deposits</th><th>Alert types</th></tr></thead><tbody>${b.rows.map(row).join("")}${extra.map(row).join("")}</tbody></table></div>
    <div class="rs-src">${esc(b.source)} · FDIC Summary of Deposits branch deposits × NWS polygons${p.alerts.available ? `, alerts fetched ${esc(when(p.alerts.generated_utc))}` : ""}</div>`;
}

function alertsMap(p, { id }) {
  const a = p.alerts;
  if (!a.available) return missing(a);
  const storms = a.active_storms || [];
  const html = `<div class="rs-chips">${Object.entries(a.alerts_by_event).sort((x, y) => y[1] - x[1]).map(([k, v]) => `<span>${esc(k)}<b>${v}</b></span>`).join("")}</div>` +
    kpis([["Live US flood alerts", num(a.n_alerts_conus)], ["Active NHC storms", storms.length ? storms.map((s) => s.name).join(", ") : "none"], ["Fetched", when(a.generated_utc)]]) +
    `<div class="rs-map" id="${id}-map"></div>` + legend(Object.entries(ALERT_COLORS).map(([k, c]) => [c, k])) +
    `<div class="rs-src">${esc(a.source)} · api.weather.gov active alerts, NHC CurrentStorms</div>`;
  return { html, after: () => drawLater(document.getElementById(`${id}-map`), async () => {
    const { L, map } = await makeMap(document.getElementById(`${id}-map`), CONUS);
    await addAlerts(L, map);
  }) };
}

function lineChart(dates, series, H = 170) {
  const W = 900, M = { l: 44, r: 12, t: 10, b: 22 };
  const all = series.flatMap((s) => s.values.filter((v) => v != null));
  if (!all.length) return `<div class="rs-note">No readings yet.</div>`;
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all); const pad = (hi - lo) * 0.1 || 0.5; lo -= pad; hi += pad;
  const t0 = Date.parse(dates[0]), t1 = Date.parse(dates[dates.length - 1]) || t0 + 1;
  const X = (d) => M.l + (Date.parse(d) - t0) / Math.max(1, t1 - t0) * (W - M.l - M.r);
  const Y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
  const ticks = [lo + pad, 0, hi - pad].filter((v, i, a) => a.indexOf(v) === i);
  let g = ticks.map((v) => `<line x1="${M.l}" x2="${W - M.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v === 0 ? "#3a3f47" : "#23262c"}"/><text x="${M.l - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="#686b73">${v.toFixed(1)}σ</text>`).join("");
  g += `<text x="${M.l}" y="${H - 5}" font-size="11" fill="#686b73">${esc(dates[0])}</text><text x="${W - M.r}" y="${H - 5}" text-anchor="end" font-size="11" fill="#686b73">${esc(dates[dates.length - 1])}</text>`;
  for (const s of series) {
    const pts = s.dates.map((d, i) => [d, s.values[i]]).filter(([, v]) => v != null);
    if (pts.length > 1) g += `<path d="${pts.map(([d, v], i) => (i ? "L" : "M") + X(d).toFixed(1) + "," + Y(v).toFixed(1)).join("")}" fill="none" stroke="${s.color}" stroke-width="${s.w || 1.6}"/>`;
  }
  return `<svg class="rs-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Parking-lot activity over time">${g}</svg>`;
}

function parkingLot(p, { id }) {
  const sr = p.series;
  const tiles = (p.metrics || []).filter((m) => m.value != null).slice(0, 6).map((m) => [m.label,
    typeof m.value === "number" ? (m.unit === "σ" ? `${m.value.toFixed(2)} σ` : m.unit === "%" ? pct(m.value, 1) : num(m.value, Number.isInteger(m.value) ? 0 : 2)) : m.value]);
  let html = (p.verdict ? `<p class="why">Signal: <b>${esc(p.verdict.stance)}</b> · ${esc(p.verdict.reason)}</p>` : "") + kpis(tiles);
  if (sr) {
    const dates = [...sr.dates, ...sr.signal_dates].sort();
    html += lineChart([dates[0], dates[dates.length - 1]], [
      { dates: sr.dates, values: sr.values, color: "#9b8cff" }, { dates: sr.signal_dates, values: sr.signal_values, color: "#e9b949", w: 2 }]) +
      legend([["#9b8cff", "Activity index (5-evening mean)"], ["#e9b949", "Year-over-year signal"]]);
  }
  if (p.stores.length) html += `<div class="rs-map" id="${id}-map" style="margin-top:12px"></div>` + legend([[C.store, `${p.stores.length} tracked store parking lots`, true]]);
  html += `<div class="rs-src">${esc(p.source)}</div>` + (p.caveats.length ? `<ul class="rs-cav">${p.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : "");
  return { html, after: () => p.stores.length && drawLater(document.getElementById(`${id}-map`), async () => {
    const { L, map } = await makeMap(document.getElementById(`${id}-map`), pointBounds(p.stores, CONUS));
    for (const s of p.stores) {
      L.circleMarker([s.lat, s.lon], { radius: 5, color: C.store, fillColor: C.store, fillOpacity: 0.7, weight: 1 })
        .bindTooltip(tip(s.name, s.city, s.metro, s.lot_area_m2 ? `${num(s.lot_area_m2 / 1e3, 1)}k m² lot` : "")).addTo(map);
    }
  }) };
}

function stormHistory(p, { ticker }) {
  const rows = [...p.rows].sort((a, b) => a.year - b.year);
  if (!rows.length) return `<div class="rs-note">No landfall inside ${esc(ticker)}'s price history.</div>`;
  const W = 900, H = 190, M = { l: 44, r: 12, t: 14, b: 34 };
  const m = Math.max(1, ...rows.map((r) => Math.abs(r.excess_pct)));
  const bw = (W - M.l - M.r) / rows.length, Y = (v) => M.t + (1 - (v + m) / (2 * m)) * (H - M.t - M.b);
  let g = `<line x1="${M.l}" x2="${W - M.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="#3a3f47"/>
    <text x="${M.l - 6}" y="${Y(m) + 8}" text-anchor="end" font-size="11" fill="#686b73">+${m.toFixed(0)}%</text>
    <text x="${M.l - 6}" y="${Y(-m)}" text-anchor="end" font-size="11" fill="#686b73">−${m.toFixed(0)}%</text>`;
  rows.forEach((r, i) => {
    const y0 = Y(0), y1 = Y(r.excess_pct), x = M.l + i * bw;
    g += `<rect x="${x + bw * 0.18}" y="${Math.min(y0, y1)}" width="${bw * 0.64}" height="${Math.max(1, Math.abs(y1 - y0))}" rx="3" fill="${r.excess_pct >= 0 ? "#22c38e" : "#f0565a"}" opacity=".85"><title>${esc(r.storm)} ${r.year}: ${pct(r.excess_pct, 2, true)}</title></rect>
      <text x="${x + bw / 2}" y="${H - 18}" text-anchor="middle" font-size="11" fill="#9a9da5">${esc(r.storm)}</text>
      <text x="${x + bw / 2}" y="${H - 5}" text-anchor="middle" font-size="10" fill="#686b73">${r.year}</text>`;
  });
  const mean = rows.reduce((s, r) => s + r.excess_pct, 0) / rows.length, under = rows.filter((r) => r.excess_pct < 0).length;
  return kpis([["Average excess return", pct(mean, 2, true)], [`Lagged ${p.bench}`, `${under} of ${rows.length} storms`], ["Window", p.window]]) +
    `<svg class="rs-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Excess return after each past landfall">${g}</svg>
    <div class="rs-src">Log return of ${esc(ticker)} minus ${esc(p.bench)}; landfall times from config/storms.yaml, prices from yfinance.</div>`;
}

// Each renderer takes (panel data, {id, ticker}) and returns HTML, or {html, after} when a map draws after insertion.
const RENDER = {
  branch_map: branchMap, customer_stress: customerStress, flood_map: floodMap, alert_exposure: alertExposure,
  alerts_map: alertsMap, parking_lot: parkingLot, storm_history: stormHistory,
};

// ---------------------------------------------------------------- dialog --
function dialog() {
  let d = document.getElementById("research");
  if (d) return d;
  d = document.createElement("dialog");
  d.id = "research"; d.className = "research"; d.setAttribute("aria-labelledby", "rs-title");
  d.addEventListener("click", (ev) => { if (ev.target === d) d.close(); });  // click on the backdrop
  document.body.appendChild(d);
  return d;
}

/** Open the research popup. keys: panels the analyst chose (empty = every panel offered for this company). */
export async function openResearch({ ticker, name, keys, chosenBy }) {
  const d = dialog();
  d.innerHTML = `<div class="rs-head"><div class="t"><h2 id="rs-title">Research behind the call on ${esc(ticker)}</h2>
    <div class="sub">${esc(name)} · ${esc(chosenBy)}</div></div>
    <button type="button" class="rs-close" aria-label="Close research">×</button></div>
    <nav class="rs-tabs" id="rs-tabs"></nav><div class="rs-body" id="rs-body"><div class="rs-loading">Loading research…</div></div>`;
  d.querySelector(".rs-close").addEventListener("click", () => d.close());
  if (!d.open) d.showModal();
  const body = d.querySelector("#rs-body");
  let data;
  try {
    data = await api(`/api/research/${encodeURIComponent(ticker)}${keys.length ? `?panels=${keys.map(encodeURIComponent).join(",")}` : ""}`);
  } catch (e) {
    body.innerHTML = `<div class="rs-note">Could not load research: ${esc(e.message)}</div>`;
    return;
  }
  const entries = Object.entries(data.panels);
  if (!entries.length) { body.innerHTML = `<div class="rs-note">No research data for ${esc(ticker)}.</div>`; return; }
  const after = [];
  body.innerHTML = entries.map(([key, panel]) => {
    const id = `rs-${key}`, out = RENDER[key](panel, { id, ticker });
    const html = typeof out === "string" ? out : out.html;
    if (out.after) after.push(out.after);
    const shows = (data.options.find((o) => o.key === key) || {}).shows || "";
    return `<section class="rs-panel" id="${id}"><h3>${esc(panel.title)}</h3><p class="why">${esc(shows.charAt(0).toUpperCase() + shows.slice(1))}</p>${html}</section>`;
  }).join("");
  d.querySelector("#rs-tabs").innerHTML = entries.length > 1 ? entries.map(([key, panel]) => `<a href="#rs-${key}">${esc(panel.title)}</a>`).join("") : "";
  d.querySelectorAll("#rs-tabs a").forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    body.querySelector(a.getAttribute("href")).scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  after.forEach((fn) => fn());
}
