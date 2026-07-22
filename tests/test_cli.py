"""CLI smoke tests: the app wires up; ``reindex`` (Step 8) and ``mcp`` (Step 10) fail cleanly."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from typer.testing import CliRunner

from vaultrag import __version__
from vaultrag.cli import app
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.eval import load_candidates
from vaultrag.index.base import VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.retrieval import q_hash
from vaultrag.store.sqlite import SQLiteMetaStore

# Every assertion below matches help/error *text*, so the output must be unstyled. Rich
# renders a flag as two spans — `--force` becomes
# `ESC[1;2;36m-ESC[0m ESC[1;2;36m-forceESC[0m` — and the literal substring is then absent
# from the output entirely. That only bites where color is forced, so it passed on every dev
# box (a captured, non-tty run is uncolored) and failed on the very first CI run, which sets
# FORCE_COLOR. Reproduce the CI condition locally with `FORCE_COLOR=1 uv run pytest`.
#
# `TERM=dumb` is the switch that actually wins: Rich's FORCE_COLOR beats NO_COLOR, so
# NO_COLOR alone does not strip the styling. Both are set because they state the same intent
# and `test_cli_output_is_unstyled` pins the outcome rather than the mechanism — if a Rich
# upgrade changes the precedence, that one canary fails with a clear reason instead of a
# dozen confusing substring mismatches.
runner = CliRunner(env={"TERM": "dumb", "NO_COLOR": "1"})


def test_cli_output_is_unstyled() -> None:
    """The canary for every text assertion in this module (see ``runner`` above).

    Rich splits a flag across styled spans, so styled output makes ``"--force" in output``
    false even though the help plainly shows ``--force``. Asserting the *absence of escape
    codes* fails once, and legibly, instead of as a scatter of substring mismatches.
    """
    result = runner.invoke(app, ["reindex", "--help"])
    assert result.exit_code == 0
    assert "\x1b[" not in result.output, "CLI output is styled; text assertions will misfire"


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


def test_reindex_offers_force() -> None:
    """The documented DR path (CLAUDE.md §6) must be reachable from the CLI, not just the
    library — a plain reindex cannot repair a drifted vector store."""
    result = runner.invoke(app, ["reindex", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output


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


def _tool_names(server: FastMCP) -> list[str]:
    """Tool names registered on a server, from a sync context (`list_tools` is async)."""
    return sorted(t.name for t in asyncio.run(server.list_tools()))


def test_mcp_serves_read_only_without_a_state_db_and_omits_related(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only server pointed at a state dir with no index must still come up.

    The graph is a *reader*: it attaches to an existing DB and never conjures one, so this
    path leaves no empty database behind and simply does not advertise `related` — a server
    must not offer a tool it has no data for (P2 Stage 3).
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    tools: list[list[str]] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: tools.append(_tool_names(self)))

    result = runner.invoke(
        app,
        ["mcp", "--project", "test-proj", "--vault", str(vault_dir), "--no-write"],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "state" / "vaultrag.db").exists()
    # `list_projects` is unconditional (discovery); `overview` needs the metadata store
    # this path deliberately does not open, so it is absent alongside `related`.
    assert tools == [["list_projects", "open", "search"]]


def test_mcp_registers_related_when_a_state_db_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror case: with an index on disk, the graph attaches even in read-only mode."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    SQLiteMetaStore(tmp_path / "state" / "vaultrag.db").close()  # an already-indexed vault
    tools: list[list[str]] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: tools.append(_tool_names(self)))

    result = runner.invoke(
        app,
        ["mcp", "--project", "test-proj", "--vault", str(vault_dir), "--no-write"],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    assert tools == [["list_projects", "open", "overview", "related", "search"]]


# ---------------------------------------------------------------------------
# `mcp` serving N projects (P3 Stage 2)
#
# What is pinned here is the *resolution* layer — which projects one process ends up
# serving for a given argv — because that is where a mistake is silent: serving the wrong
# set looks like a working server that answers about the wrong vault.
# ---------------------------------------------------------------------------
def _unwrapped(output: str) -> str:
    """Collapse Rich's box drawing + word wrapping to one line of plain text."""
    stripped = "".join(c for c in output if c not in "│╭╮╰╯─")
    return " ".join(stripped.split())


def _register(state_dir: Path, **projects: Path) -> None:
    """Register projects directly through the store, as `vaultrag project add` would."""
    store = SQLiteMetaStore(state_dir / "vaultrag.db")
    for pid, vault in projects.items():
        store.upsert_project(pid, name=pid, vault_path=str(vault))
    store.close()


