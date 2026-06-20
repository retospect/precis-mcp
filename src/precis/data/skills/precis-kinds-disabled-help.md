---
id: precis-kinds-disabled-help
title: precis — recognize and enable disabled kinds
summary: disabled-kind diagnostics — Unsupported errors, env var gating, operator prohibition
applies-to: all kinds (boot-time enablement)
status: active
---

# precis-kinds-disabled-help — recognize and enable disabled kinds

A kind is *disabled* when its env vars are missing or when the
operator listed it in `PRECIS_KINDS_DISABLED`. Calls against a
disabled kind raise `Unsupported`; the error names the reason.

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

The parenthetical names the reason: either `prohibited` (operator
listed it in `PRECIS_KINDS_DISABLED`) or `missing <ENV1>, <ENV2>`
(required env vars not set). The agent cannot fix this — relay
the missing var(s) to the operator.

## Which env vars does each kind need?
## What credentials enable a kind?
## How do I tell the operator what to set?

| Kind | Required env |
|---|---|
| `patent` | `EPO_OPS_CLIENT_KEY`, `EPO_OPS_CLIENT_SECRET`, `PRECIS_PATENT_RAW_ROOT` |
| `math` | `WOLFRAM_APP_ID` |
| `websearch`, `perplexity-reasoning`, `perplexity-research` | `PERPLEXITY_API_KEY` |
| `markdown`, `plaintext`, `tex` | `PRECIS_ROOT` |
| `python` | `PRECIS_PYTHON_ROOTS` |

Store-backed kinds (`paper`, `oracle`, `conv`, `todo`,
`memory`, `gripe`, `flashcard`, `citation`) need a configured store; if
absent they report `store required` in the boot banner.

## Check what's actually live in this build
## See which kinds loaded vs which got gated out
## What's wired right now?

```python
get(kind='skill', id='precis-help')        # live kinds + verbs in this build
get(kind='skill', id='precis-overview')    # full kind topology + Needs column
```

The cold-start banner already names every absent kind:

```text
Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited).
```

## Operator prohibited a kind on purpose
## A kind shows reason='prohibited' on the banner
## What is PRECIS_KINDS_DISABLED?

`PRECIS_KINDS_DISABLED` is a comma-separated list of kind names
the operator has turned off even when resources are present (air-
gapped deployment disabling `web`; security review keeping
`patent` off until audit lands). Prohibition wins over resource
state: a prohibited kind that *could* load still reports
`prohibited` on the banner.

To re-enable: operator removes the kind from
`PRECIS_KINDS_DISABLED` and restarts the server.

## A pinned skill targets a disabled kind
## Startup-skills mention a kind I can't call

The skill body still loads — it's package data, no I/O on the
kind. The skill teaches you how to call a verb that will then
raise `Unsupported`. If you hit this, tell the operator to either
enable the kind or remove the skill from `PRECIS_STARTUP_SKILLS`.

## See also

```python
get(kind='skill', id='precis-overview')              # Needs column maps kinds to env vars
get(kind='skill', id='precis-startup-skills-help')   # sibling env var for pinned skills
get(kind='skill', id='precis-preflight')             # health probe before calling unfamiliar kinds
```
