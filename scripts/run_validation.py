#!/usr/bin/env python
"""Validate SAR flood maps against NFIP claims for every storm that has finished processing (re-runnable).

    .venv/bin/python scripts/run_validation.py [--storms harvey,michael] [--no-grid]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantcomm.validation.nfip import run_validation  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storms", default=None)
    ap.add_argument("--no-grid", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = run_validation(a.storms.split(",") if a.storms else None, do_grid=not a.no_grid)
    print(json.dumps({k: v for k, v in summary.items() if k != "threshold_tuning"}, indent=1, default=str)[:6000])
    tt = summary.get("threshold_tuning")
    if tt:
        print("threshold tuning:", json.dumps({k: v for k, v in tt.items() if k != "train_table"}, indent=1))
    print("pending storms:", summary["storms_pending"])


if __name__ == "__main__":
    main()
