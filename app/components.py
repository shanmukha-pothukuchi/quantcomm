"""Shared loaders, palette and helpers for the FloodAlpha dashboard.

Every loader is keyed on the file's mtime, so a pipeline re-run shows up on the next
Streamlit rerun without restarting the server. Missing files return None; the pages
render a "not generated yet" notice instead of inventing numbers.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from floodalpha.config import load_yaml  # noqa: E402

# FLOODALPHA_DATA_DIR lets tests point the app at an empty directory.
DATA = Path(os.environ.get("FLOODALPHA_DATA_DIR") or (ROOT / "data" / "processed"))

# ---------------------------------------------------------------- palette --
# dataviz reference palette; categorical slots 1-4 validated together (light mode):
# node scripts/validate_palette.js "#2a78d6,#eb6834,#1baf7a,#eda100" -> all checks pass
# (aqua/yellow are below 3:1 contrast on light surfaces, so every chart ships a table view).
S1_BLUE = "#2a78d6"
S2_ORANGE = "#eb6834"
S3_AQUA = "#1baf7a"
S4_YELLOW = "#eda100"
S7_VIOLET = "#4a3aa7"
S8_RED = "#e34948"
STATUS_GOOD = "#0ca30c"
STATUS_WARN = "#d58a00"
STATUS_SERIOUS = "#d03b3b"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#d7d6d0"

C_FLOOD = S1_BLUE
C_COVER = INK_2
C_WARN = S7_VIOLET
C_EXPOSED = STATUS_SERIOUS
C_OK = STATUS_GOOD
C_AFFECTED = S2_ORANGE
C_CONTROL = S4_YELLOW

ALERT_COLORS = {  # fixed order, never cycled; anything else folds to grey
    "Coastal Flood Warning": S1_BLUE,
    "Coastal Flood Watch": S3_AQUA,
    "Coastal Flood Advisory": S4_YELLOW,
    "Flood Warning": S2_ORANGE,
    "Flash Flood Warning": S8_RED,
    "Storm Surge Warning": S7_VIOLET,
}
ALERT_OTHER = INK_2

PITCH = ("Banks don't know their flood exposure for weeks after a storm. Free radar satellites can "
         "estimate it within days. We measure it for every US bank, stress-test a Capital One "
         "portfolio, and test whether the market prices it in.")


def altair_theme() -> None:
    import altair as alt

    def _theme():
        return {"config": {
            "background": "transparent",
            "view": {"stroke": None},
            "axis": {"gridColor": GRID, "gridOpacity": 0.5, "domainColor": GRID, "tickColor": GRID,
                     "labelColor": INK_2, "titleColor": INK_2, "labelFontSize": 11, "titleFontSize": 11},
            "legend": {"labelColor": INK_2, "titleColor": INK_2},
            "title": {"color": INK, "fontSize": 13, "anchor": "start", "fontWeight": 600},
            "bar": {"cornerRadiusEnd": 4},
            "line": {"strokeWidth": 2},
            "point": {"size": 64},
        }}

    alt.themes.register("floodalpha", _theme)
    alt.themes.enable("floodalpha")


# ----------------------------------------------------------------- loaders --
def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return -1.0


@st.cache_data(show_spinner=False)
def _json(path: str, mtime: float) -> dict | list | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


@st.cache_data(show_spinner=False)
def _parquet(path: str, mtime: float) -> pd.DataFrame | None:
    p = Path(path)
    return pd.read_parquet(p) if p.exists() else None


@st.cache_data(show_spinner=False)
def _geoparquet(path: str, mtime: float):
    p = Path(path)
    if not p.exists():
        return None
    import geopandas as gpd

    g = gpd.read_parquet(p)
    return g.to_crs(4326) if g.crs and g.crs.to_epsg() != 4326 else g


@st.cache_data(show_spinner=False)
def _text(path: str, mtime: float) -> str | None:
    p = Path(path)
    return p.read_text() if p.exists() else None


@st.cache_data(show_spinner=False)
def _png_url(path: str, mtime: float) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def load_json(path: Path):
    return _json(str(path), _mtime(path))


def load_parquet(path: Path):
    return _parquet(str(path), _mtime(path))


def load_geoparquet(path: Path):
    return _geoparquet(str(path), _mtime(path))


def load_text(path: Path):
    return _text(str(path), _mtime(path))


def png_data_url(path: Path):
    return _png_url(str(path), _mtime(path))


@st.cache_data(show_spinner=False)
def storms_config() -> dict:
    return load_yaml("storms.yaml")


@st.cache_data(show_spinner=False)
def model_config() -> dict:
    return load_yaml("model.yaml")


def completed_storms() -> list[str]:
    """Storms with a flood_meta.json, in storms.yaml order."""
    return [k for k in storms_config()["storms"] if (DATA / k / "flood_meta.json").exists()]


# ----------------------------------------------------------------- helpers --
def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def missing(label: str, path: Path, hint: str | None = None) -> None:
    st.info(f"**{label}** not generated yet (expected `{rel(path)}`)." + (f" Run `{hint}`." if hint else ""))


def source(path: Path | str, note: str = "") -> None:
    p = rel(path) if isinstance(path, Path) else path
    st.caption(f"Source: `{p}`" + (f" — {note}" if note else ""))


def synthetic_banner() -> None:
    st.warning("**SYNTHETIC DATA.** Customers, balances, loans and losses in this section are generated by "
               "`floodalpha.nessie.portfolio` for a simulated Capital One portfolio and pushed to the Nessie "
               "sandbox. They are not real people or real Capital One accounts.", icon="⚠️")


def fmt_dt(s, with_tz: bool = True) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)) or s == "":
        return "—"
    try:
        t = pd.Timestamp(s)
        t = t.tz_convert("UTC") if t.tzinfo else t
        return t.strftime("%Y-%m-%d %H:%M" + (" UTC" if with_tz else ""))
    except Exception:  # noqa: BLE001
        return str(s)


def fmt_num(v, fmt: str = "{:,.1f}", dash: str = "—") -> str:
    try:
        if v is None or pd.isna(v):
            return dash
        return fmt.format(v)
    except Exception:  # noqa: BLE001
        return dash


def hours_between(a, b) -> float | None:
    try:
        return (pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None
