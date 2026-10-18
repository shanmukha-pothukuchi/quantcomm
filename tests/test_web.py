"""Web app API: payloads built from pipeline outputs, honest "not generated" states, safe routing.

Fixture outputs are written to a temp FLOODALPHA_DATA_DIR; they exist only to exercise the joins
and serializers and never touch data/processed/.
"""
from __future__ import annotations

import json

import geopandas as gpd
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point, box

from quantcomm.config import ROOT
from quantcomm.web import server, stocks, store

STORM = "isaias"
EVENT = "2026-10-11T05:38:28+00:00"


@pytest.fixture()
def empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOODALPHA_DATA_DIR", str(tmp_path))
    return tmp_path


def _write_storm(d):
    s = d / STORM
    s.mkdir(parents=True)
    (s / "flood_meta.json").write_text(json.dumps({
        "landfall_time_utc": "2026-10-10T01:30:00Z", "event_time_utc": EVENT, "hours_landfall_to_event": 28.1,
        "flooded_km2": 3.4, "coverage_pct_of_aoi": 52.0, "n_passes": 1, "relative_orbits_used": [19],
        "aoi_bbox": [-88.6, 29.6, -83.5, 32.2], "preview_bounds": [[29.5, -87.7], [32.2, -84.0]],
        "thresholds": {"diff_db_threshold": -3.0, "post_db_max": -15.0, "resolution_m": 40},
        "passes": [{"pass_id": "p1", "relative_orbit": 19, "pass_direction": "ascending",
                    "post_acquired_utc": "2026-10-10T23:45:00Z", "published_utc": EVENT, "post_scene_ids": ["a", "b"],
                    "pre_passes": {"2026-09-28": 1}, "coverage_pct_of_aoi": 52.0, "flooded_km2": 3.4, "n_polygons": 2}],
        "frame_stats": [{"threshold_sensitivity_km2_before_cluster_filter": {"km2_diff_lt_-3.0": 40.0, "km2_diff_lt_-3.0_and_post_lt_-15.0": 9.0}},
                        {"threshold_sensitivity_km2_before_cluster_filter": {"km2_diff_lt_-3.0": 5.6, "km2_diff_lt_-3.0_and_post_lt_-15.0": 0.6}}],
    }))
    (s / "preview_change.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    pd.DataFrame({"kind": ["branch", "atm", "branch"], "name": ["A", "B", "C"], "city": ["x"] * 3, "state": ["VA"] * 3,
                  "lat": [38.9, 38.8, 39.0], "lon": [-77.1, -77.0, -76.9], "in_aoi": [False, False, True],
                  "sar_covered": [False, False, True], "dist_to_flood_m": [9e5, 8e5, 120.0], "exposed": [False, False, True]}
                 ).to_parquet(s / "nessie_exposure.parquet")
    (s / "nws_sanity.json").write_text(json.dumps({
        "n_warnings": 2, "warnings_available": True, "pct_flood_area_inside_warnings": 1.5, "pct_warning_area_flooded": 0.01,
        "verdict": "plausible: something", "flooded_km2_by_county": {"Bay (FL)": 0.2, "Gulf (FL)": 0.4},
        "fema_declared_counties": ["Gulf (FL)"], "warnings_geojson": {"type": "FeatureCollection", "features": []}}))
    pd.DataFrame({"customer_key": range(4), "is_flooded": [True, False, False, False], "ead": [100.0, 200.0, 300.0, 400.0],
                  "expected_loss": [10.0, 2.0, 3.0, 4.0], "pd_12m": [0.075, 0.025, 0.025, 0.025],
                  "withdrawal_shock": [25.0, 0, 0, 0], "missed_bill": [1, 0, 0, 0], "group": ["affected", "affected", "control", "control"]}
                 ).to_parquet(s / "SYNTHETIC_stress_results.parquet")
    (s / "stress_summary.json").write_text(json.dumps({"incremental_expected_loss": 6.67, "baseline_expected_loss_total": 12.33}))
    pd.DataFrame({"scene_id": ["S1"], "acquired_utc": pd.to_datetime([EVENT]), "published_utc": pd.to_datetime([EVENT]),
                  "relative_orbit": [19], "geometry_json": ["{}"], "vv_href": ["http://x"], "is_post_landfall": [True]}
                 ).to_parquet(s / "s1_catalog.parquet")
    gpd.GeoDataFrame({"pass_id": ["p1", "p1"], "area_m2": [5000.0, 7000.0],
                      "published_utc": pd.to_datetime([EVENT, EVENT])},
                     geometry=[box(-86.0, 30.4, -85.99, 30.41), box(-85.5, 30.5, -85.49, 30.51)], crs=4326
                     ).to_parquet(s / "flood_polygons.parquet")


