# FloodAlpha

**Banks don't know their flood exposure for weeks after a storm. Free radar satellites can estimate it within days.
We measure it for every US bank, stress-test a Capital One portfolio, and test whether the market prices it in.**

Built at Technica 2026. Flagship case: **Hurricane Isaias**, landfall near Destin, FL at 2026-10-10 01:30 UTC (Cat 2).
Live demo: the mid-Atlantic coastal flood event (Outer Banks → Hampton Roads → Maryland beaches, Oct 19–20, 2026).

## What it does

1. **Maps flooding from Sentinel-1 radar** within hours of the first post-storm pass (sees through cloud; keyless via
   Microsoft Planetary Computer). Each map carries the Copernicus *publication* timestamp of the imagery, so every
   downstream signal is lookahead-free.
2. **Scores every FDIC-insured bank**: share of national deposits held in branches within 500 m of mapped flooding,
   using the Summary of Deposits vintage that existed before the storm.
3. **Stress-tests a Capital One portfolio** through the Nessie API: 500 *synthetic* customers (incomes from ACS tract
   medians) created in the Nessie sandbox; expected loss = EAD × PD × LGD with a flooded-tract uplift.
4. **Tests the market**: event-study CARs vs KRE, cross-section with storm fixed effects, long-short backtest.
5. **Validates against ground truth**: tract-level flood % vs OpenFEMA NFIP claims, thresholds tuned on training storms only.
6. **Live mode**: active NWS flood alerts + NHC storms → which banks and Capital One locations sit under an alert now,
   and when the next satellite pass lands.

## Architecture

```
 Sentinel-1 RTC (Planetary Computer) ─┐
 Copernicus catalog (PublicationDate) ─┤ flood/catalog.py       scenes, orbits, event time
 JRC surface water · NASADEM slope ────┤ flood/mpc_backend.py   post−pre dB change → mask, polygons, previews
 NWS warnings · NHC track · FEMA ──────┘ flood/sanity.py        independent check of each map
                                          │
 FDIC Summary of Deposits ────────────────┤ banks/fdic.py, banks/exposure.py    every bank × storm
 config/bank_tickers.csv · yfinance ──────┤ market/event_study.py, backtest.py  CARs, cross-section, long-short
 Census ACS + TIGER tracts ───────────────┤ census/acs.py, exposure.py          tract % flooded
 Nessie /branches /atms + customers ──────┤ nessie/client.py, portfolio.py, stress.py   SYNTHETIC stress test
 OpenFEMA NFIP claims v3 ─────────────────┤ validation/nfip.py                  claims vs flood %, threshold tuning
 api.weather.gov · NHC CurrentStorms ─────┘ live/alerts.py, live/exposure.py    pre-event exposure, next pass
                                          ▼
                          floodalpha.web (FastAPI): /  landing · /dashboard  6 tabs · /stocks
```

## Run

```bash
uv venv -p 3.13 && uv pip install -e ".[dev]"
cp .env.example .env                       # NESSIE_API_KEY (writes only); CENSUS_API_KEY and EE_PROJECT optional
.venv/bin/python scripts/verify_sources.py # every source: PASS / NEEDS_KEY / FAIL + sample record
.venv/bin/python scripts/run_all.py        # flood map + tracts + exposure + sanity for all 12 storms (hours; 3 in parallel)
.venv/bin/python scripts/run_storm.py --storm isaias --steps portfolio,stress   # SYNTHETIC Nessie portfolio
.venv/bin/python scripts/run_banks.py      # every bank × storm exposure (FDIC SOD)
.venv/bin/python scripts/run_market.py     # prices, event study, cross-section, backtest
.venv/bin/python scripts/run_validation.py # NFIP claims vs flood maps, threshold grid
.venv/bin/python scripts/run_live.py       # alerts, NHC, pre-event exposure, satellite status
.venv/bin/python -m floodalpha.web         # http://127.0.0.1:8000  (/, /dashboard, /stocks, /api/docs)
.venv/bin/python -m pytest -q
```

The site is one FastAPI app (`src/floodalpha/web/`) serving static pages from `web/`: the landing page at `/`,
the dashboard at `/dashboard` (storm case study, live, bank exposure, market test, validation, methodology) and
the bank/ETF/insurer/retail stocks page at `/stocks`. Pages read a small JSON API (`/api/*`) built only from
`data/processed/`; a missing output is shown as "not generated yet" with the script that produces it.

Config lives in `config/storms.yaml` (12 storms, AOIs, landfall times) and `config/model.yaml` (thresholds, PD/LGD,
buffers). Raw pulls are cached in `data/raw/`, outputs in `data/processed/` (column contracts in `src/floodalpha/schemas.py`).
Everything derived from generated customers is prefixed `SYNTHETIC_`.

## Results (generated 2026-10-17 21:55 UTC by `scripts/write_results.py` from `data/processed/`)

