"""Usage ledger and per-agent cooldowns, stored under ~/.aihub."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.usage_path = self.root / "usage.jsonl"
        self.state_path = self.root / "state.json"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ usage
    def record(self, entry: dict) -> None:
        entry = {"ts": time.time(), **entry}
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.usage_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self, days: float) -> list[dict]:
        if not self.usage_path.exists():
            return []
        since = time.time() - days * 86400
        out = []
        for line in self.usage_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) >= since:
                out.append(e)
        return out

    def summary(self, days: float) -> dict:
        """{agent: {tier: {calls, failed, input_tokens, output_tokens, cost_usd, seconds}}}"""
        agg: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for e in self.entries(days):
            s = agg[e.get("agent", "?")][e.get("tier", "?")]
            s["calls"] += 1
            s["failed"] += 0 if e.get("ok") else 1
            usage = e.get("usage") or {}
            s["input_tokens"] += usage.get("input_tokens", 0) or 0
            s["output_tokens"] += usage.get("output_tokens", 0) or 0
            s["cost_usd"] += e.get("cost_usd", 0) or 0
            s["seconds"] += e.get("seconds", 0) or 0
        return {a: {t: dict(v) for t, v in tiers.items()} for a, tiers in agg.items()}

    def summary_text(self, days: float) -> str:
        data = self.summary(days)
        if not data:
            return "no usage recorded yet"
        lines = []
        for agent in sorted(data):
            parts = []
            for tier in ("light", "medium", "heavy"):
                if tier in data[agent]:
                    s = data[agent][tier]
                    parts.append(f"{tier}: {int(s['calls'])} calls, "
                                 f"{int(s['input_tokens'] + s['output_tokens'])} tokens")
            lines.append(f"{agent} — " + "; ".join(parts))
        return "\n".join(lines)

    # ---------------------------------------------------------------- history
    def record_run(self, entry: dict) -> None:
        entry = {"ts": time.time(), **entry}
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "runs.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def runs(self, limit: int = 50) -> list[dict]:
        path = self.root / "runs.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[::-1][:limit]

    # -------------------------------------------------------------- cooldowns
    def _state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def cooldowns(self) -> dict[str, float]:
        now = time.time()
        return {a: until for a, until in self._state().get("cooldown", {}).items() if until > now}

    def set_cooldown(self, agent: str, hours: float) -> float:
        until = time.time() + hours * 3600
        with self._lock:
            state = self._state()
            state.setdefault("cooldown", {})[agent] = until
            self._save_state(state)
        return until

    def clear_cooldown(self, agent: str | None = None) -> None:
        with self._lock:
            state = self._state()
            cd = state.get("cooldown", {})
            if agent is None:
                cd.clear()
            else:
                cd.pop(agent, None)
            state["cooldown"] = cd
            self._save_state(state)

    def is_cooling(self, agent: str) -> bool:
        return agent in self.cooldowns()
