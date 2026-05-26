---
title: Startup-skill pinning via PRECIS_STARTUP_SKILLS
tier: 2
applies-to: skill
---

# Startup-skill pinning (`PRECIS_STARTUP_SKILLS`)

The MCP server ships with a deliberately spare cold-start banner.
The default action it teaches is **discovery**:

```python
search(kind='skill', q='<your goal in 2-5 words>')
```

If a particular deployment knows in advance which skills its agents
will need (e.g. a research workspace centred on the `paper` and
`patent` kinds), the operator can **pin** specific skill ids so they
surface in the cold-start banner without an explicit search.

## Configuration

Set the env var to a comma-separated list of skill slugs:

```
PRECIS_STARTUP_SKILLS=precis-search-help,precis-paper-help,precis-patent-search-help
```

The default is empty — cold-start stays lean unless an operator
opts in.

Whitespace around commas is tolerated. Duplicates are dropped
silently in the operator-stated order (first occurrence wins).

## What pinning does

For each surviving slug:

1. The banner in `serverInfo.instructions` carries a one-line notice
   listing the pinned ids:

   ```
   Pinned skills (load via prompts/get): precis-search-help,
     precis-paper-help, precis-patent-search-help.
   ```

   An agent sees this on the very first message and can decide
   whether to pre-fetch the bodies via `get(kind='skill', id='<slug>')`
   or `prompts/get`.

2. The corresponding MCP **prompt** carries a `pinned` tag (alongside
   the existing `precis`, `skill`, `tier-<N>`, `kind:<X>` tags). Modern
   clients can use the tag to prioritise the operator's recommended
   set in their prompt picker.

What pinning does **not** do:

- It does not inline skill bodies into the banner. The banner stays
  small; bodies stream on demand.
- It does not change the seven-verb wire surface. Pinned skills
  remain reachable via the same `get(kind='skill', ...)` and
  `search(kind='skill', ...)` calls every other skill uses.
- It does not pre-emit `notifications/prompts/list_changed`. The
  prompts are registered at boot like every other skill; the
  `pinned` tag is the only extra signal.

## Cap and truncation

To prevent operator misconfiguration from inflating context for
every connecting agent, the resolver enforces a cap on total
resolved-body bytes.

- Default cap: **50 KB** of cumulative skill-body size.
- Configurable via `PRECIS_STARTUP_SKILLS_CAP_KB` (integer; set to
  `0` to disable, not recommended).

Behaviour when the cap is exceeded is **drop-tail** to preserve the
operator-stated priority order:

1. Resolve slugs in the order they appear in the env var.
2. Sum body bytes as we go.
3. The first slug that would push the total over the cap, and
   every slug after it, lands in the `truncated` set.
4. A warning notice is appended to the banner:

   ```
   ⚠ PRECIS_STARTUP_SKILLS truncated to cap (50 KB): omitted
     precis-python-help, precis-files-help.
   ```

Reorder the env-var entries (or raise the cap) to change which
skills survive.

## Unknown slug handling

A slug that doesn't resolve to a shipped skill (typo, removed
skill, third-party skill from a sibling deployment) is dropped
with a one-line warning:

```
⚠ PRECIS_STARTUP_SKILLS skipped unknown skill ids: foo, bar.
```

This is **suppressed entirely** when the config is valid: zero
banner bytes are paid by an operator who set the env var
correctly.

## Discovery

To list every available slug:

```python
get(kind='skill', id='toc')
```

To find the right slugs for your workflow:

```python
search(kind='skill', q='patent search prior art')
```

## Operator workflow

1. `get(kind='skill', id='toc')` and `search(kind='skill', q=...)`
   to identify the skill ids your agents will need most often.
2. Set `PRECIS_STARTUP_SKILLS=<slug1>,<slug2>,...`.
3. (Optional) Adjust `PRECIS_STARTUP_SKILLS_CAP_KB` if the default
   50 KB is too tight or too loose for your context budget.
4. Restart the server.
5. Verify the banner: a connecting agent should see the
   `Pinned skills (load via prompts/get): ...` line on the first
   message.

## Related

- `precis-overview` — the seven-verb agent tool surface and the
  full kind topology.
- `precis-search-help` — the canonical first-action skill that the
  cold-start banner advertises by default.
- `PRECIS_KINDS_DISABLED` — sibling env var that turns whole kinds
  off; pinning a skill whose subject kind is disabled surfaces a
  notice on the banner (see `precis-kinds-disabled-help` once that
  ships).
