---
id: precis-session-context-help
family: core
title: precis — session context (banner, disabled kinds, default tags)
summary: read the cold-start banner, understand connector session binding, and identify default tags on a put
answers:
  - why did a verb fail with Unsupported on a kind I expected in this session?
  - why did my note end up tagged with a project tag I didn't add?
  - what does the workspace tag mean on a file?
  - how is my connector session bound for the word-handle switch?
applies-to: cold-start banner; put/tag response hints
status: active
tags: orientation, troubleshooting
---

# precis-session-context-help — what this session is set up with

Project prose lives in the memory mesh: find the project hub with `search(kind='memory', tags=['project:<slug>'])`, then read its navigation links. Write linked memories or the bound draft; file kinds are import/export only. `meta.workspace` is the export home. Compile/export a draft only when requested. See [[precis-memory-help]] and [[precis-draft-help]].

The cold-start banner reports what is live in this process; read it
before assuming a kind is live or a tag is yours alone.

## What kinds are available in this session?
## Which kinds are turned off here?
## Why does `get(kind='patent')` raise Unsupported here?

Look at the cold-start banner for the `Kinds:` line and, when a kind was
gated out at boot, `Kinds unavailable:`:

```text
Kinds: paper, memory, gripe, conv, ...
Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited).
```

An unavailable kind raises `Unsupported` on every verb (see
[[precis-kinds-disabled-help]]) — don't retry, and don't suggest it to
the user without flagging that it is off here. `Kinds unavailable:` is
absent on a clean boot; a kind that was never configured does not appear
on either line.

## What skills were pre-pinned?
## Which skills should I load on the first message?

A `Pinned skills:` line lists skills this deployment surfaces for every
connecting agent; see [[precis-startup-skills-help]] for it and its ⚠
warning lines.

## How is my connector session bound?

The server binds calls on a stateful MCP connection to its live session,
created by the transport. A stateless HTTP connector gets a fresh session
per request. On that transport, the word-handle switch follows the
authenticated OAuth grant across calls and access-token refreshes. The
acknowledgement says either `for this session` or `for this connector credential`.

A shared installation bearer on stateless HTTP has no dedicated switch
scope. If `put(kind='handle', text='on')` returns `BadInput` asking for a live
session or dedicated credential, reconnect through OAuth for a persistent
switch, or call `get(kind='handle', id=648264)` with the ref's numeric id to
convert it once. See [[precis-word-handles-help]] for client matching and
how to check whether the switch is on.

The banner points every connecting agent to [[precis-voice-chat-help]].
Connectors that hide the banner can get the same pointer in the first
tool reply of a live session, or in each stateless HTTP tool reply.

## How do I tell the user what this deployment is configured for?

Read the cold-start banner. It carries:

- `Kinds: ...` — the live verb surface.
- `Kinds unavailable: ... (reason)` — what is off and why.
- `Pinned skills: ...` — the curated reading list.
- Inline `[info]` hints on `put` / `tag` responses — the per-call
  default-tag activity below.

## What tags get auto-added to every put?
## Why did my note end up tagged `fbproj`?
## Which tags is the deployment stamping onto everything?

A deployment may define default tags. They merge into `tags=` on every
`put` for note-like kinds, and the response says what was added:

```text
[info] Added PRECIS_DEFAULT_TAGS to put: fbproj, 2026-q2.
```

Note-like kinds (merge applies) are the ones that hold your own notes and
working records: `memory`, `gripe`, `todo`, `anki`, `conv`, `draft`, and
the file kinds `markdown`, `plaintext`, `tex`, among others.

Not note-like (no merge): ingested or fetched kinds such as `paper`,
`patent`, `web`, `youtube`, plus `oracle`, `random`, `skill`, `calc`,
`python`. Ingested metadata is canonical across deployments;
auto-tagging would corrupt the shared store.

## Why didn't my `tag(add=...)` get the defaults?

`tag(kind=..., id=..., add=[...])` does **not** mutate the
caller's set. The response suggests:

```text
[info] PRECIS_DEFAULT_TAGS suggested for tag add: fbproj.
```

Re-issue with the merged set if you want the defaults applied.

A default tag removed via `tag(remove=...)` will be re-added on
the next `put`.

## What does the `workspace` tag mean on a file?

File-rooted kinds (`markdown`, `plaintext`, `tex`) auto-stamp
`workspace` on every ref under the sandbox root. It's applied by the
handler with `set_by='system'` — distinguishes workspace files from
arbitrary content.

`workspace` and the default tags layer. Both apply; neither
supersedes the other. `workspace` identifies file-rooted-ness;
defaults identify deployment intent.

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-startup-skills-help]] — the Pinned skills line
- [[precis-kinds-disabled-help]] — kinds that are not live in this process
- [[precis-tags]] — tag axis matrix, closed prefixes
- [[precis-files-help]] — file-rooted kinds
