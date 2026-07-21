---
title: Poison pill message
type: runbook
severity: sev3
tags: [runbook, oncall, kafka]
created: 2024-06-09
---

# Poison pill message

A single record that crashes or hangs the consumer every time it is retried, blocking its
partition indefinitely. Symptom is one partition's lag climbing while the rest are flat.

## Confirm it

Find the repeating offset in the consumer logs — the same `partition=` and `offset=` pair
appearing every retry interval is conclusive. Then read the record without consuming it:

```
kafkacat -C -b broker-1:9092 -t events.raw -p 37 -o 1049823 -c 1 -e | xxd | head
```

Look at the first five bytes. A magic byte other than `0x00` means the record was not
written by our Avro serialiser, which almost always means a producer bypassed the schema
registry.

## Common causes

Decode failures are the largest group — a record encoded with a schema the consumer's
registry cache cannot resolve, raising the `MER-2xxx` family from [[error-codes]]. Second
is an oversized field, typically a customer sending a 40 MB properties blob that passes
gateway validation but explodes in the enricher's JSON expansion. Third is genuinely
corrupt bytes from a producer with a broken buffer.

## Route it out

Do not skip the offset by force unless the record is unreadable. The supported path is to
raise the consumer's `poison_threshold` behaviour: after three consecutive failures on the
same offset the consumer publishes the raw bytes to `events.dlq` with the failure reason
and commits past it. That is the mechanism `dead-letter-reaper` is built around, and it
keeps the evidence.

If the consumer version in production predates that behaviour, `kafka-consumer-groups
--reset-offsets --to-offset N+1 --execute` works, and you must copy the record out first
with the `kafkacat` command above and attach it to the incident.

## After the fact

Every poison pill is a producer bug or a validation gap. The follow-up is a gateway
validation rule so the record cannot enter the topic again — [[ingest-api]] documents where
those live. [[schema-registry-avro-not-json]] explains why decode failures should be
impossible for compliant producers, which makes each one a signal that something bypassed
the registry.
