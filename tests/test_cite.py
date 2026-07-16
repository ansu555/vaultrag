"""P1 Stage 3 verify: citation stamping in ``retrieval/cite.py`` (retrieval stage 8).

Two things to pin: the ``obsidian://open`` URI is built correctly and round-trips (spaces
and subfolder slashes survive percent-encoding, so the link opens the *right* note), and
the :func:`cite` stage is a pure no-op when no vault name is configured (a caller not
serving an Obsidian client carries no URI and pays nothing).
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

from vaultrag.models import Snippet
from vaultrag.retrieval.cite import cite, obsidian_uri


def _snippet(path: str, *, uri: str | None = None) -> Snippet:
    return Snippet(path=path, text="body", score=1.0, tokens=1, uri=uri)


# ---------------------------------------------------------------------------
# obsidian_uri — the URI shape (arch §10-stage8)
# ---------------------------------------------------------------------------
def test_uri_has_the_open_action_and_both_query_params() -> None:
    uri = obsidian_uri("Rag", "projects/vaultrag/chunking-strategy.md")
    parts = urlsplit(uri)
    assert parts.scheme == "obsidian"
    assert parts.netloc == "open"
    params = parse_qs(parts.query)
    assert params["vault"] == ["Rag"]
    assert params["file"] == ["projects/vaultrag/chunking-strategy.md"]


def test_uri_percent_encodes_spaces_in_the_vault_name_and_path() -> None:
    uri = obsidian_uri("My Notes", "a folder/a note.md")
    # Encoded on the wire — no raw spaces — but decodes back to the originals.
    assert " " not in uri
    assert "%20" in uri
    params = parse_qs(urlsplit(uri).query)
    assert params["vault"] == ["My Notes"]
    assert params["file"] == ["a folder/a note.md"]


def test_uri_encodes_subfolder_slashes_and_round_trips() -> None:
    path = "decisions/some sub/vector-search-qdrant.md"
    uri = obsidian_uri("Rag", path)
    # safe="" so '/' is encoded too — Obsidian decodes it, so the whole path round-trips
    # rather than being mistaken for a URL path segment.
    assert "%2F" in uri
    file_value = parse_qs(urlsplit(uri).query)["file"][0]
    assert file_value == path
    assert unquote(file_value) == path


def test_uri_encodes_reserved_characters() -> None:
    # '&', '#', '?', '=' in a path would break query parsing if left raw.
    uri = obsidian_uri("Rag", "notes/q&a #1 (draft)?.md")
    params = parse_qs(urlsplit(uri).query)
    assert params["file"] == ["notes/q&a #1 (draft)?.md"]


# ---------------------------------------------------------------------------
# cite — the stage 8 wiring
# ---------------------------------------------------------------------------
def test_cite_stamps_a_uri_on_every_snippet() -> None:
    snippets = [_snippet("a/b.md"), _snippet("c/d.md")]
    cited = cite(snippets, vault_name="Rag")
    assert [s.uri for s in cited] == [
        obsidian_uri("Rag", "a/b.md"),
        obsidian_uri("Rag", "c/d.md"),
    ]


def test_cite_does_not_mutate_the_input_snippets() -> None:
    original = _snippet("a/b.md")
    cite([original], vault_name="Rag")
    assert original.uri is None  # cite returns copies; the caller's list is untouched


def test_cite_is_a_no_op_without_a_vault_name() -> None:
    snippets = [_snippet("a/b.md")]
    same = cite(snippets, vault_name=None)
    assert same is snippets  # returned unchanged, not re-allocated
    assert same[0].uri is None


def test_cite_treats_an_empty_vault_name_as_unset() -> None:
    snippets = [_snippet("a/b.md")]
    assert cite(snippets, vault_name="")[0].uri is None
