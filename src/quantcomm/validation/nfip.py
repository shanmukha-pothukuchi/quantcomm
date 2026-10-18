"""Milestone 4: validate SAR flood maps against OpenFEMA NFIP claims (v3 `NfipClaims`).

Ground truth: NFIP claims with dateOfLoss within [landfall - 2 d, landfall + 14 d] in the storm's AOI states,
aggregated to census tract (`censusGeoid`). Prediction: % of tract area in the SAR flood mask (tract_flood.parquet),
restricted to tracts the post-storm SAR mosaic actually covered.

Because the pipeline saves post_db.tif / pre_db.tif / eligible.tif, alternative thresholds can be evaluated with
`rethreshold()` without re-running the SAR pipeline (used for the train/held-out threshold grid).
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import ExitStack
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio import features
from rasterio.io import MemoryFile
from rasterio.merge import merge
from scipy import ndimage, stats

from quantcomm import config

log = logging.getLogger(__name__)

NFIP_URL = "https://www.fema.gov/api/open/v3/NfipClaims"
B25001_URL = "https://www2.census.gov/programs-surveys/acs/summary_file/2023/table-based-SF/data/5YRData/acsdt5y2023-b25001.dat"
PAGE = 10000  # verified: $top=10000 returns 10000 rows
RAW = config.DATA_RAW / "fema"
OUT = config.DATA_PROCESSED / "validation"
TRAIN = ["harvey", "irma", "florence", "michael", "laura", "sally"]
HELDOUT = ["ida", "ian", "idalia", "helene", "milton"]
DAYS_BEFORE, DAYS_AFTER = 2, 14
FLOOD_PCT_THRESHOLD = 1.0   # tract "flooded" if >= 1% of its area in the mask
CLAIMS_POS_MIN = 5          # tract "claims-positive" if >= 5 NFIP claims
NODATA = 255


# --------------------------------------------------------------------------- claims
def fetch_claims(storm_key: str, force: bool = False) -> pd.DataFrame:
    """All NFIP claims in the storm window and AOI states. Cached raw JSON at data/raw/fema/nfip_<storm>.json."""
    st = config.storm(storm_key)
    raw = RAW / f"nfip_{storm_key}.json"
    if raw.exists() and not force:
        rows = json.loads(raw.read_text())["NfipClaims"]
    else:
        lf = pd.Timestamp(st["landfall_time_utc"])
        t0 = (lf - pd.Timedelta(days=DAYS_BEFORE)).strftime("%Y-%m-%dT00:00:00.000Z")
        t1 = (lf + pd.Timedelta(days=DAYS_AFTER)).strftime("%Y-%m-%dT23:59:59.000Z")
        states = " or ".join(f"state eq '{s}'" for s in st["aoi_states"])
        flt = f"dateOfLoss ge '{t0}' and dateOfLoss le '{t1}' and ({states})"
        # No $inlinecount: on large windows (Harvey) FEMA's count times out at its 60 s gateway and returns 503,
        # even for a $top=1 count-only query (verified 2026-10-18). Page on a stable $orderby until a short page.
        rows, skip = [], 0
        while True:
            params = {"$filter": flt, "$top": PAGE, "$skip": skip, "$orderby": "id"}
            page = _get_page(params)
            rows += page
            skip += len(page)
            if len(page) < PAGE:
                break
        n_unique = len({r["id"] for r in rows})
        if n_unique != len(rows):
            raise RuntimeError(f"{storm_key}: NFIP paging returned {len(rows)} rows but {n_unique} unique ids")
        total = len(rows)
        # independent count by FEMA's own event label, for the report
        ev = _flood_event_count(st["name"])
        RAW.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"filter": flt, "count": total, "floodEvent_count": ev, "NfipClaims": rows}))
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["censusGeoid", "dateOfLoss", "state", "paid"])
    df["paid"] = df[["netBuildingPaymentAmount", "netContentsPaymentAmount"]].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
    df["censusGeoid"] = df["censusGeoid"].astype("string")
    return df


def _get_page(params: dict, attempts: int = 4) -> list[dict]:
    """One NfipClaims page; retries 5xx (FEMA's gateway returns transient 503s) with backoff, raises otherwise."""
    for i in range(attempts):
        r = requests.get(NFIP_URL, params=params, timeout=300)
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()["NfipClaims"]
        log.warning("NfipClaims HTTP %s (attempt %d/%d, skip=%s)", r.status_code, i + 1, attempts, params.get("$skip"))
        time.sleep(10 * (i + 1))
    r.raise_for_status()
    return []


def _flood_event_count(name: str) -> dict:
    out = {}
    for label in (f"Hurricane {name}", f"Tropical Storm {name}"):
        try:
            r = requests.get(NFIP_URL, params={"$filter": f"floodEvent eq '{label}'", "$top": 1, "$inlinecount": "allpages", "$select": "id"}, timeout=120)
            out[label] = r.json()["metadata"]["count"] if r.ok else None
        except Exception as e:  # noqa: BLE001
            out[label] = f"error: {e}"
    return out


def claims_by_tract(claims: pd.DataFrame) -> pd.DataFrame:
    """One row per tract geoid: n_claims, paid_total. Claims without a geoid are counted in the report, not dropped silently."""
    if claims.empty:
        return pd.DataFrame({"geoid": pd.Series(dtype="string"), "n_claims": pd.Series(dtype="int64"), "paid_total": pd.Series(dtype="float64")})
    c = claims.copy()
    # censusGeoid is a 12-digit block-group id in v3 (verified: '120459603003'); the tract is the first 11 digits
    c["geoid"] = c["censusGeoid"].astype("string").str.strip().str[:11]
    c = c[c["geoid"].notna() & (c["geoid"].str.len() == 11)]
    g = c.groupby("geoid").agg(n_claims=("geoid", "size"), paid_total=("paid", "sum")).reset_index()
    g["geoid"] = g["geoid"].astype("string")
    return g


def housing_units() -> pd.DataFrame:
    """ACS 2023 5-yr B25001 (housing units) per tract from the keyless summary file."""
    dest = config.DATA_RAW / "census" / "acsdt5y2023-b25001.dat"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(B25001_URL, timeout=600)
        r.raise_for_status()
        dest.write_bytes(r.content)
    df = pd.read_csv(dest, sep="|", dtype={"GEO_ID": "string"}, na_values=["", "."])
    tr = df[df["GEO_ID"].str.startswith("1400000US")]
    hu = pd.to_numeric(tr["B25001_E001"], errors="coerce")
    return pd.DataFrame({"geoid": tr["GEO_ID"].str[9:].astype("string").values, "housing_units": hu.where(hu >= 0).values})


# --------------------------------------------------------------------------- re-thresholding
def _frame_mask(frames: Path, tag: str, diff_db: float, post_db_max: float, min_cluster_px: int) -> tuple[np.ndarray, dict] | None:
    """One post-storm frame's flood mask on its own grid, as mpc_backend.process_frame writes it.

    The saved frame mask gives the footprint (NODATA where the pipeline masked permanent water, no DEM or no data;
    that footprint does not depend on the thresholds). The flood rule and the cluster filter are re-applied to the
    saved dB rasters. Returns None if the frame's rasters are not on disk.
    """
    paths = {k: frames / f"{k}_{tag}.tif" for k in ("post", "pre", "eligible", "mask")}
    if not all(pp.exists() for pp in paths.values()):
        return None
    with rasterio.open(paths["post"]) as ds:
        post, prof = ds.read(1), ds.profile
    with rasterio.open(paths["pre"]) as ds:
        pre = ds.read(1)
    with rasterio.open(paths["eligible"]) as ds:
        land = ds.read(1) == 1
    with rasterio.open(paths["mask"]) as ds:
        footprint = ds.read(1) != NODATA
    cand = land & ~np.isnan(post) & ~np.isnan(pre) & ((post - pre) < diff_db) & (post < post_db_max)
    lab, n = ndimage.label(cand)
    if n:
        sizes = np.bincount(lab.ravel())
        small = sizes < min_cluster_px
        small[0] = False
        cand[small[lab]] = False
    return np.where(footprint, cand.astype("uint8"), NODATA).astype("uint8"), prof


def _memory_raster(stack: ExitStack, arr: np.ndarray, prof: dict):
    """Open an in-memory single-band uint8 raster (for rasterio.merge) that lives until the ExitStack closes."""
    mem = stack.enter_context(MemoryFile())
    with mem.open(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="uint8",
                  crs=prof["crs"], transform=prof["transform"], nodata=NODATA) as w:
        w.write(arr.astype("uint8"), 1)
    return stack.enter_context(mem.open())


def rethreshold(storm_key: str, diff_db: float, post_db_max: float, min_cluster_px: int) -> tuple[np.ndarray, dict]:
    """Recompute the flood mask from the saved per-frame dB rasters and eligibility masks.

    Merges in the pipeline's order, so the result lands on the same pixels: frames are merged within each pass,
    then the passes are merged (max). Merging all frames in one step rounds sub-pixel frame offsets differently
    and moves flood edges by a pixel.
    Returns (mask uint8: 1 flood / 0 dry / 255 nodata, raster profile).
    """
    d = config.storm_dir(storm_key)
    meta = json.loads((d / "flood_meta.json").read_text())
    with rasterio.open(d / "flood_mask.tif") as ds:
        profile = ds.profile
    frames = config.storm_dir(storm_key, "raw") / "s1" / "frames"
    with ExitStack() as stack:
        pass_srcs = []
        for ps in meta["passes"]:
            frame_srcs = []
            for sid in ps["post_scene_ids"]:
                tag = sid.split("_")[4]
                res = _frame_mask(frames, tag, diff_db, post_db_max, min_cluster_px)
                if res is None:
                    log.warning("%s: per-frame rasters for %s missing — skipped", storm_key, tag)
                    continue
                frame_srcs.append(_memory_raster(stack, *res))
            if frame_srcs:
                arr, tr = merge(frame_srcs, nodata=NODATA, method="max")
                pass_srcs.append(_memory_raster(stack, arr[0], {"crs": profile["crs"], "transform": tr}))
        if not pass_srcs:
            raise RuntimeError(f"{storm_key}: no per-frame rasters found under {frames}")
        merged, tr = merge(pass_srcs, nodata=NODATA, method="max")
    mask = merged[0].astype("uint8")
    return mask, {**profile, "height": mask.shape[0], "width": mask.shape[1], "transform": tr}


def _match(ds: rasterio.DatasetReader, profile: dict) -> np.ndarray:
    """Read a raster onto the reference grid (mosaics share a grid; guard against off-by-one extents)."""
    if ds.height == profile["height"] and ds.width == profile["width"] and ds.transform == profile["transform"]:
        return ds.read(1)
    from rasterio.vrt import WarpedVRT
    with WarpedVRT(ds, crs=profile["crs"], transform=profile["transform"], width=profile["width"], height=profile["height"]) as vrt:
        return vrt.read(1)


def tract_pct_from_mask(mask: np.ndarray, profile: dict, tracts: gpd.GeoDataFrame) -> pd.DataFrame:
    """Zonal stats: % of each tract's area (in the raster CRS) whose pixels are flood=1. One row per tract."""
    t = tracts[["geoid", "geometry"]].to_crs(profile["crs"]).reset_index(drop=True)
    idx = features.rasterize(((geom, i + 1) for i, geom in enumerate(t.geometry.values)), out_shape=mask.shape,
                             transform=profile["transform"], fill=0, dtype="int32")
    n = len(t)
    flooded = np.bincount(idx.ravel(), weights=(mask == 1).ravel(), minlength=n + 1)[1:]
    covered = np.bincount(idx.ravel(), weights=(mask != NODATA).ravel(), minlength=n + 1)[1:]
    px_area = abs(profile["transform"].a * profile["transform"].e)
    out = pd.DataFrame({"geoid": t["geoid"].astype("string"), "tract_area_m2": t.geometry.area.values,
                        "flooded_area_m2": flooded * px_area, "covered_area_m2": covered * px_area})
    out["pct_flooded"] = 100 * out["flooded_area_m2"] / out["tract_area_m2"]
    out["sar_covered"] = out["covered_area_m2"] > 0
    return out


# --------------------------------------------------------------------------- metrics
def metrics(df: pd.DataFrame, flood_pct: float = FLOOD_PCT_THRESHOLD, claims_min: int = CLAIMS_POS_MIN) -> dict:
    """Correlations and confusion matrix on SAR-covered tracts. `df` needs pct_flooded, n_claims, claims_per_1k_hu, fema_declared."""
    d = df[df["sar_covered"]].copy()
    out = {"n_tracts": int(len(d)), "n_tracts_with_claims": int((d["n_claims"] > 0).sum()), "n_claims": int(d["n_claims"].sum())}
    if len(d) < 3:
        return out
    dens = d["claims_per_1k_hu"].fillna(0)
    out["spearman_pct_vs_claims"] = _corr(stats.spearmanr, d["pct_flooded"], d["n_claims"])
    out["spearman_pct_vs_density"] = _corr(stats.spearmanr, d["pct_flooded"], dens)
    out["pearson_pct_vs_density"] = _corr(stats.pearsonr, d["pct_flooded"], dens)
    pred = d["pct_flooded"] >= flood_pct
    truth = d["n_claims"] >= claims_min
    out["confusion"] = _confusion(pred, truth)
    out["baseline_fema_declared"] = _confusion(d["fema_declared"].astype(bool), truth)
    if truth.nunique() == 2:
        from sklearn.metrics import roc_auc_score
        out["auc_pct_flooded"] = float(roc_auc_score(truth, d["pct_flooded"]))
        out["auc_fema_declared"] = float(roc_auc_score(truth, d["fema_declared"].astype(int)))
    out["mean_claims_flooded_tracts"] = float(d.loc[pred, "n_claims"].mean()) if pred.any() else None
    out["mean_claims_dry_tracts"] = float(d.loc[~pred, "n_claims"].mean()) if (~pred).any() else None
    return out


def _corr(fn, a, b) -> dict | None:
    if a.nunique() < 2 or b.nunique() < 2:
        return None
    r, p = fn(a, b)
    return {"r": float(r), "p": float(p)}


def _confusion(pred: pd.Series, truth: pd.Series) -> dict:
    tp = int((pred & truth).sum()); fp = int((pred & ~truth).sum())
    fn = int((~pred & truth).sum()); tn = int((~pred & ~truth).sum())
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
            "positive_rate_pred": (tp + fp) / len(pred) if len(pred) else None}


