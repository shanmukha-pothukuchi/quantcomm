#!/usr/bin/env python
"""Prices -> event study -> cross-section -> backtest. Usage: run_market.py [--force-prices]"""
import json, logging, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from floodalpha.market import backtest, event_study, prices  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    px = prices.load_prices(force="--force-prices" in sys.argv)
    print(f"[market] prices {px.shape} {px.index.min().date()}..{px.index.max().date()}")
    es = event_study.run_event_study()
    print(f"[market] event study rows: {len(es)} ({es['storm'].nunique()} storms, {es['ticker'].nunique()} tickers)")
    cs = event_study.cross_section(es)
    print(json.dumps(cs["by_definition"], indent=1))
    print("[market] plots:", [str(p) for p in event_study.plots(es)])
    print(json.dumps(backtest.run_backtest(es), indent=1))
