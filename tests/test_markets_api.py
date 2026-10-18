"""Markets app API: chart-first payloads, cached agent advice, honest states when nothing has run yet."""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantcomm.agent import advisor
from quantcomm.agent.llm import LLMError
from quantcomm.web import markets, server, stocks


def _payload() -> dict:
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-01", periods=5)]
    return {
        "dates": dates, "as_of": dates[-1], "alerts_generated": None, "events": [
            {"key": "isaias", "name": "Isaias", "landfall": "2026-10-10T01:30:00", "event_time": None}],
        "series": {"COF": [100, 101, None, 103, 104], "KRE": [50, 50, 51, 52, 53], "JPM": [1, 2, 3, 4, 5]},
        "summary": {
            "COF": {"name": "Capital One Financial Corp.", "group": "Large banks", "last": 104.0, "prev": 103.0,
                    "chg": 1.0, "chg_pct": 0.97, "r1y_pct": None, "last_date": dates[-1], "alert_exposure_pct": None},
            "KRE": {"name": "SPDR S&P Regional Banking ETF", "group": "ETFs", "last": 53.0, "prev": 52.0,
                    "chg": 1.0, "chg_pct": 1.9, "r1y_pct": 15.3, "last_date": dates[-1], "alert_exposure_pct": None},
            "JPM": {"name": "JPMorgan Chase", "group": "Large banks", "last": 5.0, "prev": 4.0, "chg": 1.0,
                    "chg_pct": 25.0, "r1y_pct": 13.0, "last_date": dates[-1], "alert_exposure_pct": 0.03},
        },
        "signals": {"COF": {"type": "branch_network", "title": "Capital One branches", "source": "Nessie",
                            "metrics": [{"label": "Branches/ATMs under alerts", "value": 0, "unit": "", "note": "of 220"}],
                            "verdict": {"stance": "clear", "reason": "none"}, "caveats": [], "storm_history": []}},
    }


class FakeClient:
    model = "fake-model"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def chat_json(self, system, user):
        if self.fail:
            raise LLMError("upstream down")
        if "portfolio manager" in system:
            return {"headline": "Hold COF", "focus": {"ticker": "COF", "action": "hold", "confidence": 50,
                                                       "explanation": "clear", "what_would_change_my_mind": "alert"},
                    "picks": [{"ticker": "JPM", "action": "buy", "confidence": 52, "reason": "steady"}],
                    "risks": [], "method_note": "m"}
        import json as _j
        t = _j.loads(user)["ticker"]
        return {"ticker": t, "view": "neutral", "confidence": 50, "expected_move": "flat", "headline": "h",
                "reasoning": "r", "evidence_used": [], "data_gaps": []}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(stocks, "payload", lambda: _payload())
    monkeypatch.setattr(markets, "ADVICE_PATH", tmp_path / "advice.json")
    monkeypatch.setattr(markets, "CONFIG_UNIVERSE", ["COF", "JPM"])
    monkeypatch.setattr(markets, "FOCUS", "COF")
    monkeypatch.setenv("FLOODALPHA_DATA_DIR", str(tmp_path))
    return TestClient(server.create_app())


def test_markets_list_marks_focus_and_agent_universe(api):
    body = api.get("/api/markets").json()
    assert body["success"] and body["data"]["focus"] == "COF" and body["data"]["universe"] == ["COF", "JPM"]
    tickers = {t["ticker"]: t for t in body["data"]["tickers"]}
    assert set(tickers) == {"COF", "KRE", "JPM"} and tickers["COF"]["has_signal"] is True
    assert tickers["KRE"]["has_signal"] is False


def test_ticker_detail_has_series_benchmark_events_and_signal(api):
    d = api.get("/api/markets/COF").json()["data"]
    assert d["name"] == "Capital One Financial Corp." and d["focus"] is True
    assert d["dates"][0] == "2026-10-01" and d["closes"] == [100, 101, None, 103, 104]
    assert d["benchmark"]["ticker"] == "KRE" and d["benchmark"]["closes"][0] == 50
    assert d["events"][0]["name"] == "Isaias"
    assert d["signal"]["metrics"][0]["label"] == "Branches/ATMs under alerts"


def test_unknown_ticker_is_a_404_envelope(api):
    r = api.get("/api/markets/NOPE")
    assert r.status_code == 404 and r.json()["success"] is False


def test_advice_before_any_run_says_so(api):
    d = api.get("/api/advice").json()["data"]
    assert d["available"] is False and "run_advice" in d["hint"]


def test_advice_run_needs_llm_settings(api, monkeypatch):
    def no_settings(cls):
        raise LLMError("LLM_BASE_URL, LLM_API_KEY, LLM_MODEL not set; add to .env")

    monkeypatch.setattr(markets.LLMClient, "from_env", classmethod(no_settings))
    r = api.post("/api/advice/run")
    assert r.status_code == 503 and "LLM_BASE_URL" in r.json()["error"]


def test_advice_run_caches_and_goes_stale_when_prices_move(api, monkeypatch):
    monkeypatch.setattr(markets.LLMClient, "from_env", classmethod(lambda cls: FakeClient()))
    run = api.post("/api/advice/run")
    assert run.status_code == 200 and run.json()["data"]["status"] == "ok"
    fresh = api.get("/api/advice").json()["data"]
    assert fresh["available"] is True and fresh["stale"] is False
    assert fresh["advice"]["portfolio"]["focus"]["ticker"] == "COF"
    # new prices -> the cached analysis no longer matches the evidence it was built from
    p = _payload()
    p["summary"]["COF"]["last"] = 1.0
    monkeypatch.setattr(stocks, "payload", lambda: p)
    assert api.get("/api/advice").json()["data"]["stale"] is True


def test_failed_run_is_saved_as_an_error_with_no_portfolio(api, monkeypatch):
    monkeypatch.setattr(markets.LLMClient, "from_env", classmethod(lambda cls: FakeClient(fail=True)))
    r = api.post("/api/advice/run").json()["data"]
    assert r["status"] == "error" and r["portfolio"] is None
    assert advisor.load(markets.ADVICE_PATH)["status"] == "error"


def test_pages_route_to_the_markets_app(api):
    for url in ("/", "/stocks"):
        r = api.get(url)
        assert r.status_code == 200 and "Capital One" in r.text and 'id="app"' in r.text
    assert "Quantcomm" in api.get("/about").text
