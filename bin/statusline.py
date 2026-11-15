"""Default claudette status line.

Claude Code's built-in numbers assume Claude: its context percentage uses the
window it guesses for an unknown model, and cost.total_cost_usd uses Anthropic
prices. This recomputes both for Mistral.

The status line input has no running token totals (context_window.total_*
describe the current context and the last reply), so the cost is summed from
the session transcript instead, read incrementally so long sessions stay fast.

Reads Claude Code's status line JSON on stdin, prints one line.
Set CLAUDETTE_STATUSLINE_DEBUG=1 to also save the input to
$CLAUDETTE_STATE/statusline-input.json.
"""

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

# USD per million tokens (input, output, cached input), from docs.mistral.ai
# model pages, 2026-11-15. Small has no published cached rate, so cached input
# is charged at the full rate. Medium 3.5 is deprecated and has no price.
PRICES = {
    "mistral-large-latest": (0.70, 2.10, 0.07),
    "mistral-small-latest": (0.15, 0.60, 0.15),
}


def message_cost(model: str, usage: dict) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    fresh = (usage.get("input_tokens") or 0) + (
        usage.get("cache_creation_input_tokens") or 0
    )
    cached = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    return (fresh * price[0] + out * price[1] + cached * price[2]) / 1_000_000


def scan(path: str, entry: dict) -> None:
    """Add the cost of assistant messages written since entry["offset"]."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < entry["offset"]:
        entry.update(offset=0, cost=0.0, last_id=None, unpriced=False)
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        chunk = f.read()
    # Only consume complete lines; a half-written one is picked up next time.
    complete = chunk[: chunk.rfind(b"\n") + 1]
    entry["offset"] += len(complete)
    for line in complete.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        # Claude Code writes one line per content block, each repeating the
        # message's usage, so count each message id once.
        if message.get("id") == entry["last_id"]:
            continue
        entry["last_id"] = message.get("id")
        cost = message_cost(message.get("model") or "", message.get("usage") or {})
        if cost is None:
            entry["unpriced"] = True
        else:
            entry["cost"] += cost


def session_cost(data: dict) -> tuple[float, bool] | None:
    transcript = data.get("transcript_path")
    state = os.environ.get("CLAUDETTE_STATE")
    if not transcript or not state:
        return None
    base = Path(transcript)
    paths = [
        transcript,
        *glob.glob(str(base.with_suffix("") / "subagents" / "*.jsonl")),
    ]
    cache_file = Path(state, "statusline-cost", f"{base.stem}.json")
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    for path in paths:
        entry = cache.setdefault(
            path, {"offset": 0, "cost": 0.0, "last_id": None, "unpriced": False}
        )
        scan(path, entry)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache))
    except OSError:
        pass
    return (
        sum(e["cost"] for e in cache.values()),
        any(e["unpriced"] for e in cache.values()),
    )


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def git_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def render(data: dict) -> str:
    parts = []
    model = (data.get("model") or {}).get("id") or "unknown"
    parts.append(model.removesuffix("-latest"))

    window = int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 0)
    ctx = data.get("context_window") or {}
    usage = ctx.get("current_usage") or {}
    used = sum(
        usage.get(k) or 0
        for k in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    if window:
        parts.append(f"ctx {tokens(used)}/{tokens(window)} {used * 100 // window}%")
    else:
        parts.append(f"ctx {tokens(used)}")

    spent = session_cost(data)
    if spent and (spent[0] or not spent[1]):
        cost, partial = spent
        # A trailing "+" means some replies came from a model without a price.
        parts.append(f"~${cost:.2f}{'+' if partial else ''}")

    cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir")
    if cwd:
        branch = git_branch(cwd)
        name = Path(cwd).name
        parts.append(f"{name} ({branch})" if branch else name)

    return " | ".join(parts)


def main() -> None:
    raw = sys.stdin.read()
    if os.environ.get("CLAUDETTE_STATUSLINE_DEBUG") == "1":
        state = os.environ.get("CLAUDETTE_STATE")
        if state:
            Path(state, "statusline-input.json").write_text(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    print(render(data))


if __name__ == "__main__":
    main()