# --------------------------------------------------------------------------- per-storm
def available_storms() -> list[str]:
    return [k for k in config.storms() if (config.storm_dir(k) / "flood_meta.json").exists()]


def validate_storm(storm_key: str, tract_pct: pd.DataFrame | None = None) -> pd.DataFrame:
    """Tract table joining SAR pct_flooded with NFIP claims (pipeline default thresholds unless tract_pct is given)."""
    from quantcomm.nessie.portfolio import fema_declared_counties

    d = config.storm_dir(storm_key)
    tracts = gpd.read_parquet(d / "tracts.parquet")[["geoid", "county_fips", "county_name", "geometry"]]
    if tract_pct is None:
        tf = pd.read_parquet(d / "tract_flood.parquet")[["geoid", "pct_flooded", "sar_covered", "fema_declared", "tract_area_m2"]]
    else:
        declared = set(fema_declared_counties(storm_key)["county_fips"])
        tf = tract_pct.merge(tracts[["geoid", "county_fips"]], on="geoid", how="left")
        tf["fema_declared"] = tf["county_fips"].isin(declared)
        tf = tf[["geoid", "pct_flooded", "sar_covered", "fema_declared", "tract_area_m2"]]
    n = len(tf)
    cl = claims_by_tract(fetch_claims(storm_key))
    hu = housing_units()
    df = tf.merge(cl, on="geoid", how="left").merge(hu, on="geoid", how="left")
    assert len(df) == n, "validation join changed the tract count"
    df["n_claims"] = df["n_claims"].fillna(0).astype("int64")
    df["paid_total"] = df["paid_total"].fillna(0.0)
    df["claims_per_km2"] = df["n_claims"] / (df["tract_area_m2"] / 1e6)
    df["claims_per_1k_hu"] = np.where(df["housing_units"] > 0, 1000 * df["n_claims"] / df["housing_units"], np.nan)
    df["storm"] = storm_key
    # claims that fell in the AOI states but outside our tract table (e.g. outside the AOI bbox states) are reported, not hidden
    df.attrs["claims_total"] = int(cl["n_claims"].sum())
    df.attrs["claims_in_tracts"] = int(df["n_claims"].sum())
    return df


