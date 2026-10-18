"""Integrity of config/bank_tickers.csv and the matching rules."""
from pathlib import Path

import pandas as pd
import pytest

from quantcomm import config
from quantcomm.banks import tickers

SOD24 = config.DATA_RAW / "fdic" / "sod_2024.parquet"
PRICES = config.DATA_RAW / "market" / "prices.parquet"


@pytest.fixture(scope="module")
def mapping():
    return tickers.load()


def test_no_duplicate_active_tickers_or_companies(mapping):
    act = mapping[mapping["status"] == "active"]
    assert act["ticker"].is_unique, act[act["ticker"].duplicated(keep=False)]
    assert mapping["company"].is_unique
    assert (act["ticker"] != "").all()


def test_delisted_rows_have_no_active_ticker(mapping):
    d = mapping[mapping["status"] == "delisted"]
    assert len(d) >= 15 and (d["yf_ok"] == False).all()  # noqa: E712


@pytest.mark.skipif(not SOD24.exists(), reason="SOD 2024 cache not pulled")
def test_every_active_row_matches_2024_sod(mapping):
    sod = pd.read_parquet(SOD24)
    got = tickers.assign_tickers(sod, mapping)
    matched = set(got.loc[got["ticker_status"] == "active", "ticker"])
    active = set(mapping.loc[mapping["status"] == "active", "ticker"])
    assert active - matched == set(), f"active tickers with no 2024 branches: {active - matched}"


@pytest.mark.skipif(not SOD24.exists(), reason="SOD 2024 cache not pulled")
def test_same_name_small_holding_companies_are_not_matched(mapping):
    sod = pd.read_parquet(SOD24)
    got = tickers.assign_tickers(sod, mapping)
    cbsh = got[got["ticker"] == "CBSH"]
    assert cbsh["rssdhcr"].nunique() == 1, "CBSH must map to exactly one holding company"
    fcnca = got[got["ticker"] == "FCNCA"]
    assert fcnca["rssdhcr"].nunique() == 1


@pytest.mark.skipif(not PRICES.exists(), reason="prices cache not downloaded")
def test_every_active_ticker_has_2024_prices(mapping):
    px = pd.read_parquet(PRICES)
    y = px.loc["2024-01-01":"2024-12-31"]
    missing = [t for t in mapping.loc[mapping["status"] == "active", "ticker"] if t not in y.columns or y[t].notna().sum() < 200]
    assert missing == [], missing


def test_rssdhcr_wins_over_name():
    sod = pd.DataFrame({"cert": [1, 2, 3], "brnum": [0, 0, 0], "bank_name": ["Truist Bank", "Commerce Bank", "Bank of Commerce"],
                        "rssdhcr": [1074156, 1049341, 999], "namehcr": ["BB&T CORPORATION", "COMMERCE BANCSHARES, INC.", "COMMERCE BANCSHARES, INC."],
                        "deposits_k": [100.0, 50.0, 1.0]})
    m = pd.DataFrame([{"company": "Truist", "ticker": "TFC", "namehcr_pattern": "^TRUIST", "match_on": "hc", "rssdhcr": 1074156, "status": "active"},
                      {"company": "Commerce", "ticker": "CBSH", "namehcr_pattern": "^COMMERCE BANCSHARES", "match_on": "hc", "rssdhcr": 0, "status": "active"}])
    got = tickers.assign_tickers(sod, m)
    assert got["ticker"].tolist() == ["TFC", "CBSH", ""], got["ticker"].tolist()
