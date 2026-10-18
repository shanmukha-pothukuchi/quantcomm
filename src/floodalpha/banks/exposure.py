"""Every-bank flood exposure: FDIC SOD branches x SAR flood polygons, per storm.

exposure_pct      = deposits in branches within `buffer_m` of a flood polygon / bank's total US deposits
aoi_deposits_pct  = deposits in branches inside the storm AOI bbox / total deposits (flood-agnostic alternative)
Two definitions: `strict` uses only polygons published by the storm's event time (first post-landfall pass);
`full` uses every pass in the window. SOD vintage: see fdic.sod_vintage_for.
"""
from __future__ import annotations

import json
import logging

import geopandas as gpd
import pandas as pd

from floodalpha import config, schemas
from floodalpha.banks import fdic, tickers
from floodalpha.exposure import point_exposure

log = logging.getLogger(__name__)


def storm_flood_inputs(storm_key: str):
    d = config.storm_dir(storm_key)
    meta = json.loads((d / "flood_meta.json").read_text())
    polys = gpd.read_parquet(d / "flood_polygons.parquet")
    cov = gpd.read_file(d / "coverage.geojson")
    event_time = pd.Timestamp(meta["event_time_utc"])
    if "published_utc" not in polys.columns:  # older single-pass output
        polys["published_utc"] = event_time
        polys["pass_id"] = "single"
    polys["published_utc"] = pd.to_datetime(polys["published_utc"], utc=True)
    cov_passes_path = d / "coverage_passes.geojson"
    cov_strict = cov
    if cov_passes_path.exists():
        cp = gpd.read_file(cov_passes_path)
        cp["published_utc"] = pd.to_datetime(cp["published_utc"], utc=True)
        first = cp[cp["published_utc"] <= event_time]
        if len(first):
            cov_strict = gpd.GeoDataFrame(geometry=[first.geometry.make_valid().union_all()], crs="EPSG:4326")
    return meta, polys, cov, cov_strict, event_time


def bank_exposure_for_storm(storm_key: str) -> tuple[pd.DataFrame, dict]:
    st = config.storm(storm_key)
    ex = config.model()["exposure"]
    meta, polys, cov, cov_strict, event_time = storm_flood_inputs(storm_key)
    br = fdic.branches_for_storm(storm_key)
    year = int(br["year"].iloc[0])
    n_all = len(br)
    n_nocoord = int(br["lat"].isna().sum())
    br = tickers.assign_tickers(br)
    totals = br.groupby("cert").agg(total_deposits_k=("deposits_k", "sum"), n_branches=("brnum", "size"),
                                    bank_name=("bank_name", "first"), rssdhcr=("rssdhcr", "first"), namehcr=("namehcr", "first"),
                                    ticker=("ticker", "first"), company=("company", "first"), ticker_status=("ticker_status", "first"))
    # spatial work only for branches with coordinates inside a generous box around the AOI (speed); others are unexposed
    w, s, e, n = st["aoi_bbox"]
    geo = br[br["lat"].notna() & br["lon"].between(w - 1, e + 1) & br["lat"].between(s - 1, n + 1)].copy()
    log.info("%s: SOD %d: %d branches, %d without coordinates, %d near the AOI", storm_key, year, n_all, n_nocoord, len(geo))
    rows = []
    for definition, pp, cc in (("strict", polys[polys["published_utc"] <= event_time], cov_strict), ("full", polys, cov)):
        pe = point_exposure(geo, pp, cc, st["aoi_bbox"], ex["buffer_m"], ex["branch_crs"]) if len(geo) else geo.assign(in_aoi=False, sar_covered=False, dist_to_flood_m=float("nan"), exposed=False)
        assert len(pe) == len(geo)
        agg = pe.groupby("cert").agg(flooded_deposits_k=("deposits_k", lambda x: float(x[pe.loc[x.index, "exposed"]].sum())),
                                     aoi_deposits_k=("deposits_k", lambda x: float(x[pe.loc[x.index, "in_aoi"]].sum())),
                                     n_flooded_branches=("exposed", "sum"), n_branches_in_aoi=("in_aoi", "sum"),
                                     n_branches_sar_covered=("sar_covered", "sum"))
        t = totals.join(agg, how="left").fillna({"flooded_deposits_k": 0.0, "aoi_deposits_k": 0.0, "n_flooded_branches": 0, "n_branches_in_aoi": 0, "n_branches_sar_covered": 0})
        t["exposure_pct"] = 100 * t["flooded_deposits_k"] / t["total_deposits_k"].where(t["total_deposits_k"] > 0)
        t["aoi_deposits_pct"] = 100 * t["aoi_deposits_k"] / t["total_deposits_k"].where(t["total_deposits_k"] > 0)
        t["definition"] = definition
        t["storm"] = storm_key
        t["sod_year"] = year
        t["event_time_utc"] = event_time
        t["landfall_time_utc"] = pd.Timestamp(st["landfall_time_utc"])
        rows.append(t.reset_index())
    out = pd.concat(rows, ignore_index=True)
    for c in ("n_flooded_branches", "n_branches_in_aoi", "n_branches_sar_covered", "n_branches"):
        out[c] = out[c].astype("int64")
    out["cert"] = out["cert"].astype("int64")
    out["bank_name"] = out["bank_name"].astype("string")
    out["ticker"] = out["ticker"].astype("string")
    out["exposure_pct"] = out["exposure_pct"].fillna(0.0)
    out["aoi_deposits_pct"] = out["aoi_deposits_pct"].fillna(0.0)
    schemas.check(out, schemas.BANK_EXPOSURE, "bank_exposure")
    full = out[out["definition"] == "full"]
    report = {
        "storm": storm_key, "sod_year": year, "branches_total": n_all, "branches_without_coords": n_nocoord,
        "branches_near_aoi": int(len(geo)), "banks_total": int(len(totals)),
        "banks_with_ticker": int((totals["ticker"] != "").sum()), "banks_with_active_ticker": int((totals["ticker_status"] == "active").sum()),
        "banks_any_exposure_full": int((full["exposure_pct"] > 0).sum()),
        "banks_any_exposure_strict": int((out[(out["definition"] == "strict")]["exposure_pct"] > 0).sum()),
        "banks_in_aoi": int((full["aoi_deposits_pct"] > 0).sum()),
        "flooded_deposits_bn_full": round(float(full["flooded_deposits_k"].sum()) / 1e6, 3),
        "event_time_utc": event_time.isoformat(), "flooded_km2": meta.get("flooded_km2"),
    }
    return out, report


def run(storms: list[str] | None = None) -> pd.DataFrame:
    storms = storms or list(config.storms())
    done, pending, reports = [], [], []
    for s in storms:
        if not (config.storm_dir(s) / "flood_meta.json").exists():
            pending.append(s)
            continue
        df, rep = bank_exposure_for_storm(s)
        print(f"[banks] {json.dumps(rep)}", flush=True)
        done.append(df)
        reports.append(rep)
    if pending:
        print(f"[banks] pending (no flood output yet): {pending}")
    if not done:
        raise RuntimeError("no storm has flood outputs yet")
    out = pd.concat(done, ignore_index=True)
    out.to_parquet(config.DATA_PROCESSED / "bank_exposure.parquet", index=False)
    (config.DATA_PROCESSED / "bank_exposure_report.json").write_text(json.dumps({"storms": reports, "pending": pending}, indent=1))
    return out
