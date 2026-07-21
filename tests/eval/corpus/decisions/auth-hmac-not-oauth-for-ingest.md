---
title: HMAC request signing on the ingest path
type: decision
status: accepted
tags: [decision, security, gateway]
created: 2024-07-01
---

# HMAC request signing on the ingest path

## Context

The ingest endpoint is called from customer servers, mobile SDKs, and embedded devices,
tens of thousands of times per second per large account. The management API already uses
OAuth 2.0 bearer tokens and the obvious move was to use them everywhere.

## Decision

The ingest path authenticates with an HMAC-SHA256 signature over the request body,
timestamp, and path, using a per-tenant secret. The management API keeps OAuth. Two
mechanisms, chosen per traffic shape rather than for uniformity.

## Why signing beats bearer tokens here

A bearer token is a password in transit: anyone who captures it can replay it until it
expires. Short expiry means a token refresh round trip, which on a device sending one
event every few minutes can double the request count. The signature is computed from the
body, so an intercepted request cannot be modified, and the timestamp with a five-minute
tolerance window makes replay useless.

Signing also fails closed on the thing that actually goes wrong. Devices with skewed
clocks or truncated bodies get a hard `MER-1401` rather than silently posting corrupt data
that we would discover in a customer's dashboard a week later.

## Key handling

Secrets are 32 random bytes, shown once at creation, stored as an Argon2id hash. Rotation
is overlapping — two secrets are valid at once for a seven-day window — and the procedure
is [[credential-rotation]]. No secret ever appears in a log line; the gateway redacts the
`Authorization` and `X-Meridian-Signature` headers before structured logging.

## Rejected: mutual TLS

mTLS is stronger and we would prefer it, but certificate distribution to customer-managed
embedded fleets was the blocker. Several customers could not rotate a client certificate
without a firmware release. We revisit when the embedded share of traffic drops below 5%.
