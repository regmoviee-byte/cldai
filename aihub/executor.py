"""Runs a plan: dependency order, failover on quota limits, tier escalation, logging."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .agents import Agent, Result
from .config import AGENTS, TIERS
from .ledger import Ledger
from .planner import topo_order

WORKER_PROMPT = """\
You are one worker in a multi-agent pipeline. Do ONLY the subtask below; other workers handle the
rest of the overall goal, so don't expand the scope.

Overall goal (context only): {goal}

Your subtask ({tid}): {title}
{prompt}
{context}
When finished, reply with a short report: what you did, which files you changed, anything left
undone or suspicious. Keep it under 30 lines.
"""


def other_agent(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def available_agents(agents: dict[str, Agent], ledger: Ledger, only: str | None = None) -> list[str]:
    names = [only] if only else list(AGENTS)
    return [a for a in names if agents[a].enabled and not ledger.is_cooling(a)]


def reroute(plan: dict, available: list[str]) -> list[str]:
    """Move tasks off unavailable agents. Returns human-readable notes."""
    if not available:
        raise RuntimeError("no agent is available (all disabled or on cooldown; see `aihub cooldown`)")
    notes = []
    for t in plan["tasks"]:
        if t["agent"] not in available:
            new = available[0]
            notes.append(f"{t['id']}: {t['agent']} unavailable → {new}")
            t["agent"] = new
    return notes


class Executor:
    def __init__(self, cfg: dict, agents: dict[str, Agent], ledger: Ledger, workdir: Path,
                 run_dir: Path, log: Callable[[str], None] = print, only: str | None = None):
        self.cfg = cfg
        self.agents = agents
        self.ledger = ledger
        self.workdir = workdir
        self.run_dir = run_dir
        self.log = log
        self.only = only
        ex = cfg["execution"]
        self.parallel = max(1, int(ex.get("parallel", 1)))
        self.escalate = bool(ex.get("escalate_on_failure", True))
        self.context_chars = int(ex.get("context_chars", 4000))
        self.timeout = float(ex.get("timeout", 3600))
        self.cooldown_hours = float(cfg["routing"].get("cooldown_hours", 5))

    # ----------------------------------------------------------------- public
    def run(self, plan: dict, goal: str) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                                encoding="utf-8")
        order = topo_order(plan)
        by_id = {t["id"]: t for t in plan["tasks"]}
        status: dict[str, str] = {tid: "pending" for tid in order}
        results: dict[str, Result] = {}

        while any(s == "pending" for s in status.values()):
            for tid in order:
                if status[tid] == "pending" and any(status[d] in ("failed", "skipped")
                                                    for d in by_id[tid]["depends_on"]):
                    status[tid] = "skipped"
                    self.log(f"[{tid}] skipped: a dependency failed")
            ready = [tid for tid in order if status[tid] == "pending"
                     and all(status[d] == "done" for d in by_id[tid]["depends_on"])]
            if not ready:
                break
            batch = ready[: self.parallel]
            for tid in batch:
                status[tid] = "running"
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                outs = list(pool.map(lambda tid: self._run_task(by_id[tid], goal, results), batch))
            for tid, res in zip(batch, outs):
                results[tid] = res
                status[tid] = "done" if res.ok else "failed"

        summary = self._write_summary(plan, goal, status, results)
        return {"status": status, "results": results, "summary_path": summary}

    # --------------------------------------------------------------- internals
    def _context(self, task: dict, results: dict[str, Result]) -> str:
        if not task["depends_on"]:
            return ""
        per = max(500, self.context_chars // len(task["depends_on"]))
        parts = ["\nReports from the subtasks this one builds on:"]
        for d in task["depends_on"]:
            text = results[d].text if d in results else "(no report)"
            if len(text) > per:
                text = text[:per] + "\n…(truncated)"
            parts.append(f"--- {d} ---\n{text}")
        return "\n".join(parts) + "\n"

    def _run_task(self, task: dict, goal: str, results: dict[str, Result]) -> Result:
        prompt = WORKER_PROMPT.format(goal=goal.strip(), tid=task["id"], title=task["title"],
                                      prompt=task["prompt"], context=self._context(task, results))
        agent, tier = task["agent"], task["tier"]
        tried: set[tuple[str, str]] = set()
        res: Result | None = None
        while (agent, tier) not in tried:
            tried.add((agent, tier))
            if self.ledger.is_cooling(agent) or not self.agents[agent].enabled:
                alt = other_agent(agent)
                if self.only or not self._usable(alt):
                    break
                self.log(f"[{task['id']}] {agent} is on cooldown → {alt}")
                agent = alt
                continue
            label = self.agents[agent].model_label(tier)
            self.log(f"[{task['id']}] → {agent} {tier} ({label}): {task['title']}")
            res = self.agents[agent].run(prompt, tier, self.workdir, timeout=self.timeout)
            self._record(task, res)
            if res.ok:
                self.log(f"[{task['id']}] ✓ done in {res.seconds:.0f}s")
                break
            self.log(f"[{task['id']}] ✗ {agent} {tier}: {res.error.splitlines()[0] if res.error else 'failed'}")
            if res.rate_limited:
                until = self.ledger.set_cooldown(agent, self.cooldown_hours)
                self.log(f"[{task['id']}] {agent} hit its quota; cooling down until "
                         f"{time.strftime('%a %H:%M', time.localtime(until))}")
                alt = other_agent(agent)
                if not self.only and self._usable(alt):
                    agent = alt
                    continue
                break
            if self.escalate and TIERS.index(tier) < len(TIERS) - 1:
                tier = TIERS[TIERS.index(tier) + 1]
                self.log(f"[{task['id']}] escalating to {tier}")
                continue
            break
        if res is None:
            res = Result(False, "", agent, tier, "", error="no available agent")
        (self.run_dir / f"{task['id']}.md").write_text(
            f"# {task['id']}: {task['title']}\n\nagent: {res.agent} / {res.tier} ({res.model})\n"
            f"ok: {res.ok}\n\n## Prompt\n\n{prompt}\n\n## Result\n\n{res.text or res.error}\n",
            encoding="utf-8")
        return res

    def _usable(self, agent: str) -> bool:
        return self.agents[agent].enabled and not self.ledger.is_cooling(agent)

    def _record(self, task: dict, res: Result) -> None:
        self.ledger.record({"run": self.run_dir.name, "task": task["id"], "agent": res.agent,
                            "tier": res.tier, "model": res.model, "ok": res.ok,
                            "rate_limited": res.rate_limited, "seconds": round(res.seconds, 1),
                            "usage": res.usage, "cost_usd": res.cost_usd})

    def _write_summary(self, plan, goal, status, results) -> Path:
        lines = [f"# aihub run {self.run_dir.name}", "", f"**Goal:** {goal.strip()}", ""]
        if plan.get("summary"):
            lines += [f"**Plan:** {plan['summary']}", ""]
        lines += ["| id | task | agent | tier | status | time |", "|---|---|---|---|---|---|"]
        for t in plan["tasks"]:
            r = results.get(t["id"])
            agent = f"{r.agent} ({r.model})" if r else t["agent"]
            tier = r.tier if r else t["tier"]
            secs = f"{r.seconds:.0f}s" if r else "-"
            lines.append(f"| {t['id']} | {t['title']} | {agent} | {tier} | {status[t['id']]} | {secs} |")
        for t in plan["tasks"]:
            r = results.get(t["id"])
            if r:
                lines += ["", f"## {t['id']}: {t['title']}", "", r.text or f"ERROR: {r.error}"]
        path = self.run_dir / "summary.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
