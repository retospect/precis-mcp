# Single-machine setup

Fresh machine to a working precis: MCP server + worker + web UI. Cluster
instead? → [`deploy/README.md`](../deploy/README.md).

## 1. Install

```bash
pip install 'precis-mcp[all]'
```

Python >=3.12. `all` = `embed` + `paper` + `web`; the light API-client
kinds (`math`, `youtube`, `patent`, `edgar`, the Perplexity trio, …) ship
in core and gate on their credential, not an extra. See
[`README.md` §Install](../README.md#install) for the full extras table
(deterministic-in-core vs. extra-gated kinds, and what each extra needs).

## 2. Database

PostgreSQL with the `vector` extension (pgvector).

```bash
createdb precis
psql precis -c 'CREATE EXTENSION vector;'

export PRECIS_DATABASE_URL=postgresql://localhost/precis
precis migrate
```

`precis migrate` on a fresh DB loads the baseline schema snapshot
(`migrations/baseline/schema.sql`) then applies the post-snapshot migration
tail (`src/precis/cli/migrate.py`).

### Upgrading

```bash
pip install -U 'precis-mcp[all]'   # or: uv tool upgrade precis-mcp
precis migrate                     # then restart serve / worker / web
```

Every long-running entrypoint (`precis serve`, `precis web`,
`precis worker`, `precis ingest --watch`, `precis alertmanager-receiver`)
checks the migration ledger at startup and **exits non-zero while
migrations are pending** (the `PRECIS_SCHEMA_GATE=refuse` default), so an
upgrade can never run new code on an old schema. A DB *ahead* of the
package (rolling deploy) warns and still boots. Running under docker?
Skip the manual step — the image's entrypoint defaults
`PRECIS_AUTO_MIGRATE=1` on `precis …` commands, so a container restart
self-migrates. Alternatives to the manual `precis migrate` step:

- `PRECIS_AUTO_MIGRATE=1` — daemons apply pending migrations themselves at
  startup, serialized under a Postgres advisory lock. Also available as
  `--migrate` on `serve` / `web` / `worker`.
- `PRECIS_SCHEMA_GATE=warn` — log one loud warning and boot anyway. For
  a read-mostly client of a DB another host migrates (e.g. a session MCP
  server): it stays up through the window between a deploy publishing new
  code and `precis migrate` landing, and `precis-status` reports the
  pending count as `schema_state`.
- `PRECIS_SKIP_SCHEMA_CHECK=1` — bypass the check entirely with a loud
  warning; emergency escape hatch for read-only diagnosis, not for
  running services. (`PRECIS_SCHEMA_GATE=off` is the same skip spelled
  as a mode.)

### Prod-shaped dev data (repo dev, cluster access)

A freshly migrated DB is empty, so bugs that need real data shapes (a
pathway's Pourbaix map, a board's net classes, a gripe with long comments)
don't show on it. With read access to prod, seed a dev DB from an
anonymized slice:

```bash
scripts/dev seed-prod-shaped               # --per-kind N (default 20)
scripts/dev env PRECIS_DATABASE_URL=postgresql://postgres@precis-test-db:5432/precis_dev \
    uv run --no-sync precis eval "get(kind='pcb', id=482131)"
```

- **Reads prod only through `scripts/prod-psql --ro`** (the `agent_ro`
  role inside `BEGIN READ ONLY`): the catalog, a ref-id pick, a names list
  for pseudonymizing, then one statement for the rows.
- **The slice:** the newest live refs of every kind, plus named anchors
  (qu164903 with pw483780/pw483766, pcb taz15-blank, memory root me474312
  and its hubs) and their link neighbours, the longest gripes and the
  most-seen alerts. Every table that hangs off `refs` by foreign key comes
  along (chunks, tags, links, pcb_*, struct_*, se_*, design_*, ...), with
  the lookup rows they point at. Not copied: users, mail accounts,
  settings, nanopub signing records, logs, embeddings (the worker
  regenerates them) and bulk caches (`EXCLUDED` in
  `scripts/_seed_prod_shaped.py`).
- **Anonymized before it touches disk:** credential shapes
  (`precis.utils.secret_scan`), secret-named JSON keys, emails, private
  and tailnet addresses, home paths, Discord mentions, and the names of
  prod users outside the bibliographic record (a paper's authors stay).
  Only the anonymized slice is written, to `.scratch/dev-seed/slice.jsonl`
  (gitignored).
- **Loads into `precis_dev`** on this worktree's `precis-test-db`. The
  schema comes from `precis migrate`, never from the dump. The load
  truncates the slice tables, inserts the slice, drops rows whose foreign
  keys point outside it and moves sequences past the copied ids, all in
  one transaction. Re-running replaces the slice; dev writes to those
  tables go with it.

## 3. Pick an embedder

`PRECIS_EMBEDDER` ∈ `mock` | `bge-m3` | `remote`
(`src/precis/embedder.py::make_embedder`):

- `mock` — deterministic, no model download. Good for trying precis out;
  `search` still works, just not semantically. This is the config default.
- `bge-m3` — real in-process embedder (BAAI/bge-m3, downloads ~2 GB on first
  use). Needs the `[embed]` extra. **`precis worker`'s own `--embedder` flag
  defaults to `bge-m3`** even though the config default is `mock` — set
  `PRECIS_EMBEDDER` explicitly if you want them to agree.
- `remote` — HTTP client to a `precis serve-embeddings` daemon; needs
  `PRECIS_EMBEDDER_URL`.

```bash
export PRECIS_EMBEDDER=mock   # or bge-m3 / remote
```

## 4. Run the worker

Embeddings, summaries, and fetches all happen here — never in ingest.

```bash
precis worker   # default --profile system
```

## 5. Run the MCP server

```bash
precis serve   # stdio
```

Wire it into your agent's MCP config — see
[`README.md` §Run](../README.md#run) for the JSON block.

## 6. Web UI (optional)

Needs the `[web]` extra. Create an account **first** — every page 503s
without one:

```bash
precis users add <login> --abbrev <ab>
precis web   # default 127.0.0.1:9100
```

The password pepper (`PRECIS_WEB_PASSWORD_PEPPER`) is auto-minted into the
DB secrets vault on first account creation (`src/precis/users.py::ensure_pepper`)
— nothing to set, and never rotate it (that invalidates every peppered hash).

## 7. Secrets

Application API keys (Perplexity, EPO OPS, ORCID, …) go in the DB secrets
vault, not env vars:

```bash
precis secret set NAME --prompt
```

or the web `/secrets` page, which shows a red/amber/green status dot per
known key, a free/paid cost badge, and a how-to-get-one link. Full env-var catalog (deploy-time and
feature-toggle vars, not app secrets):
[`docs/reference/config-variables.md`](reference/config-variables.md).

## Cluster instead?

Multi-host, ansible-provisioned: [`deploy/README.md`](../deploy/README.md).


## Read an authorized task checkout through Python navigation

Register roots at construction of a separately authorized local MCP reader,
using the existing configuration (directories must already exist):

```sh
PRECIS_PYTHON_ROOTS=precis:/absolute/task/root
```

An embedded reader uses the same boundary:
`PythonHandler(hub=hub, roots={"precis": Path("/absolute/task/root")})`.
Neither a navigation id nor `expected_root` grants access or registers a root.
Keep production registrations unchanged; do not bind production to mutable
session source. This setup recipe does not authorize starting a new service.

List available aliases without indexing them, then assert the expected root
before any source lookup:

```python
get(kind="python")
get(kind="python", id="precis", args={"expected_root": "/absolute/task/root"})
search(kind="python", q="cache", scope="precis", args={"expected_root": "/absolute/task/root"})
```

The assertion requires an explicit alias id/scope; unscoped assertions and
mismatches fail before indexing. Read the resolved root and indexed Python
corpus fingerprint, independently of observed Git HEAD/branch/dirty state.
Reuse is stat-checked with content-not-revalidated explicitly labelled;
partial walks, unreadable files and parse errors limit the represented corpus.
