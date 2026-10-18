"""Stocks page payload: real yfinance closes for every public bank in config/bank_tickers.csv,
benchmarks and insurers, plus flood-alert exposure per ticker. Nothing is invented: a missing
price cache is reported, not filled."""
from __future__ import annotations

import json
from functools import lru_cache

import pandas as pd

from floodalpha.banks import tickers
from floodalpha.config import DATA_RAW, ROOT
from floodalpha.market import stock_view
from floodalpha.web import store

PRICES = DATA_RAW / "market" / "prices.parquet"
DATA_PLACEHOLDER = "/*__DATA__*/"


class PricesMissing(FileNotFoundError):
    pass


def _inputs() -> list:
    d = store.data_dir()
    return [PRICES, ROOT / "config" / "bank_tickers.csv", d / "live" / "live_summary.json",
            d / "live" / "bank_alert_exposure.parquet", d / "isaias" / "s1_catalog.parquet"]


@lru_cache(maxsize=2)
def _payload_json(stamp: tuple) -> str:
    d = store.data_dir()
    live = store.load_json(d / "live" / "live_summary.json") or {}
    prices, mapping = pd.read_parquet(PRICES), tickers.load()
    exposure = store.load_parquet(d / "live" / "bank_alert_exposure.parquet")
    storms = store.storms_config()

    payload = stock_view.build_payload(
        prices=prices, mapping=mapping, exposure=exposure, storms=storms,
        catalog=store.load_parquet(d / "isaias" / "s1_catalog.parquet"))
    gen = live.get("generated_utc")
    payload["alerts_generated"] = pd.Timestamp(gen).strftime("%b %d %H:%M UTC") if gen else None
    # keep "</script>" out of the inline JSON block
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def payload_json() -> str:
    if not PRICES.exists():
        raise PricesMissing(f"Price cache not found at {store.rel(PRICES)}. "
                            "Run: PYTHONPATH=src .venv/bin/python -m floodalpha.market.prices")
    stamp = tuple((str(p), p.stat().st_mtime if p.exists() else -1.0) for p in _inputs())
    return _payload_json(stamp)


def render(template: str) -> str:
    if DATA_PLACEHOLDER not in template:
        raise ValueError(f"stocks template has no {DATA_PLACEHOLDER} placeholder")
    return template.replace(DATA_PLACEHOLDER, payload_json())
