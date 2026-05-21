# Plan: `precis.identity` (B2)

- **Status**: in-progress (2026-05-21)
- **Parent plan**: [`storage-v2.md`](./storage-v2.md) §step B
- **Branch**: `feat/storage-v2-step-b`
- **Touchpoints**: ADR 0002 (pub_id), ADR 0006 (cite_key), ADR 0008
  (slug dropped, identifiers normalized into `ref_identifiers`)

## Goal

A single, pure module that owns identifier derivation for v2.
Everything that mints a `paper_id`, `pub_id`, `cite_key`,
`pdf_sha256`, `content_hash`, or `node_id` calls into here.

Pure functions only — no DB, no I/O, no model loads. Inputs are
plain Python; outputs are short strings. Deterministic: same input,
same output. Re-ingesting the same paper on a fresh DB produces the
same `paper_id` and the same `pub_id` byte-for-byte.

## Public surface

```python
# Identifier normalisers ---------------------------------------------------

def normalize_doi(s: str | None) -> str | None: ...
    # Strip leading "doi:", "DOI:", "https://doi.org/", "http://dx.doi.org/",
    # "doi.org/". Lowercase. Empty / None → None.

def normalize_arxiv(s: str | None) -> str | None: ...
    # Strip "arXiv:" / "arxiv:" prefix and "https://arxiv.org/abs/" URL form.
    # Strip trailing "v\d+" version suffix (so paper_id is version-stable).
    # Old-style ids ("cs.LG/0501001") preserve their case in the slash-free
    # part; new-style ids stay digits.

# Hashes -------------------------------------------------------------------

def make_pdf_sha256(pdf_bytes: bytes) -> str: ...
    # Hex SHA-256 of the raw file bytes. 64 chars.

def normalize_text_for_hash(text: str) -> str: ...
    # NFKD-fold, lowercase, collapse ASCII whitespace runs to single space,
    # strip leading/trailing whitespace. Returns the canonical form used
    # by content_hash; useful in tests as a debugging probe.

def make_content_hash(text: str) -> str: ...
    # Hex SHA-256 of normalize_text_for_hash(text). 64 chars.

# Primary identifiers ------------------------------------------------------

def make_paper_id(*, arxiv: str | None = None,
                  doi: str | None = None,
                  pdf_sha256: str | None = None) -> str: ...
    # Priority: arxiv > doi > sha256 (storage-v2.md §"Identity & naming").
    # Returns "arxiv:<id>" / "doi:<id>" / "sha256:<hex>".
    # Raises ValueError if all three are None / empty.
    # Inputs are normalized first (caller may pass raw forms).

def make_pub_id(paper_id: str) -> str: ...
    # base32(sha256(paper_id))[:6].lower(). 6 chars, [a-z2-7].
    # Pinned at first ingest; re-ingest of same paper_id → same pub_id.

def make_cite_key(authors: list[str | dict] | None,
                  year: int | None,
                  *,
                  taken: set[str] = frozenset()) -> str: ...
    # firstauthor + 2-digit year + collision-letter suffix.
    # No suffix when base is free; "a" on first collision; "b", ... on next.
    # Raises CiteKeyOverflow if 'a'..'z' are all taken.
    # Surname extraction reuses precis.utils.slug._first_author.
    # Missing first author → "anon"; missing year → "00".

def make_node_id(paper_id: str, page: int | None, block_index: int) -> str: ...
    # base32(sha256("{paper_id}:p{page}:b{block_index}"))[:8].lower().
    # 8 chars, opaque. Stable across DB rebuilds; used as a deterministic
    # handle for blocks/chunks within a paper independent of BIGSERIAL ids.
    # page=None is encoded as "pNone" so non-paginated refs (notes, code)
    # still get a stable id space.
```

## Algorithms (the locked-in choices)

### `paper_id` priority

`arxiv` > `doi` > `sha256(pdf bytes)`, per
storage-v2.md §"Identity & naming". Rationale: an arXiv id survives
moves between hosts; a DOI survives most changes; only the file
hash survives nothing but byte-equality. We pick the most stable
identifier we have.

### `pub_id` derivation (from ADR 0002)

```python
import base64, hashlib
digest = hashlib.sha256(paper_id.encode("utf-8")).digest()
pub_id = base64.b32encode(digest)[:6].decode("ascii").lower()
```

