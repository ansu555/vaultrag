"""Citation stamping — retrieval stage 8 (P1 Stage 3): make every snippet openable.

By the time a hit reaches this stage it already carries what a citation needs *inside*
VaultRAG — ``path``, ``heading_path``/``anchor``, ``line_start``/``line_end``, ``score`` —
all set when the snippet was built (child in ``pipeline._to_snippet``, or the reconstructed
section in ``parents._parent_snippet``). Stage 8 adds the one thing that reaches *out* of
VaultRAG: a working ``obsidian://open`` URI the agent (or a human reading its output) can
click to land on the exact note in Obsidian (arch §10-stage8,
``../Rag/projects/vaultrag/retrieval-pipeline.md`` stage 8).

The URI needs the vault's Obsidian **display name** (what the app calls it), which isn't
derivable from the vault path — a vault opened from ``/home/anik/Documents/Rag`` might be
named "Rag" or anything else — so it comes from a config knob (``Settings.vault_name``,
resolved to the directory name as a fallback at the composition root). With no name
configured, :func:`cite` is a no-op and ``Snippet.uri`` stays ``None`` (callers that don't
serve an Obsidian client, e.g. the eval harness, simply don't pay for or carry a URI).
"""

from __future__ import annotations

from urllib.parse import quote

from vaultrag.models import Snippet


def obsidian_uri(vault_name: str, path: str) -> str:
    """Build ``obsidian://open?vault=<name>&file=<path>`` with both parts percent-encoded.

    ``safe=""`` encodes ``/`` too (``%2F``); Obsidian decodes it back, so subfolder paths
    and spaces round-trip exactly — a note at ``a folder/note.md`` opens the right file, not
    a sibling with a similar name. This is the base ``open`` action only: jumping to a
    heading/line needs a community plugin, so the heading + line range travel as their own
    snippet fields instead of in the URI (arch §10-stage8 names only ``open?vault=…&file=…``).
    """
    return f"obsidian://open?vault={quote(vault_name, safe='')}&file={quote(path, safe='')}"


def cite(snippets: list[Snippet], *, vault_name: str | None) -> list[Snippet]:
    """Stage 8: stamp each snippet with its ``obsidian://`` URI (no-op without a vault name).

    Returns new snippets (``model_copy``) rather than mutating in place — the pipeline treats
    each stage's output as immutable, and a snippet may be shared with logging/telemetry. Only
    ``uri`` is added; the rest of the citation was set at build time.
    """
    if not vault_name:
        return snippets
    return [s.model_copy(update={"uri": obsidian_uri(vault_name, s.path)}) for s in snippets]