def threshold_grid(storms: list[str], grid_diff=(-1.5, -2.0, -3.0, -4.0), grid_post=(-13.0, -15.0, -17.0), min_cluster_px: int = 8) -> pd.DataFrame:
    rows = []
    for s in storms:
        d = config.storm_dir(s)
        if not (d / "flood_mask.tif").exists() or not list((config.storm_dir(s, "raw") / "s1" / "frames").glob("eligible_*.tif")):
            log.warning("%s: no per-frame eligibility rasters (older run) — skipped in threshold grid", s)
            continue
        tracts = gpd.read_parquet(d / "tracts.parquet")
        for dd in grid_diff:
            for pp in grid_post:
                mask, prof = rethreshold(s, dd, pp, min_cluster_px)
                tp = tract_pct_from_mask(mask, prof, tracts)
                m = metrics(validate_storm(s, tp))
                rows.append({"storm": s, "diff_db": dd, "post_db_max": pp, "flooded_km2": float((mask == 1).sum() * abs(prof["transform"].a * prof["transform"].e) / 1e6),
                             "spearman": (m.get("spearman_pct_vs_density") or {}).get("r"), "auc": m.get("auc_pct_flooded"),
                             "precision": m.get("confusion", {}).get("precision"), "recall": m.get("confusion", {}).get("recall"), "n_tracts": m["n_tracts"]})
                log.info("grid %s diff=%s post=%s spearman=%s auc=%s", s, dd, pp, rows[-1]["spearman"], rows[-1]["auc"])
    return pd.DataFrame(rows)


