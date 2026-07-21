---
title: FINAL is not free and not always right
type: til
tags: [til, clickhouse]
created: 2024-07-19
---

# FINAL is not free and not always right

Spent an afternoon on a report that showed different numbers on every refresh, and the
answer was a misunderstanding of how `ReplacingMergeTree` behaves before a merge runs.

## What I assumed

That inserting a row with an existing sort key replaces the old one. It does not — not
immediately. The old and new rows coexist as separate parts until a background merge
happens, and merges are asynchronous with no schedule you can rely on. A `SELECT` without
`FINAL` sees both.

## What `FINAL` actually does

It applies the merge logic at query time, so you get the deduplicated view. Correct, but
it disables several optimisations and forces a merge-sorted read across all parts covering
the query range. On our largest table a query went from 200 ms to 14 seconds.

It also does not make the query transactional. A concurrent insert can still land between
the planning and the read.

## What we do instead

`rollup-worker` never reads back its own writes to make a decision — it keeps whatever
state it needs in the consumer's own memory and offsets. Where a deduplicated read is
genuinely required, the aggregate is expressed with `argMax(value, version)` and a
`GROUP BY` on the sort key, which gets the same answer with normal optimisations intact
and costs roughly 1.2x a plain scan rather than 70x.

The one place `FINAL` is still used is the blue-green output diff
([[deploy-blue-green-not-canary]]), where it runs once against a bounded window and
correctness matters far more than the fourteen seconds.

## Takeaway

Eventual consistency in the storage engine is not a detail you can leave to the storage
engine. It has to show up in how the application reads.
