"""Load and merge YAML configuration files."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if key == "extends":
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(name: str = "default.yaml") -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open(encoding="utf-8") as handle:
        cfg: dict[str, Any] = yaml.safe_load(handle) or {}

    extends = cfg.pop("extends", None)
    if extends:
        parent = load_config(extends)
        cfg = _deep_merge(parent, cfg)

    cfg["_project_root"] = str(PROJECT_ROOT)
    cfg["_config_name"] = name
    return cfg


def data_dir(cfg: dict[str, Any]) -> Path:
    rel = cfg.get("data", {}).get("data_dir", "data")
    return PROJECT_ROOT / rel


def trd_env_name(cfg: dict[str, Any]) -> str:
    """SIMULATE = paper/sim account; REAL = live account. Single switch for MHImain."""
    env = str(cfg.get("mhimain", {}).get("trd_env", "SIMULATE")).upper()
    return "REAL" if env == "REAL" else "SIMULATE"


def trd_env_from_config(cfg: dict[str, Any]):
    """Futu TrdEnv enum for the configured account."""
    from futu import TrdEnv

    return TrdEnv.REAL if trd_env_name(cfg) == "REAL" else TrdEnv.SIMULATE


def is_paper_trading(cfg: dict[str, Any]) -> bool:
    """Deprecated alias: means Futu SIMULATE account, not skip-the-API dry-run."""
    return trd_env_name(cfg) != "REAL"
