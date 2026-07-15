"""CLI smoke tests: the app wires up, ``reindex`` (Step 8) and the ``mcp`` stub run."""

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


def test_mcp_stub_runs() -> None:
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0
    assert "not implemented" in result.output.lower()
