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

> **Status.** v8.13.0 — see the git history (`git log`) for the
> live story. v5.2.6 on PyPI is the last v1-line release.
>
> **README is stale beyond this banner.** Things the architecture
> section + verb table below still capture correctly: seven-verb
> surface, `kind=` dispatch, `pgvector` hybrid search, content
> kinds vs tool kinds. Things you should treat the ref-kinds list
> as a sample of, not a catalogue:
>
> * **The todo tree (Slices 1–5).** `kind='todo'` is now a
>   hierarchical task graph with `parent_id`, level gradient
>   (`strategic|tactical|recurring|subtask`), PRIO column on
>   refs, `meta.auto_check` wait-for-condition leaves,
>   `meta.schedule` recurring spawn (Watches umbrella), and
>   review tiers (`nursery` SQL-only hourly, `structural` 6h
>   opus, `deep_review` weekly opus).
> * **`kind='job'` as child of `kind='todo'`.** Slice 5: every
>   new job requires `parent_id` pointing at a todo. The
>   `dispatch` worker is the canonical path from `meta.executor`
>   on a todo to a queued job.
> * **Worker consolidation.** Two long-running worker daemons
>   (`precis worker --profile=system` everywhere,
>   `--profile=agent` on gateway) handle every pass between
>   them; per-pass LaunchDaemons are retired.
>
> The skill catalogue under
> `src/precis/data/skills/precis-*-help.md` is authoritative for
> the LLM-facing surface. Start at `precis-toolpath-help`
> ("I want to X — what do I call?") and
> `precis-overview` (the kinds + skill index).

## What it does

One tool surface — **seven verbs** discriminated by a single `kind=`
argument — over three categories of content:

- **Ref kinds** (content addressed by slug or integer id): `paper`,
  `skill`, `oracle`, `conv`, `markdown`, `plaintext`, `tex`,
  `python`, `todo`, `memory`, `gripe`, `flashcard`,
  `citation` (verified claim → source quote), `finding`
  (reviewer-persona claim + chase chain), `job` (offline LLM run,
  child of a `todo`), `provenance` (derivation audit trail),
  `pres` (slide decks), `cad` (parametric solid-model design probed
  analytically — point/ray/section/clearance — not meshed; ADR 0041),
  `pcb` (netlist + placement graph read as a traversable graph,
  exported to BOM/CPL/DSN + Freerouting; ADR 0042).
  This is a sample — `precis-overview` and
  the synthesised `precis-help` skill enumerate the live set.
- **Tool kinds** (stateless or cache-backed; pass `q=` or `id=`, get
  text back): `calc`, `math` (Wolfram), `youtube`, `web` (fetch +
  search + bookmark), `websearch` / `perplexity-reasoning` /
  `perplexity-research` (Perplexity Sonar tiers), `patent` (EPO OPS).
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
| `web`       | `precis web` browser UI (FastAPI + Jinja + HTMX)  | no |
| `tex`       | `tex` kind — `.tex` files under `PRECIS_ROOT` (lxml) | no |
| `docx`      | (queued — not yet wired)                          | — |
| `plot`      | (queued — not yet wired)                          | — |
| `all`       | All of the above.                                 | yes |

