---
id: precis-session-context-help
title: precis — session context (pinned skills, disabled kinds, default tags)
applies-to: env (PRECIS_STARTUP_SKILLS, PRECIS_KINDS_DISABLED, PRECIS_DEFAULT_TAGS)
status: active
---

# precis-session-context-help — what the operator has set for this session

Three env vars shape the session. The cold-start banner reports
all three; read it before assuming a kind is live or a tag is
yours alone.

## What kinds are available in this session?
## Which kinds did the operator turn off?
## Why does `get(kind='patent')` return NotFound here?

Look at the cold-start banner for `Kinds loaded:` and
`Kinds unavailable: <kind> (prohibited)`. A prohibited kind
raises `NotFound` on every verb — don't retry, don't suggest
it to the user without flagging the prohibition.

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

```text
Pinned skills (load via prompts/get): precis-search-help,
  precis-paper-help, precis-patent-search-help.
```

Pre-fetch the bodies:

```python
get(kind='skill', id='precis-search-help')
get(kind='skill', id='precis-paper-help')
```

`PRECIS_STARTUP_SKILLS=<comma-list>` pins them. A cap
(`PRECIS_STARTUP_SKILLS_CAP_KB`, default 50) drops the tail when
exceeded; the banner names dropped slugs. Pinned skills targeting
a prohibited kind still load — the banner flags the mismatch.

## What tags get auto-added to every put?
## Why did my note end up tagged `fbproj`?
## Which tags is the operator stamping onto everything?

`PRECIS_DEFAULT_TAGS=<comma-list>` merges into `tags=` on every
`put` for note-like kinds. The dispatcher prints what it added:

```text
[info] Added PRECIS_DEFAULT_TAGS to put: fbproj, 2026-q2.
```

Note-like kinds (merge applies): `memory`, `gripe`,
`conversation`, `flashcard`, `todo`, `markdown`,
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

```python
get(kind='skill', id='precis-overview')              # verbs and kinds
get(kind='skill', id='precis-startup-skills-help')   # PRECIS_STARTUP_SKILLS detail
get(kind='skill', id='precis-kinds-disabled-help')   # PRECIS_KINDS_DISABLED detail
get(kind='skill', id='precis-tags')                  # tag axis matrix, closed prefixes
get(kind='skill', id='precis-files-help')            # PRECIS_ROOT and file-rooted kinds
```
