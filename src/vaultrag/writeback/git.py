"""Best-effort per-write git commit (P1 Stage 5).

Each accepted write auto-commits to the *vault's own* git repo — a free audit trail with
rollback, diff, and blame, and (with a private remote) an offsite backup
(``../Rag/projects/vaultrag/write-back-design.md``; ``reference-security-model.md`` "git
audit"). **Best-effort by design:** a vault that isn't a git repo, or a box without
``git`` on ``PATH``, still gets the write and the index — the commit is simply skipped and
reported (``committed=False``). The vault is the source of truth, not git; a missing commit
loses an audit line, never a note.

Commits are attributed to the **agent** identity (``user.name = author``,
``user.email = <author>@vaultrag.local``) via ``-c`` overrides for this one invocation, so
agent writes stand out from the human's commits in ``git log`` regardless of the repo's
configured user — the same provenance goal the ``author:`` frontmatter serves. Nothing is
written to the repo's git config.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_GIT_TIMEOUT = 15  # seconds; a hung git must never wedge a write tool.


def _run(vault_root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Run ``git <args>`` in ``vault_root``; ``None`` if git is missing or times out.

    Never raises: a missing binary (``FileNotFoundError``) or a hung command
    (``TimeoutExpired``) degrades to "no commit", the same as any other best-effort miss.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=vault_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("writeback.git_unavailable", error=str(exc))
        return None


def is_git_repo(vault_root: Path) -> bool:
    """True if ``vault_root`` is inside a git work tree (and git is available)."""
    result = _run(vault_root, ["rev-parse", "--is-inside-work-tree"])
    return result is not None and result.returncode == 0 and result.stdout.strip() == "true"


def commit_paths(
    vault_root: Path,
    rel_paths: Sequence[str],
    *,
    message: str,
    author: str,
) -> str | None:
    """Stage + commit exactly ``rel_paths`` with ``message``; return the short SHA or ``None``.

    ``None`` (no commit) is a normal outcome, not an error: the vault isn't a git repo, git
    is absent, or the content was identical to HEAD (nothing to commit). Only ``rel_paths``
    are committed (``commit -- <paths>``), so a write never sweeps in unrelated working-tree
    changes the human hasn't staged.
    """
    if not rel_paths:
        return None
    if not is_git_repo(vault_root):
        log.info("writeback.git_skipped", reason="not-a-git-repo", vault=str(vault_root))
        return None

    email = f"{author}@vaultrag.local"
    add = _run(vault_root, ["add", "--", *rel_paths])
    if add is None or add.returncode != 0:
        log.warning(
            "writeback.git_add_failed", error=(add.stderr.strip() if add else "git-missing")
        )
        return None

    commit = _run(
        vault_root,
        [
            "-c",
            f"user.name={author}",
            "-c",
            f"user.email={email}",
            "commit",
            "--only",
            "-m",
            message,
            "--",
            *rel_paths,
        ],
    )
    if commit is None or commit.returncode != 0:
        # Most common non-error case: identical content → "nothing to commit". Log at info,
        # not warning — the write still happened, there was just no diff to record.
        detail = commit.stderr.strip() or commit.stdout.strip() if commit else "git-missing"
        log.info("writeback.git_no_commit", detail=detail[:200])
        return None

    head = _run(vault_root, ["rev-parse", "--short", "HEAD"])
    if head is None or head.returncode != 0:
        return None
    return head.stdout.strip() or None
