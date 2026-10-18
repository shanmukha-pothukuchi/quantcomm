"""SYNTHETIC Capital One customer portfolio for the Nessie stress test.

Everything produced here is SYNTHETIC: customers are sampled from census tracts with incomes drawn
around real ACS tract medians, but no record corresponds to a real person. Output files carry the
SYNTHETIC_ prefix and every Nessie object is tagged with the project prefix (FLOODALPHA_).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import Point

from quantcomm import config, schemas
from quantcomm.census.acs import FIPS_STATE, load_tracts
from quantcomm.nessie.client import Nessie, NessieError, created_ids

log = logging.getLogger(__name__)

SYNTHETIC = "SYNTHETIC"
FEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"


def fema_declared_counties(storm_key: str) -> pd.DataFrame:
    """County FIPS codes designated under the storm's FEMA declarations (cached raw JSON)."""
    st = config.storm(storm_key)
    nums = st.get("fema_disaster_numbers")
    raw = config.DATA_RAW / "fema" / f"declarations_{storm_key}.json"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        if nums:
            flt = " or ".join(f"disasterNumber eq {n}" for n in nums)
        else:
            # match by title within +-45 days of landfall (e.g. "HURRICANE HARVEY", "TROPICAL STORM HARVEY")
            lf = pd.Timestamp(st["landfall_time_utc"])
            t0, t1 = (lf - pd.Timedelta(days=45)).strftime("%Y-%m-%d"), (lf + pd.Timedelta(days=45)).strftime("%Y-%m-%d")
            flt = (f"substringof('{st['fema_title_match']}',declarationTitle) and declarationDate ge '{t0}' "
                   f"and declarationDate le '{t1}'")
        r = requests.get(FEMA_URL, params={"$filter": flt, "$top": 5000}, timeout=120)
        r.raise_for_status()
        raw.write_text(r.text)
    rows = json.loads(raw.read_text())["DisasterDeclarationsSummaries"]
    if not rows:
        raise RuntimeError(f"OpenFEMA returned no rows for disasters {nums}")
    df = pd.DataFrame(rows)
    df = df[df["fipsCountyCode"].astype(str).str.strip().ne("000")]
    df["county_fips"] = df["fipsStateCode"].astype(str).str.zfill(2) + df["fipsCountyCode"].astype(str).str.zfill(3)
    out = df[["disasterNumber", "state", "county_fips", "designatedArea", "declarationDate"]].drop_duplicates("county_fips")
    return out.reset_index(drop=True)


def _random_point_in(poly, rng: np.random.Generator, tries: int = 200) -> Point:
    minx, miny, maxx, maxy = poly.bounds
    for _ in range(tries):
        p = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if poly.contains(p):
            return p
    return poly.representative_point()


def generate_portfolio(storm_key: str = "isaias", force: bool = False) -> pd.DataFrame:
    """SYNTHETIC: build data/processed/<storm>/SYNTHETIC_portfolio.parquet (PORTFOLIO schema)."""
    out = config.storm_dir(storm_key) / "SYNTHETIC_portfolio.parquet"
    if out.exists() and not force:
        return pd.read_parquet(out)
    cfg = config.model()["portfolio"]
    rng = np.random.default_rng(cfg["seed"])
    tracts = load_tracts(storm_key)
    tracts = tracts[tracts["median_hh_income"].notna() & (tracts["aland_m2"] > 0)].reset_index(drop=True)
    declared = set(fema_declared_counties(storm_key)["county_fips"])
    # Affected = FEMA-declared county AND inside the storm AOI bbox. The GA declaration covers 76
    # counties including metro Atlanta; without the AOI filter Atlanta would dominate the sample.
    minx, miny, maxx, maxy = config.storm(storm_key)["aoi_bbox"]
    cen = tracts.geometry.representative_point()
    in_aoi = (cen.x >= minx) & (cen.x <= maxx) & (cen.y >= miny) & (cen.y <= maxy)
    tracts["affected"] = tracts["county_fips"].isin(declared) & in_aoi
    tracts["declared"] = tracts["county_fips"].isin(declared)
    aff, ctl = tracts[tracts.affected], tracts[~tracts.declared]
    if aff.empty or ctl.empty:
        raise RuntimeError(f"need both affected ({len(aff)}) and control ({len(ctl)}) tracts")

    n = int(cfg["n_customers"])
    n_aff = int(round(n * cfg["affected_share"]))
    picks = pd.concat([
        aff.sample(n_aff, replace=True, random_state=int(rng.integers(1 << 31))).assign(group="affected"),
        ctl.sample(n - n_aff, replace=True, random_state=int(rng.integers(1 << 31))).assign(group="control"),
    ]).reset_index(drop=True)

    pts = [_random_point_in(g, rng) for g in picks.geometry]
    income = np.exp(rng.normal(np.log(picks["median_hh_income"].values), cfg.get("income_sigma", 0.5)))
    income = np.clip(income, 8_000, 1_000_000)
    monthly = income / 12
    lognorm = lambda mult, sigma: np.exp(rng.normal(np.log(monthly * mult), sigma))  # noqa: E731
    checking = lognorm(cfg["checking_months_of_income"], 0.6)
    savings = lognorm(cfg["savings_months_of_income"], 0.9)
    loan = lognorm(cfg["loan_amount_months_of_income"], 0.5)
    bills = monthly * cfg["monthly_bills_share_of_income"] * rng.uniform(0.7, 1.3, n)

    df = pd.DataFrame({
        "customer_key": [f"{cfg['nickname_prefix']}{i:04d}" for i in range(n)],
        "nessie_customer_id": pd.array([None] * n, dtype="string"),
        "group": picks["group"].values,
        "geoid": picks["geoid"].values,
        "county_fips": picks["county_fips"].values,
        "state": [FIPS_STATE[s] for s in picks["state_fips"]],
        "lat": [p.y for p in pts],
        "lon": [p.x for p in pts],
        "annual_income": income.round(0),
        "checking_balance": checking.round(2),
        "savings_balance": savings.round(2),
        "loan_amount": loan.round(2),
        "loan_rate_pct": float(cfg["loan_rate_pct"]),
        "monthly_bills": bills.round(2),
    })
    df["synthetic"] = True
    df["tract_median_hh_income"] = picks["median_hh_income"].values
    for c in ("customer_key", "group", "geoid", "county_fips", "state"):
        df[c] = df[c].astype("string")
    schemas.check(df, schemas.PORTFOLIO, "SYNTHETIC_portfolio")
    df.to_parquet(out, index=False)
    log.info("%s: wrote %s (%d customers: %d affected / %d control, %d declared counties)",
             SYNTHETIC, out, n, n_aff, n - n_aff, len(declared))
    return df


