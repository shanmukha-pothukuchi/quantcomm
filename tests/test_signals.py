import numpy as np
import pandas as pd
import pytest

from floodalpha.market import signals as sg

IDX = pd.bdate_range("2019-01-01", periods=800)
CFG = {"defaults": {"bank": "flood_deposits", "etf": "benchmark", "insurer": "weather_alerts"},
       "tickers": {"COF": {"signal": "branch_network"}},
       "retail": {"WMT": {"name": "Walmart Inc.", "signal": "parking_lot_sar"}},
       "parking_sar": {"holiday_window": ["11-15", "12-31"], "yoy_window_days": 91, "min_scenes_per_window": 4,
                       "benchmark": "SPY", "publication_lag_days": 3, "forward_days": 63, "significance": 0.1}}


def _mapping():
    return pd.DataFrame({"company": ["Capital One", "Big Bank"], "ticker": ["COF", "BIG"],
                         "namehcr_pattern": ["^CAPITAL ONE", "^BIG HC"], "match_on": ["hc", "hc"],
                         "rssdhcr": [10, 100], "deposits_2024_k": [3e8, 9e8], "status": ["active", "active"]})


def _prices():
    rng = np.random.default_rng(2)
    return pd.DataFrame({t: 50 * np.exp(np.cumsum(rng.normal(0, .01, len(IDX)))) for t in ("COF", "BIG", "KRE", "SPY", "TRV", "WMT")},
                        index=IDX)


def test_signal_type_routes_by_company():
    assert sg.signal_type("COF", "Large banks", CFG) == "branch_network"
    assert sg.signal_type("WMT", "Retail", CFG) == "parking_lot_sar"
    assert sg.signal_type("BIG", "Large banks", CFG) == "flood_deposits"
    assert sg.signal_type("TRV", "Insurers", CFG) == "weather_alerts"
    assert sg.signal_type("SPY", "ETFs", CFG) == "benchmark"


def test_bank_alert_detail_sums_certs_per_ticker():
    exp = pd.DataFrame({"cert": [1, 2], "bank_name": ["Big A", "Big B"], "holding_company": ["BIG HC", "BIG HC"],
                        "rssdhcr": [100, 100], "n_branches_in_alert": [2, 3], "deposits_in_alert_k": [10.0, 30.0],
                        "total_deposits_k": [100.0, 300.0], "alert_events": ["Flood Warning", "Coastal Flood Watch;Flood Warning"]})
    d = sg.bank_alert_detail(exp, _mapping())
    assert d.loc["BIG", "n_branches_in_alert"] == 5
    assert d.loc["BIG", "exposure_pct"] == pytest.approx(10.0)
    assert d.loc["BIG", "alert_events"] == "Coastal Flood Watch;Flood Warning"
    assert sg.bank_alert_detail(None, _mapping()).empty


def test_storm_history_uses_close_before_landfall():
    px = pd.DataFrame({"X": np.r_[np.ones(10), np.full(10, 0.9)], "B": np.ones(20)}, index=pd.bdate_range("2020-01-01", periods=20))
    storms = {"s": {"name": "S", "year": 2020, "landfall_time_utc": "2020-01-15T03:00:00Z"}}
    h = sg.storm_history(px, "X", "B", storms, days=3)
    assert h["n"] == 1 and h["rows"][0]["excess_pct"] == pytest.approx(np.log(0.9) * 100)
    assert sg.storm_history(px, "X", "B", {"late": {"name": "L", "landfall_time_utc": "2030-01-01T00:00:00Z"}})["n"] == 0


def test_build_signals_gives_each_company_its_own_source():
    meta = {"COF": {"group": "Large banks"}, "BIG": {"group": "Large banks"}, "TRV": {"group": "Insurers"},
            "SPY": {"group": "ETFs"}, "WMT": {"group": "Retail"}}
    nessie = pd.DataFrame({"kind": ["branch", "branch", "atm"], "state": ["VA", "MD", "VA"], "in_alert": [True, False, False]})
    port = pd.DataFrame({"in_alert": [True, False], "loan_amount": [1000.0, 5000.0]})
    live = {"generated_utc": "2026-10-18T03:23:21Z", "n_alerts_conus": 19,
            "alerts_by_event": {"Coastal Flood Watch": 5, "Flood Warning": 4}, "nhc_active_storms": [],
            "banks": {"states_checked": ["VA"]}}
    out = sg.build_signals(meta, CFG, _prices(), _mapping(), None, live, nessie, port, None, {}, {})
    assert out["COF"]["type"] == "branch_network" and out["COF"]["verdict"]["stance"] == "exposed"
    labels = {m["label"]: m["value"] for m in out["COF"]["metrics"]}
    assert labels["Nessie ATMs"] == 1 and labels["SYNTHETIC customers under alerts"] == "1/2"
    assert out["BIG"]["type"] == "flood_deposits" and out["BIG"]["verdict"]["stance"] == "clear"
    assert out["TRV"]["type"] == "weather_alerts"
    assert {m["label"]: m["value"] for m in out["TRV"]["metrics"]}["Coastal flood alerts"] == 5
    assert out["SPY"]["type"] == "benchmark"
    assert out["WMT"]["type"] == "parking_lot_sar" and out["WMT"]["verdict"]["stance"] == "no reading"


def test_metric_nan_becomes_none():
    assert sg._m("x", float("nan"))["value"] is None
