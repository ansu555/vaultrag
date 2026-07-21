---
title: Service configuration variables
type: reference
tags: [reference, config, infra]
created: 2024-05-20
---

# Service configuration variables

Every service reads configuration from the environment with the `MERIDIAN_` prefix. There
are no configuration files in the container images; a value that is not in the environment
takes its documented default.

## Shared by all services

| Variable | Default | Notes |
|---|---|---|
| `MERIDIAN_LOG_LEVEL` | `info` | `debug` is never enabled in production for more than an hour |
| `MERIDIAN_REGION` | — | Required. Used in metric labels and failover routing |
| `MERIDIAN_KAFKA_BROKERS` | — | Required. Comma-separated `host:port` list |
| `MERIDIAN_SCHEMA_REGISTRY_URL` | — | Required except for `query-api` |
| `MERIDIAN_OTEL_ENDPOINT` | unset | Tracing is off when unset |

## Gateway

| Variable | Default | Notes |
|---|---|---|
| `MERIDIAN_MAX_BATCH_BYTES` | `5242880` | Raising this without raising the Kafka message limit produces `MER-5002` |
| `MERIDIAN_SIGNATURE_SKEW_SECONDS` | `300` | The replay tolerance window |
| `MERIDIAN_DEDUPE_REDIS_URL` | — | Unreachable means fail-open and `MER-1409` |
| `MERIDIAN_DEDUPE_TTL_SECONDS` | `86400` | The 24-hour window |

## Consumers

| Variable | Default | Notes |
|---|---|---|
| `MERIDIAN_CONSUMER_GROUP` | per service | Overridden during a blue-green promotion |
| `MERIDIAN_MAX_POLL_RECORDS` | `500` | Lowering it helps a consumer that is being kicked mid-batch |
| `MERIDIAN_POISON_THRESHOLD` | `3` | Consecutive failures on one offset before routing to the dead letter topic |
| `MERIDIAN_CLICKHOUSE_DSN` | — | Required for `rollup-worker` and `enricher-service` |

## A note on `MERIDIAN_MAX_POLL_RECORDS`

This is the first knob to reach for when a consumer is being evicted from its group during
long batches. Fewer records per poll means the poll loop returns to the coordinator more
often and the session heartbeat is not starved. See [[kafka-rebalance-storms]] for why the
default is not lower.
