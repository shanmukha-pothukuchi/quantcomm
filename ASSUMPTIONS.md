# Quantcomm — Assumptions log

Every modeling choice, with a one-line justification. Parameters live in `config/model.yaml`.


## Synthetic portfolio and Nessie stress test (SYNTHETIC — no real customers)

All of the following are modeling assumptions chosen for plausibility, not calibrated facts.

| Parameter | Value | Justification |
|---|---|---|
| Customer locations | Random point inside a sampled census tract | Tracts sampled uniformly; "affected" = tract in a FEMA-declared county (EM-3655/3656/3657) **and** inside the storm AOI bbox, "control" = tract in FL/AL/GA outside any declared county. The AOI filter prevents metro Atlanta (declared under EM-3657) from dominating the affected group. |
| Affected share | 60% of 500 | Enough flooded-tract customers to measure an effect; control group large enough for a comparison. |
| Income | Lognormal around the tract's ACS 2023 5-yr median household income, sigma 0.5 | Within-tract income dispersion is roughly lognormal; sigma 0.5 gives a 10th–90th percentile spread of ~0.5x–1.9x the median. |
| Checking balance | ~1 month of income (lognormal, sigma 0.6) | Order-of-magnitude for transactional balances. |
| Savings balance | ~2 months of income (lognormal, sigma 0.9) | Wide dispersion reflects that many households hold little savings. |
| Loan amount (EAD) | ~6 months of income (lognormal, sigma 0.5), 9.5%, 48 months | Auto-loan-sized unsecured/secured consumer loan; EAD = outstanding amount. |
| Monthly bills | 35% of monthly income (±30%) | Typical housing + utilities + insurance share. |
| Baseline PD (12-month) | 2.5% | Roughly the 2–3% annualized default rate on unsecured US consumer credit in recent years (assumption, not a Capital One figure). |
| Flooded PD multiplier | 3x | Post-disaster delinquency studies report large but short-lived increases in missed payments in flooded areas; 3x is an order-of-magnitude assumption, not an estimate from Isaias data. |
| Flooded tract threshold | >= 1% of tract area in the SAR flood mask, and tract covered by the post-storm SAR mosaic | Avoids calling a tract flooded on a handful of noisy pixels; tracts without SAR coverage are explicitly *not* flooded (unknown ≠ flooded). |
| LGD | 75% | Typical range for unsecured consumer credit; applied uniformly. |
| Withdrawal shock | 25% of checking in 30 days post-event for flooded customers | Emergency spending and relocation; assumption. |
| Missed-bill probability | 30% flooded vs 3% control | Assumption consistent with the PD multiplier direction. |
| Expected loss | EAD x PD x LGD per customer, summed | Standard expected-credit-loss identity; no discounting over the 90-day horizon. |

Nessie specifics: request bodies for POST /customers, /accounts, /loans, /bills were verified on 2026-10-17 by pushing the full SYNTHETIC portfolio (500 customers, 1,000 accounts, 500 loans, 1,500 bills) with a real key; /deposits, /withdrawals, /purchases bodies follow the same documented shape but have not been exercised yet (no flooded customers to stress). Every Nessie object we create is tagged `FLOODALPHA_` and logged to `data/raw/nessie/created_ids.jsonl` for cleanup.

Capital One branch coordinates: Nessie `/branches` records carry street addresses but no coordinates; 193 were geocoded with the Census Bureau batch geocoder and 14 (suite numbers, PO boxes, mall names) with Nominatim, of which 4 fell back to ZIP-code centroids (`data/raw/nessie/branch_geocodes_nominatim.csv`). ATMs carry Nessie-provided coordinates.

## Flood mapping (Sentinel-1 change detection)

