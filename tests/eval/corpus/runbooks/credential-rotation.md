---
title: Rotating a tenant ingest secret
type: runbook
severity: sev3
tags: [runbook, security, oncall]
created: 2024-07-15
---

# Rotating a tenant ingest secret

Run this when a customer reports a leaked key, when a secret passes its 365-day age limit,
or as step one of any suspected-compromise incident.

## Overlapping validity

Rotation is never a swap. Issue the new secret first; both the old and the new are
accepted for a seven-day overlap window, during which the customer redeploys at their own
pace. Only after their traffic has fully moved does the old secret get revoked. Cutting
over atomically would black-hole every device that has not yet been updated, which for
embedded fleets can be weeks.

The signing scheme this protects is described in [[auth-hmac-not-oauth-for-ingest]].

## Procedure

1. `meridianctl secret issue --tenant <id>` prints the new secret **once**. It is stored
   as an Argon2id hash and cannot be retrieved again.
2. Deliver it through the customer's configured secure channel, never over email or a
   ticket comment.
3. Watch `meridian_auth_secret_version` split across the two versions. The dashboard panel
   `Ingest / Secret adoption` breaks it down per tenant.
4. When the old version's share has been zero for 24 hours, `meridianctl secret revoke
   --tenant <id> --version <n>`.
5. If day six arrives with meaningful traffic still on the old version, contact the
   customer before revoking. Extending the window is always cheaper than an outage.

## Compromise variant

If the secret is known to be leaked, skip the overlap. Revoke immediately, accept that the
customer's ingest breaks, and tell them before they discover it. Then check
`meridian_auth_failures_total` by source IP for the preceding thirty days to establish
whether the key was actually used by someone else, and open a security incident if so.

## What not to do

Never log the secret to verify it, not even at debug level, and not even briefly. The
gateway's redaction is a safety net rather than the control. Never reuse a revoked
version number. Never issue a secret shorter than the 32 bytes `meridianctl` produces.
