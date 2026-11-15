"""Runtime patches for LiteLLM, loaded through `callbacks` in config.yaml.

Mistral streams parallel tool calls as complete calls in a single chunk
(`delta.tool_calls` with several entries). LiteLLM's /v1/messages adapter
assumes one call per chunk, so it opens one tool_use block and concatenates
every call's arguments into it ('{...}{...}'), which Claude Code rejects.
Splitting such chunks into one chunk per call before the adapter sees them
gives each call its own block.

Written against LiteLLM 1.106.6. Importing this module fails if the patched
internals move, so a LiteLLM bump breaks proxy startup loudly instead of
silently re-breaking parallel tool calls.
"""

import copy

from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.anthropic.pass_through.adapters import streaming_iterator

_Splitter = streaming_iterator._CombinedChunkSplitter
_original_split = _Splitter._split_by_payload_kind


def _split_parallel_tool_calls(chunk):
    pieces = []
    for piece in _original_split(chunk):
        choices = getattr(piece, "choices", None) or []
        delta = getattr(choices[0], "delta", None) if len(choices) == 1 else None
        tool_calls = getattr(delta, "tool_calls", None) or []
        # Only split calls that each carry a name. A chunk with several
        # argument continuations would need per-index routing instead.
        if len(tool_calls) < 2 or not all(
            getattr(getattr(call, "function", None), "name", None)
            for call in tool_calls
        ):
            pieces.append(piece)
            continue
        for position, call in enumerate(tool_calls):
            single = copy.deepcopy(piece)
            single.choices[0].delta.tool_calls = [copy.deepcopy(call)]
            if position > 0 and hasattr(single.choices[0].delta, "role"):
                single.choices[0].delta.role = None
            pieces.append(single)
    return tuple(pieces)


_Splitter._split_by_payload_kind = staticmethod(_split_parallel_tool_calls)

# LiteLLM's `callbacks` setting needs an instance to register; the patch above
# is the point of importing this module.
handler = CustomLogger()
