"""Deprecated shim — the eval harness moved to ``vaultrag.eval`` in P2 Stage 1.

The P1 script that lived here was folded into :mod:`vaultrag.eval` so the CLI could import
it (a console script cannot import from the test tree), and grew nDCG@10, A/B comparison
and a merge-blocking gate along the way. The data it used still lives beside this file.

    uv run vaultrag eval                    # hermetic gate over tests/eval/corpus/
    uv run vaultrag eval --golden design    # what this script used to do

Kept as a shim because ``../Rag/projects/vaultrag/p1-step-plan.md`` documents the old
invocation.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "tests.eval.run_eval has been replaced by the `vaultrag eval` subcommand.\n"
        "\n"
        "  uv run vaultrag eval                  # hermetic gate (no Qdrant/Ollama needed)\n"
        "  uv run vaultrag eval --golden design  # the live design-vault run this did\n"
        "  uv run vaultrag eval --help           # A/B, --record, --show-cases\n",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
