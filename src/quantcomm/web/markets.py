"""Markets app: the stock list, one company's chart data, and the agents' cached analysis.

The focus company (Capital One) and the covered universe come from config/signals.yaml `agent`. The analysis is
never produced on page load: it is cached at ADVICE_PATH and only (re)built by POST /api/advice/run or
scripts/run_advice.py. The cache knows when it is stale: its evidence hash no longer matches the current data.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from quantcomm.agent import advisor, evidence
from quantcomm.agent.llm import LLMClient
from quantcomm.config import DATA_PROCESSED, load_yaml
from quantcomm.web import store

ADVICE_PATH = DATA_PROCESSED / "agent" / "advice.json"
_AGENT_CFG = load_yaml("signals.yaml")["agent"]
FOCUS: str = _AGENT_CFG["focus"]
CONFIG_UNIVERSE: list[str] = list(_AGENT_CFG["universe"])
MAX_WORKERS: int = int(_AGENT_CFG["max_workers"])
HORIZON_DAYS: int = int(_AGENT_CFG["forecast_horizon_days"])
_run_lock = threading.Lock()


class AnalysisBusy(RuntimeError):
    pass


def covered(payload: dict) -> list[str]:
    """Covered universe in config order, keeping only companies that have prices."""
    return [t for t in CONFIG_UNIVERSE if t in payload["summary"]]


def list_view(payload: dict) -> dict:
    rows = [{"ticker": t, "name": s["name"], "group": s["group"], "last": s["last"], "chg_pct": s["chg_pct"],
             "r1y_pct": s.get("r1y_pct"), "has_signal": t in payload["signals"]}
            for t, s in payload["summary"].items()]
    rows.sort(key=lambda r: (r["ticker"] != FOCUS, r["ticker"] not in CONFIG_UNIVERSE, r["ticker"]))
    return {"as_of": payload["as_of"], "focus": FOCUS, "universe": covered(payload), "tickers": rows}


def ticker_view(payload: dict, ticker: str) -> dict:
    if ticker not in payload["summary"]:
        raise KeyError(ticker)
    s = payload["summary"][ticker]
    bench = evidence.bench_for(s["group"])
    return {
        "ticker": ticker, "name": s["name"], "group": s["group"], "focus": ticker == FOCUS, "as_of": payload["as_of"],
        "summary": {"last": s["last"], "chg_pct": s["chg_pct"], "r1y_pct": s.get("r1y_pct"), "last_date": s["last_date"]},
        "dates": payload["dates"], "closes": payload["series"][ticker],
        "benchmark": {"ticker": bench, "closes": payload["series"].get(bench)},
        "events": payload["events"],
        "signal": payload["signals"].get(ticker),
        "research_options": evidence.research_options(payload["signals"].get(ticker)),
    }


def _evidence(payload: dict) -> tuple[list[dict], dict]:
    live = store.load_json(store.data_dir() / "live" / "live_summary.json")
    bundles = [evidence.bundle(t, payload, FOCUS, HORIZON_DAYS) for t in covered(payload)]
    return bundles, evidence.context(payload, live, FOCUS)


def advice_view(payload: dict) -> dict:
    cached = advisor.load(ADVICE_PATH)
    if cached is None:
        return {"available": False, "stale": None, "advice": None,
                "hint": "No agent analysis yet. Press Run analysis, or run: PYTHONPATH=src .venv/bin/python scripts/run_advice.py"}
    bundles, ctx = _evidence(payload)
    current = evidence.evidence_hash(bundles, ctx, advisor.PROMPT_VERSION)
    return {"available": True, "stale": cached.get("evidence_hash") != current, "advice": cached, "hint": None}


def run_analysis(payload: dict) -> dict:
    """Runs the analysts and portfolio manager, caches the result and returns it. One run at a time."""
    if not _run_lock.acquire(blocking=False):
        raise AnalysisBusy("An analysis is already running; wait for it to finish.")
    try:
        client = LLMClient.from_env()
        bundles, ctx = _evidence(payload)
        advice = advisor.run(bundles=bundles, context=ctx, client=client, model=client.model, focus=FOCUS,
                             max_workers=MAX_WORKERS, generated_utc=datetime.now(UTC).isoformat(timespec="seconds"))
        advisor.save(advice, ADVICE_PATH)
        return advice
    finally:
        _run_lock.release()
