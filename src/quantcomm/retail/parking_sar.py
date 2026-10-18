"""Sentinel-1 RTC backscatter over store parking lots (Planetary Computer, keyless).

Parked cars are strong radar reflectors (metal + double bounce off the asphalt), so a fuller lot
returns more VV backscatter. For every scene we record per store:
  lot_lin   mean gamma0 (linear) of 10 m pixels inside the OSM parking polygon
  bldg_lin  mean gamma0 over the store roof - a same-scene reference that soaks up rain / soil
            moisture / calibration swings that hit the whole neighbourhood
Only ascending passes (~18:00-19:00 local over CONUS) are kept: descending passes are ~06:00,
when lots are empty. Output: data/processed/retail/<ticker>_lot_backscatter.parquet, cached.
"""
from __future__ import annotations

import logging
import warnings
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
import numpy as np
import pandas as pd
import planetary_computer as pc
import pyproj
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from shapely import wkt
from shapely.geometry import mapping
from shapely.ops import transform, unary_union

from quantcomm import config
from quantcomm.flood.catalog import mpc_client

log = logging.getLogger(__name__)
WINDOW_PAD_M = 30
N_THREADS = 16
COLUMNS = ["scene_id", "datetime_utc", "relative_orbit", "pass_direction", "store_id", "metro",
           "lot_lin", "bldg_lin", "lot_px"]


def _inside(w: Window, width: int, height: int) -> bool:
    return w.col_off >= 0 and w.row_off >= 0 and w.col_off + w.width <= width and w.row_off + w.height <= height


def _mean_in(a: np.ndarray, geom, win_transform, min_px: int) -> tuple[float, int]:
    m = geometry_mask([mapping(geom)], out_shape=a.shape, transform=win_transform, invert=True)
    v = a[m]
    v = v[np.isfinite(v) & (v > 0)]
    return (float(v.mean()) if len(v) >= min_px else float("nan")), int(len(v))


def read_scene(item, stores: gpd.GeoDataFrame, pol: str, min_px: int) -> list[dict]:
    """Per-store lot / roof backscatter for one scene. Stores outside the frame are skipped."""
    rows = []
    with warnings.catch_warnings(), rasterio.open(pc.sign(item.assets[pol].href)) as src:
        warnings.simplefilter("ignore")
        to_src = pyproj.Transformer.from_crs(4326, src.crs, always_xy=True).transform
        for s in stores.itertuples():
            lot, bldg = transform(to_src, s.lot_geom), transform(to_src, s.geometry)
            w = from_bounds(*unary_union([lot, bldg]).buffer(WINDOW_PAD_M).bounds, src.transform)
            w = w.round_offsets().round_lengths()
            if not _inside(w, src.width, src.height):
                continue
            a = src.read(1, window=w, masked=True).astype("float32").filled(np.nan)
            if not np.isfinite(a).any():
                continue
            wt = src.window_transform(w)
            lot_v, n = _mean_in(a, lot, wt, min_px)
            if np.isnan(lot_v):
                continue
            bldg_v, _ = _mean_in(a, bldg, wt, 1)
            rows.append({"scene_id": item.id, "datetime_utc": pd.Timestamp(item.datetime),
                         "relative_orbit": item.properties.get("sat:relative_orbit"),
                         "pass_direction": item.properties.get("sat:orbit_state"),
                         "store_id": s.store_id, "metro": s.metro, "lot_lin": lot_v, "bldg_lin": bldg_v, "lot_px": n})
    return rows


def _search(bbox: list[float], start: str, end: str, collection: str, pass_direction: str) -> list:
    items = mpc_client().search(collections=[collection], bbox=bbox, datetime=f"{start}/{end}",
                                query={"sat:orbit_state": {"eq": pass_direction}}).items()
    return list(items)


def _safe_read(item, stores, pol, min_px) -> list[dict]:
    try:
        return read_scene(item, stores, pol, min_px)
    except (rasterio.errors.RasterioError, OSError) as exc:
        log.warning("scene %s failed: %s", item.id, exc)
        return []


def build_backscatter(ticker: str, stores: gpd.GeoDataFrame, end: str | None = None, force: bool = False) -> pd.DataFrame:
    p = config.load_yaml("signals.yaml")["parking_sar"]
    out = config.DATA_PROCESSED / "retail" / f"{ticker}_lot_backscatter.parquet"
    if out.exists() and not force:
        return pd.read_parquet(out)
    end = end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    stores = stores.assign(lot_geom=stores["lot_geometry_wkt"].map(wkt.loads))
    parts = []
    for metro, grp in stores.groupby("metro"):
        bbox = list(grp.total_bounds)
        items = _search(bbox, p["start"], end, p["collection"], p["pass_direction"])
        log.info("%s %s: %d stores x %d %s scenes", ticker, metro, len(grp), len(items), p["pass_direction"])
        with ThreadPoolExecutor(N_THREADS) as ex:
            rows = [r for rs in ex.map(lambda it: _safe_read(it, grp, p["polarization"], p["min_lot_px"]), items) for r in rs]
        if not rows:
            log.warning("%s %s: no usable lot readings", ticker, metro)
            continue
        parts.append(pd.DataFrame(rows, columns=COLUMNS))
    if not parts:
        raise RuntimeError(f"no Sentinel-1 lot readings for {ticker} - check the store table and the MPC search")
    df = dedupe_overlapping_frames(pd.concat(parts, ignore_index=True))
    df.to_parquet(out, index=False)
    return df


def dedupe_overlapping_frames(df: pd.DataFrame, gap: str = "10min") -> pd.DataFrame:
    """Adjacent frames of one pass overlap (seconds apart, and the CONUS ascending pass can straddle
    00:00 UTC), so a store can be read twice per pass. Keep the reading with the most lot pixels."""
    d = df.sort_values(["store_id", "relative_orbit", "datetime_utc"]).reset_index(drop=True)
    new_pass = d.groupby(["store_id", "relative_orbit"])["datetime_utc"].diff().fillna(pd.Timedelta(days=1)) > pd.Timedelta(gap)
    d = d.assign(pass_id=new_pass.cumsum())
    best = d.loc[d.groupby("pass_id")["lot_px"].idxmax()]
    return best.drop(columns="pass_id").sort_values(["store_id", "datetime_utc"]).reset_index(drop=True)
