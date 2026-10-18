"""Read-only access to pipeline outputs for the web app.

Every loader is keyed on the file's mtime, so a pipeline re-run shows up on the next request
without restarting the server. Missing files return None; the API reports them as
"not generated yet" with the command that produces them instead of inventing numbers.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import shapely

from quantcomm.config import ROOT, load_yaml

CACHE_SIZE = 64
COORD_PRECISION_DEG = 1e-5  # ~1 m; trims GeoJSON size without visible change


def data_dir() -> Path:
    """FLOODALPHA_DATA_DIR lets tests point the app at an empty or fixture directory."""
    return Path(os.environ.get("FLOODALPHA_DATA_DIR") or (ROOT / "data" / "processed"))


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return -1.0


@lru_cache(maxsize=CACHE_SIZE)
def _json(path: str, mtime: float) -> Any:
    return json.loads(Path(path).read_text()) if mtime >= 0 else None


@lru_cache(maxsize=CACHE_SIZE)
def _parquet(path: str, mtime: float) -> pd.DataFrame | None:
    return pd.read_parquet(path) if mtime >= 0 else None


@lru_cache(maxsize=CACHE_SIZE)
def _text(path: str, mtime: float) -> str | None:
    return Path(path).read_text() if mtime >= 0 else None


def load_json(path: Path) -> Any:
    return _json(str(path), _mtime(path))


def load_parquet(path: Path) -> pd.DataFrame | None:
    df = _parquet(str(path), _mtime(path))
    return None if df is None else df.copy()  # callers may filter/assign; keep the cached frame pristine


def load_text(path: Path) -> str | None:
    return _text(str(path), _mtime(path))


@lru_cache(maxsize=CACHE_SIZE)
def _geojson(path: str, mtime: float, simplify_deg: float, keep: tuple[str, ...]) -> dict | None:
    if mtime < 0:
        return None
    import geopandas as gpd

    g = gpd.read_parquet(path) if path.endswith(".parquet") else gpd.read_file(path)
    if g.crs is not None and g.crs.to_epsg() != 4326:
        g = g.to_crs(4326)
    g = g[[c for c in keep if c in g.columns] + ["geometry"]]
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    if simplify_deg > 0:
        g = g.assign(geometry=g.geometry.simplify(simplify_deg, preserve_topology=True))
    g = g.assign(geometry=shapely.set_precision(g.geometry.values, COORD_PRECISION_DEG))
    g = g[~g.geometry.is_empty]
    for c in g.columns:  # timestamps aren't JSON-serializable; ship ISO strings
        if c != "geometry" and pd.api.types.is_datetime64_any_dtype(g[c]):
            g = g.assign(**{c: g[c].map(lambda t: None if pd.isna(t) else t.isoformat())})
    return json.loads(g.to_json(na="null", to_wgs84=False, drop_id=True))


def load_geojson(path: Path, simplify_deg: float = 0.0, keep: tuple[str, ...] = ()) -> dict | None:
    """Vector file -> GeoJSON dict in EPSG:4326, optionally simplified for the browser."""
    return _geojson(str(path), _mtime(path), simplify_deg, keep)


def records(df: pd.DataFrame | None, cols: list[str] | None = None) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN -> null, timestamps -> ISO strings)."""
    if df is None:
        return []
    view = df[[c for c in cols if c in df.columns]] if cols else df
    return json.loads(view.to_json(orient="records", date_format="iso"))


def clean(v: Any) -> Any:
    """Scalar -> JSON-safe value (NaN/inf -> None, numpy -> python)."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def missing(label: str, path: Path, hint: str | None = None) -> dict:
    return {"available": False, "label": label, "expected": rel(path), "hint": hint}


@lru_cache(maxsize=4)
def _yaml(name: str, mtime: float) -> dict:
    return load_yaml(name)


def config(name: str) -> dict:
    return _yaml(name, _mtime(ROOT / "config" / name))


def storms_config() -> dict:
    return config("storms.yaml")["storms"]


def completed_storms() -> list[str]:
    """Storms with a flood_meta.json, in storms.yaml order."""
    return [k for k in storms_config() if (data_dir() / k / "flood_meta.json").exists()]
