---
status: idea
title: Record host and session lifecycle events; per-host timeline view
pillar: platform
---

# Host and session lifecycle events

**Problem.** On 2026-10-18 at 14:21Z every Claude builder session exited at
once (gr496610). Nothing recorded it: no alert, no log line tying the exits
together, no way afterwards to tell a host reboot from a network drop from a
power event from a mass quota refusal. The alert kind and the watchdog feed
only say what is wrong *now*; once the condition clears, the evidence is gone.

**Idea.** Extend the alert kind (or the watchdog feed) to *record* lifecycle
events, not only open conditions:

- host reboot (boot id changed between two beats);
- network loss and regain (per host, with the gap length);
- power events (where a host can report them);
- Claude / Devin session exits (clean exit, crash, killed, quota refusal),
  so a burst of exits shows up as one correlated cluster.

Plus a per-host timeline read, for example
`get(kind='alert', view='timeline', args={'host': ..., 'since': ...})`:
events for one host in time order, newest last, so "what happened to this
host at 14:21Z" is one call.

## Open questions

- **Source of events.** Heartbeat gaps and boot-id changes (host-side beat),
  node exporter metrics, the session launcher's exit hook, or the session
  harness itself? Which events can only the host report, and which can the
  server infer from a missing beat?
- **Retention.** Events are cheap but unbounded. How long to keep them, and
  must they outlive `worker_logs` pruning (the host-dark detector was moved
  off that table for exactly this reason)?
- **Dedup.** A flapping link or a mass exit produces a burst. One event per
  occurrence, or one grouped event with a count and a time span? How does it
  relate to the failure-id registry
  ([alert-failure-id-registry.md](alert-failure-id-registry.md))?
- **Alert vs. event.** Does a recorded event page anyone, or only appear in
  the timeline unless a rule promotes it?
- **Session identity.** How is a session exit attributed to a host and a
  builder when the session has no durable identity of its own?

Motivation: gr496610.
