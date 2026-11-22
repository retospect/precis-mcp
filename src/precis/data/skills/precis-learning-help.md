---
id: precis-learning-help
family: memory
title: precis — private study activity and pacing settings
summary: private study activity from card answers, Anki reviews and optional check-ins, with personal pacing settings
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

Learning activity is private to the authenticated account. The usual route is
an OAuth connection: add the Precis MCP URL in the app, sign in when it opens
Precis, and the app acts as your account (it then appears under **Account →
Connected apps**). Where the deployment accepts personal tokens, you can
instead mint a personal MCP token in **Account → Personal MCP tokens**, copy it
when shown, and use it as the client's bearer credential. It is shown once;
mint another if lost. Revoke an individual token from Account. An install-wide
token, slice grant, stdio actor or owner argument does not identify a learner.

A personal token permits calls to your own learning and GTD data. It also
permits `get(kind='skill', id='precis-learning-help')`,
`get(kind='profile', id='me')` (which shows your key-memory handle) and one
read of your own key memory, `get(kind='memory', id='me<N>')`. These reads
take no `view`, `args` or other keys.

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

The calendar counts answers submitted from **Current context** on the Learning
page and reviews imported by Anki sync. It groups both by day in your configured
timezone. On the Learning page, select **Check answer** to reveal a cloze answer
and count the attempt. Precis discards your submitted text. Counts measure
engagement. Studied (S) and rest (R) are optional self-reports and can appear
alongside automatic counts. Minutes can be unknown.
Foreign and missing event handles return the same refusal. Private learning
records have no general refs/chunks, tags, graph links or search index.

## Optionally record study or rest

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

A new check-in for the same day replaces the displayed self-report and keeps
the earlier entry in your history. Automatic counts stay with the day. Omit
minutes for a rest check-in.

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

Learning records engagement. Answer submissions and Anki review counts
carry no scores or mastery judgments. Shared agents cannot bypass identity
by naming another owner. Private results are bounded and stay out of shared
continuation caches and tool-log payloads.
