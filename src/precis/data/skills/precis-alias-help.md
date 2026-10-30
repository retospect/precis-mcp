---
id: precis-alias-help
family: core
title: precis — the alias kind (bidirectional string map)
summary: kind='alias' stores a typed pair a ⇄ b looked up from either side — short names for long strings, crosswalks between id systems, provisional mappings tied to a gripe; plus which short form (handle, $ref, alias, have=) to reach for
answers:
  - how do I give a long URL or query a short name I can reuse?
  - how do I map an external id to a precis handle when precis has no field for it?
  - how do I record a temporary mapping until a gripe fixes the real one?
  - what does this string map to, from either side?
  - how do I avoid re-sending long text in tool calls?
applies-to: put/get/search/edit/delete (kind='alias')
tags: addressing, verbs
kinds: alias, gripe
status: active
---

# precis-alias-help — the `alias` kind

An **alias** is one row holding a pair `a ⇄ b` with a relation. A lookup
by **either** side returns the other side, so one row answers "what is
`X` short for" and "what short name does `Y` have". Handles are `as<N>`.

Rows are durable, not per-session: a short name that disappeared when a
session compacted would leave the summary pointing at nothing. For
"temporary", set `expires=`.

## Which short form do I want?

| You have | Use | Why |
|---|---|---|
| a stored ref or chunk | its handle (`pa5`, `dc12`) | already short, already resolves |
| text in a chunk you read and must write elsewhere | `text={'$ref': 'dc12'}` | the server copies it; you emit 4 tokens (`precis-addressing-help`) |
| a skill you already read | `get(kind='skill', id=…, args={'have': <rev>})` | unchanged skill returns a stub |
| two refs that belong together | `link(rel=…)` | typed edge between refs, both directions |
| a string with no handle (URL, external id, path, a non-ref name) | `kind='alias'` | this skill |

Don't alias a string you will send once. The saving is per re-send:
alias a long string you expect to send three or more times, or a mapping
other sessions need.

## Verbs

```python
put(kind='alias', args={'a': 'sciadv-aeg5791',
    'b': 'https://api.crossref.org/works/10.1126/sciadv.aeg5791',
    'rel': 'short-for'})
# created alias as12: 'sciadv-aeg5791' short-for 'https://…'

get(kind='alias', id='sciadv-aeg5791')   # → the URL (rel short-for)
get(kind='alias', id='https://api.crossref.org/works/10.1126/sciadv.aeg5791')
                                         # → the short name (rel expands-to)
get(kind='alias', id='as12')             # one pair, full detail
get(kind='alias')                        # newest 50 live pairs
search(kind='alias', q='crossref')       # substring over both sides + note
edit(kind='alias', id='as12', args={'expires': '30d', 'note': '…'})
delete(kind='alias', id='as12')
```

`put` fields (all in `args=`): `a`, `b` (required, ≤ 2000 chars each,
different), `rel` (default `same-as`), `scope` (optional namespace,
≤ 64 chars, e.g. `url`, `lcsc`), `expires` (`30m`/`12h`/`7d`/`2w` or an
ISO UTC time), `gripe` (`gr<N>`), `note` (≤ 500 chars), plus `tags=`.
Putting a pair that already exists live returns the existing handle.

Lookup is exact first, then case-insensitive if nothing matched exactly.
`args={'scope': 'url'}` on `get` limits the lookup to one scope. Expired
pairs are hidden from lookup and search.

To change a side, delete the pair and put a new one; `edit` changes only
`rel`, `scope`, `expires`, `gripe` and `note`.

## Relations

| rel (stored) | read from the b side | Use |
|---|---|---|
| `same-as` | `same-as` | two spellings of one thing (`LCSC C25804` ⇄ `cp7`) |
| `see-also` | `see-also` | loosely related |
| `short-for` | `expands-to` | a short name for a long string |
| `renamed-to` | `renamed-from` | an old name that moved |
| `maps-to` | `mapped-from` | a one-way crosswalk between id systems |

`put` also accepts the right-column names; it swaps `a`/`b` and stores
the left-column form, so one pair never exists twice under two spellings.

## Provisional mappings — tie them to a gripe

When precis maps two things wrongly or not at all and a gripe is filed,
record the workaround mapping with the gripe:

```python
put(kind='alias', args={'a': 'pa36264', 'b': 'doi:10.1000/xyz',
    'rel': 'same-as', 'gripe': 'gr537001',
    'note': 'paper ingest dropped the DOI'})
```

This stores the gripe on the pair and adds a `related-to` link, so the
gripe's link view shows the workaround. Once the gripe is `done` or
`wontfix`, `get(kind='alias', id='/stale')` lists the pair, together with
pairs whose `expires` has passed. For each one, either delete it (the fix
made it moot) or keep it as a permanent mapping:
`edit(id='as<N>', args={'gripe': '', 'expires': 'never'})`.

## Limits

- An alias side can't be another alias handle (`as<N>`). It can be any
  other handle: mapping `pa5` to a DOI is a normal use.
- `{'$ref': 'as12'}` doesn't expand: an alias has no body chunk. Read the
  pair and pass the other side yourself.
- An alias is not a link. When both sides are refs and the relation is
  part of the knowledge (cites, supports, part-of), use `link`.
