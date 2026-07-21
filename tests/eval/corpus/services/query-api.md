---
title: query-api
type: service
owner: analytics-team
tags: [service, api, clickhouse]
created: 2024-04-05
---

# query-api

Serves customer dashboards and the public reporting API. Go, canary-deployed, stateless.
The only service that talks to ClickHouse on the read side.

## Query construction

Customers do not write SQL. They compose reports from a typed query DSL, and the service
translates that into one of eleven parameterised ClickHouse statements — the same shapes
`rollup-worker` maintains tables for. Anything the DSL cannot express is not exposed,
which keeps the query planner's behaviour predictable and makes SQL injection structurally
impossible rather than filtered.

## Tenant isolation

Every generated query carries a `tenant_id` predicate injected from the authenticated
session, appended after DSL translation so no client input can influence it. A query
without that predicate fails a pre-execution assertion and raises an internal alert; the
assertion has never fired in production and is deliberately kept.

## Caching

Results cache for 30 seconds keyed on the full normalised query plus tenant. That is short
enough to stay inside the freshness objective in [[slo-definitions]] and long enough to
absorb the dashboard refresh stampede when a customer leaves a wall display running.

## Slow queries

Anything scanning more than 90 days routes to a separate ClickHouse user with a lower
`max_execution_time` and its own concurrency pool, so an expensive report cannot starve
interactive dashboards. Those queries are excluded from the p95 latency objective.

## Failover behaviour

During a regional failover the service is repointed at the surviving region's tables by
the table swap in [[region-failover]]. It holds no consumer group and no local state, so
it is the easiest component to move — which is why it moves last, after the data it reads
is confirmed current.
