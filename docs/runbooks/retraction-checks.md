# Retraction checking — demand-driven model, and how to verify it

Retraction checking is **demand-driven by design**: there is no corpus sweep.
Reto's explicit call — *"we check on taproot cite discovery, and when the user
presses the button. That's all."* Sparse coverage is deliberate, so
**"never checked" is a first-class state everywhere** and must never be
rounded down to "clean".

## The two triggers

1. **`precis.taproot.hub::attach_evidence`** — a paper entering the claim graph.
2. **The draft's retraction-watch button** — `precis.export.retraction`, routed
   from `src/precis_web/routes/drafts.py`.

Around them: a 30-day TTL gate (`ingest.provenance::check_ref_retraction`);
export blocks on `retracted` with an `ignore_retractions=1` override; search
**downranks (never excludes)** and annotates with `⚠`.

## Verification state

**Trigger 2 — verified in prod 2026-08-12.** A watch-button press on draft
173020 completed a 40-cite walk in 23.3s and stamped 37 papers
(`retraction_checked_at` 10 → 92). TTL gate, Crossref, and
`touch_retraction_checked` all work end to end.

**Trigger 1 — still unverified, and a flat count is NOT evidence against it.**
The last taproot evidence edge (`links.relation IN ('establishes',
'corroborates','contradicts')`) predated the deploy, and no `chase`/`hub_refine`
jobs had run for days. **Zero upstream traffic fully explains a flat number** —
do not read one as a defect. Verifying trigger 1 requires taproot chase running
first.

**Accrual check.** Count live `kind='paper'` refs with `retraction_checked_at`
set vs. total, via `scripts/prod-psql`.

## Correctness trap — do not "un-retract"

A clean Crossref read must call **`store.touch_retraction_checked`** (timestamp
only). It must **never** call `set_retraction_status(status=None)`: Crossref is
blind to Retraction-Watch-only notices, so writing back a null status would
**silently un-retract** papers that a non-Crossref source had flagged.

(Incidental API note: `precis.store.types.Ref` exposes `.id`, not `.ref_id`.)

## Cap semantics (fixed 2026-08-15)

The 40-cite cap was originally a **head slice**, so every press re-selected the
same first 40 and cites 41..N were permanently uncheckable (draft 173020 cites
95). `select_for_check` now orders cites **by need** — never-checked first, then
oldest stamp — which makes the cap a *per-press budget* rather than a window. A
`check_slugs` kwarg narrows only the network walk, keeping the report whole so
the pane's "N of M never checked" prompt still reflects the full set.

Two related fixes landed with it: the ~10s `cited_paper_refs`/`render_body` had
been running blocking on the event loop *outside* the 90s budget (now inside the
thread+budget), and the 504 text blamed Crossref when Crossref measured
0.34s/DOI.

**Open config gap.** `PRECIS_CROSSREF_MAILTO` is unset on melchior's web
process, so Crossref calls go out anonymous and throttling-prone — the likely
cause of the one observed 90s blowout. Setting it is the fix.

## Known limitation

The export override (`ignore_retractions=1`) leaves only a server
`log.warning` — it is **not** traced in the exported artifact itself, though
the sources appendix does record the override.