| Choice | Value | Justification |
|---|---|---|
| Imagery | Sentinel-1 IW GRD, VV, radiometrically terrain-corrected gamma0 from Microsoft Planetary Computer `sentinel-1-rtc` | Same scenes as GEE `COPERNICUS/S1_GRD` but already terrain-corrected and keyless; GEE needs interactive auth we did not have. Switch via `flood.backend`. |
| Pre/post pairing | Same relative orbit and pass direction; post = first post-landfall pass; pre = median of the last 2 passes on that orbit | Same geometry removes incidence-angle differences; the median of two pre passes suppresses transient dark areas (wet soil, wind-still water). |
| Event time | Copernicus catalog `PublicationDate` of the last post-storm frame used in the mosaic | The map could not have existed before the data was public; this is the no-lookahead timestamp for the market test. For Isaias: 2026-10-11T05:38Z, 28 h after landfall. |
| Working resolution | 40 m (COG overview, 4x of native 10 m) | Keeps a 3-frame mosaic in memory on a laptop; a Capital One branch or a census tract is much larger than 40 m. |
| Speckle filter | 3x3 focal median on linear backscatter | Standard first-order despeckle (UN-SPIDER recommended practice uses a 3x3 smoothing). |
| Change rule | post − pre < −3 dB **and** post < −15 dB | New open water is dark (< −15 dB in VV) and must be darker than before; −3 dB is a halving of backscatter, which is above the ~1.5 dB speckle noise of a filtered 40 m pixel. Stricter than UN-SPIDER's ratio 1.25 (~ −1 dB); we prefer false negatives to false positives for a bank-exposure signal. Sensitivity at −1/−1.5/−2/−4 dB is logged in `flood_meta.json` for each run and must be tuned only on training storms (Milestone 4). |
| Permanent water | JRC Global Surface Water occurrence ≥ 50% or ocean (value 255) masked | Rivers, lakes, bays and the Gulf are always dark; their change is tide/wind, not flooding. |
| Terrain | NASADEM slope > 5° masked | Radar shadow/layover on slopes mimics dark water (UN-SPIDER threshold). |
| Minimum cluster | 8 connected pixels (1.3 ha at 40 m) | Removes isolated speckle hits (UN-SPIDER uses 8 px). |
| Coverage | Mosaic only what the first post-storm pass covers; tracts/points outside are flagged `sar_covered = False`, never treated as dry | Partial coverage is the norm (Isaias: 52% of the AOI bbox on orbit 19). Unknown ≠ dry. |
| Buffer | Branch/ATM counted as exposed within 500 m of a flood polygon | Access and basement/ground-floor damage extend past the mapped water edge; 500 m is a stated, configurable default. |
| Timing caveat | Isaias post image is 22 h after landfall | Surge had largely receded; the map shows standing water at that instant, not peak inundation. Later passes (orbit 121 on 2026-10-17, orbit 165 on 2026-10-21) can be added via `s1_relative_orbits`. |

## Multi-storm runs (Milestone 2)

| Choice | Value | Justification |
|---|---|---|
| Storm list | Harvey, Irma, Florence, Michael, Laura, Sally, Ida, Ian, Idalia, Helene, Milton, Isaias | Major US flood-producing landfalls since Sentinel-1A/B gave reliable 12-day CONUS coverage (2017+). |
| Landfall time | NHC Tropical Cyclone Report landfall, cross-checked against the first best-track point over CONUS land (`flood/nhc.py`) | Best-track points are 6-hourly, so the TCR value is more precise; the two agree within one step except where a storm crossed Florida first (Sally, Ian). |
| AOI | Analyst-chosen bbox around the main surge + rain footprint (see `storms.yaml` notes) | The NHC 34-kt wind swath ∩ land exceeds 500,000 km² for Helene and Irma, which is not tractable; branches outside the box get exposure 0 for that storm. This is a stated limitation, not a data result. |
| Passes used | Every relative orbit whose first post-landfall pass falls within 10 days; pre = median of the last 2 passes on the same orbit | More orbits = more coverage; 10 days keeps the map tied to the storm rather than later rain. Each pass keeps its own publication time. |
| Event time (`strict`) | Publication time of the first post-landfall pass | Nothing from later passes could have been traded on at that time. |
| Mosaic complete (`full`) | Publication time of the last pass used | Upper bound on information; the market test reports both definitions. |
| Resolution | 80 m for the multi-storm runs, 40 m for the Isaias case study | Compute budget for 12 storms; tract- and branch-level exposure is insensitive at this scale. The threshold sensitivity table is logged per frame to check comparability. |
| Michael caveat | First post pass is 6 h after landfall, while the storm was still over the AOI | Rain cells and wind-roughened water affect VV; later orbits within the window add cleaner views. |

