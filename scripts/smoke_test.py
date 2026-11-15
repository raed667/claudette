#!/usr/bin/env python3
"""Send Claude Code-shaped Anthropic requests through the claudette proxy.

Each check mimics something Claude Code actually sends, so a failure here
predicts a failure inside a real session.

Usage: smoke_test.py [--base-url URL] [--model NAME ...]
Reads the proxy key from CLAUDETTE_PROXY_KEY.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HEADERS = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

READ_FILE_TOOL = {
    "name": "Read",
    "description": "Read a file from disk.",
    "input_schema": {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    },
}


def post(base, key, path, body, extra_headers=None, stream=False):
    headers = dict(HEADERS, **{"x-api-key": key}, **(extra_headers or {}))
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, raw if stream else json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600]


def text_of(msg):
    return "".join(
        b.get("text", "") for b in msg.get("content", []) if b["type"] == "text"
    )


def check_basic(base, key, model):
    code, msg = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        },
    )
    assert code == 200, f"HTTP {code}: {msg}"
    assert "pong" in text_of(msg).lower(), f"unexpected reply: {msg}"


def check_claude_code_extras(base, key, model):
    # Fields Claude Code sends for unrecognized model ids. The proxy must
    # drop or translate them rather than pass them to Mistral.
    code, msg = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 2048,
            "system": [
                {"type": "text", "text": "You are a terse assistant."},
                {
                    "type": "text",
                    "text": "Answer in one word.",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "thinking": {"type": "adaptive"},
            "metadata": {"user_id": "smoke-test"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "What colour is the sky on a clear day?",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
        },
        extra_headers={
            "anthropic-beta": "interleaved-thinking-2025-05-14,context-management-2025-06-27"
        },
    )
    assert code == 200, f"HTTP {code}: {msg}"
    assert "blue" in text_of(msg).lower(), f"unexpected reply: {msg}"


def check_tool_call(base, key, model):
    code, msg = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 2048,
            "tools": [READ_FILE_TOOL],
            "messages": [
                {"role": "user", "content": "Use the Read tool to read /etc/hosts."}
            ],
        },
    )
    assert code == 200, f"HTTP {code}: {msg}"
    uses = [b for b in msg["content"] if b["type"] == "tool_use"]
    assert uses, f"no tool_use block: {msg}"
    assert msg["stop_reason"] == "tool_use", f"stop_reason={msg['stop_reason']}"
    assert uses[0]["input"].get("file_path") == "/etc/hosts", uses[0]


def check_parallel_tool_results(base, key, model):
    # Anthropic puts all tool_results in one user turn; Mistral needs one
    # tool message per result. Claude-style toolu_ ids exercise id mapping.
    history = [
        {
            "role": "user",
            "content": "Read /tmp/a.txt and /tmp/b.txt, then tell me both contents.",
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading both files."},
                {
                    "type": "tool_use",
                    "id": "toolu_01AbCdEfGhIjKlMnOpQrStUv",
                    "name": "Read",
                    "input": {"file_path": "/tmp/a.txt"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_01ZyXwVuTsRqPoNmLkJiHgFe",
                    "name": "Read",
                    "input": {"file_path": "/tmp/b.txt"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01AbCdEfGhIjKlMnOpQrStUv",
                    "content": "apple",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01ZyXwVuTsRqPoNmLkJiHgFe",
                    "content": [{"type": "text", "text": "banana"}],
                },
                {"type": "text", "text": "Summarise both in one line."},
            ],
        },
    ]
    code, msg = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 2048,
            "tools": [READ_FILE_TOOL],
            "messages": history,
        },
    )
    assert code == 200, f"HTTP {code}: {msg}"
    reply = text_of(msg).lower()
    assert "apple" in reply and "banana" in reply, f"unexpected reply: {msg}"


def check_streaming_tool_call(base, key, model):
    code, raw = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 2048,
            "stream": True,
            "tools": [READ_FILE_TOOL],
            "messages": [
                {"role": "user", "content": "Use the Read tool to read /etc/hosts."}
            ],
        },
        stream=True,
    )
    assert code == 200, f"HTTP {code}: {raw}"
    events = [
        line[len("event: ") :]
        for line in raw.splitlines()
        if line.startswith("event: ")
    ]
    for required in ("message_start", "content_block_start", "message_stop"):
        assert required in events, f"missing {required} in {events}"
    assert '"input_json_delta"' in raw or '"tool_use"' in raw, "no tool_use in stream"


def check_streaming_parallel_tool_calls(base, key, model):
    # Mistral sends parallel calls in one chunk, which unpatched LiteLLM merges
    # into a single tool_use block with concatenated JSON ('{...}{...}').
    # proxy/claudette_patches.py splits them; this checks it still does.
    code, raw = post(
        base,
        key,
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 2048,
            "stream": True,
            "tools": [READ_FILE_TOOL],
            "messages": [
                {
                    "role": "user",
                    "content": "Read /tmp/a.txt, /tmp/b.txt and /tmp/c.txt. "
                    "Call the Read tool for all three files in parallel in a single turn.",
                }
            ],
        },
        stream=True,
    )
    assert code == 200, f"HTTP {code}: {raw}"
    blocks = {}
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: ") :])
        if (
            event["type"] == "content_block_start"
            and event["content_block"]["type"] == "tool_use"
        ):
            blocks[event["index"]] = ""
        elif (
            event["type"] == "content_block_delta"
            and event["delta"]["type"] == "input_json_delta"
        ):
            blocks[event["index"]] += event["delta"]["partial_json"]
    assert len(blocks) >= 2, f"expected parallel tool_use blocks, got {len(blocks)}"
    for args in blocks.values():
        try:
            json.loads(args or "{}")
        except json.JSONDecodeError:
            raise AssertionError(f"tool_use input is not one JSON object: {args}")


def check_count_tokens(base, key, model):
    code, msg = post(
        base,
        key,
        "/v1/messages/count_tokens",
        {
            "model": model,
            "messages": [{"role": "user", "content": "hello world"}],
        },
    )
    assert code == 200, f"HTTP {code}: {msg}"
    assert msg.get("input_tokens", 0) > 0, msg


CHECKS = [
    check_basic,
    check_claude_code_extras,
    check_tool_call,
    check_parallel_tool_results,
    check_streaming_tool_call,
    check_streaming_parallel_tool_calls,
    check_count_tokens,
]


def check_web_search_mcp():
    """Drive mcp_servers/web_search.py over stdio the way Claude Code does."""
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        ["uv", "run", "--quiet", "--frozen", "--project", str(root), "python",
         str(root / "mcp_servers" / "web_search.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip

    def rpc(msg_id, method, params):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None:
            msg["id"] = msg_id
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if msg_id is None:
            return None
        while True:
            reply = json.loads(proc.stdout.readline())
            if reply.get("id") == msg_id:
                assert "error" not in reply, reply
                return reply["result"]

    try:
        rpc(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0"},
        })  # fmt: skip
        rpc(None, "notifications/initialized", {})
        tools = [t["name"] for t in rpc(2, "tools/list", {})["tools"]]
        assert tools == ["web_search"], f"unexpected tools: {tools}"
        result = rpc(3, "tools/call", {
            "name": "web_search",
            "arguments": {"query": "Who maintains the CPython project?"},
        })  # fmt: skip
        text = "".join(c.get("text", "") for c in result["content"])
        assert not result.get("isError"), text[:500]
        assert "Sources:\n- [" in text, f"no sources in result: {text[:500]}"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CLAUDETTE_BASE_URL", "http://127.0.0.1:4791"),
    )
    parser.add_argument("--model", action="append")
    parser.add_argument(
        "--search",
        action="store_true",
        help="test the web_search MCP server (reads the key from the claudette config file)",
    )
    args = parser.parse_args()
    key = os.environ.get("CLAUDETTE_PROXY_KEY")
    if not key:
        sys.exit("CLAUDETTE_PROXY_KEY is not set")
    models = args.model or [
        "mistral-large-latest",
        "mistral-medium-latest",
        "mistral-small-latest",
    ]

    failures = 0
    for model in models:
        for check in CHECKS:
            name = f"{model:24} {check.__name__[len('check_'):]}"
            try:
                check(args.base_url, key, model)
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}\n     {str(e)[:500]}")

    if os.environ.get("MISTRAL_API_KEY") or args.search:
        try:
            check_web_search_mcp()
            print(f"PASS {'mcp':24} web_search")
        except (AssertionError, json.JSONDecodeError) as e:
            failures += 1
            print(f"FAIL {'mcp':24} web_search\n     {str(e)[:500]}")
    else:
        print(f"SKIP {'mcp':24} web_search (set MISTRAL_API_KEY or pass --search)")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
