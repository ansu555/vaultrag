---
title: Deduplicate on a client-supplied idempotency key
type: decision
status: accepted
tags: [decision, ingest, correctness]
created: 2024-03-19
---

# Deduplicate on a client-supplied idempotency key

## Context

Clients retry. A gateway timeout at 30 seconds does not tell the SDK whether the batch
landed, so it sends again, and the naive result is double-counted revenue on customer
dashboards. We saw a 0.4% duplication rate in the first month of the beta.

## Decision

Every event carries an `idempotency_key` chosen by the client — the SDK defaults to a
UUIDv7 minted when the event is created, not when it is sent, so a retry reuses the key.
The gateway keeps a 24-hour rolling set of seen keys in Redis and drops repeats before
they reach Kafka.

## Why the client chooses the key

A server-generated key cannot help: by the time the server sees the second copy it has no
way to know it is the same logical event. Content hashing was the alternative and it fails
on legitimately identical events — two users clicking the same button in the same second
produce byte-identical payloads and both are real.

## The 24-hour window

The window is a deliberate correctness/cost trade. Holding keys forever would need roughly
2 TB of Redis at current volume. Twenty-four hours covers every retry policy our SDKs
implement, with the longest being a 6-hour exponential backoff ceiling. Beyond the window
a duplicate is possible and we accept it; measured residual duplication is under 0.001%.

## Interaction with the storage layer

This is the reason [[storage-clickhouse-not-postgres]] can live without unique constraints.
Deduplication is a pipeline property, not a storage property, so the analytics store never
has to enforce it.

## Failure mode to watch

If Redis is unavailable the gateway **fails open** — it accepts the event and lets the
duplicate through rather than rejecting customer data. Duplicates are recoverable; refused
events are not. The `MER-1409` warning is emitted on every fail-open so the rate is visible.
