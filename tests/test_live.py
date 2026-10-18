"""Live-mode tests: zone resolution on a cached fixture, point-in-alert exposure on toy data, satellite status math."""
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from floodalpha.live import alerts as A
from floodalpha.live import exposure as E


def test_alerts_gdf_resolves_zone_geometry(tmp_path, monkeypatch):
    # cached zone fixture: a GeometryCollection with one polygon (the NWS format) + one point
    monkeypatch.setattr(A, "ZONES", tmp_path)
    zone = {"id": "https://api.weather.gov/zones/forecast/VAZ089",
            "geometry": {"type": "GeometryCollection", "geometries": [
                {"type": "MultiPolygon", "coordinates": [[[[-76.5, 36.8], [-76.0, 36.8], [-76.0, 37.2], [-76.5, 37.2], [-76.5, 36.8]]]]},
                {"type": "Point", "coordinates": [-76.2, 37.0]}]}}
    (tmp_path / "forecast_VAZ089.json").write_text(json.dumps(zone))
    fc = {"fetched_utc": "2026-10-17T20:00:00+00:00", "features": [
        {"geometry": None, "properties": {"id": "a1", "event": "Coastal Flood Watch", "severity": "Moderate", "headline": "h", "areaDesc": "Norfolk",
                                          "sent": "s", "onset": "o", "ends": "e", "affectedZones": ["https://api.weather.gov/zones/forecast/VAZ089"]}},
        {"geometry": {"type": "Polygon", "coordinates": [[[-98.0, 33.0], [-97.0, 33.0], [-97.0, 34.0], [-98.0, 34.0], [-98.0, 33.0]]]},
         "properties": {"id": "a2", "event": "Flood Warning", "severity": "Severe", "headline": "h", "areaDesc": "Wichita, TX", "sent": "s", "onset": "o", "ends": "e", "affectedZones": []}},
        {"geometry": None, "properties": {"id": "a3", "event": "Flood Watch", "severity": "Minor", "headline": "h", "areaDesc": "x", "sent": "s", "onset": "o", "ends": "e", "affectedZones": []}},
    ]}
    g = A.alerts_gdf(fc)
    assert len(g) == 2 and g.attrs["n_unresolved"] == 1
    z = g.set_index("alert_id").loc["a1"]
    assert z.geometry.geom_type in ("Polygon", "MultiPolygon") and z["geometry_source"] == "zones:1/1"
    assert z.geometry.contains(Point(-76.25, 37.0))


def test_points_in_alerts_buffer_and_row_preservation():
    alerts = gpd.GeoDataFrame({"event": ["Coastal Flood Watch", "Flood Warning"], "severity": ["Moderate", "Severe"], "areaDesc": ["x", "y"]},
                              geometry=[box(-76.5, 36.8, -76.0, 37.2), box(-76.4, 36.9, -76.3, 37.0)], crs="EPSG:4326")
    pts = gpd.GeoDataFrame({"name": ["inside_both", "inside_one", "just_outside_300m", "far"]},
                           geometry=[Point(-76.35, 36.95), Point(-76.1, 37.1), Point(-75.9967, 37.0), Point(-77.0, 38.9)], crs="EPSG:4326")
    out = E.points_in_alerts(pts, alerts, buffer_m=500)
    assert list(out["name"]) == list(pts["name"])
    assert out["in_alert"].tolist() == [True, True, True, False]
    assert out.loc[0, "alert_events"] == "Coastal Flood Watch;Flood Warning" and out.loc[0, "max_severity"] == "Severe"
    assert out.loc[1, "alert_events"] == "Coastal Flood Watch"
    out0 = E.points_in_alerts(pts, alerts, buffer_m=0)
    assert out0["in_alert"].tolist() == [True, True, False, False]


def test_points_in_alerts_handles_empty_alerts():
    pts = gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    empty = gpd.GeoDataFrame(columns=["event", "severity", "geometry"], geometry="geometry", crs="EPSG:4326")
    out = E.points_in_alerts(pts, empty)
    assert len(out) == 1 and not out["in_alert"].iloc[0]


def test_conus_filter_drops_guam():
    g = gpd.GeoDataFrame({"event": ["a", "b"]}, geometry=[box(144.6, 13.2, 144.9, 13.6), box(-76.5, 36.8, -76.0, 37.2)], crs="EPSG:4326")
    out = E.conus_only(g)
    assert len(out) == 1 and out.attrs["n_outside_conus"] == 1


def test_satellite_status_next_pass_math(monkeypatch):
    now = pd.Timestamp("2026-10-17T20:00:00Z")
    items = [{"properties": {"sat:relative_orbit": 19, "sat:orbit_state": "ascending", "platform": "sentinel-1d", "datetime": "2026-10-10T23:45:00Z"},
              "geometry": {"type": "Polygon", "coordinates": [[[-88, 29], [-83, 29], [-83, 33], [-88, 33], [-88, 29]]]}},
             {"properties": {"sat:relative_orbit": 121, "sat:orbit_state": "ascending", "platform": "sentinel-1d", "datetime": "2026-10-05T23:37:00Z"},
              "geometry": {"type": "Polygon", "coordinates": [[[-88, 29], [-83, 29], [-83, 33], [-88, 33], [-88, 29]]]}}]
    monkeypatch.setattr(E, "mpc_scenes", lambda *a, **k: items)
    s = E.satellite_status({"x": [-88.6, 29.6, -83.5, 32.2]}, now=now)
    orb = {o["relative_orbit"]: o for o in s["areas"]["x"]["orbits"]}
    assert pd.Timestamp(orb[19]["next_expected_utc"]) == pd.Timestamp("2026-10-22T23:45:00Z")
    # orbit 121's +12 d (Oct 17 23:37) is still in the future at 20:00 -> expected tonight
    assert pd.Timestamp(orb[121]["next_expected_utc"]) == pd.Timestamp("2026-10-17T23:37:00Z")
    assert s["areas"]["x"]["next_expected_utc"].startswith("2026-10-17T23:37")
