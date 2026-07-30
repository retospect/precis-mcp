# 0074 — Taproot living citation + the authorial cite pin

- **Status**: accepted (2026-07-30) · **built + verified** (this commit;
  `src/precis/utils/pub_id_lookup.py`, `src/precis/cli/resolve.py`,
  `src/precis/utils/refeye.py`). Taproot slices A1 (living citation, already
  shipped) + A2 (authorial pin, this ADR) of `docs/proposals/taproot.md`
  open #4 ("citations resolve at export time"). Extends
  [0073](./0073-taproot-evidence-relations.md) — reads the evidence graph
  0073 defines the write-path for; adds nothing to that vocabulary or write
  door. The proposal stays `draft` (the shared model + decisions log across
  all five phases); this ADR is the durable record for the export/render
  **contract** — what a `[pub_id]` cite means and how an author can steer it.
- **Deciders**: Reto + agent

## Context

A Taproot claim hub (`kind='finding'` tagged `TAPROOT:claim`, ADR 0073) can
accumulate evidence from many papers over time — `establishes` (originator),
`corroborates`, `contradicts` — and `taproot/seniority.py::derive_evidence`
re-derives the originator/corroborator split from the `cites` graph on every
read (no cached role). A `[pub_id]` cite naming that hub therefore has no
fixed answer to "which paper does this cite resolve to" — the honest answer
changes as evidence accumulates, exactly the problem taproot.md open #4
names.

Two surfaces render that cite: `precis resolve` (document finalisation,
`.bib`/`\cite{}` output) and the `fisheye+1hop` reference ring's Claims group
(read-time preview). Slice A1 already answered "what does a bare `[pub_id]`
resolve to": the *current* derived `establishes` originator(s), falling back
to corroborators, then in-flight — recomputed every run, "living citation."

