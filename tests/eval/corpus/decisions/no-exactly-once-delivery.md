---
title: At-least-once delivery, never exactly-once
type: decision
status: accepted
tags: [decision, ingest, correctness]
created: 2024-04-02
---

# At-least-once delivery, never exactly-once

## Context

Two customers with financial reporting requirements asked us to guarantee that each event
is processed precisely one time. Kafka transactions plus idempotent producers can get
close within a single cluster, and the engineering pull to promise it was strong.

## Decision

We promise at-least-once delivery end to end and make every consumer idempotent instead.
No part of the pipeline claims exactly-once semantics, and the contract documents say so
in those words.

## Reasoning

Exactly-once holds only inside a boundary the transaction coordinator controls. Our
pipeline crosses that boundary constantly: an HTTP call to a customer's enrichment
webhook, a write to ClickHouse, a notification to a third-party alerting service. The
moment a side effect leaves the transactional world the guarantee is fiction, and a
fiction stated in a contract is worse than an honest weaker promise.

The honest version is cheap to build. Consumers key their writes on
`(idempotency_key, stage)`, so replaying a partition is a no-op for anything already done.
This falls out of [[dedupe-by-idempotency-key]] almost for free.

## What we tell customers

Processing is at-least-once; results are idempotent; a replay never changes a computed
aggregate. That last sentence is the property they actually wanted, and we can defend it.

## Rejected alternative

Kafka's transactional producer with `read_committed` consumers would have covered the
Kafka-to-Kafka hops only, at roughly a 25% throughput penalty measured on our staging
cluster, while leaving every external side effect exposed. We would have paid real
performance for partial coverage of a property we could not honestly advertise.
