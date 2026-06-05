---
title: Prohibiting kinds via PRECIS_KINDS_DISABLED
tier: 2
applies-to: skill
---

# Prohibiting kinds (`PRECIS_KINDS_DISABLED`)

A deployment may want to **prohibit** a kind even when the
resources to run it are available — for example, an air-gapped
research workspace might disable the `web` kind to keep the
agent from issuing outbound HTTP requests even though
`PERPLEXITY_API_KEY` is set, or a security review might want
the `patent` kind off until the EPO Terms-of-Use audit lands.

## Configuration

Set the env var to a comma-separated list of kind names:

```
PRECIS_KINDS_DISABLED=patent,web,youtube
```

The default is empty — every kind whose resources are available
loads, matching today's behaviour.

Whitespace around commas is tolerated. Unknown kind names (typo,
removed kind) are accepted silently as a no-op against the live
registry; treating typos as a hard error would create a footgun
every time a kind is renamed.

## What prohibition does

The kind-enablement predicate is:

```
loaded(kind) = NOT prohibited(kind) AND resources_present(kind)
```

Where `resources_present(kind)` is the existing machinery —
env vars on `KindSpec.requires_env`, store / embedder / file root
checks inside the handler's `__init__`.

A prohibited kind:

- Is **not constructed** at boot. The handler module is not
  imported; no sockets open, no env vars are read.
- Is **not registered** in the dispatch table. Any
  `get(kind='patent', ...)` etc. raises `NotFound` with the
  standard "kind not registered" recovery message.
- **Surfaces on the cold-start banner**:

  ```
  Kinds unavailable: patent (prohibited), web (prohibited).
  ```

  Connected agents see this on the first message and can advise
  the operator if the prohibition was unintentional.

## Resource vs prohibition

The two axes are independent:

| Resources present? | Prohibited? | Outcome             | Banner reason         |
|--------------------|-------------|---------------------|-----------------------|
| Yes                | No          | Loaded              | (none — kind appears in `Kinds loaded:`) |
| No                 | No          | Skipped             | `missing <ENV>` / `store required` / …    |
| Yes                | Yes         | Skipped             | `prohibited`          |
| No                 | Yes         | Skipped             | `prohibited` (prohibition wins)           |

When both axes skip the kind, the **prohibition reason wins** so
the operator sees the intent they expressed, not the incidental
resource state.

## Operator workflow

1. `get(kind='skill', id='precis-overview')` for the catalogue of
   live kinds.
2. Decide which kinds to keep off (review the security posture or
   the deployment shape).
3. Set `PRECIS_KINDS_DISABLED=<kind1>,<kind2>,...`.
4. Restart the server.
5. Verify the banner: a connecting agent should see the
   `Kinds unavailable: <kind> (prohibited)` entries on the first
   message; `Kinds loaded:` should no longer mention the
   prohibited kinds.

## Interaction with `PRECIS_STARTUP_SKILLS`

When a pinned skill targets a kind that's been prohibited, the
skill body still loads (it's a markdown file in the package
data — no I/O on the kind itself). The startup-skills banner
notice is unchanged. The pinned skill teaches the agent how to
use a kind that won't actually answer; future iteration may
emit a stronger warning.

For now, the simplest discipline is: remove the skill from
`PRECIS_STARTUP_SKILLS` when the matching kind is in
`PRECIS_KINDS_DISABLED`.

## Related

- `precis-overview` — the seven-verb agent tool surface and the
  full kind topology.
- `precis-startup-skills-help` — sibling env var that pre-surfaces
  specific skill ids.
- `precis-status` — runtime health probe; lists every live kind
  and the env vars each consumes.
- `docs/conventions/kind-enablement.md` — handler-author
  contract: declare resource requirements via
  `KindSpec.requires_env` rather than via inline checks at the
  boot site.
