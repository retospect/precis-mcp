---
status: draft
title: Paper backup/restore and PDF transfer over MCP (gr517423)
pillar: memory-graph
prio: normal
model: sonnet
---

# Paper backup/restore and PDF transfer over MCP (gr517423)

Source: gr517423. Reto approved the six-part scope 2026-10-25. Thread:
[ingest-and-fetch](threads/ingest-and-fetch.md). Six slices, each
landable on its own. Slices 1–2 are the undo pair. Slices 3–6 move PDF
bytes between the corpus and an agent without the bytes ever passing
through an MCP result.

## Motivation / why

Before running drop_body/clear_abstract on 25 refs (gr513878), the
executor needed an undo. No paper view dumps a ref's full state, so it
wrote hand-built SQL through `scripts/prod-psql --ro` into
`~/precis-exports/gr513878/pa<N>.json` (keys `ref`, `chunks`,
`identifiers`, `links_in`, `links_out`). Nothing could load those files
back. Three more gaps around PDFs:

- An agent cannot get a stored PDF out of the corpus.
- An agent cannot attach a PDF it holds locally. `put(args={'pdf_url'})`
  only works for a public URL.
- An agent cannot re-extract a ref from its own stored PDF, or move a
  stored PDF from a bodiless ref to the right one. `move_body_to` needs
  a live body (`handlers/paper.py::_require_live_body_ref`).

What exists today, with anchors:

- **se paging precedent.** `precis_se/ops_export.py::render_ops` returns
  markdown containing one fenced JSON block. Windows come from
  `handlers/_slice_args.py::slice_window` (`page`/`page_size`, plus
  `offset`/`limit` aliases). Each slice ends with a `next slice: get(…)`
  line. `NOT_CARRIED` names every gap, and
  `tests/test_se_ops_export.py::test_every_stored_field_has_an_export_verdict`
  pins it.
- **Edit doors.** `handlers/paper.py`: `_EDIT_DOOR_ARGS`, `ARGS['edit']`,
  `_require_reason`, `_body_op_tx`, `store.append_event`
  (`store/_events_ops.py`). Their dry runs return prose with no
  `next=`.
- **Body primitives.** `ingest/body_ops.py`: `probe_body`,
  `detach_body`, `move_body`, `ensure_paper_cards`.
- **Identity gate.** `ingest/pdf_identity.py`: `read_leading_pages` +
  `identity_verdict` (pure, DOI or ≥0.8 title-word overlap over 3 pages).
  `workers/job_types/paper_pull.py::check_pdf` wraps it and adds the
  preview and unreadable checks.
- **Extraction path.** `ingest/paper_hygiene.py::heal_bodiless_pdfs`
  (`_default_body_extractor`, then `db_writer.register_aliases_and_maybe_upgrade`).
  `_bodiless_candidates` skips a ref that already carries a verdict
  event for its current sha, so a ref that was extracted once is never
  picked up again.
- **Storage.** `pdfs.storage_path` and `refs.pdf_sha256`. The layout is
  `corpus_layout.py::corpus_pdf_dest` (`<root>/<letter>/<cite_key>.pdf`),
  resolved by `corpus_layout.py::resolve_local_pdf`, and on the web side
  by `precis_web/corpus.py::resolve_pdf_for_ref`.
- **Which process can do what.** precis_web is a launchd daemon that
  reads and writes the corpus (`routes/papers.py::replace_pdf`). The MCP
  container mounts the corpus read-only and has no `PRECIS_CORPUS_DIR`.
  So bytes go through precis_web, and PDF reads in MCP flows go through
  worker jobs.
- **Web auth.** `precis_web/auth.py`: HTTP Basic plus a process-local
  HMAC session cookie. `_SELF_AUTH_PREFIXES` is the documented way a
  route authenticates itself (precedent: `/podcast` `?t=`).
- **Shared-secret precedent.** `users.py::ensure_pepper`: mint 32 bytes,
  store them with `secrets.set_secret`, read them with `get_secret`.

## In scope

- Slice 1: `view='ops'` paper dump, plus dry-run pointers from the
  destructive doors to it.
- Slice 2: `put(args={'restore': …})`, which rebuilds a paper from that
  dump.
