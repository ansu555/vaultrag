---
title: rollup-worker
type: service
owner: analytics-team
tags: [service, clickhouse, analytics]
created: 2024-03-22
---

# rollup-worker

Consumes `events.enriched` and maintains the pre-aggregated tables the dashboards read.
Python, blue-green, the heaviest ClickHouse writer we run.

## Why pre-aggregate at all

Dashboard queries are known in advance — there are eleven distinct shapes across the whole
product. Computing them on read from raw events costs seconds even on a column store;
maintaining them on write costs a few milliseconds per event and turns the read into a
point lookup. [[storage-clickhouse-not-postgres]] made raw scans fast; rollups made them
unnecessary for the common path.

## Batching

Writes are batched to 200 000 rows or two seconds, whichever comes first. This is not a
throughput optimisation, it is a survival requirement: ClickHouse creates a part per
insert, and small frequent inserts generate more parts than the merge scheduler can keep
up with, which is the leading cause of [[clickhouse-disk-pressure]].

Never lower the batch size to improve freshness. The freshness objective in
[[slo-definitions]] is 60 seconds and a two-second batch is nowhere near the binding
constraint.

## Late events

An event can arrive with an `occurred_at` hours or days in the past — mobile clients buffer
offline. Rollups accept lateness up to seven days by rewriting the affected partition's
aggregate rows; beyond seven days the event lands in raw storage and is excluded from
rollups, and the dashboard footnote says so.

## Reading its own writes

A merge-tree aggregate is not immediately consistent after insert. The worker never reads
back its own output to make decisions; when verification is needed it goes through the
`FINAL` path described in [[clickhouse-final-modifier]], which is expensive and only used
in the blue-green output diff.

## Scaling

Bound by ClickHouse write throughput, not by CPU. Adding workers past six per region makes
the disk pressure problem worse rather than making rollups faster.
