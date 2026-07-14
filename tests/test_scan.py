"""Step 3 verify: scan a fixture vault → correct diff; secrets never surface.

The planted-fake-secret assertions (:func:`test_secret_file_never_indexed`,
:func:`test_note_with_embedded_secret_never_indexed`) are the first-class guardrail
gate from ``CLAUDE.md`` §6 / p0-step-plan.md Step 3 — a secret must never appear in the
indexable set, by filename *or* by content.
"""

from __future__ import annotations

from pathlib import Path

from vaultrag.indexer.scan import (
    Rename,
    ScannedFile,
    diff_manifest,
    scan_vault,
    walk_vault,
)
from vaultrag.indexer.secret_guard import is_secret_filename, scan_for_secrets


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _make_vault(root: Path) -> None:
    """A small vault: 3 notes, an ignored tree, an unsupported file, secrets."""
    _write(root, "index.md", "# Home\nwelcome")
    _write(root, "projects/vaultrag/chunking.md", "# Chunking\nheading-tree")
    _write(root, "daily/2026-07-15.md", "# Today\nnotes")
    # non-markdown → unsupported (skipped, not a secret)
    _write(root, "assets/diagram.png", "not really a png")
    # machine-managed tree → always pruned
    _write(root, ".obsidian/workspace.json", "{}")
    # .ragignore excludes the private/ folder (dir-level prune) and a single file
    _write(root, ".ragignore", "private/\nscratch.md\n")
    _write(root, "private/draft.md", "# secret plans")
    _write(root, "scratch.md", "# ignored at file level")


def _indexable_paths(root: Path) -> set[str]:
    files, _ = walk_vault(root)
    return {f.path for f in files}


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------
def test_walk_finds_only_markdown(tmp_path: Path) -> None:
    _make_vault(tmp_path)
    assert _indexable_paths(tmp_path) == {
        "index.md",
        "projects/vaultrag/chunking.md",
        "daily/2026-07-15.md",
    }


