"""Milestone 5: live ingestion of NWS flood-type alerts (with zone geometry) and NHC active storms."""
from __future__ import annotations

import os

import io
import json
import logging
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape
from shapely.ops import unary_union

from floodalpha import config

log = logging.getLogger(__name__)

UA = {"User-Agent": f"floodalpha ({os.environ.get('FLOODALPHA_CONTACT', 'contact-not-set')})", "Accept": "application/geo+json"}
NWS_ACTIVE = "https://api.weather.gov/alerts/active"
NHC_CURRENT = "https://www.nhc.noaa.gov/CurrentStorms.json"
NHC_5DAY = "https://www.nhc.noaa.gov/gis/forecast/archive/{id}_5day_latest.zip"  # verified with ep212026 (2026-10-17)
EVENTS = ["Coastal Flood Warning", "Coastal Flood Watch", "Coastal Flood Advisory", "Flood Warning", "Flash Flood Warning",
          "Flood Watch", "Storm Surge Warning", "Storm Surge Watch", "Hurricane Warning", "Hurricane Watch",
          "Tropical Storm Warning", "Tropical Storm Watch"]
RAW = config.DATA_RAW / "nws"
ZONES = RAW / "zones"
OUT = config.DATA_PROCESSED / "live"
ALERT_TTL_S = 15 * 60


def _fresh(path: Path, ttl_s: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_s


def fetch_active_alerts(force: bool = False) -> dict:
    """Raw active-alert FeatureCollection, cached for 15 minutes at data/raw/nws/alerts_active.json."""
    raw = RAW / "alerts_active.json"
    if _fresh(raw, ALERT_TTL_S) and not force:
        return json.loads(raw.read_text())
    # note: /alerts/active rejects `limit`; it returns all matching alerts in one page (verified 2026-10-17)
    feats, url, params = [], NWS_ACTIVE, {"event": ",".join(EVENTS)}
    while url:
        r = requests.get(url, params=params, headers=UA, timeout=120)
        r.raise_for_status()
        d = r.json()
        feats += d["features"]
        url = d.get("pagination", {}).get("next")
        params = None
    fc = {"type": "FeatureCollection", "fetched_utc": datetime.now(timezone.utc).isoformat(), "features": feats}
    RAW.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(fc))
    return fc


def zone_geometry(zone_url: str) -> dict | None:
    """GeoJSON geometry of an NWS zone (forecast/county/fire). Cached forever per zone id."""
    zid = zone_url.rstrip("/").split("/")[-1]
    kind = zone_url.rstrip("/").split("/")[-2]
    ZONES.mkdir(parents=True, exist_ok=True)
    cache = ZONES / f"{kind}_{zid}.json"
    if cache.exists():
        return json.loads(cache.read_text()).get("geometry")
    for attempt in range(3):
        r = requests.get(zone_url, headers=UA, timeout=60)
        if r.ok:
            break
        time.sleep(1 + attempt)
    if not r.ok:
        log.warning("zone %s: HTTP %s", zone_url, r.status_code)
        return None
    f = r.json()
    cache.write_text(json.dumps({"id": zone_url, "geometry": f.get("geometry")}))
    return f.get("geometry")


def _geom_from(geojson_geom: dict | None):
    if not geojson_geom:
        return None
    g = shape(geojson_geom)
    if g.geom_type == "GeometryCollection":
        g = unary_union([x for x in g.geoms if x.geom_type in ("Polygon", "MultiPolygon")])
    return g if not g.is_empty else None


def alerts_gdf(fc: dict | None = None, force: bool = False) -> gpd.GeoDataFrame:
    """One row per active alert with a polygon: own geometry if present, else union of its zones."""
    fc = fc or fetch_active_alerts(force=force)
    rows, unresolved = [], 0
    for f in fc["features"]:
        p = f["properties"]
        if f.get("geometry"):
            geom = _geom_from(f["geometry"])
            src = "alert_polygon"
        else:
            parts = [_geom_from(zone_geometry(z)) for z in p.get("affectedZones", [])]
            parts = [g for g in parts if g is not None]
            geom = unary_union(parts) if parts else None
            src = f"zones:{len(parts)}/{len(p.get('affectedZones', []))}"
        if geom is None:
            unresolved += 1
            continue
        rows.append({"alert_id": p["id"], "event": p["event"], "severity": p.get("severity"), "certainty": p.get("certainty"),
                     "headline": p.get("headline"), "areaDesc": p.get("areaDesc"), "sent": p.get("sent"), "onset": p.get("onset"),
                     "ends": p.get("ends") or p.get("expires"), "senderName": p.get("senderName"), "geometry_source": src, "geometry": geom})
    if unresolved:
        log.warning("%d alerts had no resolvable geometry and were dropped", unresolved)
    g = gpd.GeoDataFrame(rows, crs="EPSG:4326") if rows else gpd.GeoDataFrame(columns=["alert_id", "event", "geometry"], geometry="geometry", crs="EPSG:4326")
    g.attrs["fetched_utc"] = fc.get("fetched_utc")
    g.attrs["n_alerts_total"] = len(fc["features"])
    g.attrs["n_unresolved"] = unresolved
    return g


def fetch_nhc(force: bool = False) -> dict:
    """Active NHC storms; for Atlantic storms also the 5-day cone/track/points as GeoJSON. Cached 15 min."""
    raw = RAW / "nhc_current.json"
    if _fresh(raw, ALERT_TTL_S) and not force:
        return json.loads(raw.read_text())
    r = requests.get(NHC_CURRENT, headers=UA, timeout=60)
    r.raise_for_status()
    storms = r.json().get("activeStorms", [])
    out = {"fetched_utc": datetime.now(timezone.utc).isoformat(), "active_storms": [], "atlantic": []}
    for s in storms:
        rec = {k: s.get(k) for k in ("id", "binNumber", "name", "classification", "intensity", "pressure", "latitudeNumeric", "longitudeNumeric", "movementDir", "movementSpeed", "lastUpdate")}
        rec["basin"] = "atlantic" if s["id"].lower().startswith("al") else ("east_pacific" if s["id"].lower().startswith("ep") else "other")
        out["active_storms"].append(rec)
        if rec["basin"] == "atlantic":
            try:
                rec_geo = nhc_forecast_geojson(s["id"])
                out["atlantic"].append({**rec, **rec_geo})
            except Exception as e:  # noqa: BLE001
                log.warning("NHC forecast products for %s failed: %s", s["id"], e)
                out["atlantic"].append({**rec, "error": str(e)})
    RAW.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(out))
    return out


def nhc_forecast_geojson(storm_id: str) -> dict:
    """Cone polygon, track line, forecast points from the *_5day_latest.zip shapefile bundle."""
    r = requests.get(NHC_5DAY.format(id=storm_id.lower()), headers=UA, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    tmp = RAW / "nhc_zips" / f"{storm_id.lower()}_5day_latest.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(r.content)
    out = {}
    for key, pat in (("cone", "_pgn"), ("track", "_lin"), ("points", "_pts")):
        shp = [n for n in names if pat in n.lower() and n.lower().endswith(".shp")]
        if shp:
            g = gpd.read_file(f"zip://{tmp}!{shp[0]}").to_crs(4326)
            out[key] = json.loads(g.to_json(default=str))
    return out


def run_alerts(force: bool = False) -> tuple[gpd.GeoDataFrame, dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    g = alerts_gdf(force=force)
    g.to_file(OUT / "alerts.geojson", driver="GeoJSON")
    nhc = fetch_nhc(force=force)
    (OUT / "nhc.json").write_text(json.dumps(nhc, default=str))
    return g, nhc
