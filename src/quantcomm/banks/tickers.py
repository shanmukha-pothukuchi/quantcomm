"""Holding company / bank -> ticker mapping (config/bank_tickers.csv), applied to any SOD vintage.

Rules (tested in tests/test_bank_tickers.py):
  * mapping rows with a known rssdhcr match on rssdhcr equality only (names change across vintages: BB&T -> Truist);
  * rows with rssdhcr == 0 and match_on == "hc" (delisted HCs whose id we did not resolve) match NAMEHCR by regex, but
    only the single holding company with the largest deposits in that vintage is taken — several unrelated small
    HCs share names like "COMMERCE BANCSHARES, INC." or "FIRST CITIZENS BANCSHARES, INC.";
  * rows with match_on == "bank" match the bank's own name (banks with no holding company), same largest-only rule.
"""
from __future__ import annotations

import re

import pandas as pd

from quantcomm import config


def load() -> pd.DataFrame:
    df = pd.read_csv(config.CONFIG_DIR / "bank_tickers.csv", dtype={"ticker": "string", "notes": "string"})
    df["ticker"] = df["ticker"].fillna("").astype(str)
    df["notes"] = df["notes"].fillna("").astype(str)
    df["rssdhcr"] = df["rssdhcr"].fillna(0).astype(int)
    return df


def assign_tickers(branches: pd.DataFrame, mapping: pd.DataFrame | None = None) -> pd.DataFrame:
    m = mapping if mapping is not None else load()
    out = branches.copy()
    out["ticker"] = ""
    out["company"] = ""
    out["ticker_status"] = "none"
    hc_dep = out[out["rssdhcr"] > 0].groupby(["rssdhcr", "namehcr"])["deposits_k"].sum().reset_index()
    bank_dep = out.groupby(["cert", "bank_name"])["deposits_k"].sum().reset_index()
    for r in m.itertuples():
        if r.rssdhcr > 0:
            sel = out["rssdhcr"] == int(r.rssdhcr)
        else:
            rx = re.compile(r.namehcr_pattern, re.I)
            if r.match_on == "hc":
                c = hc_dep[hc_dep["namehcr"].astype(str).str.contains(rx)]
                if c.empty:
                    continue
                sel = out["rssdhcr"] == int(c.sort_values("deposits_k").iloc[-1]["rssdhcr"])
            else:
                c = bank_dep[bank_dep["bank_name"].astype(str).str.contains(rx)]
                if c.empty:
                    continue
                sel = out["cert"] == int(c.sort_values("deposits_k").iloc[-1]["cert"])
        sel = sel & (out["ticker"] == "")
        out.loc[sel, ["ticker", "company", "ticker_status"]] = [r.ticker, r.company, r.status]
    return out
