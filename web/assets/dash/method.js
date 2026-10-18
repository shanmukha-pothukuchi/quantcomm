// Methodology tab: pipeline diagram, how to reproduce, and ASSUMPTIONS.md rendered from the repo.
import { api, h, note, section, source } from "./core.js";

const PIPELINE = ` Sentinel-1 RTC COGs (Planetary Computer) ─┐
 Copernicus catalog: PublicationDate ───────┤ flood/catalog.py      scenes, orbits, event time (no lookahead)
 JRC surface water · NASADEM slope ─────────┤ flood/mpc_backend.py  post−pre dB change → flood mask, polygons, previews
 NWS warnings · NHC track · FEMA counties ──┘ flood/sanity.py       independent check of each map
                                              │
 FDIC Summary of Deposits (vintage < storm) ──┤ banks/fdic.py, banks/exposure.py   every bank: flooded deposits / total
 config/bank_tickers.csv · yfinance ──────────┤ market/event_study.py, backtest.py  CARs vs KRE, cross-section, long-short
 Census ACS income · TIGER tracts ────────────┤ census/acs.py, exposure.py          tract % flooded
 Nessie /branches /atms (+ Census geocoder) ──┤ nessie/client.py                    Capital One locations
 SYNTHETIC customers → Nessie sandbox ────────┤ nessie/portfolio.py, stress.py      EL = EAD × PD × LGD
 OpenFEMA NFIP claims v3 ─────────────────────┤ validation/nfip.py                  claims vs flood %, threshold tuning
 api.weather.gov alerts · NHC CurrentStorms ──┘ live/alerts.py, live/exposure.py    pre-event exposure, next pass
                                              ▼
                              quantcomm.web (this site: /, /dashboard, /stocks)`;

function markdown(md) {
  const div = h("div.prose");
  if (window.marked && window.DOMPurify) {
    div.innerHTML = window.DOMPurify.sanitize(window.marked.parse(md));
  } else {
    div.append(h("pre", md)); // libraries didn't load: show the raw text rather than nothing
  }
  return div;
}

export async function render() {
  const d = await api("/api/methodology");
  return [
    h("header.view-head", h("h1", "Methodology ", h("span.muted", "· how the numbers are made"))),
    section("Pipeline", h("pre.diagram", PIPELINE)),
    section("Reproduce",
      h("p", "Verify every source first: ", h("code", ".venv/bin/python scripts/verify_sources.py"),
        ". Then ", h("code", "scripts/run_all.py"), " → ", h("code", "run_banks.py"), " → ", h("code", "run_market.py"), " → ",
        h("code", "run_validation.py"), " → ", h("code", "run_live.py"), ". Raw pulls are cached under ", h("code", "data/raw/"),
        "; outputs under ", h("code", "data/processed/"), ". Serve this site with ", h("code", ".venv/bin/python -m quantcomm.web"), "."),
      d.storms_done.length ? h("p.muted", `Storms with a finished flood map right now: ${d.storms_done.join(", ")}.`) : null),
    section("Assumptions",
      d.assumptions_md ? markdown(d.assumptions_md) : note("info", "ASSUMPTIONS.md is missing."),
      source("ASSUMPTIONS.md")),
    h("details.more-data", h("summary", "config/model.yaml"), h("pre", JSON.stringify(d.model, null, 2))),
  ];
}
