# Single-machine setup

Fresh machine to a working precis: MCP server + worker + web UI. Cluster
instead? → [`deploy/README.md`](../deploy/README.md).

## 1. Install

```bash
pip install 'precis-mcp[all]'
```

Python >=3.12. `all` = embed + paper + external + patent + edgar + web. See
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
