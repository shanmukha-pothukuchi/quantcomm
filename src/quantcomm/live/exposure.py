"""Milestone 5: pre-event exposure under active alerts + Sentinel-1 pass status."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import box

from quantcomm import config
from quantcomm.flood.catalog import mpc_scenes
from quantcomm.live.alerts import OUT, run_alerts

log = logging.getLogger(__name__)

FDIC_SOD = "https://api.fdic.gov/banks/sod"
SOD_FIELDS = "CERT,NAMEFULL,BRNUM,NAMEBR,CITYBR,STALPBR,DEPSUMBR,SIMS_LATITUDE,SIMS_LONGITUDE,RSSDHCR,NAMEHCR"
RAW_FDIC = config.DATA_RAW / "fdic"
SOD_YEAR = 2026  # latest vintage (verified present, index built 2026-09-18)
BUFFER_M = 500
CRS_EQ = "EPSG:5070"
REPEAT_DAYS = 12


# --------------------------------------------------------------------------- FDIC branches
def fdic_branches_state(state: str, year: int = SOD_YEAR) -> pd.DataFrame:
    cache = RAW_FDIC / f"live_sod_{year}_{state}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    rows, offset = [], 0
    while True:
        r = requests.get(FDIC_SOD, params={"filters": f"YEAR:{year} AND STALPBR:{state}", "fields": SOD_FIELDS, "limit": 10000, "offset": offset}, timeout=180)
        r.raise_for_status()
        d = r.json()
        rows += [x["data"] for x in d["data"]]
        total = d["totals"]["count"]
        offset += 10000
        if offset >= total:
            break
    df = pd.DataFrame(rows)
    if len(df) != total:
        raise RuntimeError(f"FDIC SOD {state} {year}: got {len(df)} rows, expected {total}")
    RAW_FDIC.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    return df


def fdic_national_totals(certs: list[int], year: int = SOD_YEAR) -> pd.DataFrame:
    """Nationwide deposits per CERT from the aggregation endpoint (verified: agg_by=CERT, agg_sum_fields=DEPSUMBR)."""
    cache = RAW_FDIC / f"live_sod_{year}_cert_totals.parquet"
    have = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=["cert", "total_deposits_k", "n_branches_total"])
    missing = [c for c in certs if c not in set(have["cert"])]
    rows = []
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        flt = f"YEAR:{year} AND CERT:(" + " OR ".join(str(c) for c in chunk) + ")"
        r = requests.get(FDIC_SOD, params={"filters": flt, "fields": "CERT,DEPSUMBR", "agg_by": "CERT", "agg_sum_fields": "DEPSUMBR", "agg_limit": 1000, "limit": 1}, timeout=180)
        r.raise_for_status()
        for x in r.json()["data"]:
            rows.append({"cert": int(x["data"]["CERT"]), "total_deposits_k": float(x["data"]["sum_DEPSUMBR"]), "n_branches_total": int(x["data"]["count"])})
    if rows:
        have = pd.concat([have, pd.DataFrame(rows)], ignore_index=True)
        RAW_FDIC.mkdir(parents=True, exist_ok=True)
        have.to_parquet(cache, index=False)
    got = set(have["cert"])
    still = [c for c in certs if c not in got]
    if still:
        raise RuntimeError(f"FDIC aggregation returned no national total for certs {still[:10]}")
    return have[have["cert"].isin(certs)].reset_index(drop=True)


# --------------------------------------------------------------------------- point-in-alert
def points_in_alerts(points: gpd.GeoDataFrame, alerts: gpd.GeoDataFrame, buffer_m: float = BUFFER_M) -> gpd.GeoDataFrame:
    """Add in_alert, alert_events (joined ';'), max_severity. Row count preserved."""
    n = len(points)
    if alerts.empty or points.empty:
        out = points.copy()
        out["in_alert"] = False; out["alert_events"] = ""; out["max_severity"] = ""
        return out
    p = points.to_crs(CRS_EQ).copy(); p["_i"] = np.arange(n)
    a = alerts.to_crs(CRS_EQ)[["event", "severity", "geometry"]].copy()
    if buffer_m:
        a["geometry"] = a.geometry.buffer(buffer_m)
    j = gpd.sjoin(p[["_i", "geometry"]], a, how="inner", predicate="intersects")
    sev_rank = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
    agg = j.groupby("_i").agg(alert_events=("event", lambda s: ";".join(sorted(set(s)))),
                              max_severity=("severity", lambda s: max(s, key=lambda x: sev_rank.get(x, 0))))
    out = points.copy()
    out["in_alert"] = pd.Series(np.arange(n)).isin(agg.index).values
    out["alert_events"] = pd.Series(np.arange(n)).map(agg["alert_events"]).fillna("").values
    out["max_severity"] = pd.Series(np.arange(n)).map(agg["max_severity"]).fillna("").values
    assert len(out) == n
    return out


def bank_alert_exposure(alerts: gpd.GeoDataFrame) -> pd.DataFrame:
    """Per bank: branches and deposits inside active alert polygons, and share of nationwide deposits."""
    states = sorted({s.strip() for a in alerts["areaDesc"].fillna("") for s in _states_from_areadesc(a)} | set(_states_from_bbox(alerts)))
    if not states:
        return pd.DataFrame()
    br = pd.concat([fdic_branches_state(s) for s in states], ignore_index=True)
    br = br.dropna(subset=["SIMS_LATITUDE", "SIMS_LONGITUDE"])
    pts = gpd.GeoDataFrame(br, geometry=gpd.points_from_xy(br["SIMS_LONGITUDE"], br["SIMS_LATITUDE"]), crs="EPSG:4326")
    ex = points_in_alerts(pts, alerts)
    hit = ex[ex["in_alert"]]
    if hit.empty:
        return pd.DataFrame(columns=["cert", "bank_name", "holding_company", "rssdhcr", "n_branches_in_alert", "deposits_in_alert_k", "total_deposits_k", "n_branches_total", "exposure_pct", "alert_events"])
    g = hit.groupby("CERT").agg(bank_name=("NAMEFULL", "first"), holding_company=("NAMEHCR", "first"), rssdhcr=("RSSDHCR", "first"),
                                n_branches_in_alert=("BRNUM", "size"), deposits_in_alert_k=("DEPSUMBR", "sum"),
                                alert_events=("alert_events", lambda s: ";".join(sorted({e for x in s for e in x.split(";") if e})))).reset_index().rename(columns={"CERT": "cert"})
    tot = fdic_national_totals([int(c) for c in g["cert"]])
    g = g.merge(tot, on="cert", how="left", validate="1:1")
    g["exposure_pct"] = 100 * g["deposits_in_alert_k"] / g["total_deposits_k"]
    g.attrs["branches_checked"] = int(len(ex)); g.attrs["branches_in_alert"] = int(len(hit)); g.attrs["states"] = states
    return g.sort_values("deposits_in_alert_k", ascending=False).reset_index(drop=True)


def _states_from_areadesc(area: str) -> list[str]:
    import re
    return re.findall(r",\s*([A-Z]{2})(?:;|$)", area)


def _states_from_bbox(alerts: gpd.GeoDataFrame) -> list[str]:
    """States whose TIGER county polygons intersect any alert (zone-based alerts do not carry state codes in areaDesc)."""
    cz = config.DATA_RAW / "census" / "tl_2024_us_county.zip"
    if not cz.exists() or alerts.empty:
        return []
    from quantcomm.census.acs import FIPS_STATE
    c = gpd.read_file(f"zip://{cz}", bbox=tuple(alerts.total_bounds))
    hit = gpd.sjoin(c[["STATEFP", "geometry"]], alerts[["geometry"]], predicate="intersects")
    return sorted({FIPS_STATE.get(s, s) for s in hit["STATEFP"].unique() if s in FIPS_STATE})


def nessie_alert_exposure(alerts: gpd.GeoDataFrame) -> pd.DataFrame:
    loc = pd.read_parquet(config.DATA_PROCESSED / "isaias" / "nessie_locations.parquet")
    pts = gpd.GeoDataFrame(loc, geometry=gpd.points_from_xy(loc["lon"], loc["lat"]), crs="EPSG:4326")
    return pd.DataFrame(points_in_alerts(pts, alerts).drop(columns="geometry"))


def portfolio_alert_exposure(alerts: gpd.GeoDataFrame) -> pd.DataFrame | None:
    p = config.DATA_PROCESSED / "isaias" / "SYNTHETIC_portfolio.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")
    return pd.DataFrame(points_in_alerts(pts, alerts).drop(columns="geometry"))


# --------------------------------------------------------------------------- satellite status
def satellite_status(bboxes: dict[str, list[float]], now: pd.Timestamp | None = None) -> dict:
    """Last Sentinel-1 RTC pass per relative orbit over each bbox in the last 30 days, and the next expected (+12 d)."""
    now = now or pd.Timestamp.now(tz="UTC")
    out = {"checked_utc": now.isoformat(), "areas": {}}
    for name, bbox in bboxes.items():
        start = (now - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        cache = config.DATA_RAW / "live" / f"s1_{name}_{now.strftime('%Y%m%d%H')}.json"
        try:
            items = mpc_scenes(bbox, start, None, "sentinel-1-rtc", cache)
        except RuntimeError as e:
            out["areas"][name] = {"error": str(e)}
            continue
        aoi = box(*bbox)
        rows = []
        for it in items:
            from shapely.geometry import shape as _shape
            p = it["properties"]
            rows.append({"relative_orbit": int(p["sat:relative_orbit"]), "pass_direction": p.get("sat:orbit_state"), "platform": p.get("platform"),
                         "acquired_utc": pd.Timestamp(p["datetime"]), "aoi_coverage_pct": 100 * _shape(it["geometry"]).intersection(aoi).area / aoi.area})
        df = pd.DataFrame(rows)
        df = df[df["aoi_coverage_pct"] >= 0.5]
        passes = df.groupby(["relative_orbit", "pass_direction"]).agg(last_pass_utc=("acquired_utc", "max"), platform=("platform", "last"),
                                                                     coverage_pct=("aoi_coverage_pct", "sum"), n_scenes_30d=("acquired_utc", "size")).reset_index()
        passes["next_expected_utc"] = passes["last_pass_utc"] + pd.Timedelta(days=REPEAT_DAYS)
        passes.loc[passes["next_expected_utc"] < now, "next_expected_utc"] += pd.Timedelta(days=REPEAT_DAYS)
        passes = passes.sort_values("last_pass_utc", ascending=False)
        out["areas"][name] = {"bbox": bbox, "last_pass_utc": passes["last_pass_utc"].max().isoformat() if len(passes) else None,
                              "next_expected_utc": passes["next_expected_utc"].min().isoformat() if len(passes) else None,
                              "orbits": json.loads(passes.to_json(orient="records", date_format="iso"))}
    return out


def flag_new_passes(status: dict, storm_key: str = "isaias") -> dict:
    meta_p = config.storm_dir(storm_key) / "flood_meta.json"
    if not meta_p.exists() or storm_key not in status["areas"] or "orbits" not in status["areas"][storm_key]:
        return {"storm": storm_key, "note": "no processed flood map or no catalog"}
    meta = json.loads(meta_p.read_text())
    processed = max(pd.Timestamp(x) for x in [meta["post_acquired_utc"]] + [p["post_acquired_utc"] for p in meta.get("passes", [])])
    newer = [o for o in status["areas"][storm_key]["orbits"] if pd.Timestamp(o["last_pass_utc"]) > processed + pd.Timedelta(hours=1)]
    return {"storm": storm_key, "processed_through_utc": processed.isoformat(), "new_passes": newer,
            "refresh_recommended": bool(newer), "command": f"scripts/run_storm.py --storm {storm_key} --steps flood,exposure,sanity,stress --force" if newer else None}


CONUS_BBOX = [-125.0, 24.0, -66.0, 50.0]


def conus_only(alerts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep alerts touching the contiguous US (drops Guam/Hawaii/Alaska/PR, which would blow up the satellite bbox)."""
    if alerts.empty:
        return alerts
    keep = alerts.geometry.intersects(box(*CONUS_BBOX))
    out = alerts[keep].copy()
    out.attrs = dict(alerts.attrs)
    out.attrs["n_outside_conus"] = int((~keep).sum())
    return out


