import math

import numpy as np
import pandas as pd
import pytest

from quantcomm.market import stock_view as sv

IDX = pd.bdate_range("2025-01-01", periods=300)


def _mapping():
    return pd.DataFrame({
        "company": ["Big Bank", "Small Bank", "Gone Bank", "No HC Bank"],
        "ticker": ["BIG", "SML", "GONE", "NOHC"],
        "namehcr_pattern": ["^BIG HC", "^SMALL HC", "^GONE HC", "^NO HC BANK$"],
        "match_on": ["hc", "hc", "hc", "bank"],
        "rssdhcr": [100, 200, 300, 0],
        "deposits_2024_k": [900e6, 5e6, 1e6, 2e6],
        "status": ["active", "active", "delisted", "active"],
    })


def _prices():
    rng = np.random.default_rng(1)
    cols = {t: 50 * np.exp(np.cumsum(rng.normal(0, 0.01, len(IDX)))) for t in ("BIG", "SML", "NOHC", "SPY", "TRV")}
    px = pd.DataFrame(cols, index=IDX)
    px.iloc[:10, px.columns.get_loc("SML")] = np.nan  # late IPO
    return px


def test_meta_groups_and_drops_tickers_without_prices_or_delisted():
    m = sv.ticker_meta(_prices(), _mapping())
    assert m["BIG"]["group"] == "Large banks"
    assert m["SML"]["group"] == "Regional"
    assert m["SPY"]["group"] == "ETFs" and m["TRV"]["group"] == "Insurers"
    assert "GONE" not in m and "KRE" not in m  # delisted / no price column


def test_summarize_changes_and_1y_return():
    s = pd.Series(np.arange(1.0, 301.0), index=IDX)
    r = sv.summarize(s)
    assert r["last"] == 300 and r["prev"] == 299 and r["chg"] == 1
    assert r["chg_pct"] == pytest.approx(100 / 299)
    assert r["r1y_pct"] == pytest.approx((300 / (300 - 252) - 1) * 100)
    assert sv.summarize(pd.Series([np.nan, 5.0], index=IDX[:2])) is None


def test_summarize_short_history_has_no_1y_return():
    assert sv.summarize(pd.Series([1.0, 2.0, 3.0], index=IDX[:3]))["r1y_pct"] is None


def test_alert_exposure_aggregates_certs_of_one_hc_and_matches_bank_rows():
    exp = pd.DataFrame({
        "cert": [1, 2, 3, 4],
        "bank_name": ["Big Bank NA", "Big Bank Trust", "NO HC BANK", "Gone Bank NA"],
        "holding_company": ["BIG HC INC", "BIG HC INC", None, "GONE HC"],
        "rssdhcr": [100, 100, 0, 300],
        "deposits_in_alert_k": [10.0, 30.0, 5.0, 1.0],
        "total_deposits_k": [100.0, 300.0, 50.0, 10.0],
    })
    out = sv.alert_exposure_by_ticker(exp, _mapping())
    assert out["BIG"] == pytest.approx(10.0)  # (10+30)/(100+300)
    assert out["NOHC"] == pytest.approx(10.0)
    assert "GONE" not in out  # delisted HCs have no price
    assert sv.alert_exposure_by_ticker(None, _mapping()) == {}


def test_storm_events_use_first_post_landfall_publication():
    storms = {
        "isaias": {"name": "Isaias", "landfall_time_utc": "2025-06-10T01:30:00Z"},
        "old": {"name": "Old", "landfall_time_utc": "2019-01-01T00:00:00Z"},
    }
    cat = pd.DataFrame({
        "published_utc": pd.to_datetime(["2025-06-08T00:00Z", "2025-06-11T05:35Z", "2025-06-12T00:00Z"]),
        "is_post_landfall": [False, True, True],
    })
    ev = sv.storm_events(storms, cat, IDX.min(), IDX.max())
    assert [e["key"] for e in ev] == ["isaias"]
    assert ev[0]["event_time"].startswith("2025-06-11T05:35")


def test_payload_series_align_with_dates_and_nan_becomes_none():
    p = sv.build_payload(_prices(), _mapping(), None, {}, None)
    assert len(p["dates"]) == len(IDX) and p["dates"] == sorted(p["dates"])
    assert all(len(v) == len(IDX) for v in p["series"].values())
    assert p["series"]["SML"][0] is None and not any(isinstance(v, float) and math.isnan(v) for v in p["series"]["SML"])
    assert set(p["series"]) == set(p["summary"])
    assert p["as_of"] == IDX.max().strftime("%Y-%m-%d")
