"""ACS 5-year median household income (B19013) by census tract + TIGER tract geometry.

Keyless path: the ACS table-based summary file on www2.census.gov (verified 2026-10-17; the
api.census.gov endpoint now redirects to a "Missing Key" page without CENSUS_API_KEY).
If CENSUS_API_KEY is set, the API is used instead.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from quantcomm import config, schemas

log = logging.getLogger(__name__)

ACS_VINTAGE = "2023_5yr"
ACS_YEAR = 2023
SF_BASE = f"https://www2.census.gov/programs-surveys/acs/summary_file/{ACS_YEAR}/table-based-SF"
B19013_URL = f"{SF_BASE}/data/5YRData/acsdt5y{ACS_YEAR}-b19013.dat"
GEOS_URL = f"{SF_BASE}/documentation/Geos{ACS_YEAR}5YR.txt"
TIGER_YEAR = 2024
TIGER_TRACT = f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/TRACT/tl_{TIGER_YEAR}_{{fips}}_tract.zip"
TIGER_COUNTY = f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/COUNTY/tl_{TIGER_YEAR}_us_county.zip"

STATE_FIPS = {"AL": "01", "FL": "12", "GA": "13", "MS": "28", "LA": "22", "TX": "48", "SC": "45", "NC": "37",
              "VA": "51", "TN": "47", "MD": "24", "DE": "10", "NJ": "34", "NY": "36", "CT": "09", "PA": "42", "DC": "11", "KY": "21"}
FIPS_STATE = {v: k for k, v in STATE_FIPS.items()}

RAW = config.DATA_RAW / "census"


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    log.info("downloading %s", url)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    tmp.rename(dest)
    return dest


def tract_income_summary_file(state_fips: list[str]) -> pd.DataFrame:
    """B19013_001E for every tract in the given states, from the keyless summary file."""
    dat = _download(B19013_URL, RAW / f"acsdt5y{ACS_YEAR}-b19013.dat")
    _download(GEOS_URL, RAW / f"Geos{ACS_YEAR}5YR.txt")  # cached for reference / validation
    df = pd.read_csv(dat, sep="|", dtype={"GEO_ID": "string"}, na_values=["", "."])
    if not {"GEO_ID", "B19013_E001"} <= set(df.columns):
        raise RuntimeError(f"unexpected B19013 summary file columns: {df.columns.tolist()}")
    tr = df[df["GEO_ID"].str.startswith("1400000US")].copy()
    tr["geoid"] = tr["GEO_ID"].str[9:]
    tr = tr[tr["geoid"].str[:2].isin(state_fips)]
    inc = pd.to_numeric(tr["B19013_E001"], errors="coerce")
    inc = inc.where(inc > 0)  # ACS jam values are large negatives (e.g. -666666666)
    out = pd.DataFrame({"geoid": tr["geoid"].astype("string").values, "median_hh_income": inc.values})
    if out.empty:
        raise RuntimeError("summary file parsed but returned zero tracts; check GEO_ID format")
    return out


def tract_income_api(state_fips: list[str], key: str) -> pd.DataFrame:
    rows = []
    for st in state_fips:
        r = requests.get(
            f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5",
            params={"get": "B19013_001E", "for": "tract:*", "in": f"state:{st}", "key": key},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        d = pd.DataFrame(data[1:], columns=data[0])
        d["geoid"] = d["state"] + d["county"] + d["tract"]
        inc = pd.to_numeric(d["B19013_001E"], errors="coerce")
        rows.append(pd.DataFrame({"geoid": d["geoid"].astype("string"), "median_hh_income": inc.where(inc > 0)}))
    return pd.concat(rows, ignore_index=True)


def tiger_tracts(state_fips: list[str]) -> gpd.GeoDataFrame:
    parts = []
    for st in state_fips:
        z = _download(TIGER_TRACT.format(fips=st), RAW / f"tl_{TIGER_YEAR}_{st}_tract.zip")
        g = gpd.read_file(f"zip://{z}")
        parts.append(g[["GEOID", "STATEFP", "COUNTYFP", "ALAND", "AWATER", "geometry"]])
    g = pd.concat(parts, ignore_index=True)
    g = gpd.GeoDataFrame(g, geometry="geometry", crs=parts[0].crs).to_crs(4326)
    return g


def counties() -> gpd.GeoDataFrame:
    z = _download(TIGER_COUNTY, RAW / f"tl_{TIGER_YEAR}_us_county.zip")
    g = gpd.read_file(f"zip://{z}")[["GEOID", "STATEFP", "COUNTYFP", "NAME", "NAMELSAD", "geometry"]]
    return g.to_crs(4326)


def build_tracts(storm_key: str, force: bool = False) -> gpd.GeoDataFrame:
    """Build data/processed/<storm>/tracts.parquet (TRACTS schema) for the storm's AOI states."""
    out = config.storm_dir(storm_key) / "tracts.parquet"
    if out.exists() and not force:
        return gpd.read_parquet(out)
    st = config.storm(storm_key)
    fips = [STATE_FIPS[s] for s in st["aoi_states"]]

    key = config.env("CENSUS_API_KEY")
    inc = tract_income_api(fips, key) if key else tract_income_summary_file(fips)
    if inc["geoid"].duplicated().any():
        raise RuntimeError("duplicate tract geoids in ACS income table")

    g = tiger_tracts(fips)
    n_before = len(g)
    cty = counties()[["GEOID", "NAME"]].rename(columns={"GEOID": "county_fips", "NAME": "county_name"})
    g["county_fips"] = g["STATEFP"] + g["COUNTYFP"]
    g = g.merge(cty, on="county_fips", how="left", validate="m:1")
    g = g.merge(inc, left_on="GEOID", right_on="geoid", how="left", validate="1:1")
    if len(g) != n_before:
        raise RuntimeError(f"tract join changed row count {n_before} -> {len(g)}")
    unmatched = g["geoid"].isna().sum()
    if unmatched:
        # TIGER 2024 vs ACS 2023 boundaries can differ for a handful of tracts; log, don't hide.
        log.warning("%d TIGER tracts had no ACS income row (left as NaN)", unmatched)
    g["geoid"] = g["GEOID"].astype("string")
    g["state_fips"] = g["STATEFP"].astype("string")
    g["county_fips"] = g["county_fips"].astype("string")
    g["county_name"] = g["county_name"].astype("string")
    g["acs_vintage"] = ACS_VINTAGE
    g["aland_m2"] = g["ALAND"].astype("float64")
    cols = list(schemas.TRACTS) + ["aland_m2", "geometry"]
    g = gpd.GeoDataFrame(g[cols], geometry="geometry", crs=4326)
    schemas.check(g, schemas.TRACTS, "tracts")
    g.to_parquet(out)
    log.info("wrote %s (%d tracts, %.1f%% income NaN)", out, len(g), 100 * g["median_hh_income"].isna().mean())
    return g


def load_tracts(storm_key: str) -> gpd.GeoDataFrame:
    return build_tracts(storm_key)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    g = build_tracts(sys.argv[1] if len(sys.argv) > 1 else "isaias", force="--force" in sys.argv)
    print(g.drop(columns="geometry").head())
    print(len(g), "tracts; income NaN share", round(g["median_hh_income"].isna().mean(), 4))
    print(g.groupby("state_fips").size())
