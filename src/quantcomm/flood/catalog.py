"""Sentinel-1 scene catalog for a storm AOI.

Two sources, both keyless:
  * Microsoft Planetary Computer STAC `sentinel-1-rtc`  -> the imagery we actually process.
  * Copernicus Data Space OData catalog                  -> `PublicationDate`, i.e. when the scene
    became publicly available. That timestamp is the event time for the market test (no lookahead).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import planetary_computer as pc
import pystac_client
import requests
from shapely.geometry import box, shape

from quantcomm import config, schemas

MPC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
CDSE_ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def mpc_client() -> pystac_client.Client:
    return pystac_client.Client.open(MPC_STAC, modifier=pc.sign_inplace)


def _scene_key(name: str) -> str:
    """S1D_IW_GRDH_1SDV_20261010T234452_20261010T234522 — identical between MPC ids and CDSE names."""
    return "_".join(name.split("_")[:6])


def cdse_publication_times(bbox: list[float], start: str, end: str | None, cache: Path) -> dict[str, str]:
    """Earliest Copernicus PublicationDate per scene key. Cached as JSON."""
    if cache.exists():
        return json.loads(cache.read_text())
    w, s, e, n = bbox
    poly = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    flt = (
        "Collection/Name eq 'SENTINEL-1' and contains(Name,'GRDH') and contains(Name,'_IW_') "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{poly}') "
        f"and ContentDate/Start gt {start}T00:00:00.000Z"
    )
    if end:
        flt += f" and ContentDate/Start lt {end}T23:59:59.000Z"
    out: dict[str, str] = {}
    url = CDSE_ODATA
    params = {"$filter": flt, "$orderby": "ContentDate/Start asc", "$top": 1000}
    while url:
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
        d = r.json()
        for p in d.get("value", []):
            k = _scene_key(p["Name"])
            pub = p.get("PublicationDate")
            if pub and (k not in out or pub < out[k]):
                out[k] = pub
        url = d.get("@odata.nextLink")
        params = None
    if not out:
        raise RuntimeError("CDSE catalog returned no Sentinel-1 GRD scenes for the AOI/window")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=1))
    return out


def mpc_scenes(bbox: list[float], start: str, end: str | None, collection: str, cache: Path) -> list[dict]:
    """STAC items (as dicts) intersecting the bbox. Cached as a JSON list."""
    if cache.exists():
        return json.loads(cache.read_text())
    end_s = end or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    search = mpc_client().search(collections=[collection], bbox=bbox, datetime=f"{start}/{end_s}", limit=500)
    items = [it.to_dict() for it in search.items()]
    if not items:
        raise RuntimeError(f"MPC {collection}: no scenes for bbox={bbox} {start}..{end_s}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(items))
    return items


def build_catalog(storm_key: str, force: bool = False) -> pd.DataFrame:
    st = config.storm(storm_key)
    mdl = config.model()["flood"]
    out_path = config.storm_dir(storm_key) / "s1_catalog.parquet"
    if out_path.exists() and not force:
        return pd.read_parquet(out_path)
    raw = config.storm_dir(storm_key, "raw") / "s1"
    bbox = st["aoi_bbox"]
    aoi = box(*bbox)
    landfall = pd.Timestamp(st["landfall_time_utc"])
    start = st.get("s1_search_start") or (landfall - pd.Timedelta(days=mdl["search_days_before"])).strftime("%Y-%m-%d")
    end = st.get("s1_search_end") or (landfall + pd.Timedelta(days=mdl["max_days_after"] + 2)).strftime("%Y-%m-%d")
    if pd.Timestamp(end, tz="UTC") > pd.Timestamp.now(tz="UTC"):
        end = None
    items = mpc_scenes(bbox, start, end, mdl["collection"], raw / "mpc_items.json")
    pubs = cdse_publication_times(bbox, start, end, raw / "cdse_publication.json")
    rows = []
    for it in items:
        p = it["properties"]
        g = shape(it["geometry"])
        key = _scene_key(it["id"])
        acquired = pd.Timestamp(p["datetime"])
        rows.append(
            {
                "scene_id": it["id"],
                "acquired_utc": acquired,
                "published_utc": pd.Timestamp(pubs[key]) if key in pubs else pd.NaT,
                "platform": p.get("platform"),
                "relative_orbit": int(p["sat:relative_orbit"]),
                "pass_direction": p.get("sat:orbit_state"),
                "aoi_coverage_pct": 100.0 * g.intersection(aoi).area / aoi.area,
                "is_post_landfall": bool(acquired > landfall),
                "geometry_json": json.dumps(it["geometry"]),
                "vv_href": it["assets"]["vv"]["href"].split("?")[0],  # unsigned; re-sign at read time
            }
        )
    df = pd.DataFrame(rows).sort_values("acquired_utc").reset_index(drop=True)
    missing_pub = df["published_utc"].isna().sum()
    if missing_pub:
        print(f"WARNING: {missing_pub}/{len(df)} MPC scenes have no Copernicus publication timestamp")
    df["published_utc"] = pd.to_datetime(df["published_utc"], utc=True)
    df["acquired_utc"] = pd.to_datetime(df["acquired_utc"], utc=True)
    schemas.check(df, schemas.S1_CATALOG, "s1_catalog")
    df.to_parquet(out_path, index=False)
    return df


def select_pair(cat: pd.DataFrame, landfall: pd.Timestamp, n_pre: int, relative_orbits: list[int] | None = None) -> dict:
    """Pick the orbit with the best post-landfall AOI coverage and the last n_pre pre-landfall passes on it.

    A 'pass' = all frames of one orbit acquired on one date. Returns scene ids plus the event time
    (= max publication time across the post scenes used — the mosaic was not usable before then).
    """
    post = cat[cat["is_post_landfall"]]
    if relative_orbits:
        post = post[post["relative_orbit"].isin(relative_orbits)]
    if post.empty:
        raise RuntimeError("no post-landfall Sentinel-1 scenes in the catalog")
    cov = post.groupby("relative_orbit")["aoi_coverage_pct"].sum().sort_values(ascending=False)
    orbit = int(cov.index[0])
    post_o = post[post["relative_orbit"] == orbit]
    first_date = post_o["acquired_utc"].dt.floor("D").min()
    post_pass = post_o[post_o["acquired_utc"].dt.floor("D") == first_date]
    pre = cat[(~cat["is_post_landfall"]) & (cat["relative_orbit"] == orbit)].copy()
    pre["pass_date"] = pre["acquired_utc"].dt.floor("D")
    pre_dates = sorted(pre["pass_date"].unique())[-n_pre:]
    if not pre_dates:
        raise RuntimeError(f"orbit {orbit}: no pre-landfall passes")
    pre_passes = {str(d.date()): pre[pre["pass_date"] == d]["scene_id"].tolist() for d in pre_dates}
    event_time = post_pass["published_utc"].max()
    return {
        "relative_orbit": orbit,
        "pass_direction": post_pass["pass_direction"].iloc[0],
        "post_scene_ids": post_pass["scene_id"].tolist(),
        "post_acquired_utc": post_pass["acquired_utc"].min().isoformat(),
        "pre_passes": pre_passes,
        "event_time_utc": event_time.isoformat() if pd.notna(event_time) else None,
        "post_coverage_pct_of_aoi": float(post_pass["aoi_coverage_pct"].sum()),
        "orbit_coverage_ranking": {int(k): float(v) for k, v in cov.items()},
    }


if __name__ == "__main__":
    import sys

    key = sys.argv[1] if len(sys.argv) > 1 else "isaias"
    cat = build_catalog(key, force="--force" in sys.argv)
    pd.set_option("display.width", 200)
    print(cat.drop(columns=["geometry_json", "vv_href"]).to_string())
    st = config.storm(key)
    sel = select_pair(cat, pd.Timestamp(st["landfall_time_utc"]), config.model()["flood"]["pre_images_per_orbit"], st["s1_relative_orbits"])
    print(json.dumps(sel, indent=1))


def select_passes(cat: pd.DataFrame, landfall: pd.Timestamp, n_pre: int, max_days_after: int,
                  relative_orbits: list[int] | None = None, min_coverage_pct: float = 0.5) -> list[dict]:
    """One entry per (orbit, first post-landfall pass date) within max_days_after, each with its own pre passes.

    Frames with < min_coverage_pct of the AOI are ignored (they only clip a corner). Sorted by acquisition time,
    so the first entry's publication time is the storm's event time.
    """
    c = cat[cat["aoi_coverage_pct"] >= min_coverage_pct].copy()
    c["pass_date"] = c["acquired_utc"].dt.floor("D")
    post = c[c["is_post_landfall"] & (c["acquired_utc"] <= landfall + pd.Timedelta(days=max_days_after))]
    if relative_orbits:
        post = post[post["relative_orbit"].isin(relative_orbits)]
    out = []
    for orbit, g in post.groupby("relative_orbit"):
        first = g["pass_date"].min()
        post_pass = g[g["pass_date"] == first]
        pre = c[(~c["is_post_landfall"]) & (c["relative_orbit"] == orbit)]
        pre_dates = sorted(pre["pass_date"].unique())[-n_pre:]
        if not pre_dates:
            print(f"WARNING: orbit {orbit} has a post pass but no pre pass; skipped")
            continue
        out.append({
            "pass_id": f"orb{int(orbit):03d}_{first.strftime('%Y%m%d')}",
            "relative_orbit": int(orbit),
            "pass_direction": post_pass["pass_direction"].iloc[0],
            "post_scene_ids": post_pass["scene_id"].tolist(),
            "post_acquired_utc": post_pass["acquired_utc"].min().isoformat(),
            "published_utc": post_pass["published_utc"].max().isoformat() if post_pass["published_utc"].notna().all() else None,
            "pre_passes": {str(d.date()): pre[pre["pass_date"] == d]["scene_id"].tolist() for d in pre_dates},
            "post_coverage_pct_of_aoi": float(post_pass["aoi_coverage_pct"].sum()),
        })
    if not out:
        raise RuntimeError(f"no usable post-landfall pass within {max_days_after} days")
    return sorted(out, key=lambda d: d["post_acquired_utc"])
