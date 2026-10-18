"""Research payloads: every-bank exposure by storm, the market test and NFIP validation."""
from __future__ import annotations

import pandas as pd

from quantcomm.config import ROOT
from quantcomm.web import store

BANK_COLS = ["bank_name", "ticker", "ticker_status", "sod_year", "total_deposits_k", "flooded_deposits_k",
             "exposure_pct", "aoi_deposits_pct", "n_branches", "n_flooded_branches", "n_branches_in_aoi",
             "n_branches_sar_covered"]
EVENT_COLS = ["storm", "ticker", "bank_name", "exposure_pct", "aoi_deposits_pct", "day0", "days_landfall_to_day0",
              "pre_car", "car_0_1", "car_0_5", "car_0_20", "beta", "n_est"]
MAX_TRACT_ROWS = 500


def bank_exposure(storm: str | None = None, definition: str | None = None) -> dict:
    d = store.data_dir()
    be = store.load_parquet(d / "bank_exposure.parquet")
    if be is None:
        return store.missing("Bank exposure table", d / "bank_exposure.parquet", "scripts/run_banks.py")
    cfgs = store.storms_config()
    storms = [s for s in cfgs if s in set(be["storm"])]
    defs = sorted(be["definition"].unique())
    storm = storm if storm in storms else (storms[0] if storms else None)
    definition = definition if definition in defs else ("full" if "full" in defs else (defs[0] if defs else None))
    v = be[(be["storm"] == storm) & (be["definition"] == definition)]
    tk = ROOT / "config" / "bank_tickers.csv"
    tickers = pd.read_csv(tk) if tk.exists() else None
    return {
        "available": True, "source": store.rel(d / "bank_exposure.parquet"),
        "storms": [{"key": s, "name": cfgs[s]["name"], "year": cfgs[s]["year"]} for s in storms],
        "definitions": defs, "storm": storm, "definition": definition,
        "rows": store.records(v.sort_values("exposure_pct", ascending=False), BANK_COLS),
        "report": store.load_json(d / "bank_exposure_report.json"),
        "ticker_counts": None if tickers is None else {
            "active": int((tickers["status"] == "active").sum()), "delisted": int((tickers["status"] == "delisted").sum())},
    }


def _cross_section_rows(cs: dict) -> list[dict]:
    rows = []
    for defn, by_meas in (cs.get("by_definition") or {}).items():
        for meas, by_win in by_meas.items():
            for win, r in by_win.items():
                rows.append({"definition": defn, "measure": meas, "window": win, "coef_per_pct": store.clean(r.get("coef_per_pct")),
                             "t": store.clean(r.get("t")), "p": store.clean(r.get("p")), "n": r.get("n"),
                             "n_storms": r.get("n_storms"), "n_nonzero": r.get("n_nonzero_exposure"),
                             "se_type": r.get("se_type"), "note": r.get("note", "")})
    return rows


def market() -> dict:
    d = store.data_dir()
    cs = store.load_json(d / "cross_section.json")
    es = store.load_parquet(d / "event_study.parquet")
    bt = store.load_parquet(d / "backtest.parquet")
    bts = store.load_json(d / "backtest_summary.json")
    if cs is None and es is None and bts is None:
        return store.missing("Market test", d / "event_study.parquet", "scripts/run_market.py")
    full = es[es["definition"] == "full"] if es is not None and "definition" in es else es
    model = store.config("model.yaml").get("event_study") or {}
    return {
        "available": True,
        "benchmark": model.get("benchmark", "KRE"), "estimation_window": model.get("estimation_window", [-250, -30]),
        "caveats": (cs or {}).get("caveats") or [],
        "cross_section": _cross_section_rows(cs) if cs else [],
        "event_study": store.records(full, EVENT_COLS),
        "n_storms": int(full["storm"].nunique()) if full is not None else None,
        "backtest_summary": bts, "backtest": store.records(bt),
        "sources": [store.rel(d / f) for f in ("cross_section.json", "event_study.parquet", "backtest_summary.json")],
    }


def validation() -> dict:
    v = store.data_dir() / "validation"
    vs = store.load_json(v / "validation_summary.json")
    if vs is None:
        return store.missing("Validation summary", v / "validation_summary.json", "scripts/run_validation.py")
    grid = store.load_parquet(v / "threshold_grid.parquet")
    tv = store.load_parquet(v / "tract_validation.parquet")
    if tv is not None and "sar_covered" in tv:
        tv = tv[tv["sar_covered"]].sort_values("n_claims", ascending=False).head(MAX_TRACT_ROWS)
    return {"available": True, "source": store.rel(v / "validation_summary.json"), "summary": vs,
            "threshold_grid": store.records(grid), "tracts": store.records(tv)}


def methodology() -> dict:
    return {"assumptions_md": store.load_text(ROOT / "ASSUMPTIONS.md"), "model": store.config("model.yaml"),
            "storms_done": store.completed_storms()}