def _amortized_payment(principal: float, rate_pct: float, months: int) -> float:
    r = rate_pct / 100 / 12
    return principal * r / (1 - (1 + r) ** -months) if r else principal / months


def push_to_nessie(storm_key: str = "isaias", limit: int | None = None, api: Nessie | None = None) -> pd.DataFrame:
    """SYNTHETIC: create customers/accounts/loan/bills in Nessie. Idempotent: skips customer_keys
    already present in created_ids.jsonl. Requires NESSIE_API_KEY."""
    api = api or Nessie()
    if not api.key:
        raise NessieError("NESSIE_API_KEY not set; cannot push SYNTHETIC portfolio")
    cfg = config.model()["portfolio"]
    path = config.storm_dir(storm_key) / "SYNTHETIC_portfolio.parquet"
    df = generate_portfolio(storm_key)
    done = {r["body"].get("last_name"): r["id"] for r in created_ids() if r["kind"] == "customer"}
    todo = df[df["nessie_customer_id"].isna() | ~df["customer_key"].isin(done)]
    if limit:
        todo = todo.head(limit)
    for i, row in todo.iterrows():
        if row.customer_key in done:
            df.loc[i, "nessie_customer_id"] = done[row.customer_key]
            continue
        addr = {"street_number": str(int(1 + abs(hash(row.customer_key)) % 9000)), "street_name": f"{cfg['nickname_prefix']}Synthetic St",
                "city": SYNTHETIC, "state": row.state, "zip": "00000"}
        cust = api.create_customer(first_name=SYNTHETIC, last_name=row.customer_key, address=addr)
        cid = cust["_id"]
        chk = api.create_account(cid, "Checking", f"{row.customer_key}_checking", row.checking_balance)
        api.create_account(cid, "Savings", f"{row.customer_key}_savings", row.savings_balance)
        api.create_loan(chk["_id"], row.loan_amount,
                        _amortized_payment(row.loan_amount, row.loan_rate_pct, cfg["loan_term_months"]),
                        credit_score=int(np.clip(600 + row.annual_income / 2000, 550, 820)),
                        type_="auto", description=f"{row.customer_key} {SYNTHETIC} loan")
        for k, payee in enumerate(("Utility", "Rent", "Insurance")):
            api.create_bill(chk["_id"], payee=f"{cfg['nickname_prefix']}{payee}",
                            nickname=f"{row.customer_key}_bill{k}", payment_amount=row.monthly_bills / 3,
                            payment_date="2026-11-01", recurring_date=1 + 9 * k)
        df.loc[i, "nessie_customer_id"] = cid
        df.to_parquet(path, index=False)  # checkpoint after each customer
    return df


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    d = generate_portfolio(force="--force" in sys.argv)
    print(d.groupby("group")[["annual_income", "checking_balance", "loan_amount"]].median())
    print(d.groupby(["group", "state"]).size())
    if "--push" in sys.argv:
        n = int(sys.argv[sys.argv.index("--push") + 1]) if len(sys.argv) > sys.argv.index("--push") + 1 else None
        print(push_to_nessie(limit=n)[["customer_key", "nessie_customer_id"]].head())
