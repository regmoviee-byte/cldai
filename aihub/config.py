"""Loading and merging the TOML config."""

from __future__ import annotations

import copy
import json
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


def user_config_path() -> Path:
    """The file the UI edits: ./aihub.toml if present, else ~/.aihub/config.toml."""
    return next((p for p in candidate_paths() if p.exists()), hub_home() / "config.toml")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ConfigError(f"cannot write {type(v).__name__} to TOML")


def dump_toml(data: dict, prefix: str = "") -> str:
    scalars = [f"{k} = {_toml_value(v)}" for k, v in data.items()
               if not isinstance(v, dict) and not k.startswith("_")]
    out = []
    if scalars:
        if prefix:
            out.append(f"[{prefix}]")
        out += scalars
        out.append("")
    for k, v in data.items():
        if isinstance(v, dict):
            out.append(dump_toml(v, f"{prefix}.{k}" if prefix else k))
    return "\n".join(out)


def save_user_config(text: str, path: Path | None = None) -> Path:
    """Validate TOML text against defaults and write it to path (default: user_config_path())."""
    try:
        user = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML: {e}") from None
    validate(deep_merge(tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")), user))
    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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
