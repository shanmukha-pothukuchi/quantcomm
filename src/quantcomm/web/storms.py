"""Storm case-study payloads: flood map metadata, passes, sanity check, Capital One exposure,
SYNTHETIC stress test and the Sentinel-1 catalog for one storm."""
from __future__ import annotations

import pandas as pd

from quantcomm.web import store

PREVIEWS = ("preview_pre.png", "preview_post.png", "preview_change.png", "diagnostic_diff.png")
LAYERS = {  # layer name -> (file candidates, simplify tolerance in degrees, properties kept)
    "polygons": (("flood_polygons.parquet",), 0.0004, ("pass_id", "area_m2")),
    "coverage": (("coverage_passes.geojson", "coverage.geojson"), 0.0, ("pass_id", "published_utc")),
    "track": (("nhc_track.geojson",), 0.0, ()),
}
SENSITIVITY_THRESHOLDS = (-1.0, -1.5, -2.0, -3.0, -4.0)


def _hours(a, b) -> float | None:
    try:
        return round((pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def storm_index() -> list[dict]:
    done = set(store.completed_storms())
    return [{"key": k, "name": s["name"], "year": s["year"], "done": k in done}
            for k, s in store.storms_config().items()]


def _passes(meta: dict) -> list[dict]:
    rows = []
    for p in meta.get("passes") or []:
        rows.append({
            "pass": p.get("pass_id"), "orbit": p.get("relative_orbit"), "direction": p.get("pass_direction"),
            "acquired_utc": p.get("post_acquired_utc"), "published_utc": p.get("published_utc"),
            "hours_after_landfall": _hours(meta.get("landfall_time_utc"), p.get("post_acquired_utc")),
            "frames": len(p.get("post_scene_ids") or []),
            "pre_passes": ", ".join((p.get("pre_passes") or {}).keys()),
            "coverage_pct": store.clean(p.get("coverage_pct_of_aoi")),
            "flooded_km2": store.clean(p.get("flooded_km2")), "polygons": p.get("n_polygons"),
        })
    return rows


def _sensitivity(meta: dict) -> list[dict]:
    tot: dict[str, float] = {}
    for f in meta.get("frame_stats") or []:
        for k, v in (f.get("threshold_sensitivity_km2_before_cluster_filter") or {}).items():
            tot[k] = tot.get(k, 0.0) + float(v)
    thr = meta.get("thresholds") or {}
    post_max = thr.get("post_db_max", -15.0)
    rows = []
    for t in SENSITIVITY_THRESHOLDS:
        a, b = tot.get(f"km2_diff_lt_{t}"), tot.get(f"km2_diff_lt_{t}_and_post_lt_{post_max}")
        if a is None and b is None:
            continue
        rows.append({"threshold_db": t, "km2_darker": round(a or 0, 1), "km2_darker_and_post_dark": round(b or 0, 1),
                     "post_db_max": post_max, "used": t == thr.get("diff_db_threshold")})
    return rows


def _flood(key: str, cfg: dict) -> dict:
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
        "thresholds": meta.get("thresholds") or {},
        "aoi_bbox": meta.get("aoi_bbox") or cfg.get("aoi_bbox"),
        "preview_bounds": meta.get("preview_bounds"),
        "previews": [p for p in PREVIEWS if (d / p).exists()],
        "passes": _passes(meta), "sensitivity": _sensitivity(meta),
    }


def _nws(key: str) -> dict:
    path = store.data_dir() / key / "nws_sanity.json"
    nws = store.load_json(path)
    if nws is None:
        return store.missing("NWS sanity check", path)
    fema = set(nws.get("fema_declared_counties") or [])
    counties = sorted(({"county": c, "flooded_km2": store.clean(v), "fema_declared": c in fema}
                       for c, v in (nws.get("flooded_km2_by_county") or {}).items()),
                      key=lambda r: -(r["flooded_km2"] or 0))
    feats = (nws.get("warnings_geojson") or {}).get("features") or []
    return {
        "available": True, "source": store.rel(path), "n_warnings": nws.get("n_warnings"),
        "warnings_available": nws.get("warnings_available"),
        "pct_flood_area_inside_warnings": store.clean(nws.get("pct_flood_area_inside_warnings")),
        "pct_warning_area_flooded": store.clean(nws.get("pct_warning_area_flooded")),
        "verdict": nws.get("verdict"), "counties": counties,
        "warnings_geojson": {"type": "FeatureCollection", "features": feats} if feats else None,
    }


def _nessie(key: str, buffer_m: int) -> dict:
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


def _stress(key: str, model: dict) -> dict:
    d = store.data_dir() / key
    res = store.load_parquet(d / "SYNTHETIC_stress_results.parquet")
    port = store.load_parquet(d / "SYNTHETIC_portfolio.parquet")
    if res is None and port is None:
        return store.missing("SYNTHETIC stress test", d / "SYNTHETIC_stress_results.parquet",
                             f"scripts/run_storm.py --storm {key} --steps portfolio,stress")
    out: dict = {"available": True, "synthetic": True, "source": store.rel(d / "SYNTHETIC_stress_results.parquet"),
                 "n_customers": len(port) if port is not None else None,
                 "n_pushed": int(port["nessie_customer_id"].notna().sum()) if port is not None and "nessie_customer_id" in port else 0,
                 "nickname_prefix": (model.get("portfolio") or {}).get("nickname_prefix", "FLOODALPHA_"),
                 "params": {**{f"stress.{k}": v for k, v in (model.get("stress") or {}).items()},
                            **{f"portfolio.{k}": v for k, v in (model.get("portfolio") or {}).items()}}}
    if res is not None:
        g = (res.assign(group_label=res["is_flooded"].map({True: "Flooded tract", False: "Not flooded"}))
             .groupby("group_label")
             .agg(n=("customer_key", "size"), ead=("ead", "sum"), expected_loss=("expected_loss", "sum"),
                  pd_12m=("pd_12m", "mean"), withdrawal=("withdrawal_shock", "sum"), missed=("missed_bill", "mean"))
             .reset_index())
        g = g.assign(loss_rate_pct=100 * g["expected_loss"] / g["ead"])
        out["groups"] = store.records(g)
        if "group" in res:
            by = res.groupby(["group", "is_flooded"]).agg(n=("customer_key", "size"), expected_loss=("expected_loss", "sum")).reset_index()
            out["by_design_group"] = store.records(by)
    summ = store.load_json(d / "stress_summary.json")
    if summ:
        out["incremental_expected_loss"] = store.clean(summ.get("incremental_expected_loss"))
        out["baseline_expected_loss"] = store.clean(summ.get("baseline_expected_loss_total"))
    return out


def _catalog(key: str) -> dict:
    path = store.data_dir() / key / "s1_catalog.parquet"
    cat = store.load_parquet(path)
    if cat is None:
        return store.missing("Sentinel-1 catalog", path)
    cat = cat.drop(columns=[c for c in ("geometry_json", "vv_href") if c in cat.columns])
    cat = cat.sort_values("acquired_utc", ascending=False)
    return {"available": True, "source": store.rel(path), "rows": store.records(cat)}


def storm_case(key: str) -> dict:
    cfgs = store.storms_config()
    if key not in cfgs:
        raise KeyError(key)
    cfg, model = cfgs[key], store.config("model.yaml")
    buffer_m = (model.get("exposure") or {}).get("buffer_m", 500)
    return {
        "key": key, "name": cfg["name"], "year": cfg["year"], "notes": cfg.get("notes"),
        "aoi_bbox": cfg.get("aoi_bbox"), "landfall_utc": cfg.get("landfall_time_utc"),
        "flood": _flood(key, cfg), "nws": _nws(key), "nessie": _nessie(key, buffer_m),
        "stress": _stress(key, model), "catalog": _catalog(key),
    }


def storm_layer(key: str, layer: str) -> dict | None:
    if key not in store.storms_config() or layer not in LAYERS:
        raise KeyError(f"{key}/{layer}")
    files, tol, keep = LAYERS[layer]
    for name in files:
        path = store.data_dir() / key / name
        if path.exists():
            return store.load_geojson(path, tol, keep)
    return None