- Slice 3: a short-lived signed PDF download link served by precis_web.
- Slice 4: `edit(args={'reextract': True})`, which drops the body,
  keeps the PDF and re-extracts.
- Slice 5: `move_pdf_to` / `attach_pdf`, which re-home a stored PDF,
  gated on the target's identity.
- Slice 6: a signed upload link, then `put(args={'pdf_upload': sha})`.
- A CLI twin for slices 1–2 (`precis paper dump|restore`), sharing the
  same library, for operator batch runs.

## Explicitly NOT in scope

- Server-side snapshots: an automatic dump stored in the DB before
  every destructive door. That needs a table and storage budget. See
  Q1.
- Restoring derived rows: embeddings, `chunk_summaries`, keywords and
  `chunk_citations`. Workers refill them, as after ingest.
- PDF bytes, or base64 of them, inside an MCP request or response.
- Replacing `routes/papers.py::replace_pdf`, the human UI bypass. It
  stays as it is.
- Dumping kinds other than paper. Patents could reuse the library
  later.
- Building `drop_identifier`. That belongs to the keep_pdf work
  (gr513878 goal). This spec only consumes `keep_pdf`.
- Changing the corpus layout or `pdf_locations`.

## Slice 1 — `view='ops'` export (read-only)

**Shape.** `get(kind='paper', id=<pa>, view='ops', args={'page': N,
'page_size': K})` returns markdown with a header, a "Not carried:" list
and one fenced JSON block:

```json
{"format": "precis-paper-dump/1",
 "manifest": {"ref_id": 15, "handle": "…", "chunks_total": 5012,
   "body_sha": "<sha256 over (ord, chunk_kind, text) in ord order>",
   "window": [0, 200], "exported_at": "…Z"},
 "ref": {…every refs column except generated ones…},
 "identifiers": [...], "tags": [...], "paper_authors": [...],
 "bib_entries": [...],
 "links_out": [...], "links_in": [...],
 "chunks": [{"ord": -6, "chunk_kind": "card_abstract", "text": "…", …}]}
```

- The window runs over every chunk of the ref, card chunks (`ord < 0`)
  and body chunks alike, ordered by `ord`. The default `page_size` is
  100, the cap 500, and `0` means everything that remains.
- Only the slice whose window starts at 0 carries `ref`, `identifiers`,
  `tags`, `paper_authors`, `bib_entries` and the links. Later slices
  carry `manifest` and `chunks` only.
- A link row adds `src_chunk_ord` / `dst_chunk_ord` and the far ref's
  handle next to the raw chunk ids. Chunk ids do not survive a restore;
  `(ref, ord)` does.
- Excluded chunk columns: `tsv`, `keywords`, `keywords_meta` and the
  derived tables. `NOT_CARRIED` lists them.
- When more remains, the slice ends with
  `next slice: get(kind='paper', id='<h>', view='ops', args={'page': N+1, 'page_size': K})`,
  copied from `ops_export.py`. Set `pagination_alt_hint` the same way.

**Dry-run pointers.** The dry runs of `drop_body`, `clear_abstract`,
`split_body`, `move_body_to` (and later `reextract`/`move_pdf_to`) gain
the line `undo: save get(kind='paper', id='<h>', view='ops') first
(N slice(s) at page_size 100)`. `Response` has no `next` field, so this
goes in the body.

**Code.**
- New: `src/precis/ingest/paper_dump.py::dump_ref(conn, ref_id, *,
  offset, size) -> dict` and `NOT_CARRIED`.
