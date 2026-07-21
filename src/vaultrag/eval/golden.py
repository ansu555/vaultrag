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


def load_golden(path: Path) -> GoldenSet:
    """Read and validate a golden JSON file.

    Raises :class:`GoldenSetError` rather than returning a partial set: a gate that
    silently skips malformed cases scores a smaller, easier set and still reports a pass.
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
        if not expected:
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
