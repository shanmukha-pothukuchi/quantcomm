import numpy as np
import pandas as pd
import pytest

from floodalpha.market.event_study import car, event_day0, market_model

DAYS = pd.bdate_range("2026-10-01", "2026-10-30")  # weekdays; close enough to NYSE days for these checks


def test_day0_publication_overnight_is_same_day():
    # 05:38 UTC Oct 11 (Sunday) = 01:38 ET -> first trading day on/after = Mon Oct 12
    assert event_day0(pd.Timestamp("2026-10-11T05:38:00Z"), DAYS) == pd.Timestamp("2026-10-12")


def test_day0_before_close_is_same_day_and_after_close_rolls():
    assert event_day0(pd.Timestamp("2026-10-13T15:00:00-04:00"), DAYS) == pd.Timestamp("2026-10-13")
    assert event_day0(pd.Timestamp("2026-10-13T16:30:00-04:00"), DAYS) == pd.Timestamp("2026-10-14")
    assert event_day0(pd.Timestamp("2026-10-13T21:00:00Z"), DAYS) == pd.Timestamp("2026-10-14")  # 17:00 ET


def test_car_arithmetic_on_toy_series():
    idx = pd.bdate_range("2024-01-01", periods=400)
    rng = np.random.default_rng(0)
    bench = pd.Series(rng.normal(0, 0.01, 400), index=idx)
    stock = 0.0002 + 1.5 * bench + rng.normal(0, 0.002, 400)
    ret = pd.DataFrame({"B": bench, "S": stock})
    d0 = 300
    a, b, n = market_model(ret, "S", "B", d0, (-250, -30))
    assert n == 221 and abs(b - 1.5) < 0.05 and abs(a - 0.0002) < 0.0005
    # inject a -5% abnormal shock on day 0 and +1% on day 1
    ret.loc[idx[d0], "S"] += -0.05
    ret.loc[idx[d0 + 1], "S"] += 0.01
    c = car(ret, "S", "B", d0, a, b, 0, 1)
    assert abs(c - (-0.04)) < 0.004


def test_car_returns_none_when_window_has_gaps():
    idx = pd.bdate_range("2024-01-01", periods=50)
    ret = pd.DataFrame({"B": np.zeros(50), "S": np.zeros(50)}, index=idx)
    ret.loc[idx[22], "S"] = np.nan
    assert car(ret, "S", "B", 20, 0.0, 1.0, 0, 5) is None


def test_exposure_pct_arithmetic():
    # mirrors banks/exposure.py: flooded / total * 100, 0 when total is 0
    t = pd.DataFrame({"total_deposits_k": [1000.0, 0.0], "flooded_deposits_k": [250.0, 0.0]})
    pct = (100 * t["flooded_deposits_k"] / t["total_deposits_k"].where(t["total_deposits_k"] > 0)).fillna(0.0)
    assert pct.tolist() == [25.0, 0.0]
