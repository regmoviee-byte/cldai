"""Local web UI: a tiny JSON API over the planner/executor plus a single-page frontend.

Binds to 127.0.0.1 only. Every API call must carry the per-launch token that is embedded in
the served page, and the Host header must be localhost — so other websites open in the same
browser can't drive the agents (CSRF / DNS rebinding).
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__, service
from ..agents import make_agents
from ..config import (AGENTS, DEFAULT_CONFIG_PATH, TIERS, ConfigError, deep_merge, dump_toml,
                      hub_home, load, save_user_config, user_config_path)
from ..executor import Executor, reroute
from ..ledger import Ledger
from ..planner import PlanError

STATIC = Path(__file__).with_name("static")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Job:
    def __init__(self, goal: str, plan: dict, workdir: Path, run_dir: Path):
        self.id = uuid.uuid4().hex[:10]
        self.goal = goal
        self.plan = plan
        self.workdir = workdir
        self.run_dir = run_dir
        self.started = time.time()
        self.finished: float | None = None
        self.state = "running"
        self.log: list[dict] = []
        self.tasks = {t["id"]: {"id": t["id"], "title": t["title"], "agent": t["agent"],
                                "tier": t["tier"], "depends_on": t["depends_on"], "model": "",
                                "status": "pending", "started": None, "seconds": None,
                                "text": "", "error": "", "usage": {}}
                      for t in plan["tasks"]}
        self.executor: Executor | None = None
        self.lock = threading.Lock()

    def add_log(self, msg: str) -> None:
        with self.lock:
            self.log.append({"ts": time.time(), "msg": msg})

    def on_task(self, tid: str, status: str, agent=None, tier=None, model=None, result=None):
        with self.lock:
            t = self.tasks[tid]
            t["status"] = status
            if status == "running":
                t.update(agent=agent, tier=tier, model=model, started=time.time())
            if result is not None:
                t.update(agent=result.agent, tier=result.tier, model=result.model,
                         seconds=round(result.seconds, 1), text=result.text,
                         error=result.error, usage=result.usage)

    def to_dict(self) -> dict:
        with self.lock:
            return {"id": self.id, "goal": self.goal, "summary": self.plan.get("summary", ""),
                    "workdir": str(self.workdir), "run_dir": str(self.run_dir),
                    "started": self.started, "finished": self.finished, "state": self.state,
                    "tasks": [dict(t) for t in self.tasks.values()], "log": list(self.log)}


class App:
    def __init__(self, config_path, workdir: Path):
        self.config_path = config_path
        self.workdir = workdir
        self.token = secrets.token_urlsafe(24)
        self.jobs: dict[str, Job] = {}
        self._doctor: dict | None = None
        self.reload()

    def reload(self) -> None:
        self.cfg = load(self.config_path)
        self.agents = make_agents(self.cfg)
        self.ledger = Ledger(hub_home())
        self._doctor = None

    # ------------------------------------------------------------- helpers
    def _workdir(self, value) -> Path:
        p = Path(value).expanduser() if value else self.workdir
        if not p.is_dir():
            raise ApiError(f"Папка не найдена: {p}")
        return p.resolve()

    @staticmethod
    def _only(value) -> str | None:
        if value in (None, "", "both"):
            return None
        if value not in AGENTS:
            raise ApiError(f"unknown agent {value!r}")
        return value

    # ------------------------------------------------------------ handlers
    def state(self, _q) -> dict:
        cds = self.ledger.cooldowns()
        agents = {}
        for name, agent in self.agents.items():
            agents[name] = {"enabled": agent.enabled, "cooldown_until": cds.get(name),
                            "tiers": {t: agent.model_label(t) for t in TIERS}}
        return {"version": __version__, "workdir": str(self.workdir),
                "config_source": self.cfg["_source"], "planner": self.cfg["planner"]["backend"],
                "prefer": self.cfg["routing"]["prefer"], "agents": agents,
                "running": [j.id for j in self.jobs.values() if j.state == "running"]}

    def doctor(self, q) -> dict:
        if self._doctor is None or q.get("refresh"):
            out = {}
            for name, agent in self.agents.items():
                cmd = agent.cfg.get("command", name)
                exe = (cmd if isinstance(cmd, list) else cmd.split())[0]
                path = shutil.which(exe)
                info = {"installed": bool(path), "path": path, "version": ""}
                if path:
                    try:
                        info["version"] = subprocess.run(
                            [path, "--version"], capture_output=True, text=True, timeout=20
                        ).stdout.strip().splitlines()[0]
                    except (subprocess.SubprocessError, OSError, IndexError):
                        pass
                out[name] = info
            self._doctor = out
        return self._doctor

    def plan(self, body: dict) -> dict:
        task = (body.get("task") or "").strip()
        if not task:
            raise ApiError("Опиши задачу")
        backend = body.get("planner") or self.cfg["planner"]["backend"]
        if backend not in ("manual", "claude", "codex", "heuristic"):
            raise ApiError(f"unknown planner {backend!r}")
        only = self._only(body.get("only"))
        workdir = self._workdir(body.get("workdir"))
        try:
            if backend == "manual":
                return {"manual": True,
                        "prompt": service.planner_prompt(task, self.cfg, self.agents, self.ledger, only)}
            notes: list[str] = []
            plan = service.auto_plan(task, self.cfg, self.agents, self.ledger, workdir, backend,
                                     only, "ui-plan", notes.append)
        except (service.NoAgentError, PlanError) as e:
            raise ApiError(str(e))
        warnings = [f"Планировщик не справился, поэтому задача пойдёт одним шагом. Причина: {n}"
                    for n in notes if "falling back" in n]
        return {"plan": plan, "notes": warnings}

    def parse_plan(self, body: dict) -> dict:
        try:
            plan = service.parse_plan(body.get("text") or "", self.cfg, self.agents, self.ledger,
                                      self._only(body.get("only")))
        except (service.NoAgentError, PlanError) as e:
            raise ApiError(f"Не получилось разобрать ответ: {e}")
        return {"plan": plan}

    def run(self, body: dict) -> dict:
        goal = (body.get("task") or "").strip()
        only = self._only(body.get("only"))
        workdir = self._workdir(body.get("workdir"))
        try:
            plan = service.parse_plan(body.get("plan") or {}, self.cfg, self.agents, self.ledger, only)
            notes = reroute(plan, service.usable_agents(self.cfg, self.agents, self.ledger, only))
        except (service.NoAgentError, PlanError, RuntimeError) as e:
            raise ApiError(str(e))
        run_dir = workdir / ".aihub" / "runs" / time.strftime("%Y%m%d-%H%M%S")
        job = Job(goal or plan.get("summary", ""), plan, workdir, run_dir)
        for n in notes:
            job.add_log(f"reroute: {n}")
        cfg = json.loads(json.dumps(self.cfg))  # snapshot: settings edits don't affect this run
        if body.get("parallel"):
            cfg["execution"]["parallel"] = int(body["parallel"])
        job.executor = Executor(cfg, self.agents, self.ledger, workdir, run_dir, log=job.add_log,
                                only=only, on_task=job.on_task)
        self.jobs[job.id] = job

        def work():
            try:
                out = job.executor.run(plan, job.goal)
                statuses = set(out["status"].values())
                job.state = ("cancelled" if job.executor.cancelled
                             else "done" if statuses == {"done"} else "failed")
            except Exception as e:  # surface anything unexpected in the UI instead of dying silently
                job.add_log(f"internal error: {e!r}")
                job.state = "failed"
            job.finished = time.time()

        threading.Thread(target=work, daemon=True).start()
        return {"job": job.to_dict()}

    def job(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise ApiError("job not found", 404)
        return {"job": self.jobs[job_id].to_dict()}

    def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError("job not found", 404)
        if job.executor and job.state == "running":
            job.add_log("stopping…")
            job.executor.cancel()
        return {"job": job.to_dict()}

    def stats(self, q) -> dict:
        days = float(q.get("days", 7))
        runs = [r for r in self.ledger.runs(10_000) if r.get("ts", 0) >= time.time() - days * 86400]
        return {"days": days, "usage": self.ledger.summary(days), "cooldowns": self.ledger.cooldowns(),
                "runs": len(runs), "runs_ok": sum(1 for r in runs if r.get("ok"))}

    def cooldown(self, body: dict) -> dict:
        agent = body.get("agent")
        if agent not in AGENTS:
            raise ApiError("unknown agent")
        if body.get("clear"):
            self.ledger.clear_cooldown(agent)
        else:
            self.ledger.set_cooldown(agent, float(body.get("hours", 5)))
        return {"cooldowns": self.ledger.cooldowns()}

    def history(self, _q) -> dict:
        return {"runs": self.ledger.runs(100)}

    def history_detail(self, q) -> dict:
        run_dir = q.get("run_dir", "")
        known = {r.get("run_dir") for r in self.ledger.runs(10_000)}
        if run_dir not in known:  # only serve directories aihub itself wrote
            raise ApiError("unknown run", 404)
        d = Path(run_dir)
        plan = json.loads((d / "plan.json").read_text(encoding="utf-8")) if (d / "plan.json").exists() else {}
        reports = {}
        for t in plan.get("tasks", []):
            f = d / f"{t['id']}.md"
            if f.exists():
                text = f.read_text(encoding="utf-8")
                reports[t["id"]] = text.split("## Result", 1)[-1].strip()
        return {"plan": plan, "reports": reports}

    def config_file(self) -> Path:
        """The file settings are read from and written to: --config if given, else the usual lookup."""
        return Path(self.config_path) if self.config_path else user_config_path()

    def settings(self, _q) -> dict:
        path = self.config_file()
        effective = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        return {"path": str(path), "exists": path.exists(),
                "text": path.read_text(encoding="utf-8") if path.exists() else "",
                "defaults": DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), "effective": effective}

    def save_settings(self, body: dict) -> dict:
        try:
            path = self.config_file()
            if "text" in body:
                save_user_config(body["text"], path)
            else:
                current = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                save_user_config(dump_toml(deep_merge(current, body.get("values") or {})), path)
            self.reload()
        except ConfigError as e:
            raise ApiError(str(e))
        return self.settings({})


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    get_routes = {"/api/state": app.state, "/api/doctor": app.doctor, "/api/stats": app.stats,
                  "/api/history": app.history, "/api/history/detail": app.history_detail,
                  "/api/settings": app.settings}
    post_routes = {"/api/plan": app.plan, "/api/plan/parse": app.parse_plan, "/api/run": app.run,
                   "/api/cooldown": app.cooldown, "/api/settings": app.save_settings}

    class Handler(BaseHTTPRequestHandler):
        server_version = "aihub"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=_default).encode(),
                       "application/json; charset=utf-8")

        def _guard(self, api: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Aihub-Token", ""), app.token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad token — reload the page"})
                return False
            return True

        def _dispatch(self, fn, arg) -> None:
            try:
                self._json(HTTPStatus.OK, fn(arg))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                if not self._guard(api=False):
                    return
                html = (STATIC / "index.html").read_text(encoding="utf-8")
                html = html.replace("__AIHUB_TOKEN__", app.token)
                self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
                return
            if url.path == "/favicon.svg":
                self._send(HTTPStatus.OK, (STATIC / "favicon.svg").read_bytes(), "image/svg+xml")
                return
            if not self._guard(api=True):
                return
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path.startswith("/api/jobs/"):
                self._dispatch(app.job, url.path.rsplit("/", 1)[-1])
            elif url.path in get_routes:
                self._dispatch(get_routes[url.path], q)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._guard(api=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if url.path.startswith("/api/jobs/") and url.path.endswith("/cancel"):
                self._dispatch(app.cancel, url.path.split("/")[3])
            elif url.path in post_routes:
                self._dispatch(post_routes[url.path], body)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def _default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o).__name__)


def create_server(config_path, port: int, workdir: Path) -> tuple[ThreadingHTTPServer, App]:
    app = App(config_path, workdir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), None)
    httpd.RequestHandlerClass = make_handler(app, httpd.server_address[1])
    return httpd, app


def serve(config_path=None, port: int = 8765, open_browser: bool = True,
          workdir: Path | None = None) -> int:
    try:
        httpd, _ = create_server(config_path, port, workdir or Path.cwd())
    except ConfigError as e:
        print(f"config error: {e}")
        return 1
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{port}: {e} (try --port)")
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"aihub UI: {url}\nCtrl+C — остановить")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0