## Banks and market test

| Choice | Value | Justification |
|---|---|---|
| Branch universe | FDIC Summary of Deposits, all US branches, one vintage per storm | Only public source with deposits per branch and coordinates. |
| SOD vintage (no lookahead) | Event year − 1, unless the event is on/after Oct 15 (SOD is as of June 30 and released late Sept/early Oct); Isaias uses 2026 because the 2026 index was verified built 2026-09-18 | The June-30 snapshot must have been public before the trade. Harvey/Irma → 2016, Florence/Michael → 2017, Laura/Sally → 2019, Ida → 2020, Ian → 2021, Idalia → 2022, Helene/Milton → 2023. |
| exposure_pct | deposits in branches within 500 m of a SAR flood polygon ÷ bank's total US deposits | Direct, branch-level measure; small by construction because SAR masks are sparse. |
| aoi_deposits_pct | deposits in the storm AOI bbox ÷ total deposits | Flood-agnostic alternative that captures regional concentration; reported alongside. |
| strict vs full | strict = polygons published by the first post-landfall pass (event time); full = all passes within 10 days | Strict is tradable at event time; full is the upper bound and would need a later trade date. |
| Ticker map | `config/bank_tickers.csv`, 87 active + 25 delisted holding companies; rssdhcr-first, then largest-deposit name match | Several unrelated small HCs share names (e.g. "Commerce Bancshares"); taking the largest avoids mis-assignment. Verified against 2024 SOD and yfinance 2024 prices; reused tickers (STI → Solidion, LION → Lionsgate) are flagged by a company/quote name check. |
| Survivorship | Delisted banks (SunTrust, IBERIABANK, CenterState, BancorpSouth, Comerica, Synovus, Cadence, Veritex, IBTX, Stellar, ...) keep their exposure rows but have no yfinance history and drop out of the market test | yfinance serves no history for delisted symbols; stated in `cross_section.json["caveats"]`. |
| Market model | daily return on KRE (regional bank ETF), estimation window [−250, −30] trading days, ≥ 100 observations | KRE strips out sector moves so CARs reflect bank-specific news; window ends 30 days before the event to exclude forecast-period drift. |
| Day 0 | first NYSE close strictly after the SAR publication timestamp in America/New_York; publications after 16:00 ET roll to the next day | The map could not have been acted on before then. |
| CAR windows | [0,1], [0,5], [0,20]; plus `pre_car` from the first trading day on/after landfall to day −1 | Short windows isolate the SAR-information effect; pre_car shows what the market priced from forecasts and landfall. |
| Cross-section | CAR ~ exposure + storm fixed effects, SEs clustered by storm | Storm FE absorb common shocks; clustering acknowledges within-storm correlation (few clusters → treat t-stats as indicative). |
| Backtest | short top exposure tercile / long bottom tercile (KRE if < 6 exposed banks), hold 5 or 20 days, 10 bps per side, entered at the day-0 close | Simple, implementable rule; skipped when fewer than 3 banks have exposure > 0 rather than trading noise. |

## Validation (SAR flood mask vs NFIP claims)

