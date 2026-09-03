# precis-mcp

[![check](https://github.com/retospect/precis-mcp/actions/workflows/check.yml/badge.svg)](https://github.com/retospect/precis-mcp/actions/workflows/check.yml)
[![PyPI](https://img.shields.io/pypi/v/precis-mcp.svg)](https://pypi.org/project/precis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/precis-mcp.svg)](https://pypi.org/project/precis-mcp/)
[![License](https://img.shields.io/github/license/retospect/precis-mcp.svg)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that
gives language-model agents a small, uniform API for reading, writing,
and searching across papers, documents, personal state, code, and
cached tool calls. Small-model-friendly (7B-class agents are the design
target); stores content in PostgreSQL with `pgvector`, with a web
interface ([see how it works](guide/README.md)).

## See it

A narrated tour of the web interface, section by section, right in
this repo: **[guide/README.md](guide/README.md)**. Video: coming.

**Set it up:** single machine —
[`docs/setup-single-machine.md`](docs/setup-single-machine.md) ·
cluster (multi-host, ansible) — [`deploy/README.md`](deploy/README.md).

> **Status.** Actively developed on the v8 line. There is no
> CHANGELOG — `git log` is the change story. The kinds catalogue
> below is a *living* set: the authoritative, build-specific
> enumeration is always `get(kind='skill', id='precis-help')`
> against a running server (it introspects the live registry),
> paired with `get(kind='skill', id='precis-overview')` for the
> guided tour. Agents should start at `precis-toolpath-help`
> ("I want to X — what do I call?").

## What it does

One tool surface — **eight verbs** discriminated by a single `kind=`
argument — over three categories of content. Ref kinds are addressed
by slug or integer id (output hands you a compact `<2-char><id>`
handle, e.g. `pa5` a paper, `me42` a memory); tool kinds take `q=`
or `id=` and hand back text.

- **Reading & reference** — `paper` (ingested research PDF),
  `patent` (EPO OPS record), `cfp` (call-for-proposal / spec doc),
  `oracle` (curated wisdom entry), `conv` (past conversation),
  `pres` (slide deck), `skill` (agent how-to — you're reading one).
- **Files under `PRECIS_ROOT` / code** — `markdown`, `plaintext`,
  `tex`, and `python` (symbol- and callgraph-aware repo navigator).
- **Authored artifacts** — `draft` (chunk-native document that
  exports to LaTeX/PDF/Word), `cad` (parametric
  solid-model design probed analytically, not meshed),
  `structure` (atomistic cell + bond graph for DFT/molecular work),
  `pcb` (netlist + placement graph → BOM/CPL/DSN + Freerouting), and
  `folder` (organizational container for the above).
- **Personal state & knowledge** — `todo` (hierarchical todo tree),
  `memory`, `gripe`, `anki` (spaced-repetition cloze cards → AnkiWeb),
  `citation` (verified claim → source quote), `finding`
  (chain-of-evidence over a citation chase), `job` (offline LLM run,
  child of a `todo`).
- **Identity, comms & audit** — `orcid` (researcher-identity hub),
  `cron` (push-notification scheduler),
  `message` (proactive outbound), `alert` (machine-detected ops
  condition), `agentlog` (per-run attribution trail), `provenance`
  (derivation audit).
- **Tool kinds** (stateless or cache-backed) — `calc` (local SymPy),
  `math` (Wolfram), `youtube` (transcript), `web` (fetch + extract),
  `wikipedia` (on-demand article), `websearch` /
  `perplexity-reasoning` / `perplexity-research` (Perplexity Sonar
  tiers).
- **Discovery** — `random`: pick a random indexed block to stumble
  into content when you don't know what to ask for.

The active set depends on which optional extras and env vars are
configured (see [Install](#install)) — a kind whose dependency or
env var is missing simply drops off the surface. This list is a
snapshot; `get(kind='skill', id='precis-help')` enumerates the kinds
wired in *your* build, and `get(kind='skill', id='precis-overview')`
gives the design-rationale tour with an example handle per kind.

## Eight verbs

| Verb     | Use when                                            |
|----------|-----------------------------------------------------|
| `get`    | You know the **name** (slug, id, file path) — or you're calling a tool. |
| `search` | You're looking for **content** by topic or phrase. Hybrid lexical (tsvector) + semantic (pgvector) with RRF fusion. |
| `put`    | Create a new ref. Optionally tag and link on creation. |
| `edit`   | Rewrite a region of a file-kind ref by content anchors (`find-replace`, `append`, `insert`, `replace`). |
| `delete` | Soft-delete a numeric ref, or delete a region from a file kind by selector. |
| `tag`    | Add and/or remove tags. Three namespaces: closed (`STATUS:done`), flag (`pinned`), open (`topic-foo`). |
| `link`   | Add or remove a cross-link to another ref. Vocabulary: `related-to`, `blocks`, `contradicts`, `cites`, `derived-from`, `supports`, … |
| `more`   | Fetch the next page of a truncated response (`more(cursor='…')` — every truncation footer hands you the cursor). |

Address by `id=` for names, `q=` for content. No URI selector strings
for ids; region selectors inside files use the compact `slug~SELECTOR`
shape (e.g. `notes--meeting~L42-58`).

## Install

```bash
pip install 'precis-mcp[all]'
```

**Deterministic, no-API tool kinds ship in core** — no extra needed: `calc`
(sympy + pint), `plot` / `figure` (matplotlib), `mermaid` (`mermaidx`, no
Node/Chromium), `docx` + `tex` export (python-docx / latex2mathml / lxml /
resvg), `cad` STL/3MF export (manifold3d CSG kernel), and `structure` CIF
I/O + symmetry (ASE + spglib). These are local, deterministic, torch-free
code, so they live in core rather than behind an extra that could go missing.

Extras cover the rest — network/API tools, torch-bound ML, and heavy or
host-specific packs (each enables its kinds; omit any you don't want):

| Extra        | Enables                                            | Heavy? |
|--------------|----------------------------------------------------|--------|
| `embed`      | In-process bge-m3 embedder (sentence-transformers + torch) — needed for `search` unless you point at a remote embedder | yes (~2 GB model on first load) |
| `paper`      | `paper` ingest — Marker PDF → chunks + CrossRef/S2 metadata | yes (pulls torch via Marker) |
| `external`   | `math` (Wolfram), `youtube`, `web`, Perplexity trio, `news` | no |
| `patent`     | `patent` kind (EPO Open Patent Services)           | no |
| `edgar`      | `edgar` kind — SEC EDGAR filings (httpx)           | no |
| `web`        | `precis web` browser UI (FastAPI + Jinja + HTMX)   | no |
| `cad-step`   | `cad` exact STEP export (OpenCASCADE B-rep)        | yes (~200 MB OCCT libs) |
| `pcb`        | `pcb` footprint resolution (LCSC → KiCad)          | no |
| `dft-ml`     | `structure` ML-potential relax (ASE + MACE-torch)  | yes (pulls torch) |
| `chem`       | `route` kind — retrosynthesis tool-pack (RDKit)    | yes (~150 MB) |
| `tts`        | Audio export — local TTS for voice drafts + the morning brief (Kokoro) | yes (host-specific) |
| `asa`        | `asa-bot` Discord bridge (discord.py)              | no |
| `all`        | `embed` + `paper` + `external` + `patent` + `edgar` + `web`. Excludes the heavier / specialized `cad-step`, `dft-ml`, `pcb`, `chem`, `tts`, `asa` tiers — install those explicitly. | yes |

A bare `pip install precis-mcp` gives you the state kinds (`todo`,
`memory`, `gripe`, `anki`, `conv`, `oracle`, `skill`, `random`), the
core deterministic tool kinds listed above, and the `markdown` /
`plaintext` / `python` / `tex` file kinds (the file kinds ride on
`PRECIS_ROOT`).
Optional deps surface as `InitError` at boot: the kind silently drops
off the tool surface with a WARNING, the server stays up.

### Database

`precis-mcp` requires PostgreSQL with the `pgvector` extension (SQL
extension name: `vector`). The CLI `precis migrate` applies the
forward-only numbered SQL migrations in `src/precis/migrations/`. See
`0001_initial.sql` for the schema.

```bash
createdb precis
psql precis -c 'CREATE EXTENSION vector;'

export PRECIS_DATABASE_URL=postgresql://localhost/precis
export PRECIS_EMBEDDER=bge-m3   # or "mock" for tests
precis migrate
```

Step-by-step single-machine runbook (worker, web UI, secrets):
[`docs/setup-single-machine.md`](docs/setup-single-machine.md). Cluster
(multi-host, ansible): [`deploy/README.md`](deploy/README.md).

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

### One-tool profile

Setting `PRECIS_MCP_PROFILE=command` in the server env collapses the
eight-tool surface into a single `precis(command=..., text=None)`
tool. `command` takes the same one-string call syntax the docs
already teach — e.g. `get(kind='skill', id='toc')` — parsed by
`src/precis/tools/command_parser.py`: one call, keyword args only,
`ast.literal_eval`-safe literal values; a bad call gets an
actionable `[error:BadInput]`, not a crash. `text=` is the escape
hatch for large bodies, so a caller doesn't have to quote-escape
them inside `command`. The frozen schema is ~850 bytes vs ~22 KB for
the typed per-verb schemas — cheaper cold-start, and a `tools/list`
block that never changes, so prompt caches keyed on it never
invalidate. Default profile is `typed` (unset = the per-verb tools
above).

`precis eval '<call>'` is the CLI twin: it evaluates one call string
against the same parser (`--text` / `--text-file` for the large-body
escape hatch) without building the multi-flag `precis tools <verb>
--flag value` form.

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
| `ORCID_CLIENT_ID` + `_SECRET` | Enables the `orcid` researcher-identity kind.    |
| `WOLFRAM_APP_ID`              | Enables `math` kind.                             |
| `PERPLEXITY_API_KEY`          | Enables `websearch` / `perplexity-reasoning` / `perplexity-research`. |
| `PRECIS_WEB_AUTH`             | `off` disables the `precis web` HTTP Basic gate (local dev only). Anything else — including unset — keeps it **on**: every route requires an account from `precis users`. |
| `PRECIS_WEB_PASSWORD_PEPPER`  | Vault-resident pepper HMAC'd into web passwords before scrypt, so a shareable *logical* `pg_dump` carries no crackable hashes. `precis users add` mints one on first use; you rarely set this by hand. |
| `PRECIS_CORPUS_DIR`           | Corpus root(s) for the `precis web` paper viewer. An `os.pathsep`-separated list is allowed (e.g. `/opt/a/corpus:/opt/b/corpus`); the web tries each `<root>/<letter>/<cite_key>.pdf` in order and serves the first that exists. Point it at the same path the ingest watcher writes to. |
| `LOG_LEVEL`                   | `DEBUG` / `INFO` / `WARNING` / `ERROR`.          |
| `PRECIS_MCP_PROFILE`          | `typed` (default, per-verb tools) or `command` (single `precis(command)` tool — see [One-tool profile](#one-tool-profile)). |

This table is the getting-started subset. precis reads ~150 `PRECIS_*`
variables in all — feature toggles, autonomy modes, budgets, model ids,
compute-routing, paths, and secrets. For the **exhaustive catalog** —
every var, its code default, the value deployed to each cluster service,
and an assessment of whether that state is right — see
[`docs/reference/config-variables.md`](docs/reference/config-variables.md).
The policy for *adding* a var (the three-tier scheme) is
[`docs/conventions/env-vars.md`](docs/conventions/env-vars.md).

## Design highlights

- **Eight verbs, one `kind=`**. The whole surface is
  `get`/`search`/`put`/`edit`/`delete`/`tag`/`link`/`more`. No
  per-kind bespoke tools.
- **Content-anchored edits.** `edit(find=..., before=..., after=...)`
  resolves by literal content match; unique/first/all/nth policy;
  fuzzy nearest-line hint on not-found. Pure resolver in
  `precis.utils.edit_resolve`; ships for `markdown`, `plaintext`, and
  `python`.
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
- **Progressive disclosure.** Eight verbs and a `kind=` argument is
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
- **The todo tree.** `kind='todo'` is a hierarchical todo graph — a
  level gradient (`strategic` → `tactical` → `subtask`, plus
  `recurring`), a PRIO sort key, `meta.auto_check` wait-for-condition
  leaves, and `meta.schedule` recurring spawn (the *Watches*
  umbrella). It is the unified substrate for intent, execution, and
  review; `kind='job'` (an offline LLM run) always hangs off a todo
  via `parent_id`, and the `minter` worker is the canonical path
  from a todo's `meta.executor` to a queued job. See
  [`precis-todo-tree-help`](src/precis/data/skills/precis-todo-tree-help.md).
- **Two-profile worker.** Every background pass runs under one of two
  long-running daemons: `precis worker --profile=system` (embeddings,
  keywords, minter, sweepers — safe to run on every node) and
  `--profile=agent` (the LLM-heavy review/planner rotation, each pass
  self-gated by env + a load-average ceiling). Per-pass daemons are
  retired.
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
  without forking — the contract lives in the `precis.dispatch`
  docstrings; `src/precis_chem/` is the richest first-party example.

## Extending

Write a plugin handler in 3 steps — the contract is documented in the
`precis.dispatch` docstrings (`_load_plugins`), with the
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
# Serving
precis serve                       # Start the MCP stdio server.
precis serve-embeddings            # HTTP embedding service (server side of
                                   #   PRECIS_EMBEDDER=remote; /healthz /readyz
                                   #   /model /embed /metrics).
precis web [--host H --port P]      # Browser UI: Tasks / Papers / Console /
                                   #   Conversations / Status tabs (needs the
                                   #   [web] extra; binds 127.0.0.1:9100 behind
                                   #   HTTP Basic — create an account first, or
                                   #   every page answers 503).

# Background processing
precis worker [--profile system|agent]
                                   # Drive the background passes. 'system'
                                   #   (default) = embeddings/keywords/minter/
                                   #   sweepers; 'agent' = the LLM-heavy review
                                   #   + planner rotation. --only X --once for
                                   #   ad-hoc backfills.
precis watch [PATH]                # Watch an inbox dir and ingest dropped PDFs
                                   #   (papers / books / presentations routing).
precis add <pdf|url>               # Ingest one paper on the spot.

# Web accounts (precis web logins; every account is fully authorized)
precis users add <login> --abbrev <ab> [--name N --email E]
                                   # Create an account; password from a no-echo
                                   #   prompt (or --password-stdin). Never argv.
precis users list                  # The roster.
precis users passwd <login>        # THE recovery path — Basic auth has no
                                   #   email reset flow, by design.
precis users disable|enable|rm <login>
precis users feed-token <login>    # Mint + print the private podcast feed URL
                                   #   (?t=… , since podcast apps handle Basic
                                   #   on enclosures inconsistently).
                                   # Signed-in users do the self-service half —
                                   #   password, profile, podcast link — at
                                   #   /account in the web UI. Creating and
                                   #   removing accounts stays here.

# Database
precis migrate                     # Run pending forward-only SQL migrations.
precis db ...                      # Schema utilities (dump-schema, …).
precis schema-doc                  # Generate the Mermaid ER diagram
                                   #   (docs/reference/schema.md) from a DSN.

# Interactive & inspection
precis repl                        # Interactive verb console (tab-complete).
precis draft ...                   # Manage / export draft-kind documents.
precis stats | logs | stubs | verify
                                   # Corpus stats, event logs, stub triage,
                                   #   integrity checks.
precis stats --utilization [--hours N]
                                   # Hourly CPU (host_heartbeat_log) + LLM
                                   #   (llm_call_log) utilization + idle gaps.
precis cron | heartbeat            # Scheduler tick / liveness ping.

# Claim-hub curation (taproot)
precis taproot ...                 # Claim-hub authoring/repair: mint / refine /
                                   #   merge / backfill / backfill-grounding /
                                   #   repair-evidence / direct-mint / lint.
precis taproot verify-edges        # Certify withheld/unverified evidence edges
                                   #   for the publish preflight (stamps the
                                   #   meta.support verdict; dry-run default).
precis taproot reword-sweep        # LLM batch reword of lint-blocked claim hub
                                   #   sentences through the retitle door
                                   #   (dry-run default).

# One-shot jobs
precis jobs ingest[-md|-oracles] ...   # Pre-warm files under PRECIS_ROOT.
precis jobs import-perplexity ...      # Bulk-import Perplexity web-UI answers.
precis jobs {watch,list,run}-patent-watches / sweep-patent-fulltext
                                       # Saved CQL patent watches (patent kind).
precis jobs check-provenance / sync-retraction-watch
                                       # Provenance + retraction audits.
```

Run any subcommand with `--help` for the full option list.

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

- `book`, `rmk` file handlers. (`tex` and `docx` shipped.)
- `web` bookmark mode + Wayback enrichment (gripe:3681 phase 2 + 4 — see [`docs/backlog/`](docs/backlog/README.md)).
- `voice` kind — STT/TTS bound to transcript refs (spec: [`docs/backlog/voice-kind-spec.md`](docs/backlog/voice-kind-spec.md)).
- SDK extraction (`precis-core`) once the plugin API has settled.

## Documentation

- [`AGENTS.md`](AGENTS.md) — **start here to contribute or change code.** The canonical guide: conventions, workflow, definition-of-done, ingest guarantees.
- [`docs/mission.md`](docs/mission.md) — the mission, the pitch narrative, and the current corpus facts (positioning, not architecture — the single source for decks and talks).
- [`docs/README.md`](docs/README.md) — the documentation landing index (directory-by-directory map).
- [`docs/codebase.md`](docs/codebase.md) — orientation: invariants, lifecycle, seams, and the generated package map (subsystem detail lives in each package's `__init__.py` docstring).
- [`docs/reference/schema.md`](docs/reference/schema.md) — the **generated** DB schema diagram (Mermaid ER, produced from the live database — can't drift).
- [`docs/reference/config-variables.md`](docs/reference/config-variables.md) — the full `PRECIS_*` config catalog: every var, its default, the value deployed to each cluster service, and a correctness assessment.
- [`docs/reference/schema.md`](docs/reference/schema.md) — generated schema (full ER view: `schema-v2.svg`).
- [`src/precis/data/skills/precis-citation-help.md`](src/precis/data/skills/precis-citation-help.md) — `citation` kind + verifier-workflow agent surface.
- [`src/precis/data/skills/precis-toc-help.md`](src/precis/data/skills/precis-toc-help.md) — TOC machinery (segments, sentences, matryoshka keywords).
- Git history (`git log`) — what shipped in each phase (no CHANGELOG file).

## Contributing

The repo lives at
[`retospect/precis-mcp`](https://github.com/retospect/precis-mcp).
Issues and PRs welcome. Development workflow:

```bash
uv sync --all-extras --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

Run the **full test suite in the dev container**, which bakes every
optional extra and wires the test database:

```bash
scripts/dev pytest                       # full suite, all extras
scripts/dev bash -lc "ruff check . && ruff format --check . && mypy src tests && pytest"
```

A host `uv run pytest` only sees the torch-free base install, so the
full run there fails with spurious missing-extra errors (`sympy`,
`marker`, `lxml`, …) — use it for targeted subsets only.

All tooling goes through `uv run` (host) or `scripts/dev` (container)
— see [`AGENTS.md`](AGENTS.md) for the full workflow and
definition-of-done.

## License

GPL-3.0-or-later. See the full text at
[gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html).
