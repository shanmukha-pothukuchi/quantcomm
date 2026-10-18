"""Stocks: dark market view of every public bank in config/bank_tickers.csv, plus benchmarks and P&C insurers.

Real yfinance closes only (data/raw/market/prices.parquet). The page is one self-contained HTML/JS
view (app/stocks.html) so range buttons, comparisons, hover and sorting don't trigger Streamlit reruns.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from components import (
    DATA,
    ROOT,
    load_json,
    load_parquet,
    load_text,
    rel,
    storms_config,
)

from floodalpha.banks import tickers
from floodalpha.market import stock_view

PRICES = ROOT / "data" / "raw" / "market" / "prices.parquet"
TEMPLATE = ROOT / "app" / "stocks.html"
PAGE_HEIGHT_PX = 1290

st.set_page_config(page_title="FloodAlpha · Stocks", page_icon="📈", layout="wide")
st.markdown("""<style>
.stApp, [data-testid="stHeader"] { background: #0c0d0f; }
.block-container { padding: 3.25rem 0.5rem 0 !important; max-width: 100% !important; }
[data-testid="stSidebar"] { background: #131417; }
[data-testid="stSidebar"] * { color: #ecedef; }
.stApp [data-testid="stMarkdownContainer"] p { color: #9a9da5; }
</style>""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def _payload(prices_mtime: float, exposure_mtime: float) -> str:
    live = load_json(DATA / "live" / "live_summary.json") or {}
    payload = stock_view.build_payload(
        prices=pd.read_parquet(PRICES),
        mapping=tickers.load(),
        exposure=load_parquet(DATA / "live" / "bank_alert_exposure.parquet"),
        storms=storms_config()["storms"],
        catalog=load_parquet(DATA / "isaias" / "s1_catalog.parquet"),
    )
    gen = live.get("generated_utc")
    payload["alerts_generated"] = pd.Timestamp(gen).strftime("%b %d %H:%M UTC") if gen else None
    # keep "</script>" out of the inline JSON block
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def _mtime(p) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


if not PRICES.exists():
    st.error(f"Price cache not found at `{rel(PRICES)}`. Run `PYTHONPATH=src .venv/bin/python -m floodalpha.market.prices`.")
    st.stop()
template = load_text(TEMPLATE)
if template is None:
    st.error(f"Template missing: `{rel(TEMPLATE)}`.")
    st.stop()

try:
    data = _payload(_mtime(PRICES), _mtime(DATA / "live" / "bank_alert_exposure.parquet"))
except Exception as exc:  # noqa: BLE001 — surface any pipeline-output problem instead of a blank page
    st.error(f"Could not build the stocks view from cached outputs: {exc}")
    st.stop()

components.html(template.replace("/*__DATA__*/", data), height=PAGE_HEIGHT_PX, scrolling=True)
