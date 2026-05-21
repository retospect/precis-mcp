# ADR 0002 — `pub_id` as primary LLM handle, TOON for tabular output

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing

## Context

Two distinct but related concerns surfaced while designing the v2
ingest path:

1. **What identifier does the LLM use to name a paper?** Today the
   answer is the human-friendly slug (`smith2024foo`). Slugs are
   *guessable*: an agent asked about "graphene synthesis 2024" will
   happily emit `chen2024graphene` whether or not that ref exists,
   creating fabricated citations. Slugs also cost 4–6 tokens each.

2. **What format does the MCP server emit for tabular results?**
   Today: pretty-printed JSON. JSON wastes tokens on repeated keys in
   homogeneous lists (search results, citation lists). Token budget
   matters at scale.

## Decision

### Identifier scheme

- **`pub_id`**: 6-character lowercase base32 string derived as
  `base32(sha256(paper_id))[:6].lower()`. This is the primary handle
  emitted to LLMs and the canonical citation key.
  - Stable across re-ingest (deterministic from `paper_id`).
  - 1 token in cl100k / o200k tokenizers.
  - Unguessable — eliminates a class of hallucinated citations.
  - 6 chars × base32 ≈ 1 G combinations; birthday collision at ~46 K
    refs. Sufficient for our scale.
- **`slug`**: `smith2024foo` form, generated as today. Stays for:
  - filesystem layout (`corpus/s/smith2024foo.pdf`),
  - human-typed CLI lookups,
  - alias citations in pre-existing user docs.
- **`ref_id`**: bigserial, internal FK target only. Never surfaced to
  external clients.

`precis get <handle>` accepts either `pub_id` or `slug`. The MCP API
emits `pub_id` everywhere a single canonical key is needed; responses
include `slug` and `title` alongside for human readability.

### Output format

- **TOON** (Token-Oriented Object Notation,
  https://toonformat.dev/) for tabular MCP responses (lists of refs,
  blocks, citations, search hits). Approximately 40 % token reduction
  vs JSON at equal retrieval accuracy on the upstream benchmarks.
- **Tab as TOON delimiter** (not comma) — paper titles routinely
  contain commas; tab is rare.
- **JSON** for nested or single-record responses (`get_paper(...)`)
  and for all *input* arguments (LLMs already emit JSON well; TOON
  parsers are still maturing).
- CLI `--format` flag: `toon` | `json` | `table` (default depends on
  TTY: `table` when interactive, `toon` when piped).

### Citation format in agent-generated text

LLMs emit citations as `[a3f7k1]` (LaTeX `\cite{a3f7k1}`,
markdown `[a3f7k1]`). Slugs survive as a human-author convention; the
resolver tries `pub_id` first, then slug.

## Consequences

### Positive

- Hallucinated citations drop sharply: an LLM cannot guess a 6-char
  random string.
- Token cost of search results drops ~40 % via TOON, and citation
  bodies drop from 4–6 tokens to 1 token.
- Single canonical key in the DB; slug becomes a query alias rather
  than a primary join target.
- Re-ingest produces the same `pub_id` (deterministic), so external
  documents citing `[a3f7k1]` keep resolving across DB wipes.

### Negative

- Slight friction for users who memorised slugs — the CLI accepts
  both, but log lines and MCP responses now show `pub_id` first.
- TOON tooling: we add `toons` (Rust-backed PyO3) or
  `toon-format/toon-python` as a dep. Pin in `pyproject.toml`.
- Agents trained on JSON outputs need a brief TOON parsing snippet
  in their system prompt; we ship one in `data/skills/precis-toon.md`.

## `pub_id` derivation reference

```python
import base64, hashlib

def make_pub_id(paper_id: str) -> str:
    digest = hashlib.sha256(paper_id.encode("utf-8")).digest()
    return base64.b32encode(digest)[:6].decode("ascii").lower()
```

Properties:
- Deterministic.
- Lowercase a-z and 2-7 (RFC 4648 base32 alphabet); easy to type and
  paste.
- 6 chars; bump to 8 if the corpus exceeds ~50 K refs.

## Open follow-ups

- Decide whether to disambiguate the very-rare birthday collision
  (`UNIQUE` constraint + retry with extra hash bytes? prefix the
  collider with a digit? error to user?). Specification deferred to
  `docs/design/storage-v2.md` §pub-id.
- Decide whether to allow a user-overridable `pub_id` (e.g., for
  classics whose canonical citation is widely known). Default: no.
