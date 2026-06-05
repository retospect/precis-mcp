# ADR 0006 — Tri-identifier scheme: `pub_id` / `cite_key` / `slug`

- **Status**: accepted (2026-05-21) — *slug section superseded*; `pub_id`
  + `cite_key` portions remain in force.
  - **Slug section**: superseded by
    [ADR 0008 — Drop slug identifier normalisation](./0008-drop-slug-identifier-normalisation.md).
    Slugs were dropped as a normalised identifier; `cite_key` is the
    canonical human-readable form.
  - **`pub_id` + `cite_key` sections**: in force. See
    `src/precis/identity.py` and the `ref_identifiers` table.
- **Deciders**: Reto + agent
- **Supersedes**: ADR 0002 §"Identifier scheme" (the rest of ADR
  0002 — TOON output format, pub_id derivation reference — remains
  in force)
- **Superseded by**: 0008 (slug section only).

## Context

ADR 0002 introduced a two-identifier scheme: `pub_id` (6-char base32,
opaque LLM handle) and `slug` (long human-readable form, e.g.
`smith2024foo`). This works for two of three real audiences but
under-serves the third:

| Audience | Wants | ADR 0002 answer |
|---|---|---|
| Machine (DB joins, MCP tool I/O) | Stable, opaque, 1-token | `pub_id` ✅ |
| Reader (`precis show` output, URL alias) | Long, descriptive, contextual | `slug` ✅ |
| **Writer (LaTeX `\cite{}`, bibtex)** | **Short, recognisable, ASCII** | — |

The user pointed out: papers ingested into precis are also cited in
papers the user is writing. `\cite{wang2020dopamine}` is too long
to type fluently; `\cite{k7m3xq}` is unrecognisable in source. The
academic norm is `\cite{wang20a}` — first author last name + 2-digit
year + a letter suffix when collisions arise (`natbib` already
handles the suffix).

## Decision

Adopt three identifiers. Each serves a distinct audience.

| Identifier | Format | Length | Audience | Used in |
|---|---|---|---|---|
| `pub_id` | `k7m3xq` (base32, lowercase) | 6 | Machines | DB joins, MCP API, URL paths, citations in agent-generated text |
| `cite_key` | `miller23a` | 7–9 | Humans typing `\cite{}` | LaTeX bib files, hand-typed CLI lookups |
| `slug` | `miller2023dopamine` | 15–25 | Humans reading | `precis show` output, optional URL alias, search aliases |

### Derivation

```python
# pub_id (unchanged from ADR 0002)
def make_pub_id(paper_id: str) -> str:
    digest = hashlib.sha256(paper_id.encode("utf-8")).digest()
    return base64.b32encode(digest)[:6].decode("ascii").lower()

# cite_key (new)
def make_cite_key(authors, year, *, taken: set[str]) -> str:
    """First author last name + 2-digit year + letter suffix on collision.

    `authors` may be a list of strings, a list of `{family, given}`
    dicts, or a free-text byline; the last-name extractor is the
    same one slug minting uses.

    `taken` is the set of cite_keys already minted in this corpus.
    Pass `set(SELECT cite_key FROM refs WHERE cite_key LIKE 'miller23%')`
    or, more cheaply, only the keys that share the prefix.

    Examples:
        make_cite_key([{"family": "Miller"}], 2023, taken=set())
            -> "miller23"
        make_cite_key([{"family": "Miller"}], 2023, taken={"miller23"})
            -> "miller23a"
        make_cite_key([{"family": "Miller"}], 2023, taken={"miller23", "miller23a"})
            -> "miller23b"
    """
```

ASCII only, lowercased, diacritics stripped. The base form is
`firstauthor + 2-digit-year`; if the corpus already has that exact
key, append `a`; if `a` is also taken, append `b`; etc. No suffix on
the first paper (`miller23` not `miller23a`) so the common case stays
short.

### Coexistence

`refs` carries all three columns: `pub_id` (UNIQUE NOT NULL),
`cite_key` (UNIQUE NOT NULL), `slug` (UNIQUE NOT NULL). The
`ref_identifiers` alias table also carries all three so a single
`SELECT ref_id FROM ref_identifiers WHERE id_value = $1` resolves any
of them.

`pub_id` is canonical. `cite_key` and `slug` are alternative views;
all responses include `pub_id` first.

### Mutability

- `pub_id` is **immutable** — derived from `paper_id`, deterministic.
- `slug` is **immutable for new mints** — set once at ingest. Older
  refs may have legacy slugs that don't match current rules; we leave
  them.
- `cite_key` is **immutable except for collision resolution** — if
  paper X with `miller23` is added before paper Y also matching that
  key, both stay (`miller23` and `miller23a` respectively). Re-ingest
  of the same paper produces the same `cite_key` because we hash
  `(authors, year, paper_id)` to choose the suffix deterministically
  rather than relying on insertion order. (Tracked in §"Open
  questions" — initial implementation may pick the next free letter
  in insert order.)

## Consequences

### Positive

- LaTeX-using writers can cite papers with `\cite{miller23a}` —
  short, recognisable, ASCII-clean.
- `pub_id` keeps its anti-hallucination property: agents still emit
  the opaque handle when their context includes one.
- `slug` becomes a true legacy alias rather than the primary surface,
  reducing pressure to bikeshed slug-generation rules.
- All three resolve via the same `ref_identifiers` lookup; no new
  resolution path.

### Negative

- `refs` gets one more UNIQUE column (`cite_key`). Three uniqueness
  checks at insert time instead of two; negligible at our scale.
- Collision resolution adds one round-trip at insert time (read the
  existing `LIKE 'miller23%'` cite_keys, pick next free suffix).
  Mitigated by an index on `cite_key`.
- Three identifier columns to keep in sync if any ingest path bypasses
  the canonical `precis.identity` helpers. Documented in B2's plan and
  enforced by a `CHECK` that all three are non-null after insert.

### Open questions

- **Deterministic suffix vs. insertion-order suffix.** A deterministic
  suffix (hash-based) lets re-ingest produce the same `cite_key` even
  on a fresh DB. Insertion-order is simpler. We start with insertion
  order; if a real workflow surfaces (e.g., regenerating `cite_key`s
  on a corpus rebuild and finding the suffixes shuffled), upgrade to
  deterministic.
- **Should `cite_key` be user-overridable?** A classic paper might
  have a canonical citation key in the literature (`einstein05`).
  Default: no override. Add later if needed.

## Migration

This ADR ships in storage-v2 step B1's greenfield schema. There is
no separate migration; `cite_key` exists from `0001_initial.sql`
onward. The old corpus is wiped and re-ingested, so every ref gets
its three identifiers minted at first ingest.
