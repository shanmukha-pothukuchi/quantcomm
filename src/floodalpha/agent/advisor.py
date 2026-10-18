"""Two-stage agent run: one analyst per covered company (in parallel), then one portfolio manager over their views.

Every output is validated against a schema. A company whose analyst fails is recorded as an error with its message;
nothing is filled in for it. If no analyst succeeds, or the portfolio manager's reply is invalid, the run is an error.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from floodalpha.agent.evidence import evidence_hash
from floodalpha.agent.llm import LLMError
from floodalpha.agent.prompts import ANALYST_SYSTEM, PORTFOLIO_SYSTEM, PROMPT_VERSION

VIEWS = ("positive", "neutral", "negative")
EXPECTED_MOVES = ("up", "flat", "down")
ACTIONS = ("buy", "hold", "avoid")
FOCUS_ACTIONS = ("buy", "hold", "avoid", "watch")
RECOVERABLE = (LLMError, ValueError, KeyError, TypeError)


def _choice(v, allowed: tuple, where: str) -> None:
    if v not in allowed:
        raise ValueError(f"{where} must be one of {', '.join(allowed)}; got {v!r}")


def _confidence(v, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
        raise ValueError(f"{where} confidence must be an integer 0-100; got {v!r}")
    return v


def _text(v, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return v.strip()


def _texts(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{where} must be a list of strings")
    return list(v)


def validate_analyst(obj: dict, ticker: str) -> dict:
    if obj.get("ticker") != ticker:
        raise ValueError(f"ticker mismatch: expected {ticker}, got {obj.get('ticker')!r}")
    _choice(obj.get("view"), VIEWS, "view")
    _choice(obj.get("expected_move"), EXPECTED_MOVES, "expected_move")
    return {
        "ticker": ticker, "view": obj["view"], "expected_move": obj["expected_move"],
        "confidence": _confidence(obj.get("confidence"), ticker),
        "headline": _text(obj.get("headline"), "headline"), "reasoning": _text(obj.get("reasoning"), "reasoning"),
        "evidence_used": _texts(obj.get("evidence_used", []), "evidence_used"),
        "data_gaps": _texts(obj.get("data_gaps", []), "data_gaps"),
    }


def validate_portfolio(obj: dict, universe: list[str], focus: str) -> dict:
    fo = obj.get("focus")
    if not isinstance(fo, dict) or fo.get("ticker") != focus:
        raise ValueError(f"focus section must be for {focus}")
    _choice(fo.get("action"), FOCUS_ACTIONS, "focus action")
    picks = []
    for p in obj.get("picks", []):
        if not isinstance(p, dict) or p.get("ticker") not in universe:
            raise ValueError(f"picks: {p.get('ticker') if isinstance(p, dict) else p!r} is not a covered ticker")
        _choice(p.get("action"), ACTIONS, f"{p['ticker']} action")
        picks.append({"ticker": p["ticker"], "action": p["action"],
                      "confidence": _confidence(p.get("confidence"), p["ticker"]),
                      "reason": _text(p.get("reason"), f"{p['ticker']} reason")})
    return {
        "headline": _text(obj.get("headline"), "headline"),
        "focus": {"ticker": focus, "action": fo["action"],
                  "confidence": _confidence(fo.get("confidence"), focus),
                  "explanation": _text(fo.get("explanation"), "focus explanation"),
                  "what_would_change_my_mind": _text(fo.get("what_would_change_my_mind"), "what_would_change_my_mind")},
        "picks": sorted(picks, key=lambda p: -p["confidence"]),
        "risks": _texts(obj.get("risks", []), "risks"),
        "method_note": _text(obj.get("method_note", "Analyst views combined by the portfolio manager."), "method_note"),
    }


def _analyze(bundle: dict, client) -> dict:
    raw = client.chat_json(ANALYST_SYSTEM, json.dumps(bundle, default=str))
    return validate_analyst(raw, bundle["ticker"])


def run_analysts(bundles: list[dict], client, max_workers: int) -> dict[str, dict]:
    """Returns {ticker: {"status": "ok", "analysis": ...} | {"status": "error", "error": msg}} in universe order."""
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(_analyze, b, client): b["ticker"] for b in bundles}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                results[ticker] = {"status": "ok", "analysis": fut.result()}
            except RECOVERABLE as exc:
                results[ticker] = {"status": "error", "error": str(exc)}
    return {b["ticker"]: results[b["ticker"]] for b in bundles}


def run_portfolio(views: dict[str, dict], context: dict, client, universe: list[str], focus: str) -> dict:
    user = json.dumps({"focus": focus, "universe": universe, "context": context,
                       "analyst_views": [views[t] for t in universe if t in views]}, default=str)
    return validate_portfolio(client.chat_json(PORTFOLIO_SYSTEM, user), universe, focus)


def run(*, bundles: list[dict], context: dict, client, model: str, focus: str, max_workers: int,
        generated_utc: str) -> dict:
    universe = [b["ticker"] for b in bundles]
    analysts = run_analysts(bundles, client, max_workers)
    views = {t: r["analysis"] for t, r in analysts.items() if r["status"] == "ok"}
    base = {"generated_utc": generated_utc, "model": model, "prompt_version": PROMPT_VERSION, "focus": focus,
            "universe": universe, "context": context, "analysts": analysts,
            "evidence_hash": evidence_hash(bundles, context, PROMPT_VERSION)}
    if not views:
        return {**base, "status": "error", "portfolio": None,
                "error": "no analyst produced a valid view; see analysts for each company's error"}
    try:
        portfolio = run_portfolio(views, context, client, universe, focus)
    except RECOVERABLE as exc:
        return {**base, "status": "error", "portfolio": None, "error": f"portfolio manager failed: {exc}"}
    return {**base, "status": "ok", "portfolio": portfolio, "error": None}


def save(advice: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(advice, indent=2, default=str))
    os.replace(tmp, path)


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None