- Edit: `handlers/paper.py` (add `"ops"` to `_SUPPORTED_VIEWS`, extend
  the view's args spec, add the render).
- CLI: `precis paper dump <ref> --out <file>`, which writes every slice
  into one JSON with the same `format`.

**Tests.** `tests/test_paper_ops_export.py`, on real Postgres. Fixture
text is generated at runtime, never paper prose
(`test_paper_text_gate`). Cases:
- slices concatenate to the full chunk list;
- `body_sha` matches;
- a slice names its window and the next call;
- card chunks included;
- a chunk-anchored link carries ords;
- every column of every dumped table has a verdict;
- `page_size` cap;
- an offset past the end gives an honest empty slice;
- each of the four door dry runs carries the `undo:` line.

**Store/migration:** none. Read-only.

## Slice 2 — restore door (Opus review: store writes)

**Shape.** `put(kind='paper', id=<pa or omitted>, args={'restore':
<one dump slice>, 'target': 'same'|'new', 'reason': '…'},
dry_run=True|False)`. The agent sends slices in order. The CLI twin is
`precis paper restore <file> [--apply] [--target new]`.

**Choosing the target** (`target='same'` is the default):
- The dump's `ref_id` is live with no body (`ord >= 0` count is 0):
  restore into it.
- It is retired: restore into it and un-retire. The ack names the
  un-retire.
- It has a body: refuse, with `next=` pointing at
  `edit(kind='paper', id=…, args={'drop_body': True, 'reason': …})`,
  preceded by an export.
- It is gone (merged or hard-deleted), or `target='new'`: mint a new
  ref from `ref` (title, authors, year, meta) under the dump's handle.
  If the handle is taken, refuse with `next=` repeating the same call
  plus `args={'handle': '<free-slug>'}`.

**Identity.** Check before the first write. If any dumped identifier
(doi, arxiv, pdf_sha256, …) belongs to a different live ref, refuse
with `next=` set to `get(kind='paper', id='<owner>')` and options
`merge_into`/`target`. Identifiers already on the target are skipped.
`pdf_sha256`/`pdf_pages` are restored only when the `pdfs` row exists.

**Writes.** One transaction per slice, in `_body_op_tx`, under
`@trust_door`:
- Chunks are INSERTed and never UPDATEd.
- An `ord >= 0` row that already exists with the same text is skipped
  (re-sending a slice is idempotent). The same ord with different text
  refuses.
- `ord < 0` cards may be DELETEd and re-INSERTed (the card exception in
  `invariants.md`). The ack counts the cards it replaced.
- Embeddings, keywords and summaries are never written. New rows carry
  `embedding IS NULL`, and the worker refills them.

**Progress.** The first slice stamps `meta.restore = {dump_body_sha,
chunks_total, chunks_done, started_at}`. Each slice records ref_event
`paper_restore_slice`. The slice that completes the count:
- checks `body_sha` over the stored rows;
- writes links, remapping chunk anchors through `(ref, ord)` and
  skipping a link whose far end is retired or missing (the ack lists
  them);
- clears `meta.restore`;
- records `paper_restored` with `{dump_body_sha, chunks, cards,
  identifiers, links, skipped_links, reason}`.

A slice arriving out of order refuses, with `next=` naming the window
it expects.

**Dry run.** Runs every check and returns the counts and target
decision for that slice. It writes nothing.

**Code.**
- `paper_dump.py::restore_slice(conn, dump, *, target, handle, dry) ->
  RestoreReport`.
- `handlers/paper.py`: add `restore`/`target`/`handle` to `ARGS['put']`.

**Tests.** `tests/test_paper_restore.py`, real Postgres. Cases:
- dump, then drop_body, then restore round-trips to an identical
  `body_sha`, identical identifiers and identical links (chunk anchors
  remapped);
- clear_abstract undo brings back `card_abstract`;
- restore into a retired ref un-retires it;
- `target='new'` with a taken handle refuses;
- a foreign-owned DOI refuses with `next=`;
- a body present refuses;
- the same slice twice is idempotent;
- an out-of-order slice refuses;
- the chunk_embeddings count is 0 after restore;
- dry run writes nothing (row counts unchanged);
- a restore near the `chunks_forbid_body_text_update` trigger never
  trips it.

**Store/migration:** store writes across refs, chunks, ref_identifiers
and links. No migration.

## Slice 3 — signed PDF download link

**Shape.** `get(kind='paper', id=<pa>, view='pdf_link',
args={'ttl_s': 600})` returns a URL, its expiry (UTC `Z`), the sha, the
byte size and page count from `pdfs`, and the curl line
`curl -fo <cite_key>.pdf '<url>'`. It never returns bytes. A ref with
no `pdf_sha256` refuses, with `next=` pointing at
`put(kind='paper', id=…, args={'pdf_url': …})`.

**Signing.** New module `src/precis/file_links.py`, imported by both
the MCP and precis_web:
- `mint(op, sha, ref_id, ttl_s) -> token` and
  `verify(token, *, op, sha) -> Claims`.
- The token is `v1.<op>.<sha>.<ref_id>.<exp>.<hexsig>`, where the
  signature is HMAC-SHA256 over every field before it.
- `op` ∈ {`get`, `put`}. `ttl_s` defaults to 600 and is capped at 3600.
- Verification uses `hmac.compare_digest` and recomputes over the
  parsed fields, as in `auth.py::SessionTokens`.
- The key is the vault secret `PRECIS_FILE_LINK_KEY`, read through
  `secrets.get_secret` and minted on first use by
  `ensure_file_link_key`, a copy of `users.py::ensure_pepper`. It is
  persistent and shared, because the MCP signs and the web verifies. A
  process-local key, as `SessionTokens` uses, cannot work here.

**Base URL.** `get_secret("PRECIS_WEB_BASE_URL")`, which reads env,
then file, then vault, and is set by the operator from the overlay. No
hostname goes into the repo. When it is unset, the view returns the
path-only link, says so in its first line, and still mints the token.

**Route.** `precis_web/routes/files.py`:
`GET /files/pdf/{sha}?t=<token>`.
- Register `/files` in `_SELF_AUTH_PREFIXES`. The route rejects
  anything without a valid token, with no Basic fallback (as in
  podcast: a bad token is a 401).
- The path sha must equal the token sha, and the token's `ref_id` must
  still own that sha.
- The file is resolved via `pdfs.storage_path`, then
  `corpus_layout.resolve_local_pdf`.
- Response: `FileResponse` with
  `Content-Disposition: attachment; filename="<cite_key>.pdf"` and
  `Cache-Control: private, no-store`.
- Errors: 404 when the file is missing, 410 when the token has
  expired.

**Tests.**
- `tests/test_file_links.py`: round trip; expired; tampered field; a
  different sha; a different op; key rotation invalidates old tokens.
- `tests/test_web_file_routes.py`: a valid token streams the file; no
  token gives 401; a token for sha A refuses path sha B; `/filesx` is
  not exempt; ownership has moved since minting gives 404.
- The view's refusal on a PDF-less ref.

**Store/migration:** none. This is auth surface: request a security
review (`/security-review`) before landing.

## Slice 4 — re-extract from the stored PDF (Opus review: body write)

**Blocked by** the keep_pdf door (gr513878 goal, branch
`worktree-keep-pdf`, no commits as of 2026-10-25). That door is
`edit(args={'drop_body': True, 'keep_pdf': True, 'pdf_sha256'?:
…})`: it deletes chunks, embeddings and chunk-anchored links, and keeps
`refs.pdf_sha256`/`pdf_pages` and the matching `pdf_sha256`
identifier. If it has not landed when this slice starts, build it here
in its own commit to that goal. Do not write a second drop path.

**Shape.** `edit(kind='paper', id=<pa>, args={'reextract': True,
'reason': …, 'pdf_sha256'?: …})`. Add `reextract` to `_EDIT_DOOR_ARGS`.
1. Run the keep_pdf drop in the call's transaction.
2. Enqueue job `paper_reextract` (`executor='job_inproc'`,
   `idem_key=f'reextract:{ref_id}:{sha}'`). Marker is slow, so the
   extraction does not run inline.
3. The ack names the job, plus
   `get(kind='job', id=…)` to watch it.

**Job.** `workers/job_types/paper_reextract.py` calls a new
`paper_hygiene.py::heal_bodiless_ref(store, ref_id, *, force=True)`.
That function is the per-ref body of the `heal_bodiless_pdfs` loop
(probe, then `_default_body_extractor`, then
`register_aliases_and_maybe_upgrade`, then `_journal_bodiless`).
`force` bypasses the per-sha verdict-event exclusion in
`_bodiless_candidates`. Refactor so both callers share it; do not
copy it. Outcomes: missing_file, unreadable or preview go into
`meta.reextract` and a ref_event `reextract_failed`, and the ref stays
bodiless.

**Refusals and dry run.**
- No PDF on the ref: refuse, with `next=` pointing at `pdf_url` or the
  slice 6 upload.
- Retired: refuse, with `next=` set to `retire: False`.
- The dry run shows the drop counts, the sha it keeps, the job it would
  queue and the slice 1 `undo:` line.

**Tests.** `tests/test_paper_reextract.py`, with the extractor stubbed
through the `extractor=` seam:
- body replaced, with new chunk_ids and embeddings NULL;
- a previously extracted sha is re-run with `force`, and
  `_bodiless_candidates` still skips it without `force`;
- a missing file leaves the ref bodiless plus an event;
- dry run writes nothing;
- `idem_key` dedups a double call.

**Store/migration:** body write via the keep_pdf drop and db_writer. No
migration.

## Slice 5 — move or attach a stored PDF (Opus review: store writes)

**Shapes** (the two door args are mutually exclusive):
- `edit(kind='paper', id=<donor>, args={'move_pdf_to': '<target>',
  'reason': …, 'pdf_sha256'?: …})` re-homes the sha from a bodiless
  donor. A donor that still has a body refuses, with `next=` set to
  `move_body_to`, because body and PDF move together.
- `edit(kind='paper', id=<target>, args={'attach_pdf': '<sha>',
  'reason': …})` adopts a `pdfs` row that no live ref owns. drop_body
  leaves exactly such orphans; `ref_identifiers` (PK id_kind+id_value)
  allows one owner per sha.
- The target must be live and bodiless, with no `pdf_sha256`.
  Otherwise refuse, with `next=` set to `reextract` or `drop_body`.

**Gate.** Before any write, compute `identity_verdict(texts,
title=target.title, doi=target.doi)`:
- When the donor has stored chunks on pages 1–3, the texts come from
  those chunks.
- Otherwise the edit enqueues job `pdf_attach`. The job reads the
  file with `read_leading_pages` (same `check_pdf` reasons) and
  applies the move only on `doi`/`title`.
- `mismatch` refuses with the detail line. `unchecked`, meaning the
  target has neither title words nor a DOI, refuses with `next=`
  `edit(id=<target>, title=…|doi=…)`.

**Write.** One transaction:
- clear the donor's `pdf_sha256`/`pdf_pages`/`pdf_role`;
- move the `pdf_sha256` identifier row;
- set the target's columns;
- record ref_event `pdf_moved` on both refs (`{sha, from, to,
  verdict, reason}`);
- enqueue the slice 4 `paper_reextract` for the target.

The dry run shows the verdict (or "deferred to job"), the rows it
would change and the `undo:` line for the donor.

**Tests.** `tests/test_paper_move_pdf.py`:
- happy move with DOI match;
- title mismatch refuses, with no rows changed;
- a donor with a body refuses toward `move_body_to`;
- `attach_pdf` of an owned sha refuses, naming the owner;
- an orphan sha attaches;
- the job path with a stubbed `read_leading_pages`;
- events on both refs;
- dry run writes nothing.

**Store/migration:** store writes on refs and ref_identifiers. No
migration.

## Slice 6 — signed upload, then attach (highest risk: new ingress)

**Mint.** `put(kind='paper', id=<pa>, args={'pdf_upload_link': True})`
returns a `put`-op token URL for `POST /files/upload/{ref_id}?t=…`,
plus `curl -fF file=@paper.pdf '<url>'`. The target must pass
`paper_pull.target_problem`, the same precondition as `pdf_url`.

**Route.** The route (in `routes/files.py`) streams the multipart body
to `<PRECIS_WATCH_INBOX>/.uploads/<nonce>.part`:
- hashes while streaming;
- rejects at 50 MB (413, the `paper_pull` cap) and without `%PDF`
  magic (415);
- renames to `<sha>.pdf`;
- returns JSON `{sha256, bytes, next: "put(kind='paper', id='<h>',
  args={'pdf_upload': '<sha>', 'reason': '…'})"}`.

Each token is good for one upload: the nonce is the token's exp+sig,
and a second POST with it is refused (409).

**Attach.** `put(kind='paper', id=<pa>, args={'pdf_upload': '<sha>',
'reason': …})` enqueues `paper_pull` with `params={ref_id,
upload_sha}`. Generalise `paper_pull.pull` so the source is either a
download or the staged upload. The same `check_pdf` gate, sidecar and
inbox drop then apply, so dedup, `Claim` and the watcher fold are
reused unchanged. Record ref_event `pdf_uploaded` with the verdict.
`paper_reconcile` sweeps `.uploads/` entries older than 24 h.

**Deploy.** The `precis_web` role adds `PRECIS_WATCH_INBOX`, set from
the overlay's `papers_inbox_path`, to the plist template and defaults.
No literal path or host.

**Tests.** `tests/test_paper_pdf_upload.py`:
- route: valid upload, oversize, non-PDF, replayed nonce, expired
  token, wrong ref_id;
- handler: an unknown sha refuses with `next=` set to the mint call;
  a bodied target refuses;
- `pull` from upload runs `check_pdf` (identity mismatch leaves nothing
  in the inbox);
- the sweep removes stale parts.

**Store/migration:** none. It touches deploy and auth, so it needs
`/security-review`.

## Acceptance criteria

- `get(kind='paper', id=<5000-chunk ref>, view='ops')` pages to
  completion. The concatenated `body_sha` equals one computed in SQL.
- On the dev DB (`scripts/dev`): dump a ref, `drop_body` it, restore
  it. Chunks (ord, kind, text), identifiers and links compare equal
  except for chunk_id. `chunk_embeddings` rows for the ref reach 0,
  then get refilled by the worker.
- Each destructive paper door's dry run carries the `undo:` export
  line.
- A `pdf_link` URL fetched with curl returns bytes whose sha256 equals
  `refs.pdf_sha256`. After `ttl_s` it returns 410. Changing any token
  field gives 401.
- `reextract` on a ref already extracted from its sha yields a fresh
  body via the job, and leaves `ref_events` `body_dropped` and
  heal-verdict rows.
- `move_pdf_to` with a mismatched title refuses with nothing written.
  A matching move re-homes the sha and queues re-extraction.
- A local PDF uploaded via the signed link and attached with
  `pdf_upload` ends with the target's `pdf_sha256` set and a body
  extracted. A wrong-paper upload ends `identity_mismatch` with nothing
  in the inbox.
- Every write door refuses without `reason`, writes nothing under
  `dry_run`, and records a ref_event. `tests/test_mcp_raise_next_ratchet.py`
  stays green with no new hint-less raises.
- `precis-paper-ref-help` gains rows for `view='ops'`, `restore`,
  `pdf_link`, `reextract`, `move_pdf_to`/`attach_pdf` and the upload.
  `precis-paper-help` lists the `ops` and `pdf_link` views.

## Target + blast radius

- **Handler.** `handlers/paper.py`: views `ops`/`pdf_link`; put args
  `restore`/`target`/`handle`/`pdf_upload_link`/`pdf_upload`; edit
  doors `reextract`/`move_pdf_to`/`attach_pdf`.
- **Library.** New `ingest/paper_dump.py` and `file_links.py`. Edits to
  `ingest/paper_hygiene.py` (the `heal_bodiless_ref` refactor) and
  `workers/job_types/paper_pull.py` (upload source).
- **Jobs.** New `workers/job_types/paper_reextract.py` and `pdf_attach`
  in the job registry.
- **Web.** New `precis_web/routes/files.py`, plus `/files` in
  `auth.py::_SELF_AUTH_PREFIXES`.
- **Deploy.** `deploy/roles/precis_web` (the inbox env).
- **Vault.** New secrets `PRECIS_FILE_LINK_KEY` (self-minted) and
  `PRECIS_WEB_BASE_URL` (operator-set).
- **Skills.** `precis-paper-ref-help` and `precis-paper-help`.
- **Post-deploy look.** `tool_calls` errors for `get|paper` and
  `edit|paper`; web 401/410 counts on `/files`; `paper_reextract` job
  failures.

## Open questions / decisions log

- **Q1. Server-side snapshot as well as the client dump?** Recommend
  no for now: slice 1 plus the CLI covers the gr513878 case with no
  storage cost. Revisit if an agent loses a dump.
- **Q2. Slice order.** Ordered by risk: read-only first, new ingress
  last. Restore sits at 2 because it is the undo for 1. Recommend
  keeping it.
- **Q3. `PRECIS_WEB_BASE_URL` in the vault vs container env.**
  Recommend the vault (operator-set, via `get_secret`). Container env
  would need `scripts/lib/cluster-topology.sh`, a harness file on the
  `--remote` lane.
- **Q4. Token TTL.** Recommend a default of 600 s and a cap of 3600 s.
  A download link may be reused within its TTL; an upload link is
  single-use.
