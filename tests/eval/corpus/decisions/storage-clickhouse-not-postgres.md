---
title: ClickHouse for the analytics store
type: decision
status: accepted
tags: [decision, storage, analytics]
created: 2024-03-04
---

# ClickHouse for the analytics store

## Context

Customer-facing dashboards run wide aggregations over 90 days of event history — count
distinct users by country by hour, funnel conversion across six steps, retention cohorts.
On Postgres these took 40 to 200 seconds even with materialised views, and the dashboards
have a 2 second budget.

## Decision

Move the analytics tables to ClickHouse with a `MergeTree` family engine, partitioned by
month and ordered by `(tenant_id, event_time)`. Postgres remains the system of record for
tenant configuration, billing, and anything that needs a transaction.

## Why column storage won

The dashboard queries touch three or four columns out of ninety. A row store reads whole
rows off disk to answer them; a column store reads only what the query names. On our
benchmark the same cohort query went from 96 seconds to 340 milliseconds, and compression
took the 90-day working set from 4.1 TB to 380 GB.

## What we gave up

There are no real transactions and no enforced uniqueness. Deduplication happens at write
time in the pipeline instead — see [[dedupe-by-idempotency-key]]. Updates are asynchronous
merges rather than in-place edits, which is why reading freshly written rows needs care;
[[clickhouse-final-modifier]] covers the trap we hit there.

Joins across large tables are also markedly worse than Postgres. We denormalise on write
so the read path rarely joins at all.

## Two stores, one truth

Running two databases is a real cost. We keep the rule simple: if losing a row would cost
money or break a contract it lives in Postgres, and ClickHouse is a derived analytical copy
that can be rebuilt from Kafka within six hours.
