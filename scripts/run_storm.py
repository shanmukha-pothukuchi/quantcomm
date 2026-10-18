#!/usr/bin/env python
"""Run the FloodAlpha pipeline for one storm.

    .venv/bin/python scripts/run_storm.py --storm isaias [--steps catalog,flood,exposure,sanity,portfolio,stress] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STEPS = ["catalog", "flood", "tracts", "locations", "exposure", "sanity", "portfolio", "stress"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storm", default="isaias")
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    steps = [s.strip() for s in a.steps.split(",")]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        sys.exit(f"unknown steps {bad}; choose from {STEPS}")
    for s in steps:
        t = time.time()
        print(f"\n=== [{a.storm}] {s} ===")
        if s == "catalog":
            from floodalpha.flood.catalog import build_catalog
            cat = build_catalog(a.storm, force=a.force)
            print(cat.drop(columns=["geometry_json", "vv_href"]).to_string())
        elif s == "flood":
            from floodalpha.flood.mpc_backend import run
            m = run(a.storm, force=a.force)
            print(json.dumps({k: v for k, v in m.items() if k not in ("frame_stats",)}, indent=1))
        elif s == "tracts":
            from floodalpha.census.acs import build_tracts
            print(len(build_tracts(a.storm, force=a.force)), "tracts")
        elif s == "locations":
            from floodalpha.nessie.client import build_locations
            print(len(build_locations(a.storm, force=a.force)), "Nessie locations")
        elif s == "exposure":
            from floodalpha.exposure import run
            print(json.dumps(run(a.storm), indent=1))
        elif s == "sanity":
            from floodalpha.flood.sanity import run
            run(a.storm)
        elif s == "portfolio":
            from floodalpha.nessie.portfolio import generate_portfolio
            print(len(generate_portfolio(a.storm, force=a.force)), "SYNTHETIC customers")
        elif s == "stress":
            from floodalpha.nessie.stress import run_stress
            print(json.dumps(run_stress(a.storm), indent=1)[:3000])
        print(f"--- {s} done in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
