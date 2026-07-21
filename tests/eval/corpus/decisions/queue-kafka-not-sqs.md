---
title: Kafka over SQS for the ingest buffer
type: decision
status: accepted
tags: [decision, ingest, infra]
created: 2024-02-11
---

# Kafka over SQS for the ingest buffer

## Context

The gateway accepts bursts of up to 400k events per second during customer batch uploads,
but the enricher and rollup stages sustain roughly 90k/s. Something has to absorb the
difference, and it has to survive a full-day outage of the downstream stages without
dropping data.

## Decision

Use a self-managed Kafka cluster as the durable buffer between `gateway-service` and
every downstream consumer. Topics are partitioned by tenant hash, retention is seven days,
and replication factor is three across availability zones.

## Why not SQS

SQS was the obvious managed alternative and we rejected it for three reasons.

First, replay. SQS deletes a message once it is acknowledged, so recovering from a bad
deploy of the enricher means re-uploading the source data from customers. Kafka's offset
model lets us rewind a consumer group to any point inside the retention window, which we
have already used twice to reprocess mis-enriched events.

Second, fan-out. Four independent consumer groups read the same event stream. With SQS
that means an SNS topic plus a queue per consumer, and per-message costs multiply by the
number of consumers. Kafka charges us once for storage regardless of how many groups read.

Third, ordering. Rollups depend on per-tenant ordering to compute session boundaries.
SQS FIFO queues cap out at 3000 messages per second with batching, two orders of magnitude
below what we need.

## Costs we accepted

We now operate a stateful cluster, which is real toil: broker replacement, partition
rebalancing, and the rebalance storms described in [[kafka-rebalance-storms]]. We judged
that acceptable because the replay capability is load-bearing for our recovery story.

## Revisit when

Sustained peak drops below 5k events per second, or a managed Kafka offering lands in our
compliance region at less than 1.4x our current infrastructure spend.
