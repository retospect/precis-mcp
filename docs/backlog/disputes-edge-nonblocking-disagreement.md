---
status: draft
title: "split `contradicts` into non-blocking `disputes` + adjudicated `contradicts` — make disagreement free to file"
model: opus
---

# The corpus cannot disagree with itself

**0 hub↔hub `contradicts` across 1,524 claim hubs** mined from
deliberately overlapping literature.

> **The count in this heading was wrong twice.** It first read "2",
> which was this document's author miscounting: prod holds 2
> `finding`↔`paper` `contradicts` rows, which are **evidence** edges (a
> paper's finding contradicts a claim), not hub↔hub claim disagreements.
> They share the relation slug and nothing else —
> `hub.py`'s `_mint_for_placement` comments the distinction explicitly.
> A read-only prod count on 2026-08-20 put the true hub↔hub figure at
> **zero**: the machine path has never written a single edge.
>
> That the spec's own author confused an evidence edge for a claim
> disagreement, while writing the spec about that very distinction, is
> the strongest available argument for splitting the vocabulary. The case
> for a distinct relation does **not** rest on volume — it would hold at
> zero rows, because the overload is what makes the corpus unreadable to
> the people maintaining it.

**The full census (prod, 2026-08-20, all 6 `contradicts` rows).** The
relation is directional — one row per edge, no reciprocal row, no
self-loops, and no pair appears in both directions:

| direction | n | the relationship it encodes |
|---|---|---|
| `finding` → `paper` | 2 | a finding contradicts a paper |
| `paper` → `finding` | 1 | a paper's result contradicts a claim |
| `finding` → `finding` | 1 | a **review note** contradicts a hub |
| `memory` → `memory` | 2 | unrelated subsystem |
| hub ↔ hub | **0** | *what the relation was designed for* |

The lone `finding`→`finding` row is not a hub↔hub disagreement either:
`fi192706` → `fi191316`, *"dc2445944: fi191316 claim-strength inflation —
'will ultimately require' vs source's 'could be used'"*. The source is
tagged `TAPROOT:review`, not `TAPROOT:claim` — it is the claim-adversary
persona's critique, a **fourth** distinct relationship wearing the same
slug. One relation currently means: evidence-contradicts-claim,
claim-contradicts-paper, critique-contradicts-claim, and
memory-contradicts-memory. The one meaning it was built for is the only
one unused.

**Schema note, relevant to any automated pass.** `links` carries only a
primary key on `link_id` — there is **no unique constraint on
`(src_ref_id, dst_ref_id, relation)`**. Nothing at the DB level prevents
the same edge being written twice. Harmless at 6 hand-written rows;
a live hazard for any pass that writes edges at corpus scale, which must
supply its own idempotency or double every edge on a re-run. Filed
separately as `docs/backlog/links-no-unique-edge-constraint.md`.

> **Corrected 2026-08-20 by a readiness review, after this document
> asserted the wrong cause.** The original draft blamed an incentive bug:
> `contradicts` blocks the other hub, so agents self-censor rather than
> detonate a colleague's claim. **That story is false, and the code says
> so.** No agent chooses to file a hub↔hub `contradicts` at all. It is
> written *automatically* by `taproot.hub._mint_for_placement`
> (`hub.py`, the `new_contradicts` branch) whenever `taproot.canon.place()`
> receives a `"contradicts"` verdict from an unreviewed MEDIUM-tier LLM
> call during ingest. The reasoning below is preserved only because the
> conclusion — build the disagreement half — survives its own broken
> premise. Do not cite the incentive story.

The real mechanism is a retrieval failure, and we already found and fixed
its cause without noticing what else it explained. From `place()`'s own
docstring: *"`judged` may be empty (`block` found no candidates) →
**new**."* Two claims can only be judged as contradicting each other if
`block()` retrieved one as a candidate for the other — and `block()`
retrieved over `card_combined`, **a chunk kind nothing writes**, leaving
88% of hubs invisible to candidate retrieval. An empty candidate set
cannot contain a contradiction. The judge was almost never asked.

So the zero is not 1,524 acts of self-censorship. It is a corpus in which
nearly every claim was compared against nearly nothing. Prod coverage,
measured 2026-08-20: **187 of 1,524 hubs (12.3%) carried a
`card_combined` embedding; 1,524 of 1,524 (100%) carry a `finding_body`
one.** The judge was blind to 88% of the corpus and was almost never
asked a question it could answer.

**Step 0 is done — and it argues for less than this document proposes.**
With the fixed index, near-neighbour pairs among live hubs (cosine over
`finding_body`, pairs already joined by any link excluded) come out at:
9 under 0.05, 23 under 0.10, 76 under 0.15, 205 under 0.20, 1,204 under
0.30. A 15-pair sample from the < 0.15 band contained **one** genuine
disagreement (`fi218623` ↔ `fi218626`, lateral vs vertical
heterostructure FETs outperforming vs falling orders of magnitude short).
The rest were restatements, paraphrases and scope variants.

Two things follow, and they point in different directions:

1. **The near-neighbour band is dominated by duplicates, not disputes.**
   That is partly real and partly an artefact: embedding proximity
   measures *topical* similarity, not propositional opposition. "X
   enhances Y" and "X has no significant effect on Y" can sit far apart
   while two paraphrases sit at 0.03. So 76 is a **floor** on the
   candidate set, and the sample's 1-in-15 is one positive out of
   fifteen — an interval wide enough (roughly 1 to 20 genuine disputes)
   that it cannot by itself decide build-vs-manual.

2. **The higher-value repair in the same fixed index is deduplication.**
   23 pairs under 0.10 look like hubs that should have converged and did
   not, including two byte-identical sentences forked on `scope`
   (`fi191179`/`fi191260`, `fi191192`/`fi191262` — both already known).
   That work is measurable, mechanical, and does not need new vocabulary.

**Revised recommendation: lead with dedup, demote the disputes edge to a
follow-on** — but keep the vocabulary split, on the naming argument
above rather than the volume one.

**A second defect, independent and worse — FIXED 2026-08-20.** The
asymmetry in `place()` was: a `"same"` verdict at low confidence triggers
a *second* `merge-confirm` LLM call before acting, while a
`"contradicts"` verdict triggered no confirmation at any confidence — it
was acted on first-hit, and its action makes another author's claim
unpublishable. We double-checked the reversible, low-harm decision
(merging two hubs) and single-shot the irreversible, high-harm one.

This became urgent rather than theoretical the moment the `block()` fix
was ready to deploy: the branch had never fired at scale *because*
retrieval was blind, so shipping the index repair would have switched on
an unconfirmed publication-blocking write path across all 1,524 hubs at
once. `place()` now filters `"contradicts"` on
`confidence >= confidence_threshold`; a sub-threshold verdict mints the
hub **unlinked**, carrying the suspicion in `Placement.reason` only.
Every consumer already grouped `new` with `new_contradicts`
(`hub.py`'s `apply_placement`, `cli/taproot.py`'s result printer), so the
sub-threshold case falls into a path they all handle.

What is **not** fixed: there is still no `merge-confirm`-equivalent
second call for a high-confidence contradiction. The threshold is a
filter, not a confirmation. Adding a `contradiction_confirm` at BIG tier
would make the two branches genuinely symmetric.

This is the difference between an archive and an instrument. Every gate
we have is an **admissibility** gate — well-formed, sourced, traceable.
Admissible is not true. The only truth-bearing mechanism in the system
is claim-versus-claim disagreement, and it has never actually run.

Independently reached by an external review (2026-08-20,
`get(kind='perplexity-research', id='critique-the-design-of-a-scientific-claim-publication-pipeli')`):
the corpus is *"impeccably traced but epistemically flat"* — claims that
cannot disagree productively cannot support discovery.

## The change

Split one relation into two, along the axis of *who has decided*:

| relation | who files it | blocks publication? | means |
|---|---|---|---|
| `disputes` | anyone, freely — agent or human; **and the ingest LLM judge** | **no** | "these two claims appear to conflict; someone should look" |
| `contradicts` | adjudication only | **yes** | "these two claims do conflict, and it has been established" |

**Both columns describe the target, not today.** Today `contradicts` is
written by the ingest judge with no adjudication anywhere, and `disputes`
does not exist. The single most valuable line of this change is therefore
**repointing `canon.place()`'s `"contradicts"` verdict at `disputes`** —
an unreviewed LLM call then raises a question instead of silently
blocking a stranger's publication, which is what it should have been
doing all along. That one repoint fixes the asymmetry named above without
needing any of the adjudication machinery below.

Filing becomes free; only *resolution* is expensive. That is the correct
cost allocation — noticing a possible conflict is cheap and should be,
establishing one is expensive and should be.

A `disputes` edge is a **question**, not a verdict, and must render as
one everywhere it appears. It is not a demerit against either hub.

## What carries the disagreement: an edge, or a claim about two claims?

Reto, 2026-08-20: *"we have one paper, a nanopub with one claim, and
another nanopub with an opposing claim, and a … nanopub that says A and B
are opposing?"*

Yes — and that is the better structure. The table above quietly assumed a
`links` row, which is what we have today. A `links` row is a poor carrier
for a scientific disagreement: it has a `set_by` and a `meta` blob, no
sentence, no evidence, no author accountability, no signature, no
identity. It cannot be cited, reviewed, or disagreed with. It is a
database fact about two rows, not a scientific statement.

**A statement about two claims is itself a claim.** This is standard
nanopublication practice — assertions whose subject is another
nanopublication, published with their own trusty URI, provenance and
signature; the micropublication model's `supports` / `challenges` and
CiTO's `cito:disagreesWith` are exactly this shape. Making the
adjudication a first-class hub buys, for free, everything hub machinery
already does: an authored sentence, a `pub_id`, grounding, review,
minting, signing, anchoring.

It also buys the property that makes this an argumentation network rather
than a flat edge set: **a dispute can itself be disputed.** "A and B
conflict" is a substantive, falsifiable, often-wrong claim — the expected
majority verdict is `scope-mismatch`, i.e. *the dispute was mistaken*.
An edge cannot record that it was overturned; a claim can.

### The two tiers, and which is which

The cost argument above still holds, so keep both — they are not
competitors, they are the two ends of one lifecycle:

| tier | carrier | who | cost | means |
|---|---|---|---|---|
| **flag** | `disputes` link | anyone, freely | ~zero | "these look like they conflict; someone should look" |
| **adjudication** | a **claim hub** whose sentence is about two hubs | a reviewer, with reasoning | full mint path | "these do/don't conflict, and here is why" |

Filing stays free because the *flag* is free. The nanopub appears only at
resolution — which is where the spec already says the cost belongs.

**`contradicts` is then derived, not authored.** Today it is hand-set,
which is why it doubles as both "I suspect" and "it is established" and
why nobody dares set it. Under this structure a live `contradicts` edge
exists because a signed adjudication hub with verdict `genuine-conflict`
says so. The blocking gate keys on an artifact with an author, evidence
and a signature — a far better warrant for refusing to publish someone's
claim than an anonymous row.

### The gate that has to change first

Every claim hub today must ground in a **primary-source passage**. An
adjudication hub grounds in *two other claims* plus, usually, a third
source. That is a second grounding mode, and `nanopub/gates.py` currently
has no notion of it — an adjudication hub would be rejected as unsourced.
Resolve this before building, not after: either admit
`grounding.mode='claims'` explicitly, or the whole tier is unmintable.

Open question deliberately left open: whether the adjudication hub's
`pub_id` should hash the two claim ids alongside the sentence. It
probably must — otherwise two adjudications of *different* pairs that
happen to share a sentence ("These claims differ in measurement regime.")
collide into one.

## Why this is safe to make free

The reason `contradicts` blocks is sound: we must not publish a claim
that is known to be contested. That reason applies to *adjudicated*
conflict and to nothing else. An unreviewed suspicion has never been
grounds to block publication, and treating it as such is what produced
the silence. Publication safety is fully preserved by the `contradicts`
half.

## Adjudication verdicts

A `disputes` edge is resolved into exactly one of five outcomes. Only
the last blocks:

- `same-claim` → attach evidence to the survivor, retire the duplicate
- `refines` → typed `refines` edge, `disputes` retired
- `scope-mismatch` → different functional / cell size / measurement
  regime; annotate scope on both, no edge. **Expected majority.**
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts`, plus a hunt for a third
  adjudicating source

## Scope of work

**Split into two shippable specs** (readiness review, 2026-08-20). Part 2
is `blocked-by` part 1; do not start it first.

### Part 1 — write path + vocabulary (ship this alone)

0. **Measure first.** Re-run placement over the corpus with the `block()`
   fix in, and count `contradicts`. This is a read-only measurement and it
   sizes everything below. If the count moves sharply, revise this spec
   before building.
1. **Relation vocabulary** — add `disputes`. Check whether the relation
   set is a DB enum (forward-only migration) or open text before
   assuming a migration is needed.
1a. **Repoint the ingest judge** — `taproot/canon.py` `place()`'s
   `"contradicts"` branch and `taproot/hub.py`'s `new_contradicts`
   placement action must emit `disputes`, not `contradicts`. **This is the
   change with the highest value-to-risk ratio in the document** and the
   original spec touched neither file.
2. **Publish gates — three call sites, not one.** `nanopub/gates.py`'s
   `check_contradicts` and `nanopub_render.py` both filter to
   `EVIDENCE_SRC_KINDS = {paper, patent, edgar, datasheet}`, which
   **excludes hub sources** — so the blocking gate may already be dormant
   for hub↔hub edges. `nanopub/overview.py`'s `hub_rows`/`hub_tree` read
   it *unfiltered*. Establish which of the three actually blocks today
   before changing any of them; the answer may be "none," which changes
   the story again.
3. **A write door with invariants.** `link_claims`'s
   `CLAIM_LINK_RELATIONS = {"refines", "conjunct-of"}` excludes both
   `disputes` and manual `contradicts`; the generic MCP `link()` door has
   none of `link_claims`'s guards. Decide which door files a `disputes`
   and give it the guards — the original spec never said.
4. **Render** — the claim page and `view='nanopub'` currently show an
   UNMINTABLE warning for a contradicts edge. `disputes` gets its own
   visibly non-blocking treatment ("open question", with the counterpart
   hub linked), never the red banner.

### Part 2 — review workflow (blocked by part 1)

5. **Second grounding mode** — `grounding.mode='claims'` so an
   adjudication hub, whose subject is two other hubs, can pass
   admissibility. Without it the adjudication tier is unmintable.
6. **Skills** — `precis-taproot-help` and `precis-nanopub-help` must
   actively *invite* `disputes`: filing one is free, expected, and harms
   neither claim. Add the five adjudication verdicts.
7. **Reviewer persona** — `precis-adversarial-reviewer` **cannot simply be
   adapted**, contrary to the original spec: `scripts/review-paper/run.sh`
   runs it against a single paper handle, and `precis-common-reviewer.md`
   makes it explicitly read-only. Hub review needs pairwise comparison and
   a write capability, neither of which it has. Budget for a new persona
   or a real extension, not a rename.

## First run

Run over the dense topic neighbourhoods first — conflicts hide where
coverage is thickest: MOF conduction, DNA bricks, molecular switches.

Two seed cases already in hand that no automated gate caught:

- fi191120 vs fi218681 — possible genuine contradiction
- pa1992 — GPa/TPa unit error, off by ~10³

Success looks like the count going from 2 to hundreds. **A large
`disputes` graph is the deliverable, not a regression** — it is the map
of where inquiry should go. Anyone reading the number as corpus damage
has misunderstood the change.

## Related

- `docs/backlog/claim-review-mechanism.md` — the procedure this plugs into
- `docs/backlog/nanopub-corpus-remediation.md` Phase 4 — the original
  observation and the verdict list

## Readiness review (2026-08-20)

Verdict passed to caller: **needs-work** (5 blockers, 3 advisories, split
suggested). Checked against the code, not just the prose. Full findings
below; not repeated in the caller response.

- **blocker** — no `## Acceptance criteria`, `## Explicitly NOT in scope`,
  `## Target + blast radius`, or `## Open questions / decisions log`
  section (`docs/backlog/TEMPLATE.md`'s required shape). There is no
  buildable definition of "done" anywhere in this file — "the count going
  from 2 to hundreds" (First run) is a corpus-sweep outcome, not something
  a gate or a post-deploy look can check per scope item. Each of the 5
  scope items needs its own checkable criterion before this can carry
  `status: ready`.

- **blocker** — the spec's central design claim ("`contradicts`: who
  files it — adjudication only") misdescribes the dominant real edge
  shape. `taproot.hub.link_claims` — "the single write door for
  claim→claim links" — restricts `relation` to `CLAIM_LINK_RELATIONS =
  frozenset({"refines", "conjunct-of"})` (`hub.py:91`); it does not accept
  `contradicts` at all. The only hub↔hub `contradicts` edge in the system
  is written by a raw `store.add_link` call inside
  `taproot.hub._mint_for_placement` (`hub.py:1009-1022`), triggered
  automatically by `taproot.canon.place()` branch 3 (`canon.py:1090-1097`)
  whenever `dedup_judge` — an unreviewed MEDIUM-tier LLM call, "biased
  hard toward different" per its own docstring — returns verdict
  `"contradicts"` during ingest. Today's hub↔hub `contradicts` is itself
  an automatically-filed, unreviewed edge, not an adjudicated one. Scope
  items 1-5 never touch `taproot/canon.py`, `taproot/hub.py`, or
  `CLAIM_LINK_RELATIONS` — so as scoped, this pipeline keeps minting
  blocking `contradicts` edges from unreviewed LLM verdicts exactly as
  before, unchanged by the rest of the spec.

- **blocker** — the write path for filing a `disputes` edge (or, today,
  a manual `contradicts` edge) is unaddressed and the two candidate doors
  disagree on invariants. `taproot.hub.link_claims` enforces real
  guards (both endpoints must be live `TAPROOT:claim` findings, no
  self-link, idempotent) but only for `{refines, conjunct-of}`. The
  generic MCP door (`handlers/_link_tag_ops.py::apply_link_ops`, backing
  `put(link=, rel=)`) only checks `validate_relation` (FK registration)
  and calls `store.add_link` directly — no endpoint-kind check, no
  hub-ness check, no self-link guard. Because `contradicts` is already a
  registered relation (migration 0001), an agent can *already* write
  `finding --contradicts--> finding` via the generic door today, bypassing
  `link_claims` entirely — and once `disputes` is added to the `relations`
  table (item 1), the same is true for it, with the same missing guards.
  The spec never says which door the adversarial reviewer (or any other
  filer) should use for `disputes`; if it's the generic door, `disputes`
  inherits none of `link_claims`'s invariants; if it's meant to be
  `link_claims`, item 1 needs to add `disputes` to `CLAIM_LINK_RELATIONS`
  (unscoped) and item 5's reviewer needs to actually call it (also
  unscoped — the persona as it exists cannot call any tool that writes).

- **blocker** — item 2's "confirm no other gate ... treats 'any
  disagreement edge' as blocking" undercounts the actual surface, and the
  gate it names may already be dormant. `nanopub.gates.check_contradicts`
  and `nanopub_render.py`'s per-hub `disputed` flag both key off
  `evidence.HubBundle.contradicts`, which is built by
  `seniority._fetch_evidence_rows` filtered to `EVIDENCE_SRC_KINDS =
  {"paper", "patent", "edgar", "datasheet"}` (`taproot/hub.py:126-128`) —
  it structurally excludes hub-kind sources, i.e. it never sees the
  hub↔hub `contradicts` edges described above. Meanwhile no code path
  found (`taproot.hub.attach_evidence`'s `role` always defaults to
  `_DEFAULT_ROLE = "corroborates"`; no caller passes
  `role="contradicts"`) ever writes the paper/patent-shaped `contradicts`
  role this gate actually looks for. So the corpus's "2 `contradicts`"
  are almost certainly all hub↔hub, and the gate/render pair the spec
  names as the one to guard is very likely never firing today regardless.
  A *third*, separately-sourced call site — `nanopub.overview.hub_rows` /
  `hub_tree` (the `/nanopub` browse/dashboard page, not named in item 3
  at all) — computes its own `disputed` bucket with an **unfiltered**
  query (`overview.py:277-285`, `l.relation = 'contradicts'`, no
  source-kind restriction) that *does* pick up hub↔hub edges. Item 2/3
  need to state plainly which of these three call sites is the one that
  currently blocks anything in practice, and item 3 needs to add
  `overview.py`'s dashboard query to its target list.

- **blocker** — item 5 ("adapt it, do not write a new one" re
  `precis-adversarial-reviewer`) is wishful as written.
  `scripts/review-paper/run.sh` invokes the persona against exactly one
  `paper:` handle; the persona is explicitly **read-only** by the shared
  ground rules it includes (`precis-common-reviewer.md` §"Ground rules
  for read-only work": "Treat the subject ref as read-only... Do not file
  gripes... Your output is the report"). "First run" needs pairwise/
  neighbourhood hub comparison (`fi191120 vs fi218681`), not single-
  document review, and needs the reviewer to *write* `disputes` /
  `refines` / `contradicts` verdicts, not just report them. Of the 7
  existing categories, only `unsupported-claim` and `overgeneralisation`
  plausibly transfer to a claim-hub sentence; `internal-inconsistency` as
  defined is about one document's self-consistency, not cross-claim
  disagreement; `missing-control`/`statistics`/`selective-citation`/
  `reproducibility` don't apply to a short TAPROOT:claim sentence at all.
  What's needed is closer to a new persona (pairwise input shape, a write
  capability none of its siblings have, ~2 reused categories) than an
  adaptation of this one.

- **advisory** — item 1's "check whether the relation set is a DB enum...
  or open text before assuming a migration is needed" is answerable today
  by reading any sibling migration (e.g. `0100_taproot_refines_relation.sql`,
  `0085_integration_disposition_relations.sql`): the vocabulary is a DB
  table (`relations`) + FK (`links_relation_fkey`) + a hand-maintained
  `Relation` Literal in `store/types.py` that every migration's own
  comment says to keep in sync with. State this as fact, not an open
  question — cheap to fix, but as written it invites a builder to
  re-derive an already-established, well-documented convention.

- **advisory** — "First run" implies a browsable backlog of newly-filed
  `disputes` edges for a reviewer to work through (the "map of where
  inquiry should go"), but no scope item builds a queue/dashboard for
  it — `overview.py` already has a `withheld_count` queue for evidence
  edges; nothing analogous is scoped for `disputes`.

- **advisory** — model: opus is appropriate (this is architecture-adjacent
  judgment work touching taproot's ingest pipeline, not sonnet/haiku-tier
  work), no mismatch there.

**Split signal.** This reads as (at least) two independently-shippable
deliverables with a real dependency, not one deliverable from several
angles:

1. **The write-path/vocabulary fix** — add `disputes` to the DB relation
   table + `store/types.py`; decide and implement which door files it
   (extend `CLAIM_LINK_RELATIONS` + `link_claims`, most likely, given its
   invariants); decide the fate of `taproot.canon.place()` branch 3 /
   `new_contradicts` (should the automatic ingest-time LLM verdict now
   write `disputes` instead of `contradicts`, given that's the actual
   source of nearly all existing `contradicts` edges?); reconcile
   `gates.check_contradicts` / `nanopub_render.py` / `overview.py`'s three
   different `contradicts`-reading queries. Touches `taproot/canon.py`,
   `taproot/hub.py`, `nanopub/gates.py`, `nanopub/overview.py`,
   `nanopub_render.py`, a migration.
2. **The review workflow** — the pairwise/neighbourhood hub-comparison
   reviewer (new or heavily adapted persona), the skills prose inviting
   `disputes`, and the "First run" sweep itself.

(2) cannot be built or run meaningfully until (1) exists — there is
nowhere for a reviewer's `disputes` verdict to land, and no settled
answer for what should happen to the automatic pipeline's existing
`contradicts` writes, until (1) is decided. Suggest (1) as its own
backlog item, `blocked-by` nothing, with (2) `blocked-by: <that slug>`.

**Note on scope growth mid-review.** The file gained the "What carries
the disagreement" / "The two tiers" / "The gate that has to change
first" sections and scope item 2a after this review's code checks were
already in progress (a concurrent edit — this addendum reconciles rather
than restarts):

- **blocker** — an inline, unresolved, blocker-severity open question:
  "Open question deliberately left open: whether the adjudication hub's
  `pub_id` should hash the two claim ids alongside the sentence." Per
  this file's own template (`## Open questions / decisions log` — still
  absent, see above), no blocker-severity open question may remain for
  `status: ready`. It also isn't filed in that section, so it's easy to
  miss on a skim.
- **blocker** — scope ambiguity between the two tiers as now written.
  The new prose states "`contradicts` is then derived, not authored.
  Today it is hand-set" — but that is not what the code shows (see the
  second blocker above): today's hub↔hub `contradicts` is not hand-set,
  it is auto-written by `taproot.canon.place()`'s unreviewed LLM verdict.
  Item 2a defers the adjudication-hub tier and says "ship the flag tier
  first; it needs none of this" — but the flag-tier-only version still
  inherits the false "hand-set" premise for what happens to `contradicts`
  meanwhile, and the file doesn't say whether the adjudication-hub tier
  (a new claim-hub subtype, a new `grounding.mode='claims'`, a new
  `pub_id`-hashing rule) is in scope for *this* backlog item or a
  separate follow-on. As written, a large, independently-shippable third
  deliverable (the adjudication-hub tier) is sitting in the same file as
  the "ship first" flag tier without a clean boundary — add it as the
  split's third leg, `blocked-by` leg (1), rather than merging it into
  this file's scope.