| Choice | Value | Justification |
|---|---|---|
| Ground truth | OpenFEMA v3 `NfipClaims`, `dateOfLoss` in [landfall − 2 d, landfall + 14 d], AOI states | Claims are the only nationwide, geocoded, per-property flood loss record; the window captures surge (day 0) and river crests (days 2–10). The v3 `censusGeoid` is a 12-digit block group; the tract is its first 11 digits. FEMA's own `floodEvent` label count is stored alongside for comparison. |
| Unit of comparison | Census tract, SAR-covered tracts only | Tracts are the finest unit with both reliable claim geocoding and ACS housing counts; tracts the post-storm mosaic did not cover are excluded, not scored as dry. |
| Claims density | Claims per 1,000 ACS 2023 housing units (B25001) | Normalises for how much there is to flood; raw counts and per-km² are also kept. |
| "Flooded" tract | ≥ 1% of tract area in the flood mask | Same rule as the stress test; avoids calling a tract flooded on a few pixels. |
| "Claims-positive" tract | ≥ 5 NFIP claims | Removes isolated claims (single-property plumbing/seepage losses are common in NFIP data). |
| Baseline | FEMA county declaration vs claims-positive | The cheapest alternative signal; SAR must beat it to be worth anything. |
| Threshold tuning | Grid over diff ∈ {−1.5, −2, −3, −4} dB × post ∈ {−13, −15, −17} dB, scored by AUC of % flooded vs claims-positive, chosen on training storms (Harvey, Irma, Florence, Michael, Laura, Sally) and reported on held-out storms (Ida, Ian, Idalia, Helene, Milton) | Re-thresholding re-uses the saved per-frame dB and eligibility rasters, so the grid costs seconds, not SAR re-processing. Held-out reporting prevents the tuned threshold from being an in-sample artefact. |
| Not validated | Isaias | NFIP claims for October 2026 are not published yet (dataset refreshed 2026-08-04). |

## Live mode

| Choice | Value | Justification |
|---|---|---|
| Alert types | NWS Coastal Flood Warning/Watch/Advisory, Flood Warning/Watch, Flash Flood Warning, Storm Surge Warning/Watch, Hurricane and Tropical Storm Warning/Watch | The flood-relevant subset of `api.weather.gov/alerts/active`; zone-based alerts are resolved to polygons through their `affectedZones`. |
| Scope | Contiguous US only for exposure and satellite status | Guam/Hawaii/Alaska alerts would otherwise dominate the bounding box; they are counted but not scored. |
| Pre-event exposure | FDIC SOD 2026 branches within 500 m of an alert polygon; deposits-in-alert ÷ nationwide deposits | Same buffer as the post-event exposure so the two are comparable; the 2026 vintage is the latest available. |
| Capital One | Nessie `/branches` + `/atms` (geocoded) and the SYNTHETIC portfolio, same 500 m rule | Nessie locations are all in the DC/Baltimore area, so a Hampton Roads coastal flood watch can genuinely show zero Capital One exposure. |
| Satellite status | Sentinel-1 RTC scenes over the alert bbox in the last 30 days; next pass = last pass + 12 days per relative orbit | 12-day repeat per satellite per relative orbit; with 1A/1C/1D flying, effective revisit is shorter, so "next expected" is an upper bound. |
| Refresh | Alerts and NHC re-fetched if older than 15 minutes; zone polygons and FDIC pulls cached indefinitely | NWS asks clients not to hammer the API; zone geometry does not change. |

## Per-company signals (Stocks page, `config/signals.yaml`)

