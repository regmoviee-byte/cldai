"""Installing and logging in to the agent CLIs in the background, without console windows."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# Keeps child console programs (claude.exe, codex.cmd, powershell) from flashing a window when
# aihub itself runs without a console (the Windows app).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TITLES = {"claude": "Claude Code", "codex": "Codex CLI"}

# Official install commands. Codex needs Node.js; on Windows we get it with winget first.
INSTALL = {
    "windows": {
        "claude": "irm https://claude.ai/install.ps1 | iex",
        "codex": ("if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { "
                  "winget install -e --id OpenJS.NodeJS.LTS --silent --accept-source-agreements "
                  "--accept-package-agreements; $env:Path = "
                  "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
                  "[Environment]::GetEnvironmentVariable('Path','User') }; "
                  "npm install -g @openai/codex"),
    },
    "unix": {
        "claude": "curl -fsSL https://claude.ai/install.sh | bash",
        "codex": "npm install -g @openai/codex",
    },
}


def os_key() -> str:
    return "windows" if os.name == "nt" else "unix"


def child_env() -> dict:
    env = os.environ.copy()
    if os.name == "nt":
        # Inherited from PowerShell 7 this points Windows PowerShell 5.1 at the wrong modules
        # (e.g. Get-FileHash goes missing and the Claude installer dies).
        env.pop("PSModulePath", None)
    return env


def install_argv(name: str) -> list[str]:
    script = INSTALL[os_key()][name]
    if os.name == "nt":
        # The progress bar makes Invoke-WebRequest many times slower in Windows PowerShell.
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", "$ProgressPreference = 'SilentlyContinue'; " + script]
    return ["sh", "-c", script]


def login_argv(name: str, cmd: list[str]) -> list[str]:
    return cmd + (["auth", "login", "--claudeai"] if name == "claude" else ["login"])


def run_quiet(argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, env=child_env(),
                          encoding="utf-8", errors="replace")


def version(cmd: list[str]) -> str:
    try:
        return run_quiet(cmd + ["--version"]).stdout.strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError, IndexError):
        return ""


def logged_in(name: str, cmd: list[str]) -> bool | None:
    """True/False when the CLI can tell, None when it can't be checked."""
    try:
        if name == "claude":
            out = run_quiet(cmd + ["auth", "status", "--json"])
            return bool(json.loads(out.stdout).get("loggedIn"))
        return run_quiet(cmd + ["login", "status"]).returncode == 0
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return None


class ActionRunner:
    """One background install/login per agent, with its output kept in a log file."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.state: dict[str, dict] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def start(self, name: str, action: str, argv: list[str]) -> dict:
        with self.lock:
            cur = self.state.get(name)
            if cur and cur["state"] == "running":
                return dict(cur)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = self.log_dir / f"{action}-{name}.log"
            f = log.open("w", encoding="utf-8")
            try:
                proc = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                                        env=child_env())
            except OSError as e:
                f.close()
                entry = {"action": action, "state": "failed", "started": time.time(),
                         "finished": time.time(), "log": str(log), "error": str(e)}
                self.state[name] = entry
                return dict(entry)
            entry = {"action": action, "state": "running", "started": time.time(),
                     "finished": None, "log": str(log), "error": ""}
            self.state[name] = entry
            self.procs[name] = proc

        def wait():
            rc = proc.wait()
            f.close()
            with self.lock:
                entry.update(state="done" if rc == 0 else "failed", finished=time.time(),
                             error="" if rc == 0 else f"exit code {rc}")

        threading.Thread(target=wait, daemon=True).start()
        return dict(entry)

    def stop_all(self) -> None:
        with self.lock:
            for proc in self.procs.values():
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass

    def status(self, name: str) -> dict | None:
        with self.lock:
            cur = self.state.get(name)
            if not cur:
                return None
            out = dict(cur)
        try:
            text = Path(out["log"]).read_text(encoding="utf-8", errors="replace")
            out["tail"] = "\n".join(text.strip().splitlines()[-12:])
        except OSError:
            out["tail"] = ""
        return out
