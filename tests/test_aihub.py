import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aihub import cli, config
from aihub.agents import make_agents
from aihub.executor import Executor
from aihub.ledger import Ledger
from aihub.planner import PlanError, extract_json, heuristic_tier, manual_plan, normalize, topo_order

FAKES = Path(__file__).parent / "fakes"

PLAN = {
    "summary": "two steps",
    "tasks": [
        {"id": "t1", "title": "rename things", "agent": "codex", "tier": "light", "prompt": "rename foo"},
        {"id": "t2", "title": "write tests", "agent": "claude", "tier": "medium",
         "depends_on": ["t1"], "prompt": "write tests for foo"},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log_file = self.tmp / "calls.jsonl"
        self.env = mock.patch.dict(os.environ, {
            "AIHUB_HOME": str(self.tmp / "home"),
            "FAKE_LOG": str(self.log_file),
            "FAKE_PLAN": json.dumps(PLAN),
        })
        self.env.start()
        for k in ("FAKE_CLAUDE_LIMIT", "FAKE_CODEX_LIMIT", "FAKE_CLAUDE_FAIL_MODEL"):
            os.environ.pop(k, None)
        cfg_path = self.tmp / "aihub.toml"
        cfg_path.write_text(f"""
[planner]
backend = "claude"
[execution]
confirm = false
[agents.claude]
command = {json.dumps([sys.executable, str(FAKES / "fake_claude.py")])}
[agents.codex]
command = {json.dumps([sys.executable, str(FAKES / "fake_codex.py")])}
""")
        self.cfg_path = cfg_path
        self.cfg = config.load(cfg_path)

    def tearDown(self):
        self.env.stop()

    def calls(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def executor(self, only=None):
        agents = make_agents(self.cfg)
        ledger = Ledger(config.hub_home())
        return Executor(self.cfg, agents, ledger, self.tmp, self.tmp / "run", log=lambda m: None,
                        only=only), ledger


class PlannerTests(unittest.TestCase):
    def test_extract_json_from_fenced_reply(self):
        reply = 'Sure!\n```json\n{"tasks": [{"prompt": "x"}]}\n```\nbye'
        self.assertEqual(extract_json(reply)["tasks"][0]["prompt"], "x")

    def test_normalize_fixes_bad_values_and_dangling_deps(self):
        plan = normalize({"tasks": [
            {"id": "a", "agent": "gemini", "tier": "ultra", "prompt": "p", "depends_on": ["zzz", "a"]},
        ]})
        t = plan["tasks"][0]
        self.assertEqual((t["agent"], t["tier"], t["depends_on"]), ("claude", "medium", []))

    def test_cycle_detected(self):
        with self.assertRaises(PlanError):
            normalize({"tasks": [{"id": "a", "prompt": "p", "depends_on": ["b"]},
                                 {"id": "b", "prompt": "p", "depends_on": ["a"]}]})

    def test_topo_order(self):
        self.assertEqual(topo_order(normalize(PLAN)), ["t1", "t2"])

    def test_heuristic_tier(self):
        self.assertEqual(heuristic_tier("fix a typo in README"), "light")
        self.assertEqual(heuristic_tier("спроектируй архитектуру сервиса очередей"), "heavy")
        self.assertEqual(heuristic_tier("add a /users endpoint with pagination, filtering by role "
                                        "and sorting, plus wire it into the existing router module "
                                        "and update the client"), "medium")

    def test_manual_plan_reads_pasted_json(self):
        pasted = io.StringIO("```json\n" + json.dumps(PLAN, indent=2) + "\n```\n")
        with tempfile.TemporaryDirectory() as d, mock.patch("aihub.planner.copy_to_clipboard", return_value=False):
            plan = manual_plan("PROMPT", Path(d), stdin=pasted, out=io.StringIO())
            self.assertTrue((Path(d) / "planner_prompt.txt").exists())
        self.assertEqual([t["id"] for t in plan["tasks"]], ["t1", "t2"])


class LookupTests(unittest.TestCase):
    def test_finds_cli_in_known_location_off_path(self):
        from aihub import agents
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            with mock.patch.object(Path, "home", return_value=Path(d)), \
                 mock.patch.dict(os.environ, {"PATH": ""}):
                self.assertEqual(agents.find_executable("claude"), str(exe))
                self.assertIsNone(agents.find_executable("codex"))


class AgentTests(Base):
    def test_claude_command_and_usage(self):
        res = make_agents(self.cfg)["claude"].run("hello", "light", self.tmp)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.usage, {"input_tokens": 100, "output_tokens": 5})
        args = self.calls()[0]["args"]
        self.assertIn("haiku", args)
        self.assertEqual(args[args.index("--effort") + 1], "low")
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")

    def test_codex_command_and_output(self):
        res = make_agents(self.cfg)["codex"].run("hello", "heavy", self.tmp, text_only=True)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.text.startswith("codex did"))
        args = self.calls()[0]["args"]
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertIn("model_reasoning_effort=high", args)
        self.assertEqual(args[args.index("-m") + 1], "gpt-6-astra")

    def test_rate_limit_detected(self):
        os.environ["FAKE_CODEX_LIMIT"] = "1"
        res = make_agents(self.cfg)["codex"].run("hello", "light", self.tmp)
        self.assertFalse(res.ok)
        self.assertTrue(res.rate_limited)

    def test_task_text_mentioning_quota_is_not_a_limit(self):
        res = make_agents(self.cfg)["claude"].run("implement a quota / rate limit middleware", "light", self.tmp)
        self.assertTrue(res.ok)
        self.assertFalse(res.rate_limited)


class ExecutorTests(Base):
    def test_runs_in_order_and_passes_context(self):
        ex, ledger = self.executor()
        out = ex.run(normalize(PLAN), "goal")
        self.assertEqual(out["status"], {"t1": "done", "t2": "done"})
        calls = self.calls()
        self.assertEqual([c["agent"] for c in calls], ["codex", "claude"])
        self.assertIn("codex did", calls[1]["prompt"])  # t1's report reached t2
        self.assertEqual(ledger.summary(1)["claude"]["medium"]["calls"], 1)
        self.assertTrue((self.tmp / "run" / "summary.md").exists())

    def test_failover_to_other_agent_on_quota(self):
        os.environ["FAKE_CLAUDE_LIMIT"] = "1"
        ex, ledger = self.executor()
        out = ex.run(normalize(PLAN), "goal")
        self.assertEqual(out["status"]["t2"], "done")
        self.assertEqual(out["results"]["t2"].agent, "codex")
        self.assertTrue(ledger.is_cooling("claude"))

    def test_escalates_tier_on_failure(self):
        os.environ["FAKE_CLAUDE_FAIL_MODEL"] = "sonnet"
        ex, _ = self.executor()
        out = ex.run(normalize(PLAN), "goal")
        self.assertEqual(out["results"]["t2"].tier, "heavy")
        self.assertEqual(out["status"]["t2"], "done")

    def test_dependents_skipped_when_dependency_fails(self):
        os.environ["FAKE_CODEX_LIMIT"] = "1"
        os.environ["FAKE_CLAUDE_LIMIT"] = "1"
        ex, _ = self.executor()
        out = ex.run(normalize(PLAN), "goal")
        self.assertEqual(out["status"], {"t1": "failed", "t2": "skipped"})

    def test_only_flag_prevents_failover(self):
        os.environ["FAKE_CODEX_LIMIT"] = "1"
        ex, _ = self.executor(only="codex")
        out = ex.run(normalize({"tasks": [PLAN["tasks"][0]]}), "goal")
        self.assertEqual(out["status"]["t1"], "failed")
        self.assertNotIn("claude", [c["agent"] for c in self.calls()])


class CliTests(Base):
    def run_cli(self, *argv):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = cli.main(["--config", str(self.cfg_path), *argv])
        return rc, err.getvalue()

    def test_run_end_to_end_with_claude_planner(self):
        rc, err = self.run_cli("run", "-C", str(self.tmp), "do the thing")
        self.assertEqual(rc, 0, err)
        agents = [c["agent"] for c in self.calls()]
        self.assertEqual(agents, ["claude", "codex", "claude"])  # plan, t1, t2
        plan_args = self.calls()[0]["args"]
        self.assertEqual(plan_args[plan_args.index("--tools") + 1], "")  # planner runs tool-less

    def test_planner_garbage_falls_back_to_heuristic(self):
        os.environ["FAKE_PLAN"] = "no json here"
        rc, err = self.run_cli("run", "-C", str(self.tmp), "--dry-run", "fix typo")
        self.assertEqual(rc, 0)
        self.assertIn("falling back to heuristic", err)

    def test_plan_then_exec(self):
        out = self.tmp / "p.json"
        rc, _ = self.run_cli("plan", "-C", str(self.tmp), "-o", str(out), "task")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.read_text())["goal"], "task")
        rc, err = self.run_cli("exec", "-C", str(self.tmp), "-y", str(out))
        self.assertEqual(rc, 0, err)

    def test_cooldown_reroutes_plan(self):
        Ledger(config.hub_home()).set_cooldown("codex", 1)
        rc, err = self.run_cli("run", "-C", str(self.tmp), "task")
        self.assertEqual(rc, 0, err)
        self.assertIn("t1: codex unavailable → claude", err)
        self.assertNotIn("codex", [c["agent"] for c in self.calls()])


