---
title: Avro with a schema registry on the wire
type: decision
status: accepted
tags: [decision, schema, ingest]
created: 2024-04-22
---

# Avro with a schema registry on the wire

## Context

The first version of the pipeline moved JSON documents through Kafka with no declared
shape. Producers added fields freely, consumers guessed at types, and a Tuesday afternoon
where someone changed `user_id` from an integer to a string took the rollup worker down
for two hours before anyone connected cause to effect.

## Decision

Events on internal topics are Avro-encoded with the schema id in a five-byte prefix, and a
central registry owns compatibility. The public HTTP ingest API still accepts JSON — that
is a product decision, not a transport one — and the gateway encodes to Avro at the edge.

## Compatibility mode

The registry runs in `BACKWARD` mode: a new schema must be readable by consumers compiled
against the previous one. Practically that means new fields need defaults, and removing a
field or narrowing a type is refused at registration time rather than at 3 a.m.

## Why not JSON Schema

JSON Schema validates but does not encode. We would still be shipping verbose JSON over
the wire and paying the parse cost in every consumer. Switching to Avro cut the average
serialised event from 1.9 KB to 640 bytes, which at our volume is around 40% of the
inter-zone network bill, and Avro's resolution rules gave us the compatibility checking
as a side effect rather than a second system.

## Operational cost

The registry is now on the critical path for producing. We mitigate with an aggressive
local schema cache in every producer — a registry outage stops new schema registration
but does not stop traffic on schemas already in use.

See [[event-schema]] for the current field list and [[error-codes]] for the `MER-2xxx`
family raised on decode failure.
