"""Two-stage agent run: one analyst per ticker, then a portfolio manager. Failures are recorded, never filled in."""
from __future__ import annotations

import json

import pytest

from quantcomm.agent import advisor
from quantcomm.agent.llm import LLMError

UNIVERSE = ["COF", "WMT", "JPM"]


def _analyst(ticker: str, view: str = "neutral") -> dict:
    return {"ticker": ticker, "view": view, "confidence": 55, "expected_move": "flat",
            "headline": f"{ticker} looks {view}", "reasoning": "Two sentences citing 0 of 220 branches.",
            "evidence_used": ["branches under alert"], "data_gaps": [],
            "forecast": {"return_pct": 0.4, "low_pct": -3.0, "high_pct": 3.5}, "research": ["branch_map"]}


def _portfolio() -> dict:
    return {"headline": "Hold Capital One; no flood-driven edge",
            "focus": {"ticker": "COF", "action": "hold", "confidence": 60,
                      "explanation": "No branch is under an alert.", "what_would_change_my_mind": "An alert over a Capital One branch."},
            "picks": [{"ticker": "JPM", "action": "buy", "confidence": 52, "reason": "steady"},
                      {"ticker": "WMT", "action": "avoid", "confidence": 40, "reason": "no edge"}],
            "risks": ["sandbox footprint"], "method_note": "Analyst views + portfolio rules."}


class ScriptedClient:
    """Stands in for LLMClient: answers per-ticker analyst prompts and the portfolio prompt."""

    def __init__(self, fail: set[str] | None = None, bad_pick: bool = False):
        self.fail = fail or set()
        self.bad_pick = bad_pick
        self.prompts: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str) -> dict:
        self.prompts.append((system, user))
        if "portfolio manager" in system:
            pm = _portfolio()
            if self.bad_pick:
                pm = {**pm, "picks": [{"ticker": "XYZ", "action": "buy", "confidence": 50, "reason": "x"}]}
            return pm
        ticker = json.loads(user)["ticker"]
        if ticker in self.fail:
            raise LLMError(f"model timed out for {ticker}")
        return _analyst(ticker)


def _bundle(t: str) -> dict:
    return {"ticker": t, "is_focus": t == "COF", "group": "Large banks", "signal": None,
            "price": {"as_of": "2026-10-16", "last": 201.74}, "forecast_horizon_days": 20,
            "research_options": [{"key": "branch_map", "shows": "map"}, {"key": "storm_history", "shows": "bars"}]}


def test_analyst_output_must_match_schema():
    assert advisor.validate_analyst(_analyst("COF"), _bundle("COF"))["view"] == "neutral"
    with pytest.raises(ValueError, match="view"):
        advisor.validate_analyst({**_analyst("COF"), "view": "moon"}, _bundle("COF"))
    with pytest.raises(ValueError, match="ticker"):
        advisor.validate_analyst(_analyst("WMT"), _bundle("COF"))
    with pytest.raises(ValueError, match="confidence"):
        advisor.validate_analyst({**_analyst("COF"), "confidence": 140}, _bundle("COF"))


def test_forecast_is_anchored_to_the_bundle_price_and_horizon():
    f = advisor.validate_analyst(_analyst("COF"), _bundle("COF"))["forecast"]
    assert f == {"horizon_days": 20, "from_date": "2026-10-16", "from_price": 201.74,
                 "return_pct": 0.4, "low_pct": -3.0, "high_pct": 3.5}


@pytest.mark.parametrize("bad, match", [
    ({"return_pct": 1.0, "low_pct": 2.0, "high_pct": 3.0}, "low_pct <= return_pct <= high_pct"),
    ({"return_pct": 80.0, "low_pct": 0.0, "high_pct": 90.0}, "within"),
    ({"return_pct": "up", "low_pct": 0.0, "high_pct": 1.0}, "number"),
    (None, "forecast"),
])
def test_forecast_must_be_a_sane_range(bad, match):
    with pytest.raises(ValueError, match=match):
        advisor.validate_analyst({**_analyst("COF"), "forecast": bad}, _bundle("COF"))


