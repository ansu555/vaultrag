---
title: Canonical event schema
type: reference
tags: [reference, schema, ingest]
created: 2024-04-25
---

# Canonical event schema

The Avro record every internal topic carries. Registered as subject `events.raw-value`;
compatibility rules are in [[schema-registry-avro-not-json]].

## Required fields

| Field | Type | Notes |
|---|---|---|
| `idempotency_key` | `string` | UUIDv7 minted by the client at event creation, not at send |
| `event_name` | `string` | Lowercase snake_case, max 64 chars, from the tenant's declared set |
| `occurred_at` | `long` | Epoch milliseconds, client clock |
| `received_at` | `long` | Epoch milliseconds, gateway clock; added at the edge |
| `tenant_id` | `string` | Injected by the gateway from the signing key; never client-supplied |

`tenant_id` being server-injected is a security property, not a convenience. A client that
sends the field has it overwritten, so a compromised key can never write into another
tenant's data.

## Optional fields

| Field | Type | Default |
|---|---|---|
| `user_id` | `["null", "string"]` | `null` |
| `session_id` | `["null", "string"]` | `null` |
| `properties` | `map<string>` | `{}` |
| `geo` | `["null", "GeoRecord"]` | `null`, populated by the enricher |
| `schema_version` | `int` | `1` |

Every optional field has a default. That is what makes `BACKWARD` compatibility hold — a
consumer built against an older schema can read a newer record by falling back to defaults.

## Properties are flat strings

`properties` is a string-to-string map, not a nested structure. Nested objects are
flattened with dotted keys at the gateway, so `{"cart": {"value": 10}}` becomes
`{"cart.value": "10"}`. Numeric typing is recovered at query time from the tenant's
declared property types.

This is the single most-questioned part of the schema. Nested Avro unions made schema
evolution unmanageable across tenant-defined property sets, and the flattening keeps
compatibility checks tractable.

## Size limits

A single serialised record must stay under 256 KB, enforced at the gateway with
`MER-1413`. The largest contributor in practice is an oversized `properties` map; a map
above 200 entries is a strong signal a customer is misusing events as documents.
