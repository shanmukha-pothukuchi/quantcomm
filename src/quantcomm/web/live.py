"""Live-mode payloads: active NWS flood alerts, NHC storms, pre-event exposure and satellite status."""
from __future__ import annotations

from quantcomm.web import store

ALERT_SIMPLIFY_DEG = 0.002  # ~200 m; alert polygons are county/zone outlines, the browser doesn't need more
ALERT_PROPS = ("event", "severity", "areaDesc", "headline", "onset", "ends")
TOP_BANKS = 25


def _live_dir():
    return store.data_dir() / "live"


def _banks() -> dict:
    path = _live_dir() / "bank_alert_exposure.parquet"
    be = store.load_parquet(path)
    if be is None:
        return store.missing("Bank alert exposure", path, "scripts/run_live.py")
    be = be.sort_values("deposits_in_alert_k", ascending=False)
    return {"available": True, "source": store.rel(path), "n": len(be),
            "rows": store.records(be.head(TOP_BANKS), ["bank_name", "holding_company", "n_branches_in_alert",
                                                       "deposits_in_alert_k", "exposure_pct", "alert_events",
                                                       "total_deposits_k"])}


def _nessie() -> dict:
    path = _live_dir() / "nessie_alert_exposure.parquet"
    ne = store.load_parquet(path)
    if ne is None:
        return store.missing("Capital One alert exposure", path, "scripts/run_live.py")
    return {"available": True, "source": store.rel(path), "n": len(ne), "n_in_alert": int(ne["in_alert"].sum()),
            "locations": store.records(ne, ["kind", "name", "city", "state", "lat", "lon", "in_alert", "alert_events"])}


def _portfolio() -> dict:
    path = _live_dir() / "SYNTHETIC_portfolio_alert_exposure.parquet"
    pa = store.load_parquet(path)
    if pa is None:
        return store.missing("SYNTHETIC portfolio alert exposure", path, "scripts/run_live.py")
    return {"available": True, "synthetic": True, "source": store.rel(path),
            "n": len(pa), "n_in_alert": int(pa["in_alert"].sum()),
            "loan_in_alert": store.clean(float(pa.loc[pa["in_alert"], "loan_amount"].sum()))}


def _satellite() -> dict:
    path = _live_dir() / "satellite_status.json"
    sat = store.load_json(path)
    if sat is None:
        return store.missing("Satellite status", path, "scripts/run_live.py")
    rows = []
    for area, info in (sat.get("areas") or {}).items():
        for o in info.get("orbits") or []:
            rows.append({"area": area, "orbit": o.get("relative_orbit"), "direction": o.get("pass_direction"),
                         "platform": o.get("platform"), "last_pass_utc": o.get("last_pass_utc"),
                         "next_expected_utc": o.get("next_expected_utc"),
                         "coverage_pct": store.clean(o.get("coverage_pct")), "scenes_30d": o.get("n_scenes_30d")})
    return {"available": True, "source": store.rel(path), "checked_utc": sat.get("checked_utc"), "orbits": rows}


def live_summary() -> dict:
    d = _live_dir()
    live = store.load_json(d / "live_summary.json")
    nhc = store.load_json(d / "nhc.json")
    summary = (store.missing("Live summary", d / "live_summary.json", "scripts/run_live.py") if live is None else
               {"available": True, "source": store.rel(d / "live_summary.json"),
                "generated_utc": live.get("generated_utc"), "alerts_fetched_utc": live.get("alerts_fetched_utc"),
                "n_alerts_conus": live.get("n_alerts_conus"), "alerts_by_event": live.get("alerts_by_event") or {},
                "banks": {k: v for k, v in (live.get("banks") or {}).items() if k != "top10"},
                "isaias_refresh": live.get("isaias_refresh") or {}, "buffer_m": live.get("buffer_m"),
                "sod_vintage": live.get("sod_vintage")})
    return {
        "summary": summary,
        "nhc": (store.missing("NHC status", d / "nhc.json", "scripts/run_live.py") if nhc is None else
                {"available": True, "source": store.rel(d / "nhc.json"), "fetched_utc": nhc.get("fetched_utc"),
                 "active_storms": nhc.get("active_storms") or [], "atlantic": nhc.get("atlantic") or []}),
        "banks": _banks(), "nessie": _nessie(), "portfolio": _portfolio(), "satellite": _satellite(),
    }


def live_alerts() -> dict | None:
    return store.load_geojson(_live_dir() / "alerts.geojson", ALERT_SIMPLIFY_DEG, ALERT_PROPS)
