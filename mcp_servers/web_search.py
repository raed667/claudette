"""MCP server that gives claudette web search through Mistral's web_search connector.

Claude Code's built-in WebSearch runs server-side at Anthropic, so it can't
work through Mistral. Mistral's Conversations API has its own search
connector; this exposes it to Claude Code as an MCP tool.

Reads the Mistral key from MISTRAL_API_KEY or $CLAUDETTE_CONFIG/mistral_api_key,
the same places bin/claudette looks.
"""

import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API_URL = (
    os.environ.get("CLAUDETTE_MISTRAL_API_URL", "https://api.mistral.ai")
    + "/v1/conversations"
)
MODEL = os.environ.get("CLAUDETTE_SEARCH_MODEL", "mistral-small-latest")
# web_search_premium adds news-agency sources on top of the regular engine.
CONNECTOR = os.environ.get("CLAUDETTE_SEARCH_CONNECTOR", "web_search")

server = MCPServer(
    name="claudette",
    instructions="Use the web_search tool for anything that needs current or external information.",
    # INFO logs every HTTP request to stderr, which Claude Code shows as MCP noise.
    log_level="WARNING",
)


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
        raise ToolError(
            f"no Mistral API key: set MISTRAL_API_KEY or write it to {config}/mistral_api_key"
        ) from None


def render(outputs: list[dict]) -> str:
    """Turn Conversations API outputs into text with a deduplicated source list."""
    text: list[str] = []
    sources: dict[str, str] = {}
    queries: list[str] = []
    for out in outputs:
        if out.get("type") == "tool.execution":
            args = out.get("arguments")
            if isinstance(args, str) and args:
                queries.append(args)
        elif out.get("type") == "message.output":
            content = out.get("content")
            if isinstance(content, str):
                text.append(content)
                continue
            for chunk in content or []:
                if chunk.get("type") == "text":
                    text.append(chunk.get("text", ""))
                elif chunk.get("type") == "tool_reference" and chunk.get("url"):
                    sources.setdefault(chunk["url"], chunk.get("title") or chunk["url"])

    parts = ["".join(text).strip() or "(no answer returned)"]
    if sources:
        parts.append(
            "Sources:\n" + "\n".join(f"- [{t}]({u})" for u, t in sources.items())
        )
    else:
        # Without sources the answer may come from the model's training data
        # rather than a search, so say so instead of passing it off as results.
        parts.append("No sources were returned; treat this answer as unverified.")
    if queries:
        parts.append("Queries run: " + "; ".join(queries))
    return "\n\n".join(parts)


@server.tool(
    # Plain text; structured output wraps the answer in {"result": "..."} JSON.
    structured_output=False,
    description=(
        "Search the web for current information and get a summarised answer with source URLs. "
        "Use for recent events, current versions, documentation or anything after your "
        "training data. Follow up with WebFetch on a source URL when you need the full page."
    ),
)
async def web_search(query: str) -> str:
    payload = {
        "model": MODEL,
        "inputs": query,
        "tools": [{"type": CONNECTOR}],
        # Mistral rejects tool_choice "required" with built-in connectors, and
        # without this the model answers well-known questions from memory.
        "instructions": (
            "You are a web search backend. Always run the web_search tool before "
            "answering, even if you think you know the answer. Base the answer only "
            "on the search results and cite them."
        ),
        # Don't keep a server-side conversation per search.
        "store": False,
    }
    headers = {"Authorization": f"Bearer {mistral_key()}"}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(API_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise ToolError(f"request to Mistral failed: {e!r}") from None
    if resp.status_code != 200:
        raise ToolError(f"Mistral returned HTTP {resp.status_code}: {resp.text[:500]}")
    return render(resp.json().get("outputs", []))


if __name__ == "__main__":
    server.run("stdio")
