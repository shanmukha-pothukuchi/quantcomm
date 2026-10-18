"""Evidence bundles: what an agent may see about one ticker, built only from cached payload fields."""
from __future__ import annotations

import pytest

from quantcomm.agent import evidence


def _payload() -> dict:
    return {
        "as_of": "2026-10-16",
        "events": [{"key": "isaias", "name": "Isaias", "landfall": "2026-10-10T01:30:00", "event_time": None}],
        "summary": {
            "COF": {"name": "Capital One Financial Corp.", "group": "Large banks", "last": 201.74, "prev": 198.86,
                    "chg": 2.88, "chg_pct": 1.45, "r1y_pct": -4.75, "last_date": "2026-10-16", "alert_exposure_pct": None},
            "KRE": {"name": "SPDR S&P Regional Banking ETF", "group": "ETFs", "last": 69.81, "prev": 69.75,
                    "chg": 0.06, "chg_pct": 0.09, "r1y_pct": 15.3, "last_date": "2026-10-16", "alert_exposure_pct": None},
        },
        "signals": {
            "COF": {"type": "branch_network", "title": "Capital One branches", "source": "Nessie",
                    "metrics": [{"label": "Branches/ATMs under alerts", "value": 0, "unit": "", "note": "of 220"},
                                {"label": "Nessie ATMs", "value": None, "unit": "", "note": ""}],
                    "verdict": {"stance": "clear", "reason": "no branch under alert"},
                    "caveats": ["sandbox footprint"],
                    "storm_history": [{"storm": "Harvey", "year": 2017, "excess_pct": -1.0},
                                      {"storm": "Irma", "year": 2017, "excess_pct": 2.0},
                                      {"storm": "Florence", "year": 2018, "excess_pct": -4.0}]},
        },
        "series": {},
    }


def test_bundle_carries_price_signal_and_past_storms():
    b = evidence.bundle("COF", _payload(), focus="COF")
    assert b["ticker"] == "COF" and b["is_focus"] is True and b["group"] == "Large banks"
    assert b["price"]["last"] == 201.74 and b["price"]["return_1y_pct"] == -4.75
    assert b["price"]["benchmark"] == {"ticker": "KRE", "return_1y_pct": 15.3}
    assert b["signal"]["stance"] == "clear" and b["signal"]["caveats"] == ["sandbox footprint"]
    # missing metric values stay visible as None so the agent can say the data is unavailable
    assert {"label": "Nessie ATMs", "value": None, "unit": "", "note": ""} in b["signal"]["metrics"]


def test_past_storm_summary_is_computed_from_history_rows():
    b = evidence.bundle("COF", _payload(), focus="COF")
    ps = b["past_storms"]
    assert ps["n"] == 3 and ps["n_underperformed"] == 2
    assert ps["mean_excess_pct"] == pytest.approx((-1.0 + 2.0 - 4.0) / 3, abs=1e-6)
    assert ps["window"] == "landfall to +5 trading days" and ps["bench"] == "KRE"


def test_history_rows_are_capped_to_the_most_recent_but_summary_covers_all(monkeypatch):
    monkeypatch.setattr(evidence, "HISTORY_ROWS", 2)
    ps = evidence.bundle("COF", _payload(), focus="COF")["past_storms"]
    assert ps["n"] == 3 and [r["storm"] for r in ps["rows"]] == ["Irma", "Florence"]


def test_retailers_are_compared_with_spy():
    p = _payload()
    p["summary"]["WMT"] = {**p["summary"]["COF"], "name": "Walmart", "group": "Retail"}
    p["signals"]["WMT"] = {**p["signals"]["COF"]}
    assert evidence.bundle("WMT", p, focus="COF")["past_storms"]["bench"] == "SPY"


def test_insurers_are_compared_with_spy():
    p = _payload()
    p["summary"]["ALL"] = {**p["summary"]["COF"], "name": "Allstate", "group": "Insurers"}
    p["summary"]["SPY"] = {**p["summary"]["KRE"], "name": "SPDR S&P 500 ETF"}
    p["signals"]["ALL"] = {**p["signals"]["COF"]}
    assert evidence.bundle("ALL", p, focus="COF")["past_storms"]["bench"] == "SPY"


def test_ticker_without_signal_block_says_so():
    p = _payload()
    p["signals"] = {}
    b = evidence.bundle("COF", p, focus="COF")
    assert b["signal"] is None and "no signal block" in b["data_gaps"]


def test_context_carries_focus_and_live_counts():
    live = {"n_alerts_conus": 19, "alerts_by_event": {"Coastal Flood Watch": 14}, "nhc_active_storms": [{"name": "Trudy", "basin": "east_pacific"}]}
    ctx = evidence.context(_payload(), live, focus="COF")
    assert ctx["as_of"] == "2026-10-16" and ctx["focus"] == "COF"
    assert ctx["live_flood_alerts"] == 19 and ctx["active_tropical_storms"] == ["Trudy"]
    assert ctx["landfalls_in_price_history"] == [{"storm": "Isaias", "landfall": "2026-10-10T01:30:00"}]
    assert evidence.context(_payload(), None, focus="COF")["live_flood_alerts"] is None


def test_hash_is_stable_and_follows_the_evidence():
    p = _payload()
    ctx = evidence.context(p, None, focus="COF")
    bundles = [evidence.bundle("COF", p, focus="COF")]
    h1 = evidence.evidence_hash(bundles, ctx, "v1")
    assert h1 == evidence.evidence_hash([evidence.bundle("COF", p, focus="COF")], ctx, "v1")
    assert h1 != evidence.evidence_hash(bundles, ctx, "v2")  # prompt revisions invalidate cached advice
    p["summary"]["COF"]["last"] = 150.0
    assert h1 != evidence.evidence_hash([evidence.bundle("COF", p, focus="COF")], ctx, "v1")


def test_bundle_offers_research_panels_that_fit_the_signal():
    b = evidence.bundle("COF", _payload(), focus="COF")
    keys = [o["key"] for o in b["research_options"]]
    assert keys == ["branch_map", "customer_stress", "flood_map", "storm_history"]
    assert all(o["shows"] for o in b["research_options"])
    p = _payload()
    p["signals"]["COF"] = {**p["signals"]["COF"], "storm_history": []}
    assert "storm_history" not in [o["key"] for o in evidence.bundle("COF", p, focus="COF")["research_options"]]
    p["signals"] = {}
    assert evidence.bundle("COF", p, focus="COF")["research_options"] == []


def test_research_options_by_signal_type():
    hist = [{"storm": "Ian", "year": 2022, "excess_pct": 1.0}]
    assert evidence.research_options({"type": "parking_lot_sar"}) == ["parking_lot"]
    assert evidence.research_options({"type": "flood_deposits", "storm_history": hist}) == \
        ["alert_exposure", "alerts_map", "flood_map", "storm_history"]
    assert evidence.research_options({"type": "weather_alerts", "storm_history": hist})[0] == "alerts_map"
    assert evidence.research_options({"type": "benchmark"}) == []
    assert evidence.research_options(None) == []


def test_bundle_asks_for_a_forecast_over_the_configured_horizon():
    assert evidence.bundle("COF", _payload(), focus="COF", horizon_days=15)["forecast_horizon_days"] == 15