def _served(tmp_path: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run `mcp` with `run()` stubbed, returning the project ids it came up serving."""
    seen: list[list[str]] = []

    def _capture(self: FastMCP, **kwargs: Any) -> None:
        # The instructions string names the served scope; the tool set is identical
        # regardless, so the projects are read off the server's own description.
        seen.append(sorted(_project_ids(self)))

    monkeypatch.setattr(FastMCP, "run", _capture)
    result = runner.invoke(
        app,
        argv,
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 0, result.output
    return seen[0]


def _project_ids(server: FastMCP) -> list[str]:
    """Ask the running server which projects it serves, through its own tool."""

    async def _run() -> list[str]:
        async with Client(server) as client:
            got = await client.call_tool("list_projects", {})
            assert got.structured_content is not None
            return [row["id"] for row in got.structured_content["result"]]

    return asyncio.run(_run())


def test_mcp_with_no_project_serves_every_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the stage exists for: `claude mcp add vaultrag -- uv run vaultrag mcp`
    picks up every registered vault, so adding a project later needs no re-registration
    of the MCP server itself."""
    alpha, beta = tmp_path / "a", tmp_path / "b"
    alpha.mkdir()
    beta.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", alpha=alpha, beta=beta)
    assert _served(tmp_path, ["mcp", "--no-write"], monkeypatch) == ["alpha", "beta"]


def test_mcp_with_one_project_serves_only_that_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowing must stay possible — a registry with N projects does not force serving N."""
    alpha, beta = tmp_path / "a", tmp_path / "b"
    alpha.mkdir()
    beta.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", alpha=alpha, beta=beta)
    assert _served(tmp_path, ["mcp", "--project", "alpha", "--no-write"], monkeypatch) == ["alpha"]


def test_mcp_project_flag_repeats_to_serve_a_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", alpha=tmp_path / "a", beta=tmp_path / "b", gamma=tmp_path / "c")
    served = _served(
        tmp_path, ["mcp", "--project", "alpha", "--project", "gamma", "--no-write"], monkeypatch
    )
    assert served == ["alpha", "gamma"]


def test_mcp_keeps_the_unregistered_single_project_shorthand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0's `--project X --vault Y` must keep working with nothing registered — and must
    still not conjure a state db just to discover the registry is empty."""
    vault = tmp_path / "vault"
    vault.mkdir()
    served = _served(
        tmp_path, ["mcp", "--project", "adhoc", "--vault", str(vault), "--no-write"], monkeypatch
    )
    assert served == ["adhoc"]
    assert not (tmp_path / "state" / "vaultrag.db").exists()


def test_mcp_refuses_vault_with_several_projects(tmp_path: Path) -> None:
    """`--vault` names one tree; pairing it with several projects is a contradiction, and
    guessing which one it meant is how a tenant ends up pointed at the wrong vault."""
    result = runner.invoke(
        app,
        ["mcp", "--project", "a", "--project", "b", "--vault", str(tmp_path)],
        env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code != 0
    assert "cannot apply to several" in _unwrapped(result.output)


def test_mcp_names_an_unregistered_project_in_a_multi_project_run(tmp_path: Path) -> None:
    """With several ids there is no vault path to fall back on, so an unregistered one is
    an error naming the fix, not a silent fallback to the process-wide vault."""
    alpha = tmp_path / "a"
    alpha.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", alpha=alpha)
    result = runner.invoke(
        app,
        ["mcp", "--project", "alpha", "--project", "ghost"],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code != 0
    # Rich word-wraps inside its error box, so match on the unwrapped text: the point is
    # that the message names the fix, not that it fits a terminal width.
    assert "is not registered" in _unwrapped(result.output)
    assert "vaultrag project add" in _unwrapped(result.output)


def test_mcp_reports_which_projects_vault_path_is_broken(tmp_path: Path) -> None:
    """With N tenants, "vault path is not a directory" is useless without the tenant."""
    alpha = tmp_path / "a"
    alpha.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", alpha=alpha)
    alpha.rmdir()  # registered, then removed underneath the deployment
    result = runner.invoke(
        app,
        ["mcp"],
        env={
            "VAULTRAG_STATE_DIR": str(tmp_path / "state"),
            "VAULTRAG_QDRANT_URL": "http://127.0.0.1:1",
        },
    )
    assert result.exit_code == 1
    assert "project 'alpha'" in result.output


# ---------------------------------------------------------------------------
# `project rm` clears BOTH derived caches (P3 Stage 2, found by dogfooding)
# ---------------------------------------------------------------------------
def test_project_rm_clears_the_vector_store_as_well_as_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this pins: deregistering cascaded SQLite and left the vectors behind.

    Orphaned points are not merely wasted space — re-registering the same id makes stale
    chunks searchable with no `documents` row behind them, which is precisely the drift
    `consistency.check_drift` exists to detect after the fact.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", doomed=vault)
    deleted: list[str] = []
    monkeypatch.setattr(
        QdrantVectorStore, "from_settings", classmethod(lambda cls, s: _FakeVectors(deleted))
    )

    result = runner.invoke(
        app,
        ["project", "rm", "doomed"],
        env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 0, result.output
    assert deleted == ["doomed"], "the vector half of the cascade must run"
    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    assert store.get_project("doomed") is None
    store.close()


def test_project_rm_keeps_the_project_registered_when_the_vector_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering, not just presence: vectors go first, so a failure leaves the project
    registered and the command re-runnable. Deregistering first would strand live vectors
    with nothing in the registry left to clean them up."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", doomed=vault)
    monkeypatch.setattr(
        QdrantVectorStore, "from_settings", classmethod(lambda cls, s: _FailingVectors())
    )

    result = runner.invoke(
        app,
        ["project", "rm", "doomed"],
        env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 1
    assert "still registered" in _unwrapped(result.output)
    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    assert store.get_project("doomed") is not None, "a failed cascade must not deregister"
    store.close()


def test_project_rm_keep_vectors_is_an_explicit_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch exists for a broken vector store, and says what it leaves behind."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state", doomed=vault)
    deleted: list[str] = []
    monkeypatch.setattr(
        QdrantVectorStore, "from_settings", classmethod(lambda cls, s: _FakeVectors(deleted))
    )

    result = runner.invoke(
        app,
        ["project", "rm", "doomed", "--keep-vectors"],
        env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 0, result.output
    assert deleted == []
    assert "orphaned" in _unwrapped(result.output)


def test_project_rm_of_an_unknown_project_touches_no_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existence is checked before anything is deleted anywhere."""
    (tmp_path / "state").mkdir()
    _register(tmp_path / "state")
    deleted: list[str] = []
    monkeypatch.setattr(
        QdrantVectorStore, "from_settings", classmethod(lambda cls, s: _FakeVectors(deleted))
    )
    result = runner.invoke(
        app,
        ["project", "rm", "ghost"],
        env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 1
    assert deleted == []


class _FakeVectors:
    """Records `delete_by_project` calls; never touches a real Qdrant."""

    def __init__(self, deleted: list[str]) -> None:
        self._deleted = deleted

    def delete_by_project(self, project_id: str) -> None:
        self._deleted.append(project_id)

    def close(self) -> None:
        pass


class _FailingVectors:
    def delete_by_project(self, project_id: str) -> None:
        raise VectorStoreError("qdrant is down")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# `vaultrag queries` — the mining CLI (P3 Stage 3)
# ---------------------------------------------------------------------------
def _log(state_dir: Path, project_id: str, query: str, *, hits: int, text: bool = True) -> None:
    """Write one query row directly, as a served search would."""
    store = SQLiteMetaStore(state_dir / "vaultrag.db")
    store.log_query(
        project_id=project_id,
        q_hash=q_hash(query),
        query_text=query if text else None,
        mode="snippets",
        k=10,
        latency_ms=12,
        n_results=hits,
        budget_used=200,
    )
    store.close()


def test_queries_reports_nothing_when_no_index_was_ever_built(tmp_path: Path) -> None:
    """Same rule the graph reader keeps: report on a db, never conjure one to report on."""
    result = runner.invoke(app, ["queries"], env={"VAULTRAG_STATE_DIR": str(tmp_path / "state")})
    assert result.exit_code == 1
    assert "no query log yet" in result.output
    assert not (tmp_path / "state" / "vaultrag.db").exists()


def test_queries_zero_hit_report_names_only_the_unanswered(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _log(state, "solo", "answered fine", hits=6)
    _log(state, "solo", "how do I rotate the signing key", hits=0)
    result = runner.invoke(
        app,
        ["queries", "--project", "solo", "--report", "zero-hit"],
        env={"VAULTRAG_STATE_DIR": str(state)},
    )
    assert result.exit_code == 0, result.output
    assert "how do I rotate the signing key" in result.output
    assert "answered fine" not in result.output


def test_queries_requires_project_when_several_are_registered(tmp_path: Path) -> None:
    """The MCP tools' rule, at the CLI: with several tenants there is no defensible default."""
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _register(state, alpha=tmp_path / "a", beta=tmp_path / "b")
    result = runner.invoke(app, ["queries"], env={"VAULTRAG_STATE_DIR": str(state)})
    assert result.exit_code != 0
    assert "alpha" in result.output and "beta" in result.output


def test_queries_export_writes_loadable_candidates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _log(state, "solo", "unanswerable thing", hits=0)
    out = tmp_path / "candidates.json"
    result = runner.invoke(
        app,
        ["queries", "--project", "solo", "--export", str(out)],
        env={"VAULTRAG_STATE_DIR": str(state)},
    )
    assert result.exit_code == 0, result.output
    assert [c.query for c in load_candidates(out).cases] == ["unanswerable thing"]


def test_queries_export_refuses_when_no_text_was_stored(tmp_path: Path) -> None:
    """The Decision-2 consequence, said out loud instead of writing an empty file."""
    state = tmp_path / "state"
    state.mkdir()
    _log(state, "solo", "unanswerable thing", hits=0, text=False)
    out = tmp_path / "candidates.json"
    result = runner.invoke(
        app,
        ["queries", "--project", "solo", "--export", str(out)],
        env={"VAULTRAG_STATE_DIR": str(state)},
    )
    assert result.exit_code == 1
    assert "VAULTRAG_QUERY_LOG_TEXT" in result.output
    assert not out.exists()


def test_queries_prune_applies_the_retention_caps(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for i in range(5):
        _log(state, "solo", f"q{i}", hits=1)
    result = runner.invoke(
        app,
        ["queries", "--project", "solo", "--prune"],
        env={"VAULTRAG_STATE_DIR": str(state), "VAULTRAG_QUERY_LOG_MAX_ROWS": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "pruned" in result.output
