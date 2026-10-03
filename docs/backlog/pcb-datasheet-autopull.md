---
status: in-progress
title: a part a board uses gets its datasheet pulled, ingested and linked without anyone asking
prio: high
pillar: 3d-design
---

# auto-pull datasheets for the parts a board uses

Reto, 2026-10-02 (review queue, `reto-datasheet-autopull-1`): **"For
datasheets, we should auto-pull the sheets when the PCB uses it."** Follow-up,
same item: **"And start... adding them to memory mesh."**

This is the unbuilt lazy-ingest path the `datasheet` kind was designed around
(`handlers/datasheet.py` module docstring: "a datasheet is acquired by *lazy
ingest* from a part's `datasheet_url` (Slice 3)"; spec in
`pcb-0042-implementation.md` Slice 3).

## Prod state this has to start from (measured 2026-10-02)

- `parts` has **0 rows**, so no `datasheet_url` exists anywhere. A trigger
  that reads `parts.datasheet_url` fires on nothing. (gr458878 records the
  same empty catalog.)
- 0 live `datasheet` refs.
- A board names its parts by C-number (`part_lcsc` / `part` on the
  component, `store/_pcb_ops.py`), not by a `parts` row.

So the pull has to resolve the URL itself: C-number → `JlcApi.component_info`
(`pcb/jlc_api.py`, `dataManualUrl`), upserting the `parts` row it gets back,
then the PDF. Check first whether prod has JLC API credentials
(`jlc_api.credentials_available`); without them the community-dump fallback
(`pcb/catalog.py`, its `datasheet` column) is the only URL source.

## Shape

1. **Trigger:** a component with a C-number lands on a board
   (`pcb_apply` insert, generator expansion, EasyEDA import). Enqueue one
   job per C-number not already linked to a datasheet. Never inline in the
   put/place call.
2. **Worker job** (`datasheet_pull`, own lane): resolve URL → fetch with
   `safe_get`/`safe_stream` (supplier URLs are external input; raw httpx is
   an SSRF) → dedupe by content sha (one datasheet per part family, many
   parts) → ingest as a `datasheet` ref with `meta.source_url` (the fetch URL)
   and `meta.part_lcsc` (the C-number) — the keys claims-and-evidence's
   `precis.nanopub.evidence.datasheet_url` reads — carrying exactly one
   `pdf_sha256` identifier, which the nanopub cites as `urn:sha256` →
   link `datasheet-of` to the part. Idempotency key: C-number + URL.
3. **Failure is a recorded state, not silence:** no URL, fetch refused,
   not a PDF — each lands as a named reason on the part, readable from
   `get(kind='part')`, so "no datasheet" is distinguishable from "never tried".
4. **Read side:** `get(kind='pcb', view='bom')` (or `parts`) shows each
   line's datasheet ref or its failure reason.

## Built (undeployed) and not yet built

Built: the `datasheet_pull` job (`workers/job_types/datasheet_pull.py`,
`job_inproc`: JLC `component_info` URL with `parts` upsert and a stored-URL
fallback, `safe_stream` fetch capped at 50 MB, `%PDF` check, sha dedupe,
ingest through `precis_add(PdfInput(as_kind='datasheet'))`, `datasheet-of`
link, outcome in the part ref's `meta.datasheet_pull`); the trigger
(`pcb/datasheets.py`: `put(kind='pcb')` and the `import-epro` CLI queue one
job per unlinked, unattempted C-number, key `datasheet_pull:<LCSC>`);
`put(args={'op':'datasheets','force':True})` re-queues failed pulls;
`get(kind='part')` shows the datasheet or the reason; `get(view='bom')` lists
each C-number's state beside the CSV (not a column: the JLCPCB BOM upload has
fixed headers). Failure reasons: `no_url`, `jlc_api_error:<status|class>`,
`fetch_refused`, `http_<status>`, `not_pdf`, `too_large`,
`fetch_error:<class>`, `fetch_timeout` (300 s download deadline),
`ingest_empty` (0 body chunks; ref and link kept), `ingest_failed:<class>`.
`jlc_api_error:*`, `http_5xx`/`http_429`, `fetch_error:*` and `fetch_timeout`
are transient: a later put re-queues them once the record is 24 h old
(`pcb/datasheets.py::is_transient`); the rest need `op='datasheets', force`.
`force` on an `ingest_empty` part re-runs the pull but the sha dedupe finds the
empty datasheet again, so it does not re-run Marker; delete the empty
datasheet first.

Not built: the knowledge-mesh half (below, theirs); the community-dump URL
fallback beyond an existing `parts.datasheet_url` (a populated catalog
supplies it); a worker host without the Marker extra falls back to fitz page
extraction, so confirm the first prod pull's chunk quality. A failed pull
mints the part ref even for a C-number outside the catalog
(`ensure_part_ref(uncatalogued_ok=True)`) so the reason has a place to live.

## Consumers (why it is ranked where it is)

- **claims-and-evidence:** a nanopub citing a datasheet needs URL + sha on
  the ref (their item 2); step 2 stores both.
- **knowledge-mesh** (owner of the mesh half): the follow-up wants the
  datasheet's content (key specs, ratings, pinouts) as graph nodes linked to
  the part and the datasheet ref. That consumer reads the refs this item
  produces; building it is theirs. **Shape confirmed by Reto 2026-10-02
  (review item `ewod-pcb-2`, option 1):** ratings, key specs, package and
  the pin table as nodes, each citing its datasheet page and linked to the
  part, and the pin table checked against the footprint's pad map.
- **gr458878** (pin-level datasheet provenance): needs a datasheet to exist
  before a pin fact can cite its page.

## Acceptance

- Putting a board with a C-numbered part enqueues exactly one pull per
  unlinked C-number; a re-put enqueues nothing.
- Two C-numbers whose URLs return the same bytes share one datasheet ref.
- The fetch path is `safe_fetch` (a test asserts a private-address URL is
  refused).
- A part whose pull failed shows the reason on `get(kind='part')`.
- Negative control: a component with no C-number enqueues nothing.

Touches `safe_fetch.py` only as a caller; if it needs a change there, it goes
to the orchestrator to gate (no `/qgo`).
