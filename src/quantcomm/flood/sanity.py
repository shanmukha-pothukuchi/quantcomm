"""Sanity check of the SAR flood mask against independent signals:
  * NWS Flood / Flash Flood Warning polygons issued in the landfall window (api.weather.gov history)
  * FEMA-declared counties
  * NHC best track
Writes nws_sanity.json (+ warnings GeoJSON for the map) and nhc_track.geojson, and prints a short report.
"""
from __future__ import annotations

import os

import json

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

from quantcomm import config
from quantcomm.exposure import load_flood

NWS = "https://api.weather.gov/alerts"
UA = {"User-Agent": f"quantcomm ({os.environ.get('FLOODALPHA_CONTACT', 'contact-not-set')})"}
EVENTS = "Flash Flood Warning,Flood Warning,Storm Surge Warning"


def nws_warnings(storm_key: str, days_after: int = 3) -> gpd.GeoDataFrame:
    st = config.storm(storm_key)
    raw = config.storm_dir(storm_key, "raw") / "nws_warnings.json"
    if not raw.exists():
        t0 = pd.Timestamp(st["landfall_time_utc"]) - pd.Timedelta(days=1)
        t1 = pd.Timestamp(st["landfall_time_utc"]) + pd.Timedelta(days=days_after)
        feats, url, params = [], NWS, {
            "start": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": EVENTS, "area": ",".join(st["aoi_states"]), "limit": 500,
        }
        while url:
            r = requests.get(url, params=params, headers=UA, timeout=120)
            r.raise_for_status()
            d = r.json()
            feats += d["features"]
            url = d.get("pagination", {}).get("next") if len(d["features"]) == 500 else None
            params = None
        raw.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    fc = json.loads(raw.read_text())
    rows = [
        {"event": f["properties"]["event"], "area": f["properties"]["areaDesc"], "sent": f["properties"]["sent"],
         "headline": f["properties"].get("headline"), "geometry": shape(f["geometry"])}
        for f in fc["features"] if f.get("geometry")
    ]
    if not rows:  # api.weather.gov only serves recent history; historical storms legitimately have none
        return gpd.GeoDataFrame({"event": [], "area": [], "sent": [], "headline": []}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def nhc_track(storm_key: str) -> gpd.GeoDataFrame:
    st = config.storm(storm_key)
    z = config.DATA_RAW / "nhc" / f"{st['nhc_id']}_best_track.zip"
    if not z.exists():
        z.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(f"https://www.nhc.noaa.gov/gis/best_track/{st['nhc_id']}_best_track.zip", timeout=120)
        r.raise_for_status()
        z.write_bytes(r.content)
    from quantcomm.flood.nhc import best_track_points  # handles lowercase file names and month abbreviations
    g = best_track_points(st["nhc_id"])
    cols = [c for c in ("STORMNAME", "dtg_utc", "LAT", "LON", "INTENSITY", "STORMTYPE", "SS", "geometry") if c in g.columns]
    return g[cols]


def run(storm_key: str) -> dict:
    from quantcomm.nessie.portfolio import fema_declared_counties

    st = config.storm(storm_key)
    d = config.storm_dir(storm_key)
    crs = config.model()["exposure"]["branch_crs"]
    polys, cov, meta = load_flood(storm_key)
    warn = nws_warnings(storm_key)
    track = nhc_track(storm_key)
    track.assign(dtg_utc=track["dtg_utc"].astype(str)).to_file(d / "nhc_track.geojson", driver="GeoJSON")

    polys_m = polys.to_crs(crs)
    cov_m = cov.to_crs(crs).geometry.make_valid().union_all()
    flood_u = polys_m.geometry.make_valid().union_all() if len(polys_m) else None
    warn_m = warn.to_crs(crs)
    warn_u = warn_m.geometry.make_valid().union_all() if len(warn_m) else None
    warn_in_cov = warn_u.intersection(cov_m) if warn_u is not None else None
    flood_area = flood_u.area if flood_u is not None else 0.0
    inside = flood_u.intersection(warn_u).area if (flood_u is not None and warn_u is not None) else 0.0

    # flooded km2 by county (from tract_flood if available)
    by_county = {}
    tf_path = d / "tract_flood.parquet"
    if tf_path.exists():
        tf = pd.read_parquet(tf_path)
        tr = gpd.read_parquet(d / "tracts.parquet")[["geoid", "county_name", "state_fips"]]
        m = tf.merge(tr, on="geoid", how="left")
        m["county"] = m["county_name"] + " (" + m["state_fips"].map({"12": "FL", "01": "AL", "13": "GA"}).fillna(m["state_fips"]) + ")"
        s = m.groupby("county")["flooded_area_m2"].sum().sort_values(ascending=False) / 1e6
        by_county = {k: round(float(v), 3) for k, v in s[s > 0].head(25).items()}

    declared = fema_declared_counties(storm_key)
    out = {
        "n_warnings": int(len(warn)),
        "n_with_geometry": int(len(warn)),
        "warning_events": warn["event"].value_counts().to_dict() if len(warn) else {},
        "warnings_available": bool(len(warn)),
        "warning_window_utc": [str(pd.Timestamp(st["landfall_time_utc"]) - pd.Timedelta(days=1)), str(pd.Timestamp(st["landfall_time_utc"]) + pd.Timedelta(days=3))],
        "flooded_km2": round(flood_area / 1e6, 3),
        "pct_flood_area_inside_warnings": round(100 * inside / flood_area, 1) if flood_area else None,
        "pct_warning_area_flooded": round(100 * inside / warn_in_cov.area, 3) if (warn_in_cov is not None and warn_in_cov.area) else None,
        "pct_sar_coverage_under_warning": round(100 * warn_in_cov.area / cov_m.area, 1) if warn_in_cov is not None else None,
        "flooded_km2_by_county": by_county,
        "fema_declared_counties": sorted(declared["designatedArea"].astype(str).str.replace(" (County)", "", regex=False).add(" " + declared["state"]).tolist()),
        "warnings_geojson": json.loads(warn[["event", "area", "sent", "geometry"]].to_json()),
        "verdict": _verdict(flood_area / 1e6, inside / flood_area if flood_area else 0.0, meta),
    }
    (d / "nws_sanity.json").write_text(json.dumps(out))
    print(f"[sanity] flooded {out['flooded_km2']} km2 over {meta['coverage_pct_of_aoi']}% of AOI; "
          f"{out['pct_flood_area_inside_warnings']}% of flood area lies inside NWS warning polygons; "
          f"{out['pct_sar_coverage_under_warning']}% of the SAR footprint was under a warning. Verdict: {out['verdict']}")
    print("[sanity] top counties by flooded km2:", dict(list(by_county.items())[:8]))
    return out


def _verdict(flood_km2: float, frac_inside: float, meta: dict) -> str:
    land_km2 = sum(f.get("land_km2", 0) for f in meta.get("frame_stats", []))
    if land_km2 and flood_km2 / land_km2 > 0.15:
        return "SUSPICIOUS: >15% of covered land flagged — threshold probably too loose"
    if flood_km2 < 0.5:
        return "SUSPICIOUS: almost nothing flagged — check acquisition timing / thresholds"
    return "plausible"


if __name__ == "__main__":
    import sys

    run(sys.argv[1] if len(sys.argv) > 1 else "isaias")
