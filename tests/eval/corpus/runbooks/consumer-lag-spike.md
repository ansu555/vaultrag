---
title: Consumer lag spike
type: runbook
severity: sev2
tags: [runbook, oncall, kafka]
created: 2024-05-30
---

# Consumer lag spike

Page fires when `meridian_consumer_lag_seconds` for any group exceeds 300 for five minutes.

## First look

Check whether lag is growing on one partition or all of them. `kafka-consumer-groups
--describe --group enricher-v4` prints per-partition lag. One hot partition means a tenant
key is skewed or a single message is stuck; all partitions climbing together means the
consumer as a whole cannot keep up or has stopped.

Then check whether consumers are alive. A group with fewer members than expected is
usually mid-rebalance, and a rebalance that will not settle is its own failure mode —
see [[kafka-rebalance-storms]] before you restart anything, because restarting is what
keeps it going.

## Single stuck partition

Almost always a message the consumer cannot process, retrying forever. Confirm by looking
for the same offset repeating in the consumer log, then follow [[poison-pill-message]] to
route it to the dead letter topic. Do not skip the offset manually unless the runbook
there says to; you will lose whatever else is in that batch.

## Uniform lag growth

Look at input rate first. A tenant replaying history can legitimately triple the stream —
check the gateway's per-tenant accept rate before assuming the consumer is broken. If one
tenant is responsible, tightening their token bucket is the fast lever
([[rate-limit-token-bucket-not-leaky]] explains the knobs) and buys time without dropping
data, because Kafka is still holding everything.

If input is normal, the consumer got slower. Common causes in order of frequency:
ClickHouse write latency (check [[clickhouse-disk-pressure]]), a slow customer enrichment
webhook holding the enricher's worker pool, and a deploy that landed in the last hour.

## Scaling up

Adding consumer instances only helps up to the partition count — a group can never have
more useful members than partitions. Current topic `events.raw` has 240 partitions, so
there is headroom. Scale in steps of ten and wait for each rebalance to settle before the
next step.

## When to escalate

Lag above 3600 seconds, or any lag growth that continues after the consumer has been
scaled to partition count. At seven days of lag we begin losing data to retention, and the
`MER-5003` alert fires at 72 hours as the last warning before that becomes real.
