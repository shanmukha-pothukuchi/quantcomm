"""Column contracts for every Parquet table that crosses a module boundary.

Each entry maps column -> pandas dtype string. GeoParquet tables carry a `geometry`
column (EPSG:4326 unless stated). Use `check(df, NAME)` at module boundaries.
Files live under data/processed/<storm>/ unless noted.
"""
from __future__ import annotations

import pandas as pd

# s1_catalog.parquet — every Sentinel-1 scene intersecting the AOI in the search window
S1_CATALOG = {
    "scene_id": "string",          # STAC item id
    "acquired_utc": "datetime64[ns, UTC]",
    "published_utc": "datetime64[ns, UTC]",  # Copernicus catalog PublicationDate (event-time basis)
    "platform": "string",
    "relative_orbit": "int64",
    "pass_direction": "string",    # ascending / descending
    "aoi_coverage_pct": "float64", # % of AOI bbox covered by this scene
    "is_post_landfall": "bool",
}

# flood_polygons.parquet (GeoParquet, EPSG:4326)
FLOOD_POLYGONS = {"flood_id": "int64", "area_m2": "float64", "pass_id": "string", "relative_orbit": "int64",
                  "published_utc": "datetime64[ns, UTC]"}  # published_utc = when this pass's imagery became public

# tracts.parquet (GeoParquet, EPSG:4326) — census tracts in AOI states with ACS income
TRACTS = {
    "geoid": "string",             # 11-digit state+county+tract
    "state_fips": "string",
    "county_fips": "string",       # 5-digit
    "county_name": "string",
    "median_hh_income": "float64", # ACS 5-yr B19013_001E; NaN where suppressed
    "acs_vintage": "string",
}

# tract_flood.parquet — flood mask aggregated to tracts
TRACT_FLOOD = {
    "geoid": "string",
    "tract_area_m2": "float64",
    "flooded_area_m2": "float64",
    "pct_flooded": "float64",
    "sar_covered": "bool",         # False if the post-storm SAR mosaic did not cover the tract
    "fema_declared": "bool",       # county is in a FEMA declaration for this storm
}

# nessie_locations.parquet — Capital One branches + ATMs from Nessie
NESSIE_LOCATIONS = {
    "nessie_id": "string",
    "kind": "string",              # branch / atm
    "name": "string",
    "lat": "float64",
    "lon": "float64",
    "state": "string",
    "geocode_source": "string",    # nessie / census_geocoder
}

# nessie_exposure.parquet
NESSIE_EXPOSURE = {
    **NESSIE_LOCATIONS,
    "in_aoi": "bool",
    "sar_covered": "bool",
    "dist_to_flood_m": "float64",  # NaN if no flood polygon within 50 km
    "exposed": "bool",             # dist_to_flood_m <= buffer
}

# SYNTHETIC_portfolio.parquet — generated customers (never real people)
PORTFOLIO = {
    "customer_key": "string",      # FLOODALPHA_<n>
    "nessie_customer_id": "string",  # NaN until pushed to Nessie
    "group": "string",             # affected / control
    "geoid": "string",
    "county_fips": "string",
    "state": "string",
    "lat": "float64",
    "lon": "float64",
    "annual_income": "float64",
    "checking_balance": "float64",
    "savings_balance": "float64",
    "loan_amount": "float64",
    "loan_rate_pct": "float64",
    "monthly_bills": "float64",
}

# SYNTHETIC_stress_results.parquet
STRESS_RESULTS = {
    **PORTFOLIO,
    "pct_tract_flooded": "float64",
    "is_flooded": "bool",
    "pd_12m": "float64",
    "lgd": "float64",
    "ead": "float64",
    "expected_loss": "float64",
    "withdrawal_shock": "float64",
    "missed_bill": "bool",
}

# bank_exposure.parquet (Milestone 2) — one row per storm x bank
BANK_EXPOSURE = {
    "storm": "string",
    "cert": "int64",
    "bank_name": "string",
    "ticker": "string",
    "sod_year": "int64",
    "total_deposits_k": "float64",
    "flooded_deposits_k": "float64",
    "exposure_pct": "float64",
    "n_branches": "int64",
    "n_flooded_branches": "int64",
    "event_time_utc": "datetime64[ns, UTC]",
}


def check(df: pd.DataFrame, schema: dict, name: str = "table") -> pd.DataFrame:
    missing = [c for c in schema if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    return df
