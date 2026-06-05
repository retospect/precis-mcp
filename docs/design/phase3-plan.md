# Phase 3 — Paper Kind + Bundle Ingest

End-to-end paper handling. After phase 3, the cutover commands work:

```bash
createdb precis
precis migrate
precis jobs ingest-bundles ~/.acatome/papers/
# Then v2 has a populated database; ready for cutover.
```

## Where we are

Phases 1 and 2 are committed. Memory is the proof-of-concept ref-backed
kind. Phase 3 introduces the first **slug-addressed** ref kind (paper)
plus the heaviest piece of write logic in the system: bundle ingest.

```
git log --oneline -5
# Should show the four v2 commits since main, ending with
# "v2 phase 2: DB backbone (sync, psycopg 3) + memory handler"
```

## Read first (in order)

| File | Why |
|---|---|
| `README.md` | Phase plan + conventions |
| `docs/user-facing/paper_ingest.md` | Bundle-ingest architecture sketch |
| `src/precis/data/skills/precis-paper-help.md` | Target agent-facing API |
| `src/precis/data/skills/precis-overview.md` | Kind topology recap |
| `src/precis/migrations/0001_initial.sql` | Schema (paper kind already seeded) |
| Sibling repo `pips/packages/precis-mcp/` | v1 reference for ingest patterns — **read for ideas, do not copy code** |

## Deliverables

### 1. `PaperHandler`

`src/precis/handlers/paper.py` — slug-addressed, ref-backed.

```python
spec = KindSpec(
    kind="paper",
    title="Paper",
    description="Scientific paper. Slug-addressed; one ref per paper, "
                "blocks per chunk. Ingested from .acatome bundles.",
    supports_get=True,
    supports_search=True,
    supports_put=False,         # phase 3: read-only via ingest
    is_numeric=False,
    id_required=False,          # get(kind='paper') without id = list mode
    views=("bibtex", "ris", "endnote", "abstract", "toc"),
)
```

Operations to implement:

- `get(id=slug)` — overview: title, authors, year, abstract, TOC of blocks
- `get(id=slug~38)` — block 38 by chunk index
- `get(id=slug~38..42)` — block range
- `get(id=slug, view='bibtex')` — citation in BibTeX
- `get(id=slug, view='ris')` / `'endnote'` — alternate citation formats
- `get(id=slug, view='abstract')` — abstract only
- `get(id=slug, view='toc')` — block index, no body
- `get(id=slug/cite/bib)` — view shortcut path syntax (slash-separated)
- `search(q=..., kind='paper')` — block-level lexical+semantic, RRF fused
- `search(q=..., kind='paper', scope=slug)` — within one paper

