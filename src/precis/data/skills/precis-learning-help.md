---
id: precis-learning-help
family: memory
title: precis — private study activity and pacing settings
summary: owner-only manual study check-ins, contribution calendar and optional session guidance; authenticated personal MCP identity required
answers:
  - how do I record that I studied today?
  - how do I see my study activity calendar?
  - how do I change my suggested study duration or timezone?
  - why does the shared agent MCP refuse private learning data?
applies-to: get/put/edit (kind='learning')
kinds: [learning]
status: active
tags: [workflow, addressing]
---

# precis-learning-help — private study activity

Learning activity is private to the authenticated account. Mint a personal MCP
token in **Account → MCP identity**, copy the token when shown, and use it as
the client's bearer credential. It is shown once; mint another if lost.
Revoke an individual token from Account. An install-wide token, slice grant,
stdio actor or owner argument does not identify a learner.

Personal calls use the streamable HTTP transport with SDK session ownership.
Start a new MCP session when changing users or switching between an install
credential and a personal one. A server lacking that capability refuses
personal access; upgrade/sync its locked dependencies rather than bypassing
the check. Legacy SSE cannot carry private learning calls.

## Read your activity and settings

```python
get(kind="learning")  # private activity, 84 days by default
get(kind="learning", id="/activity", args={"days": 14})  # 1..90
get(kind="learning", id="/settings")
get(kind="learning", id="le123")  # an event handle returned by your check-in
```

The calendar shows studied, rest and no study recorded. Minutes can be unknown.
These are **self-reports of engagement**, not successful recall or mastery.
Foreign and missing event handles return the same refusal. Private learning
records have no general refs/chunks, tags, graph links or search index.

## Record study or rest

```python
put(kind="learning", id="/checkin", args={
    "status": "studied",
    "minutes": 12,
    "note": "Read the methods section and tried explaining the main figure.",
    "idempotency_key": "f3081257-60f1-4100-b269-0b0040449175",
})
```

Omit date for today in your configured timezone; supply date="2026-11-04" to
report a past day. Omit minutes when unknown. Notes are limited to 500
characters. Every action needs a new opaque idempotency_key (1–128 characters).
Retrying the exact same action with its key returns the original event;
reusing the key with different content refuses.

A new check-in for the same day supersedes its earlier self-report without
erasing history. This corrects the activity status; it does not create evidence
that you answered a question correctly. A rest check-in omits minutes:

```python
put(kind="learning", id="/checkin", args={
    "status": "rest",
    "idempotency_key": "d28c553d-d1f1-463b-8a59-f5cb8269bd22",
})
```

## Adjust guidance

```python
edit(kind="learning", id="/settings", mode="replace", args={
    "timezone": "Europe/Dublin",
    "typical_session_minutes": 30,
})
edit(kind="learning", id="/settings", mode="replace", args={
    "typical_session_minutes": None,
})
```

Thirty minutes is an editable suggestion, never a quota, cutoff or requirement
for marking a study day. Podcasts remain extra listening. Use **Learning**
in the web navigation (or the Account link) for the private calendar,
check-in form and the same settings.

## Current boundary

This first slice records manual activity. It does not import Anki history,
score answers, estimate forgetting, rank a paper queue or personalize audio.
Those features must consume owner-scoped evidence and keep performance
separate from a study check-in. Shared agents cannot bypass identity by
naming another owner. Private results are bounded and are not placed into
shared continuation caches or tool-log payloads.