def _write_live(d):
    live = d / "live"
    live.mkdir(parents=True)
    (live / "live_summary.json").write_text(json.dumps({
        "generated_utc": EVENT, "alerts_fetched_utc": EVENT, "n_alerts_conus": 2, "alerts_by_event": {"Flood Warning": 2},
        "banks": {"n_banks_with_branches_in_alert": 2, "deposits_in_alert_k_total": 3e6, "top10": [{"x": 1}]}}))
    gpd.GeoDataFrame({"event": ["Flood Warning", "Flood Warning"], "onset": pd.to_datetime([EVENT, EVENT]), "junk": [1, 2]},
                     geometry=[box(-77, 36, -76, 37), box(-76, 37, -75, 38)], crs=4326).to_file(live / "alerts.geojson", driver="GeoJSON")
    pd.DataFrame({"bank_name": ["Small", "Big"], "holding_company": ["S", "B"], "n_branches_in_alert": [1, 9],
                  "deposits_in_alert_k": [1e5, 2.9e6], "exposure_pct": [1.0, 40.0], "alert_events": ["Flood Warning"] * 2,
                  "total_deposits_k": [1e7, 7.25e6]}).to_parquet(live / "bank_alert_exposure.parquet")
    pd.DataFrame({"kind": ["branch"], "name": ["A"], "city": ["x"], "state": ["VA"], "lat": [38.9], "lon": [-77.1],
                  "in_alert": [False], "alert_events": [None]}).to_parquet(live / "nessie_alert_exposure.parquet")
    pd.DataFrame({"in_alert": [True, False], "loan_amount": [1000.0, 5.0]}).to_parquet(live / "SYNTHETIC_portfolio_alert_exposure.parquet")


def _write_research(d):
    pd.DataFrame({"storm": [STORM, STORM, STORM], "definition": ["full", "full", "strict"], "bank_name": ["A", "B", "A"],
                  "ticker": ["AAA", "", "AAA"], "total_deposits_k": [2e6, 5e5, 2e6], "flooded_deposits_k": [1e4, 0.0, 0.0],
                  "exposure_pct": [0.5, 0.0, 0.0], "aoi_deposits_pct": [3.0, 1.0, 3.0], "n_branches_in_aoi": [2, 1, 2]}
                 ).to_parquet(d / "bank_exposure.parquet")
    (d / "cross_section.json").write_text(json.dumps({"caveats": ["small sample"], "by_definition": {
        "full": {"exposure_pct": {"car_0_1": {"coef_per_pct": -0.7, "t": -7.8, "p": 0.0, "n": 10, "n_storms": 2, "n_nonzero_exposure": 1}}}}}))
    pd.DataFrame({"storm": [STORM], "definition": ["full"], "ticker": ["AAA"], "bank_name": ["A"], "exposure_pct": [0.5],
                  "car_0_1": [-0.01], "car_0_5": [float("nan")]}).to_parquet(d / "event_study.parquet")
    v = d / "validation"
    v.mkdir()
    (v / "validation_summary.json").write_text(json.dumps({"pooled": {"n_tracts": 10, "auc_pct_flooded": 0.54}}))