Defer for phase 3 (don't try to ship it):
- `put` — paper edits are out of scope; ingest is the only writer
- DOI / arXiv ID URI scheme prefixes — phase 4 acatome-quest-mcp territory

### 2. Store extensions

In `src/precis/store/store.py`:

- `insert_blocks(ref_id, blocks: list[BlockInsert]) -> list[Block]` — bulk
  insert with `RETURNING id, pos`. One transaction per ref.
- `get_block(ref_id, pos) -> Block | None`
- `list_blocks_for_ref(ref_id, *, with_text=True) -> list[Block]`
- `search_blocks_lexical(q, *, kind=None, scope_ref_id=None, limit=20)
   -> list[tuple[Block, Ref, float]]`
- `search_blocks_semantic(query_vec, *, kind=None, scope_ref_id=None,
   limit=20) -> list[tuple[Block, Ref, float]]`
- `search_blocks_fused(q, query_vec, *, kind=None, scope_ref_id=None,
   limit=20, k=60) -> list[tuple[Block, Ref, float]]` — RRF fusion at
  the SQL level using `ts_rank_cd` for lexical and `embedding <=> vec`
  for semantic. Rank with `1 / (k + lex_rank) + 1 / (k + sem_rank)`.
- `ingest_bundle(path: Path) -> IngestResult` — see §3.

### 3. Bundle ingest

`Store.ingest_bundle(path)` — read `.acatome` file, populate refs +
blocks + tags. **Idempotent on `(provider, doi)`**.

```python
@dataclass(frozen=True, slots=True)
class IngestResult:
    ref_id: int
    slug: str
    block_count: int
    inserted: bool          # False if already present (idempotent return)
    embedding_dim: int
```

Pipeline:

1. Open bundle, parse manifest. Extract: doi, title, authors, year,
   abstract, blocks (text + position), bundle metadata.
2. Compute slug via §4. Guard against collision.
3. Check if a ref already exists for `(provider='paper', doi=...)`.
   If yes: return `inserted=False`. Skip everything else.
4. In one transaction:
   - Insert ref (kind='paper', slug, title, provider='paper',
     meta={doi, year, authors, abstract})
   - Embed each block's text via the embedder (§5).
   - Bulk-insert blocks with embeddings.
   - Apply density tags per block (§6).
   - Apply `SRC:bundle` tag at ref level.

Error handling: any failure inside the transaction rolls back. Bundle
file is left untouched (we don't delete bundles on ingest).

### 4. Slug minting

Pattern: `<first-author-lastname><year><first-content-word>`.

- Lastname: lowercase, ASCII-folded, first contiguous word
- Year: 4-digit
- First content word: from title, lowercased, first word that isn't
  a stopword (`a`, `an`, `the`, `of`, `on`, `in`, `and`, `or`, `for`,
  `with`, `to`, `by`, ...)
- All joined with no separator: `wang2020state`, `kim2024electrocatalytic`

Collision: if `<base>` exists, try `<base>-2`, `<base>-3`, ...

Function signature:

```python
def mint_slug(
    *,
    authors: list[str],
    year: int | None,
    title: str,
    existing_slugs: Callable[[str], bool],
) -> str: ...
```

Test the pure logic in isolation (no DB needed) plus an integration
test that exercises collision against a real Store.

### 5. Embedder

Phase 3 needs vectors for blocks at ingest time and for queries at
search time. Wire a small abstraction so we can mock in tests:

```python
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def embed_one(self, text: str) -> list[float]: ...

    @property
    def dim(self) -> int: ...
    @property
    def model(self) -> str: ...
```

Implementations:
- `MockEmbedder(dim=1024)` — deterministic vectors hashed from text.
  Used by all unit tests.
- `BgeM3Embedder` — real implementation via `sentence-transformers`.
  Optional dep; only loaded if config requests it.

Pass `Embedder` to `PaperHandler` at construction (registry change).
Default to MockEmbedder if `config.embedder == 'mock'`, real otherwise.

### 6. Density tags

At ingest, classify each block as `sparse` / `medium` / `dense` based
on token-count + structure heuristics (figures captions = sparse,
methods/results body = dense). Apply as closed tag `DENSITY:medium`
etc. Defer fancy classification — start with token-count thresholds.

### 7. CLI: `precis jobs ingest-bundles <dir>`

In `src/precis/cli.py` extend the `jobs` subcommand:

```bash
precis jobs ingest-bundles ~/.acatome/papers/ \
    [--dry-run] [--limit N] [--provider paper]
```

Walks the dir for `*.acatome` files. For each:
- Calls `store.ingest_bundle(path)`
- Reports `inserted` / `skipped` (already present) / `failed` per file
- At end: summary `inserted=N skipped=M failed=K`
- Non-zero exit if any failures

`--dry-run` validates parsing without writing. `--limit` for partial
runs while iterating.

### 8. Skill drafts

Existing `src/precis/data/skills/precis-paper-help.md` is the target.
After phase 3, the skill should match what the handler actually does —
update minor discrepancies as you find them.

### 9. Cutover note

Drop `docs/design/v2-cutover.md` covering:
- New postgres DB name (`precis`, separate from `cluster`)
- Required extensions (`vector`, `pg_trgm`)
- Migration command sequence
- Bundle ingest invocation
- Expected runtime for ~25 papers (a few minutes with real embedder)
- Rollback story (v1 `acatome` DB stays; just relaunch v1 MCP)

This is for ops, not the agent. ~30–40 lines is fine.

## Suggested order

1. Slug minting (`utils/slug.py` + tests) — pure logic, fast feedback.
2. Embedder Protocol + MockEmbedder (`embedder.py` + tests).
3. Block CRUD on Store + tests.
4. `PaperHandler.get()` overview + view modes + tests (no search yet).
5. Block-level search (`search_blocks_lexical/semantic/fused`) + tests.
6. `PaperHandler.search()` wiring on top.
7. `Store.ingest_bundle()` + tests using a fixture bundle.
8. CLI `jobs ingest-bundles`.
9. Real `BgeM3Embedder` (optional dep).
10. `docs/design/v2-cutover.md`.
11. Skill draft polish.

Each step gets its own commit. Don't pile a 1500-line PR.

## Conventions (non-negotiable)

- **Sync only below FastMCP.** No `async`/`await`. Use psycopg 3.
- **No setuptools entry points.** Register handlers in
  `src/precis/registry.py` `builtins(store=...)`.
- **`pos = -1`** in DB = ref-level. Python sees `None` at boundaries.
- **JSONB via `psycopg.types.json.Jsonb()`** wrapper at call sites.
- **Tests** use `fresh_db` + `store` fixtures from `tests/conftest.py`.
- **No reaching into v1 DB.** `.acatome` bundles are the canonical
  interchange. v1 stays untouched.

## Verification before each commit

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

All four must pass. Smoke-test the CLI when relevant:

```bash
uv run precis --help
uv run precis jobs --help
uv run precis jobs ingest-bundles --dry-run /path/to/some/bundles/
```

## Out of scope (defer to later phases)

- Cache-backed kinds (`web`, `youtube`, `math`) — phase 4
- Other numeric kinds (todo, gripe, fc) — phase 5
- File kinds (docx, tex) — phase 6
- Handler class hierarchy refactor (`RefHandler`, `NumericRefHandler`,
  `SlugRefHandler`) — small follow-up after phase 3 ships
- Ansible role for v2 deployment — separate task, after phase 3
- Publishing to PyPI — only after phase 5+ when v2 is feature-complete

## Done state

Phase 3 is "done" when:

- `pytest -q` shows ≥130 tests passing (88 from p1+p2 plus ~40 new)
- `precis jobs ingest-bundles ~/.acatome/papers/` ingests successfully
- `precis serve` running, agent can `get(kind='paper', id='wang2020state')`
  and get a sensible overview
- `precis serve` running, agent can `search(q='nitrate', kind='paper')`
  and get block-level hits
- `docs/design/v2-cutover.md` written
- `git log --oneline -10` shows phase-3 commits, each focused on one
  step from the order above

Then it's `v2 phase 3: paper kind + bundle ingest` ready to commit.
