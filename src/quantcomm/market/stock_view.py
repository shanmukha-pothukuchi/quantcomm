"""Payload for the dashboard's Stocks page: real yfinance closes + per-ticker summaries + flood context.

Everything here is derived from cached pipeline outputs; nothing is invented. Tickers whose
prices are missing simply don't appear. Flood-alert exposure is joined to tickers with the same
rules the exposure pipeline uses (quantcomm.banks.tickers.assign_tickers).
"""
from __future__ import annotations

import math

import pandas as pd

from quantcomm.banks import tickers as tickers_mod

LARGE_BANK_DEPOSITS_K = 250e6  # >= $250 bn of 2024 SOD deposits -> "Large banks" tab; else "Regional"
TRADING_DAYS_1Y = 252

ETF_NAMES = {
    "SPY": "SPDR S&P 500 ETF",
    "KRE": "SPDR S&P Regional Banking ETF",
    "KBE": "SPDR S&P Bank ETF",
    "XLF": "Financial Select Sector SPDR",
}
INSURER_NAMES = {
    "ALL": "Allstate Corp.",
    "TRV": "Travelers Companies",
    "PGR": "Progressive Corp.",
    "CB": "Chubb Ltd.",
}


def ticker_meta(prices: pd.DataFrame, mapping: pd.DataFrame, retail: dict[str, dict] | None = None) -> dict[str, dict]:
    """name + group for every ticker that has prices. `retail` = signals.yaml `retail` section."""
    banks = mapping[(mapping["ticker"] != "") & (mapping["status"] == "active")].drop_duplicates("ticker")
    out: dict[str, dict] = {}
    for r in banks.itertuples():
        if r.ticker not in prices.columns:
            continue
        large = pd.notna(r.deposits_2024_k) and r.deposits_2024_k >= LARGE_BANK_DEPOSITS_K
        out[r.ticker] = {"name": r.company, "group": "Large banks" if large else "Regional"}
    for names, group in ((ETF_NAMES, "ETFs"), (INSURER_NAMES, "Insurers")):
        for t, n in names.items():
            if t in prices.columns:
                out[t] = {"name": n, "group": group}
    for t, spec in (retail or {}).items():
        if t in prices.columns:
            out[t] = {"name": spec["name"], "group": "Retail"}
    return out


def summarize(series: pd.Series) -> dict | None:
    """Last close, change vs previous close, and trailing-1Y return. None if < 2 closes."""
    s = series.dropna()
    if len(s) < 2:
        return None
    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
    r1y = None
    if len(s) > TRADING_DAYS_1Y:
        base = float(s.iloc[-1 - TRADING_DAYS_1Y])
        r1y = (last / base - 1) * 100
    return {"last": last, "prev": prev, "chg": last - prev, "chg_pct": (last / prev - 1) * 100,
            "r1y_pct": r1y, "last_date": s.index[-1].strftime("%Y-%m-%d")}


def alert_exposure_by_ticker(exposure: pd.DataFrame | None, mapping: pd.DataFrame) -> dict[str, float]:
    """% of each public holding company's deposits sitting in branches under an active NWS flood alert.

    Denominator = national deposits of the HC's certs that appear in the live table (certs with no
    branch under an alert are not in that table), so multi-charter HCs are slightly overstated.
    """
    if exposure is None or exposure.empty:
        return {}
    br = exposure.rename(columns={"holding_company": "namehcr"}).assign(deposits_k=exposure["total_deposits_k"])
    br["namehcr"] = br["namehcr"].fillna("").astype(str)
    br = tickers_mod.assign_tickers(br, mapping)
    br = br[(br["ticker"] != "") & (br["ticker_status"] == "active")]
    g = br.groupby("ticker")[["deposits_in_alert_k", "total_deposits_k"]].sum()
    g = g[g["total_deposits_k"] > 0]
    return {t: float(v) for t, v in (g["deposits_in_alert_k"] / g["total_deposits_k"] * 100).items()}


def storm_events(storms: dict, catalog: pd.DataFrame | None, first: pd.Timestamp, last: pd.Timestamp,
                 case_key: str = "isaias") -> list[dict]:
    """Landfall markers inside the price range. The case-study storm also gets an end = first
    post-landfall Sentinel-1 publication (the no-lookahead event time)."""
    out = []
    for key, s in storms.items():
        lf = pd.Timestamp(s["landfall_time_utc"]).tz_convert(None)
        if not (first <= lf <= last + pd.Timedelta(days=7)):
            continue
        ev = {"key": key, "name": s["name"], "landfall": lf.isoformat(), "event_time": None}
        if key == case_key and catalog is not None and not catalog.empty:
            post = catalog[catalog["is_post_landfall"].astype(bool)]
            if not post.empty:
                ev["event_time"] = pd.Timestamp(post["published_utc"].min()).tz_convert(None).isoformat()
        out.append(ev)
    return out


def _clean(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)


def build_payload(prices: pd.DataFrame, mapping: pd.DataFrame, exposure: pd.DataFrame | None,
                  storms: dict, catalog: pd.DataFrame | None, retail: dict[str, dict] | None = None,
                  signals_fn=None) -> dict:
    """signals_fn(meta) -> {ticker: signal block}; see quantcomm.market.signals.build_signals."""
    px = prices.sort_index()
    meta = ticker_meta(px, mapping, retail)
    exp = alert_exposure_by_ticker(exposure, mapping)
    summary, series = {}, {}
    for t in meta:
        sm = summarize(px[t])
        if sm is None:
            continue
        summary[t] = {**meta[t], **sm, "alert_exposure_pct": exp.get(t)}
        series[t] = [_clean(v) for v in px[t].tolist()]
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in px.index],
        "series": series,
        "summary": summary,
        "events": storm_events(storms, catalog, px.index.min(), px.index.max()),
        "signals": signals_fn({t: meta[t] for t in summary}) if signals_fn else {},
        "as_of": px.index.max().strftime("%Y-%m-%d"),
    }
