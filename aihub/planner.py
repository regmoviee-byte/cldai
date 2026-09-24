"""Turning a task into a plan: a DAG of subtasks, each routed to an agent + tier."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AGENTS, TIERS


class PlanError(Exception):
    pass


PLANNER_PROMPT = """\
You are a cost-aware router for two coding agents that run on limited weekly subscription quotas:
- "claude" = Claude Code. Tiers: light=Haiku, medium=Sonnet, heavy=Opus.
- "codex"  = OpenAI Codex CLI. Tiers: light=GPT-6 Luna, medium=GPT-6 Sol, heavy=GPT-6 Astra.

Your job: split the TASK into the smallest sensible number of subtasks (1 is fine for small tasks,
rarely more than 6) and give each one the CHEAPEST tier that will still do it right.

Tier guide:
- light  — trivial or mechanical: renames, formatting, boilerplate, config tweaks, docs/comments,
           small single-file edits, running commands and summarizing output, simple questions,
           searching the codebase.
- medium — normal engineering: a feature or bug fix with clear scope, writing tests, moderate
           multi-file changes, code review.
- heavy  — only when really needed: architecture/design decisions, hard debugging with unclear
           cause, concurrency/security-sensitive code, large cross-cutting refactors.
Most subtasks should be light or medium. Heavy is expensive — justify it in "reason".

Agent guide: both can read/edit files and run commands in the repo. Spread load between them
according to the preference and recent usage below; do not send everything to one agent.
Preference: {prefer}
{availability}
Recent usage (last {days} days):
{usage}

Each subtask is executed by a separate agent with NO memory of the others, so its "prompt" must be
self-contained: say exactly what to do, which files/areas, and what "done" means. Later subtasks
automatically receive the reports of the subtasks listed in their "depends_on". If a subtask needs
another's result (docs/tests for a feature, a fix after an investigation), it MUST depend on it;
independent subtasks should not depend on each other.
Don't add a separate "plan"/"analyze" step unless the analysis itself is the heavy part.

Reply with ONLY a JSON object, no prose, no code fences:
{{"summary": "<one line>",
  "tasks": [{{"id": "t1", "title": "<short>", "agent": "claude|codex", "tier": "light|medium|heavy",
             "depends_on": [], "reason": "<why this tier/agent>", "prompt": "<full instructions>"}}]}}

TASK:
{task}
"""


def build_prompt(task: str, *, prefer: str, usage: str, days: int, unavailable: list[str]) -> str:
    availability = ""
    if unavailable:
        availability = ("UNAVAILABLE right now (quota exhausted/disabled) — do not use: "
                        + ", ".join(unavailable))
    return PLANNER_PROMPT.format(task=task.strip(), prefer=prefer, usage=usage, days=days,
                                 availability=availability)


# ------------------------------------------------------------------- parsing
def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise PlanError("planner reply contains no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise PlanError(f"planner reply is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PlanError("planner reply must be a JSON object")
    return data


def normalize(data: dict, default_agent: str = "claude") -> dict:
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise PlanError("plan has no tasks")
    seen: set[str] = set()
    out = []
    for i, t in enumerate(tasks, 1):
        if not isinstance(t, dict):
            raise PlanError(f"task #{i} is not an object")
        tid = str(t.get("id") or f"t{i}")
        if tid in seen:
            tid = f"{tid}_{i}"
        seen.add(tid)
        prompt = str(t.get("prompt") or t.get("title") or "").strip()
        if not prompt:
            raise PlanError(f"task {tid} has no prompt")
        agent = str(t.get("agent", "")).lower()
        tier = str(t.get("tier", "")).lower()
        out.append({
            "id": tid,
            "title": str(t.get("title") or prompt[:60]),
            "agent": agent if agent in AGENTS else default_agent,
            "tier": tier if tier in TIERS else "medium",
            "depends_on": [str(d) for d in (t.get("depends_on") or [])],
            "reason": str(t.get("reason") or ""),
            "prompt": prompt,
        })
    ids = {t["id"] for t in out}
    for t in out:
        t["depends_on"] = [d for d in t["depends_on"] if d in ids and d != t["id"]]
    plan = {"summary": str(data.get("summary") or ""), "tasks": out}
    topo_order(plan)  # raises on cycles
    return plan


def topo_order(plan: dict) -> list[str]:
    deps = {t["id"]: set(t["depends_on"]) for t in plan["tasks"]}
    order: list[str] = []
    while deps:
        ready = [tid for tid, d in deps.items() if not d]
        if not ready:
            raise PlanError("plan has a dependency cycle: " + ", ".join(deps))
        for tid in ready:
            order.append(tid)
            del deps[tid]
        for d in deps.values():
            d.difference_update(ready)
    return order


# ----------------------------------------------------------------- heuristic
HEAVY_WORDS = r"architect|design|redesign|refactor (the )?(whole|entire)|migrat|race condition|deadlock|" \
              r"security|vulnerab|performance|optimi[sz]|concurren|архитект|рефактор|безопасн|" \
              r"уязвим|производительн|оптимиз|гонк|сложн"
LIGHT_WORDS = r"rename|typo|format|lint|comment|docstring|readme|docs?\b|changelog|bump|version|" \
              r"translate|explain|what is|how do|переимен|опечат|формат|коммент|документ|" \
              r"перевед|объясни|что такое|как сделать"


def heuristic_tier(task: str) -> str:
    low = task.lower()
    if re.search(HEAVY_WORDS, low):
        return "heavy"
    if re.search(LIGHT_WORDS, low) or len(task) < 120:
        return "light"
    return "medium"


def heuristic_plan(task: str, agent: str) -> dict:
    tier = heuristic_tier(task)
    return normalize({
        "summary": "single task (heuristic routing)",
        "tasks": [{"id": "t1", "title": task.strip().splitlines()[0][:60], "agent": agent,
                   "tier": tier, "reason": "keyword heuristic", "prompt": task}],
    }, agent)


# -------------------------------------------------------------------- manual
def copy_to_clipboard(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


def manual_plan(prompt: str, run_dir: Path, *, stdin=sys.stdin, out=sys.stderr) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = run_dir / "planner_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    copied = copy_to_clipboard(prompt)
    print("\n" + "=" * 72, file=out)
    print("Вставь этот промпт в чат ChatGPT (лучше новый чат):", file=out)
    print("=" * 72, file=out)
    print(prompt, file=out)
    print("=" * 72, file=out)
    print(f"Промпт {'уже скопирован в буфер обмена и ' if copied else ''}сохранён в {prompt_file}", file=out)
    print("Вставь сюда JSON-ответ ChatGPT (ввод закончится, как только JSON соберётся;"
          " пустая строка + Ctrl-D/Ctrl-Z — выйти):", file=out)
    buf = ""
    for line in stdin:
        buf += line
        if "}" in line:
            try:
                return normalize(extract_json(buf))
            except PlanError:
                continue
    return normalize(extract_json(buf))
