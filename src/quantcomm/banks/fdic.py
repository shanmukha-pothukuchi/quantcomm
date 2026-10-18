"""FDIC Summary of Deposits (SOD) branches with deposits and coordinates, cached per vintage year.

API: https://api.fdic.gov/banks/sod  (max limit 10,000 per page, offset paging; verified 2026-10-17)
DEPSUMBR is in $ thousands. Vintage = June 30 of YEAR, published late Sept / early Oct.
"""
from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from quantcomm import config

log = logging.getLogger(__name__)
SOD_URL = "https://api.fdic.gov/banks/sod"
INST_URL = "https://api.fdic.gov/banks/institutions"
FIELDS = "CERT,NAMEFULL,BRNUM,NAMEBR,ADDRESBR,CITYBR,STALPBR,ZIPBR,CNTYNAMB,STCNTYBR,DEPSUMBR,SIMS_LATITUDE,SIMS_LONGITUDE,RSSDHCR,NAMEHCR,YEAR,BKCLASS,BRSERTYP"
PAGE = 10000
RAW = config.DATA_RAW / "fdic"


def sod_year(year: int, force: bool = False) -> pd.DataFrame:
    """All US SOD branch records for one vintage year. Cached as parquet; de-duplicated on (CERT, BRNUM, YEAR)."""
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / f"sod_{year}.parquet"
    if cache.exists() and not force:
        return pd.read_parquet(cache)
    rows, offset, total = [], 0, None
    while True:
        r = requests.get(SOD_URL, params={"filters": f"YEAR:{year}", "fields": FIELDS, "limit": PAGE, "offset": offset}, timeout=300)
        r.raise_for_status()
        d = r.json()
        if "data" not in d:
            raise RuntimeError(f"FDIC SOD {year}: {str(d)[:300]}")
        total = d["meta"]["total"]
        rows += [x["data"] for x in d["data"]]
        offset += PAGE
        if offset >= total or not d["data"]:
            break
    if total == 0:
        raise RuntimeError(f"FDIC SOD has no rows for YEAR:{year}")
    df = pd.DataFrame(rows)
    if len(df) != total:
        raise RuntimeError(f"FDIC SOD {year}: fetched {len(df)} rows but meta.total={total}")
    n0 = len(df)
    df = df.drop_duplicates(subset=["CERT", "BRNUM", "YEAR"])
    if len(df) != n0:
        log.warning("SOD %d: dropped %d duplicate (CERT,BRNUM,YEAR) rows", year, n0 - len(df))
    df = _normalize(df)
    df.to_parquet(cache, index=False)
    return df


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "cert": df["CERT"].astype("int64"),
        "brnum": df["BRNUM"].astype("int64"),
        "year": df["YEAR"].astype("int64"),
        "bank_name": df["NAMEFULL"].astype("string"),
        "branch_name": df["NAMEBR"].astype("string"),
        "rssdhcr": pd.to_numeric(df["RSSDHCR"], errors="coerce").fillna(0).astype("int64"),
        "namehcr": df["NAMEHCR"].fillna("").astype("string"),
        "deposits_k": pd.to_numeric(df["DEPSUMBR"], errors="coerce").fillna(0.0).astype("float64"),
        "lat": pd.to_numeric(df.get("SIMS_LATITUDE"), errors="coerce").astype("float64"),
        "lon": pd.to_numeric(df.get("SIMS_LONGITUDE"), errors="coerce").astype("float64"),
        "state": df["STALPBR"].astype("string"),
        "county": df["CNTYNAMB"].astype("string"),
        "city": df["CITYBR"].astype("string"),
        "bkclass": df.get("BKCLASS", pd.Series([""] * len(df))).astype("string"),
        "brsertyp": pd.to_numeric(df.get("BRSERTYP"), errors="coerce"),
    })
    # SIMS coordinates of exactly 0 are placeholders, not the Gulf of Guinea
    bad = (out["lat"] == 0) & (out["lon"] == 0)
    out.loc[bad, ["lat", "lon"]] = float("nan")
    return out


def sod_vintage_for(storm_key: str) -> int:
    """SOD year available (publicly released) before the storm's event. See ASSUMPTIONS.md."""
    st = config.storm(storm_key)
    if st.get("sod_year"):
        return int(st["sod_year"])
    lf = pd.Timestamp(st["landfall_time_utc"])
    if storm_key == "isaias":
        return 2026  # verified: 2026 SOD index created 2026-09-18, before the 2026-10-10 landfall
    return lf.year if lf >= pd.Timestamp(f"{lf.year}-10-15", tz="UTC") else lf.year - 1


def branches_for_storm(storm_key: str) -> gpd.GeoDataFrame:
    """All US branches for the storm's SOD vintage as a GeoDataFrame (EPSG:4326); rows without coordinates kept with empty geometry."""
    year = sod_vintage_for(storm_key)
    df = sod_year(year)
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")
    return g


def institutions(certs: list[int]) -> pd.DataFrame:
    """Institution-level attributes (holding company, total deposits) for a list of certs."""
    out = []
    for i in range(0, len(certs), 100):
        chunk = certs[i:i + 100]
        flt = " OR ".join(f"CERT:{c}" for c in chunk)
        r = requests.get(INST_URL, params={"filters": flt, "fields": "CERT,NAME,RSSDHCR,NAMEHCR,ASSET,DEP,ACTIVE,STALP,OFFICES", "limit": 1000}, timeout=120)
        r.raise_for_status()
        out += [x["data"] for x in r.json()["data"]]
    return pd.DataFrame(out)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    for y in sys.argv[1:]:
        d = sod_year(int(y))
        print(y, len(d), "rows;", d["lat"].isna().sum(), "without coordinates;", d["cert"].nunique(), "banks;", round(d["deposits_k"].sum() / 1e6, 1), "$bn")
