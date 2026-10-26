---
status: draft
title: pcb .epro2 round trip through the MCP — what is left after the fetch/import rulings
pillar: 3d-design
prio: normal
---

# pcb .epro2 round trip through the MCP

Source: gr495289 (dogfood on prod, 2026-10-17, `usbc-attiny-dogfood-b`).
Thread: [pcb-easyeda-round-trip](threads/pcb-easyeda-round-trip.md).
The full options analysis (inline base64, a web download route, an
artifact store; base64/reference/URL/upload imports) is this file's
earlier revision in git history.

## Rulings (Reto, 2026-10-17, first-hand, relayed by the coordinator)

1. **Fetch = option 1a.** A web UI route, `GET /pcb/<slug>/export.epro2`,
   that rebuilds the file on request. No inline base64 in MCP responses
   and no artifact store.
2. **MCP import = option 2a.** `put(kind='pcb', id='<new slug>',
   args={'op':'import_epro', 'source':'export:<slug>'})`: the server
   re-imports a board's own export. No web upload form and no base64 or
   URL `put`.
   **Superseded for copies (gr519745, 2026-10-26):** the op now refuses a
   board source — the .epro2 round-trip is lossy (part/LCSC fields,
   routed copper, sub-2-pin nets) — and its `next=` points at
   `put(kind='pcb', args={'op':'fork','from':'<slug>'})`, the lossless
   copy. A real `.epro2` file still imports via the CLI / web upload.

Built on these (gr495289 slices 2–4): `view='epro' args={'verify': true}`
(`precis.pcb.epro_verify`), the sha256 and download path in every epro
response, `PcbHandler.epro_build` (the one in-memory build the view and
the route share), the route in `precis_web/routes/pcb.py` with
a link on the board page, `op='import_epro'`
(`PcbHandler._op_import_epro` — kept as the refusal that names
`op='fork'`), `op='fork'` (`PcbHandler._op_fork`), and the
`precis-pcb-fab-help` section.

## Decisions (Reto 2026-10-21)

- Apply the GET-rebuild-with-`?sha=` pattern to `view='gerber'` and
  `view='fab'` downloads: the same determinism constraints hold
  (`zip_epro` precedent), no bytes go through MCP, no state changes.
  Buildable now.

## Constraints the rulings rest on (keep them true)

- **Deterministic bytes, nothing stored.** `zip_epro` pins member order
  and timestamps and the writer reads no clock, so one board state gives
  one sha256. The route's `?sha=` prefix turns a link to a since-edited
  board into a 409; a test pins view sha == route sha.
- **No caller bytes reach the reader.** `epro.read_archive` has no
  member-count or uncompressed-size caps. That is safe only because the
  CLI / web upload (operator files) are its only inputs —
  `op='import_epro'` no longer feeds it (it refuses board copies,
  gr519745). Any future path that hands it caller-supplied bytes or
  a fetched URL needs caps first (precedent: `ingest/markup.py`'s 16 MiB
  per member / 64 MiB total) and `safe_stream` for the fetch. Under the
  rulings there is no such path.
- **The route is a GET.** Web accounts have no roles
  (`precis_web/__init__.py` §Auth), so a state-changing route would be
  open to the read-only agent credential. The download changes nothing.
- **Agents cannot fetch web UI paths** (Basic auth, 401). The download
  path in the MCP response is for a human's browser; `verify` is the
  agent's round trip.
- **Real boards stay out of model context.** Reto's boards are
  proprietary (memory me474357); neither ruling moves file bytes through
  an MCP argument or result.

## Left

- **Measure** the export size and build time of taz15 and
  heater-base-test on the dev DB, and record them here. The route builds
  in a worker thread per request; a slow real board is the reason to
  look again (not to add a store).
- **Gerber and fab downloads.** `view='gerber'`/`'fab'` have the same
  "path on the server" gap. Build the same pattern for them: a GET that
  rebuilds the zip with a `?sha=` guard (see Decisions).
- **Export dir confinement** stays its own item
  ([pcb-export-dir-confinement](pcb-export-dir-confinement.md)); the
  route never touches `args['dir']`.

## Not covered here (stays on gr495289)

The export warnings now say what they dropped (the orphan-pad line names
stray pads, the blind-via line gives each span); the auto fiducials ride as
`FID<n>` free pads. Still open: the router emitting through vias only unless
the fab capability allows blind/buried (gr495289 ruling 1, after R).
