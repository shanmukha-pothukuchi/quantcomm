"""Validation module tests. Toy fixtures only, except one integration check against the Michael run if present."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from floodalpha import config
from floodalpha.validation import nfip


def test_claims_by_tract_uses_first_11_digits_of_block_group_geoid():
    claims = pd.DataFrame({
        "censusGeoid": ["120459603003", "120459603001", "120050006001", None, "bad"],
        "paid": [100.0, 50.0, 10.0, 5.0, 1.0],
    })
    t = nfip.claims_by_tract(claims)
    assert set(t["geoid"]) == {"12045960300", "12005000600"}
    row = t.set_index("geoid").loc["12045960300"]
    assert row["n_claims"] == 2 and row["paid_total"] == 150.0
    assert t["n_claims"].sum() == 3  # the None and the malformed id are dropped (reported upstream)


def test_confusion_and_metrics_arithmetic():
    df = pd.DataFrame({
        "sar_covered": [True] * 6 + [False],
        "pct_flooded": [5.0, 2.0, 0.0, 0.0, 1.5, 0.2, 9.0],
        "n_claims": [10, 0, 7, 0, 6, 1, 50],
        "claims_per_1k_hu": [10.0, 0.0, 7.0, 0.0, 6.0, 1.0, 50.0],
        "fema_declared": [True, True, True, False, False, False, True],
    })
    m = nfip.metrics(df, flood_pct=1.0, claims_min=5)
    assert m["n_tracts"] == 6  # uncovered tract excluded
    c = m["confusion"]
    assert (c["tp"], c["fp"], c["fn"], c["tn"]) == (2, 1, 1, 2)
    assert c["precision"] == pytest.approx(2 / 3) and c["recall"] == pytest.approx(2 / 3)
    b = m["baseline_fema_declared"]
    assert (b["tp"], b["fp"], b["fn"], b["tn"]) == (2, 1, 1, 2)
    assert 0 <= m["auc_pct_flooded"] <= 1


def test_tract_pct_from_mask_zonal_stats():
    import geopandas as gpd
    from rasterio.transform import from_origin
    from shapely.geometry import box

    transform = from_origin(0, 100, 10, 10)  # 10 m pixels, 10x10 grid covering x 0..100, y 0..100
    profile = {"crs": "EPSG:32616", "transform": transform, "height": 10, "width": 10}
    mask = np.zeros((10, 10), dtype="uint8")
    mask[0:5, 0:5] = 1          # top-left quarter flooded
    mask[:, 8:] = nfip.NODATA   # right 20% not covered
    tracts = gpd.GeoDataFrame({"geoid": ["a", "b", "c"]},
                              geometry=[box(0, 50, 50, 100), box(50, 0, 80, 50), box(80, 0, 100, 100)], crs="EPSG:32616")
    out = nfip.tract_pct_from_mask(mask, profile, tracts).set_index("geoid")
    assert out.loc["a", "pct_flooded"] == pytest.approx(100.0)
    assert out.loc["b", "pct_flooded"] == 0.0 and out.loc["b", "sar_covered"]
    assert not out.loc["c", "sar_covered"] and out.loc["c", "flooded_area_m2"] == 0.0


@pytest.mark.skipif(not list((config.storm_dir("michael", "raw") / "s1" / "frames").glob("eligible_*.tif")), reason="michael run not available")
def test_rethreshold_matches_pipeline_mask_for_michael():
    """Re-thresholding the saved mosaics at the default thresholds must reproduce the pipeline's flood extent
    (small differences allowed: the pipeline cluster-filters per frame before mosaicking)."""
    import rasterio

    meta = json.loads((config.storm_dir("michael") / "flood_meta.json").read_text())
    t = meta["thresholds"]
    mask, prof = nfip.rethreshold("michael", t["diff_db_threshold"], t["post_db_max"], t["min_cluster_px"])
    with rasterio.open(config.storm_dir("michael") / "flood_mask.tif") as ds:
        ref = ds.read(1)
    assert mask.shape == ref.shape
    a, b = (mask == 1), (ref == 1)
    inter, union = (a & b).sum(), (a | b).sum()
    assert union > 0
    assert inter / union > 0.8, f"IoU {inter / union:.2f} between re-threshold and pipeline mask"
    # the re-threshold must (almost) never flag pixels the pipeline deemed ineligible/nodata; a handful of
    # edge pixels differ because rasterio.merge snaps to the first frame's grid while reproject uses the mosaic grid
    assert (a & (ref == nfip.NODATA)).sum() < 0.02 * a.sum()
