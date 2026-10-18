// Bank exposure, market test and validation tabs. These read research outputs that may not be
// generated yet; each says exactly which script produces them instead of showing placeholders.
import { api, fmt, h, hbars, isNum, kpis, missingNote, note, panel, scatter, section, source, table } from "./core.js";

const TOP_BANKS = 15;
const WINDOWS = ["car_0_1", "car_0_5", "car_0_20"];
const winLabel = (w) => (w.startsWith("car_") ? `CAR [${w.slice(4).replace("_", ", +")}]` : w);
const head = (title, muted, lede) => h("header.view-head", h("h1", title, h("span.muted", ` · ${muted}`)), lede ? h("p.lede", lede) : null);

// ------------------------------------------------------------ bank exposure --
export async function renderBanks(state) {
  const q = new URLSearchParams();
  if (state.storm) q.set("storm", state.storm);
  if (state.definition) q.set("definition", state.definition);
  const d = await api(`/api/banks?${q}`);
  const header = head("Bank exposure", "every FDIC-insured bank, by storm",
    "exposure_pct = deposits in branches within the flood buffer ÷ the bank's national deposits (FDIC SOD vintage before the storm). " +
    "aoi_deposits_pct = deposits inside the storm's AOI box regardless of mapped flooding. strict uses only polygons published by the event time; full uses every pass.");
  if (!d.available) return [header, missingNote(d)];

  const body = h("div");
  const controls = h("div.controls",
    h("label", "Storm", h("select", { onchange: (e) => state.go({ storm: e.target.value }) },
      d.storms.map((s) => h("option", { value: s.key, selected: s.key === d.storm }, `${s.name} ${s.year}`)))),
    h("label", "Definition", h("select", { onchange: (e) => state.go({ definition: e.target.value }) },
      d.definitions.map((x) => h("option", { value: x, selected: x === d.definition }, x)))),
    h("label", "Min national deposits ($ bn)", h("input", { type: "number", min: 0, step: 0.5, value: 1, oninput: () => draw() })),
    h("label.check", h("input", { type: "checkbox", onchange: () => draw() }), "Public (ticker) banks only"),
    h("label", "Rank by", h("select", { onchange: () => draw() },
      ["exposure_pct", "aoi_deposits_pct", "flooded_deposits_k"].map((m) => h("option", { value: m }, m)))));
  const [, , minIn, tickerChk, measureSel] = controls.children;

  const draw = () => {
    const minDep = Math.max(0, Number(minIn.querySelector("input").value) || 0) * 1e6;
    const tickerOnly = tickerChk.querySelector("input").checked;
    const measure = measureSel.querySelector("select").value;
    const rows = d.rows.filter((r) => (r.total_deposits_k || 0) >= minDep && (!tickerOnly || r.ticker))
      .sort((a, b) => (b[measure] || 0) - (a[measure] || 0));
    const top = rows.slice(0, TOP_BANKS).filter((r) => (r[measure] || 0) > 0);
    body.replaceChildren(
      kpis([
        { label: "Banks shown", value: fmt.int(rows.length) },
        { label: "Banks with any flooded deposits", value: fmt.int(rows.filter((r) => r.flooded_deposits_k > 0).length) },
        { label: "Flooded deposits (sum)", value: fmt.bnFromK(rows.reduce((s, r) => s + (r.flooded_deposits_k || 0), 0), 2) },
        { label: "Banks with branches in AOI", value: fmt.int(rows.filter((r) => r.n_branches_in_aoi > 0).length) },
      ]),
      top.length ? panel(hbars(top.map((r) => ({ label: r.bank_name, value: r[measure], display: measure === "flooded_deposits_k" ? fmt.mFromK(r[measure]) : fmt.pct(r[measure], 2) })),
        { title: `Top banks by ${measure}` }))
        : note("info", `No bank has ${measure} > 0 for this storm and definition. With sparse SAR flood maps that is a real result, not a missing file.`),
      table(rows, [
        { key: "bank_name", label: "Bank" }, { key: "ticker", label: "Ticker" }, { key: "sod_year", label: "SOD", num: true },
        { key: "total_deposits_k", label: "Deposits", num: true, fmt: (v) => fmt.bnFromK(v, 2) },
        { key: "flooded_deposits_k", label: "Flooded deposits", num: true, fmt: fmt.mFromK },
        { key: "exposure_pct", label: "exposure %", num: true, fmt: (v) => fmt.num(v, 3) },
        { key: "aoi_deposits_pct", label: "AOI deposits %", num: true, fmt: (v) => fmt.num(v, 2) },
        { key: "n_flooded_branches", label: "Flooded branches", num: true }, { key: "n_branches_in_aoi", label: "Branches in AOI", num: true },
      ], { maxHeight: 440 }));
  };
  draw();
  const report = d.report && d.report.storms ? h("details.more-data", h("summary", "Per-storm join report"),
    table(d.report.storms, Object.keys(d.report.storms[0] || {}).map((k) => ({ key: k, label: k })))) : null;
  const tk = d.ticker_counts ? h("p.muted", `Ticker map: ${d.ticker_counts.active} active, ${d.ticker_counts.delisted} delisted holding companies. ` +
    "Delisted banks still get exposure numbers but have no price history, so the market test is survivorship-biased toward banks that still trade.") : null;
  return [header, controls, body, report, tk, source(d.source)];
}

