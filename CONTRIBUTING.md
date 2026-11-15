# Contributing

## How it works

Claude Code only speaks Anthropic's Messages API, and Mistral doesn't offer
one. `POST https://api.mistral.ai/v1/messages` exists at the gateway but
returns 404 `Not found` for every model (tested 2026-11-15). Mistral's
documented compatibility is OpenAI-style, so something has to translate.

```
claude  --Anthropic /v1/messages-->  LiteLLM on 127.0.0.1:4791  --/v1/chat/completions-->  api.mistral.ai
```

`bin/claudette` does three things before handing over to `claude`:

1. Starts the LiteLLM proxy if it isn't already running, with a random key
   generated into `$CLAUDETTE_STATE/proxy.key`.
2. Points Claude Code at the proxy through `ANTHROPIC_BASE_URL`,
   `ANTHROPIC_AUTH_TOKEN` and the model env vars, and sets `CLAUDE_CONFIG_DIR`
   to `~/.claudette` so nothing touches the user's regular install.
3. Writes an MCP config and a settings file into the state dir, which register
   the web search server, deny the built-in WebSearch, add the default status
   line and replace the `/model` lineup with Mistral models.

## Layout

| Path | |
|---|---|
| `bin/claudette` | Launcher and proxy lifecycle (`proxy start/stop/restart/status/logs`, `update`) |
| `install.sh` | Idempotent installer, also used by `claudette update` |
| `proxy/config.yaml` | LiteLLM model list and settings |
| `proxy/claudette_patches.py` | Runtime fix for LiteLLM's streamed parallel tool calls |
| `mcp_servers/web_search.py` | MCP server wrapping Mistral's `web_search` connector |
| `bin/models.py` | Cached Mistral model list, used by `claudette models` and the compaction lookup |
| `bin/settings.py` | Generates the `--settings` file: permissions, status line, `/model` lineup |
| `bin/statusline.py` | Default status line with Mistral context and cost figures |
| `scripts/smoke_test.py` | Sends Claude Code-shaped requests through the proxy |
| `pyproject.toml`, `uv.lock` | Pinned Python dependencies |

## Development

Run the launcher straight from your checkout. Give it its own state, config
dir and port so it doesn't collide with an installed copy:

```sh
uv sync
export CLAUDETTE_STATE=/tmp/claudette-dev/state
export CLAUDETTE_CLAUDE_CONFIG_DIR=/tmp/claudette-dev/claude
export CLAUDETTE_PORT=4795
bin/claudette proxy start
bin/claudette -p "say hi" </dev/null
```

`claude -p` reads stdin and appends it to the prompt, so close stdin
(`</dev/null`) when calling it from scripts or heredocs.

`proxy/config.yaml` routes every model name to Mistral with a `*` wildcard,
so new Mistral models work without changes here. `bin/models.py` caches
`GET /v1/models` in `$CLAUDETTE_STATE/models.json` for a day; it backs
`claudette models` and the launcher's compaction lookup. Mistral's
`capabilities.function_calling` isn't reliable: it's true for `codestral-latest`
and `mistral-code-latest`, which reply that they can't access files instead of
calling tools. Those are listed in `NO_TOOLS`, and the `TESTED` set should
grow as more models get a smoke test run (`scripts/smoke_test.py --model <id>`).

## Testing

With a proxy running:

```sh
CLAUDETTE_PROXY_KEY=$(cat "$CLAUDETTE_STATE/proxy.key") \
CLAUDETTE_BASE_URL=http://127.0.0.1:$CLAUDETTE_PORT \
  scripts/smoke_test.py
```

It covers system blocks with `cache_control`, `thinking`, beta headers, single
tool calls, parallel tool results with `toolu_` ids, streaming with parallel
calls requested, and `count_tokens`, for each default model. With
`MISTRAL_API_KEY` set or `--search`, it also starts the MCP server over stdio
and runs a real search.

Lint before committing. The `lint` GitHub workflow runs the same commands,
plus an import of `proxy/claudette_patches.py` to catch a LiteLLM bump that
moves the internals it patches. The smoke test needs a Mistral key, so it stays
manual.

```sh
shellcheck bin/claudette install.sh
uvx ruff@0.8.6 check bin/ scripts/ mcp_servers/ proxy/
uvx ruff@0.8.6 format --check bin/ scripts/ mcp_servers/ proxy/
uv lock --check
```

