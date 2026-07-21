---
title: Service level objectives
type: reference
tags: [reference, slo, oncall]
created: 2024-06-25
---

# Service level objectives

Four objectives, measured over a rolling 28-day window. Error budget burn above 2x
triggers a feature freeze on the owning service until the burn rate is back under 1x.

## Ingest availability

99.95% of authenticated `POST /v1/events` requests return a non-5xx response. Rate-limit
`429`s and validation `422`s are **not** failures — the service correctly refused a request
it was supposed to refuse. Counting them would create pressure to accept bad data.

Budget: 21 minutes of full downtime per 28 days.

## Ingest latency

p99 of the gateway's accept path under 150 ms, measured from first byte in to response
out, excluding client network time. The accept path ends when Kafka acknowledges; nothing
downstream is in this number.

## Freshness

99% of accepted events queryable within 60 seconds of acceptance, p99.9 within 300
seconds. This is the objective that consumer lag threatens, which is why
[[consumer-lag-spike]] is a sev2 rather than a sev3, and it is also what
[[clickhouse-disk-pressure]] ultimately protects.

Freshness is measured end to end by a synthetic event injected every ten seconds per
region and polled until it appears in `query-api`.

## Query latency

p95 of dashboard queries under 2 seconds, p99 under 8 seconds, measured server-side at
`query-api`. Queries scanning more than 90 days are excluded — they are an explicit
"slow query" product tier with its own expectations.

## What is deliberately not an SLO

Correctness is not expressed as an SLO because we do not accept a budget for it. Duplicate
or lost events are incidents, not budget spend. The pipeline's at-least-once contract
([[no-exactly-once-delivery]]) is a design property held to absolutely, not statistically.
