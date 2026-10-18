"""Per-ticker alternative-data signal blocks for the Stocks page.

Each ticker gets the data source that actually says something about that company (config/signals.yaml):
  branch_network   Capital One   Nessie branches/ATMs + SYNTHETIC portfolio under live NWS flood alerts
  flood_deposits   other banks   FDIC SOD deposits in branches under live NWS flood alerts
  parking_lot_sar  retailers     Sentinel-1 radar over store parking lots (floodalpha.retail.activity)
  weather_alerts   insurers      live NWS alerts + NHC storms
  benchmark        ETFs          none
Banks and insurers also get a storm-history block: excess return around every past landfall in storms.yaml.

Every block has the same shape so the page renders them with one component:
  {type, title, source, metrics: [{label, value, unit, note}], verdict: {stance, reason}, caveats: [...],
   series?: {dates, values, signal_dates, signal_values}}
Missing inputs produce a block that says so; nothing is filled in.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from floodalpha.banks import tickers as tickers_mod
from floodalpha.retail import activity

STORM_WINDOW_DAYS = 5          # trading days after landfall for the storm-history return
EXPOSED_PCT = 1.0              # >= this % of deposits under alerts -> "exposed"


def _m(label: str, value, unit: str = "", note: str = "") -> dict:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        value = None
    return {"label": label, "value": value, "unit": unit, "note": note}


def _run_label(live: dict) -> str:
    gen = live.get("generated_utc")
    return pd.Timestamp(gen).strftime("%b %d %H:%M UTC") if gen else "—"


def signal_type(ticker: str, group: str, sig_cfg: dict) -> str:
    if ticker in sig_cfg.get("tickers", {}):
        return sig_cfg["tickers"][ticker]["signal"]
    if ticker in sig_cfg.get("retail", {}):
        return sig_cfg["retail"][ticker]["signal"]
    d = sig_cfg["defaults"]
    return {"ETFs": d["etf"], "Insurers": d["insurer"]}.get(group, d["bank"])


# ------------------------------------------------------------- live bank exposure --
def bank_alert_detail(exposure: pd.DataFrame | None, mapping: pd.DataFrame) -> pd.DataFrame:
    """ticker -> branches / deposits under alert, national deposits of the certs seen, alert events."""
    cols = ["n_branches_in_alert", "deposits_in_alert_k", "total_deposits_k", "exposure_pct", "alert_events"]
    if exposure is None or exposure.empty:
        return pd.DataFrame(columns=cols)
    br = exposure.rename(columns={"holding_company": "namehcr"}).assign(deposits_k=exposure["total_deposits_k"])
    br["namehcr"] = br["namehcr"].fillna("").astype(str)
    br = tickers_mod.assign_tickers(br, mapping)
    br = br[(br["ticker"] != "") & (br["ticker_status"] == "active")]
    if br.empty:
        return pd.DataFrame(columns=cols)
    g = br.groupby("ticker").agg(n_branches_in_alert=("n_branches_in_alert", "sum"),
                                 deposits_in_alert_k=("deposits_in_alert_k", "sum"),
                                 total_deposits_k=("total_deposits_k", "sum"),
                                 alert_events=("alert_events", lambda s: ";".join(sorted({e for v in s.dropna() for e in str(v).split(";") if e}))))
    g = g[g["total_deposits_k"] > 0]
    return g.assign(exposure_pct=g["deposits_in_alert_k"] / g["total_deposits_k"] * 100)


# ---------------------------------------------------------------- storm history --
def storm_history(prices: pd.DataFrame, ticker: str, bench: str, storms: dict, days: int = STORM_WINDOW_DAYS) -> dict:
    """Excess log return vs bench from the last close before each landfall to `days` closes after it."""
    if ticker not in prices or bench not in prices:
        return {"n": 0, "rows": []}
    px = prices[[ticker, bench]].dropna().sort_index()
    lr = np.log(px)
    rows = []
    for s in storms.values():
        lf = pd.Timestamp(s["landfall_time_utc"]).tz_convert(None)
        i = px.index.searchsorted(lf.normalize()) - 1
        j = i + days
        if i < 0 or j >= len(px):
            continue
        ex = (lr[ticker].iloc[j] - lr[ticker].iloc[i]) - (lr[bench].iloc[j] - lr[bench].iloc[i])
        rows.append({"storm": s["name"], "year": int(s.get("year", lf.year)), "excess_pct": float(ex * 100)})
    if not rows:
        return {"n": 0, "rows": []}
    ex = np.array([r["excess_pct"] for r in rows])
    return {"n": len(rows), "mean_pct": float(ex.mean()), "n_negative": int((ex < 0).sum()), "rows": rows,
            "bench": bench, "days": days}


def _storm_metrics(h: dict) -> list[dict]:
    if not h.get("n"):
        return [_m("Past storms", None, note="no landfall inside the price history")]
    return [_m(f"Avg excess return, landfall → +{h['days']}d", h["mean_pct"], "%", f"vs {h['bench']}, {h['n']} storms"),
            _m("Storms with underperformance", f"{h['n_negative']}/{h['n']}")]


# --------------------------------------------------------------------- builders --
def flood_deposits_block(ticker: str, detail: pd.DataFrame, live: dict, hist: dict) -> dict:
    gen = _run_label(live)
    states = ", ".join(live.get("banks", {}).get("states_checked", [])) or "—"
    if ticker in detail.index:
        r = detail.loc[ticker]
        pct = float(r["exposure_pct"])
        metrics = [_m("Deposits under flood alerts", pct, "%", "share of the bank's deposits in alerted branches"),
                   _m("Branches under alerts", int(r["n_branches_in_alert"])),
                   _m("Deposits under alerts", float(r["deposits_in_alert_k"]) * 1e3, "$"),
                   _m("Alert types", r["alert_events"].replace(";", ", ") or "—")]
        stance = ("exposed", f"{pct:.2f}% of deposits sit in branches under active NWS flood alerts") if pct >= EXPOSED_PCT \
            else ("minor", f"only {pct:.2f}% of deposits are under active alerts")
    else:
        metrics = [_m("Deposits under flood alerts", 0.0, "%", f"no branch inside an alert polygon (states checked: {states})")]
        stance = ("clear", "no branches under active NWS flood alerts")
    return {"type": "flood_deposits", "title": "Flood exposure of the branch network",
            "source": f"FDIC Summary of Deposits × NWS active flood alerts (run {gen}) · past landfalls from storms.yaml",
            "metrics": metrics + _storm_metrics(hist), "verdict": {"stance": stance[0], "reason": stance[1]},
            "storm_history": hist.get("rows", []),
            "caveats": ["Live alerts are forecasts, not observed flooding; satellite flood maps replace them after the next Sentinel-1 pass."]}


def branch_network_block(ticker: str, detail: pd.DataFrame, live: dict, nessie: pd.DataFrame | None,
                         portfolio: pd.DataFrame | None, stress: dict | None, hist: dict) -> dict:
    metrics = []
    if nessie is not None and not nessie.empty:
        n_in = int(nessie["in_alert"].sum())
        kinds = nessie["kind"].value_counts().to_dict()
        metrics += [_m("Nessie branches", int(kinds.get("branch", 0)), note=", ".join(sorted(x for x in nessie["state"].dropna().unique() if x))),
                    _m("Nessie ATMs", int(kinds.get("atm", 0))),
                    _m("Branches/ATMs under alerts", n_in, note=f"of {len(nessie)} within a live NWS flood alert")]
    else:
        metrics.append(_m("Nessie branches/ATMs", None, note="data/processed/live/nessie_alert_exposure.parquet missing - run scripts/run_live.py"))
    if portfolio is not None and not portfolio.empty:
        inn = portfolio[portfolio["in_alert"].astype(bool)]
        metrics += [_m("SYNTHETIC customers under alerts", f"{len(inn)}/{len(portfolio)}"),
                    _m("SYNTHETIC loan balance under alerts", float(inn["loan_amount"].sum()), "$")]
    if stress and stress.get("tract_flood_available"):
        metrics.append(_m("Isaias stress: incremental expected loss", stress.get("incremental_expected_loss"), "$",
                          f"SYNTHETIC portfolio, {stress.get('total', {}).get('n_flooded', 0)} customers in flooded tracts"))
    elif stress:
        metrics.append(_m("Isaias stress: incremental expected loss", None, "$", "pending - no Isaias tract flood map yet"))
    if ticker in detail.index:
        metrics.append(_m("FDIC deposits under alerts", float(detail.loc[ticker, "exposure_pct"]), "%"))
    n_in = int(nessie["in_alert"].sum()) if nessie is not None and not nessie.empty else 0
    verdict = {"stance": "exposed" if n_in else "clear",
               "reason": f"{n_in} Capital One branches/ATMs are inside active flood alerts" if n_in
               else "no Capital One branch or ATM is inside an active flood alert"}
    return {"type": "branch_network", "title": "Capital One branches, ATMs & customer portfolio",
            "source": f"Capital One Nessie API (/branches, /atms) × NWS active alerts (run {_run_label(live)})",
            "metrics": metrics + _storm_metrics(hist), "verdict": verdict, "storm_history": hist.get("rows", []),
            "caveats": ["Nessie lists 207 branches in MD/VA/DC and 13 ATMs - a sandbox footprint, not Capital One's real network.",
                        "Customer portfolio is SYNTHETIC (generated from ACS tract incomes)."]}


def weather_block(live: dict, hist: dict) -> dict:
    by_ev = live.get("alerts_by_event", {})
    storms = live.get("nhc_active_storms", [])
    metrics = [_m("Active NWS flood alerts", live.get("n_alerts_conus"), note="CONUS"),
               _m("Coastal flood alerts", sum(v for k, v in by_ev.items() if k.startswith("Coastal"))),
               _m("Active NHC storms", len(storms), note=", ".join(f"{s['name']} ({s['basin'].replace('_', ' ')})" for s in storms) or "none")]
    return {"type": "weather_alerts", "title": "Weather & catastrophe activity",
            "source": f"api.weather.gov active alerts + NHC CurrentStorms (run {_run_label(live)}) · past landfalls from storms.yaml",
            "metrics": metrics + _storm_metrics(hist),
            "verdict": {"stance": "watch" if live.get("n_alerts_conus") else "quiet",
                        "reason": "P&C insurers pay the claims: track active alerts and how the stock moved after past landfalls"},
            "storm_history": hist.get("rows", []),
            "caveats": ["Insurer state-level premium mix is not modeled; alerts are national counts."]}


def retail_block(ticker: str, spec: dict, p: dict, backscatter: pd.DataFrame | None, stores: pd.DataFrame | None,
                 prices: pd.DataFrame) -> dict:
    base = {"type": "parking_lot_sar", "title": f"{spec['name']} parking-lot activity from radar",
            "source": "Sentinel-1 RTC (Planetary Computer) over OpenStreetMap parking lots, ascending passes ~18:00-19:00 local"}
    if backscatter is None or backscatter.empty:
        return {**base, "metrics": [_m("Parking-lot readings", None, note=f"run scripts/run_retail.py --ticker {ticker}")],
                "verdict": {"stance": "no reading", "reason": "satellite pipeline has not been run for this ticker"}, "caveats": []}
    idx = activity.activity_index(backscatter)
    val = activity.validate(idx, p["holiday_window"])
    # signal + backtest use the causal index (store z-scores from past readings only)
    sig = activity.yoy_signal(activity.activity_index(backscatter, causal=True), p["yoy_window_days"], p["min_scenes_per_window"])
    bt = activity.backtest(sig, prices, ticker, p["benchmark"], p["publication_lag_days"], p["forward_days"]) \
        if ticker in prices else {"n": 0}
    latest = float(sig["signal"].iloc[-1]) if not sig.empty else None
    v = activity.verdict(latest, bt, val, p["significance"])
    hol = val["holiday"]
    metrics = [
        _m("Stores tracked", int(backscatter["store_id"].nunique()), note=", ".join(sorted(backscatter["metro"].unique()))),
        _m("Satellite evenings", int(len(idx)), note=f"{idx['date'].min():%b %Y} – {idx['date'].max():%b %Y}" if len(idx) else ""),
        _m("Latest YoY activity change", latest, "σ", f"{p['yoy_window_days']}-day mean vs a year earlier" +
           (f", as of {sig['date'].iloc[-1]:%b %d, %Y}" if latest is not None else "")),
        _m("Holiday-season lift", hol["diff"], "σ", f"p = {hol['p']:.3f}" if hol["p"] is not None else "too few scenes"),
        _m("Backtest rank corr. (signal → fwd excess)", bt.get("spearman"), "",
           f"p = {bt['p_overlap_adj']:.2f} with {bt['n_independent']} independent windows" if bt.get("p_overlap_adj") is not None else "too few readings"),
        _m(f"Hit rate, {p['forward_days']}d vs {p['benchmark']}", bt["hit_rate"] * 100 if bt.get("hit_rate") is not None else None, "%",
           f"{bt['n']} readings" if bt.get("n") else ""),
    ]
    roll = idx["activity"].rolling(5, min_periods=3, center=True).mean()
    series = {"dates": idx["date"].dt.strftime("%Y-%m-%d").tolist(),
              "values": [None if pd.isna(x) else round(float(x), 3) for x in roll],
              "signal_dates": sig["date"].dt.strftime("%Y-%m-%d").tolist(),
              "signal_values": [round(float(x), 3) for x in sig["signal"]]}
    return {**base, "metrics": metrics, "verdict": v, "series": series, "validation": val, "backtest": bt,
            "n_stores_mapped": int(len(stores)) if stores is not None else None,
            "caveats": ["10 m radar pixels can't count cars; the index is mean lot brightness relative to the store roof, z-scored per store.",
                        "Sentinel-1 revisits every 12 days, so the index samples ~30 evenings a year per metro.",
                        f"Backtest windows overlap; the p-value uses ~{bt.get('n_independent', '?')} independent windows. Not investment advice."]}


def build_signals(meta: dict[str, dict], sig_cfg: dict, prices: pd.DataFrame, mapping: pd.DataFrame,
                  exposure: pd.DataFrame | None, live: dict, nessie: pd.DataFrame | None, portfolio: pd.DataFrame | None,
                  stress: dict | None, storms: dict, retail: dict[str, dict]) -> dict[str, dict]:
    """retail: ticker -> {"backscatter": df|None, "stores": df|None}."""
    detail = bank_alert_detail(exposure, mapping)
    out = {}
    for t, m in meta.items():
        kind = signal_type(t, m["group"], sig_cfg)
        if kind == "benchmark":
            out[t] = {"type": "benchmark", "title": "Index ETF", "source": "", "metrics": [],
                      "verdict": {"stance": "benchmark", "reason": "index fund - used as the comparison, no company-specific data"},
                      "caveats": []}
        elif kind == "parking_lot_sar":
            r = retail.get(t, {})
            out[t] = retail_block(t, sig_cfg["retail"][t], sig_cfg["parking_sar"], r.get("backscatter"), r.get("stores"), prices)
        else:
            bench = "SPY" if kind == "weather_alerts" else "KRE"
            hist = storm_history(prices, t, bench, storms)
            if kind == "branch_network":
                out[t] = branch_network_block(t, detail, live, nessie, portfolio, stress, hist)
            elif kind == "weather_alerts":
                out[t] = weather_block(live, hist)
            else:
                out[t] = flood_deposits_block(t, detail, live, hist)
    return out
