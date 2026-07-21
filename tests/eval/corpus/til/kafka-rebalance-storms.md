---
title: Rebalance storms and why restarting makes them worse
type: til
tags: [til, kafka, oncall]
created: 2024-05-14
---

# Rebalance storms and why restarting makes them worse

A four-hour incident where the enricher group never processed a single message because it
spent the whole window rebalancing, and every remediation we tried extended it.

## The loop

An instance misses its heartbeat and is evicted. Eviction triggers a rebalance. Under the
eager protocol, a rebalance revokes **every** partition from **every** member, so all
instances stop, hand back their assignments, and wait. Our instances take about 40 seconds
to warm up their GeoIP and user-agent data before they can poll again
([[enricher-service]]). During that warm-up another member misses its heartbeat. Another
rebalance. Repeat.

Restarting the group — the instinctive fix — resets every instance into the slow warm-up
path simultaneously, which is precisely the condition that starts the loop.

## What actually broke it

Raising `session.timeout.ms` from 10 s to 45 s and `max.poll.interval.ms` to 300 s, so a
slow instance is not evicted for being slow, and lowering `MERIDIAN_MAX_POLL_RECORDS` so
the poll loop returns to the coordinator more often ([[config-envvars]]). The group
settled in under two minutes with no restarts.

## The real fix

Cooperative sticky assignment. It revokes only the partitions that need to move rather than
all of them, so a rebalance no longer stops the world and a warming instance does not drag
its healthy peers down with it. We migrated the following month.

## Why the poll records default is not lower

Smaller polls mean more round trips and lower throughput. 500 is the point where the
coordinator gets enough attention without materially hurting the rate. Lower it during an
incident, then put it back.

## Operational note

This is the reason [[consumer-lag-spike]] says to check whether a rebalance is in progress
*before* restarting anything. It is the one runbook step that is counterintuitive enough
that people skip it.
