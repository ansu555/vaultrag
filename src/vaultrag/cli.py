"""``vaultrag`` command-line entrypoint.

P0 exposes two subcommands, stubbed here at Step 0 and filled in later:

* ``reindex`` — build the index from a vault (wired at Step 8).
* ``mcp``     — serve the MCP tools over stdio (wired at Step 10).

Later phases add ``serve`` (HTTP) and ``eval``. See
``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vaultrag import __version__
from vaultrag.config import load_settings
from vaultrag.logging import configure_logging, get_logger

app = typer.Typer(
    name="vaultrag",
    help="Self-hosted, MCP-first RAG backend over Obsidian vaults.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"vaultrag {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """VaultRAG CLI."""


@app.command()
def reindex(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope for the index.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault to index.")
    ] = None,
) -> None:
    """Build the index from a vault (scan → parse → chunk → embed → upsert).

    Stubbed until Step 8 of the P0 plan.
    """
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")
    log.info(
        "reindex.stub",
        project=project or settings.project_id,
        vault=str(vault or settings.vault_path),
        status="not_implemented",
        step="P0 Step 8",
    )
    typer.echo("reindex is not implemented yet (P0 Step 8).")


@app.command()
def mcp(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope to serve.")
    ] = None,
) -> None:
    """Serve the MCP tools (search/open) over stdio.

    Stubbed until Step 10 of the P0 plan.
    """
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")
    log.info(
        "mcp.stub",
        project=project or settings.project_id,
        status="not_implemented",
        step="P0 Step 10",
    )
    typer.echo("mcp is not implemented yet (P0 Step 10).")


def main() -> None:
    """Console-script entrypoint (``vaultrag`` → this)."""
    app()


if __name__ == "__main__":
    main()
