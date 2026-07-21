---
title: Ingest API reference
type: reference
tags: [reference, api, gateway]
created: 2024-04-12
---

# Ingest API reference

Base URL `https://ingest.meridian.example`. All endpoints require an HMAC signature; see
[[auth-hmac-not-oauth-for-ingest]] for the scheme and [[credential-rotation]] for secrets.

## POST /v1/events

Accepts a batch of one to 1000 events as a JSON array.

```http
POST /v1/events HTTP/1.1
Content-Type: application/json
X-Meridian-Timestamp: 1719849600
X-Meridian-Signature: v1=3f9c...c21a
```

```json
[
  {
    "idempotency_key": "018fb2e1-6c1a-7c33-9a41-6b0b9f1d2e77",
    "event_name": "checkout_completed",
    "occurred_at": "2024-07-01T12:00:00.123Z",
    "user_id": "u_88213",
    "properties": {"cart_value_cents": 4990, "currency": "EUR"}
  }
]
```

Responses are `202 Accepted` with a per-event result array, never `200`. Acceptance means
the event reached Kafka, not that it has been processed — the pipeline is at-least-once and
asynchronous by design ([[no-exactly-once-delivery]]).

## Limits

A batch may not exceed 5 MB or 1000 events; a single event may not exceed 256 KB. Over
either limit returns `MER-1413`. The timestamp header must be within five minutes of
server time. Rate limits are per tenant and documented in
[[rate-limit-token-bucket-not-leaky]].

## Partial success

A batch is not atomic. Each element of the response array carries `status` and, on
failure, a `MER-` code and the field path that failed. Clients must retry only the failed
elements, reusing the same `idempotency_key` so the retry deduplicates.

## GET /v1/events/{idempotency_key}

Returns the processing state of a single event for up to 24 hours after acceptance. Useful
for support, not intended for production polling — it queries the dedupe cache directly and
is rate limited to 10 requests per second per tenant.

## Versioning

The path version changes only on a breaking change. Additive fields ship without a version
bump, which is the same compatibility rule the wire format follows
([[schema-registry-avro-not-json]]).
