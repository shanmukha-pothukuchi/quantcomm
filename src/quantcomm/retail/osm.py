"""Store footprints + adjacent parking lots from OpenStreetMap (Overpass API, keyless).

Raw Overpass responses are cached per metro in data/raw/retail/; the processed table is one row per
store with its building polygon and the union of OSM `amenity=parking` polygons within
`lot_search_radius_deg` of it (minus the building), in EPSG:4326.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Polygon
from shapely.ops import unary_union

from quantcomm import config

log = logging.getLogger(__name__)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RAW = config.DATA_RAW / "retail"
EQUAL_AREA_CRS = "EPSG:5070"
HEADERS = {"User-Agent": "quantcomm-research/0.1 (hackathon; contact via repo)"}


def overpass_query(brand_wikidata: str, shop_regex: str, bbox: list[float]) -> str:
    s, w, n, e = bbox
    return f"""[out:json][timeout:120];
way["brand:wikidata"="{brand_wikidata}"]["shop"~"{shop_regex}"]({s},{w},{n},{e})->.s;
.s out geom tags;
way(around.s:100)["amenity"="parking"]->.p;
.p out geom tags;"""


def _has_parking(d: dict) -> bool:
    return any(e.get("tags", {}).get("amenity") == "parking" for e in d["elements"])


def fetch_metro(ticker: str, metro: str, spec: dict, force: bool = False, retries: int = 4,
                empty_confirmations: int = 2) -> dict:
    """Overpass sometimes returns the first statement and silently drops the second (no parking ways).
    A parking-less answer is retried; after `empty_confirmations` identical empty answers it is accepted
    as real (the metro's lots just aren't mapped next to the stores)."""
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / f"osm_{ticker}_{metro}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text())
    q = overpass_query(spec["brand_wikidata"], spec["shop_regex"], spec["metros"][metro])
    last_err, empties = None, 0
    for attempt in range(retries):
        try:
            r = requests.post(OVERPASS_URL, data={"data": q}, timeout=180, headers=HEADERS)
            r.raise_for_status()
            d = r.json()
            if "elements" not in d:
                raise ValueError(f"Overpass response has no 'elements': {str(d)[:200]}")
            if _has_parking(d) or (empties := empties + 1) >= empty_confirmations:
                if not _has_parking(d):
                    log.warning("%s/%s: no parking mapped next to %d store ways (confirmed %dx)", ticker, metro,
                                len(d["elements"]), empties)
                cache.write_text(json.dumps(d))
                return d
            raise ValueError(f"no parking ways in response ({len(d['elements'])} elements) - retrying")
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            log.warning("Overpass %s/%s attempt %d: %s", ticker, metro, attempt + 1, exc)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Overpass fetch failed for {ticker}/{metro} after {retries} tries: {last_err}")


def _polygon(el: dict) -> Polygon | None:
    pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
    if len(pts) < 4:
        return None
    p = Polygon(pts)
    if not p.is_valid:
        p = p.buffer(0)  # self-intersecting OSM rings
    return p if p.area > 0 else None


def stores_from_overpass(d: dict, ticker: str, metro: str, brand_wikidata: str, radius_deg: float,
                         min_lot_area_m2: float) -> gpd.GeoDataFrame:
    """One row per store footprint that has >= min_lot_area_m2 of OSM parking next to it."""
    els = d["elements"]
    stores = [(e, _polygon(e)) for e in els if e.get("tags", {}).get("brand:wikidata") == brand_wikidata]
    lots = [p for e in els if e.get("tags", {}).get("amenity") == "parking" and (p := _polygon(e)) is not None]
    rows = []
    for e, bldg in stores:
        if bldg is None:
            continue
        near = [p for p in lots if p.distance(bldg) < radius_deg]
        if not near:
            continue
        lot = unary_union(near).difference(bldg)
        if lot.is_empty:
            continue
        t = e.get("tags", {})
        rows.append({"store_id": f"osm_way_{e['id']}", "ticker": ticker, "metro": metro, "name": t.get("name", ""),
                     "city": t.get("addr:city", ""), "lot_geometry_wkt": lot.wkt, "geometry": bldg})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326") if rows else \
        gpd.GeoDataFrame(columns=["store_id", "ticker", "metro", "name", "city", "lot_geometry_wkt", "geometry"],
                         geometry="geometry", crs="EPSG:4326")
    if gdf.empty:
        return gdf.assign(lot_area_m2=[])
    lot_area = gpd.GeoSeries.from_wkt(gdf["lot_geometry_wkt"], crs="EPSG:4326").to_crs(EQUAL_AREA_CRS).area
    gdf = gdf.assign(lot_area_m2=lot_area.values)
    # one footprint can be split into several OSM ways sharing a lot; keep each store once
    return gdf[gdf["lot_area_m2"] >= min_lot_area_m2].drop_duplicates("store_id").reset_index(drop=True)


def build_stores(ticker: str, force: bool = False) -> gpd.GeoDataFrame:
    sig = config.load_yaml("signals.yaml")
    spec, p = sig["retail"][ticker], sig["parking_sar"]
    out_dir = config.DATA_PROCESSED / "retail"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{ticker}_stores.parquet"
    if out.exists() and not force:
        return gpd.read_parquet(out)
    parts = []
    for metro in spec["metros"]:
        try:
            d = fetch_metro(ticker, metro, spec, force=force)
        except RuntimeError as exc:  # one metro down shouldn't sink the others; logged and reported
            log.error("%s %s skipped: %s", ticker, metro, exc)
            continue
        g = stores_from_overpass(d, ticker, metro, spec["brand_wikidata"], p["lot_search_radius_deg"], p["min_lot_area_m2"])
        log.info("%s %s: %d stores with >= %d m2 of parking", ticker, metro, len(g), p["min_lot_area_m2"])
        parts.append(g)
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    if gdf.empty:
        raise RuntimeError(f"no {ticker} stores with mapped parking found in any metro - check the OSM query")
    gdf.to_parquet(out)
    return gdf


def cache_path(ticker: str) -> Path:
    return config.DATA_PROCESSED / "retail" / f"{ticker}_stores.parquet"
