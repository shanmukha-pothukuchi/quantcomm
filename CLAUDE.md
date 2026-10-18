# Quantcomm — Claude Code Build Prompt

> Paste this whole file into Claude Code at the start of the project (or save it as `CLAUDE.md` in the repo root so every teammate's session shares it). Then say: **"Read CLAUDE.md, verify the data sources, and build Milestone 1."**

---

## Mission

Build **Quantcomm**: a system that uses free satellite radar (Sentinel-1 SAR) to map flooding after US storms within days, measures how exposed each US bank is to the flooded area, stress-tests a simulated Capital One customer portfolio through the Capital One Nessie API, and tests whether the stock market underprices that exposure.

**Flagship case study: Hurricane Isaias (2026).** Isaias made landfall near Destin, Florida on the night of Friday, October 9, 2026 as a Category 2 hurricane (briefly Cat 3 beforehand), then weakened to post-tropical over Alabama. Storm surge plus river and flash flooding hit the Florida Panhandle, southern Alabama, and southwest Georgia.

**Live demo event:** a coastal storm forecast to form off the Carolinas around October 18, 2026, with coastal flood alerts from the Outer Banks through coastal Virginia and Maryland. Use it for the "live mode" demo if it's still active at demo time; otherwise use whatever event is active.

The pitch: *"Banks don't know their flood exposure for weeks after a storm. Free radar satellites can estimate it within days. We measure it for every US bank, stress-test a Capital One portfolio, and test whether the market prices it in."*

---

## Ground rules (read before writing code)

1. **Never fabricate data.** If an API, dataset, or download fails, stop and tell me what failed and what you tried. Do not substitute made-up numbers, placeholder CSVs, or "example" data without a loud `SYNTHETIC` label in the file name, the code, and the dashboard.
2. **Verify every external source before building on it.** Dataset IDs, API base URLs, and field names below are my best understanding and may be out of date. At the start, write `scripts/verify_sources.py` that hits each source, prints a sample record and its schema, and fails loudly on anything unexpected. Fix this spec (and tell me) where reality differs.
3. **No lookahead bias.** Every signal must use only data that was publicly available at the time of the trade. The "event time" for a storm is the **publication time of the first post-storm Sentinel-1 image that covers the affected area**, not landfall. Branch/deposit data must be the most recent FDIC Summary of Deposits release *before* the event.
4. **Cache everything.** Raw pulls go to `data/raw/` and processed tables to `data/processed/` as Parquet. Re-runs must not re-download. Earth Engine exports are slow; start them early and poll.
5. **Config, not constants.** Storm list, dates, AOIs, thresholds, and model parameters live in `config/*.yaml`. Secrets (Nessie key, etc.) in `.env`, never committed.
6. **Log assumptions.** Every modeling choice (flood threshold, PD uplift, LGD, buffer radius, event window) goes in `ASSUMPTIONS.md` with a one-line justification. Judges will ask.
7. **Test the joins.** Write pytest checks for every spatial and tabular join (row counts before/after, no silent drops, CRS consistency, no duplicate branches). Most wrong results come from bad joins, not bad models.
8. **Vertical slice first.** Milestone 1 must be a complete, demoable end-to-end pipeline for Isaias before anything is generalized.

---

## Architecture

```
quantcomm/
  config/
    storms.yaml          # storm list: name, landfall date, AOI bbox, notes
    model.yaml           # thresholds, PD/LGD params, event windows
  src/quantcomm/
    flood/               # Sentinel-1 change detection in Earth Engine
    banks/               # FDIC branches + deposits, ticker mapping
    nessie/              # Nessie client, synthetic portfolio generation, stress test
    census/              # ACS income by tract
    validation/          # OpenFEMA NFIP claims vs flood maps
    market/              # prices, event study, long-short backtest
    live/                # NHC / NWS alert ingestion for active events
    exposure.py          # flood mask x branches -> bank exposure scores
  web/                   # web app pages + assets (served by src/quantcomm/web, FastAPI)
  scripts/               # verify_sources.py, run_storm.py, run_all.py
  tests/
  data/{raw,processed}/
  ASSUMPTIONS.md
  README.md
```

Python 3.11+. Suggested libs: `earthengine-api`, `geemap`, `geopandas`, `shapely`, `rasterio`, `pandas`, `pyarrow`, `requests`, `yfinance`, `statsmodels`, `fastapi` + `uvicorn` (web app; Leaflet maps in the browser), `python-dotenv`, `pytest`.

---

## Data sources (verify each one first)

| Purpose | Source | Notes |
|---|---|---|
| Flood detection | **Verified 2026-10-17:** Microsoft Planetary Computer STAC `sentinel-1-rtc` (keyless, terrain-corrected gamma0 COGs, VV+VH) is the working backend; Copernicus Data Space OData catalog supplies `PublicationDate` for event time. GEE `COPERNICUS/S1_GRD` still needs `earthengine authenticate` + a Cloud project (not done). | Match pre/post images on the **same relative orbit and pass direction**. rioxarray `overview_level` is a 0-based index into the COG factors [2,4,8,…]. |
| Permanent water mask | Planetary Computer `jrc-gsw` asset `occurrence` (255 = ocean/no data) — GEE equivalent `JRC/GSW1_4/GlobalSurfaceWater` | Remove rivers/lakes/ocean that are always wet. |
| Terrain mask | Planetary Computer `nasadem` asset `elevation` (GEE equivalent `USGS/SRTMGL1_003`) | Mask steep slopes (radar shadow false positives). |
| Storm tracks / cones | NOAA NHC GIS products (best track, forecast cone shapefiles) | For AOIs and live mode. |
| Active flood alerts | `api.weather.gov/alerts/active` | Coastal Flood / Flash Flood / Flood Warning polygons for live mode (for events that aren't named NHC storms). |
| Capital One branches & ATMs | Nessie API `https://api.nessieisreal.com/branches`, `/atms` | **Verified:** only HTTPS works; GETs succeed with any key, POSTs need a real `NESSIE_API_KEY` (401 otherwise). 207 branches (MD/VA/DC only, no coordinates — geocoded via the Census batch geocoder), 13 ATMs with `geocode{lat,lng}`. |
| Simulated customers | Nessie API (`/customers`, `/accounts`, `/loans`, `/bills`, `/purchases`, `/deposits`, `/withdrawals`) | Mock data we create. Rate-limit and cache created IDs locally. |
| All US bank branches + deposits | FDIC BankFind API (`api.fdic.gov/banks/`, `locations` and `sod` endpoints) | Summary of Deposits gives deposits per branch (as of June 30 each year) with coordinates. |
| Bank → ticker | Build `config/bank_tickers.csv` mapping FDIC holding company / cert to ticker for the ~60 largest public US banks | Do this carefully and test it; this table drives the whole event study. |
| Income by area | ACS 2023 5-yr table-based summary file `acsdt5y2023-b19013.dat` + TIGER2024 tracts (keyless). `api.census.gov` now **requires a key** (`CENSUS_API_KEY`, optional). | For realistic synthetic customers. |
| Validation ground truth | OpenFEMA **v3 `NfipClaims`** (v2 `FimaNfipClaims` deprecated, frozen 2026-06-01; `censusTract` → `censusGeoid`, new `floodEvent`) + v2 `DisasterDeclarationsSummaries` (Isaias = EM-3655 FL / 3656 AL / 3657 GA) | Claims for Isaias won't be available yet; validate on historical storms. |
| Prices | `yfinance` (bank stocks, KRE, SPY, insurers) | Daily adjusted close. |

---

## Milestones

### Milestone 1 — Isaias vertical slice (target: first ~12 hours)

The whole story working for one storm, even if rough.

1. **Flood map for Isaias.**
   - AOI: Florida Panhandle, southern Alabama, southwest Georgia (put the bbox in `storms.yaml`).
   - Query all Sentinel-1 acquisitions over the AOI from ~3 weeks before to the present. Print them (date, orbit, pass direction, coverage %). Pick the best pre/post pair(s) on matching orbits; post-event coverage may be partial, so mosaic what exists and record the coverage.
   - Method: speckle filter (e.g., focal median), compute post/pre ratio (or difference in dB) on VV, threshold (start with a documented fixed threshold; add Otsu if time), mask permanent water and steep slopes, remove tiny clusters.
   - Output: flood mask GeoTIFF + vectorized polygons + a before/after preview PNG. Record the **image publication timestamp** for the event-time logic.
   - Sanity check: overlay with NWS warning polygons / news-reported flooded towns and print a short report. If the map is obviously wrong (e.g., flooding everywhere or nowhere), stop and tell me.
2. **Nessie exposure.** Pull Capital One branches and ATMs. Flag those inside the flood mask or within a configurable buffer (default 500 m). Note: Capital One's branch footprint is concentrated outside the Gulf Coast, so low direct exposure is a valid result — report it honestly.
3. **Synthetic Nessie portfolio.** Generate N customers (default 500) in the affected counties plus a control group outside, with incomes drawn from ACS tract medians, and create them in Nessie with checking/savings accounts, a loan, and recurring bills. Tag every created object with a project prefix in its nickname so we can clean up.
4. **Stress test.** For customers in flooded tracts: increase withdrawals, add missed bills, raise probability of default. Expected loss = EAD × PD × LGD, with parameters in `model.yaml` and justified in `ASSUMPTIONS.md`. Output loss for flooded vs. control groups.
5. **Minimal dashboard.** Streamlit page: before/after imagery toggle, flood polygons, branches/ATMs colored by exposure, stress test summary numbers.

**Stop after Milestone 1 and show me the results before continuing.**

### Milestone 2 — Every bank, every storm

1. Turn the Isaias pipeline into `run_storm.py --storm <name>` driven by `storms.yaml`.
2. Build the storm list: major US flood-producing hurricanes since Sentinel-1 coverage became reliable (~2017). Start with Harvey, Irma, Florence, Michael, Laura, Sally, Ida, Ian, Idalia, Helene, Milton, Isaias; fill in others from NHC data. Exclude storms where Sentinel-1 coverage of the AOI is poor and log why.
3. For each storm, compute **bank exposure** for every FDIC-insured bank: share of the bank's total deposits held in branches inside the flood mask (+ buffer). Use the SOD vintage before the storm.
4. Output a tidy table: `storm, bank, ticker, exposure_pct, flooded_deposits, event_time`.

### Milestone 3 — Market test

1. **Event study.** For each storm × public bank, compute abnormal returns with a market model (estimation window e.g. [-250, -30] trading days vs SPY or KRE). Cumulative abnormal returns over windows starting at event time: [0,+1], [0,+5], [0,+20]. Also report returns from landfall to event time to show how much the market already knew.
2. **Cross-section.** Regress CAR on exposure, with storm fixed effects; cluster standard errors by storm. Report coefficients, t-stats, N, and plots.
3. **Long-short backtest.** At each event time, short the top-exposure tercile, long the bottom tercile (or long KRE), hold H days. Show the cumulative P&L, hit rate, and per-storm returns. Include transaction cost assumptions.
4. **Honesty section.** Small sample (~a dozen storms), overlapping events, banks with near-zero exposure, multiple testing across windows. Print these caveats in the dashboard. A clean null result is fine and should be presented as such.

### Milestone 4 — Validation against ground truth

For historical storms, aggregate the flood mask to ZIP code or census tract (% area flooded) and compare with OpenFEMA NFIP claim counts/amounts for the same areas. Report correlation, a scatter plot, and a confusion matrix (flooded vs. claims above a threshold). Use this to tune the SAR threshold on **training storms only**, then report performance on held-out storms.

### Milestone 5 — Live mode

1. Ingest active NHC storms (cone + track) and active NWS coastal/flash flood warning polygons.
2. **Pre-event:** estimate expected exposure for each bank and for the Nessie portfolio inside the warning area / cone.
3. **Post-event:** when a new Sentinel-1 pass over the area becomes available, rerun flood mapping and update exposures automatically. Show "last satellite pass" and "next expected pass" in the UI.
4. Default target: the mid-Atlantic coastal storm (Outer Banks, coastal VA, MD) if active; otherwise any active event.

### Milestone 6 — Demo polish

Web app (FastAPI + static pages: `/` landing, `/stocks` Markets; replaced the original Streamlit app 2026-10-18). **2026-10-18: the `/dashboard` page was removed** — the items below now appear only as "Show research" popups behind each AI analysis on `/stocks`, when the analyst picks them. Original tab list:
- **Isaias case study** — before/after slider, flood map, Capital One branches/ATMs, stress test results.
- **Live** — current alerts and exposure.
- **Bank exposure** — sortable table of every public bank's exposure by storm.
- **Market test** — event study plots, regression table, long-short P&L, caveats.
- **Validation** — satellite vs. FEMA claims.
- **Methodology** — pulls from `ASSUMPTIONS.md`.

Also write a 1-page `README.md` with the pitch, architecture diagram, how to run, and results.

---

## Team parallelization

After Milestone 1, these streams can run in parallel sessions/worktrees with clear interfaces (Parquet schemas defined up front in `src/quantcomm/schemas.py`):

- **Flood** — Earth Engine pipeline, multi-storm runs, validation tuning.
- **Banks + market** — FDIC data, ticker mapping, event study, backtest.
- **Nessie** — client, synthetic portfolio, stress model, live exposure.
- **App** — dashboard, live mode UI, pitch materials.

One person owns **correctness**: reviews every join test, checks for lookahead, and signs off on every number before it goes in the pitch.

---

## First actions

1. Set up the repo skeleton, `pyproject.toml`, `.env.example`, and `config/` files.
2. Write and run `scripts/verify_sources.py`. Tell me which sources work, which need keys or auth (Earth Engine requires `earthengine authenticate` and a Cloud project), and any schema differences from this spec.
3. Ask me for any missing API keys.
4. Start Milestone 1.
