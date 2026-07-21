---
title: Fire-and-forget tasks get garbage collected
type: til
tags: [til, python, asyncio]
created: 2024-08-12
---

# Fire-and-forget tasks get garbage collected

Roughly one in every few thousand webhook calls in the enricher simply never happened. No
error, no log line, no timeout — the work vanished. The cause was a Python detail I had
never internalised.

## The bug

```python
async def handle(event):
    asyncio.create_task(notify_webhook(event))   # nothing holds the result
    return enrich(event)
```

The event loop keeps only a **weak** reference to a task. If nothing else holds a strong
reference, the task is eligible for garbage collection at any await point, and a collected
task is cancelled silently. Under low load the task usually completes before a collection
happens, which is exactly why this looked like flakiness rather than a bug.

## The fix

```python
_background: set[asyncio.Task] = set()

def spawn(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task
```

Holding the task in a module-level set until it finishes is the documented workaround. The
`add_done_callback` is what stops the set growing without bound.

## How we found it

Not from logs, because there were none — a cancelled task raises nothing. We found it by
counting: webhook attempts emitted at call time versus attempts recorded by the receiving
side, over a week. A consistent 0.02% gap that scaled with concurrency.

## Wider lesson

The symptom of this class of bug is missing work rather than failing work, and monitoring
built around error rates cannot see it. The enricher now counts task spawns and task
completions as separate metrics, and alerts on the difference. Any fire-and-forget path
needs that pair of counters, otherwise silence is indistinguishable from success.
