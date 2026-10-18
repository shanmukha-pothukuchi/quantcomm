"""Build the parking-lot radar signal for one retailer: OSM stores -> Sentinel-1 lot backscatter.

    PYTHONPATH=src .venv/bin/python scripts/run_retail.py --ticker WMT [--force]
"""
from __future__ import annotations

import argparse
import logging

from floodalpha.retail import osm, parking_sar


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="WMT")
    ap.add_argument("--force", action="store_true", help="re-download OSM and re-read every scene")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stores = osm.build_stores(a.ticker, force=a.force)
    print(f"{a.ticker}: {len(stores)} stores with mapped parking ({stores.groupby('metro').size().to_dict()})")
    df = parking_sar.build_backscatter(a.ticker, stores, force=a.force)
    print(f"{a.ticker}: {len(df)} lot readings, {df['store_id'].nunique()} stores, "
          f"{df['datetime_utc'].dt.date.nunique()} acquisition days, "
          f"{df['datetime_utc'].min():%Y-%m-%d} -> {df['datetime_utc'].max():%Y-%m-%d}")


if __name__ == "__main__":
    main()
