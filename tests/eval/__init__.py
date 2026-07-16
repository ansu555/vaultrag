"""Minimal retrieval-quality harness (P1 Stages 1-2, decision 3).

A hand-run subset of ``../Rag/projects/vaultrag/evaluation-framework.md`` — the full
golden set + merge-blocking CI gate is P2. Nothing here is collected by pytest (no
``test_*`` modules): the harness needs a live Qdrant + Ollama + the reranker model, which
CI does not have. Run it by hand around a retrieval change::

    uv run python -m tests.eval.run_eval           # fusion vs reranked, side by side
"""