What A1 left open: an author sometimes has better information than the
derivation — they read the actual paper and know precisely which passage
grounds the claim, or they disagree with what seniority currently derives
(sparse evidence, a citation graph that hasn't caught up). Forcing them to
accept the living default, or to hand-edit the rendered `.bib` and have it
silently overwritten on the next `resolve`, are both wrong. This ADR adds
the escape hatch.

## Decision

**The living default stays the default — pinning is opt-in, inline, and
never silent.** An author overrides a hub cite by extending the placeholder
token itself:

- **Replace**: `[<pub_id>>pa5,pc293]` — cite exactly these handles, ignoring
  the derived `establishes` set for this citation.
- **Supplement**: `[<pub_id>+pa5]` — the derived originators *plus* these
  (deduped).

Handles are universal handles (ADR 0036): `pa5` names a paper directly,
`pc293` names a paper **chunk** (a passage/figure). **A passage handle
resolves to its parent paper's cite_key** — the `.bib` is paper-level, so
pinning a passage doesn't mint a new citable unit, it's the author saying
"grounded specifically here" at a finer address than the derivation offers.
`Store.resolve_handle` already does chunk→parent-ref resolution (used
unchanged, not reimplemented); `Store.ref_cite_keys` resolves the paper to
its oldest cite_key alias, same as the unpinned path.

**Purely syntactic — no storage, no draft-side edge.** The pin lives only in
the placeholder text the author typed. There is no migration, no new
`links` row, no `finding` field. We own the token grammar (it's our own
`[a-z2-7]{6}]` alphabet already), so extending it costs nothing structural:
`pub_id_lookup.py::PLACEHOLDER_RE` grew an optional trailing group
(`(>|+)<handle>(,<handle>)*`) that both `resolve` and the reference ring
share, same as they already share the bare-pub_id grammar.

**The derivation is a living default, never a silently-clobbered override.**
A pin that has gone stale — the citation graph moved on and seniority now
derives a different originator than what's pinned — doesn't get silently
overwritten (that would defeat the point of pinning) and doesn't get
silently honored either (a stale pin citing last month's best-guess paper
while the graph has since converged on a *better* originator is a real
authoring bug). So: **a divergence advisory, replace only.** A **replace**
pin claims to be the *complete* citation, so when its handle set differs
from the hub's currently-derived `establishes` originators, that's a real
"you overrode the derivation" signal worth surfacing. A **supplement** pin
is purely additive ("derived originators plus these") and its own handle
set legitimately differs from the full derived set on every normal use —
it has no divergence concept and never fires the advisory. For a replace
pin, `resolve` prints a stderr diagnostic:

    resolve: [<pub_id>] pinned {pa5} but derived originator is {pa99} — reconsider

Advisory by default — it doesn't block the render. `--strict-pins` (mirrors
`--strict-verified`) promotes a divergence to a CI-gate exit 3, for a
manuscript pipeline that wants to catch a pin gone stale before it ships.
The reference ring reflects the same signal at read time, non-blocking by
construction (it's a preview): the pinned paper is marked `📌` wherever it
renders (even when it isn't part of the derived evidence at all — still
"cited" via the pin), plus a short `(pinned; derived: pa99)` note on
divergence.

**Fail-safe, never fail-silent, on a bad pin.** An unresolvable pinned
handle (bad id, deleted ref, no cite_key alias) is skipped with a warning,
not a hard error — one bad handle in a multi-handle pin shouldn't sink the
whole citation. If that empties a `>` (replace) pin entirely, `resolve`
falls through to the normal hub resolution (derived originators, then
corroborators, then in-flight) rather than dropping the citation outright —
the one thing a pin must never do is make a previously-resolvable cite
vanish. A pin on a **non-hub** finding is meaningless (there's no derived
`establishes` set to override) and is ignored with a warning rather than an
error — the ordinary `primary_cite_key` path resolves as if the pin weren't
there.

## Consequences

- No migration, no new storage, no new `links` relation — this ADR is a
  render/export contract change only, riding the existing `[pub_id]`
  placeholder grammar both surfaces already parse.
- `PLACEHOLDER_RE`'s three capture groups (pub_id, op, handles) are now the
  shared contract every consumer of `precis.utils.pub_id_lookup` must
  respect; a bare `[pub_id]` still matches with groups 2/3 `None` — this is
  additive, not a breaking grammar change (existing citations, tests, and
  in-flight manuscripts are untouched).
- `precis resolve --strict-pins` joins `--strict` / `--strict-verified` as a
  third, independent CI gate a manuscript pipeline can opt into.
- The reference ring's pin reflection is read-only and best-effort (an
  unresolvable pin handle is silently skipped there, not warned — the ring
  previews, it doesn't validate; `resolve` is the surface that gates).

## Alternatives rejected

- **A stored pin (a `links` edge or a `finding.meta` field recording the
  author's chosen cite_keys)** — rejected. It would need a migration, a
  write door, and a staleness-invalidation story of its own (what happens
  when the pinned paper is superseded/merged?) for a decision that's purely
  about *how this one document renders this one citation* — exactly the
  kind of thing the placeholder token already carries syntactically for
  free.
- **Let the pin silently win forever (no divergence check)** — rejected;
  that's indistinguishable from a hand-edited `.bib` that never gets
  updated, which is the exact failure mode the living-citation design (A1)
  exists to prevent. A pin should be a deliberate, periodically-reconsidered
  choice, not a fork that drifts from the evidence graph unnoticed.
- **Only paper-level pins (no passage handles)** — rejected; a `pc<id>`
  passage handle is free to support (it's the same `resolve_handle`
  chunk→parent-ref resolution `precis resolve` already needed) and lets an
  author record *where* they read the grounding, useful context even though
  the `.bib` output is identical to pinning the parent paper directly.
- **Hard error on an unresolvable or empty pin** — rejected; a citation must
  never disappear because of a typo in a handle. Warn-and-fall-through beats
  fail-closed here, matching the rest of `resolve`'s posture (dead-chain,
  no-cite_key, and no-supporter cases all degrade gracefully with a visible
  marker rather than raising).

## Cross-references

- `docs/proposals/taproot.md` — the shared model; open #4 ("citations
  resolve at export time") is what this ADR closes out (A1 + A2).
- `docs/decisions/0073-taproot-evidence-relations.md` — the evidence-relation
  vocabulary (`establishes`/`corroborates`/`contradicts`) and single hub
  write-path this ADR's derivation reads.
- `src/precis/taproot/seniority.py::derive_evidence` — the pure read/derive
  function both the living default and the divergence check compare
  against; unchanged by this ADR.
- `docs/decisions/0036-universal-handles.md` — the `pa<id>` / `pc<id>`
  handle grammar the pin's handle list reuses verbatim.
- `docs/architecture/state-map.md` §Taproot — A1/A2 status lines.
