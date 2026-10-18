"""Storm payloads for the research panels: Sentinel-1 flood map metadata, Capital One exposure and the SYNTHETIC
stress test for one storm, plus its vector layers (flood polygons, SAR coverage, NHC track)."""
from __future__ import annotations

import pandas as pd

from quantcomm.web import store

PREVIEWS = ("preview_pre.png", "preview_post.png", "preview_change.png", "diagnostic_diff.png")
LAYERS = {  # layer name -> (file candidates, simplify tolerance in degrees, properties kept)
    "polygons": (("flood_polygons.parquet",), 0.0004, ("pass_id", "area_m2")),
    "coverage": (("coverage_passes.geojson", "coverage.geojson"), 0.0, ("pass_id", "published_utc")),
    "track": (("nhc_track.geojson",), 0.0, ()),
}


def _hours(a, b) -> float | None:
    try:
        return round((pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def flood_summary(key: str) -> dict:
    cfg = store.storms_config()[key]
    d = store.data_dir() / key
    meta = store.load_json(d / "flood_meta.json")
    if meta is None:
        return store.missing("Flood map metadata", d / "flood_meta.json", f"scripts/run_storm.py --storm {key}")
    event = meta.get("event_time_utc")
    lag = meta.get("hours_landfall_to_event") or _hours(cfg.get("landfall_time_utc"), event)
    return {
        "available": True, "source": store.rel(d / "flood_meta.json"), "storm_key": key,
        "landfall_utc": cfg.get("landfall_time_utc"), "event_time_utc": event,
        "hours_landfall_to_event": store.clean(lag), "flooded_km2": store.clean(meta.get("flooded_km2")),
        "coverage_pct": store.clean(meta.get("coverage_pct_of_aoi")), "n_passes": meta.get("n_passes", 1),
        "orbits": meta.get("relative_orbits_used") or [meta.get("relative_orbit")],
        "mosaic_complete_utc": meta.get("mosaic_complete_utc") or event,
        "resolution_m": (meta.get("thresholds") or {}).get("resolution_m"),
        "diff_db_threshold": (meta.get("thresholds") or {}).get("diff_db_threshold"),
        "aoi_bbox": meta.get("aoi_bbox") or cfg.get("aoi_bbox"),
        "preview_bounds": meta.get("preview_bounds"),
        "previews": [p for p in PREVIEWS if (d / p).exists()],
    }


def nessie_exposure(key: str) -> dict:
    buffer_m = (store.config("model.yaml").get("exposure") or {}).get("buffer_m", 500)
    path = store.data_dir() / key / "nessie_exposure.parquet"
    exp = store.load_parquet(path)
    if exp is None:
        return store.missing("Capital One exposure", path, f"scripts/run_storm.py --storm {key}")
    exp = exp.sort_values(["exposed", "dist_to_flood_m"], ascending=[False, True])
    return {
        "available": True, "source": store.rel(path), "buffer_m": buffer_m, "n": len(exp),
        "n_in_aoi": int(exp["in_aoi"].sum()) if "in_aoi" in exp else 0, "n_exposed": int(exp["exposed"].sum()),
        "locations": store.records(exp, ["kind", "name", "city", "state", "lat", "lon", "in_aoi",
                                         "sar_covered", "dist_to_flood_m", "exposed"]),
    }


def stress_test(key: str) -> dict:
    d = store.data_dir() / key
    res = store.load_parquet(d / "SYNTHETIC_stress_results.parquet")
    port = store.load_parquet(d / "SYNTHETIC_portfolio.parquet")
    if res is None and port is None:
        return store.missing("SYNTHETIC stress test", d / "SYNTHETIC_stress_results.parquet",
                             f"scripts/run_storm.py --storm {key} --steps portfolio,stress")
    out: dict = {"available": True, "synthetic": True, "source": store.rel(d / "SYNTHETIC_stress_results.parquet"),
                 "n_customers": len(port) if port is not None else None,
                 "n_pushed": int(port["nessie_customer_id"].notna().sum()) if port is not None and "nessie_customer_id" in port else 0}
    if res is not None:
        g = (res.assign(group_label=res["is_flooded"].map({True: "Flooded tract", False: "Not flooded"}))
             .groupby("group_label")
             .agg(n=("customer_key", "size"), ead=("ead", "sum"), expected_loss=("expected_loss", "sum"),
                  pd_12m=("pd_12m", "mean"), withdrawal=("withdrawal_shock", "sum"), missed=("missed_bill", "mean"))
             .reset_index())
        g = g.assign(loss_rate_pct=100 * g["expected_loss"] / g["ead"])
        out["groups"] = store.records(g)
    summ = store.load_json(d / "stress_summary.json")
    if summ:
        out["incremental_expected_loss"] = store.clean(summ.get("incremental_expected_loss"))
        out["baseline_expected_loss"] = store.clean(summ.get("baseline_expected_loss_total"))
    return out


def storm_layer(key: str, layer: str) -> dict | None:
    if key not in store.storms_config() or layer not in LAYERS:
        raise KeyError(f"{key}/{layer}")
    files, tol, keep = LAYERS[layer]
    for name in files:
        path = store.data_dir() / key / name
        if path.exists():
            return store.load_geojson(path, tol, keep)
    return None
