"""Sentinel-1 change-detection flood mapping on Microsoft Planetary Computer `sentinel-1-rtc` COGs.

No authentication needed. Per post-storm frame:
  1. read VV gamma0 (RTC) at the configured overview level (40 m default) clipped to the AOI
  2. reproject the matching pre-storm frames (same relative orbit, last N passes) onto the post grid,
     take the median across pre passes
  3. 3x3 focal median speckle filter, convert to dB
  4. flood candidate = (post - pre < diff_db_threshold) & (post < post_db_max)
  5. mask permanent water / ocean (JRC GSW occurrence) and steep slopes (NASADEM)
  6. drop connected components smaller than min_cluster_px
Frames are then mosaicked into one uint8 GeoTIFF (1 flood, 0 dry, 255 no data / not covered).
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import planetary_computer as pc
import rasterio
import rioxarray  # noqa: F401  (registers .rio accessor)
import xarray as xr
from PIL import Image
from rasterio import features
from rasterio.merge import merge as rio_merge
from scipy import ndimage
from shapely.geometry import box, shape
from shapely.ops import unary_union

from floodalpha import config, schemas
from floodalpha.flood import catalog

NODATA = 255


def _overview_level(target_res_m: float, native_res_m: float) -> int | None:
    """rioxarray `overview_level` is a 0-based index into the COG overview factors [2, 4, 8, ...]."""
    factor = target_res_m / native_res_m
    if factor <= 1:
        return None
    return int(round(np.log2(factor))) - 1


def _open(href: str, overview_level: int | None, masked: bool = True) -> xr.DataArray:
    da = rioxarray.open_rasterio(pc.sign(href), overview_level=overview_level, masked=masked)
    return da.squeeze("band", drop=True)


def _clip_to_bbox(da: xr.DataArray, bbox: list[float]) -> xr.DataArray:
    """Clip a raster (any CRS) to a lon/lat bbox."""
    b = gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(da.rio.crs).total_bounds
    return da.rio.clip_box(*b)


def _median_filter(a: np.ndarray, size: int) -> np.ndarray:
    nan = np.isnan(a)
    f = ndimage.median_filter(np.where(nan, 0.0, a), size=size, mode="nearest")
    f[nan] = np.nan
    return f


def _to_db(lin: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10.0 * np.log10(lin)
    db[~np.isfinite(db)] = np.nan
    return db


def _mosaic_pass(scene_rows: pd.DataFrame, template: xr.DataArray, overview_level: int, bbox: list[float]) -> xr.DataArray:
    """Reproject all frames of one pass onto `template` and take the first valid value."""
    out = np.full(template.shape, np.nan, dtype="float32")
    tb = template.rio.bounds()
    for _, r in scene_rows.iterrows():
        da = _open(r["vv_href"], overview_level)
        # skip frames that do not touch the template
        fb = gpd.GeoSeries([shape(json.loads(r["geometry_json"]))], crs="EPSG:4326").to_crs(template.rio.crs).total_bounds
        if fb[2] < tb[0] or fb[0] > tb[2] or fb[3] < tb[1] or fb[1] > tb[3]:
            continue
        da = _clip_to_bbox(da, bbox)
        rp = da.rio.reproject_match(template, resampling=rasterio.enums.Resampling.bilinear).values.astype("float32")
        rp[rp <= 0] = np.nan
        fill = np.isnan(out) & ~np.isnan(rp)
        out[fill] = rp[fill]
    return template.copy(data=out)


def _layer_from_collection(coll: str, asset: str, template: xr.DataArray, overview_level: int | None, nodata_to_nan: bool,
                           resampling=rasterio.enums.Resampling.nearest) -> np.ndarray:
    """Mosaic a global-layer collection (JRC, NASADEM) onto the template grid (nearest for categorical)."""
    bounds_ll = gpd.GeoSeries([box(*template.rio.bounds())], crs=template.rio.crs).to_crs("EPSG:4326").total_bounds
    items = list(catalog.mpc_client().search(collections=[coll], bbox=list(bounds_ll), limit=100).items())
    out = np.full(template.shape, np.nan, dtype="float32")
    for it in items:
        da = _open(it.assets[asset].href, overview_level, masked=nodata_to_nan)
        try:
            da = da.rio.clip_box(*bounds_ll)
        except Exception:  # tile touches the template bbox only at the edge
            continue
        # float + NaN nodata so that area outside this tile is NaN (not 0/255) and can be filled by the next tile
        da = da.astype("float32").rio.write_nodata(np.nan, encoded=False)
        rp = da.rio.reproject_match(template, resampling=resampling, nodata=np.nan).values.astype("float32")
        fill = np.isnan(out) & ~np.isnan(rp)
        out[fill] = rp[fill]
    return out


def _slope_deg(elev: np.ndarray, res_m: float) -> np.ndarray:
    e = np.where(np.isnan(elev), 0.0, elev)
    gy, gx = np.gradient(e, res_m)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def storm_crs(bbox: list[float]) -> str:
    """One UTM zone per storm so frames from neighbouring zones mosaic cleanly."""
    lon = (bbox[0] + bbox[2]) / 2
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"


def process_frame(post_row: pd.Series, pre_passes: dict[str, pd.DataFrame], bbox: list[float], p: dict, workdir: Path) -> dict:
    lvl = _overview_level(p["resolution_m"], 10)
    post = _clip_to_bbox(_open(post_row["vv_href"], lvl), bbox)
    crs = storm_crs(bbox)
    if post.rio.crs.to_string() != crs:
        post = post.rio.reproject(crs, resolution=float(p["resolution_m"]), resampling=rasterio.enums.Resampling.bilinear)
        post = post.where(post != post.rio.nodata) if post.rio.nodata is not None else post
    post_v = post.values.astype("float32")
    post_v[post_v <= 0] = np.nan
    pre_stack = [_mosaic_pass(rows, post, lvl, bbox).values for rows in pre_passes.values()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        pre_v = np.nanmedian(np.stack(pre_stack), axis=0).astype("float32")

    post_db = _to_db(_median_filter(post_v, p["speckle_filter_px"]))
    pre_db = _to_db(_median_filter(pre_v, p["speckle_filter_px"]))
    diff = post_db - pre_db
    valid = ~np.isnan(post_db) & ~np.isnan(pre_db)
    cand = valid & (diff < p["diff_db_threshold"]) & (post_db < p["post_db_max"])

    occ = _layer_from_collection(p["gsw_collection"], "occurrence", post, _overview_level(p["resolution_m"], 30), nodata_to_nan=False)
    perm_water = np.isnan(occ) | (occ >= p["permanent_water_occurrence_pct"]) | (occ == 255)
    elev = _layer_from_collection(p["dem_collection"], "elevation", post, _overview_level(p["resolution_m"], 30), nodata_to_nan=True,
                                  resampling=rasterio.enums.Resampling.bilinear)
    steep = _slope_deg(elev, p["resolution_m"]) > p["max_slope_deg"]
    no_dem = np.isnan(elev)
    cand &= ~perm_water & ~steep & ~no_dem
    if valid.sum() > 1_000_000 and (valid & perm_water).sum() / valid.sum() > 0.97:
        print(f"WARNING: {post_row['scene_id'][:40]}: {(valid & perm_water).sum() / valid.sum():.0%} of valid pixels masked as "
              "permanent water/ocean - plausible only for an almost all-ocean frame; check the JRC layer")

    lab, n = ndimage.label(cand)
    if n:
        sizes = np.bincount(lab.ravel())
        small = sizes < p["min_cluster_px"]
        small[0] = False
        cand[small[lab]] = False

    mask = np.where(valid & ~perm_water & ~no_dem, cand.astype("uint8"), NODATA).astype("uint8")
    px_area = p["resolution_m"] ** 2
    land = valid & ~perm_water & ~no_dem & ~steep
    diag = {}
    for t in (-1.0, -1.5, -2.0, -3.0, -4.0):
        diag[f"km2_diff_lt_{t}"] = float((land & (diff < t)).sum() * px_area / 1e6)
        diag[f"km2_diff_lt_{t}_and_post_lt_{p['post_db_max']}"] = float((land & (diff < t) & (post_db < p["post_db_max"])).sum() * px_area / 1e6)
    stats = {
        "threshold_sensitivity_km2_before_cluster_filter": diag,
        "land_km2": float(land.sum() * px_area / 1e6),
        "scene_id": post_row["scene_id"],
        "valid_px": int(valid.sum()),
        "flood_px": int((mask == 1).sum()),
        "flood_km2": float((mask == 1).sum() * px_area / 1e6),
        "candidate_before_masks_px": int((valid & (diff < p["diff_db_threshold"]) & (post_db < p["post_db_max"])).sum()),
        "perm_water_px": int((valid & perm_water).sum()),
        "steep_px": int((valid & steep).sum()),
        "median_diff_db": float(np.nanmedian(diff)),
        "p05_diff_db": float(np.nanpercentile(diff[valid], 5)) if valid.any() else None,
    }
    tag = post_row["scene_id"].split("_")[4]
    out = {"mask": workdir / f"mask_{tag}.tif", "post_db": workdir / f"post_{tag}.tif", "pre_db": workdir / f"pre_{tag}.tif",
           "eligible": workdir / f"eligible_{tag}.tif"}
    _write(out["eligible"], np.where(valid, land.astype("uint8"), NODATA).astype("uint8"), post, NODATA, "uint8")
    _write(out["mask"], mask, post, NODATA, "uint8")
    _write(out["post_db"], np.where(valid, post_db, np.nan), post, np.nan, "float32")
    _write(out["pre_db"], np.where(valid, pre_db, np.nan), post, np.nan, "float32")
    return {"stats": stats, "files": out}


def _write(path: Path, arr: np.ndarray, template: xr.DataArray, nodata, dtype: str) -> None:
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype=dtype, crs=template.rio.crs,
                transform=template.rio.transform(), nodata=nodata, compress="LZW", tiled=True)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(arr.astype(dtype), 1)


def _merge(paths: list[Path], out: Path, nodata, dtype: str, method: str = "first") -> None:
    srcs = [rasterio.open(p) for p in paths]
    arr, tr = rio_merge(srcs, nodata=nodata, method=method)
    prof = srcs[0].profile
    prof.update(height=arr.shape[1], width=arr.shape[2], transform=tr, nodata=nodata, dtype=dtype, compress="LZW", driver="GTiff")
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(arr.astype(dtype))
    for s in srcs:
        s.close()


def _preview(src: Path, dst_png: Path, kind: str, max_px: int = 1600) -> list[list[float]]:
    """Write an EPSG:4326 PNG (alpha where nodata) and return [[south, west], [north, east]]."""
    da = rioxarray.open_rasterio(src, masked=True).squeeze("band", drop=True)
    h, w = da.shape
    scale = max(1, int(np.ceil(max(h, w) / max_px)))
    if scale > 1:
        da = da.coarsen(x=scale, y=scale, boundary="trim").max() if kind == "mask" else da.coarsen(x=scale, y=scale, boundary="trim").mean()
    ll = da.rio.reproject("EPSG:4326", resampling=rasterio.enums.Resampling.nearest if kind == "mask" else rasterio.enums.Resampling.bilinear)
    v = ll.values
    rgba = np.zeros((*v.shape, 4), dtype="uint8")
    if kind == "mask":
        flood = v == 1
        rgba[flood] = (30, 110, 255, 220)
        rgba[(v == 0)] = (0, 0, 0, 0)
    else:
        g = np.clip((v + 25.0) / 25.0, 0, 1)  # -25..0 dB -> 0..1
        gray = (g * 255).astype("uint8")
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = gray
        rgba[..., 3] = np.where(np.isnan(v), 0, 255)
    Image.fromarray(rgba, "RGBA").save(dst_png, optimize=True)
    b = ll.rio.bounds()
    return [[float(b[1]), float(b[0])], [float(b[3]), float(b[2])]]


def run(storm_key: str, force: bool = False) -> dict:
    """Process every orbit with a post-landfall pass (see catalog.select_passes); write per-pass masks and a mosaic."""
    st = config.storm(storm_key)
    p = dict(config.model()["flood"])
    p["resolution_m"] = st.get("resolution_m", p["resolution_m"])
    out_dir = config.storm_dir(storm_key)
    meta_path = out_dir / "flood_meta.json"
    if meta_path.exists() and not force:
        return json.loads(meta_path.read_text())

    cat = catalog.build_catalog(storm_key, force=force)
    landfall = pd.Timestamp(st["landfall_time_utc"])
    passes = catalog.select_passes(cat, landfall, p["pre_images_per_orbit"], p["max_days_after"], st.get("s1_relative_orbits"))
    byid = cat.set_index("scene_id")
    bbox = st["aoi_bbox"]
    work = config.storm_dir(storm_key, "raw") / "s1" / "frames"
    work.mkdir(parents=True, exist_ok=True)

    pass_meta, all_polys, mask_files, post_files, pre_files, elig_files = [], [], [], [], [], []
    for ps in passes:
        print(f"[flood] {storm_key} pass {ps['pass_id']} ({len(ps['post_scene_ids'])} frames, pre {list(ps['pre_passes'])})")
        pre_passes = {d: byid.loc[ids].reset_index() for d, ids in ps["pre_passes"].items()}
        frame_stats, masks, posts, pres, eligs = [], [], [], [], []
        for sid in ps["post_scene_ids"]:
            res = process_frame(_row(byid, sid), pre_passes, bbox, p, work)
            print(f"[flood]   {sid[:40]} flood_km2={res['stats']['flood_km2']:.2f} land_km2={res['stats']['land_km2']:.0f}")
            frame_stats.append(res["stats"])
            masks.append(res["files"]["mask"]); posts.append(res["files"]["post_db"]); pres.append(res["files"]["pre_db"]); eligs.append(res["files"]["eligible"])
        pm = out_dir / f"flood_mask_{ps['pass_id']}.tif"
        _merge(masks, pm, NODATA, "uint8", method="max")
        polys = _vectorize(pm)
        polys["pass_id"] = ps["pass_id"]
        polys["relative_orbit"] = ps["relative_orbit"]
        polys["published_utc"] = pd.Timestamp(ps["published_utc"]) if ps["published_utc"] else pd.NaT
        all_polys.append(polys)
        foot = unary_union([shape(json.loads(byid.loc[s, "geometry_json"])) for s in ps["post_scene_ids"]]).intersection(box(*bbox))
        pass_meta.append({**ps, "flooded_km2": round(float(polys["area_m2"].sum() / 1e6), 3), "n_polygons": int(len(polys)),
                          "coverage_pct_of_aoi": round(100 * foot.area / box(*bbox).area, 2), "frame_stats": frame_stats,
                          "footprint": json.loads(gpd.GeoSeries([foot], crs="EPSG:4326").to_json())["features"][0]["geometry"]})
        mask_files.append(pm); post_files += posts; pre_files += pres; elig_files += eligs

    # storm-level mosaics (max over passes: flooded in any pass within the window)
    _merge(mask_files, out_dir / "flood_mask.tif", NODATA, "uint8", method="max")
    _merge(post_files, out_dir / "post_db.tif", np.nan, "float32")
    _merge(pre_files, out_dir / "pre_db.tif", np.nan, "float32")
    _merge(elig_files, out_dir / "eligible.tif", NODATA, "uint8", method="max")
    polys = pd.concat(all_polys, ignore_index=True)
    polys["flood_id"] = polys.index.astype("int64")
    polys["published_utc"] = pd.to_datetime(polys["published_utc"], utc=True)
    schemas.check(polys, schemas.FLOOD_POLYGONS, "flood_polygons")
    gpd.GeoDataFrame(polys, geometry="geometry", crs="EPSG:4326").to_parquet(out_dir / "flood_polygons.parquet")

    foot_all = unary_union([shape(pm["footprint"]) for pm in pass_meta])
    gpd.GeoDataFrame({"pass_id": [pm["pass_id"] for pm in pass_meta], "published_utc": [pm["published_utc"] for pm in pass_meta],
                      "geometry": [shape(pm["footprint"]) for pm in pass_meta]}, crs="EPSG:4326").to_file(out_dir / "coverage_passes.geojson", driver="GeoJSON")
    gpd.GeoDataFrame({"geometry": [foot_all]}, crs="EPSG:4326").to_file(out_dir / "coverage.geojson", driver="GeoJSON")

    bounds = _preview(out_dir / "pre_db.tif", out_dir / "preview_pre.png", "db")
    _preview(out_dir / "post_db.tif", out_dir / "preview_post.png", "db")
    _preview(out_dir / "flood_mask.tif", out_dir / "preview_change.png", "mask")

    first = pass_meta[0]
    pubs = [pd.Timestamp(pm["published_utc"]) for pm in pass_meta if pm["published_utc"]]
    event_time = min(pubs) if pubs else None
    meta = {
        "storm": storm_key,
        "backend": "mpc:sentinel-1-rtc",
        "landfall_time_utc": st["landfall_time_utc"],
        "event_time_utc": event_time.isoformat() if event_time is not None else None,
        "mosaic_complete_utc": max(pubs).isoformat() if pubs else None,
        "hours_landfall_to_event": round((event_time - landfall).total_seconds() / 3600, 1) if event_time is not None else None,
        "post_acquired_utc": first["post_acquired_utc"],
        "post_scene_ids": sum([pm["post_scene_ids"] for pm in pass_meta], []),
        "pre_scene_ids": sum([sum(pm["pre_passes"].values(), []) for pm in pass_meta], []),
        "pre_pass_dates": sorted({d for pm in pass_meta for d in pm["pre_passes"]}),
        "relative_orbit": first["relative_orbit"],
        "pass_direction": first["pass_direction"],
        "relative_orbits_used": [pm["relative_orbit"] for pm in pass_meta],
        "n_passes": len(pass_meta),
        "aoi_bbox": bbox,
        "coverage_pct_of_aoi": round(100 * foot_all.area / box(*bbox).area, 2),
        "flooded_km2": round(float(polys["area_m2"].sum() / 1e6), 2),
        "n_flood_polygons": int(len(polys)),
        "thresholds": {k: p[k] for k in ("diff_db_threshold", "post_db_max", "permanent_water_occurrence_pct", "max_slope_deg", "min_cluster_px", "speckle_filter_px", "resolution_m")},
        "passes": [{k: v for k, v in pm.items() if k != "footprint"} for pm in pass_meta],
        "frame_stats": sum([pm["frame_stats"] for pm in pass_meta], []),
        "preview_bounds": bounds,
    }
    meta_path.write_text(json.dumps(meta, indent=1, default=str))
    return meta


def _vectorize(mask_path: Path) -> gpd.GeoDataFrame:
    with rasterio.open(mask_path) as ds:
        m = ds.read(1)
        shapes = features.shapes((m == 1).astype("uint8"), mask=(m == 1), transform=ds.transform, connectivity=8)
        polys = gpd.GeoDataFrame({"geometry": [shape(g) for g, v in shapes]}, crs=ds.crs)
    polys["area_m2"] = polys.geometry.area if len(polys) else []
    return polys.to_crs("EPSG:4326").reset_index(drop=True)


def _row(byid: pd.DataFrame, sid: str) -> pd.Series:
    r = byid.loc[sid].copy()
    r["scene_id"] = sid
    return r


if __name__ == "__main__":
    import sys

    m = run(sys.argv[1] if len(sys.argv) > 1 else "isaias", force="--force" in sys.argv)
    print(json.dumps({k: v for k, v in m.items() if k != "frame_stats"}, indent=1))
