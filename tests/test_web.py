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


@pytest.fixture()
def full_dir(empty_dir):
    _write_storm(empty_dir)
    _write_live(empty_dir)
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


def _payload(signals: dict) -> dict:
    """Minimal stock payload: the research endpoint reads only summary, signals and events."""
    summary = {t: {"name": f"{t} Corp.", "group": "Large banks"} for t in ("COF", "JPM", "WMT", "KRE")}
    return {"as_of": "2026-10-16", "summary": summary, "signals": signals,
            "events": [{"key": STORM, "name": "Isaias", "landfall": "2026-10-10T01:30:00"}]}


HIST = [{"storm": "Ian", "year": 2022, "excess_pct": -1.5}]
SIGNALS = {
    "COF": {"type": "branch_network", "storm_history": HIST},
    "JPM": {"type": "flood_deposits", "storm_history": HIST},
    "WMT": {"type": "parking_lot_sar", "series": {"dates": ["2026-01-01"], "values": [0.1], "signal_dates": [], "signal_values": []},
            "metrics": [], "verdict": {"stance": "no edge", "reason": "r"}, "source": "S1", "caveats": ["c"]},
    "KRE": {"type": "benchmark"},
}


@pytest.fixture()
def research_client(monkeypatch):
    monkeypatch.setattr(stocks, "payload", lambda: _payload(SIGNALS))
    return TestClient(server.create_app())


# ------------------------------------------------------------ research panels --
def test_every_research_panel_has_a_builder_and_a_title():
    from quantcomm.agent.evidence import RESEARCH_PANELS
    from quantcomm.web import research
    assert set(research.BUILDERS) == set(RESEARCH_PANELS) == set(research.TITLES)


def test_missing_outputs_are_reported_per_panel_not_errors(empty_dir, research_client):
    d = _data(research_client, "/api/research/COF")
    assert [o["key"] for o in d["options"]] == ["branch_map", "customer_stress", "flood_map", "storm_history"]
    p = d["panels"]
    assert p["branch_map"]["nessie"]["available"] is False and p["branch_map"]["alerts"]["hint"] == "scripts/run_live.py"
    assert p["customer_stress"]["stress"]["available"] is False
    assert p["flood_map"]["flood"]["hint"].startswith("scripts/run_storm.py")
    assert _data(research_client, "/api/live/alerts") is None
    assert _data(research_client, f"/api/storms/{STORM}/layers/polygons") is None


def test_capital_one_research_carries_branches_stress_and_flood_map(full_dir, research_client):
    p = _data(research_client, "/api/research/COF")["panels"]
    assert p["branch_map"]["nessie"]["n"] == 1 and p["branch_map"]["alerts"]["n_alerts_conus"] == 2
    groups = {g["group_label"]: g for g in p["customer_stress"]["stress"]["groups"]}
    assert groups["Flooded tract"]["n"] == 1 and groups["Not flooded"]["loss_rate_pct"] == pytest.approx(100 * 9 / 900)
    assert p["customer_stress"]["stress"]["synthetic"] is True and p["customer_stress"]["live"]["loan_in_alert"] == 1000.0
    f = p["flood_map"]["flood"]
    assert f["flooded_km2"] == 3.4 and f["event_time_utc"] == EVENT and f["previews"] == ["preview_change.png"]
    n = p["flood_map"]["nessie"]
    assert (n["n"], n["n_exposed"]) == (3, 1) and n["locations"][0]["name"] == "C"  # exposed first


def test_bank_research_ranks_banks_under_alerts(full_dir, research_client):
    d = _data(research_client, "/api/research/JPM?panels=alert_exposure,storm_history")
    assert list(d["panels"]) == ["alert_exposure", "storm_history"]
    banks = d["panels"]["alert_exposure"]["banks"]
    assert [r["bank_name"] for r in banks["rows"]] == ["Big", "Small"]
    assert banks["ticker_rows"] == []  # fixture rows carry no cert/rssdhcr, so nothing is tagged
    assert d["panels"]["storm_history"]["rows"] == HIST and d["panels"]["storm_history"]["bench"] == "KRE"
    assert _data(research_client, "/api/research/JPM?panels=flood_map")["panels"]["flood_map"]["nessie"] is None


def test_retail_research_is_the_parking_lot_panel(empty_dir, research_client):
    d = _data(research_client, "/api/research/WMT")
    assert [o["key"] for o in d["options"]] == ["parking_lot"]
    assert d["panels"]["parking_lot"]["series"]["values"] == [0.1] and d["panels"]["parking_lot"]["stores"] == []


def test_benchmark_has_no_research(empty_dir, research_client):
    d = _data(research_client, "/api/research/KRE")
    assert d["options"] == [] and d["panels"] == {}


@pytest.mark.parametrize("url", ["/api/research/NOPE", "/api/research/WMT?panels=branch_map"])
def test_unknown_ticker_or_panel_is_a_404(empty_dir, research_client, url):
    r = research_client.get(url)
    assert r.status_code == 404 and r.json()["success"] is False


def test_polygon_layer_is_json_safe_geojson(full_dir, client):
    gj = _data(client, f"/api/storms/{STORM}/layers/polygons")
    assert len(gj["features"]) == 2
    assert set(gj["features"][0]["properties"]) == {"pass_id", "area_m2"}  # timestamps dropped, nothing unserializable


def test_live_alert_polygons_keep_only_display_fields(full_dir, client):
    alerts = _data(client, "/api/live/alerts")
    assert len(alerts["features"]) == 2
    assert set(alerts["features"][0]["properties"]) == {"event", "onset"}
    assert isinstance(alerts["features"][0]["properties"]["onset"], str)


# ------------------------------------------------------------------ routing --
@pytest.mark.parametrize("url", ["/api/storms/atlantis/layers/polygons", f"/api/storms/{STORM}/layers/secrets"])
def test_unknown_keys_are_404_envelopes(empty_dir, client, url):
    r = client.get(url)
    assert r.status_code == 404
    assert r.json()["success"] is False and r.json()["error"]


@pytest.mark.parametrize("url", ["/dashboard", "/about", "/api/banks", "/api/market", "/api/validation", "/api/storms"])
def test_dashboard_routes_are_gone(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("url", [f"/media/{STORM}/flood_meta.json", f"/media/{STORM}/..%2F..%2F.env", "/media/atlantis/preview_change.png"])
def test_media_only_serves_whitelisted_previews(full_dir, client, url):
    assert client.get(url).status_code == 404


def test_media_serves_preview_png(full_dir, client):
    r = client.get(f"/media/{STORM}/preview_change.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_landing_is_home_and_links_to_the_markets_app(client):
    for url in ("/", "/stocks"):
        r = client.get(url)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    landing = (ROOT / "web" / "index.html").read_text()
    assert 'href="/stocks"' in landing and "/dashboard" not in landing
    assert "localhost" not in landing
    markets_page = (ROOT / "web" / "markets.html").read_text()
    assert 'class="brand" href="/"' in markets_page and "/dashboard" not in markets_page


BRAND_PAGES = ("markets.html", "index.html")


def test_pages_use_the_quantcomm_logo_and_favicon():
    for name in BRAND_PAGES:
        html = (ROOT / "web" / name).read_text()
        assert 'rel="icon"' in html and 'rel="apple-touch-icon"' in html, name
        assert "/assets/brand/mark.png" in html, name


def test_favicon_is_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"  # ICO header


def test_brand_images_are_served(client):
    for name in ("mark.png", "favicon-32.png", "apple-touch-icon.png", "quantcomm-logo.png"):
        r = client.get(f"/assets/brand/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", name


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
