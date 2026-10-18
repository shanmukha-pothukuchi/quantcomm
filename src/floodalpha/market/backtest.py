"""Long-short backtest: at each storm's day 0, short the top exposure tercile and long the bottom tercile
(equal weight; long KRE instead when fewer than 6 banks have exposure > 0), hold H trading days, 10 bps per side."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from floodalpha import config
from floodalpha.market.prices import load_prices

OUT = config.DATA_PROCESSED
TC_PER_SIDE = 0.0010
HOLDS = (5, 20)
MIN_EXPOSED = 6


def leg_return(px: pd.DataFrame, tickers: list[str], d0_pos: int, h: int) -> float | None:
    p0 = px.iloc[d0_pos - 1][tickers]  # enter at the close before day 0 is not possible (signal arrives day 0); use day-0 close
    p0 = px.iloc[d0_pos][tickers]
    p1 = px.iloc[min(d0_pos + h, len(px) - 1)][tickers]
    r = (p1 / p0 - 1).dropna()
    return float(r.mean()) if len(r) else None


def run_backtest(es: pd.DataFrame | None = None, definition: str = "full") -> dict:
    es = es if es is not None else pd.read_parquet(OUT / "event_study.parquet")
    px = load_prices()
    rows = []
    for storm, g in es[es["definition"] == definition].groupby("storm"):
        g = g.dropna(subset=["exposure_pct"]).drop_duplicates("ticker")
        g = g[g["ticker"].isin(px.columns)]
        d0 = pd.Timestamp(g["day0"].iloc[0])
        d0_pos = int(px.index.get_loc(d0))
        n_exposed = int((g["exposure_pct"] > 0).sum())
        if n_exposed < 3:
            rows.append({"storm": storm, "day0": d0, "n_exposed": n_exposed, "trade": "skipped: fewer than 3 banks with exposure > 0"})
            continue
        q = g["exposure_pct"].quantile([1 / 3, 2 / 3])
        short = g[g["exposure_pct"] >= q.iloc[1]]["ticker"].tolist()
        if n_exposed >= MIN_EXPOSED:
            long_ = g[g["exposure_pct"] <= q.iloc[0]]["ticker"].tolist()
            long_desc = f"bottom tercile ({len(long_)})"
        else:
            long_ = ["KRE"]
            long_desc = "KRE"
        for h in HOLDS:
            rs = leg_return(px, short, d0_pos, h)
            rl = leg_return(px, long_, d0_pos, h)
            if rs is None or rl is None:
                continue
            net = rl - rs - 4 * TC_PER_SIDE  # two legs, in and out
            rows.append({"storm": storm, "day0": d0, "hold_days": h, "n_exposed": n_exposed, "n_short": len(short), "long_leg": long_desc,
                         "short_ret": rs, "long_ret": rl, "net_ret": net, "trade": "executed"})
    bt = pd.DataFrame(rows)
    bt.to_parquet(OUT / "backtest.parquet", index=False)
    summary = {"definition": definition, "transaction_cost_per_side": TC_PER_SIDE, "rule": "short top exposure tercile, long bottom tercile (or KRE if < 6 exposed banks), equal weight", "by_hold": {}}
    ex = bt[bt.get("trade", pd.Series(dtype=str)) == "executed"] if len(bt) else bt
    for h in HOLDS:
        s = ex[ex["hold_days"] == h] if len(ex) else ex
        if len(s) == 0:
            summary["by_hold"][str(h)] = {"n_trades": 0, "note": "no executable trades (exposure is zero for nearly all banks)"}
            continue
        summary["by_hold"][str(h)] = {
            "n_trades": int(len(s)), "hit_rate": float((s["net_ret"] > 0).mean()), "mean_net_ret_pct": float(100 * s["net_ret"].mean()),
            "cum_net_ret_pct": float(100 * ((1 + s["net_ret"]).prod() - 1)), "per_storm_net_ret_pct": {r.storm: round(100 * r.net_ret, 3) for r in s.itertuples()},
        }
    summary["skipped"] = [r.storm for r in bt.itertuples() if r.trade != "executed"] if len(bt) else []
    (OUT / "backtest_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    _plot(ex)
    return summary


def _plot(ex: pd.DataFrame) -> None:
    if len(ex) == 0:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (OUT / "market").mkdir(exist_ok=True, parents=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    for h, c in zip(HOLDS, ("#2471a3", "#c0392b")):
        s = ex[ex["hold_days"] == h].sort_values("day0")
        if len(s):
            ax.plot(s["day0"], 100 * ((1 + s["net_ret"]).cumprod() - 1), marker="o", color=c, label=f"hold {h} d")
    ax.axhline(0, color="#888", lw=0.8)
    ax.set_ylabel("cumulative net return (%)")
    ax.set_title("Long-short flood-exposure trades (after 10 bps/side)")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT / "market" / "backtest_cum_pnl.png", dpi=110)
    plt.close(fig)
