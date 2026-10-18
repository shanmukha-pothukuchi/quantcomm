#!/usr/bin/env python
"""Agent analysis for the Markets page: one analyst per covered company, then a portfolio manager.

Calls the LLM endpoint in .env (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL), caches the result in
data/processed/agent/advice.json and prints a per-company summary. Failures are printed and cached, never filled in.

    PYTHONPATH=src .venv/bin/python scripts/run_advice.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodalpha.agent.llm import LLMError
from floodalpha.web import markets, stocks


def _print_summary(advice: dict) -> None:
    print(f"status: {advice['status']}  model: {advice['model']}  generated: {advice['generated_utc']}")
    for t, r in advice["analysts"].items():
        if r["status"] == "ok":
            a = r["analysis"]
            print(f"  {t:<5} {a['view']:<9} {a['confidence']:>3}%  {a['headline']}")
        else:
            print(f"  {t:<5} ERROR  {r['error']}")
    if advice["portfolio"]:
        pf = advice["portfolio"]
        print(f"\n{pf['headline']}")
        f = pf["focus"]
        print(f"  {f['ticker']}: {f['action']} ({f['confidence']}%) - {f['explanation']}")
        for p in pf["picks"]:
            print(f"  {p['ticker']:<5} {p['action']:<6} {p['confidence']:>3}%  {p['reason']}")
    elif advice.get("error"):
        print(f"\n{advice['error']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        advice = markets.run_analysis(stocks.payload())
    except (LLMError, stocks.PricesMissing) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(advice)
    print(f"\nsaved: {markets.ADVICE_PATH}")
    return 0 if advice["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
