---
title: ClickHouse disk pressure
type: runbook
severity: sev2
tags: [runbook, oncall, clickhouse]
created: 2024-06-02
---

# ClickHouse disk pressure

Page fires at 80% disk utilisation on any analytics node, and hard-stops writes at 95%.

## Why it happens

Three causes account for nearly everything we have seen. Merges falling behind, so the
same data exists in many unmerged parts at once. A partition that should have been dropped
by TTL but was not, usually because the TTL move was blocked by a long-running mutation.
And a backfill writing months of history into partitions that were already compacted.

## Immediate relief

Do not delete data first. Start by checking `system.merges` for stuck or slow merges and
`system.mutations` for anything with `is_done = 0` and an old `create_time`. A mutation
stuck for hours holds parts that cannot be merged or dropped, and killing it with
`KILL MUTATION` frequently releases a large fraction of the disk within twenty minutes.

If merges are simply behind, raise `background_pool_size` temporarily. It is a server
setting and takes effect without restart.

## Buying real space

Detached parts are the usual free win — `system.detached_parts` accumulates after failed
merges and nothing cleans it automatically. `DROP DETACHED PARTITION` on anything older
than a week is safe.

Next, check that TTL is actually moving cold partitions to object storage. `SELECT * FROM
system.parts WHERE disk_name = 'default' AND min_date < today() - 30` should be empty; if
it is not, the TTL move is not running and `ALTER TABLE ... MOVE PARTITION` does it by
hand.

## Writes already stopped

If writes are rejected the pipeline is buffering in Kafka, which is the design working as
intended — you have seven days of retention to fix this before data is lost, and
[[consumer-lag-spike]] will be paging in parallel. Resist the urge to drop recent
partitions to restore writes; recent data is the data customers are looking at.

## Prevention

The capacity model assumes 40% headroom for merges. Any backfill above 500 GB needs to be
throttled to run overnight and announced in `#meridian-ops`. See [[slo-definitions]] for
the freshness SLO this runbook is protecting.
