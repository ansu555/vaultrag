"""P1 Stage 2 verify: :class:`VaultParentResolver` reconstructs sections from the vault.

The parent-swap *policy* — grouping, dedupe, the size cap, the child fallback — is driven
through ``search()`` in ``tests/test_retrieval.py``. This module pins the other half: the
resolver that turns a ``(path, anchor)`` pointer back into section text by re-reading the
*current* file, so the index stays a derived cache and the vault stays truth (CLAUDE.md §6).

The resolver only ever touches disk through the ``read_note`` callable it's handed, so these
tests drive it with an in-memory reader (a missing key stands in for a gate refusal / deleted
note); the real gated reader is ``open``'s, exercised at the MCP layer in ``test_mcp.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from vaultrag.retrieval import VaultParentResolver

NOTE = """\
# Guide

Intro line.

## Setup

Install uv.
Run docker.

### Details

Nested under setup.

## Usage

Call search first.
"""


def _reader(files: dict[str, str]) -> Callable[[str], str]:
    def read(path: str) -> str:
        return files[path]  # KeyError on a missing path == a gate refusal / deleted note

    return read


def test_resolves_a_section_including_its_nested_subsections() -> None:
    resolver = VaultParentResolver(read_note=_reader({"g.md": NOTE}))
    section = resolver.resolve("g.md", "setup")

    assert section is not None
    assert section.heading == "Setup"
    assert section.anchor == "setup"
    assert section.heading_path == ["Guide", "Setup"]  # the parent's own trail, for citation
    assert "## Setup" in section.text  # the heading line leads the reconstructed section
    assert "Install uv." in section.text
    assert "Nested under setup." in section.text  # the ### Details subsection rolls up too
    assert "Call search first." not in section.text  # stops before the next same-level heading
    assert section.tokens > 0
    # Line span (for the stage-8 citation): the '## Setup' line through the line before
    # '## Usage' — i.e. the whole section including its nested ### Details subsection.
    assert (section.line_start, section.line_end) == (5, 13)


def test_missing_or_gated_note_resolves_to_none() -> None:
    resolver = VaultParentResolver(read_note=_reader({}))  # every read raises KeyError
    assert resolver.resolve("gone.md", "setup") is None


def test_unknown_heading_resolves_to_none() -> None:
    resolver = VaultParentResolver(read_note=_reader({"g.md": NOTE}))
    assert resolver.resolve("g.md", "no-such-heading") is None


def test_parses_each_note_once_across_resolves() -> None:
    reads: list[str] = []

    def read(path: str) -> str:
        reads.append(path)
        return NOTE

    resolver = VaultParentResolver(read_note=read)
    assert resolver.resolve("g.md", "setup") is not None
    assert resolver.resolve("g.md", "usage") is not None
    assert reads == ["g.md"]  # the second resolve hit the per-instance parse cache
