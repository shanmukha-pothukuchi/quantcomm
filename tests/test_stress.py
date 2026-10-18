import numpy as np
import pandas as pd
import pytest

from quantcomm import config
from quantcomm.nessie.stress import apply_stress


@pytest.fixture
def params():
    return config.model()["stress"]


@pytest.fixture
def port():
    # SYNTHETIC test-only fixture (never written to data/processed)
    return pd.DataFrame({
        "customer_key": ["A", "B", "C"], "nessie_customer_id": pd.array([None] * 3, dtype="string"),
        "group": ["affected", "affected", "control"], "geoid": ["1", "2", "3"],
        "county_fips": ["12001"] * 3, "state": ["FL"] * 3, "lat": [30.0] * 3, "lon": [-86.0] * 3,
        "annual_income": [60000.0] * 3, "checking_balance": [1000.0, 2000.0, 3000.0],
        "savings_balance": [500.0] * 3, "loan_amount": [10000.0, 20000.0, 30000.0],
        "loan_rate_pct": [9.5] * 3, "monthly_bills": [800.0] * 3,
    })


def test_el_math_and_multiplier(port, params):
    tf = pd.DataFrame({"geoid": ["1", "2", "3"], "tract_area_m2": [1e6] * 3,
                       "flooded_area_m2": [5e4, 0.0, 5e5], "pct_flooded": [5.0, 0.0, 50.0],
                       "sar_covered": [True, True, False], "fema_declared": [True, True, False]})
    res = apply_stress(port, tf, params)
    base, mult, lgd = params["baseline_pd_12m"], params["flooded_pd_multiplier"], params["lgd"]
    assert res["is_flooded"].tolist() == [True, False, False]  # C flooded but not SAR-covered
    assert res.loc[0, "expected_loss"] == pytest.approx(10000 * base * mult * lgd)
    assert res.loc[1, "expected_loss"] == pytest.approx(20000 * base * lgd)
    assert res.loc[0, "withdrawal_shock"] == pytest.approx(1000 * params["flooded_withdrawal_shock_pct"] / 100)
    assert res.loc[1, "withdrawal_shock"] == 0


def test_missing_tract_flood(port, params):
    for tf in (None, pd.DataFrame(columns=["geoid", "pct_flooded", "sar_covered"])):
        res = apply_stress(port, tf, params)
        assert not res["is_flooded"].any()
        assert (res["pd_12m"] == params["baseline_pd_12m"]).all()