def test_forecast_sign_must_agree_with_expected_move():
    up = {**_analyst("COF"), "expected_move": "up", "forecast": {"return_pct": -1.0, "low_pct": -4.0, "high_pct": 2.0}}
    with pytest.raises(ValueError, match="expected_move"):
        advisor.validate_analyst(up, _bundle("COF"))


def test_research_picks_must_come_from_the_offered_panels():
    assert advisor.validate_analyst(_analyst("COF"), _bundle("COF"))["research"] == ["branch_map"]
    with pytest.raises(ValueError, match="flood_map"):
        advisor.validate_analyst({**_analyst("COF"), "research": ["flood_map"]}, _bundle("COF"))
    deduped = advisor.validate_analyst({**_analyst("COF"), "research": ["storm_history", "storm_history"]}, _bundle("COF"))
    assert deduped["research"] == ["storm_history"]


def test_one_failing_analyst_does_not_stop_the_others():
    results = advisor.run_analysts([_bundle(t) for t in UNIVERSE], ScriptedClient(fail={"WMT"}), max_workers=2)
    assert results["WMT"]["status"] == "error" and "timed out" in results["WMT"]["error"]
    assert results["COF"]["status"] == "ok" and results["COF"]["analysis"]["view"] == "neutral"
    assert results["JPM"]["status"] == "ok"


def test_portfolio_picks_must_be_covered_tickers():
    with pytest.raises(ValueError, match="XYZ"):
        advisor.validate_portfolio(
            {**_portfolio(), "picks": [{"ticker": "XYZ", "action": "buy", "confidence": 50, "reason": "x"}]}, UNIVERSE, "COF")


def test_focus_must_be_capital_one():
    with pytest.raises(ValueError, match="focus"):
        advisor.validate_portfolio({**_portfolio(), "focus": {**_portfolio()["focus"], "ticker": "JPM"}}, UNIVERSE, "COF")


def test_full_run_is_ok_and_keeps_failures_visible():
    client = ScriptedClient(fail={"WMT"})
    out = advisor.run(bundles=[_bundle(t) for t in UNIVERSE], context={"focus": "COF"}, client=client,
                      model="qwen3", focus="COF", max_workers=2, generated_utc="2026-10-18T00:00:00Z")
    assert out["status"] == "ok" and out["error"] is None
    assert out["portfolio"]["focus"]["ticker"] == "COF"
    assert out["analysts"]["WMT"]["status"] == "error"
    assert out["model"] == "qwen3" and out["evidence_hash"]
    # the focus company is explained in the portfolio prompt, with the evidence the agent must weigh
    pm_user = next(u for s, u in client.prompts if "portfolio manager" in s)
    assert "Capital One" in next(s for s, _ in client.prompts if "portfolio manager" in s)
    assert "COF" in pm_user


def test_run_without_any_analyst_output_is_an_error_not_a_portfolio():
    client = ScriptedClient(fail=set(UNIVERSE))
    out = advisor.run(bundles=[_bundle(t) for t in UNIVERSE], context={}, client=client,
                      model="qwen3", focus="COF", max_workers=2, generated_utc="t")
    assert out["status"] == "error" and out["portfolio"] is None
    assert "no analyst" in out["error"].lower()


def test_portfolio_failure_keeps_analyst_views():
    client = ScriptedClient(bad_pick=True)
    out = advisor.run(bundles=[_bundle(t) for t in UNIVERSE], context={}, client=client,
                      model="qwen3", focus="COF", max_workers=1, generated_utc="t")
    assert out["status"] == "error" and out["portfolio"] is None
    assert out["analysts"]["COF"]["status"] == "ok"
    assert "XYZ" in out["error"]


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "advice.json"
    assert advisor.load(path) is None
    advice = {"status": "ok", "analysts": {}, "portfolio": None, "error": None}
    advisor.save(advice, path)
    assert advisor.load(path) == advice
