"""Write the settings file claudette passes to `claude --settings`.

  settings.py OUTPUT_PATH

Settings passed with --settings outrank the user's settings.json, and
modelPicker and statusLine are taken whole from the highest source, so each
is only added when the user hasn't set their own in $CLAUDE_CONFIG_DIR.

Env: ROOT, CLAUDE_CONFIG_DIR, CLAUDETTE_MODEL, CLAUDETTE_FAST_MODEL,
CLAUDETTE_STATUSLINE.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from models import TESTED, models  # noqa: E402

# Shown in the /model picker, in this order. The main model is added on top
# when it isn't one of these.
PICKER = [
    ("mistral-large-latest", "Mistral Large", "most capable"),
    ("mistral-medium-latest", "Mistral Medium", "deprecated by Mistral"),
    ("mistral-small-latest", "Mistral Small", "fastest and cheapest"),
]
assert {m for m, _, _ in PICKER} == TESTED


def user_settings() -> dict:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def context_sizes() -> dict[str, int]:
    try:
        return {m["id"]: m["context"] for m in models() if m["context"]}
    except SystemExit:
        # No key or network: rows just go without a context size.
        return {}
    except Exception:
        return {}


def picker_row(model: str, label: str, blurb: str, sizes: dict) -> dict:
    parts = [model]
    if ctx := sizes.get(model):
        parts.append(
            f"{round(ctx / 1_048_576)}M context"
            if ctx >= 1_000_000
            else f"{ctx // 1000}k context"
        )
    if blurb:
        parts.append(blurb)
    return {"model": model, "label": label, "description": ", ".join(parts)}


def build() -> dict:
    root = Path(os.environ["ROOT"])
    user = user_settings()
    settings: dict = {
        # WebSearch runs server-side at Anthropic and gets fabricated through
        # Mistral; mcp__claudette__web_search replaces it.
        "permissions": {
            "deny": ["WebSearch"],
            "allow": ["mcp__claudette__web_search"],
        }
    }

    if os.environ.get("CLAUDETTE_STATUSLINE", "1") != "0" and "statusLine" not in user:
        settings["statusLine"] = {
            "type": "command",
            "command": f'"{root}/.venv/bin/python" "{root}/bin/statusline.py"',
        }

    if "modelPicker" not in user:
        sizes = context_sizes()
        main = os.environ.get("CLAUDETTE_MODEL", "mistral-large-latest")
        rows = [picker_row(m, label, blurb, sizes) for m, label, blurb in PICKER]
        if main not in TESTED:
            rows.insert(0, picker_row(main, main, "set by CLAUDETTE_MODEL", sizes))
        # Replacing the built-in lineup hides Claude's own models, which the
        # proxy can't serve.
        settings["modelPicker"] = {"options": rows, "replaceBuiltInOptions": True}

    return settings


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(build(), indent=2) + "\n")
