#!/usr/bin/env python
"""Live mode: active NWS/NHC alerts -> bank, Capital One and SYNTHETIC-portfolio exposure + Sentinel-1 pass status.

    .venv/bin/python scripts/run_live.py [--force]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantcomm.live.exposure import run_live  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch alerts even if the cache is < 15 min old")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_live(force=a.force), indent=1, default=str))


if __name__ == "__main__":
    main()
