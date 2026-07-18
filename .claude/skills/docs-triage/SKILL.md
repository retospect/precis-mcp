---
name: docs-triage
description: >-
  Adjudicate whether a docs/design plan or an ADR has gone dead and should be
  cut, keeping docs/ current-state instead of an append-only historical record.
  Reach for it when scripts/docs-orphans flags candidates, when a feature ships
  and leaves its plan doc behind, or when a superseding ADR reaches
  accepted+implemented. Repo-dev tool for developing precis-mcp; NOT a precis
  product skill.
---

# docs-triage — cut dead plans, keep docs true

**The disease.** A `docs/design/*.md` is a point-in-time *plan*. When its
feature ships, the truth moves into code + the ADR, but nobody deletes the
plan — so it lingers and gets READ as current-state, and rots. Cure:
**delete-default** ("rest in git for the archaeologists"). `git log` is the
history; `docs/` is the present.

**Not a bulk delete.** Most design docs are load-bearing — `docs/design/`
looks bloated but the pile is far less rotten than the file count implies.
Adjudicate each doc with the dead-check below; the cost of a wrong delete is a
dangling ref in live code.

## Start here

    scripts/docs-orphans     # buckets every docs/design/*.md by inbound refs

It is advisory (never deletes, never fails a build). It does the mechanical
ref-scan only — bucket ≠ verdict. You apply the judgment steps it can't:

| Bucket | Meaning | Your move |
|---|---|---|
| load-bearing | ref from `src/` or a current anchor | keep (or de-ref first) |
| ADR-linked | ref only from `docs/decisions/` | run the dead-check |
| DOC-only | ref only from other design/proposal docs | weak protection — dead-check |
| ORPHAN | no inbound ref anywhere | dead-check (usually delete) |

An ORPHAN is *not* auto-dead: the scanner can't see the memory index or the
state-map. Active plans-of-record (e.g. `catalyst-discovery-quest`) scan as
ORPHAN yet are kept — that's exactly what steps 2–3 catch.

## The per-doc dead-check

Prove a doc is dead before cutting it. KEEP if ANY of:

1. **Load-bearing ref.** A `src/` docstring, a current anchor
   (`CLAUDE.md` / `AGENTS.md` / `codebase.md` / `state-map.md` / `conventions/`
   / product skills), or a build file (`docker/`, `.github/`, `scripts/`)
   points at it as the mechanism/spec-of-record. A ref only from another
   historical doc or the ADR-supersession README does NOT protect it.
   - **Sealed-migration path-link ⇒ KEEP, always.** If the only ref is a
     `docs/design/…md` link inside a `migrations/*.sql`, ADR-0005 forbids
     editing that file → you can't de-ref → you can't delete.
2. **Active plan-of-record.** Cross-check the memory in-flight index — a doc
   that is the design-of-record for an OPEN thread stays until that work ships.
3. **Live spec.** The state-map/codebase points AT it as the current
   description (e.g. `storage-v2`), OR a **proposed/draft ADR delegates real
   build-detail to it** ("Design: …", "build order: …", "Tracks …"). A proposed
   ADR + its design doc are one unit — the doc is the live spec.
4. Otherwise → **DELETE**. Shipped feature, truth now in code + the ADR
   captures the decision, nothing current points at it.

**Delete + fix the pointer in the SAME commit.** When you cut a doc an ADR
linked as its "plan artefact / this change's plan", drop that dead pointer from
the ADR body (keep any inline rejected-alternatives rationale — that lives once,
in the ADR). Then re-scan for stragglers:

    grep -rn "<docname>" . --exclude-dir=.git    # must be empty after the cut

Fix every surviving ref (in kept docs, README indexes, build files). If a fix
would falsify a kept doc — e.g. deleting `dream-agent-loop` exposed a
`dreaming.md` block describing the *reversed* approach — correct that block to
code-truth, don't just de-link.

## ADRs — compile-and-cut (rare, careful, last)

ADRs encode WHY and are widely referenced; they rot slowly (status lines +
supersession graph). Per **ADR-0058** (move-not-delete): relocate to
`docs/decisions/archive/`, keep the filename, prepend a one-line archive
banner (the only edit to the sealed body), update every referrer (README index,
supersession graph, relative links) in the same change.

Archive a predecessor ONLY when **all** hold:
- a **successor is accepted + implemented** (verify in code — not merely
  "proposed/draft"), and
- the successor **names** the predecessor, and
- the predecessor is **fully** superseded.

Two traps that mean NOT archivable:
- **"extends" ≠ "supersedes".** `0007→0017`, `0004→0009`, `0012→0019` extend;
  the predecessor is in force. Keep.
- **Partial supersession.** `0006`'s slug section died but `pub_id`/`cite_key`
  remain in force (`ref_identifiers` is live) → the ADR stays.

**Trigger = a successor reaching accepted+implemented, NOT an ADR count.**
Rot accrues on feature-ship, not on ADR-write; a "every N ADRs" sweep fires at
the wrong time. When nothing is ripe, say so and stop — a clean ADR set is the
expected outcome, not a failure to find work.

## Order & ship

Batch, safe → risky, each its own worktree + ship (per-batch reversible):
`design/` shipped-orphans → `proposals/` → freshen current-state (bump
`codebase.md` `_Verified @ <sha>`) → ADR archive (last). Keep AGENTS.md's
`docs/design/` = **delete-on-ship** rule true. The ship light-gate's
doc-pointer test independently confirms no dangling links before merge.
