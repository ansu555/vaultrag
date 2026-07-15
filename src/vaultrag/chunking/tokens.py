"""Cheap token-count heuristic for chunk sizing.

P0 has no model-specific tokenizer available yet -- the embedder (Step 6) is
Ollama/Qwen3, not a tiktoken-compatible BPE, so pulling in `tiktoken` would give
false precision against the wrong vocabulary. A ~4-chars/token estimate is the
same class of approximation the retrieval budget packer already uses for
`budget_tokens` (arch §13: "simple truncation for P0"); swap in a real tokenizer
once Step 6 exists to validate against, if eval data says this is off.
"""

from __future__ import annotations

import math

# Public so the one place that needs the inverse conversion (the `open` tool's
# token-cap → char-budget in mcp/server.py) shares this definition instead of
# hardcoding its own 4 — the two must move together if the heuristic is ever tuned.
CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Approximate token count: ~4 chars/token; 0 for empty/whitespace-only text."""
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, math.ceil(len(stripped) / CHARS_PER_TOKEN))
