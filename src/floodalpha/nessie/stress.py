"""SYNTHETIC stress test: flood mask -> tract -> customer -> expected loss (EAD x PD x LGD).

Inputs : SYNTHETIC_portfolio.parquet (PORTFOLIO), tract_flood.parquet (TRACT_FLOOD, from flood/).
Outputs: SYNTHETIC_stress_results.parquet (STRESS_RESULTS), stress_summary.json.
Parameters come from config/model.yaml [stress] and are justified in ASSUMPTIONS.md.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from floodalpha import config, schemas
from floodalpha.nessie.portfolio import generate_portfolio

log = logging.getLogger(__name__)


def apply_stress(portfolio: pd.DataFrame, tract_flood: pd.DataFrame | None, params: dict,
                 seed: int = 0) -> pd.DataFrame:
    """Pure function: returns STRESS_RESULTS rows. tract_flood=None means no SAR product yet
    (every customer treated as not flooded, flagged via sar_covered=False)."""
    df = portfolio.copy()
    if tract_flood is None or tract_flood.empty:
        df["pct_tract_flooded"] = np.nan
        df["sar_covered"] = False
    else:
        schemas.check(tract_flood, schemas.TRACT_FLOOD, "tract_flood")
        tf = tract_flood.drop_duplicates("geoid").set_index("geoid")
        df["pct_tract_flooded"] = df["geoid"].map(tf["pct_flooded"]).astype("float64")
        df["sar_covered"] = df["geoid"].map(tf["sar_covered"]).fillna(False).astype(bool)
    df["is_flooded"] = (df["pct_tract_flooded"].fillna(0) >= params["flooded_tract_min_pct"]) & df["sar_covered"]

    pd_base = float(params["baseline_pd_12m"])
    df["pd_12m"] = np.where(df["is_flooded"], pd_base * params["flooded_pd_multiplier"], pd_base)
    df["pd_12m"] = df["pd_12m"].clip(upper=1.0)
    df["lgd"] = float(params["lgd"])
    df["ead"] = df["loan_amount"].astype(float)
    df["expected_loss"] = df["ead"] * df["pd_12m"] * df["lgd"]
    df["withdrawal_shock"] = np.where(df["is_flooded"], df["checking_balance"] * params["flooded_withdrawal_shock_pct"] / 100, 0.0)
    rng = np.random.default_rng(seed)
    p_miss = np.where(df["is_flooded"], params["flooded_missed_bill_prob"], params["control_missed_bill_prob"])
    df["missed_bill"] = rng.uniform(size=len(df)) < p_miss
    return schemas.check(df, schemas.STRESS_RESULTS, "stress_results")


def summarize(res: pd.DataFrame, params: dict) -> dict:
    def cell(d: pd.DataFrame) -> dict:
        return {
            "n": int(len(d)),
            "n_flooded": int(d["is_flooded"].sum()),
            "ead_total": float(d["ead"].sum()),
            "expected_loss_total": float(d["expected_loss"].sum()),
            "expected_loss_mean": float(d["expected_loss"].mean()) if len(d) else 0.0,
            "loss_rate_pct": float(100 * d["expected_loss"].sum() / d["ead"].sum()) if d["ead"].sum() else 0.0,
            "withdrawal_shock_total": float(d["withdrawal_shock"].sum()),
            "missed_bill_rate_pct": float(100 * d["missed_bill"].mean()) if len(d) else 0.0,
            "sar_covered_share_pct": float(100 * d["sar_covered"].mean()) if len(d) else 0.0,
        }
    base_el = float((res["ead"] * params["baseline_pd_12m"] * params["lgd"]).sum())
    return {
        "synthetic": True,
        "params": params,
        "total": cell(res),
        "baseline_expected_loss_total": base_el,
        "incremental_expected_loss": float(res["expected_loss"].sum() - base_el),
        "by_group": {g: cell(d) for g, d in res.groupby("group")},
        "by_flooded": {str(bool(k)): cell(d) for k, d in res.groupby("is_flooded")},
    }


def run_stress(storm_key: str = "isaias") -> dict:
    params = config.model()["stress"]
    out_dir = config.storm_dir(storm_key)
    port = generate_portfolio(storm_key)
    tf_path = out_dir / "tract_flood.parquet"
    tf = pd.read_parquet(tf_path) if tf_path.exists() else None
    if tf is None:
        log.warning("%s missing: running stress with no flood exposure (sar_covered=False for all)", tf_path)
    res = apply_stress(port, tf, params, seed=config.model()["portfolio"]["seed"])
    res.to_parquet(out_dir / "SYNTHETIC_stress_results.parquet", index=False)
    summ = summarize(res, params)
    summ["tract_flood_available"] = tf is not None
    (out_dir / "stress_summary.json").write_text(json.dumps(summ, indent=2))
    log.info("SYNTHETIC stress: EL total %.0f (baseline %.0f), %d flooded customers",
             summ["total"]["expected_loss_total"], summ["baseline_expected_loss_total"], summ["total"]["n_flooded"])
    return summ


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_stress(), indent=2)[:3000])
