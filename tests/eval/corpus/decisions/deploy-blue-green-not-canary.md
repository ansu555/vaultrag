---
title: Blue-green releases for stateful consumers
type: decision
status: accepted
tags: [decision, deploy, infra]
created: 2024-06-14
---

# Blue-green releases for stateful consumers

## Context

We run canary deploys for the stateless HTTP tier and they work well. Applying the same
pattern to Kafka consumers did not: putting 5% of consumer instances on a new version
means both versions join the same consumer group, which triggers a rebalance on every
step of the rollout and leaves partitions shuttling between incompatible processing logic.

## Decision

Stateful consumers deploy blue-green at the **consumer group** level. The new version
starts as a second consumer group reading the same topics from the current offset, runs in
shadow mode writing to a scratch table, and gets promoted by an atomic table-name swap
once its output matches the incumbent's for thirty minutes.

## What this buys

Rollback is instant and total: swap the table names back. There is no partially-migrated
state, because the two versions never share a group and never write to the same table.
It also gives us a free correctness check — thirty minutes of output diffing has caught
four regressions that the test suite did not.

## What it costs

Double the consumer capacity during a release window, and roughly double the ClickHouse
write load for the shadow period. At current volume that is about ninety minutes of extra
compute per deploy, which we judged cheap against the rebalance storms documented in
[[kafka-rebalance-storms]].

## Scope

This applies to `enricher-service`, `rollup-worker`, and `dead-letter-reaper`. The
`gateway-service` and `query-api` stay on canary — they hold no partition assignment and
the canary signal is genuinely useful there.

The mechanics of a promotion are in [[region-failover]], which reuses the same swap.
