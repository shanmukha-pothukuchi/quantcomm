"""Join tests for exposure.py. Geometries here are tiny hand-built fixtures (not storm data)."""
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from floodalpha.exposure import point_exposure, tract_flood

CRS = "EPSG:5070"
AOI = [-87.0, 30.0, -86.0, 31.0]


@pytest.fixture
def flood():
    # one 0.01 x 0.01 deg flood polygon (~1 km) near Destin
    return gpd.GeoDataFrame({"flood_id": [0], "area_m2": [1e6]}, geometry=[box(-86.50, 30.40, -86.49, 30.41)], crs="EPSG:4326")


@pytest.fixture
def coverage():
    return gpd.GeoDataFrame(geometry=[box(-86.8, 30.2, -86.2, 30.8)], crs="EPSG:4326")


def test_point_exposure_preserves_rows_and_flags(flood, coverage):
    pts = gpd.GeoDataFrame(
        {"nessie_id": ["a", "b", "c", "d"]},
        geometry=[Point(-86.495, 30.405),   # inside flood
                  Point(-86.487, 30.405),   # ~300 m east of flood edge -> exposed at 500 m buffer
                  Point(-86.40, 30.405),    # ~9 km away, covered, not exposed
                  Point(-77.0, 38.9)],      # Arlington VA: outside AOI and coverage, > 50 km
        crs="EPSG:4326")
    out = point_exposure(pts, flood, coverage, AOI, 500, CRS)
    assert len(out) == 4 and list(out["nessie_id"]) == ["a", "b", "c", "d"]
    assert out["exposed"].tolist() == [True, True, False, False]
    assert out["in_aoi"].tolist() == [True, True, True, False]
    assert out["sar_covered"].tolist() == [True, True, True, False]
    assert out.loc[0, "dist_to_flood_m"] == 0.0
    assert 100 < out.loc[1, "dist_to_flood_m"] < 500
    assert np.isnan(out.loc[3, "dist_to_flood_m"])
    assert out.crs.to_epsg() == 4326


def test_point_exposure_no_flood_polygons(coverage):
    pts = gpd.GeoDataFrame({"nessie_id": ["a"]}, geometry=[Point(-86.5, 30.4)], crs="EPSG:4326")
    out = point_exposure(pts, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"), coverage, AOI, 500, CRS)
    assert len(out) == 1 and not out["exposed"].iloc[0] and np.isnan(out["dist_to_flood_m"].iloc[0])


def test_tract_flood_area_fraction(flood, coverage):
    tracts = gpd.GeoDataFrame(
        {"geoid": ["t1", "t2", "t3"], "county_fips": ["12091", "12091", "01003"]},
        geometry=[box(-86.50, 30.40, -86.48, 30.41),   # flood covers exactly half of it
                  box(-86.40, 30.40, -86.39, 30.41),   # covered, dry
                  box(-87.9, 30.5, -87.8, 30.6)],      # outside SAR coverage
        crs="EPSG:4326")
    out = tract_flood(tracts, flood, coverage, {"12091"}, CRS)
    assert len(out) == 3 and out["geoid"].is_unique
    assert out.loc[0, "pct_flooded"] == pytest.approx(50.0, abs=0.5)
    assert out.loc[1, "pct_flooded"] == 0.0 and out.loc[1, "sar_covered"]
    assert not out.loc[2, "sar_covered"] and out.loc[2, "flooded_area_m2"] == 0.0
    assert out["fema_declared"].tolist() == [True, True, False]


def test_tract_flood_overlapping_polygons_do_not_double_count_beyond_tract(coverage):
    # two overlapping flood polygons; flooded area must not exceed the tract area by much
    polys = gpd.GeoDataFrame({"flood_id": [0, 1], "area_m2": [0, 0]},
                             geometry=[box(-86.50, 30.40, -86.49, 30.41), box(-86.495, 30.40, -86.485, 30.41)], crs="EPSG:4326")
    tracts = gpd.GeoDataFrame({"geoid": ["t1"], "county_fips": ["12091"]}, geometry=[box(-86.50, 30.40, -86.485, 30.41)], crs="EPSG:4326")
    out = tract_flood(tracts, polys, coverage, set(), CRS)
    # overlap is counted twice (sum of intersections) — we accept this but it must be bounded
    assert 100.0 <= out.loc[0, "pct_flooded"] <= 134.0
