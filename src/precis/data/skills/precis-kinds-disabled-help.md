---
id: precis-kinds-disabled-help
family: core
title: precis — recognize disabled kinds
summary: disabled-kind diagnostics — Unsupported and NotFound answers for a kind this process does not serve, what each reason means, what to call instead
answers:
  - I got an Unsupported error on a kind I expected to work — what does that mean?
  - how do I check what's actually live in this build?
  - what does reason='prohibited' mean on a kind?
  - what does "kind routes through the <role> node" mean?
  - I am a worker and the kind my task needs is disabled — what do I do?
applies-to: all kinds (boot-time enablement)
tags: troubleshooting
status: active
---

# precis-kinds-disabled-help — recognize disabled kinds

A kind is *disabled* in this process when it was gated out at boot: it
is switched off, or something it needs is missing. Calls against a
disabled kind raise `Unsupported`; the error names the reason. It
stays disabled for the life of the process — retrying does not help.

## I got "kind is registered but disabled" — what now?
## A verb failed with Unsupported on a kind I expected to work
## What does "disabled in this build" mean?

The runtime raises this when the kind exists in the registry but
was gated out at boot:

```text
Unsupported: kind 'patent' is registered but disabled in this build
(missing EPO_OPS_CLIENT_KEY, EPO_OPS_CLIENT_SECRET, PRECIS_PATENT_RAW_ROOT)
Next: see get(kind='skill', id='precis-kinds-disabled-help')
      and precis-overview Needs column
```

The parenthetical names the reason: `prohibited` (the kind is switched
off here on purpose), `missing <name>, <name>` (a credential or root
this deployment has not configured), `missing setting: <key>` (a
deployment setting it is gated on is unset), `store required`, or a
**contextual hint** when a background pass disabled the kind *on purpose
for this run* and told you what to do instead. Example: a draft-editing
tick gates off the `tex` file kind and the reason reads `this project's
deliverable is draft 'X' — write prose with put(kind='draft' ...) …`.
When the reason is an instruction, follow it. Otherwise the kind is
simply not callable from this session: use a kind that is live, and tell
the user which kind is off and why instead of retrying.

## I am a worker and the kind my task needs is off
## My todo names tex or markdown files and the file kind is disabled here
## No operator to ask: what does a background tick do about a disabled kind?

```python
search(kind="memory", tags=["project:<project>"])  # the same section as a memory; the project hub links its sections

tag(kind="todo", id=<todo id>, add=["STATUS:blocked"], reason="tex disabled on this host")  # no memory holds it: park the todo
```

A file kind (`markdown`, `plaintext`, `tex`) that is off means this host
reads no workspace files, under any id or spelling. Read the section from
the project's memories. If none holds it, finish the part of the task that
needs no file, then park the todo with the reason and stop.

## "unknown kind" with "routes through the <role> node"

```text
NotFound: unknown kind: patent
Next: kind 'patent' routes through the fetch node; options above are kinds ...
```

The kind exists in the deployment but is served by another process, not
this one. The label is the serving node's *role* (derived from its
process name), never its hostname; when several nodes share a role the
label carries a numeric suffix (`compute-1 node`, `compute-2 node`).
The call cannot succeed here; use one of the listed `options` kinds, or
continue without it.

## Check what's actually live in this build
## See which kinds loaded vs which got gated out
## What's wired right now?

```python
get(kind="skill", id="precis-help")  # live kinds + verbs in this build
get(kind="skill", id="precis-overview")  # full kind topology + Needs column
```

The cold-start banner already names every absent kind:

```text
Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited).
```

## A kind shows reason='prohibited' on the banner
## Why is a kind prohibited?

`prohibited` means the deployment turned the kind off even though
everything it needs is present (an air-gapped deployment disabling `web`;
a kind held back until a review lands). It outranks resource state: a
prohibited kind that *could* load still reports `prohibited`. Nothing in
this session re-enables it.

## A pinned skill targets a disabled kind
## Startup-skills mention a kind I can't call

The skill body still loads — it's package data, no I/O on the
kind. The skill teaches you how to call a verb that will then
raise `Unsupported`. If you hit this, tell the user the kind is off here
and continue without it.

## See also

- [[precis-overview]] — Needs column maps kinds to what they need
- [[precis-startup-skills-help]] — the Pinned skills line
- [[precis-preflight]] — health probe before calling unfamiliar kinds
