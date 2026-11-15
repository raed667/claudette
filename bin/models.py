"""Mistral model metadata for claudette, cached from GET /v1/models.

  models.py list [--refresh]   print the chat models this key can use
  models.py context MODEL      print MODEL's context length; exit 2 if unknown

The cache lives in $CLAUDETTE_STATE/models.json and is refreshed daily, so the
launcher's per-model lookup doesn't add a network call to every start.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = os.environ.get("CLAUDETTE_MISTRAL_API_URL", "https://api.mistral.ai")
CACHE_TTL = 24 * 3600

# Mistral's capabilities.function_calling says True for these, but they reply
# that they can't access files instead of calling tools.
NO_TOOLS = {
    "codestral-latest",
    "codestral-2508",
    "mistral-code-latest",
    "mistral-code-fim-latest",
}
TESTED = {"mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"}


def state_dir() -> Path:
    if state := os.environ.get("CLAUDETTE_STATE"):
        return Path(state)
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "claudette"


def mistral_key() -> str:
    if key := os.environ.get("MISTRAL_API_KEY"):
        return key
    config = Path(
        os.environ.get("CLAUDETTE_CONFIG")
        or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "claudette"
    )
    try:
        return (config / "mistral_api_key").read_text().strip()
    except OSError:
        sys.exit(
            f"claudette: no Mistral API key: export MISTRAL_API_KEY or write it to {config}/mistral_api_key"
        )


def fetch() -> list[dict]:
    req = urllib.request.Request(
        API_URL + "/v1/models", headers={"Authorization": f"Bearer {mistral_key()}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)["data"]
    return [
        {"id": m["id"], "context": m.get("max_context_length")}
        for m in data
        if (m.get("capabilities") or {}).get("completion_chat")
    ]


def models(refresh: bool = False) -> list[dict]:
    cache = state_dir() / "models.json"
    if not refresh:
        try:
            cached = json.loads(cache.read_text())
            if time.time() - cached["fetched_at"] < CACHE_TTL:
                return cached["models"]
        except (OSError, ValueError, KeyError):
            pass
    fresh = fetch()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetched_at": time.time(), "models": fresh}))
    return fresh


def tokens(n: int | None) -> str:
    if not n:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_048_576:.0f}M" if n % 1_000_000 else f"{n // 1_000_000}M"
    return f"{n // 1000}k"


def cmd_list(refresh: bool) -> None:
    rows = sorted(models(refresh), key=lambda m: m["id"])
    current = os.environ.get("CLAUDETTE_MODEL", "mistral-large-latest")
    print(f"{'MODEL':36} {'CONTEXT':>8}  NOTES")
    for m in rows:
        notes = []
        if m["id"] == current:
            notes.append("default")
        if m["id"] in TESTED:
            notes.append("tested")
        if m["id"] in NO_TOOLS:
            notes.append("doesn't call tools")
        print(f"{m['id']:36} {tokens(m['context']):>8}  {', '.join(notes)}")


def cmd_context(model: str) -> None:
    for m in models():
        if m["id"] == model and m["context"]:
            print(m["context"])
            return
    sys.exit(2)


def main() -> None:
    args = sys.argv[1:]
    try:
        if args[:1] == ["list"]:
            cmd_list(refresh="--refresh" in args)
        elif args[:1] == ["context"] and len(args) == 2:
            cmd_context(args[1])
        else:
            sys.exit(__doc__)
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"claudette: could not fetch the Mistral model list: {e}")


if __name__ == "__main__":
    main()
