---
title: gateway-service
type: service
owner: ingest-team
tags: [service, gateway, ingest]
created: 2024-03-01
---

# gateway-service

The public edge. Terminates TLS, authenticates, validates, deduplicates, and produces to
Kafka. Written in Go, deployed on canary, stateless apart from its Redis connection.

## Request path

Signature verification runs before body parsing so an unauthenticated caller cannot make
us allocate. Then batch size checks, per-event schema validation against the tenant's
declared event set, dedupe lookup, Avro encoding, and a produce with `acks=all`.

The whole path targets 150 ms at p99 ([[slo-definitions]]). Roughly 60% of that is the
Kafka acknowledgement, which is why `acks=all` is the one setting nobody is allowed to
weaken for latency.

## Why it produces synchronously

The gateway does not return `202` until Kafka has acknowledged. An earlier design buffered
in memory and acknowledged immediately, which was faster and lost data on every pod
eviction. Accepting an event is a promise that it is durable, so the acknowledgement waits.

## Redis dependency

Used for dedupe ([[dedupe-by-idempotency-key]]) and rate limiting
([[rate-limit-token-bucket-not-leaky]]). Both fail open. Redis being down degrades
correctness slightly and never stops ingest, which is the trade the service is built
around.

## Scaling

CPU-bound on signature verification and Avro encoding, roughly 4000 events per second per
core. Horizontal scaling is linear and the HPA targets 60% CPU. There is no partition
affinity, so instances are fully interchangeable.

## Known sharp edge

The tenant event-set cache refreshes every 30 seconds. A customer who declares a new event
name and immediately sends it can see `MER-1422` for up to half a minute. Documented in
[[ingest-api]]; the fix is a registry push rather than a poll and it is not scheduled.
