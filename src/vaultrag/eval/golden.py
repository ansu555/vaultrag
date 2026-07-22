"""Golden-set loading — the ``(query → expected note)`` pairs the gate scores against.

Two sets ship, both read by this module (Decision 2 of
``../Rag/projects/vaultrag/p2-step-plan.md``):

* ``tests/eval/corpus_golden.json`` over the committed fixture vault
  (``tests/eval/corpus/``) — **the gate**. Hermetic and frozen, so a number computed today
  is comparable to one computed six months from now. Coupling the gate to the design vault
  instead would mean every note Anik edits silently moves the bar.
* ``tests/eval/golden.json`` over the live ``vaultrag-design`` vault — **dogfooding**. Runs
  against whatever is really indexed; drifts on purpose, because its job is to tell us how
  retrieval feels on the vault we actually use.

One harness, two sets. A case is deliberately minimal: a query and the vault-relative path
of the note that answers it. ``expected_paths`` (plural) is accepted for a question with
more than one right answer; ``expected_path`` stays the common single-answer spelling.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# tests/eval/ — data lives with the tests, the harness lives in src so the CLI can import
# it. Resolved relative to this file so `vaultrag eval` works from any working directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_DATA_DIR = _REPO_ROOT / "tests" / "eval"
CORPUS_GOLDEN_PATH = EVAL_DATA_DIR / "corpus_golden.json"
CORPUS_VAULT_PATH = EVAL_DATA_DIR / "corpus"
DESIGN_GOLDEN_PATH = EVAL_DATA_DIR / "golden.json"
VECTOR_FIXTURE_PATH = EVAL_DATA_DIR / "vectors.db"


class GoldenSetError(ValueError):
    """A golden file is missing, malformed, or points at notes that do not exist."""


@dataclass(frozen=True)
class Case:
    """One scored query."""

    query: str
    expected: frozenset[str]
    note: str = ""  # optional: why this case exists / what it probes

    @property
    def expected_path(self) -> str:
        """The single expected path (first, for a multi-answer case) — for display."""
        return sorted(self.expected)[0]


@dataclass(frozen=True)
class GoldenSet:
    """A named set of cases, plus where its corpus lives."""

    name: str
    project_id: str
    cases: list[Case]
    vault_path: Path | None = None  # set for the hermetic corpus set; None = live vault
    description: str = ""
    source: Path | None = field(default=None, compare=False)

    def __len__(self) -> int:
        return len(self.cases)


def load_golden(path: Path, *, require_expected: bool = True) -> GoldenSet:
    """Read and validate a golden JSON file.

    Raises :class:`GoldenSetError` rather than returning a partial set: a gate that
    silently skips malformed cases scores a smaller, easier set and still reports a pass.

    ``require_expected=False`` reads a **candidate** file — the query-log export (P3
    Stage 3), whose cases are real queries with the ``expected_paths`` still blank because
    only a human can say which note *should* have answered a query that returned nothing.
    Candidates use this same schema on purpose: filling the blanks in place turns the file
    into a golden set with no reformatting, and until they are filled the strict load
    refuses it by name rather than scoring an unanswerable case as a miss.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoldenSetError(f"golden set not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"golden set is not valid JSON ({path}): {exc}") from exc

    entries = raw.get("cases")
    if not isinstance(entries, list) or not entries:
        raise GoldenSetError(f"golden set has no cases: {path}")

    cases: list[Case] = []
    for i, entry in enumerate(entries):
        query = entry.get("query")
        if not isinstance(query, str) or not query.strip():
            raise GoldenSetError(f"case {i} in {path} has no query")
        expected = entry.get("expected_paths") or (
            [entry["expected_path"]] if entry.get("expected_path") else []
        )
        if not expected and require_expected:
            raise GoldenSetError(f"case {i} in {path} ({query!r}) has no expected path")
        cases.append(
            Case(
                query=query,
                expected=frozenset(str(p) for p in expected),
                note=str(entry.get("note", "")),
            )
        )

    vault = raw.get("vault_path")
    return GoldenSet(
        name=str(raw.get("name", path.stem)),
        project_id=str(raw.get("project", "default")),
        cases=cases,
        vault_path=(path.parent / vault).resolve() if vault else None,
        description=str(raw.get("description", "")),
        source=path,
    )


def load_corpus_golden() -> GoldenSet:
    """The hermetic gate set over the committed fixture vault."""
    return load_golden(CORPUS_GOLDEN_PATH)


def load_design_golden() -> GoldenSet:
    """The dogfooding set over the live ``vaultrag-design`` vault."""
    return load_golden(DESIGN_GOLDEN_PATH)


def load_candidates(path: Path) -> GoldenSet:
    """Read a query-log candidate file — a golden set whose expected paths aren't filled yet."""
    return load_golden(path, require_expected=False)


def write_candidates(
    path: Path,
    *,
    project_id: str,
    candidates: Sequence[tuple[str, str]],
    description: str = "",
) -> int:
    """Write mined queries as a golden-set-shaped candidate file. Returns how many.

    ``candidates`` is ``(query, note)`` pairs — the note records *why* the query was
    mined (zero-hit, how often, when), because six weeks later "why is this in here?" is
    the question that decides whether a case is worth writing an answer for.

    The file is deliberately **not** merged into an existing golden set. The gate's set is
    a frozen instrument (``../Rag/projects/vaultrag/evaluation-framework.md``); appending
    unreviewed cases to it would move the bar by accident, which is the one thing a gate
    must never do. Review, fill in ``expected_paths``, then copy them across.
    """
    payload = {
        "name": f"{project_id}-candidates",
        "project": project_id,
        "description": description
        or "Mined from query_log. Fill in expected_paths for each case you want to keep, "
        "delete the rest, then merge into a golden set by hand.",
        "generated_ts": int(time.time()),
        "cases": [
            {"query": query, "expected_paths": [], "note": note} for query, note in candidates
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(payload["cases"])  # type: ignore[arg-type]


def verify_paths(golden: GoldenSet) -> list[str]:
    """Return every expected path that does not exist under the set's vault.

    Only meaningful for a set with a ``vault_path`` (the fixture corpus). A typo in an
    expected path makes a case permanently unscoreable — it can never be retrieved — which
    quietly lowers the ceiling of the whole gate, so the corpus test asserts this is empty.
    """
    if golden.vault_path is None:
        return []
    missing = []
    for case in golden.cases:
        for path in sorted(case.expected):
            if not (golden.vault_path / path).is_file():
                missing.append(f"{path} (case: {case.query!r})")
    return missing