class WebTests(Base):
    def setUp(self):
        super().setUp()
        import threading
        from aihub.web.server import create_server
        self.httpd, self.app = create_server(self.cfg_path, 0, self.tmp)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, path, body=None, token=True, host=None):
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Aihub-Token"] = self.app.token
        if host:
            headers["Host"] = host
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                   data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if path.startswith("/api") else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def wait_job(self, job_id):
        import time
        for _ in range(100):
            status, data = self.req(f"/api/jobs/{job_id}")
            if data["job"]["state"] != "running":
                return data["job"]
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_page_has_token_and_api_requires_it(self):
        status, html = self.req("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, html)
        self.assertEqual(self.req("/api/state", token=False)[0], 403)
        self.assertEqual(self.req("/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("/", token=False, host="evil.example")[0], 403)

    def test_plan_run_and_history(self):
        status, data = self.req("/api/plan", {"task": "do it", "planner": "claude", "workdir": str(self.tmp)})
        self.assertEqual(status, 200, data)
        plan = data["plan"]
        plan["tasks"][1]["tier"] = "light"  # user edit in the UI
        status, data = self.req("/api/run", {"task": "do it", "plan": plan, "workdir": str(self.tmp)})
        self.assertEqual(status, 200, data)
        job = self.wait_job(data["job"]["id"])
        self.assertEqual(job["state"], "done")
        self.assertEqual([t["status"] for t in job["tasks"]], ["done", "done"])
        self.assertIn("haiku", job["tasks"][1]["model"])
        runs = self.req("/api/history")[1]["runs"]
        self.assertEqual(runs[0]["goal"], "do it")
        detail = self.req("/api/history/detail?run_dir=" + runs[0]["run_dir"])[1]
        self.assertIn("claude(haiku) did", detail["reports"]["t2"])
        self.assertEqual(self.req("/api/history/detail?run_dir=/etc")[0], 404)
        stats = self.req("/api/stats?days=1")[1]
        self.assertEqual(stats["runs"], 1)

    def test_manual_planner_flow(self):
        status, data = self.req("/api/plan", {"task": "x", "planner": "manual"})
        self.assertTrue(data["manual"])
        self.assertIn("cost-aware router", data["prompt"])
        status, data = self.req("/api/plan/parse", {"text": "```json\n" + json.dumps(PLAN) + "\n```"})
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["plan"]["tasks"]), 2)
        self.assertEqual(self.req("/api/plan/parse", {"text": "nope"})[0], 400)

    def test_bad_workdir_rejected(self):
        status, body = self.req("/api/plan", {"task": "x", "planner": "heuristic", "workdir": "/no/such/dir"})
        self.assertEqual(status, 400)

    def test_cancel_stops_job(self):
        os.environ["FAKE_SLEEP"] = "5"
        status, data = self.req("/api/run", {"task": "t", "plan": PLAN, "workdir": str(self.tmp)})
        import time
        time.sleep(0.5)
        self.req(f"/api/jobs/{data['job']['id']}/cancel", {})
        job = self.wait_job(data["job"]["id"])
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual([t["status"] for t in job["tasks"]], ["cancelled", "cancelled"])

    def test_agent_actions(self):
        status, data = self.req("/api/agent-action", {"agent": "codex", "action": "install"})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["launched"])  # not Windows: UI shows the command instead
        self.assertIn("npm install -g @openai/codex", data["command"])
        status, data = self.req("/api/agent-action", {"agent": "claude", "action": "login"})
        self.assertIn("fake_claude.py", data["command"])
        self.assertEqual(self.req("/api/agent-action", {"agent": "claude", "action": "rm -rf"})[0], 400)
        doctor = self.req("/api/doctor?refresh=1")[1]
        self.assertTrue(doctor["claude"]["installed"])
        self.assertIn("install_command", doctor["codex"])

    def test_second_launch_detects_running_instance(self):
        from aihub.web.server import already_running
        self.assertTrue(already_running(self.port))

    def test_settings_roundtrip(self):
        status, data = self.req("/api/settings", {"values": {"routing": {"prefer": "codex"},
                                                             "execution": {"parallel": 2}}})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.app.cfg["routing"]["prefer"], "codex")
        self.assertEqual(self.app.cfg["execution"]["parallel"], 2)
        # fake agent commands from the test config survived the rewrite
        self.assertIn("fake_claude.py", str(self.app.cfg["agents"]["claude"]["command"]))
        self.assertEqual(self.req("/api/settings", {"text": "[planner]\nbackend = 'nope'"})[0], 400)


if __name__ == "__main__":
    unittest.main()