def test_walk_stats(tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _, stats = walk_vault(tmp_path)
    assert stats.indexable == 3
    # scratch.md is dropped by a file-level .ragignore rule → counted as ignored.
    # private/ is a dir-level prune, so private/draft.md is never visited or counted.
    assert stats.ignored == 1
    assert stats.unsupported == 2  # assets/diagram.png + the .ragignore file itself
    assert stats.seen == 6  # 3 notes + png + .ragignore + scratch.md (pruned trees excluded)


def test_ragignore_excludes_matching_files(tmp_path: Path) -> None:
    _make_vault(tmp_path)
    assert "private/draft.md" not in _indexable_paths(tmp_path)


def test_hashes_are_content_addressed(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "same body")
    _write(tmp_path, "b.md", "same body")
    _write(tmp_path, "c.md", "different")
    files, _ = walk_vault(tmp_path)
    by_path = {f.path: f.hash for f in files}
    assert by_path["a.md"] == by_path["b.md"]  # identical content → identical hash
    assert by_path["a.md"] != by_path["c.md"]


# ---------------------------------------------------------------------------
# Secret guard — the required gate
# ---------------------------------------------------------------------------
def test_secret_file_never_indexed(tmp_path: Path) -> None:
    _write(tmp_path, "notes.md", "# ok")
    _write(tmp_path, ".env", "OPENAI_API_KEY=sk-live-abc")
    _write(tmp_path, "id_rsa", "-----BEGIN PRIVATE KEY-----\nabc\n")
    _write(tmp_path, "server.pem", "cert material")
    _write(tmp_path, "aws-credentials.md", "access stuff")
    indexable = _indexable_paths(tmp_path)
    assert indexable == {"notes.md"}
    for planted in (".env", "id_rsa", "server.pem", "aws-credentials.md"):
        assert planted not in indexable


def test_note_with_embedded_secret_never_indexed(tmp_path: Path) -> None:
    _write(tmp_path, "clean.md", "# just prose\nno secrets here")
    # A markdown note (passes the filename gate) that pastes live-looking keys.
    _write(tmp_path, "leaky-aws.md", "here is a key AKIAIOSFODNN7EXAMPLE oops")
    _write(
        tmp_path,
        "leaky-pem.md",
        "```\n-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----\n```",
    )
    _write(tmp_path, "leaky-assign.md", 'api_key = "aP9kZ2xQ7mVn4bR8tLwYcE3sD6fH1jG0"')
    indexable = _indexable_paths(tmp_path)
    assert indexable == {"clean.md"}
    _, stats = walk_vault(tmp_path)
    assert stats.secrets_blocked == 3


def test_is_secret_filename_patterns() -> None:
    secret_names = (
        ".env",
        ".env.local",
        "id_rsa",
        "id_ed25519.pub",
        "key.pem",
        "db-credentials.yaml",
    )
    for name in secret_names:
        assert is_secret_filename(name), name
    for name in ("notes.md", "readme.txt", "environment.md", "keynote.md"):
        assert not is_secret_filename(name), name


def test_scan_for_secrets_precision() -> None:
    # positives
    assert scan_for_secrets("token AKIAIOSFODNN7EXAMPLE here") == "aws-access-key-id"
    assert scan_for_secrets("-----BEGIN OPENSSH PRIVATE KEY-----") == "private-key-block"
    assert scan_for_secrets("ghp_" + "a" * 36) == "github-token"
    # negatives — ordinary dev-note content must NOT trip the guard
    assert scan_for_secrets("commit 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e") is None  # git SHA
    assert scan_for_secrets("id: 550e8400-e29b-41d4-a716-446655440000") is None  # UUID
    assert scan_for_secrets("api_key = your_api_key_here") is None  # placeholder
    assert scan_for_secrets("the secret to good code is tests") is None  # prose


# ---------------------------------------------------------------------------
# Diff (pure)
# ---------------------------------------------------------------------------
def _sf(path: str, h: str) -> ScannedFile:
    return ScannedFile(path=path, hash=h, size=len(h), mtime=0)


def test_diff_create_update_unchanged_delete() -> None:
    scanned = [_sf("a.md", "h1"), _sf("b.md", "h2new"), _sf("c.md", "h3")]
    manifest = {"b.md": "h2old", "c.md": "h3", "d.md": "h4"}
    diff = diff_manifest(scanned, manifest)
    assert [f.path for f in diff.creates] == ["a.md"]
    assert [f.path for f in diff.updates] == ["b.md"]
    assert diff.unchanged == ["c.md"]
    assert diff.deletes == ["d.md"]
    assert diff.renames == []
    assert not diff.is_empty


def test_diff_empty_manifest_is_all_creates() -> None:
    # The full-rebuild property: bare vault vs empty manifest → every file is a create.
    scanned = [_sf("a.md", "h1"), _sf("b.md", "h2")]
    diff = diff_manifest(scanned, {})
    assert {f.path for f in diff.creates} == {"a.md", "b.md"}
    assert not diff.updates and not diff.deletes and not diff.renames


def test_diff_detects_rename() -> None:
    # old.md gone, new.md appears with the same hash → a rename, not delete+create.
    scanned = [_sf("new.md", "same"), _sf("keep.md", "k")]
    manifest = {"old.md": "same", "keep.md": "k"}
    diff = diff_manifest(scanned, manifest)
    assert diff.renames == [Rename(old_path="old.md", new_path="new.md", hash="same")]
    assert diff.creates == []
    assert diff.deletes == []
    assert diff.unchanged == ["keep.md"]


def test_diff_null_manifest_hash_is_update_not_unchanged() -> None:
    # A half-recorded doc (hash NULL) must be re-processed, and NULL must not be
    # mistaken for a rename source.
    scanned = [_sf("a.md", "h1")]
    diff = diff_manifest(scanned, {"a.md": None})
    assert [f.path for f in diff.updates] == ["a.md"]
    assert diff.creates == [] and diff.unchanged == [] and diff.renames == []


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------
def test_scan_vault_end_to_end(tmp_path: Path) -> None:
    _make_vault(tmp_path)
    diff, stats = scan_vault(tmp_path, manifest={})
    assert stats.indexable == 3
    assert {f.path for f in diff.creates} == {
        "index.md",
        "projects/vaultrag/chunking.md",
        "daily/2026-07-15.md",
    }
    assert diff.is_empty is False

    # Re-scan against the manifest those creates would produce → nothing to do.
    manifest = {f.path: f.hash for f in diff.creates}
    diff2, _ = scan_vault(tmp_path, manifest=manifest)
    assert diff2.is_empty
    assert len(diff2.unchanged) == 3
