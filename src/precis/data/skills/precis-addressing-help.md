---
id: precis-addressing-help
title: precis — universal handles (the one address scheme)
summary: the 9-char type-prefixed handle, the relative grammar, the 2-char type codes, address-vs-metadata
applies-to: get / edit / delete / tag / link (any verb that addresses an existing ref)
status: rolling-out
---

# precis-addressing-help — one handle for every ref and chunk

> **Status — rolling out (ADR 0036).** Handles are being introduced as the
> single address form. During the transition the legacy forms still work on
> input (paper slugs `miller23`, numeric ids `158`, draft `¶<h>`); new output
> moves to handles. The 2-char **type codes** below are the part worth knowing
> now. Authoritative source of the codes: `src/precis/utils/handle_registry.py`.

## What a handle is

A **handle** is the one address for every persistent ref and every addressable
body chunk:

```
[2-char type code][7-char Crockford base32]      = 9 chars, lowercase
pa4m8p1rz   a paper          pc7k9q2mx   a paper chunk
dr7k9q2mx   a draft          dc4m8p1rz   a draft chunk
td9q2mx4p   a todo           me1rz7k9q   a memory
```

- **The type code tells you what it is** — see `pa…` → a paper. So
  `get(id='pa4m8p1rz')` needs no `kind=`; the prefix infers it. (`kind=` stays
  required for `put`/`search`, which name a *class*, not a record.)
- **Flat & stable.** A handle is opaque identity, minted once, immutable. It is
  *not* positional — unlike the retired `miller23~4`, it never rots when a doc
  is re-chunked.
- **Crockford base32** (no `i l o u`, case-insensitive) so it survives
  lowercasing / OCR / read-aloud / copy-paste. Bare, no internal separators.

## Relative grammar (navigation sugar — never stored)

Off a stable handle anchor; resolves against *current* structure, yields another
handle. Use for reading/navigation; the durable reference is always the bare
handle.

```
dc4m8p1rz          this chunk
dc4m8p1rz+1 / -1   next / previous sibling  (next/prev heading at that level;
                                             on a flat paper = next/prev block)
dc4m8p1rz+3        three siblings forward
dc4m8p1rz-2..3     signed sibling span: 2 before … 3 after (inclusive)
dc4m8p1rz^         parent / enclosing heading
dc4m8p1rz^2        two levels up
```

`..` present ⇒ range, absent ⇒ single step. `++`/`--`/`^^` accepted as aliases
of `+1`/`-1`/`^2`. One trailing operator (no chaining); resolve and re-address to
compose.

## Address vs metadata

A handle is the **internal** pointer. A ref's **external** identity — DOI,
arXiv, source URL, Discord path, filesystem path — is **metadata, kept as data**
(bibliography, dedup, re-fetch, verify links), *not* the handle. They coexist:
`pa4m8p1rz` ↔ `doi:10.1234/…`.

## The 2-char type codes

Records (left) and their chunk code where the kind has addressable body chunks.
Mirror of `handle_registry.py` — the module is the SSOT; this table is the
agent-facing copy.

| kind | rec | chunk | | kind | rec | chunk |
|---|---|---|---|---|---|---|
| paper | `pa` | `pc` | | memory | `me` | — |
| patent | `pt` | `pk` | | oracle | `or` | — |
| news | `nw` | `nc` | | finding | `fi` | `fb` |
| draft | `dr` | `dc` | | citation | `ci` | — |
| conv | `co` | `cc` | | flashcard | `fc` | — |
| pres | `pr` | `ps` | | random | `rn` | — |
| markdown | `md` | `mc` | | todo | `td` | — |
| plaintext | `pl` | `lc` | | job | `jo` | `jc` |
| tex | `tx` | `xc` | | alert | `al` | — |
| python | `py` | — | | agentlog | `ag` | — |
| gripe | `gr` | `gc` | | cron | `cr` | `cp` |
| skill | `sk` | — | | message | `ms` | `mb` |
| tag | `tg` | — | | | | |

Providers (`web`, `youtube`, `wikipedia`, `semanticscholar`, `websearch`,
`perplexity-*`) and stateless tools (`calc`, `math`, `provenance`) have **no
handle** — they are addressed by URL / query / compute.