# --------------------------------------------------------------------------- driver
def run_live(force: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    alerts_all, nhc = run_alerts(force=force)
    alerts = conus_only(alerts_all)
    bank = bank_alert_exposure(alerts) if len(alerts) else pd.DataFrame()
    bank.to_parquet(OUT / "bank_alert_exposure.parquet", index=False)
    ness = nessie_alert_exposure(alerts)
    ness.to_parquet(OUT / "nessie_alert_exposure.parquet", index=False)
    port = portfolio_alert_exposure(alerts)
    if port is not None:
        port.to_parquet(OUT / "SYNTHETIC_portfolio_alert_exposure.parquet", index=False)
    bboxes = {"isaias": config.storm("isaias")["aoi_bbox"]}
    if len(alerts):
        b = alerts.total_bounds
        bboxes["active_alerts"] = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        coastal = alerts[alerts["event"].str.startswith("Coastal Flood")]
        if len(coastal):
            b = coastal.total_bounds
            bboxes["coastal_flood_alerts"] = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
    sat = satellite_status(bboxes)
    sat["isaias_refresh"] = flag_new_passes(sat, "isaias")
    (OUT / "satellite_status.json").write_text(json.dumps(sat, default=str, indent=1))
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "alerts_fetched_utc": alerts.attrs.get("fetched_utc"),
        "n_alerts_total": alerts.attrs.get("n_alerts_total"), "n_alerts_with_geometry": int(len(alerts_all)), "n_alerts_unresolved": alerts.attrs.get("n_unresolved"),
        "n_alerts_conus": int(len(alerts)), "n_alerts_outside_conus": alerts.attrs.get("n_outside_conus", 0), "scope": "exposure and satellite status use CONUS alerts only",
        "alerts_by_event": alerts["event"].value_counts().to_dict() if len(alerts) else {},
        "nhc_active_storms": [{k: s.get(k) for k in ("id", "name", "classification", "basin")} for s in nhc["active_storms"]],
        "banks": {"n_banks_with_branches_in_alert": int(len(bank)), "branches_checked": bank.attrs.get("branches_checked"), "branches_in_alert": bank.attrs.get("branches_in_alert"),
                  "states_checked": bank.attrs.get("states"), "deposits_in_alert_k_total": float(bank["deposits_in_alert_k"].sum()) if len(bank) else 0.0,
                  "top10": bank.head(10)[["cert", "bank_name", "n_branches_in_alert", "deposits_in_alert_k", "exposure_pct", "alert_events"]].to_dict(orient="records") if len(bank) else []},
        "nessie": {"n_locations": int(len(ness)), "n_in_alert": int(ness["in_alert"].sum()), "by_kind_in_alert": ness[ness["in_alert"]].groupby("kind").size().to_dict() if ness["in_alert"].any() else {}},
        "synthetic_portfolio": None if port is None else {"n_customers": int(len(port)), "n_in_alert": int(port["in_alert"].sum()), "loan_amount_in_alert": float(port.loc[port["in_alert"], "loan_amount"].sum())},
        "satellite": {k: {"last_pass_utc": v.get("last_pass_utc"), "next_expected_utc": v.get("next_expected_utc")} for k, v in sat["areas"].items()},
        "isaias_refresh": sat["isaias_refresh"],
        "buffer_m": BUFFER_M,
        "sod_vintage": SOD_YEAR,
    }
    (OUT / "live_summary.json").write_text(json.dumps(summary, default=str, indent=1))
    return summary
