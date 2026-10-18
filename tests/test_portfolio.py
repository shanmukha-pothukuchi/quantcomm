import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

from floodalpha import config
from floodalpha.census.acs import load_tracts
from floodalpha.nessie.portfolio import fema_declared_counties, generate_portfolio


@pytest.fixture(scope="module")
def port():
    return generate_portfolio("isaias")


def test_portfolio_size_and_groups(port):
    cfg = config.model()["portfolio"]
    assert len(port) == cfg["n_customers"]
    assert set(port["group"]) == {"affected", "control"}
    assert port["customer_key"].is_unique
    assert port["customer_key"].str.startswith(cfg["nickname_prefix"]).all()
    assert (port["loan_amount"] > 0).all() and (port["annual_income"] > 0).all()


def test_groups_match_fema_declarations(port):
    declared = set(fema_declared_counties("isaias")["county_fips"])
    assert port.loc[port.group == "affected", "county_fips"].isin(declared).all()
    assert not port.loc[port.group == "control", "county_fips"].isin(declared).any()


def test_points_inside_their_tracts(port):
    tracts = load_tracts("isaias").set_index("geoid")
    sample = port.sample(50, random_state=1)
    for r in sample.itertuples():
        assert tracts.loc[r.geoid, "geometry"].contains(Point(r.lon, r.lat)), r.customer_key


def test_reproducible(tmp_path, monkeypatch, port):
    monkeypatch.setattr(config, "DATA_PROCESSED", tmp_path)
    again = generate_portfolio("isaias", force=True)
    # nessie_customer_id is filled in by push_to_nessie after generation; everything else must be identical
    drop = ["nessie_customer_id"]
    pd.testing.assert_frame_equal(port.drop(columns=drop).reset_index(drop=True), again.drop(columns=drop).reset_index(drop=True))
