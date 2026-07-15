"""CLI smoke tests: the app wires up; ``reindex`` (Step 8) and ``mcp`` (Step 10) fail cleanly."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vaultrag import __version__
from vaultrag.cli import app

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reindex" in result.output
    assert "mcp" in result.output


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