| Choice | Value | Justification |
|---|---|---|
| Signal routing | COF → Nessie branches/ATMs + SYNTHETIC portfolio; other banks → FDIC deposits under NWS alerts; retailers → parking-lot radar; P&C insurers → NWS/NHC activity; ETFs → none | Each company gets the data that touches its business: a bank's risk is where its deposits and borrowers sit, a retailer's is foot traffic, an insurer's is catastrophe activity. |
| Storm-history return | Excess log return vs KRE (banks) or SPY (insurers) from the last close before landfall to 5 closes after it, every storm in `storms.yaml` | Same window for every storm; landfall (not the satellite event time) because this block describes what the market did, not a tradeable signal. |
| Retail stores | OSM ways with the brand's `brand:wikidata` tag, `amenity=parking` polygons within ~100 m, lot ≥ 8,000 m² | 8,000 m² ≈ 80 Sentinel-1 pixels, enough to average speckle; smaller lots are mostly noise. Metros without mapped lots (Atlanta) are skipped, not padded. |
| Metros | Dallas, Atlanta, Phoenix, Chicago, Houston, Denver | Different climates, so rain or snow on one evening doesn't hit every store at once. |
| Pass direction | Ascending only (~18:00–19:00 local over CONUS) | Descending passes are ~06:00 local, when lots are empty. |
| Activity index | Lot dB minus roof dB in the same scene, z-scored per store, averaged across stores per evening | The roof reference absorbs neighbourhood-wide moisture/calibration changes; per-store z-scores stop big lots from dominating. 10 m pixels can't count cars, so this is a brightness proxy. |
| No lookahead in normalisation | The chart z-scores each store over its full history; the YoY signal and backtest use expanding (past-only) store mean/std | A full-history z-score leaks future level and scale into past readings. |
| Sanity check | Holiday season (Nov 15 – Dec 31) vs rest of year, Welch t-test | If the index measures cars, the holiday rush must show. Until it does (p < 0.10), the page labels the signal "unvalidated". |
| YoY signal | Mean index over the trailing 91 days minus the same 91 days a year earlier, ≥ 4 evenings in each | Removes seasonality; a quarter matches the reporting cycle. |
| Backtest | Enter at the first close ≥ acquisition + 3 days, hold 63 trading days, excess vs SPY; Spearman rank correlation with p-value on span ÷ horizon independent windows | 3-day lag covers MPC publication delay (no lookahead). Readings 12 days apart with 63-day holds overlap heavily, so the naive N would overstate significance. |
| Verdict | "lean long/short" only if the index passes the sanity check AND the overlap-adjusted p < 0.10 with positive correlation; otherwise "unvalidated" / "no edge" | A weak or unvalidated signal should say so rather than nudge a trade. Not investment advice. |

## Agent analysis (Markets page, `src/quantcomm/agent`, `config/signals.yaml` › `agent`)

LLM: any OpenAI-compatible endpoint set in `.env` (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). The current setting is the NRP Nautilus endpoint serving `qwen3`. Its reasoning output (`<think>` blocks) is stripped before the JSON is parsed.

| Choice | Value | Justification |
|---|---|---|
| Focus company | Capital One (COF), always shown first | The research exists because of Capital One; its branch/ATM and synthetic-portfolio data are the most specific evidence we have for it. |
| Covered universe | COF, WMT, JPM, BAC, WFC, C, USB, PNC, TFC, ALL, TRV, PGR, CB, KRE | Largest US banks by 2024 deposits, the four P&C insurers, and the regional-bank benchmark. Covering all ~95 tickers would cost ~95 LLM calls per run and add noise. |
| Two-stage agents | One analyst per company (parallel, 4 workers), then one portfolio manager over the valid views | Each analyst sees only its own company's evidence, so its reasoning can't be swayed by others. The portfolio manager sees only the analysts' outputs. |
| Evidence per company | Price trend, benchmark 1-year return, the company's own signal block (data source from `signals.yaml`), and its stock move after every past landfall | The agent must compare today's reading with history, so the history is part of the input rather than something it has to guess. |
| Past storms | Summary (mean excess return, count underperformed) over all storms; the 12 most recent passed in full | A summary covers every storm; passing all rows adds tokens without adding information. |
| Benchmark | KRE for banks, SPY for insurers and retailers | Same as the signal blocks, so the agent and the chart compare against the same index. |
| Output schema | Analyst: view (positive/neutral/negative), confidence 0–100, expected move, reasoning, evidence, data gaps. Portfolio: focus action (buy/hold/avoid/watch), picks limited to covered tickers | Validated before saving; anything outside the schema is an error, not a guess. |
| Failure handling | A failed analyst is recorded with its error; if none succeed or the portfolio reply is invalid the run is an error and nothing is shown as a recommendation | The user must never see a recommendation the agents did not actually produce. |
| Caching & staleness | Result saved to `data/processed/agent/advice.json` with an evidence hash (all bundles + context + prompt version); the page flags it stale when the hash changes | The LLM is only called on demand (button or script). Stale advice is labelled rather than silently replaced. |
| Temperature | 0.2 | Lower sampling noise so reruns on unchanged data mostly agree. |
| Confidence | Self-reported by the model, 0–100 | Not calibrated; shown as a label only. Treat it as the model's stated certainty, not a probability. |
| Not financial advice | The page and every run carry a research-only label | This is a hackathon prototype on a small sample of storms; the caveats above are part of the output. |