6 characters, `[a-z2-7]`. Locked: do **not** change the algorithm
without an ADR — `pub_id` values exist in user-facing artefacts
(LaTeX cites, MCP responses) and external references.

### DOI normalisation

```
DOI:10.1234/X       → 10.1234/x
doi:10.1234/X       → 10.1234/x
https://doi.org/10.1234/x   → 10.1234/x
http://dx.doi.org/10.1234/x → 10.1234/x
doi.org/10.1234/x   → 10.1234/x
10.1234/x           → 10.1234/x          (already canonical)
""                  → None
None                → None
```

DOIs are case-insensitive per the DOI Handbook; we normalise to
lowercase so `(id_kind, id_value)` PK collisions aren't created by
publisher inconsistency.

### arXiv normalisation

```
2301.12345           → 2301.12345
2301.12345v3         → 2301.12345        (version stripped)
arXiv:2301.12345     → 2301.12345
arxiv:2301.12345v2   → 2301.12345
https://arxiv.org/abs/2301.12345   → 2301.12345
https://arxiv.org/abs/2301.12345v3 → 2301.12345
cs.LG/0501001        → cs.LG/0501001     (old-style: keep case + slash)
cs.LG/0501001v2      → cs.LG/0501001     (version stripped)
""                   → None
None                 → None
```

Version suffix is stripped so re-ingesting v3 of the same preprint
collapses to the same `paper_id` as v1 / v2. Old-style ids keep
their archive prefix case (some are `cs.LG`, some `q-bio.NC`).

### `cite_key` algorithm

```python
surname = first_author_surname_lowercased_ascii()  # reuse slug._first_author
yy      = "%02d" % (year % 100) if year is not None else "00"
base    = f"{surname or 'anon'}{yy}"               # e.g. "miller23"

if base not in taken:                              # most common case
    return base
for letter in "abcdefghijklmnopqrstuvwxyz":
    candidate = base + letter
    if candidate not in taken:
        return candidate
raise CiteKeyOverflow(base, taken)                 # 27th paper edge case
```

`taken` is the set of cite_keys already in the corpus that share
the prefix. Callers compute it once via
`SELECT id_value FROM ref_identifiers WHERE id_kind = 'cite_key'
AND id_value LIKE :prefix || '%'`. Empty set on first ingest.

Insertion-order suffix progression, per ADR 0006 §"Open questions".
A deterministic-suffix variant (hash-based) can replace this if the
"suffixes shuffle on corpus rebuild" workflow surfaces.

### `node_id` derivation

Given the schema doesn't have a `node_id` column — `chunks` use a
`BIGSERIAL chunk_id` — `make_node_id` produces a *deterministic
opaque handle* that survives rebuild. Use cases:

- a stable URL path for a chunk that doesn't change when chunks are
  re-ingested
- chunk-level addressing in agent responses where `chunk_id` would
  leak DB internal state

Algorithm:

```python
key = f"{paper_id}:p{page}:b{block_index}"   # page=None → literal "pNone"
digest = hashlib.sha256(key.encode("utf-8")).digest()
node_id = base64.b32encode(digest)[:8].decode("ascii").lower()
```

8 characters, `[a-z2-7]`. Wider than `pub_id` because the namespace
is per-paper and we want collision-free results across all chunks
of a paper without coordination.

Where it is stored is **B3's call** — likely `chunks.meta` JSONB
or as a `(id_kind='node_id', id_value=…, ref_id=…)` row in
`ref_identifiers` if cross-paper lookups are needed. The function
exists in B2; consumers wire it up later.

### `content_hash` normalisation

```python
def normalize_text_for_hash(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.lower()
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded
```

Why this and not "the obvious" `sha256(text)`:

- Re-OCR with a different engine can change line breaks (`\n` vs
  `\r\n`), spacing around punctuation, ligature decomposition. The
  raw bytes diverge but it's the same paper.
- We want `content_hash` to dedup the "same paper, different bytes"
  case; the file bytes hash (`pdf_sha256`) covers byte-equality
  separately.

The normalization is intentionally minimal — we don't strip
punctuation or stopwords. That would fold genuinely different
papers ("we propose X" vs "we do not propose X") to the same hash.

## Edge cases and decisions

