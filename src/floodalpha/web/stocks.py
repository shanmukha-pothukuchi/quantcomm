"""Stocks page payload: real yfinance closes for every public bank in config/bank_tickers.csv,
benchmarks, insurers and retailers, plus each company's flood signal. Nothing is invented: a
missing price cache is reported, not filled."""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from floodalpha.banks import tickers
from floodalpha.config import DATA_RAW, ROOT
from floodalpha.market import signals, stock_view
from floodalpha.web import store

PRICES = DATA_RAW / "market" / "prices.parquet"


class PricesMissing(FileNotFoundError):
    pass


def _inputs() -> list:
    d = store.data_dir()
    return [PRICES, ROOT / "config" / "signals.yaml", ROOT / "config" / "bank_tickers.csv",
            *sorted((d / "live").glob("*")), *sorted((d / "retail").glob("*.parquet")), d / "isaias" / "stress_summary.json"]


def _retail_inputs(sig_cfg: dict) -> dict[str, dict]:
    d = store.data_dir() / "retail"
    return {t: {"backscatter": store.load_parquet(d / f"{t}_lot_backscatter.parquet"),
                "stores": store.load_parquet(d / f"{t}_stores.parquet")} for t in sig_cfg.get("retail", {})}


@lru_cache(maxsize=2)
def _payload(stamp: tuple) -> dict:
    d = store.data_dir()
    live = store.load_json(d / "live" / "live_summary.json") or {}
    sig_cfg = store.config("signals.yaml")
    prices, mapping = pd.read_parquet(PRICES), tickers.load()
    exposure = store.load_parquet(d / "live" / "bank_alert_exposure.parquet")
    storms = store.storms_config()

    def signals_fn(meta: dict) -> dict:
        return signals.build_signals(
            meta, sig_cfg, prices, mapping, exposure, live,
            nessie=store.load_parquet(d / "live" / "nessie_alert_exposure.parquet"),
            portfolio=store.load_parquet(d / "live" / "SYNTHETIC_portfolio_alert_exposure.parquet"),
            stress=store.load_json(d / "isaias" / "stress_summary.json"),
            storms=storms, retail=_retail_inputs(sig_cfg))

    payload = stock_view.build_payload(
        prices=prices, mapping=mapping, exposure=exposure, storms=storms,
        catalog=store.load_parquet(d / "isaias" / "s1_catalog.parquet"),
        retail=sig_cfg.get("retail", {}), signals_fn=signals_fn)
    gen = live.get("generated_utc")
    payload["alerts_generated"] = pd.Timestamp(gen).strftime("%b %d %H:%M UTC") if gen else None
    return payload


def payload() -> dict:
    """Cached stock payload (prices, per-ticker signals, events). Callers must treat it as read-only."""
    if not PRICES.exists():
        raise PricesMissing(f"Price cache not found at {store.rel(PRICES)}. "
                            "Run: PYTHONPATH=src .venv/bin/python -m floodalpha.market.prices")
    stamp = tuple((str(p), p.stat().st_mtime if p.exists() else -1.0) for p in _inputs())
    return _payload(stamp)
