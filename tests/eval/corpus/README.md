---
title: Meridian Engineering Notes
type: note
tags: [meta]
---

# Meridian Engineering Notes

A **synthetic** engineering knowledge base used as the frozen corpus for VaultRAG's
retrieval quality gate. Meridian is a fictional event-ingestion and analytics platform;
nothing here describes a real system, and no note contains real credentials.

This corpus exists so the eval gate is hermetic and stable. The design vault at `../Rag`
drifts every time we edit it, which would make the gate's numbers un-comparable across
commits — see `../Rag/projects/vaultrag/p2-step-plan.md`, Decision 2.

It is deliberately *harder* than a title-matchy set:

- several notes cover neighbouring topics, so ranking has to discriminate rather than
  keyword-match a unique title;
- many golden queries are paraphrases that share **no** distinctive token with the
  expected note's title;
- identifiers, error codes and env-var names appear so the BM25 half of hybrid search is
  actually exercised.

## Layout

- `decisions/` — architecture decisions with rationale and rejected alternatives
- `runbooks/` — operational procedures for on-call
- `reference/` — stable technical shapes (error codes, API, config, schema, SLOs)
- `services/` — per-service design notes
- `til/` — short "today I learned" findings
