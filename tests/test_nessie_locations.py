import json

import pandas as pd
import pytest

from quantcomm import config
from quantcomm.nessie.client import build_locations


@pytest.fixture(scope="module")
def loc():
    return build_locations("isaias")


def test_locations_complete(loc):
    assert loc["lat"].notna().all() and loc["lon"].notna().all()
    assert loc["nessie_id"].is_unique
    assert set(loc["kind"]) == {"branch", "atm"}
    # mid-Atlantic footprint sanity
    assert loc["lat"].between(36, 41).all() and loc["lon"].between(-80, -74).all()


def test_locations_count_matches_raw_pull(loc):
    raw = config.DATA_RAW / "nessie"
    b = json.loads((raw / "branches.json").read_text())
    a = json.loads((raw / "atms.json").read_text())
    assert (loc["kind"] == "branch").sum() == len(b)
    assert (loc["kind"] == "atm").sum() == len(a)
