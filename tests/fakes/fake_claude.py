"""Stand-in for `claude -p --output-format json` used by the tests.

Behaviour is driven by env vars:
  FAKE_CLAUDE_LIMIT=1         -> reply with a usage-limit error
  FAKE_CLAUDE_FAIL_MODEL=x    -> fail (non-limit) when --model x
  FAKE_LOG=path               -> append the argv as a JSON line
"""
import json
import os
import sys

args = sys.argv[1:]
prompt = sys.stdin.read()
model = args[args.index("--model") + 1] if "--model" in args else ""

if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"agent": "claude", "args": args, "prompt": prompt}) + "\n")

if os.environ.get("FAKE_CLAUDE_LIMIT"):
    print(json.dumps({"type": "result", "is_error": True,
                      "result": "Claude AI usage limit reached|1758700000"}))
    sys.exit(1)

if model and model == os.environ.get("FAKE_CLAUDE_FAIL_MODEL"):
    print(json.dumps({"type": "result", "is_error": True, "result": "could not finish the task"}))
    sys.exit(1)

if "cost-aware router" in prompt:
    result = os.environ.get("FAKE_PLAN", "{}")
else:
    result = f"claude({model}) did: " + prompt.split("Your subtask", 1)[-1][:80].strip()

print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result,
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
                  "total_cost_usd": 0.01}))
