"""Evidence bundles: everything an agent may see about one company. Built only from cached payload fields.

A bundle has the company's price trend, the benchmark's trend, its data-source signal (Capital One branches and
ATMs, Walmart parking-lot radar, flood alerts, ...) and its history after past landfalls. Nothing is invented:
missing inputs stay None and are listed in `data_gaps`.

It also lists the research panels the page can show for this company (`research_options`); the analyst picks the
ones its reasoning relies on, and the Markets page opens exactly those behind "Show research".
"""
from __future__ import annotations

import hashlib
import json
import math

HISTORY_ROWS = 12                       # most recent past storms passed in full; the summary covers all of them
DEFAULT_BENCH = "KRE"                   # banks are measured against the regional-bank ETF
BENCH_BY_GROUP = {"Insurers": "SPY", "Retail": "SPY"}  # insurers and retailers against the broad market (as in signals.py)
STORM_WINDOW = "landfall to +5 trading days"
DEFAULT_HORIZON_DAYS = 20               # forecast horizon if the caller does not pass config/signals.yaml agent value

# Research panels the Markets page can draw (web/assets/research.js); the analyst sees the descriptions.
RESEARCH_PANELS = {
    "branch_map": "map of Capital One branches and ATMs (Nessie) with live NWS flood-alert polygons",
    "customer_stress": "SYNTHETIC Capital One customer portfolio: Isaias stress test and loans under live alerts",
    "flood_map": "Sentinel-1 radar before/after images and detected flood polygons for Hurricane Isaias (Oct 2026)",
    "alert_exposure": "US banks ranked by deposits in branches under live NWS flood alerts (FDIC Summary of Deposits)",
    "alerts_map": "map of live NWS flood alerts plus active NHC tropical storms",
    "parking_lot": "Sentinel-1 radar parking-lot activity index and year-over-year signal, with the tracked stores",
    "storm_history": "the stock's excess return vs its benchmark after each past hurricane landfall",
}
PANELS_BY_SIGNAL = {
    "branch_network": ("branch_map", "customer_stress", "flood_map", "storm_history"),
    "flood_deposits": ("alert_exposure", "alerts_map", "flood_map", "storm_history"),
    "weather_alerts": ("alerts_map", "flood_map", "storm_history"),
    "parking_lot_sar": ("parking_lot",),
}


def _num(v, nd: int = 2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), nd)


def bench_for(group: str) -> str:
    return BENCH_BY_GROUP.get(group, DEFAULT_BENCH)


def research_options(signal: dict | None) -> list[str]:
    """Panel keys that have data behind them for this signal block (storm history only when there is some)."""
    if not signal:
        return []
    keys = PANELS_BY_SIGNAL.get(signal.get("type"), ())
    return [k for k in keys if k != "storm_history" or signal.get("storm_history")]


def _metric(m: dict) -> dict:
    return {"label": m.get("label", ""), "value": _num(m.get("value"), 4) if isinstance(m.get("value"), float) else m.get("value"),
            "unit": m.get("unit", ""), "note": m.get("note", "")}


def _past_storms(rows_all: list[dict], bench: str) -> dict:
    ex = [float(r["excess_pct"]) for r in rows_all]
    rows = rows_all[-HISTORY_ROWS:]
    return {
        "window": STORM_WINDOW, "bench": bench, "n": len(ex),
        "mean_excess_pct": _num(sum(ex) / len(ex)) if ex else None,
        "n_underperformed": sum(1 for x in ex if x < 0),
        "rows": [{"storm": r["storm"], "year": r["year"], "excess_pct": _num(r["excess_pct"])} for r in rows],
    }


def bundle(ticker: str, payload: dict, focus: str, horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    summ = payload["summary"][ticker]
    bench = bench_for(summ["group"])
    sig = payload.get("signals", {}).get(ticker)
    rows_all = list(sig.get("storm_history", [])) if sig else []
    gaps = []
    if sig is None:
        gaps.append("no signal block")
    if not rows_all:
        gaps.append("no past storm history")
    if summ.get("r1y_pct") is None:
        gaps.append("less than one year of prices")
    signal = None if sig is None else {
        "type": sig.get("type"), "source": sig.get("source", ""), "title": sig.get("title", ""),
        "stance": sig.get("verdict", {}).get("stance"), "reason": sig.get("verdict", {}).get("reason"),
        "metrics": [_metric(m) for m in sig.get("metrics", [])],
        "caveats": list(sig.get("caveats", [])),
    }
    return {
        "ticker": ticker, "name": summ["name"], "group": summ["group"], "is_focus": ticker == focus,
        "price": {"as_of": summ["last_date"], "last": _num(summ["last"]), "change_1d_pct": _num(summ["chg_pct"]),
                  "return_1y_pct": _num(summ.get("r1y_pct")),
                  "benchmark": {"ticker": bench, "return_1y_pct": _num(payload["summary"].get(bench, {}).get("r1y_pct"))}},
        "signal": signal,
        "past_storms": _past_storms(rows_all, bench),
        "data_gaps": gaps,
        "forecast_horizon_days": horizon_days,
        "research_options": [{"key": k, "shows": RESEARCH_PANELS[k]} for k in research_options(sig)],
    }


def context(payload: dict, live: dict | None, focus: str) -> dict:
    """Market-wide facts shared by every analyst: as-of date, live flood activity, landfalls in the price history."""
    live = live or {}
    return {
        "as_of": payload.get("as_of"),
        "focus": focus,
        "live_flood_alerts": live.get("n_alerts_conus"),
        "active_tropical_storms": [s.get("name") for s in live.get("nhc_active_storms", [])],
        "landfalls_in_price_history": [{"storm": e["name"], "landfall": e["landfall"]} for e in payload.get("events", [])],
    }


def evidence_hash(bundles: list[dict], context_: dict, prompt_version: str) -> str:
    """Fingerprint of exactly what the agents saw; a cached analysis is stale when this changes."""
    blob = json.dumps({"prompt": prompt_version, "context": context_, "bundles": bundles}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
