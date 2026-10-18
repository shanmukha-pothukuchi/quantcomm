#!/usr/bin/env python
"""Run the storm pipeline for every storm in config/storms.yaml, a few storms at a time.

    .venv/bin/python scripts/run_all.py [--storms harvey,irma] [--workers 3] [--steps catalog,flood,tracts,locations,exposure,sanity]
Each storm logs to data/raw/<storm>/run.log. Storms that fail are reported at the end, not hidden.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from quantcomm import config  # noqa: E402

DEFAULT_STEPS = "catalog,flood,tracts,locations,exposure,sanity"


def run_one(storm: str, steps: str, force: bool) -> tuple[str, int]:
    log = config.storm_dir(storm, "raw") / "run.log"
    cmd = [sys.executable, str(ROOT / "scripts" / "run_storm.py"), "--storm", storm, "--steps", steps] + (["--force"] if force else [])
    with open(log, "w") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT)
    return storm, rc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storms", default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--steps", default=DEFAULT_STEPS)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    storms = a.storms.split(",") if a.storms else list(config.storms())
    results = {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(run_one, s, a.steps, a.force): s for s in storms}
        for f in as_completed(futs):
            storm, rc = f.result()
            results[storm] = rc
            print(f"[run_all] {storm}: {'OK' if rc == 0 else f'FAILED rc={rc}'} (log: data/raw/{storm}/run.log)", flush=True)
    failed = [s for s, rc in results.items() if rc]
    print(f"[run_all] done. failed: {failed or 'none'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
