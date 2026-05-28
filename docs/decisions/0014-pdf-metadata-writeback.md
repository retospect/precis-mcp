# ADR 0014 — Re-introduce PDF metadata write-back during ingest

- **Status**: accepted (2026-05-28)
- **Deciders**: Reto + agent
- **Reverses**: the B4b removal of `write_pdf_metadata()` /
  `enrich_single_pdf()` from `acatome_extract` during the v2 vendor
  (documented at `src/precis/ingest/pdf_metadata.py:17-22`)
- **Related**:
  - ADR 0006 — tri-identifier scheme (`ref_identifiers` design)
  - `docs/design/extract-once.md` — fast-path `pdf_sha256` probe

## Context

`precis_add()` resolves a canonical DOI, title, and author list for
each ingested PDF via the metadata cascade (filename → embedded
metadata → sidecar → lookup → pdf2doi). v2 stores those resolved
values in DB rows (`refs` + `ref_identifiers`) and leaves the PDF
on disk byte-identical.

That works for any consumer that talks to the precis database — but
it doesn't help:

1. **External readers** (Zotero, Mendeley, Calibre, Preview, Acrobat)
   that show the PDF's embedded Info dict / XMP rather than asking
   precis. A user opening the file directly still sees the
   publisher's mis-tagged Title / Author / Subject — or, on
   author-submitted preprints, blank fields.
2. **Re-ingest from a clean database** — if the precis DB is lost
   (disk failure, migration to a new instance, dropped during a
   schema bake), the lookup cascade starts from scratch. Without
   embedded DOIs we re-pay the CrossRef / Semantic Scholar cost
   and risk picking a different best-DOI on borderline cases.

Both points were the motivation for `acatome_extract`'s original
write-back path. B4b removed it because the v1 design used the PDF's
`pdf_sha256` as the single identity key, so a write-back changed the
hash on disk and broke re-ingest matching — fixable only via
`_update_bundle_hash_history()`, a per-paper list of "valid old
hashes". That helper carried enough fixture-maintenance burden to
justify dropping the whole feature.

## Decision

Re-introduce write-back, but key it off the v2 alias model instead of
the v1 history list. Specifically:

* `ref_identifiers` already accepts N rows per `ref_id` with PK
  `(id_kind, id_value)`. We store **both the pre-patch and the
  post-patch `pdf_sha256`** as `pdf_sha256` rows pointing at the
  same ref. A re-ingest of either byte sequence hits the fast-path
  probe and short-circuits.
* The patch lives in a new `precis.ingest.pdf_writer` module that's
  pure (no DB knowledge). `precis_add()` calls it after the lookup
  cascade picks a canonical DOI and after `probe_existing()`
  returns miss (so we never patch a paper we already know about).
* `PaperToWrite` grows a `pdf_sha256_aliases: list[str]` field;
  `write_paper()` inserts one extra `ref_identifiers` row per alias.
* PyMuPDF's incremental save (`incremental=True,
  encryption=PDF_ENCRYPT_KEEP`) is used so the original content
  stream stays byte-identical — only an update section is appended.
  Lower risk on weird PDFs than a full re-serialize.

## Off-switch, not a gate

`PRECIS_PATCH_PDFS=0` (or `false` / `no` / `off` / empty) disables
write-back at runtime. The default is **on** — the user explicitly
asked for write-back as the default behaviour. This matches the
project pattern of "compose env in production knows what to disable;
local dev gets the full pipeline."

## Skip cases (no write, log INFO and continue with pre-hash only)

* `is_encrypted` — DRM'd PDFs can't accept metadata writes.
* `noop` — every target field already matches the existing Info
  dict (re-ingest of an already-patched file).
* `disabled` — env off-switch.
* `error` — any exception during open / set_metadata / save. Logged
  at WARNING with the exception text.

Signed PDF detection landed in the same patch series via a widget
walk (``_has_signature`` — only fires when ``doc.is_form_pdf`` is
true, then bails on the first ``Signature``-type widget). Signed
PDFs return ``PatchOutcome(skipped_reason="signed")`` and the file
is not touched. AcroForms with only text widgets still patch
normally.

XMP write support also landed: ``_build_xmp_packet`` emits a
minimal RDF/XML fragment with ``dc:title``, ``dc:creator``,
``dc:identifier`` (DOI prefixed with ``doi:``), ``prism:doi`` (raw
DOI), and ``prism:url`` (arXiv), called via
``doc.set_xml_metadata()`` alongside the standard Info-dict write.
``_xmp_already_carries`` provides the idempotency check (substring
match on every populated field). Exiftool's ``-Identifier`` flag
now reads our DOI from the canonical XMP slot, not just the
Keywords fallback.

## Consequences

* **Positive** — files on disk become self-describing; external
  readers see correct metadata; DB-loss recovery is no longer a
  re-curation pass.
* **Positive** — the alias-row mechanism is one general feature
  (multi-source dedup, hash drift handling) rather than a special
  case in the writer.
* **Negative** — `pyproject.toml` already pulls `pymupdf>=1.24` so
  no new dep, but the runtime image now mutates files in
  `~/work/new_papers/` (after the watcher moves them to
  `~/work/corpus/<letter>/`, the move happens *after* patch, so
  the corpus-tree paper is the post-patch one).
* **Negative** — this re-introduces a small failure mode the B4b
  removal eliminated: signed PDFs lose signature validity if a
  reader rejects the incremental update. Mitigated by skip-on-
  error + the rarity of signed academic PDFs. Documented as a
  follow-up rather than a blocker.

## Alternatives considered

* **Sidecar `.meta.json` files** next to each corpus PDF. Same
  benefit for "files are self-describing" without touching PDF
  bytes. Rejected because the user's stated goal includes
  *external readers* (Zotero/Acrobat) which don't read sidecars —
  the PDF itself has to carry the truth.
* **XMP metadata writes** instead of (or in addition to) the
  standard Info dict. Better long-term because exiftool's
  `-Identifier` field reads XMP `dc:identifier`, but the
  implementation requires constructing valid XMP XML. Deferred:
  the standard Info dict's `Title` / `Author` / `Keywords` covers
  the read path in `_read_existing_pdf_metadata` for now.
* **Full re-serialize save** instead of incremental. Smaller files
  but higher risk on PDFs with malformed structures. Incremental
  is the conservative choice.
