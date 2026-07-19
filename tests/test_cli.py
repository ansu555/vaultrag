"""CLI smoke tests: the app wires up; ``reindex`` (Step 8) and ``mcp`` (Step 10) fail cleanly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP
from typer.testing import CliRunner

from vaultrag import __version__
from vaultrag.cli import app
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.store.sqlite import SQLiteMetaStore

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reindex" in result.output
    assert "mcp" in result.output
    assert "watch" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_reindex_fails_gracefully_when_services_are_down(tmp_path: Path) -> None:
    """No live Qdrant/Ollama in this test run — assert a clean error, not a traceback."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    result = runner.invoke(
        app,
        ["reindex"],
        env={
            "VAULTRAG_VAULT_PATH": str(vault_dir),
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_PROJECT_ID": "test",
            # A port nothing listens on: fails fast (connection refused) instead of
            # depending on/timing out against a real Qdrant.
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 1
    assert "reindex failed" in result.output.lower()


def test_reindex_fails_gracefully_when_state_dir_is_a_file(tmp_path: Path) -> None:
    """Opening the metadata store can itself fail (bad VAULTRAG_STATE_DIR) — that too
    must be a clean exit 1 with a message, not a traceback from before the try block."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    state_path = tmp_path / "state"
    state_path.write_text("a file where the state dir should be")
    result = runner.invoke(
        app,
        ["reindex"],
        env={
            "VAULTRAG_VAULT_PATH": str(vault_dir),
            "VAULTRAG_STATE_DIR": str(state_path),
            "VAULTRAG_PROJECT_ID": "test",
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 1
    assert "reindex failed" in result.output.lower()


def test_reindex_fails_gracefully_when_db_is_unopenable(tmp_path: Path) -> None:
    """sqlite3.Error (here: the db path is a directory) exits 1 cleanly, not a traceback."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    state_dir = tmp_path / "state"
    (state_dir / "vaultrag.db").mkdir(parents=True)
    result = runner.invoke(
        app,
        ["reindex"],
        env={
            "VAULTRAG_VAULT_PATH": str(vault_dir),
            "VAULTRAG_STATE_DIR": str(state_dir),
            "VAULTRAG_PROJECT_ID": "test",
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 1
    assert "reindex failed" in result.output.lower()


def test_mcp_fails_gracefully_when_vault_is_missing(tmp_path: Path) -> None:
    """A misconfigured vault path (the common `claude mcp add` mistake) must exit 1 with a
    message on stderr before ever binding stdio — not hang as a broken protocol server."""
    result = runner.invoke(
        app,
        ["mcp", "--vault", str(tmp_path / "does-not-exist")],
        env={"VAULTRAG_PROJECT_ID": "test"},
    )
    assert result.exit_code == 1
    assert "mcp failed" in result.output.lower()


def test_mcp_fails_gracefully_on_bad_embed_dims(tmp_path: Path) -> None:
    """Misconfiguration (VAULTRAG_EMBED_DIMS <= 0) exits 1 cleanly, not a traceback."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    result = runner.invoke(
        app,
        ["mcp", "--vault", str(vault_dir)],
        env={"VAULTRAG_PROJECT_ID": "test", "VAULTRAG_EMBED_DIMS": "0"},
    )
    assert result.exit_code == 1
    assert "mcp failed" in result.output.lower()


def test_watch_fails_gracefully_when_vault_is_missing(tmp_path: Path) -> None:
    """A missing vault path exits 1 with a message before touching any service."""
    result = runner.invoke(
        app,
        ["watch", "--vault", str(tmp_path / "nope")],
        env={"VAULTRAG_PROJECT_ID": "test"},
    )
    assert result.exit_code == 1
    assert "watch failed" in result.output.lower()


def test_watch_fails_gracefully_when_services_are_down(tmp_path: Path) -> None:
    """The boot reconcile needs Qdrant; when it's down, exit 1 cleanly (no traceback)."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    result = runner.invoke(
        app,
        ["watch"],
        env={
            "VAULTRAG_VAULT_PATH": str(vault_dir),
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_PROJECT_ID": "test",
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 1
    assert "watch failed" in result.output.lower()


def test_mcp_watch_starts_and_stops_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mcp --watch` opens a store, starts one VaultWatcher, and stops+closes on exit.

    The watcher's threads are stubbed (start/stop recorded) so nothing real is scheduled;
    what's pinned is the composition-root contract: with --watch the store is opened and
    closed, and the watcher is both started and stopped in the finally.
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    events: list[str] = []

    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: events.append("ran"))
    monkeypatch.setattr(VaultWatcher, "start", lambda self: events.append("watch-start"))
    monkeypatch.setattr(VaultWatcher, "stop", lambda self, **kw: events.append("watch-stop"))
    monkeypatch.setattr(QdrantVectorStore, "close", lambda self: events.append("close-qdrant"))
    monkeypatch.setattr(OllamaEmbedder, "close", lambda self: events.append("close-ollama"))
    monkeypatch.setattr(SQLiteMetaStore, "close", lambda self: events.append("close-store"))

    result = runner.invoke(
        app,
        ["mcp", "--watch", "--project", "test-proj", "--vault", str(vault_dir)],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    # started before run(), stopped after; the store is opened and closed.
    assert events.index("watch-start") < events.index("ran") < events.index("watch-stop")
    assert "close-store" in events


def test_mcp_composes_a_stdio_server_and_closes_its_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only (`--no-write`) composition contract, with `run()` stubbed.

    Pins the two properties that break the protocol silently rather than loudly:
    `transport="stdio"` and `show_banner=False` (a banner would print to stdout — the
    JSON-RPC channel — and corrupt the stream). Also pins that both clients get closed,
    since the `finally` is the only thing preventing a leaked httpx/Qdrant connection.
    With `--no-write` no MetaStore is opened, so search-reads-Qdrant/open-reads-vault
    stays the whole story.
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    captured: dict[str, Any] = {}
    closed: list[str] = []

    def fake_run(self: FastMCP, **kwargs: Any) -> None:
        captured["name"] = self.name
        captured["kwargs"] = kwargs

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.setattr(QdrantVectorStore, "close", lambda self: closed.append("qdrant"))
    monkeypatch.setattr(OllamaEmbedder, "close", lambda self: closed.append("ollama"))

    result = runner.invoke(
        app,
        ["mcp", "--no-write", "--project", "test-proj", "--vault", str(vault_dir)],
        # A port nothing listens on: construction must not need a live service.
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"] == {"transport": "stdio", "show_banner": False}
    assert sorted(closed) == ["ollama", "qdrant"]
    # --no-write means no manifest store was ever opened.
    assert not (tmp_path / "state" / "vaultrag.db").exists()


def test_mcp_opens_a_store_for_write_back_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default `mcp` (write-back on) opens the manifest store the write tools need, and
    closes it in the finally alongside the Qdrant/Ollama clients."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    events: list[str] = []

    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: events.append("ran"))
    monkeypatch.setattr(SQLiteMetaStore, "close", lambda self: events.append("close-store"))

    result = runner.invoke(
        app,
        ["mcp", "--project", "test-proj", "--vault", str(vault_dir)],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    assert "ran" in events  # the server was served
    assert "close-store" in events  # a store was opened for write-back and closed


def test_mcp_write_back_disabled_by_env_opens_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VAULTRAG_WRITEBACK_ENABLED=false` disables the write tools even without --no-write."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    result = runner.invoke(
        app,
        ["mcp", "--project", "test-proj", "--vault", str(vault_dir)],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
            "VAULTRAG_WRITEBACK_ENABLED": "false",
        },
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "state" / "vaultrag.db").exists()
