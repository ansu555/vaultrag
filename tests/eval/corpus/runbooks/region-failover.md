---
title: Regional failover
type: runbook
severity: sev1
tags: [runbook, oncall, infra]
created: 2024-08-03
---

# Regional failover

Move ingest and query serving out of a degraded region. This is a sev1 procedure and needs
an incident commander before step one.

## Decision to fail over

Failing over costs roughly forty minutes of degraded freshness and carries its own risk,
so it is not the first response to a regional wobble. Trigger it when the region has been
hard-down for more than fifteen minutes, or when a partial failure is causing data loss
rather than latency. A slow region is usually better than a mid-flight migration.

## Ingest first

DNS for `ingest.meridian.example` has a 30-second TTL and weighted records per region.
Set the failing region's weight to zero. Clients reconnect within a minute; SDKs already
retry, so in-flight batches land in the surviving region and idempotency keys stop the
duplicates ([[dedupe-by-idempotency-key]]).

Do not stop the failing region's consumers yet. If its Kafka cluster is reachable it
should keep draining what it already accepted.

## Then consumers and queries

Consumer groups in the surviving region are already subscribed to the mirrored topics. Use
the same atomic table swap as a normal promotion — the mechanism is
[[deploy-blue-green-not-canary]] — pointing `query-api` at the surviving region's tables.

Expect freshness to breach the SLO in [[slo-definitions]] for the duration. Post the
breach in the status page before customers ask.

## Coming back

Return traffic gradually: 10%, then 50%, then full, with fifteen minutes at each step
watching error rate and lag. The recovered region's consumers must catch up on their
backlog **before** they take query traffic, or dashboards will show a hole. Verify with
`meridian_consumer_lag_seconds` under 60 across every group in that region.

## Known gap

Cross-region mirroring lags by up to ninety seconds under load. Events accepted in the
failing region within that window and not yet mirrored are recovered when the region
returns, not during the failover. We accept a temporary gap rather than blocking ingest on
a synchronous cross-region write.