A bare `pip install precis-mcp` gives you the state kinds (`todo`,
`memory`, `gripe`, `flashcard`, `conv`, `oracle`, `skill`,
`random`) and the `markdown` / `plaintext` / `python` file kinds.
(The `tex` file kind also rides on `PRECIS_ROOT`, but its `.tex`
parsing pulls in the `[tex]` extra's `lxml`.)
Optional deps surface as `InitError` at boot: the kind silently drops
off the tool surface with a WARNING, the server stays up.

### Database

`precis-mcp` requires PostgreSQL with the `pgvector` extension. The
CLI `precis migrate` applies the forward-only numbered SQL migrations
in `src/precis/migrations/`. See `0001_initial.sql` for the schema.

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
| `PRECIS_OWNER`                | Canonical username for the human running this instance — the author stamped on a web "ask a follow-up" and the `user:<owner>` addressee of an `ask-user` pause. Defaults to `owner`. |
| `PRECIS_EMBEDDER`             | `"mock"` (dev/tests), `"bge-m3"` (in-process), or `"remote"` (HTTP client to `precis serve-embeddings`). |
| `PRECIS_EMBEDDER_URL`         | Required for `remote`: ordered, comma-separated base URL(s), e.g. `http://127.0.0.1:8181`. First healthy endpoint wins; rest are fallback. |
| `PRECIS_ROOT`                 | Single root dir for `markdown` / `plaintext` / `tex` kinds. The trio is hidden when unset; every read/write is normalised against this path (`Path.resolve()` + `relative_to`). |
| `PRECIS_PYTHON_ROOTS`         | `alias:/path,alias2:/path2` — exposed Python repos. |
| `PRECIS_PYTHON_ALLOW_EXEC=1`  | Gate for `python` runtrace (spawns subprocess).  |
| `EPO_OPS_CLIENT_KEY` + `_SECRET` + `PRECIS_PATENT_RAW_ROOT` | Enables `patent` kind. |
| `WOLFRAM_APP_ID`              | Enables `math` kind.                             |
| `PERPLEXITY_API_KEY`          | Enables `websearch` / `perplexity-reasoning` / `perplexity-research`. |
| `PRECIS_CORPUS_DIR`           | Corpus root(s) for the `precis web` paper viewer. An `os.pathsep`-separated list is allowed (e.g. `/opt/a/corpus:/opt/b/corpus`); the web tries each `<root>/<letter>/<cite_key>.pdf` in order and serves the first that exists. Point it at the same path the ingest watcher writes to. |
| `LOG_LEVEL`                   | `DEBUG` / `INFO` / `WARNING` / `ERROR`.          |

## Design highlights

- **Seven verbs, one `kind=`**. The whole surface is
  `get`/`search`/`put`/`edit`/`delete`/`tag`/`link`. No per-kind
  bespoke tools. See
  [`docs/user-facing/seven-verb-surface-migration.md`](docs/user-facing/seven-verb-surface-migration.md).
- **Content-anchored edits.** `edit(find=..., before=..., after=...)`
  resolves by literal content match; unique/first/all/nth policy;
  fuzzy nearest-line hint on not-found. Pure resolver in
  `precis.utils.edit_resolve`; ships for `markdown`, `plaintext`, and
  `python`. See [`docs/user-facing/edit-protocol-spec.md`](docs/user-facing/edit-protocol-spec.md).
- **Hybrid search.** Lexical `tsvector` + semantic `pgvector` (bge-m3)
  with Reciprocal Rank Fusion. Block-level; paper chunks, markdown
  paragraphs, Perplexity answers, web pages all searchable.
- **Per-chunk discovery layer (F20).** Every body chunk gets
  KeyBERT keywords stored on `chunks.keywords TEXT[]` (GIN-indexed
  canonical forms) + `chunks.keywords_meta JSONB` (versioned
  short/long pairs with bge-m3 cosine scores), populated by the
  `chunk_keywords` worker. The paper TOC view (`view='toc'`)
  DP-clusters those keyword arrays at request time
  (`src/precis/utils/toc_db.py`) — superseding the dropped
  `ref_segments` / `ref_segment_sentences` precompute. The
  `citation` kind closes the loop: an agent's writing-thread
  workflow can persist verified `claim → source quote` records (see
  [`precis-citation-help`](src/precis/data/skills/precis-citation-help.md)).
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
  [`docs/user-facing/plugin-authoring.md`](docs/user-facing/plugin-authoring.md).

## Extending

Write a plugin handler in 3 steps — see the one-pager at
[`docs/user-facing/plugin-authoring.md`](docs/user-facing/plugin-authoring.md) and the
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
precis web [--host H --port P]      # Browser UI: Tasks / Papers / Console /
                                   #   Conversations / Status tabs (needs the
                                   #   [web] extra; binds 127.0.0.1:9100, no
                                   #   auth — reach it over Tailscale). Papers
                                   #   carry DOI/arXiv verify links; PDFs serve
                                   #   from PRECIS_CORPUS_DIR (multi-root);
                                   #   conversations render as a transcript.
precis serve-embeddings            # Run the HTTP embedding service (the
                                   #   server side of PRECIS_EMBEDDER=remote;
                                   #   /healthz /readyz /model /embed /metrics).
precis worker                      # Drive the derived-artifact queue.
precis migrate                     # Run pending SQL migrations.
precis schema-doc                  # Generate the Mermaid ER diagram of the
                                   #   DB schema (docs/design/schema.md) from a
                                   #   DSN or piped rows. scripts/gen-schema
                                   #   wraps it to regen from prod over ssh.
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
  reduction filters; reports land in a `paper-ingest/` review dir.
- **`enrich-paper-identifiers`** / **`retrofit-acatome-external-ids`**
  — backfill DOI / arXiv ids on legacy refs.

## Roadmap

- `docx`, `book`, `rmk` file handlers (Phase 6b/c). (`tex` shipped.)
- `web` bookmark mode + Wayback enrichment (gripe:3681 phase 2 + 4 — see [`OPEN-ITEMS.md`](OPEN-ITEMS.md)).
- `voice` kind — STT/TTS bound to transcript refs (see [`docs/user-facing/voice-kind-spec.md`](docs/user-facing/voice-kind-spec.md)).
- SDK extraction (`precis-core`) once the plugin API has settled.

## Documentation

- [`AGENTS.md`](AGENTS.md) — **start here to contribute or change code.** The canonical guide: conventions, workflow, definition-of-done, ingest guarantees.
- [`docs/README.md`](docs/README.md) — the documentation landing index (directory-by-directory map).
- [`docs/architecture.md`](docs/architecture.md) — the system manual: a narrative overview tying the surface, kinds, storage, todo-tree, and workers together.
- [`docs/design/schema.md`](docs/design/schema.md) — the **generated** DB schema diagram (Mermaid ER, produced from the live database — can't drift).
- [`docs/decisions/README.md`](docs/decisions/README.md) — the ADR index (one record per decision; supersession graph). The individual ADRs live in [`docs/decisions/`](docs/decisions/).
- [`docs/user-facing/plugin-authoring.md`](docs/user-facing/plugin-authoring.md) — write a third-party handler.
- [`docs/user-facing/seven-verb-surface-migration.md`](docs/user-facing/seven-verb-surface-migration.md) — verb surface design rationale.
- [`docs/user-facing/edit-protocol-spec.md`](docs/user-facing/edit-protocol-spec.md) — anchored edits across file kinds.
- [`docs/user-facing/file-kinds-unified-addressing.md`](docs/user-facing/file-kinds-unified-addressing.md) — the `slug~SELECTOR` address grammar.
- [`docs/user-facing/python-kind-spec.md`](docs/user-facing/python-kind-spec.md) — python navigator design.
- [`docs/user-facing/patent-kind-spec.md`](docs/user-facing/patent-kind-spec.md) — EPO OPS integration.
- [`docs/user-facing/paper_ingest.md`](docs/user-facing/paper_ingest.md) — `.acatome` bundle ingest path.
- [`docs/design/storage-v2.md`](docs/design/storage-v2.md) — full schema + discovery-layer design.
- [`docs/decisions/0026-precis-web-surface.md`](docs/decisions/0026-precis-web-surface.md) — the `precis web` browser UI (Tasks / Papers / Conversations / Console / Status).
- [`docs/design/precis-web-papers-conv-polish.md`](docs/design/precis-web-papers-conv-polish.md) — paper DOI/arXiv links, multi-root corpus PDF serving, conversation transcript view.
- [`docs/decisions/0029-multi-root-corpus-pdf.md`](docs/decisions/0029-multi-root-corpus-pdf.md) — why `PRECIS_CORPUS_DIR` accepts a list of roots.
- [`src/precis/data/skills/precis-citation-help.md`](src/precis/data/skills/precis-citation-help.md) — `citation` kind + verifier-workflow agent surface.
- [`src/precis/data/skills/precis-toc-help.md`](src/precis/data/skills/precis-toc-help.md) — TOC machinery (segments, sentences, matryoshka keywords).
- Git history (`git log`) — what shipped in each phase (no CHANGELOG file).

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
