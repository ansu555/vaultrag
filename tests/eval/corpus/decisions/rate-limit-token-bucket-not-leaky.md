---
title: Token bucket for tenant rate limiting
type: decision
status: accepted
tags: [decision, gateway, reliability]
created: 2024-05-08
---

# Token bucket for tenant rate limiting

## Context

A single tenant replaying six months of history saturated the gateway and pushed p99
latency for everyone else from 40 ms to 4 seconds. We needed per-tenant admission control
that tolerated legitimate bursts without letting one account monopolise capacity.

## Decision

Per-tenant token buckets held in Redis, refilled at the contracted steady rate with a
burst capacity of sixty seconds' worth of tokens. Over-limit requests get `429` with a
`Retry-After` header and the `MER-1429` code.

## Why not a leaky bucket

A leaky bucket smooths output to a constant rate, which is exactly wrong for our traffic
shape. Analytics events arrive in genuine bursts — a customer's marketing email goes out
and traffic goes up twentyfold for ninety seconds. A leaky bucket would shed that traffic
even though the tenant is well inside their daily contract. The token bucket accumulates
allowance while they are quiet and spends it when they are busy, which matches how the
contract is actually written.

## Why not a sliding window counter

Sliding windows are simpler and we prototyped one. It needed a Redis round trip per
request plus a sorted-set trim, roughly 3x the cost of the bucket's single Lua script, and
it still had a boundary artefact where a tenant could push 2x their limit across a window
edge.

## Fail-open, deliberately

If Redis is unreachable the limiter admits the request. This mirrors the reasoning in
[[dedupe-by-idempotency-key]]: refusing a customer's data because our own cache is down
turns our outage into their data loss. The gateway sheds load through backpressure at the
connection level instead, and the runbook for that is [[consumer-lag-spike]].

## Tuning

Steady rate and burst are per-tenant columns in Postgres and take effect within thirty
seconds of an update. The defaults for new accounts are 2000 events per second steady,
120000 burst.
