"""Research panels behind "Show research" on the Markets page: the data an analyst's call relies on.

Which panels a company can show comes from agent.evidence.research_options, the same list its analyst picked from.
Every panel is built from cached pipeline outputs; a missing output is reported with the script that makes it.
Large geometries (alert polygons, flood polygons, storm track) are fetched by the page from the layer endpoints.
"""
from __future__ import annotations

from typing import Callable

import shapely

from quantcomm.agent.evidence import RESEARCH_PANELS, research_options
from quantcomm.banks import tickers
from quantcomm.web import live, store, storms

FLOOD_STORM = "isaias"   # flagship case study: the only post-storm flood map of the current season
TOP_BANKS = 15

TITLES = {
    "branch_map": "Capital One branches & ATMs under live flood alerts",
    "customer_stress": "Customer portfolio stress test (SYNTHETIC)",
    "flood_map": "Hurricane Isaias: Sentinel-1 satellite flood map",
    "alert_exposure": "Bank deposits under live flood alerts",
    "alerts_map": "Live flood alerts & tropical storms",
    "parking_lot": "Parking-lot activity from satellite radar",
    "storm_history": "How the stock moved after past hurricanes",
}


def _branch_map(ticker: str, payload: dict) -> dict:
    return {"nessie": live.nessie_alerts(), "alerts": live.alert_overview()}


def _customer_stress(ticker: str, payload: dict) -> dict:
    return {"storm": FLOOD_STORM, "stress": storms.stress_test(FLOOD_STORM), "live": live.portfolio_alerts()}


def _flood_map(ticker: str, payload: dict) -> dict:
    sig = payload["signals"].get(ticker) or {}
    nessie = storms.nessie_exposure(FLOOD_STORM) if sig.get("type") == "branch_network" else None
    return {"storm": FLOOD_STORM, "name": store.storms_config()[FLOOD_STORM]["name"],
            "flood": storms.flood_summary(FLOOD_STORM), "nessie": nessie}


def _alert_exposure(ticker: str, payload: dict) -> dict:
    return {"banks": live.bank_alerts(TOP_BANKS, tickers.load(), ticker), "alerts": live.alert_overview()}


def _alerts_map(ticker: str, payload: dict) -> dict:
    return {"alerts": live.alert_overview()}


def _stores(ticker: str) -> list[dict]:
    st = store.load_parquet(store.data_dir() / "retail" / f"{ticker}_stores.parquet")
    if st is None or st.empty:
        return []
    pts = shapely.centroid(shapely.from_wkb(st["geometry"].values))
    view = st.assign(lat=shapely.get_y(pts).round(5), lon=shapely.get_x(pts).round(5))
    return store.records(view, ["store_id", "name", "city", "metro", "lat", "lon", "lot_area_m2"])


def _parking_lot(ticker: str, payload: dict) -> dict:
    sig = payload["signals"][ticker]
    return {"series": sig.get("series"), "metrics": sig.get("metrics", []), "verdict": sig.get("verdict"),
            "source": sig.get("source"), "stores": _stores(ticker), "caveats": sig.get("caveats", [])}


def _storm_history(ticker: str, payload: dict) -> dict:
    sig = payload["signals"][ticker]
    bench = "SPY" if sig.get("type") == "weather_alerts" else "KRE"
    return {"bench": bench, "window": "landfall → +5 trading days",
            "rows": sig.get("storm_history", []), "events": payload.get("events", [])}


BUILDERS: dict[str, Callable[[str, dict], dict]] = {
    "branch_map": _branch_map, "customer_stress": _customer_stress, "flood_map": _flood_map,
    "alert_exposure": _alert_exposure, "alerts_map": _alerts_map, "parking_lot": _parking_lot,
    "storm_history": _storm_history,
}


def ticker_research(payload: dict, ticker: str, keys: list[str] | None = None) -> dict:
    """Panels for one company: all its research options, or only `keys` (unknown keys are a KeyError -> 404)."""
    if ticker not in payload["summary"]:
        raise KeyError(ticker)
    options = research_options(payload["signals"].get(ticker))
    wanted = options if not keys else keys
    bad = [k for k in wanted if k not in options]
    if bad:
        raise KeyError(f"{ticker} research panel {', '.join(bad)}")
    return {
        "ticker": ticker, "name": payload["summary"][ticker]["name"], "as_of": payload.get("as_of"),
        "options": [{"key": k, "title": TITLES[k], "shows": RESEARCH_PANELS[k]} for k in options],
        "panels": {k: {"title": TITLES[k], **BUILDERS[k](ticker, payload)} for k in wanted},
    }

