"""Parking-lot activity index, its sanity checks, a YoY trading signal and a no-lookahead backtest.

Pure functions over the cached lot-backscatter table and price table; no network.

activity index: per store, rel_db = lot dB - roof dB (same scene), z-scored over the store's own
history; the index for an acquisition evening is the mean z across stores read that evening.
Ascending CONUS passes are ~00:00 UTC, i.e. the previous local evening, so the local date is taken
as (UTC - 6 h).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

LOCAL_OFFSET = pd.Timedelta(hours=6)
MIN_STORE_READINGS = 10


def _db(x: pd.Series) -> pd.Series:
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10.0 * np.log10(x.where(x > 0))


def activity_index(df: pd.DataFrame, causal: bool = False) -> pd.DataFrame:
    """date, activity (mean store z), n_stores, n_metros. Stores with < MIN_STORE_READINGS are dropped.

    causal=False z-scores each store against its whole history (descriptive chart). causal=True uses only
    that store's earlier readings (expanding mean/std, first MIN_STORE_READINGS dropped) - the version the
    trading signal and backtest must use, so no reading is normalised with future data."""
    d = df.assign(rel_db=_db(df["lot_lin"]) - _db(df["bldg_lin"])).dropna(subset=["rel_db"])
    d = d.sort_values(["store_id", "datetime_utc"])
    counts = d.groupby("store_id")["rel_db"].transform("size")
    d = d[counts >= MIN_STORE_READINGS]
    if d.empty:
        return pd.DataFrame(columns=["date", "activity", "n_stores", "n_metros"])
    g = d.groupby("store_id")["rel_db"]
    if causal:
        mu = g.transform(lambda s: s.expanding(MIN_STORE_READINGS).mean().shift())
        sd = g.transform(lambda s: s.expanding(MIN_STORE_READINGS).std().shift())
    else:
        mu, sd = g.transform("mean"), g.transform("std")
    d = d.assign(z=(d["rel_db"] - mu) / sd.replace(0, np.nan),
                 date=(d["datetime_utc"].dt.tz_convert(None) - LOCAL_OFFSET).dt.normalize())
    out = d.dropna(subset=["z"]).groupby("date").agg(activity=("z", "mean"), n_stores=("store_id", "nunique"),
                                                     n_metros=("metro", "nunique"))
    return out.reset_index().sort_values("date").reset_index(drop=True)


def _in_md_window(dates: pd.Series, start_md: str, end_md: str) -> pd.Series:
    md = dates.dt.strftime("%m-%d")
    return (md >= start_md) & (md <= end_md)


def _welch(a: pd.Series, b: pd.Series) -> dict:
    a, b = a.dropna(), b.dropna()
    if len(a) < 3 or len(b) < 3:
        return {"diff": None, "t": None, "p": None, "n_in": int(len(a)), "n_out": int(len(b))}
    t, p = stats.ttest_ind(a, b, equal_var=False)
    return {"diff": float(a.mean() - b.mean()), "t": float(t), "p": float(p), "n_in": int(len(a)), "n_out": int(len(b))}


def validate(idx: pd.DataFrame, holiday_window: list[str]) -> dict:
    """If the index measures cars, the holiday season should be busier and so (weakly) should weekends."""
    hol = _in_md_window(idx["date"], *holiday_window)
    wkend = idx["date"].dt.dayofweek >= 5
    return {"holiday": _welch(idx.loc[hol, "activity"], idx.loc[~hol, "activity"]),
            "weekend": _welch(idx.loc[wkend, "activity"], idx.loc[~wkend, "activity"]),
            "holiday_window": holiday_window}


def yoy_signal(idx: pd.DataFrame, window_days: int, min_scenes: int) -> pd.DataFrame:
    """At each acquisition date t: mean activity over (t-w, t] minus mean over the same window a year earlier."""
    s = idx.set_index("date")["activity"].sort_index()
    w, yr = pd.Timedelta(days=window_days), pd.Timedelta(days=365)
    rows = []
    for t in s.index:
        cur, prev = s[(s.index > t - w) & (s.index <= t)], s[(s.index > t - yr - w) & (s.index <= t - yr)]
        if len(cur) >= min_scenes and len(prev) >= min_scenes:
            rows.append({"date": t, "signal": float(cur.mean() - prev.mean()), "n_cur": len(cur), "n_prev": len(prev)})
    return pd.DataFrame(rows, columns=["date", "signal", "n_cur", "n_prev"])


def forward_excess(prices: pd.DataFrame, ticker: str, bench: str, dates: pd.Series, lag_days: int,
                   horizon: int) -> pd.Series:
    """Log return of ticker minus bench from the first close >= date + lag_days, held `horizon` trading days.
    NaN when the window runs past the last close (no peeking at unfinished windows)."""
    px = prices[[ticker, bench]].dropna().sort_index()
    lr = np.log(px)
    out = []
    for d in dates:
        i = px.index.searchsorted(pd.Timestamp(d) + pd.Timedelta(days=lag_days))
        j = i + horizon
        if j >= len(px):
            out.append(np.nan)
            continue
        out.append(float((lr[ticker].iloc[j] - lr[ticker].iloc[i]) - (lr[bench].iloc[j] - lr[bench].iloc[i])))
    return pd.Series(out, index=pd.Index(dates).rename("date"), name="fwd_excess")


def backtest(sig: pd.DataFrame, prices: pd.DataFrame, ticker: str, bench: str, lag_days: int, horizon: int) -> dict:
    """Does a positive YoY activity change predict outperformance over the next `horizon` trading days?

    Readings are 12 days apart and windows 63 trading days long, so consecutive observations overlap;
    `n_independent` (span / horizon) is the honest sample size and the p-value uses it."""
    if sig.empty:
        return {"n": 0}
    fwd = forward_excess(prices, ticker, bench, sig["date"], lag_days, horizon)
    d = sig.assign(fwd=fwd.values).dropna(subset=["fwd"])
    if len(d) < 5:
        return {"n": int(len(d))}
    rho = float(stats.spearmanr(d["signal"], d["fwd"]).statistic)
    span_days = (d["date"].max() - d["date"].min()).days
    n_ind = max(2, int(span_days / (horizon * 365 / 252)))
    t = rho * math.sqrt((n_ind - 2) / max(1e-9, 1 - rho ** 2))
    p = float(2 * stats.t.sf(abs(t), df=n_ind - 2))
    pos, neg = d[d["signal"] > 0], d[d["signal"] <= 0]
    hit = float(((d["signal"] > 0) == (d["fwd"] > 0)).mean())
    return {"n": int(len(d)), "n_independent": n_ind, "spearman": rho, "p_overlap_adj": p, "hit_rate": hit,
            "mean_fwd_when_up_pct": float(pos["fwd"].mean() * 100) if len(pos) else None,
            "mean_fwd_when_down_pct": float(neg["fwd"].mean() * 100) if len(neg) else None,
            "first": d["date"].min().strftime("%Y-%m-%d"), "last": d["date"].max().strftime("%Y-%m-%d"),
            "horizon_days": horizon, "lag_days": lag_days, "bench": bench}


def verdict(latest: float | None, bt: dict, val: dict, alpha: float = 0.10) -> dict:
    """Plain-language read of the signal. 'No edge' unless the index passes its sanity check AND the
    backtest is significant after the overlap adjustment."""
    sane = (val["holiday"]["diff"] or 0) > 0 and (val["holiday"]["p"] or 1) < alpha
    edge = bt.get("p_overlap_adj") is not None and bt["p_overlap_adj"] < alpha and bt["spearman"] > 0
    if latest is None:
        return {"stance": "no reading", "reason": "not enough scenes for a YoY comparison yet"}
    direction = "up" if latest > 0 else "down"
    if not sane:
        return {"stance": "unvalidated", "direction": direction,
                "reason": "index does not yet show the expected holiday-season lift, so it may be measuring weather, not cars"}
    if not edge:
        return {"stance": "no edge", "direction": direction,
                "reason": f"lot activity is {direction} YoY, but the backtest finds no reliable link to forward excess returns"}
    return {"stance": "lean long" if latest > 0 else "lean short", "direction": direction,
            "reason": f"lot activity is {direction} YoY and positive readings have preceded outperformance in the backtest"}
