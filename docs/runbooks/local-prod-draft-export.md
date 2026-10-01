# Build a real prod-data export from unshipped code

**When.** You need a **real** docx/PDF/EndNote artifact built from prod data
with *unshipped worktree code* — fast feedback on a brittle format before it
ships. This beats "ship + deploy, then download from the melchior web UI" when
the output needs a human's eyes first.

**Recipe.** Connect a `Store` straight to prod from the host and run the
exporter.

1. DSN: `~/.secrets/pw/PRECIS_DATABASE_URL` (`precis_prod` via pgbouncer, user
   `agent_rw`). In the file it uses `host.docker.internal:6432`; **rewrite to
   `127.0.0.1:6432`** on the host (a local pgbouncer forwards to the cluster).
   `agent_rw` is write-capable — keep to read-only exports.
2. Open a pool and store (`precis.store.pool.create_pool`):

       pool = create_pool(dsn, min_size=1, max_size=2, open_timeout=15)
       pool.open(wait=True)
       store = Store(pool)
       export_docx(store, store.get_ref(kind='draft', id=<slug>),
                   target_path=out, citations=...)

3. Run under `uv run --extra docx --with python-docx`. Export is pure read
   (`reading_order`, `get_ref`, `identifiers_for_refs`, `draft_terms`) — no
   embedder needed.
4. Hand the file to the user.

Code: `src/precis/export/docx.py`, `src/precis/export/endnote.py`. To verify
what the cluster actually runs, see
[`cluster-deploy`](./cluster-deploy.md) (venv `direct_url.json`).
