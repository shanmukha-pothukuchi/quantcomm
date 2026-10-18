import pytest

from floodalpha.census.acs import load_tracts


@pytest.fixture(scope="module")
def tracts():
    return load_tracts("isaias")


def test_tracts_unique_and_states(tracts):
    assert not tracts["geoid"].duplicated().any()
    assert set(tracts["state_fips"]) == {"01", "12", "13"}
    assert (tracts["geoid"].str.len() == 11).all()


def test_tracts_crs_and_income(tracts):
    assert tracts.crs.to_epsg() == 4326
    assert tracts["median_hh_income"].isna().mean() < 0.10
    assert (tracts["median_hh_income"].dropna() > 0).all()
    assert tracts["county_name"].notna().all()
    assert (tracts["county_fips"] == tracts["geoid"].str[:5]).all()
