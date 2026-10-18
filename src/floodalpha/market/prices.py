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
    return active + BENCHMARKS + INSURERS + retail_universe()


def retail_universe() -> list[str]:
    return sorted(config.load_yaml("signals.yaml").get("retail", {}))


def _download(tk: list[str]) -> pd.DataFrame:
    px = yf.download(tk, start=START, progress=False, auto_adjust=True, threads=True)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame(tk[0])
    px.index = pd.to_datetime(px.index).tz_localize(None)
    return px.sort_index()


def add_missing(cache: Path) -> pd.DataFrame:
    """Download only the retail tickers (signals.yaml) the cache lacks and merge them onto the cached dates,
    so existing series are not re-pulled. Bank tickers yfinance can't serve stay out (see load_prices)."""
    px = pd.read_parquet(cache)
    missing = [t for t in retail_universe() if t not in px.columns]
    if not missing:
        return px
    new = _download(missing).reindex(px.index)
    got = [c for c in new.columns if new[c].notna().sum() >= 100]
    if set(missing) - set(got):
        log.warning("tickers still missing after incremental download: %s", sorted(set(missing) - set(got)))
    px = px.join(new[got])
    px.to_parquet(cache)
    return px


def load_prices(force: bool = False) -> pd.DataFrame:
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "prices.parquet"
    if cache.exists() and not force:
        return add_missing(cache)
    tk = universe()
    px = _download(tk)
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