### What's been verified

The smoke test covers the proxy. These were checked in real Claude Code
sessions (2.1.325, LiteLLM 1.106.6, `mistral-large-latest` unless noted), and
are worth re-checking after a Claude Code or LiteLLM upgrade:

- Interactive sessions from a fresh install, including onboarding
- File reads, edits, writes and Bash, with parallel tool calls
- Subagents
- WebFetch, and web search through the MCP server
- Pasted images
- Auto-compaction
- The `/model` picker lineup and the `fable` alias
- The status line, including cost across turns against a hand calculation
- `ministral-14b-latest` through the wildcard route
- Auto mode, with its permission classifier running on the main model

To drive an interactive session from a script, a Python `pty` works where
`expect` didn't. Strip ANSI codes before matching text, and note that the
workspace trust dialog defaults to "No, exit", so send Down then Enter.

## Known quirks

Each of these took a while to track down, so check the list before changing
the related code.

WebSearch is a server tool. Anthropic runs the search and returns
`web_search_tool_result` blocks. Through the proxy no search happens, Mistral
answers from training data, and Claude Code wraps that answer in its usual
"Web search results" header, so it looks real. That's why the built-in tool is
denied and `mcp_servers/web_search.py` replaces it.

Mistral's `web_search` connector is optional for the model, and the
Conversations API rejects `tool_choice: "required"` with built-in connectors.
Without the system instruction in `web_search.py`, well-known questions get
answered from memory. Answers without `tool_reference` chunks are flagged as
unverified.

Mistral streams parallel tool calls as complete calls in a single chunk.
LiteLLM 1.106.6's `/v1/messages` adapter assumes one call per chunk and merges
them into one `tool_use` block with concatenated JSON (`{...}{...}`), which
Claude Code rejects. `proxy/claudette_patches.py` wraps the adapter's chunk
splitter so each call gets its own chunk, and it's loaded through `callbacks`
in the LiteLLM config so it runs inside the proxy process.
`check_streaming_parallel_tool_calls` fails if the calls merge again or stop
coming back in parallel.

`--allowedTools`, `--disallowedTools` and `--mcp-config` take variadic values
and swallow positional arguments after them (`claudette mcp list` turned into a
prompt). Permissions live in a `--settings` file, and `--settings` is passed
last because its single value ends the `--mcp-config` list.

If a proxy is already on the port with a different key, LiteLLM answers every
request with `400 No connected db`. The launcher checks the key against
`/v1/models` and fails with a readable error instead.

Claude Code loads `~/.claude/CLAUDE.md` even when `CLAUDE_CONFIG_DIR` points
somewhere else.

