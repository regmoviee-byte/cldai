"""Stand-in for `codex exec --json ... -o FILE -` used by the tests.

  FAKE_CODEX_LIMIT=1  -> emit a usage-limit failure
  FAKE_LOG=path       -> append the argv as a JSON line
"""
import json
import os
import sys

args = sys.argv[1:]
prompt = sys.stdin.read()
out_file = args[args.index("-o") + 1]

if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"agent": "codex", "args": args, "prompt": prompt}) + "\n")

print(json.dumps({"type": "thread.started", "thread_id": "x"}))
print(json.dumps({"type": "turn.started"}))

if os.environ.get("FAKE_CODEX_LIMIT"):
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again in 3 days."}))
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit"}}))
    sys.exit(1)

if "cost-aware router" in prompt:
    text = os.environ.get("FAKE_PLAN", "{}")
else:
    text = "codex did: " + prompt.split("Your subtask", 1)[-1][:80].strip()

print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20}}))
with open(out_file, "w") as f:
    f.write(text)
