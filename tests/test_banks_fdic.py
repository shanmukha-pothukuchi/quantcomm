"""FDIC SOD normalisation / dedup and vintage rule. Frames here are tiny hand-built fixtures."""
import pandas as pd
import pytest

from floodalpha.banks.fdic import _normalize, sod_vintage_for


def _raw(rows):
    cols = ["CERT", "BRNUM", "YEAR", "NAMEFULL", "NAMEBR", "RSSDHCR", "NAMEHCR", "DEPSUMBR", "SIMS_LATITUDE", "SIMS_LONGITUDE", "STALPBR", "CNTYNAMB", "CITYBR", "BKCLASS", "BRSERTYP"]
    return pd.DataFrame(rows, columns=cols)


def test_normalize_types_and_zero_coordinates():
    df = _normalize(_raw([
        [1, 0, 2024, "A Bank", "Main", "123", "A HOLDING", "1000", 30.1, -86.5, "FL", "Okaloosa", "Destin", "N", 11],
        [1, 1, 2024, "A Bank", "Branch", None, None, None, 0, 0, "FL", "Okaloosa", "Destin", "N", 11],
    ]))
    assert df["cert"].dtype == "int64" and df["deposits_k"].tolist() == [1000.0, 0.0]
    assert df["rssdhcr"].tolist() == [123, 0] and df["namehcr"].tolist() == ["A HOLDING", ""]
    assert df["lat"].isna().tolist() == [False, True], "0/0 placeholder coordinates must become NaN"


def test_dedup_key_is_cert_brnum_year():
    df = _raw([[1, 0, 2024] + ["x"] * 4 + ["1", 1, 1, "FL", "c", "d", "N", 11]] * 2)
    assert len(df.drop_duplicates(subset=["CERT", "BRNUM", "YEAR"])) == 1


@pytest.mark.parametrize("storm,year", [("harvey", 2016), ("irma", 2016), ("florence", 2017), ("michael", 2017),
                                        ("laura", 2019), ("sally", 2019), ("ida", 2020), ("ian", 2021), ("idalia", 2022),
                                        ("helene", 2023), ("milton", 2023), ("isaias", 2026)])
def test_sod_vintage_rule(storm, year):
    assert sod_vintage_for(storm) == year
