"""Planning logic shared by the CLI and the web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config import AGENTS
from .executor import available_agents
from .ledger import Ledger
from .planner import PlanError, build_prompt, extract_json, heuristic_plan, normalize


class NoAgentError(Exception):
    pass


def usable_agents(cfg: dict, agents, ledger: Ledger, only: str | None) -> list[str]:
    available = available_agents(agents, ledger, only)
    if not available:
        raise NoAgentError("no agent is available (disabled or on cooldown)")
    return available


def default_agent(cfg: dict, available: list[str]) -> str:
    prefer = cfg["routing"]["prefer"]
    return prefer if prefer in available else available[0]


def planner_prompt(task: str, cfg: dict, agents, ledger: Ledger, only: str | None) -> str:
    available = usable_agents(cfg, agents, ledger, only)
    days = int(cfg["routing"].get("usage_window_days", 7))
    return build_prompt(task, prefer=only or cfg["routing"]["prefer"],
                        usage=ledger.summary_text(days), days=days,
                        unavailable=[a for a in AGENTS if a not in available])


def parse_plan(text_or_dict, cfg: dict, agents, ledger: Ledger, only: str | None = None) -> dict:
    data = text_or_dict if isinstance(text_or_dict, dict) else extract_json(text_or_dict)
    return normalize(data, default_agent(cfg, usable_agents(cfg, agents, ledger, only)))


def auto_plan(task: str, cfg: dict, agents, ledger: Ledger, workdir: Path, backend: str,
              only: str | None, run_name: str, log: Callable[[str], None]) -> dict:
    """Plan with backend claude|codex|heuristic. Falls back to heuristic when configured."""
    available = usable_agents(cfg, agents, ledger, only)
    fallback_agent = default_agent(cfg, available)
    if backend == "heuristic":
        return heuristic_plan(task, fallback_agent)
    try:
        if backend not in available:
            raise PlanError(f"planner agent {backend} is unavailable")
        prompt = planner_prompt(task, cfg, agents, ledger, only)
        tier = cfg["planner"]["tier"]
        planner = agents[backend]
        log(f"planning with {backend} {tier} ({planner.model_label(tier)})…")
        res = planner.run(prompt, tier, workdir, text_only=True, timeout=600)
        ledger.record({"run": run_name, "task": "plan", "agent": backend, "tier": tier,
                       "model": res.model, "ok": res.ok, "rate_limited": res.rate_limited,
                       "seconds": round(res.seconds, 1), "usage": res.usage, "cost_usd": res.cost_usd})
        if res.rate_limited:
            ledger.set_cooldown(backend, float(cfg["routing"]["cooldown_hours"]))
        if not res.ok:
            raise PlanError(f"planner failed: {res.error.strip()[:300]}")
        return normalize(extract_json(res.text), fallback_agent)
    except PlanError as e:
        if not cfg["planner"].get("fallback_to_heuristic", True):
            raise
        log(f"{e} — falling back to heuristic routing")
        plan = heuristic_plan(task, fallback_agent)
        plan["summary"] = f"{plan['summary']} (planner failed: {str(e)[:120]})"
        return plan
