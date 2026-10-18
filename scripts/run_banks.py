#!/usr/bin/env python
"""Every-bank exposure for every storm that has flood outputs. Usage: run_banks.py [--storms a,b]"""
import argparse, logging, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from floodalpha.banks.exposure import run  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--storms", default=None); a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    df = run(a.storms.split(",") if a.storms else None)
    top = df[(df["definition"] == "full") & (df["ticker"] != "")].sort_values("exposure_pct", ascending=False)
    print(top[["storm", "bank_name", "ticker", "sod_year", "total_deposits_k", "flooded_deposits_k", "exposure_pct", "aoi_deposits_pct", "n_flooded_branches"]].head(25).to_string())
