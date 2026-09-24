"""Loading and merging the TOML config."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

AGENTS = ("claude", "codex")
TIERS = ("light", "medium", "heavy")

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.toml")


class ConfigError(Exception):
    pass


def hub_home() -> Path:
    return Path(os.environ.get("AIHUB_HOME", Path.home() / ".aihub"))


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def candidate_paths() -> list[Path]:
    return [Path.cwd() / "aihub.toml", hub_home() / "config.toml"]


def load(path: str | os.PathLike | None = None) -> dict:
    cfg = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["_source"] = "defaults"
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config not found: {p}")
        paths = [p]
    else:
        paths = [p for p in candidate_paths() if p.exists()]
    if paths:
        user_cfg = tomllib.loads(paths[0].read_text(encoding="utf-8"))
        cfg = deep_merge(cfg, user_cfg)
        cfg["_source"] = str(paths[0])
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    backend = cfg["planner"]["backend"]
    if backend not in ("manual", "codex", "claude", "heuristic"):
        raise ConfigError(f"planner.backend: unknown value {backend!r}")
    if cfg["planner"]["tier"] not in TIERS:
        raise ConfigError(f"planner.tier must be one of {TIERS}")
    if cfg["routing"]["prefer"] not in ("balanced", *AGENTS):
        raise ConfigError("routing.prefer must be balanced, claude or codex")
    for name in AGENTS:
        agent = cfg["agents"].get(name)
        if agent is None:
            raise ConfigError(f"agents.{name} section is missing")
        for tier in TIERS:
            if tier not in agent.get("tiers", {}):
                raise ConfigError(f"agents.{name}.tiers.{tier} is missing")