def run_validation(storms: list[str] | None = None, do_grid: bool = True) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    avail = available_storms()
    storms = [s for s in (storms or list(config.storms())) if s in avail]
    pending = [s for s in config.storms() if s not in avail]
    tables, per_storm, claim_counts = [], {}, {}
    for s in storms:
        d = config.storm_dir(s)
        if not (d / "tract_flood.parquet").exists():
            pending.append(s)
            continue
        df = validate_storm(s)
        raw = json.loads((RAW / f"nfip_{s}.json").read_text())
        claim_counts[s] = {"window_filter_count": raw["count"], "floodEvent_count": raw.get("floodEvent_count"),
                           "claims_in_tract_table": df.attrs["claims_in_tracts"]}
        per_storm[s] = metrics(df)
        tables.append(df)
    summary = {"storms_validated": storms, "storms_pending": sorted(set(pending)), "claim_counts": claim_counts, "per_storm": per_storm,
               "definitions": {"flooded_tract": f"pct_flooded >= {FLOOD_PCT_THRESHOLD}% and sar_covered", "claims_positive": f"n_claims >= {CLAIMS_POS_MIN}",
                               "window": f"dateOfLoss in [landfall-{DAYS_BEFORE}d, landfall+{DAYS_AFTER}d], AOI states", "density": "claims per 1,000 ACS 2023 housing units"},
               "notes": ["Isaias (2026) has no NFIP claims published yet; it cannot be validated.",
                         "Only tracts covered by the post-storm SAR mosaic are scored; uncovered tracts are excluded, not treated as dry."]}
    if tables:
        allt = pd.concat(tables, ignore_index=True)
        allt.to_parquet(OUT / "tract_validation.parquet", index=False)
        summary["pooled"] = metrics(allt)
        summary["pooled_train"] = metrics(allt[allt["storm"].isin(TRAIN)]) if allt["storm"].isin(TRAIN).any() else None
        summary["pooled_heldout"] = metrics(allt[allt["storm"].isin(HELDOUT)]) if allt["storm"].isin(HELDOUT).any() else None
        _scatter(allt, OUT / "scatter_pct_flooded_vs_claims.png")
    if do_grid and tables:
        grid = threshold_grid([s for s in storms if s in TRAIN + HELDOUT])
        if len(grid):
            grid.to_parquet(OUT / "threshold_grid.parquet", index=False)
            summary["threshold_tuning"] = _pick_threshold(grid)
    (OUT / "validation_summary.json").write_text(json.dumps(summary, indent=1, default=_json_default))
    return summary