@pytest.fixture()
def full_dir(empty_dir):
    _write_storm(empty_dir)
    _write_live(empty_dir)
    _write_research(empty_dir)
    return empty_dir


@pytest.fixture()
def client():
    return TestClient(server.create_app())


def _data(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


# ------------------------------------------------------------ empty outputs --
@pytest.mark.parametrize("url", ["/api/banks", "/api/market", "/api/validation"])
def test_missing_research_outputs_say_which_script(empty_dir, client, url):
    d = _data(client, url)
    assert d["available"] is False
    assert d["hint"].startswith("scripts/run_")


def test_missing_storm_and_live_outputs_are_not_errors(empty_dir, client):
    storm = _data(client, f"/api/storms/{STORM}")
    for part in ("flood", "nws", "nessie", "stress", "catalog"):
        assert storm[part]["available"] is False, part
    live = _data(client, "/api/live")
    for part in ("summary", "nhc", "banks", "nessie", "portfolio", "satellite"):
        assert live[part]["available"] is False, part
    assert _data(client, "/api/live/alerts") is None
    assert _data(client, f"/api/storms/{STORM}/layers/polygons") is None
    assert all(not s["done"] for s in _data(client, "/api/storms"))


# ------------------------------------------------------------- full outputs --
def test_storm_case_payload_carries_pipeline_values(full_dir, client):
    d = _data(client, f"/api/storms/{STORM}")
    f = d["flood"]
    assert f["flooded_km2"] == 3.4 and f["event_time_utc"] == EVENT and f["previews"] == ["preview_change.png"]
    assert f["passes"][0]["frames"] == 2 and f["passes"][0]["hours_after_landfall"] == pytest.approx(22.2, abs=0.1)
    sens = {r["threshold_db"]: r for r in f["sensitivity"]}
    assert sens[-3.0]["km2_darker"] == 45.6 and sens[-3.0]["used"] is True  # summed over frames
    assert d["nws"]["counties"][0] == {"county": "Gulf (FL)", "flooded_km2": 0.4, "fema_declared": True}
    n = d["nessie"]
    assert (n["n"], n["n_in_aoi"], n["n_exposed"]) == (3, 1, 1)
    assert n["locations"][0]["name"] == "C"  # exposed first
    groups = {g["group_label"]: g for g in d["stress"]["groups"]}
    assert groups["Flooded tract"]["n"] == 1 and groups["Not flooded"]["n"] == 3
    assert groups["Not flooded"]["loss_rate_pct"] == pytest.approx(100 * 9 / 900)
    assert d["stress"]["synthetic"] is True and d["stress"]["incremental_expected_loss"] == 6.67
    assert "geometry_json" not in d["catalog"]["rows"][0] and "vv_href" not in d["catalog"]["rows"][0]
    assert _data(client, "/api/storms")[next(i for i, s in enumerate(store.storms_config()) if s == STORM)]["done"] is True


def test_polygon_layer_is_json_safe_geojson(full_dir, client):
    gj = _data(client, f"/api/storms/{STORM}/layers/polygons")
    assert len(gj["features"]) == 2
    assert set(gj["features"][0]["properties"]) == {"pass_id", "area_m2"}  # timestamps dropped, nothing unserializable


def test_live_payload_sorts_banks_and_strips_bulky_fields(full_dir, client):
    d = _data(client, "/api/live")
    assert [r["bank_name"] for r in d["banks"]["rows"]] == ["Big", "Small"]
    assert "top10" not in d["summary"]["banks"]
    assert d["portfolio"]["synthetic"] is True and d["portfolio"]["loan_in_alert"] == 1000.0
    alerts = _data(client, "/api/live/alerts")
    assert len(alerts["features"]) == 2
    assert set(alerts["features"][0]["properties"]) == {"event", "onset"}
    assert isinstance(alerts["features"][0]["properties"]["onset"], str)


def test_bank_exposure_filters_storm_and_definition(full_dir, client):
    d = _data(client, f"/api/banks?storm={STORM}&definition=strict")
    assert d["definition"] == "strict" and len(d["rows"]) == 1
    d = _data(client, "/api/banks?storm=nope&definition=nope")  # unknown values fall back, never 500
    assert d["storm"] == STORM and d["definition"] == "full" and len(d["rows"]) == 2


def test_market_and_validation_payloads(full_dir, client):
    m = _data(client, "/api/market")
    assert m["caveats"] == ["small sample"]
    assert m["cross_section"][0]["coef_per_pct"] == -0.7 and m["cross_section"][0]["n_nonzero"] == 1
    assert m["event_study"][0]["car_0_5"] is None  # NaN -> null
    assert _data(client, "/api/validation")["summary"]["pooled"]["auc_pct_flooded"] == 0.54


# ------------------------------------------------------------------ routing --
@pytest.mark.parametrize("url", ["/api/storms/atlantis", "/api/storms/atlantis/layers/polygons", f"/api/storms/{STORM}/layers/secrets"])
def test_unknown_keys_are_404_envelopes(empty_dir, client, url):
    r = client.get(url)
    assert r.status_code == 404
    assert r.json()["success"] is False and r.json()["error"]


@pytest.mark.parametrize("url", [f"/media/{STORM}/flood_meta.json", f"/media/{STORM}/..%2F..%2F.env", "/media/atlantis/preview_change.png"])
def test_media_only_serves_whitelisted_previews(full_dir, client, url):
    assert client.get(url).status_code == 404


def test_media_serves_preview_png(full_dir, client):
    r = client.get(f"/media/{STORM}/preview_change.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_pages_are_served_and_linked(client):
    for url in ("/", "/dashboard"):
        r = client.get(url)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    landing = (ROOT / "web" / "index.html").read_text()
    assert 'href="/dashboard"' in landing and 'href="/stocks"' in landing
    assert "localhost" not in landing


def test_markets_api_reports_missing_price_cache(empty_dir, client, monkeypatch, tmp_path):
    monkeypatch.setattr(stocks, "PRICES", tmp_path / "no_prices.parquet")
    r = client.get("/api/markets")
    assert r.status_code == 503 and "Price cache not found" in r.json()["error"]


def test_markets_page_is_the_app_shell():
    html = (ROOT / "web" / "markets.html").read_text()
    assert 'id="app"' in html
    assert "/api/markets" in html and "/api/advice" in html


# ------------------------------------------------------------------- store --
def test_records_and_clean_are_json_safe():
    df = pd.DataFrame({"a": [1.0, float("nan")], "t": pd.to_datetime([EVENT, None])})
    recs = store.records(df)
    assert recs[1]["a"] is None and recs[1]["t"] is None and recs[0]["t"].startswith("2026-10-11T05:38:28")
    assert store.clean(float("inf")) is None and store.clean(pd.Series([3]).iloc[0]) == 3
    json.dumps(recs)


def test_loaders_pick_up_rewritten_files(empty_dir):
    p = empty_dir / "x.json"
    p.write_text('{"v": 1}')
    assert store.load_json(p) == {"v": 1}
    p.write_text('{"v": 22}')  # different size + mtime -> new cache key
    import os
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert store.load_json(p) == {"v": 22}


def test_load_parquet_returns_copies(empty_dir):
    p = empty_dir / "x.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(p)
    first = store.load_parquet(p)
    first.loc[0, "a"] = 99
    assert store.load_parquet(p)["a"].tolist() == [1]


def test_point_layers_keep_points(empty_dir):
    p = empty_dir / "pts.geojson"
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(-80, 30)], crs=4326).to_file(p, driver="GeoJSON")
    assert store.load_geojson(p, 0.001, ("n",))["features"][0]["geometry"]["type"] == "Point"
