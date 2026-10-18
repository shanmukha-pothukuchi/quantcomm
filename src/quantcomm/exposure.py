"""Flood mask x points/polygons -> exposure tables.

Two joins, both tested in tests/test_exposure.py:
  * point_exposure: any point table (Nessie branches/ATMs, FDIC branches) -> distance to nearest flood polygon
  * tract_flood:    census tracts -> % of tract area inside the flood mask, with SAR-coverage flag
All distance/area math happens in an equal-area projection (config exposure.branch_crs, EPSG:5070).
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from quantcomm import config, schemas

MAX_SEARCH_M = 50_000.0


def load_flood(storm_key: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    d = config.storm_dir(storm_key)
    polys = gpd.read_parquet(d / "flood_polygons.parquet")
    cov = gpd.read_file(d / "coverage.geojson")
    meta = json.loads((d / "flood_meta.json").read_text())
    return polys, cov, meta


def point_exposure(points: gpd.GeoDataFrame, polys: gpd.GeoDataFrame, coverage: gpd.GeoDataFrame,
                   aoi_bbox: list[float], buffer_m: float, crs: str) -> gpd.GeoDataFrame:
    """Add in_aoi, sar_covered, dist_to_flood_m, exposed. Preserves row count and order."""
    n = len(points)
    pts = points.to_crs(crs).copy()
    pts["_i"] = np.arange(n)
    aoi = gpd.GeoSeries([box(*aoi_bbox)], crs="EPSG:4326").to_crs(crs).iloc[0]
    cov_geom = coverage.to_crs(crs).geometry.make_valid().union_all()
    pts["in_aoi"] = pts.geometry.within(aoi)
    pts["sar_covered"] = pts.geometry.within(cov_geom)
    if len(polys):
        near = gpd.sjoin_nearest(pts[["_i", "geometry"]], polys.to_crs(crs)[["geometry"]],
                                 how="left", max_distance=MAX_SEARCH_M, distance_col="dist_to_flood_m")
        near = near.drop_duplicates("_i").set_index("_i")["dist_to_flood_m"]
        pts["dist_to_flood_m"] = pts["_i"].map(near).astype("float64")
    else:
        pts["dist_to_flood_m"] = np.nan
    pts["exposed"] = pts["dist_to_flood_m"].le(buffer_m).fillna(False).astype(bool)
    out = pts.drop(columns="_i").to_crs("EPSG:4326")
    assert len(out) == n, "point_exposure changed the row count"
    return out


def tract_flood(tracts: gpd.GeoDataFrame, polys: gpd.GeoDataFrame, coverage: gpd.GeoDataFrame,
                declared_county_fips: set[str], crs: str) -> pd.DataFrame:
    """% of each tract's area inside the flood mask. One row per input tract, no drops."""
    n = len(tracts)
    t = tracts[["geoid", "county_fips", "geometry"]].to_crs(crs).copy()
    t["tract_area_m2"] = t.geometry.area
    cov_geom = coverage.to_crs(crs).geometry.make_valid().union_all()
    t["sar_covered"] = t.geometry.intersects(cov_geom)
    if len(polys):
        # candidate pairs via bbox index, exact intersection area after
        p = polys.to_crs(crs)[["geometry"]]
        cand = gpd.sjoin(t[["geoid", "geometry"]], p, how="inner", predicate="intersects")
        if len(cand):
            pg = p.geometry.values
            inter = [t_geom.intersection(pg[j]).area for t_geom, j in zip(cand.geometry.values, cand["index_right"].values)]
            cand["flooded_area_m2"] = inter
            flooded = cand.groupby("geoid")["flooded_area_m2"].sum()
        else:
            flooded = pd.Series(dtype="float64")
    else:
        flooded = pd.Series(dtype="float64")
    t["flooded_area_m2"] = t["geoid"].map(flooded).fillna(0.0).astype("float64")
    t["pct_flooded"] = 100.0 * t["flooded_area_m2"] / t["tract_area_m2"]
    t["fema_declared"] = t["county_fips"].isin(declared_county_fips)
    out = pd.DataFrame(t.drop(columns="geometry"))
    assert len(out) == n and out["geoid"].is_unique, "tract_flood changed the row count or duplicated tracts"
    return schemas.check(out, schemas.TRACT_FLOOD, "tract_flood")


def run(storm_key: str) -> dict:
    from quantcomm.nessie.portfolio import fema_declared_counties

    st = config.storm(storm_key)
    ex = config.model()["exposure"]
    d = config.storm_dir(storm_key)
    polys, cov, meta = load_flood(storm_key)

    # Nessie branches + ATMs
    if not (d / "nessie_locations.parquet").exists():
        from quantcomm.nessie.client import build_locations
        build_locations(storm_key)
    loc = pd.read_parquet(d / "nessie_locations.parquet")
    pts = gpd.GeoDataFrame(loc, geometry=gpd.points_from_xy(loc["lon"], loc["lat"]), crs="EPSG:4326")
    ne = point_exposure(pts, polys, cov, st["aoi_bbox"], ex["buffer_m"], ex["branch_crs"])
    ne_df = pd.DataFrame(ne.drop(columns="geometry"))
    schemas.check(ne_df, schemas.NESSIE_EXPOSURE, "nessie_exposure")
    ne_df.to_parquet(d / "nessie_exposure.parquet", index=False)

    # tracts
    tracts = gpd.read_parquet(d / "tracts.parquet")
    declared = set(fema_declared_counties(storm_key)["county_fips"])
    tf = tract_flood(tracts, polys, cov, declared, ex["branch_crs"])
    tf.to_parquet(d / "tract_flood.parquet", index=False)

    summary = {
        "nessie_total": int(len(ne_df)),
        "nessie_in_aoi": int(ne_df["in_aoi"].sum()),
        "nessie_sar_covered": int(ne_df["sar_covered"].sum()),
        "nessie_exposed": int(ne_df["exposed"].sum()),
        "nessie_min_dist_to_flood_km": None if ne_df["dist_to_flood_m"].isna().all() else float(ne_df["dist_to_flood_m"].min() / 1000),
        "buffer_m": ex["buffer_m"],
        "tracts_total": int(len(tf)),
        "tracts_sar_covered": int(tf["sar_covered"].sum()),
        "tracts_flooded_ge_1pct": int(((tf["pct_flooded"] >= 1) & tf["sar_covered"]).sum()),
        "tracts_any_flood": int((tf["flooded_area_m2"] > 0).sum()),
        "tracts_fema_declared": int(tf["fema_declared"].sum()),
    }
    (d / "exposure_summary.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    import sys

    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else "isaias"), indent=1))