| Storm | Landfall → first SAR published | Passes / AOI coverage | Flooded km² | Tracts ≥ 1 % flooded (of SAR-covered) | Banks with flooded deposits |
|---|---|---|---|---|---|
| Harvey 2017 | 85 h | 4 / 100 % | 1,368 | 187 of 2464 | 13 of 6068 |
| Irma 2017 | 24 h | 3 / 100 % | 202 | 122 of 4744 | 27 of 6068 |
| Florence 2018 | 3 h | 4 / 93 % | 69 | 16 of 1099 | 0 of 5797 |
| Michael 2018 | 10 h | 2 / 100 % | 32 | 1 of 433 | 1 of 5797 |
| Laura 2020 | 119 h | 2 / 100 % | 221 | 9 of 450 | 1 of 5313 |
| Sally 2020 | 6 h | 3 / 100 % | 7 | 2 of 542 | 0 of 5313 |
| Ida 2021 | 113 h | 2 / 41 % | 8 | 0 of 289 | 0 of 5076 |
| Ian 2022 | 106 h | 2 / 99 % | 242 | 39 of 4235 | 0 of 4960 |
| Idalia 2023 | 65 h | 3 / 100 % | 27 | 3 of 537 | 0 of 4781 |
| Helene 2024 | 49 h | 3 / 65 % | 18 | 2 of 2853 | 0 of 4655 |
| Milton 2024 | 30 h | 2 / 76 % | 101 | 28 of 2759 | 2 of 4655 |
| Isaias 2026 | 28 h | 1 / 52 % | 3 | 0 of 417 | 0 of 4249 |

- **SYNTHETIC Capital One stress test (Isaias):** 500 generated customers pushed to Nessie, $23.4 M exposure at default, $438 k baseline expected loss, **$0 k incremental loss** (0 customers in tracts ≥ 1 % flooded). The Nessie branch footprint is MD/VA/DC, so direct Isaias exposure is 0 of 220 locations.
- **Market test, satellite exposure (% of deposits in flooded branches):** CAR[0,1] coef -0.741 pp per pct (t = -7.8, N = 1415, 12 storms, 19 non-zero); CAR[0,5] coef -1.073 pp per pct (t = -27.1, N = 1296, 11 storms, 19 non-zero); CAR[0,20] coef +3.503 pp per pct (t = 83.6, N = 1296, 11 storms, 19 non-zero).
- **Market test, share of deposits inside the storm AOI:** CAR[0,1] coef +0.003 pp per pct (t = 0.8, N = 1415, 12 storms, 257 non-zero); CAR[0,5] coef -0.011 pp per pct (t = -1.6, N = 1296, 11 storms, 236 non-zero); CAR[0,20] coef -0.024 pp per pct (t = -2.2, N = 1296, 11 storms, 236 non-zero).
- **Long-short backtest (short top-exposure tercile, long bottom tercile, 10 bp/side):** hold 5 d: 2 trades, hit rate 0%, cumulative net -0.70 %; hold 20 d: 2 trades, hit rate 0%, cumulative net -1.04 %. Skipped (fewer than 3 exposed priced banks): florence, helene, ian, ida, idalia, isaias, laura, michael, milton, sally.
- **Reading:** on the satellite measure only ~19 of ~1,400 storm-bank observations have non-zero exposure, and the coefficient flips sign between windows, so the large t-statistics are an artefact of clustered standard errors with few effective observations and should not be believed. On the AOI-deposits measure the coefficients are small, negative and marginal at best. The backtest lost money on both trades it could make. **We treat this as a null result.**
- **Validation vs NFIP claims (10 storms, 161,960 claims, 15,210 SAR-covered tracts):** Spearman ρ(tract % flooded, claims density) = 0.12 (p = 3.3e-49); AUC for 'tract has claims' = 0.54 for the SAR map vs 0.51 for the 'county was declared' baseline; at the ≥ 1 % rule precision = 0.48, recall = 0.06.
- **Threshold tuning (train: florence, harvey, irma, laura, michael, sally; held-out: ian, ida, idalia):** best on train = -1.5 dB / -13.0 dB (train score 0.556, held-out 0.607) vs default −3 dB / −15 dB (train 0.539, held-out 0.552). The map is weakly but consistently informative; precision is decent, recall is very low because a first-pass radar snapshot misses most claim-generating flooding.
- **Live mode (2026-10-17T20:33 UTC):** 22 active CONUS flood-type alerts (mid-Atlantic coastal flood event). 57 banks hold $50.1 bn of deposits in branches under an alert; largest: TowneBank. Re-run `scripts/run_live.py` before the demo.

## Honest limitations

- A first-pass radar image is a snapshot: 22 h after Isaias's landfall the surge had receded and the rivers had not
  crested, so the map shows ~3 km² of standing water. Later orbits help (Michael's second pass found 32 km²), but
  the "within days" signal is inherently sparse.
- VV change detection misses flooded vegetation and urban flooding, and slope masking removes mountain terrain
  (Helene's worst damage).
- AOIs are analyst-chosen boxes; banks outside the box get exposure 0 for that storm.
- ~12 storms, overlapping event windows, survivorship bias from delisted tickers (SunTrust, BB&T, IBERIABANK, …),
  and three CAR windows tested: treat any market result as exploratory.
- The Capital One portfolio is synthetic; the Nessie branch footprint is MD/VA/DC only.

Every modeling choice is justified in `ASSUMPTIONS.md`; the spec and verified data-source facts are in `CLAUDE.md`.
