---
title: dead-letter-reaper
type: service
owner: pipeline-team
tags: [service, kafka, reliability]
created: 2024-06-11
---

# dead-letter-reaper

Consumes `events.dlq`, classifies why each record failed, and either repairs and replays it
or parks it for a human. Python, blue-green, low volume by design — a busy reaper means
something upstream is wrong.

## How records get here

A consumer that fails the same offset `MERIDIAN_POISON_THRESHOLD` times publishes the raw
bytes plus failure metadata here and commits past it. That mechanism is what stops one bad
record blocking a partition; the operator-facing procedure is [[poison-pill-message]].

## Classification

Three buckets. **Transient** — a downstream timeout or a ClickHouse write rejection; these
are replayed straight onto the original topic after a backoff, and they are the majority.
**Repairable** — a decode failure against a schema that has since been registered, which
happens when a producer raced ahead of registration; these decode successfully on the
second attempt and are replayed. **Terminal** — genuinely corrupt bytes, or an event whose
tenant no longer exists.

## Replay safety

Replay is only safe because every consumer is idempotent on
`(idempotency_key, stage)`. A record replayed after partial processing recomputes the same
result and overwrites it. Without that property this service would be a duplicate factory
rather than a repair tool — it is the clearest payoff from [[no-exactly-once-delivery]].

## Terminal records

Parked in a ClickHouse table with a 90-day TTL, the raw bytes, the failure reason, and the
originating partition and offset. There is a weekly review; anything with more than ten
occurrences of the same signature becomes a gateway validation rule so the record cannot be
produced again.

## Alerting

Page when the terminal rate exceeds 100 records per hour, or when the reaper's own lag
grows — a backed-up reaper means replays are not happening and transient failures are
silently aging out.
