---
title: Meridian error codes
type: reference
tags: [reference, errors]
created: 2024-04-30
---

# Meridian error codes

Every error surfaced to a client or written to a structured log carries a stable
`MER-nnnn` code. Codes are never reused or renumbered.

## 1xxx — gateway and client errors

| Code | HTTP | Meaning |
|---|---|---|
| `MER-1400` | 400 | Malformed request body; not valid JSON |
| `MER-1401` | 401 | Signature verification failed, or timestamp outside the five-minute window |
| `MER-1403` | 403 | Valid signature, but the tenant is suspended or lacks the scope |
| `MER-1409` | 202 | Fail-open duplicate accepted because the dedupe cache was unreachable |
| `MER-1413` | 413 | Batch exceeds 5 MB, or a single event exceeds 256 KB |
| `MER-1422` | 422 | Event failed schema validation; the offending field path is in `detail` |
| `MER-1429` | 429 | Tenant token bucket exhausted; honour `Retry-After` |

`MER-1409` is a warning rather than a failure — the event was accepted. It is listed here
because it appears in client logs and support tickets ask about it.

## 2xxx — serialisation and schema

| Code | Meaning |
|---|---|
| `MER-2001` | Avro magic byte missing or wrong; producer bypassed the registry |
| `MER-2002` | Schema id not resolvable against the registry |
| `MER-2003` | Decode failed against a resolvable schema; likely truncated payload |
| `MER-2010` | Schema registration refused for backward incompatibility |

The `MER-2xxx` family on a consumer is the signature of a poison pill; see
[[poison-pill-message]].

## 3xxx — enrichment

| Code | Meaning |
|---|---|
| `MER-3001` | Customer enrichment webhook timed out after 2 seconds |
| `MER-3002` | Enrichment webhook returned a non-2xx status |
| `MER-3005` | GeoIP database older than 30 days; enrichment proceeded with stale data |

## 5xxx — internal and capacity

| Code | Meaning |
|---|---|
| `MER-5001` | ClickHouse write rejected; disk above the hard limit |
| `MER-5002` | Kafka produce failed after all retries; the request was not accepted |
| `MER-5003` | Consumer lag exceeded 72 hours; retention loss is approaching |

`MER-5002` is the only internal code that ever reaches a client, as a `503`. Everything
else in this family is log-only.
