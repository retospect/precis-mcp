---
id: precis-startup-skills-help
title: precis — pin skills into the cold-start banner
summary: operator skill pinning — surface chosen skills on cold-start banner via env var
applies-to: PRECIS_STARTUP_SKILLS env var
status: active
---

# precis-startup-skills-help — pin skills into the cold-start banner

The cold-start banner is spare by default: it teaches discovery via
`search(kind='skill', q=...)`. Operators can pin skill ids so they
surface to every connecting agent on the first message — useful when
small models would otherwise miss them.

## Pin skills for every connecting agent
## Configure PRECIS_STARTUP_SKILLS
## Tell the server which skills to advertise at boot

```bash
PRECIS_STARTUP_SKILLS=precis-search-help,precis-paper-help,precis-patent-search-help
```

Comma-separated skill slugs. Whitespace tolerated. Duplicates dropped
(first occurrence wins). Empty by default — banner stays lean.

## What pinning surfaces to the agent
## How an agent sees pinned skills
## What lands in the cold-start banner

The banner gains one line listing the pinned ids:

```text
Pinned skills (load via prompts/get): precis-search-help,
  precis-paper-help, precis-patent-search-help.
```

The agent decides whether to pre-fetch the bodies via
`get(kind='skill', id='<slug>')` or `prompts/get`. The corresponding
MCP prompts carry a `pinned` tag for clients that prioritise tagged
prompts in their picker.

Bodies are not inlined — the banner stays small, bodies stream on
demand.

## Cap pinned-body size
## Limit how much context pinning can consume
## PRECIS_STARTUP_SKILLS_CAP_KB

Default cap: **50 KB** of cumulative resolved-body size across all
pinned slugs.

```bash
PRECIS_STARTUP_SKILLS_CAP_KB=50    # default; 0 disables (not recommended)
```

Drop-tail when exceeded: slugs resolve in env-var order; the first
slug that would push the total over the cap — and every slug after
it — is omitted. A warning appends to the banner:

```text
PRECIS_STARTUP_SKILLS truncated to cap (50 KB): omitted
  precis-python-help, precis-files-help.
```

Reorder entries (highest-priority first) or raise the cap to change
which skills survive.

## Unknown slug handling

Unresolvable slugs (typo, removed skill, third-party id) drop with a
one-line warning:

```text
PRECIS_STARTUP_SKILLS skipped unknown skill ids: foo, bar.
```

Zero banner bytes are paid when the config is valid.

## Pick the right slugs to pin

```python
get(kind='skill', id='toc')                     # browse every skill, one-line synopsis
search(kind='skill', q='patent search prior art')   # fuzzy lookup for a workflow
```

Pin the skills agents in this deployment will hit first and most
often — typically `precis-search-help` plus the one or two `kind`
helpers central to the workspace.

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-search-help')      # default cold-start action
get(kind='skill', id='precis-kinds-disabled-help')   # PRECIS_KINDS_DISABLED sibling env var
```
