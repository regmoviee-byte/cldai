"""Thin wrappers that run Claude Code and Codex CLIs headlessly."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Only consulted when a run has already failed, so a task *about* rate limits
# does not trip it.
RATE_LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|limit reached|hit your limit|quota|too many requests|\b429\b"
    r"|out of (credits|messages)|try again (at|in)",
    re.IGNORECASE,
)


@dataclass
class Result:
    ok: bool
    text: str
    agent: str
    tier: str
    model: str
    seconds: float = 0.0
    rate_limited: bool = False
    error: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


def _command(value) -> list[str]:
    return list(value) if isinstance(value, list) else shlex.split(value)


class Agent:
    name = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def tier_cfg(self, tier: str) -> dict:
        return self.cfg["tiers"][tier]

    def model_label(self, tier: str) -> str:
        t = self.tier_cfg(tier)
        model = t.get("model") or "default"
        return f"{model}/{t['effort']}" if t.get("effort") else model

    def build_cmd(self, tier: str, workdir: Path, text_only: bool, out_file: Path) -> list[str]:
        raise NotImplementedError

    def parse(self, proc: subprocess.CompletedProcess, out_file: Path) -> tuple[bool, str, dict, float, str]:
        """-> (ok, text, usage, cost_usd, error)"""
        raise NotImplementedError

    def run(self, prompt: str, tier: str, workdir: Path, *, text_only: bool = False,
            timeout: float | None = None) -> Result:
        with tempfile.TemporaryDirectory(prefix="aihub-") as tmp:
            out_file = Path(tmp) / "last_message.txt"
            cmd = self.build_cmd(tier, workdir, text_only, out_file)
            start = time.monotonic()
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      cwd=workdir, timeout=timeout, encoding="utf-8",
                                      errors="replace", env=os.environ.copy())
            except FileNotFoundError:
                return Result(False, "", self.name, tier, self.model_label(tier),
                              error=f"command not found: {cmd[0]}")
            except subprocess.TimeoutExpired:
                return Result(False, "", self.name, tier, self.model_label(tier),
                              seconds=time.monotonic() - start, error=f"timeout after {timeout}s")
            seconds = time.monotonic() - start
            ok, text, usage, cost, error = self.parse(proc, out_file)
        if not ok and not error:
            error = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-2000:]
        rate_limited = not ok and bool(RATE_LIMIT_RE.search(f"{error}\n{proc.stderr}"))
        return Result(ok, text, self.name, tier, self.model_label(tier), seconds,
                      rate_limited, error, usage, cost)


class ClaudeAgent(Agent):
    name = "claude"

    def build_cmd(self, tier, workdir, text_only, out_file):
        t = self.tier_cfg(tier)
        cmd = _command(self.cfg.get("command", "claude"))
        cmd += ["-p", "--output-format", "json"]
        if t.get("model"):
            cmd += ["--model", t["model"]]
        if t.get("effort"):
            cmd += ["--effort", t["effort"]]
        if text_only:
            # No tools, no CLAUDE.md/settings/MCP and a one-line system prompt: ~1k input tokens
            # per call instead of ~30k for a full Claude Code session.
            cmd += ["--tools", "", "--system-prompt", "You are a precise assistant. Follow the "
                    "user's output format exactly.", "--strict-mcp-config", "--setting-sources", ""]
        else:
            cmd += ["--permission-mode", self.cfg.get("permission_mode", "acceptEdits")]
            if self.cfg.get("allowed_tools"):
                cmd += ["--allowedTools", ",".join(self.cfg["allowed_tools"])]
        cmd += list(self.cfg.get("extra_args", []))
        return cmd

    def parse(self, proc, out_file):
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            ok = proc.returncode == 0 and bool(proc.stdout.strip())
            return ok, proc.stdout.strip(), {}, 0.0, "" if ok else proc.stderr.strip()
        if isinstance(data, list):  # stream of messages: take the final result
            data = next((m for m in reversed(data) if m.get("type") == "result"), data[-1] if data else {})
        text = str(data.get("result") or "")
        is_error = bool(data.get("is_error")) or proc.returncode != 0
        usage = data.get("usage") or {}
        usage = {
            "input_tokens": (usage.get("input_tokens", 0) or 0)
            + (usage.get("cache_creation_input_tokens", 0) or 0)
            + (usage.get("cache_read_input_tokens", 0) or 0),
            "output_tokens": usage.get("output_tokens", 0) or 0,
        }
        cost = float(data.get("total_cost_usd") or 0)
        error = (text or proc.stderr.strip() or str(data.get("subtype", ""))) if is_error else ""
        return not is_error, text, usage, cost, error


class CodexAgent(Agent):
    name = "codex"

    def build_cmd(self, tier, workdir, text_only, out_file):
        t = self.tier_cfg(tier)
        cmd = _command(self.cfg.get("command", "codex"))
        sandbox = "read-only" if text_only else self.cfg.get("sandbox", "workspace-write")
        cmd += ["exec", "--json", "--skip-git-repo-check", "-C", str(workdir),
                "-s", sandbox, "-o", str(out_file)]
        if t.get("model"):
            cmd += ["-m", t["model"]]
        if t.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{t["effort"]}"']
        cmd += list(self.cfg.get("extra_args", []))
        cmd.append("-")  # prompt from stdin
        return cmd

    def parse(self, proc, out_file):
        usage = {"input_tokens": 0, "output_tokens": 0}
        last_msg, errors, failed = "", [], False
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            if isinstance(ev.get("usage"), dict):
                usage["input_tokens"] += ev["usage"].get("input_tokens", 0) or 0
                usage["output_tokens"] += ev["usage"].get("output_tokens", 0) or 0
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = item["text"]
            if etype == "error" or etype.endswith(".failed"):
                msg = ev.get("message") or (ev.get("error") or {}).get("message") or line
                errors.append(str(msg))
                failed = failed or etype.endswith(".failed")
        text = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""
        text = text or last_msg
        # Transient "error" events (e.g. reconnects) don't fail a run that exits 0.
        ok = proc.returncode == 0 and not failed
        return ok, text, usage, 0.0, "\n".join(errors)


def make_agents(cfg: dict) -> dict[str, Agent]:
    return {"claude": ClaudeAgent(cfg["agents"]["claude"]),
            "codex": CodexAgent(cfg["agents"]["codex"])}
