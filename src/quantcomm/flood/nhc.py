"""NHC best-track products: download, parse, derive CONUS landfall time and a wind-swath AOI."""
from __future__ import annotations

import calendar
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from quantcomm import config

NON_CONUS = {"02", "15", "60", "66", "69", "72", "78"}
_MON = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}


def best_track_zip(nhc_id: str) -> Path:
    z = config.DATA_RAW / "nhc" / f"{nhc_id}_best_track.zip"
    if not z.exists():
        z.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(f"https://www.nhc.noaa.gov/gis/best_track/{nhc_id}_best_track.zip", timeout=120)
        r.raise_for_status()
        z.write_bytes(r.content)
    return z


def _month(v) -> int:
    s = str(v).strip().upper()
    return int(float(s)) if s.replace(".", "").isdigit() else _MON[s[:3]]


def best_track_points(nhc_id: str) -> gpd.GeoDataFrame:
    z = best_track_zip(nhc_id)
    names = zipfile.ZipFile(z).namelist()
    pts = gpd.read_file(f"zip://{z}!{[n for n in names if n.endswith('_pts.shp')][0]}")
    pts["dtg_utc"] = [
        pd.Timestamp(int(float(y)), _month(m), int(float(d)), int(float(h)) // 100, int(float(h)) % 100, tz="UTC")
        for y, m, d, h in zip(pts.YEAR, pts.MONTH, pts.DAY, pts.HHMM)
    ]
    pts["INTENSITY"] = pts["INTENSITY"].astype(float)
    return pts


def wind_swath(nhc_id: str, radii_kt: int = 34):
    z = best_track_zip(nhc_id)
    names = zipfile.ZipFile(z).namelist()
    sw = gpd.read_file(f"zip://{z}!{[n for n in names if 'windswath' in n and n.endswith('.shp')][0]}")
    return sw[sw.RADII.astype(float) == radii_kt].geometry.union_all()


def conus_land():
    cz = config.DATA_RAW / "census" / "tl_2024_us_county.zip"
    if not cz.exists():
        cz.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get("https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip", timeout=300)
        r.raise_for_status()
        cz.write_bytes(r.content)
    c = gpd.read_file(f"zip://{cz}")
    return c[~c.STATEFP.isin(NON_CONUS)].geometry.union_all()


def derive(nhc_id: str, land=None) -> dict:
    """First 6-hourly best-track point over CONUS land (landfall, ±6 h) and the 34-kt swath ∩ land bbox."""
    land = land if land is not None else conus_land()
    pts = best_track_points(nhc_id)
    on = pts[pts.geometry.within(land)]
    if on.empty:
        raise RuntimeError(f"{nhc_id}: no best-track point over CONUS land")
    lf = on.iloc[0]
    sw = wind_swath(nhc_id).intersection(land)
    b = [round(x, 2) for x in sw.bounds]
    return {"name": str(pts.STORMNAME.iloc[-1]).title(), "landfall_time_utc": lf.dtg_utc.isoformat(), "landfall_lat": float(lf.LAT),
            "landfall_lon": float(lf.LON), "landfall_kt": float(lf.INTENSITY), "swath34_bbox": b,
            "swath34_land_km2": round(float(gpd.GeoSeries([sw], crs=4326).to_crs(5070).area.iloc[0] / 1e6))}


if __name__ == "__main__":
    import sys
    land = conus_land()
    for sid in sys.argv[1:]:
        print(sid, derive(sid, land))
