---
title: enricher-service
type: service
owner: pipeline-team
tags: [service, enrichment, kafka]
created: 2024-03-08
---

# enricher-service

Consumes `events.raw`, attaches derived attributes, produces to `events.enriched`. Python,
asyncio, deployed blue-green ([[deploy-blue-green-not-canary]]).

## What it adds

GeoIP resolution from the source address, user-agent parsing into device and browser
fields, a session id computed from a 30-minute inactivity gap per user, and optionally a
call to the tenant's own enrichment webhook.

Session computation is the reason this stage needs per-tenant ordering, and therefore the
reason the topic is partitioned by tenant hash rather than round-robin
([[queue-kafka-not-sqs]]).

## The webhook problem

A tenant-supplied webhook is an arbitrary external call inside our hot loop. It has a 2
second timeout (`MER-3001`), three retries with jitter, and a per-tenant circuit breaker
that opens after 20 consecutive failures and stays open for five minutes.

When a breaker is open the enricher emits events without the custom fields rather than
stalling the partition. A tenant's broken webhook must never become our incident, and this
is the single most common cause of lag investigations that turn out not to be our fault —
[[consumer-lag-spike]] lists it second.

## Idempotency

Every write is keyed on `(idempotency_key, "enrich")`, so replaying a partition recomputes
the same output and overwrites it harmlessly. This is what makes at-least-once acceptable
([[no-exactly-once-delivery]]).

## Resource shape

Memory-heavy rather than CPU-heavy: the GeoIP database is 1.2 GB resident and the
user-agent rules another 300 MB. Instances take about 40 seconds to become ready, which is
long enough that it materially affects rebalance behaviour ([[kafka-rebalance-storms]]).

## Stale GeoIP

The database refreshes weekly from a cron sidecar. Past 30 days it emits `MER-3005` on
every event and enriches with stale data rather than failing — wrong country is better
than no event.
