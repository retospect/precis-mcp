# precis-mcp / scripts

Operator utilities that wrap the precis-mcp package without going
through the `precis` CLI. Each command is a thin Bash wrapper that
re-execs under `uv run --project=<precis-mcp>` so the package's venv
(with `acatome-extract` + `sentence-transformers`) is active regardless
of the caller's CWD or shell.

## Environment

Every wrapper sets sensible defaults if not already exported:

- `PRECIS_DATABASE_URL` — defaults to
  `postgresql://acatome:acatome@127.0.0.1:5432/precis`
  (the canonical local v2 database).
- `PRECIS_EMBEDDER` — defaults to `bge-m3` (loads `BAAI/bge-m3` via
  `sentence-transformers`).

Override either by exporting the variable before invoking the script.

## Commands

### `paper-count`

Print counts of paper refs and (optionally) breakdowns.

```sh
./scripts/paper-count                      # papers + total blocks
./scripts/paper-count --by-kind            # all kinds, sorted by count
./scripts/paper-count --by-provider        # paper rows per provider
./scripts/paper-count --recent             # 10 most recent papers
./scripts/paper-count --recent 50          # custom N
```

### `paper-monitor-ingest-dir`

Watch a directory for new top-level `*.pdf` files. For each one:

1. Run `acatome-extract` → produces a `.acatome` bundle.
2. Insert the bundle via `Store.ingest_bundle(...)` (idempotent on
   DOI / pdf_hash / arxiv_id).
3. On success: move the PDF + bundle into `<dir>/completed/`.
4. On failure: move the PDF into `<dir>/errors/` alongside a
   `<stem>.error.log` traceback.

```sh
# default watch dir = /Users/bots/Documents/openclaw-cluster/paper-ingest
./scripts/paper-monitor-ingest-dir
./scripts/paper-monitor-ingest-dir --once          # one sweep, no loop
./scripts/paper-monitor-ingest-dir --interval 30   # poll every 30 s
./scripts/paper-monitor-ingest-dir --tag review-queue --tag urgent
./scripts/paper-monitor-ingest-dir --no-verify     # skip metadata cross-check
./scripts/paper-monitor-ingest-dir --dir /some/other/inbox
```

`Ctrl+C` (or `SIGTERM`) drains the current PDF and exits cleanly.

## Layout

```
scripts/
  _common.py                       # shared store/embedder helpers
  _paper_count.py                  # python impl
  _paper_monitor_ingest_dir.py     # python impl
  paper-count                      # bash wrapper (uv run)
  paper-monitor-ingest-dir         # bash wrapper (uv run)
  README.md
```

The leading-underscore Python files are private impls — invoke the
wrappers, not the `.py` files directly.
