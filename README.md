# precis-mcp

[![check](https://github.com/retospect/precis-mcp/actions/workflows/check.yml/badge.svg)](https://github.com/retospect/precis-mcp/actions/workflows/check.yml)
[![PyPI](https://img.shields.io/pypi/v/precis-mcp.svg)](https://pypi.org/project/precis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/precis-mcp.svg)](https://pypi.org/project/precis-mcp/)
[![License](https://img.shields.io/github/license/retospect/precis-mcp.svg)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that
gives language-model agents a small, uniform API for reading, writing,
and searching across papers, documents, personal state, code, and
cached tool calls. Small-model-friendly (7B-class agents are the design
target); stores content in PostgreSQL with `pgvector`.

> **Status.** v8.0.0 — ground-up redesign of v1. Twenty-two kinds
> shipping across ref / tool / discovery categories, seven verbs,
> plugin surface stable. The discovery layer (persistent
> per-segment keywords + per-sentence embeddings) and the
> verifier-workflow `citation` kind landed 2026-05-31; see
> [`docs/design/storage-v2.md § Discovery layer`](docs/design/storage-v2.md).
> v5.2.6 on PyPI is the last v1-line release;
> see [`CHANGELOG.md`](CHANGELOG.md) for the migration path.

## What it does

One tool surface — **seven verbs** discriminated by a single `kind=`
argument — over three categories of content:

- **Ref kinds** (content addressed by slug or integer id): `paper`,
  `skill`, `oracle`, `quest`, `conv`, `markdown`, `plaintext`,
  `python`, `todo`, `memory`, `gripe`, `fc` (flashcard),
  `citation` (verified claim → source quote).
- **Tool kinds** (stateless or cache-backed; pass `q=` or `id=`, get
  text back): `calc`, `math` (Wolfram), `youtube`, `web` (fetch +
  search + bookmark), `websearch` / `think` / `research` (Perplexity
  Sonar tiers), `patent` (EPO OPS).
- **Discovery kind**: `random` — pick a random indexed block to
  stumble into content when you don't know what to ask for.

The active set depends on which optional extras and env vars are
configured (see [Install](#install)). Run
`get(kind='skill', id='precis-help')` against a live server for the
live enumeration of kinds currently wired (it's a synthesised skill
that introspects the registry); pair with
`get(kind='skill', id='precis-overview')` for the design-rationale
tour.

## Seven verbs

| Verb     | Use when                                            |
|----------|-----------------------------------------------------|
| `get`    | You know the **name** (slug, id, file path) — or you're calling a tool. |
| `search` | You're looking for **content** by topic or phrase. Hybrid lexical (tsvector) + semantic (pgvector) with RRF fusion. |
| `put`    | Create a new ref. Optionally tag and link on creation. |
| `edit`   | Rewrite a region of a file-kind ref by content anchors (`find-replace`, `append`, `insert`, `replace`). |
| `delete` | Soft-delete a numeric ref, or delete a region from a file kind by selector. |
| `tag`    | Add and/or remove tags. Three namespaces: closed (`STATUS:done`), flag (`pinned`), open (`topic-foo`). |
| `link`   | Add or remove a cross-link to another ref. Vocabulary: `related-to`, `blocks`, `contradicts`, `cites`, `derived-from`, `supports`, … |

Address by `id=` for names, `q=` for content. No URI selector strings
for ids; region selectors inside files use the compact `slug~SELECTOR`
shape (e.g. `notes--meeting~L42-58`).

## Install

```bash
pip install 'precis-mcp[all]'
```

Extras (each enables its kinds; omit any you don't want):

| Extra       | Enables                                           | Heavy? |
|-------------|---------------------------------------------------|--------|
| `paper`     | `paper` kind (sentence-transformers bge-m3 + `acatome-extract`) | yes (~2 GB model on first load) |
| `calc`      | `calc` kind (sympy)                               | no |
| `external`  | `math` (Wolfram), `youtube`, `web`, Perplexity trio | no |
| `patent`    | `patent` kind (EPO Open Patent Services)          | no |
| `docx`      | (queued — not yet wired)                          | — |
| `tex`       | (queued — not yet wired)                          | — |
| `plot`      | (queued — not yet wired)                          | — |
| `all`       | All of the above.                                 | yes |

A bare `pip install precis-mcp` gives you the state kinds (`todo`,
`memory`, `gripe`, `fc`, `quest`, `conv`, `oracle`, `skill`,
`random`) and the `markdown` / `plaintext` / `python` file kinds.
Optional deps surface as `InitError` at boot: the kind silently drops
off the tool surface with a WARNING, the server stays up.

### Database

`precis-mcp` requires PostgreSQL with the `pgvector` extension. The
CLI `precis migrate` applies the forward-only numbered SQL migrations
in `src/precis/migrations/`. See
[`docs/store_sketch.py`](docs/store_sketch.py) for the Python store
interface and `0001_initial.sql` for the schema.

```bash
createdb precis
psql precis -c 'CREATE EXTENSION pgvector;'

export PRECIS_DATABASE_URL=postgresql://localhost/precis
export PRECIS_EMBEDDER=bge-m3   # or "mock" for tests
precis migrate
```

## Run

`precis serve` speaks MCP over stdio. Wire it into your agent's MCP
config:

```json
{
  "mcpServers": {
    "precis": {
      "command": "precis",
      "args": ["serve"],
      "env": {
        "PRECIS_DATABASE_URL": "postgresql://localhost/precis",
        "PRECIS_EMBEDDER": "bge-m3",
        "PRECIS_ROOT": "/absolute/path/to/notes",
        "PRECIS_PYTHON_ROOTS": "myrepo:/absolute/path/to/myrepo"
      }
    }
  }
}
```

### Environment variables

| Var                           | Purpose                                          |
|-------------------------------|--------------------------------------------------|
| `PRECIS_DATABASE_URL`         | Postgres DSN (required for all ref kinds).       |
| `PRECIS_EMBEDDER`             | `"mock"` (dev/tests) or `"bge-m3"` (prod).       |
| `PRECIS_ROOT`                 | Single root dir for `markdown` / `plaintext` / `tex` kinds. The trio is hidden when unset; every read/write is normalised against this path (`Path.resolve()` + `relative_to`). |
| `PRECIS_PYTHON_ROOTS`         | `alias:/path,alias2:/path2` — exposed Python repos. |
| `PRECIS_PYTHON_ALLOW_EXEC=1`  | Gate for `python` runtrace (spawns subprocess).  |
| `EPO_OPS_CLIENT_KEY` + `_SECRET` + `PRECIS_PATENT_RAW_ROOT` | Enables `patent` kind. |
| `WOLFRAM_APP_ID`              | Enables `math` kind.                             |
| `PERPLEXITY_API_KEY`          | Enables `websearch` / `think` / `research`.      |
| `LOG_LEVEL`                   | `DEBUG` / `INFO` / `WARNING` / `ERROR`.          |

## Design highlights

- **Seven verbs, one `kind=`**. The whole surface is
  `get`/`search`/`put`/`edit`/`delete`/`tag`/`link`. No per-kind
  bespoke tools. See
  [`docs/seven-verb-surface-migration.md`](docs/seven-verb-surface-migration.md).
- **Content-anchored edits.** `edit(find=..., before=..., after=...)`
  resolves by literal content match; unique/first/all/nth policy;
  fuzzy nearest-line hint on not-found. Pure resolver in
  `precis.utils.edit_resolve`; ships for `markdown`, `plaintext`, and
  `python`. See [`docs/edit-protocol-spec.md`](docs/edit-protocol-spec.md).
- **Hybrid search.** Lexical `tsvector` + semantic `pgvector` (bge-m3)
  with Reciprocal Rank Fusion. Block-level; paper chunks, markdown
  paragraphs, Perplexity answers, web pages all searchable.
- **Persistent discovery layer.** Papers get pre-computed per-segment
  matryoshka-ordered keywords (distinctiveness-penalty scored against
  sibling segments) and per-sentence bge-m3 embeddings. The TOC view
  serves from `ref_segments` directly — no per-request DP/KeyBERT
  recompute. Search hits carry indented `excerpt @ ~N: "..."`
  sub-lines, picked by pgvector cosine rerank against the query
  embedding, so result rows are actionable for triage without a
  second fetch. The `citation` kind closes the loop: an agent's
  writing-thread workflow can persist verified `claim → source quote`
  records (see [`precis-citation-help`](src/precis/data/skills/precis-citation-help.md)).
- **Progressive disclosure.** Seven verbs and a `kind=` argument is
  the *whole* visible surface. Behind it sits a fan-out of ~25
  per-kind help skills, dozens of read views, an anchored edit
  protocol, args-dict view payloads, and a tag/link vocabulary —
  none of which the agent has to know up front. Every response can
  emit a `next=` breadcrumb, every error names the skill that
  explains it, and `get(kind='skill', id='precis-<kind>-help')`
  unfolds the manual for whichever capability the agent just
  bumped into. Think *exploding pocket knife*: the tool grows
  blades as you reach for them, instead of advertising 20
  unfamiliar buttons in `tools/list`. (UX literature calls this
  pattern progressive disclosure.)
- **HintBus.** Any layer can emit deduplicated, novelty-decayed tips
  that are rendered after the verb's main output. Keeps slim models
  from drowning in self-inflicted reminders.
- **Slim exception surface.** `BadInput` / `NotFound` / `Gone` /
  `Unsupported` / `Upstream` / `RateLimited` / `Internal`, each
  carrying a single copy-pasteable `next=` "breaking hint".
- **`psycopg 3` sync, raw SQL.** No SQLAlchemy, no Alembic, no async
  below FastMCP — stdio's serial workload doesn't buy anything from
  async.
- **In-tree handlers, entry-point plugins.** Core kinds are
  hand-ordered in `precis.dispatch.boot()`. Third-party kinds can
  register themselves via the `precis.handlers` entry-point group
  without forking — see
  [`docs/plugin-authoring.md`](docs/plugin-authoring.md).

## Extending

Write a plugin handler in 3 steps — see the one-pager at
[`docs/plugin-authoring.md`](docs/plugin-authoring.md) and the
canonical tiny example in
[`src/precis/handlers/calc.py`](src/precis/handlers/calc.py).

```toml
# your plugin's pyproject.toml
[project]
dependencies = ["precis-mcp>=8.0.0"]

[project.entry-points."precis.handlers"]
wikipedia = "precis_wikipedia:WikipediaHandler"
```

Plugin failures are logged and skipped — one bad plugin cannot brick
the server.

## CLI

```text
precis serve                       # Start the MCP stdio server.
precis migrate                     # Run pending SQL migrations.
precis jobs ingest [root]          # Pre-warm .md / .txt / .tex under PRECIS_ROOT
                                   #   (mtime-gated; compose into launchers:
                                   #    `precis jobs ingest && precis serve`).
precis jobs ingest-bundle[s] ...   # Ingest .acatome paper bundles.
precis jobs ingest-oracles ...     # Seed the oracle kind from YAML wisdom files.
precis jobs dedupe-papers          # Collapse duplicate paper refs.
precis jobs import-perplexity ...  # Bulk-import Perplexity web-UI answers.
precis jobs watch-patents / run-patent-watches / sweep-patent-fulltext
                                   # Saved CQL patent watches (patent kind).
```

Run any subcommand with `--help` for detailed options.

### Utility scripts

The `scripts/` dir holds workspace-side utilities that run *against*
a precis store but live outside the published CLI surface. See
[`scripts/README.md`](scripts/README.md) for full coverage; the
high-traffic ones:

- **`paper-monitor-ingest-dir`** — drop-and-go PDF ingest watcher.
- **`perplexity-monitor-ingest-dir`** — bulk-import Perplexity
  markdown exports.
- **`find-citing-papers`** — sweep S2 for new papers citing the
  precis corpus, with bge-m3 cosine rerank and several noise-
  reduction filters; reports land in
  [`paper-ingest/`](../../../paper-ingest/README.md).
- **`enrich-paper-identifiers`** / **`retrofit-acatome-external-ids`**
  — backfill DOI / arXiv ids on legacy refs.

## Roadmap

- `docx`, `tex`, `book`, `rmk` file handlers (Phase 6b/c).
- `web` bookmark mode + Wayback enrichment (gripe:3681 phase 2 + 4 — see [`OPEN-ITEMS.md`](OPEN-ITEMS.md)).
- `voice` kind — STT/TTS bound to transcript refs (see [`docs/voice-kind-spec.md`](docs/voice-kind-spec.md)).
- SDK extraction (`precis-core`) once the plugin API has settled.

## Documentation

- [`docs/plugin-authoring.md`](docs/plugin-authoring.md) — write a third-party handler.
- [`docs/seven-verb-surface-migration.md`](docs/seven-verb-surface-migration.md) — verb surface design rationale.
- [`docs/edit-protocol-spec.md`](docs/edit-protocol-spec.md) — anchored edits across file kinds.
- [`docs/file-kinds-unified-addressing.md`](docs/file-kinds-unified-addressing.md) — the `slug~SELECTOR` address grammar.
- [`docs/python-kind-spec.md`](docs/python-kind-spec.md) — python navigator design.
- [`docs/patent-kind-spec.md`](docs/patent-kind-spec.md) — EPO OPS integration.
- [`docs/paper_ingest.md`](docs/paper_ingest.md) — `.acatome` bundle ingest path.
- [`docs/design/storage-v2.md`](docs/design/storage-v2.md) — full schema + discovery-layer design.
- [`src/precis/data/skills/precis-citation-help.md`](src/precis/data/skills/precis-citation-help.md) — `citation` kind + verifier-workflow agent surface.
- [`src/precis/data/skills/precis-toc-help.md`](src/precis/data/skills/precis-toc-help.md) — TOC machinery (segments, sentences, matryoshka keywords).
- [`CHANGELOG.md`](CHANGELOG.md) — what shipped in each phase.

## Contributing

The repo lives at
[`retospect/precis-mcp`](https://github.com/retospect/precis-mcp).
Issues and PRs welcome. Development workflow:

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[all]' --group dev
pytest -q
ruff check . && ruff format --check .
mypy src tests
```

## License

GPL-3.0-or-later. See the full text at
[gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html).