// --------------------------------------------------------------- market test --
export async function renderMarket(state) {
  const d = await api("/api/market");
  const header = head("Market test", "does the market price satellite-measured flood exposure?");
  if (!d.available) return [header, missingNote(d)];
  const es = d.event_study || [];
  const nz = es.filter((r) => r.exposure_pct > 0).length;
  const measure = state.measure === "aoi_deposits_pct" ? "aoi_deposits_pct" : "exposure_pct";
  const win = WINDOWS.includes(state.window) ? state.window : "car_0_5";
  const storms = [...new Set(es.map((r) => r.storm))];
  const pts = es.filter((r) => isNum(r[measure]) && isNum(r[win])).map((r) => ({
    x: r[measure], y: r[win], group: r.storm, tip: `${r.ticker} · ${r.bank_name} · ${r.storm} · ${measure} ${fmt.num(r[measure], 2)} · ${winLabel(win)} ${fmt.frac(r[win], 2)}`,
  }));
  const bts = d.backtest_summary;
  return [
    header,
    d.caveats.length ? note("warn", h("b", "Read this before the numbers."), h("ul", d.caveats.map((c) => h("li", c)))) : null,
    kpis([
      { label: "Storms with prices", value: fmt.int(d.n_storms) },
      { label: "Bank × storm observations", value: fmt.int(es.length) },
      { label: "…with exposure_pct > 0", value: fmt.int(nz), tone: nz < 3 ? "amber" : null },
      { label: "Benchmark", value: d.benchmark },
    ]),
    nz < 3 ? note("info", "Almost no priced bank has non-zero satellite exposure, so the cross-section on exposure_pct has no variation to explain. This is a null result by construction, not evidence either way.") : null,
    section("Cross-section: CAR on exposure, storm fixed effects, SE clustered by storm",
      table(d.cross_section, [
        { key: "definition", label: "Definition" }, { key: "measure", label: "Measure" }, { key: "window", label: "Window", fmt: winLabel },
        { key: "coef_per_pct", label: "Coef / pct", num: true, fmt: fmt.sci }, { key: "t", label: "t", num: true, fmt: (v) => fmt.num(v, 2) },
        { key: "p", label: "p", num: true, fmt: (v) => fmt.num(v, 3) }, { key: "n", label: "N", num: true },
        { key: "n_storms", label: "Storms", num: true }, { key: "n_nonzero", label: "Non-zero", num: true }, { key: "note", label: "Note" },
      ], { empty: "Cross-section not generated." })),
    section(`Event study (market model vs ${d.benchmark}, estimation window ${JSON.stringify(d.estimation_window)} trading days)`,
      h("div.controls",
        h("div.seg", ["exposure_pct", "aoi_deposits_pct"].map((m) => h("button", { type: "button", role: "radio", "aria-checked": String(m === measure), onclick: () => state.go({ measure: m }) }, m))),
        h("div.seg", WINDOWS.map((w) => h("button", { type: "button", role: "radio", "aria-checked": String(w === win), onclick: () => state.go({ window: w }) }, winLabel(w))))),
      pts.length ? panel(scatter(pts, { xLabel: measure, yLabel: winLabel(win), yFmt: (v) => `${(v * 100).toFixed(1)}%`, groups: storms })) : note("info", "No observations for this window."),
      h("details.more-data", h("summary", "Event-study table"), table(es, [
        { key: "storm", label: "Storm" }, { key: "ticker", label: "Ticker" }, { key: "bank_name", label: "Bank" },
        { key: "exposure_pct", label: "exposure %", num: true, fmt: (v) => fmt.num(v, 3) },
        { key: "pre_car", label: "Pre-event CAR", num: true, fmt: (v) => fmt.frac(v, 2) },
        ...WINDOWS.map((w) => ({ key: w, label: winLabel(w), num: true, fmt: (v) => fmt.frac(v, 2) })),
      ], { maxHeight: 380 })),
      source(d.sources[1], "pre_car = abnormal return from the last close before landfall to day −1, i.e. what the market already knew")),
    section("Long-short backtest (short top exposure tercile, long bottom tercile or KRE, 10 bp per side)",
      bts ? h("div",
        kpis(Object.entries(bts.by_hold || {}).flatMap(([hold, r]) => [
          { label: `Hold ${hold} d · trades`, value: fmt.int(r.n_trades || 0) },
          { label: `Hold ${hold} d · cumulative P&L`, value: r.n_trades ? fmt.frac(r.cum_return, 2) : "—", tone: (r.cum_return || 0) < 0 ? "red" : "green" },
          { label: `Hold ${hold} d · hit rate`, value: r.n_trades ? fmt.frac(r.hit_rate, 0) : (r.note || "no trades") },
        ])),
        bts.skipped && bts.skipped.length ? h("p.muted", `Skipped storms (fewer than 3 exposed banks): ${bts.skipped.join(", ")}`) : null,
        d.backtest.length ? table(d.backtest, Object.keys(d.backtest[0]).map((k) => ({ key: k, label: k }))) : null)
        : note("info", "Backtest not generated. Run ", h("code", "scripts/run_market.py"), "."),
      source(d.sources[2])),
  ];
}