def _pick_threshold(grid: pd.DataFrame) -> dict:
    g = grid.copy()
    g["score"] = g["auc"].fillna(g["spearman"])
    train = g[g["storm"].isin(TRAIN)]
    held = g[g["storm"].isin(HELDOUT)]
    out = {"train_storms_in_grid": sorted(train["storm"].unique().tolist()), "heldout_storms_in_grid": sorted(held["storm"].unique().tolist()),
           "metric": "mean AUC of pct_flooded vs claims-positive across training storms (Spearman where AUC undefined)"}
    if train.empty:
        out["note"] = "no training storms available yet"
        return out
    agg = train.groupby(["diff_db", "post_db_max"])["score"].mean().reset_index().sort_values("score", ascending=False)
    best = agg.iloc[0]
    out["best_on_train"] = {"diff_db": float(best["diff_db"]), "post_db_max": float(best["post_db_max"]), "train_score": float(best["score"])}
    out["default"] = {"diff_db": -3.0, "post_db_max": -15.0,
                      "train_score": float(agg[(agg.diff_db == -3.0) & (agg.post_db_max == -15.0)]["score"].iloc[0]) if ((agg.diff_db == -3.0) & (agg.post_db_max == -15.0)).any() else None}
    if not held.empty:
        hb = held[(held.diff_db == best["diff_db"]) & (held.post_db_max == best["post_db_max"])]
        hd = held[(held.diff_db == -3.0) & (held.post_db_max == -15.0)]
        out["heldout_score_best"] = float(hb["score"].mean()) if len(hb) else None
        out["heldout_score_default"] = float(hd["score"].mean()) if len(hd) else None
    out["train_table"] = agg.to_dict(orient="records")
    return out


def _scatter(df: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = df[df["sar_covered"]]
    fig, ax = plt.subplots(figsize=(7, 5))
    for s, g in d.groupby("storm"):
        ax.scatter(g["pct_flooded"].clip(lower=0.01), g["claims_per_1k_hu"].fillna(0).clip(lower=0.1), s=8, alpha=0.5, label=s)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("% of tract area in SAR flood mask (log, floored at 0.01)")
    ax.set_ylabel("NFIP claims per 1,000 housing units (log, floored at 0.1)")
    ax.set_title("SAR flood fraction vs NFIP claims, by tract")
    ax.legend(fontsize=7, markerscale=2)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)