Claude Code's status line input assumes Claude. `context_window.used_percentage`
is relative to the window it guesses for an unknown model, and
`cost.total_cost_usd` uses Anthropic prices (about 7x Mistral's in testing).
The input also has no running token totals: `context_window.total_input_tokens`
and `total_output_tokens` describe the current context and the last reply. So
`bin/statusline.py` measures context against `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
and sums cost from the session transcript and its subagent transcripts, one
count per message id (each reply is written once per content block), with
cache reads at the cached rate. It keeps a byte offset per transcript in
`$CLAUDETTE_STATE/statusline-cost/` so each refresh only reads new lines. The
`PRICES` table needs updating when Mistral changes prices.
Settings passed with `--settings` outrank the user's `settings.json`, so the
launcher leaves `statusLine` out when the user has their own.
`CLAUDETTE_STATUSLINE_DEBUG=1` saves the raw input to
`$CLAUDETTE_STATE/statusline-input.json`.

For unrecognised model ids Claude Code sends `thinking`, `context_management`
and other Anthropic-only fields. `drop_params: true` in the LiteLLM config and
`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` keep them away from Mistral. Claude
Code also assumes a 1M-token window for unknown models, so the launcher sets
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` from the main model's `max_context_length`,
capped at 256k because every turn resends the whole context, and clamped to
Claude Code's 100k minimum. It falls back to 256k when the lookup fails, and it
doesn't follow a mid-session `/model` switch.
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` tells Claude Code the model's real window, and
Claude Code uses the smaller of it and the auto-compact window. With the
auto-compact window already set, it changes nothing, and it doesn't remove the
`[1m]` suffix on the picker's Default row either, so the launcher leaves it
unset.

Auto mode's permission classifier runs on the main model, as two extra requests
per tool call it checks (`CLAUDE_CODE_AUTO_MODE_MODEL` can point it elsewhere).
Those requests carry a large system prompt and aren't written to the
transcript, so the status line's cost leaves them out.

Claude Code's gateway model discovery (`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`)
only keeps model ids containing "claude" or "anthropic", so the picker is
built with the `modelPicker` setting instead, with `replaceBuiltInOptions` set
so Claude's own models don't show. Every alias (`fable`, `opus`, `sonnet`,
`haiku`) is also pinned to a Mistral model through the
`ANTHROPIC_DEFAULT_*_MODEL` variables; Fable was missed at first and kept
showing Claude's Fable 5.2. `modelPricing` would fix Claude Code's own cost
figures, but it's only read from managed settings, which is why the status
line computes cost itself.

## Environment variables

| Variable | Default | |
|---|---|---|
| `MISTRAL_API_KEY` | | Mistral key, overrides the key file |
| `CLAUDETTE_CONFIG` | `~/.config/claudette` | Holds `mistral_api_key` |
| `CLAUDETTE_STATE` | `~/.local/state/claudette` | Proxy pid, log, key, generated MCP and settings files |
| `CLAUDETTE_CLAUDE_CONFIG_DIR` | `~/.claudette` | `CLAUDE_CONFIG_DIR` for claudette sessions |
| `CLAUDETTE_PORT` | `4791` | Proxy port, bound to 127.0.0.1 |
| `CLAUDETTE_MODEL` | `mistral-large-latest` | Main, `opus` and `sonnet` slots |
| `CLAUDETTE_FAST_MODEL` | `mistral-small-latest` | `haiku` slot and background tasks |
| `CLAUDETTE_SUBAGENT_MODEL` | main model | Subagents |
| `CLAUDETTE_COMPACT_WINDOW` | model's context, capped at `256000` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, minimum 100000 |
| `CLAUDETTE_SEARCH_MODEL` | `mistral-small-latest` | Model behind web search |
| `CLAUDETTE_SEARCH_CONNECTOR` | `web_search` | Or `web_search_premium` |
| `CLAUDETTE_STATUSLINE` | `1` | `0` turns off the default status line |
| `CLAUDETTE_STATUSLINE_DEBUG` | | `1` saves the status line's input JSON |
| `CLAUDETTE_HOME` | `~/.local/share/claudette` | Install location (installer) |
| `CLAUDETTE_BIN_DIR` | `~/.local/bin` | Symlink location (installer) |
| `CLAUDETTE_REPO`, `CLAUDETTE_BRANCH` | GitHub, `main` | Installer source |

## Dependencies

LiteLLM is pinned exactly and `uv.lock` records hashes. LiteLLM 1.82.7 and
1.82.8 were malicious PyPI uploads that stole credentials
([PYSEC-2026-2](https://security.snyk.io/vuln/SNYK-PYTHON-LITELLM-15762713)).
When bumping it, pick a release that has been public for at least two weeks,
check for advisories, and run the full smoke test before merging. The patch
in `proxy/claudette_patches.py` reaches into LiteLLM internals, so a bump may
break proxy startup until it's updated, or make it unnecessary. `mcp` and
`httpx` are pinned to the versions LiteLLM resolves so a LiteLLM bump can't
change them silently.

Advisory review of 1.106.6, 2026-11-15, against the
[LiteLLM security advisories](https://github.com/BerriAI/litellm/security/advisories):

- 1.107.0 was a malicious upload on 2026-10-21, pulled the same day. It was
  never in `uv.lock`.
- CVE-2026-35029 (below 1.83.0) and CVE-2026-59823 (up to 1.83.8) don't affect
  1.106.6.
- GHSA-2rwg-2wh4-7c4f (high, fixed in 1.106.9) does cover 1.106.6, but only
  for deployments with `azure/` or `azure_ai/` models, and only for a caller
  holding the proxy key. claudette routes every model name to `mistral/`, and
  the key never leaves the local `claude` process, so it doesn't apply. Bump
  to 1.106.9 or later once it has been public for two weeks (after
  2026-11-26).