| Situation | Behaviour | Rationale |
|---|---|---|
| Empty / whitespace-only `authors` | surname → "anon" | Match `slug.mint_slug` convention |
| `year = None` | yy → "00" | "miller00" reads as "year unknown"; alternative would be a separate `<surname>n` form which complicates parsing |
| 27+ papers by Miller in 2023 | `CiteKeyOverflow` | No `aa`/`ab` extension yet; punt to ADR if a real corpus hits this |
| `paper_id` empty in `make_pub_id` | `ValueError` | A pub_id of an empty string is meaningless |
| `make_paper_id` with all kwargs None / "" | `ValueError` | Caller must provide at least one source |
| Mixed-case DOI | lowercased | DOI Handbook §2.4 |
| arXiv URL with hash anchor (`#abstract`) | stripped along with rest of URL | Anchors aren't part of the id |
| DOIs containing percent-encoded chars | left as-is | DOIs aren't URL-encoded by spec; if encoding leaks in we treat the raw form as canonical |

## Tests (`tests/test_identity.py`)

Pure-function tests, no DB needed. Parametrised where there's a
table of cases.

- `normalize_doi`: 8 cases including `None`, `""`, prefix variants, URL forms, mixed case
- `normalize_arxiv`: 10 cases including new-style + old-style + version stripping + URL form + None
- `make_pdf_sha256`: known input → known hex, length == 64, deterministic
- `normalize_text_for_hash`: NFKD fold, whitespace collapse, idempotence (`f(f(x)) == f(x)`)
- `make_content_hash`: same normalised text → same hash; whitespace differences within → same hash; genuinely different content → different hash
- `make_paper_id`: priority order verified (arxiv beats doi beats sha256), normalisation applied, all-None raises
- `make_pub_id`: known paper_id → known pub_id (regression-pinned), length, charset `[a-z2-7]`, deterministic, empty raises
- `make_cite_key`:
  - surname extraction across name forms (Smith / Smith, John / John Smith / A.Clark / A.B.Clark)
  - diacritic folding (Müller → muller)
  - year handling (2023 → 23, 1999 → 99, 2003 → 03, None → 00)
  - empty taken → no suffix
  - `taken={base}` → `a`
  - `taken={base, base+a}` → `b`
  - `taken={base, base+a, …, base+y}` → `z`
  - `taken={base, base+a, …, base+z}` → raises `CiteKeyOverflow`
  - empty authors → "anon"
  - taken={base} but base+'a' free → returns base+'a' (not base)
  - re-running with same args + same `taken` is deterministic
- `make_node_id`: known input → known output, length 8, deterministic, page=None handled

Target: 30–40 tests, all passing in <1s.

## Out of scope

- DB reads (B3 / ingest will pass `taken` from a SELECT)
- PDF text extraction → `precis.ingest.pdf` (B3)
- File storage / corpus layout → `precis.ingest.pipeline` (B3)
- External-identity resolution (CrossRef / S2 / arXiv API) → `precis.ingest.lookup` (B3)
- Migration of existing data — greenfield re-ingest, no shim

## Open questions (deferred)

- **`make_cite_key` overflow at 27** — bump to `aa`, `ab`, … vs
  numeric suffix vs raise. Defer until a real corpus needs it.
- **Deterministic cite_key suffix** — hash-based collision-free
  letter assignment instead of insertion-order. Defer per ADR 0006.
- **`make_content_hash` aggressiveness** — should it strip
  hyphenation across line breaks? Page-number footers? Defer; today's
  rule is "the simplest stable normalization".
- **`make_node_id` storage location** — `chunks.meta` JSONB vs
  `ref_identifiers` row. B3's call.

## Definition of done

- [ ] `src/precis/identity.py` exists with the public surface above.
- [ ] `tests/test_identity.py` exists with ≥ 30 tests.
- [ ] `uv run pytest tests/test_identity.py` passes 100%.
- [ ] `uv run ruff check src/precis/identity.py tests/test_identity.py` clean.
- [ ] `uv run ruff format --check src/precis/identity.py tests/test_identity.py` clean.
- [ ] `uv run mypy src/precis/identity.py` clean.
- [ ] No new top-level deps (stdlib + reuse `precis.utils.slug._first_author`).
- [ ] One commit, conventional message: `B2: precis.identity module`.