// ---------------------------------------------------------------- validation --
export async function renderValidation() {
  const d = await api("/api/validation");
  const header = head("Validation", "satellite flood map vs FEMA NFIP claims");
  if (!d.available) return [header, missingNote(d)];
  const vs = d.summary;
  const pooled = vs.pooled || {};
  const conf = pooled.confusion || {}, base = pooled.baseline_fema_declared || {};
  const sp = pooled.spearman_pct_vs_density || {};
  const tt = vs.threshold_tuning || {};
  const best = tt.best_on_train || {}, dflt = tt.default || {};
  const per = Object.entries(vs.per_storm || {}).map(([storm, r]) => ({
    storm, tracts: r.n_tracts, with_claims: r.n_tracts_with_claims, claims: r.n_claims,
    rho: (r.spearman_pct_vs_density || {}).r, auc: r.auc_pct_flooded, auc_decl: r.auc_fema_declared,
  }));
  return [
    header,
    h("p.muted", Object.entries(vs.definitions || {}).map(([k, v]) => `${k}: ${v}`).join(" · ")),
    h("p.muted", `Validated: ${(vs.storms_validated || []).join(", ")} · pending: ${(vs.storms_pending || []).join(", ") || "none"}`),
    ...(vs.notes || []).map((n) => h("p.muted", n)),
    kpis([
      { label: "Tracts scored (SAR-covered)", value: fmt.int(pooled.n_tracts) },
      { label: "Spearman ρ (flood % vs claims density)", value: fmt.num(sp.r, 3), sub: `p = ${fmt.num(sp.p, 2)}` },
      { label: "AUC · SAR flood %", value: fmt.num(pooled.auc_pct_flooded, 3), tone: "teal" },
      { label: "AUC · baseline (county declared)", value: fmt.num(pooled.auc_fema_declared, 3) },
    ]),
    table([
      { classifier: "SAR flooded tract (≥ 1 %)", ...conf },
      { classifier: "Baseline: county FEMA-declared", ...base },
    ], [
      { key: "classifier", label: "Classifier" }, ...["tp", "fp", "fn", "tn"].map((k) => ({ key: k, label: k.toUpperCase(), num: true, fmt: fmt.int })),
      { key: "precision", label: "Precision", num: true, fmt: (v) => fmt.num(v, 3) }, { key: "recall", label: "Recall", num: true, fmt: (v) => fmt.num(v, 3) },
    ]),
    isNum(pooled.auc_pct_flooded) && pooled.auc_pct_flooded < 0.55 ? note("warn", "At the current thresholds the SAR map barely ranks tracts by claims better than chance. " +
      "The first post-storm pass often comes before river crests and after surge recedes; this is the central limitation of the approach and is reported as such.") : null,
    per.length ? section("Per storm", table(per, [
      { key: "storm", label: "Storm" }, { key: "tracts", label: "Tracts", num: true, fmt: fmt.int }, { key: "with_claims", label: "With claims", num: true, fmt: fmt.int },
      { key: "claims", label: "Claims", num: true, fmt: fmt.int }, { key: "rho", label: "Spearman ρ", num: true, fmt: (v) => fmt.num(v, 3) },
      { key: "auc", label: "AUC SAR", num: true, fmt: (v) => fmt.num(v, 3) }, { key: "auc_decl", label: "AUC declared", num: true, fmt: (v) => fmt.num(v, 3) },
    ])) : null,
    section("Threshold tuning (grid searched on training storms only)",
      kpis([
        { label: "Training storms in grid", value: (tt.train_storms_in_grid || []).join(", ") || "—" },
        { label: "Best on train", value: `${best.diff_db ?? "—"} dB / ${best.post_db_max ?? "—"} dB`, sub: `score ${fmt.num(best.train_score, 3)}` },
        { label: "Default thresholds", value: `${dflt.diff_db ?? "—"} dB / ${dflt.post_db_max ?? "—"} dB`, sub: `score ${fmt.num(dflt.train_score, 3)}` },
      ]),
      d.threshold_grid.length ? table(d.threshold_grid, Object.keys(d.threshold_grid[0]).map((k) => ({ key: k, label: k, num: typeof d.threshold_grid[0][k] === "number" })), { maxHeight: 300 }) : null),
    d.tracts.length ? h("details.more-data", h("summary", `Tract-level table (top ${d.tracts.length} SAR-covered tracts by claims)`),
      table(d.tracts, Object.keys(d.tracts[0]).map((k) => ({ key: k, label: k, num: typeof d.tracts[0][k] === "number" })), { maxHeight: 360 })) : null,
    source(d.source),
  ];
}
