"""Command-line interface: aihub run | plan | exec | ui | stats | cooldown | init | doctor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .agents import make_agents
from . import service
from .config import AGENTS, DEFAULT_CONFIG_PATH, TIERS, ConfigError, hub_home, load
from .executor import Executor, available_agents, reroute
from .ledger import Ledger
from .planner import PlanError, manual_plan, normalize


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def read_task(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.task:
        return " ".join(args.task)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("no task given: aihub run \"...\" or --file task.md")


def new_run_dir(workdir: Path) -> Path:
    return workdir / ".aihub" / "runs" / time.strftime("%Y%m%d-%H%M%S")


def make_plan(task: str, cfg: dict, agents, ledger: Ledger, run_dir: Path, workdir: Path,
              backend: str, only: str | None) -> dict:
    try:
        if backend == "manual":
            prompt = service.planner_prompt(task, cfg, agents, ledger, only)
            try:
                return service.parse_plan(manual_plan(prompt, run_dir), cfg, agents, ledger, only)
            except PlanError as e:
                if not cfg["planner"].get("fallback_to_heuristic", True):
                    raise
                log(f"{e} — falling back to heuristic routing")
                backend = "heuristic"
        return service.auto_plan(task, cfg, agents, ledger, workdir, backend, only, run_dir.name, log)
    except service.NoAgentError as e:
        raise SystemExit(f"{e}. Check `aihub cooldown` / config.")
    except PlanError as e:
        raise SystemExit(str(e))


def print_plan(plan: dict, agents) -> None:
    if plan.get("summary"):
        log(f"\nPlan: {plan['summary']}")
    log("")
    for t in plan["tasks"]:
        deps = f"  (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        label = agents[t["agent"]].model_label(t["tier"])
        log(f"  {t['id']:<4} {t['agent']:<6} {t['tier']:<6} {label:<18} {t['title']}{deps}")
        if t.get("reason"):
            log(f"       ↳ {t['reason']}")
    log("")


def confirm(plan_path: Path) -> str:
    """Returns 'y', 'n' or 'e'."""
    try:
        ans = input("Запускать? [Y]es / [n]o / [e]dit plan: ").strip().lower()
    except EOFError:
        return "n"
    return ans[:1] if ans[:1] in ("n", "e") else "y"


def edit_plan(plan: dict, path: Path) -> dict:
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    subprocess.run([*editor.split(), str(path)])
    return normalize(json.loads(path.read_text(encoding="utf-8")))


def execute(plan: dict, task: str, cfg: dict, agents, ledger: Ledger, workdir: Path,
            run_dir: Path, args) -> int:
    for note in reroute(plan, available_agents(agents, ledger, args.only)):
        log(f"reroute: {note}")
    print_plan(plan, agents)
    if args.dry_run:
        log("--dry-run: not executing")
        return 0
    need_confirm = cfg["execution"].get("confirm", True) and not args.yes
    while need_confirm:
        choice = confirm(run_dir / "plan.json")
        if choice == "n":
            run_dir.mkdir(parents=True, exist_ok=True)
            path = run_dir / "plan.json"
            path.write_text(json.dumps({"goal": task, **plan}, indent=2, ensure_ascii=False),
                            encoding="utf-8")
            log(f"aborted. Plan saved: {path}  (run later: aihub exec {path})")
            return 1
        if choice == "e":
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = edit_plan(plan, run_dir / "plan.edit.json")
            reroute(plan, available_agents(agents, ledger, args.only))
            print_plan(plan, agents)
            continue
        break
    if args.parallel:
        cfg["execution"]["parallel"] = args.parallel
    ex = Executor(cfg, agents, ledger, workdir, run_dir, log=log, only=args.only)
    out = ex.run(plan, task)
    failed = [tid for tid, s in out["status"].items() if s != "done"]
    log(f"\nsummary: {out['summary_path']}")
    if failed:
        log(f"not done: {', '.join(failed)}")
    return 1 if failed else 0


# ------------------------------------------------------------------ commands
def setup(args):
    try:
        cfg = load(args.config)
    except ConfigError as e:
        raise SystemExit(f"config error: {e}")
    workdir = Path(args.workdir).resolve() if getattr(args, "workdir", None) else Path.cwd()
    return cfg, make_agents(cfg), Ledger(hub_home()), workdir


def cmd_run(args) -> int:
    cfg, agents, ledger, workdir = setup(args)
    task = read_task(args)
    run_dir = new_run_dir(workdir)
    backend = args.planner or cfg["planner"]["backend"]
    if args.no_plan:
        backend = "heuristic"
    plan = make_plan(task, cfg, agents, ledger, run_dir, workdir, backend, args.only)
    if args.tier:
        for t in plan["tasks"]:
            t["tier"] = args.tier
    return execute(plan, task, cfg, agents, ledger, workdir, run_dir, args)


def cmd_plan(args) -> int:
    cfg, agents, ledger, workdir = setup(args)
    task = read_task(args)
    run_dir = new_run_dir(workdir)
    plan = make_plan(task, cfg, agents, ledger, run_dir, workdir,
                     args.planner or cfg["planner"]["backend"], args.only)
    print_plan(plan, agents)
    out = Path(args.output) if args.output else run_dir / "plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"goal": task, **plan}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"plan saved: {out}\nedit it if you like, then: aihub exec {out}")
    return 0


def cmd_exec(args) -> int:
    cfg, agents, ledger, workdir = setup(args)
    raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan = normalize(raw)
    task = raw.get("goal") or plan.get("summary") or ""
    return execute(plan, task, cfg, agents, ledger, workdir, new_run_dir(workdir), args)


def cmd_stats(args) -> int:
    ledger = Ledger(hub_home())
    data = ledger.summary(args.days)
    if not data:
        print("no usage recorded yet")
    for agent in sorted(data):
        print(f"\n{agent}")
        total = 0
        for tier in (*TIERS, *sorted(set(data[agent]) - set(TIERS))):
            s = data[agent].get(tier)
            if not s:
                continue
            total += s["calls"]
            cost = f"  ~${s['cost_usd']:.2f} API-equivalent" if s["cost_usd"] else ""
            print(f"  {tier:<7} {int(s['calls']):>4} calls  {int(s['failed']):>3} failed  "
                  f"{int(s['input_tokens']):>10} in  {int(s['output_tokens']):>8} out  "
                  f"{s['seconds'] / 60:>6.1f} min{cost}")
        print(f"  total   {int(total):>4} calls")
    cds = ledger.cooldowns()
    for agent, until in cds.items():
        print(f"\n{agent}: cooling down until {time.strftime('%a %d %b %H:%M', time.localtime(until))}")
    return 0


def cmd_cooldown(args) -> int:
    ledger = Ledger(hub_home())
    if args.clear:
        ledger.clear_cooldown(args.agent)
        print(f"cooldown cleared for {args.agent or 'all agents'}")
    elif args.agent:
        until = ledger.set_cooldown(args.agent, args.hours)
        print(f"{args.agent} off until {time.strftime('%a %d %b %H:%M', time.localtime(until))}")
    else:
        cds = ledger.cooldowns()
        if not cds:
            print("no agent on cooldown")
        for agent, until in cds.items():
            print(f"{agent}: until {time.strftime('%a %d %b %H:%M', time.localtime(until))}")
    return 0


def cmd_init(args) -> int:
    target = Path("aihub.toml") if args.local else hub_home() / "config.toml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG_PATH, target)
    print(f"config written: {target}")
    return 0


def cmd_doctor(args) -> int:
    cfg, agents, ledger, _ = setup(args)
    print(f"config: {cfg['_source']}\nplanner: {cfg['planner']['backend']}\ndata: {hub_home()}")
    rc = 0
    for name, agent in agents.items():
        cmd = agent.cfg.get("command", name)
        exe = (cmd if isinstance(cmd, list) else cmd.split())[0]
        path = shutil.which(exe)
        if not agent.enabled:
            print(f"{name}: disabled in config")
            continue
        if not path:
            print(f"{name}: ✗ `{exe}` not found in PATH")
            rc = 1
            continue
        try:
            ver = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
        except (subprocess.SubprocessError, OSError) as e:
            ver = f"error: {e}"
        tiers = ", ".join(f"{t}={agent.model_label(t)}" for t in TIERS)
        cooling = " (on cooldown)" if ledger.is_cooling(name) else ""
        print(f"{name}: ✓ {ver}{cooling}\n  tiers: {tiers}")
    return rc


def cmd_ui(args) -> int:
    from .web.server import serve
    return serve(args.config, port=args.port, open_browser=not args.no_browser,
                 workdir=Path(args.workdir).resolve() if args.workdir else Path.cwd())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aihub", description="Route coding tasks between Claude Code "
                                "and Codex on the cheapest model that can do the job.")
    p.add_argument("--version", action="version", version=f"aihub {__version__}")
    p.add_argument("--config", help="path to config TOML")
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_args(sp):
        sp.add_argument("task", nargs="*", help="task text (or --file, or stdin)")
        sp.add_argument("-f", "--file", help="read task from file")
        sp.add_argument("--planner", choices=["manual", "codex", "claude", "heuristic"])
        sp.add_argument("--only", choices=AGENTS, help="use only this agent")
        sp.add_argument("-C", "--workdir", help="project directory (default: cwd)")

    def exec_args(sp):
        sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
        sp.add_argument("--dry-run", action="store_true", help="show the plan, don't run")
        sp.add_argument("-j", "--parallel", type=int, help="subtasks to run at once")

    sp = sub.add_parser("run", help="plan a task and execute it")
    task_args(sp)
    exec_args(sp)
    sp.add_argument("--no-plan", action="store_true", help="skip the planner, one subtask")
    sp.add_argument("--tier", choices=TIERS, help="force a tier for every subtask")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("plan", help="only make a plan and save it as JSON")
    task_args(sp)
    sp.add_argument("-o", "--output", help="where to save plan JSON")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("exec", help="execute a saved (possibly hand-edited) plan JSON")
    sp.add_argument("plan")
    sp.add_argument("--only", choices=AGENTS)
    sp.add_argument("-C", "--workdir")
    exec_args(sp)
    sp.set_defaults(func=cmd_exec)

    sp = sub.add_parser("stats", help="usage per agent and tier")
    sp.add_argument("--days", type=float, default=7)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("cooldown", help="show/set/clear 'quota exhausted' marks")
    sp.add_argument("agent", nargs="?", choices=AGENTS)
    sp.add_argument("--hours", type=float, default=5)
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_cooldown)

    sp = sub.add_parser("init", help="write a config file to edit")
    sp.add_argument("--local", action="store_true", help="./aihub.toml instead of ~/.aihub/config.toml")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("ui", help="open the web interface in your browser")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("-C", "--workdir", help="default project directory")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("doctor", help="check that claude/codex CLIs are installed")
    sp.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 130
