"""Daily adjusted closes from yfinance, cached to data/raw/market/prices.parquet (wide: date x ticker)."""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from floodalpha import config
from floodalpha.banks import tickers

log = logging.getLogger(__name__)
RAW = config.DATA_RAW / "market"
BENCHMARKS = ["SPY", "KRE", "KBE", "XLF"]
INSURERS = ["ALL", "TRV", "PGR", "CB"]
START = "2016-01-01"


def universe() -> list[str]:
    m = tickers.load()
    active = sorted({t for t, s in zip(m["ticker"], m["status"]) if t and s == "active"})
    return active + BENCHMARKS + INSURERS


def load_prices(force: bool = False) -> pd.DataFrame:
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "prices.parquet"
    if cache.exists() and not force:
        return pd.read_parquet(cache)
    tk = universe()
    px = yf.download(tk, start=START, progress=False, auto_adjust=True, threads=True)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame(tk[0])
    px.index = pd.to_datetime(px.index).tz_localize(None)
    px = px.sort_index()
    empty = [c for c in px.columns if px[c].notna().sum() < 100]
    if empty:
        log.warning("tickers with <100 price rows (dropped): %s", empty)
        px = px.drop(columns=empty)
    missing = sorted(set(tk) - set(px.columns))
    if missing:
        log.warning("tickers missing from yfinance download: %s", missing)
    for b in ("SPY", "KRE"):
        if b not in px.columns:
            raise RuntimeError(f"benchmark {b} missing from price download")
    px.to_parquet(cache)
    return px


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    p = load_prices(force="--force" in sys.argv)
    print(p.shape, p.index.min().date(), p.index.max().date(), "tickers:", len(p.columns))
