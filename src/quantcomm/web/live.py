"""Live-mode payloads for the research panels: active NWS flood alerts, NHC storms, and who sits under the alerts."""
from __future__ import annotations

import pandas as pd

from quantcomm.banks import tickers
from quantcomm.web import store

ALERT_SIMPLIFY_DEG = 0.002  # ~200 m; alert polygons are county/zone outlines, the browser doesn't need more
ALERT_PROPS = ("event", "severity", "areaDesc", "headline", "onset", "ends")
BANK_COLS = ["bank_name", "holding_company", "ticker", "n_branches_in_alert", "deposits_in_alert_k", "exposure_pct",
             "alert_events", "total_deposits_k"]


def _live_dir():
    return store.data_dir() / "live"


def alert_overview() -> dict:
    """Alert counts by type and the active NHC storms from the last live run."""
    d = _live_dir()
    live = store.load_json(d / "live_summary.json")
    if live is None:
        return store.missing("Live summary", d / "live_summary.json", "scripts/run_live.py")
    nhc = store.load_json(d / "nhc.json") or {}
    return {"available": True, "source": store.rel(d / "live_summary.json"),
            "generated_utc": live.get("generated_utc"), "n_alerts_conus": live.get("n_alerts_conus"),
            "alerts_by_event": live.get("alerts_by_event") or {},
            "active_storms": nhc.get("active_storms") or live.get("nhc_active_storms") or []}


def _tag_tickers(be: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    if be.empty or not {"cert", "rssdhcr"} <= set(be.columns):
        return be.assign(ticker="")
    tagged = tickers.assign_tickers(
        be.assign(namehcr=be["holding_company"].fillna("").astype(str), deposits_k=be["total_deposits_k"]), mapping)
    return be.assign(ticker=tagged["ticker"].where(tagged["ticker_status"] == "active", "").values)


def bank_alerts(top: int, mapping: pd.DataFrame, ticker: str | None = None) -> dict:
    """Banks ranked by deposits in branches under alerts (ticker-tagged), plus every row of `ticker`."""
    path = _live_dir() / "bank_alert_exposure.parquet"
    be = store.load_parquet(path)
    if be is None:
        return store.missing("Bank alert exposure", path, "scripts/run_live.py")
    be = _tag_tickers(be, mapping).sort_values("deposits_in_alert_k", ascending=False)
    return {"available": True, "source": store.rel(path), "n": len(be),
            "rows": store.records(be.head(top), BANK_COLS),
            "ticker_rows": store.records(be[be["ticker"] == ticker], BANK_COLS) if ticker else []}


def nessie_alerts() -> dict:
    path = _live_dir() / "nessie_alert_exposure.parquet"
    ne = store.load_parquet(path)
    if ne is None:
        return store.missing("Capital One alert exposure", path, "scripts/run_live.py")
    return {"available": True, "source": store.rel(path), "n": len(ne), "n_in_alert": int(ne["in_alert"].sum()),
            "locations": store.records(ne, ["kind", "name", "city", "state", "lat", "lon", "in_alert", "alert_events"])}


def portfolio_alerts() -> dict:
    path = _live_dir() / "SYNTHETIC_portfolio_alert_exposure.parquet"
    pa = store.load_parquet(path)
    if pa is None:
        return store.missing("SYNTHETIC portfolio alert exposure", path, "scripts/run_live.py")
    return {"available": True, "synthetic": True, "source": store.rel(path),
            "n": len(pa), "n_in_alert": int(pa["in_alert"].sum()),
            "loan_in_alert": store.clean(float(pa.loc[pa["in_alert"], "loan_amount"].sum()))}


def live_alerts() -> dict | None:
    return store.load_geojson(_live_dir() / "alerts.geojson", ALERT_SIMPLIFY_DEG, ALERT_PROPS)
