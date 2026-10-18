"""Market-model event study and cross-sectional regression of CARs on flood exposure.

Day 0 = first NYSE trading day whose close comes strictly after the storm's event time (SAR publication),
in America/New_York; a publication after 16:00 ET rolls to the next trading day. Abnormal return =
bank return − (alpha + beta × benchmark return), with alpha/beta estimated over [-250, -30] trading days.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

from floodalpha import config
from floodalpha.market.prices import load_prices

log = logging.getLogger(__name__)
OUT = config.DATA_PROCESSED
PLOTS = OUT / "market"
NY = "America/New_York"


def event_day0(event_time_utc: pd.Timestamp, trading_days: pd.DatetimeIndex) -> pd.Timestamp:
    """First trading day whose 16:00 ET close is strictly after the event time."""
    t = pd.Timestamp(event_time_utc)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    t_ny = t.tz_convert(NY)
    d = t_ny.normalize().tz_localize(None)
    if t_ny.hour >= 16:
        d = d + pd.Timedelta(days=1)
    pos = trading_days.searchsorted(d)
    if pos >= len(trading_days):
        raise ValueError(f"no trading day on/after {d.date()} in price data")
    return trading_days[pos]


def market_model(ret: pd.DataFrame, ticker: str, bench: str, day0_pos: int, est: tuple[int, int]) -> tuple[float, float, int] | None:
    lo, hi = day0_pos + est[0], day0_pos + est[1]
    if lo < 0:
        return None
    w = ret.iloc[lo:hi + 1][[ticker, bench]].dropna()
    if len(w) < 100:
        return None
    X = sm.add_constant(w[bench].values)
    b = np.linalg.lstsq(X, w[ticker].values, rcond=None)[0]
    return float(b[0]), float(b[1]), int(len(w))


def car(ret: pd.DataFrame, ticker: str, bench: str, day0_pos: int, alpha: float, beta: float, w0: int, w1: int) -> float | None:
    seg = ret.iloc[day0_pos + w0: day0_pos + w1 + 1][[ticker, bench]]
    if seg[ticker].isna().any() or len(seg) < (w1 - w0 + 1):
        return None
    ar = seg[ticker] - (alpha + beta * seg[bench])
    return float(ar.sum())


def run_event_study(exposure: pd.DataFrame | None = None) -> pd.DataFrame:
    cfg = config.model()["event_study"]
    bench = cfg["benchmark"]
    windows = [tuple(w) for w in cfg["car_windows"]]
    est = tuple(cfg["estimation_window"])
    px = load_prices()
    ret = px.pct_change()
    days = px.index
    exp = exposure if exposure is not None else pd.read_parquet(OUT / "bank_exposure.parquet")
    exp = exp[(exp["ticker"] != "") & exp["ticker"].notna()]
    rows = []
    for (storm, definition), g in exp.groupby(["storm", "definition"]):
        et = pd.Timestamp(g["event_time_utc"].iloc[0])
        lf = pd.Timestamp(g["landfall_time_utc"].iloc[0])
        d0 = event_day0(et, days)
        d0_pos = int(days.get_loc(d0))
        lf_day = days[days.searchsorted(lf.tz_convert(NY).normalize().tz_localize(None))]  # first trading day on/after landfall
        lf_pos = int(days.get_loc(lf_day))
        for r in g.itertuples():
            t = r.ticker
            if t not in ret.columns:
                continue
            mm = market_model(ret, t, bench, d0_pos, est)
            if mm is None:
                continue
            a, b, n = mm
            row = {"storm": storm, "definition": definition, "ticker": t, "cert": r.cert, "bank_name": r.bank_name,
                   "exposure_pct": r.exposure_pct, "aoi_deposits_pct": r.aoi_deposits_pct, "total_deposits_k": r.total_deposits_k,
                   "event_time_utc": et, "day0": d0, "landfall_day": lf_day, "alpha": a, "beta": b, "n_est": n,
                   "days_landfall_to_day0": d0_pos - lf_pos}
            for w0, w1 in windows:
                row[f"car_{w0}_{w1}"] = car(ret, t, bench, d0_pos, a, b, w0, w1)
            # what the market already knew: landfall-eve close -> day -1 close
            pre = car(ret, t, bench, lf_pos, a, b, 0, d0_pos - lf_pos - 1) if d0_pos - lf_pos >= 1 else 0.0
            row["pre_car"] = pre
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("event study produced no rows (no priced banks with exposure data)")
    df.to_parquet(OUT / "event_study.parquet", index=False)
    return df


def cross_section(es: pd.DataFrame | None = None) -> dict:
    cfg = config.model()["event_study"]
    windows = [tuple(w) for w in cfg["car_windows"]]
    es = es if es is not None else pd.read_parquet(OUT / "event_study.parquet")
    res = {"by_definition": {}, "caveats": CAVEATS, "benchmark": cfg["benchmark"], "estimation_window": cfg["estimation_window"]}
    for definition, g in es.groupby("definition"):
        res["by_definition"][definition] = {}
        for xvar in ("exposure_pct", "aoi_deposits_pct"):
            res["by_definition"][definition][xvar] = {}
            for w0, w1 in windows:
                y = f"car_{w0}_{w1}"
                d = g.dropna(subset=[y, xvar])
                d = d[d["exposure_pct"].notna()]
                n_storms = d["storm"].nunique()
                if len(d) < 10 or n_storms < 1 or d[xvar].std() == 0:
                    res["by_definition"][definition][xvar][y] = {"n": int(len(d)), "n_storms": int(n_storms), "note": "insufficient variation"}
                    continue
                X = pd.get_dummies(d[["storm"]], drop_first=False, dtype=float)
                X[xvar] = d[xvar].values
                model = sm.OLS(d[y].values, X.values)
                cov = {"cov_type": "cluster", "cov_kwds": {"groups": pd.factorize(d["storm"])[0]}} if n_storms >= 2 else {"cov_type": "HC1"}
                fit = model.fit(**cov)
                i = list(X.columns).index(xvar)
                res["by_definition"][definition][xvar][y] = {
                    "coef_per_pct": float(fit.params[i]), "t": float(fit.tvalues[i]), "p": float(fit.pvalues[i]),
                    "n": int(len(d)), "n_storms": int(n_storms), "r2": float(fit.rsquared),
                    "se_type": cov["cov_type"], "mean_car": float(d[y].mean()),
                    "n_nonzero_exposure": int((d[xvar] > 0).sum()),
                }
    (OUT / "cross_section.json").write_text(json.dumps(res, indent=1, default=str))
    return res


CAVEATS = [
    "Small sample: at most 12 storms; standard errors clustered by storm are unreliable with so few clusters.",
    "Overlapping events (Harvey/Irma 2017, Helene/Milton 2024) share estimation and event windows.",
    "Most banks have zero measured exposure; the regression is driven by a handful of regionals.",
    "Survivorship bias: banks acquired or delisted before today (SunTrust, IBERIABANK, Comerica, Synovus, Cadence, ...) have no yfinance history and are excluded from the market test even though their exposure is computed.",
    "Three CAR windows and two exposure measures are tested; no multiple-testing correction is applied.",
    "AOI bboxes are analyst-chosen; branches outside the box count as unexposed for that storm.",
    "SAR flood masks are conservative (dark-water change only) and taken from the first post-landfall passes, often before river crests; exposure_pct measures standing water near branches at that instant, not total damage.",
    "Expected relationship: more exposure -> more negative abnormal return. A null is a legitimate finding.",
]


def plots(es: pd.DataFrame | None = None) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    PLOTS.mkdir(parents=True, exist_ok=True)
    es = es if es is not None else pd.read_parquet(OUT / "event_study.parquet")
    cfg = config.model()["event_study"]
    windows = [tuple(w) for w in cfg["car_windows"]]
    out = []
    g = es[es["definition"] == "full"]
    fig, axes = plt.subplots(1, len(windows), figsize=(5 * len(windows), 4), sharey=True)
    for ax, (w0, w1) in zip(np.atleast_1d(axes), windows):
        y = f"car_{w0}_{w1}"
        for storm, s in g.groupby("storm"):
            ax.scatter(s["exposure_pct"], 100 * s[y], s=14, alpha=0.7, label=storm)
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_xlabel("exposure_pct (deposits within 500 m of SAR flood)")
        ax.set_title(f"CAR [{w0},{w1}] vs exposure")
    np.atleast_1d(axes)[0].set_ylabel("CAR (%)")
    np.atleast_1d(axes)[-1].legend(fontsize=7)
    fig.tight_layout()
    p = PLOTS / "car_vs_exposure.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    out.append(p)

    # average CAR path, top vs bottom exposure tercile (full definition), using daily ARs recomputed from prices
    px = load_prices()
    ret = px.pct_change()
    bench = cfg["benchmark"]
    paths = {"top": [], "bottom": []}
    for storm, s in g.groupby("storm"):
        s = s[s["exposure_pct"].notna()]
        if (s["exposure_pct"] > 0).sum() < 3:
            continue
        q = s["exposure_pct"].quantile([1 / 3, 2 / 3])
        groups = {"top": s[s["exposure_pct"] >= q.iloc[1]], "bottom": s[s["exposure_pct"] <= q.iloc[0]]}
        for k, gg in groups.items():
            for r in gg.itertuples():
                d0 = int(px.index.get_loc(pd.Timestamp(r.day0)))
                seg = ret.iloc[d0 - 5: d0 + 21][[r.ticker, bench]]
                ar = (seg[r.ticker] - (r.alpha + r.beta * seg[bench])).values
                if len(ar) == 26 and not np.isnan(ar).any():
                    paths[k].append(np.cumsum(ar))
    if paths["top"] and paths["bottom"]:
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(-5, 21)
        for k, c in (("top", "#c0392b"), ("bottom", "#2471a3")):
            ax.plot(x, 100 * np.mean(paths[k], axis=0), color=c, label=f"{k} exposure tercile (n={len(paths[k])})")
        ax.axvline(0, color="#888", lw=0.8, ls="--")
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_xlabel("trading days from SAR publication (day 0)")
        ax.set_ylabel("mean cumulative abnormal return (%)")
        ax.legend()
        fig.tight_layout()
        p = PLOTS / "car_path_terciles.png"
        fig.savefig(p, dpi=110)
        plt.close(fig)
        out.append(p)
    return out
