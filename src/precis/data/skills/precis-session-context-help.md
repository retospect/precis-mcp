---
id: precis-session-context-help
title: precis — session context (pinned skills, disabled kinds, default tags)
summary: per-session configuration — pinned skills, disabled kinds, default tags via env vars
answers:
  - why did a verb fail with Unsupported on a kind I expected in this session?
  - which skills did the operator pre-pin for this deployment?
  - why did my note end up tagged with a project tag I didn't add?
  - what does the workspace tag mean on a file?
applies-to: env (PRECIS_STARTUP_SKILLS, PRECIS_KINDS_DISABLED, PRECIS_DEFAULT_TAGS)
status: active
tags: orientation, troubleshooting
---

# precis-session-context-help — what the operator has set for this session

Three env vars shape the session. The cold-start banner reports
all three; read it before assuming a kind is live or a tag is
yours alone.

## What kinds are available in this session?
## Which kinds did the operator turn off?
## Why does `get(kind='patent')` raise Unsupported here?

Look at the cold-start banner for `Kinds loaded:` and
`Kinds unavailable: <kind> (prohibited)`. A prohibited kind
raises `Unsupported` on every verb (see [[precis-kinds-disabled-help]]) —
don't retry, don't suggest it to the user without flagging the prohibition.

```text
Kinds loaded: paper, memory, gripe, conversation, ...
Kinds unavailable: patent (prohibited), web (prohibited).
```

`PRECIS_KINDS_DISABLED=<comma-list>` is the operator's lever.
Prohibition wins over resource availability — env presence
doesn't override operator intent.

## What skills did the operator pre-pin?
## Which skills should I load on the first message?
## Is there an operator-curated reading list for this deployment?

The operator pins a reading list via PRECIS_STARTUP_SKILLS so it
surfaces in the cold-start banner for every connecting agent — pin
list, size cap, and unknown-slug handling: [[precis-startup-skills-help]].

## What tags get auto-added to every put?
## Why did my note end up tagged `fbproj`?
## Which tags is the operator stamping onto everything?

`PRECIS_DEFAULT_TAGS=<comma-list>` merges into `tags=` on every
`put` for note-like kinds. The dispatcher prints what it added:

```text
[info] Added PRECIS_DEFAULT_TAGS to put: fbproj, 2026-q2.
```

Note-like kinds (merge applies): `memory`, `gripe`,
`conversation`, `anki`, `todo`, `markdown`,
`plaintext`, `tex`.

Not note-like (no merge): `paper`, `patent`, `web`, `wolfram`,
`youtube`, `oracle`, `random`, `skill`, `calc`, `python`. Ingested
metadata is canonical across deployments; auto-tagging would
corrupt the shared store.

## Why didn't my `tag(add=...)` get the defaults?

`tag(kind=..., id=..., add=[...])` does **not** mutate the
caller's set. The dispatcher suggests:

```text
[info] PRECIS_DEFAULT_TAGS suggested for tag add: fbproj.
```

Re-issue with the merged set if you want the defaults applied.

A default tag removed via `tag(remove=...)` will be re-added on
the next `put`. To remove permanently the operator must drop it
from `PRECIS_DEFAULT_TAGS` and restart.

## What does the `workspace` tag mean on a file?

File-rooted kinds (`markdown`, `plaintext`, `tex`) auto-stamp
`workspace` on every ref under `PRECIS_ROOT`. It's applied by the
handler with `set_by='system'` — distinguishes operator-authored
files from arbitrary content.

`workspace` and `PRECIS_DEFAULT_TAGS` layer. Both apply; neither
supersedes the other. `workspace` identifies file-rooted-ness;
defaults identify deployment intent.

## How do I tell the user what this deployment is configured for?

Read the cold-start banner. It carries:

- `Kinds loaded: ...` — the live verb surface.
- `Kinds unavailable: ... (prohibited|missing <ENV>|...)` — what
  the operator turned off and why.
- `Pinned skills (load via prompts/get): ...` — operator's
  curated reading list.
- Inline `[info]` hints on each `put` / `tag` response — the
  per-call default-tag activity.

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-startup-skills-help]] — PRECIS_STARTUP_SKILLS detail
- [[precis-kinds-disabled-help]] — PRECIS_KINDS_DISABLED detail
- [[precis-tags]] — tag axis matrix, closed prefixes
- [[precis-files-help]] — PRECIS_ROOT and file-rooted kinds
