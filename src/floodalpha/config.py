"""Config loading and canonical paths. Nothing here hits the network."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"

load_dotenv(ROOT / ".env")


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


def storms() -> dict:
    return load_yaml("storms.yaml")["storms"]


def storm(key: str) -> dict:
    y = load_yaml("storms.yaml")
    s = y["storms"]
    if key not in s:
        raise KeyError(f"storm {key!r} not in config/storms.yaml (have {list(s)})")
    return {"key": key, **y.get("defaults", {}), **s[key]}


def model() -> dict:
    return load_yaml("model.yaml")


def storm_dir(key: str, kind: str = "processed") -> Path:
    base = DATA_PROCESSED if kind == "processed" else DATA_RAW
    d = base / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def env(name: str, required: bool = False) -> str | None:
    v = os.environ.get(name) or None
    if required and not v:
        raise RuntimeError(f"{name} is not set; add it to .env (see .env.example)")
    return v
