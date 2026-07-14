"""Step 0 smoke tests: the CLI wires up and the stubbed subcommands run."""

from __future__ import annotations

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


def test_reindex_stub_runs() -> None:
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0
    assert "not implemented" in result.output.lower()


def test_mcp_stub_runs() -> None:
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0
    assert "not implemented" in result.output.lower()
