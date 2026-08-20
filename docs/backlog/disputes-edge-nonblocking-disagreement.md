---
status: draft
title: "split `contradicts` into non-blocking `disputes` + adjudicated `contradicts` — make disagreement free to file"
model: opus
---

# The corpus cannot disagree with itself

**Lead with dedup, demote the disputes edge to a follow-on.** Part 1
(vocabulary + write path) ships alone; Part 2 (review workflow) is
`blocked-by` it. Keep the vocabulary split regardless of volume: one slug
(`contradicts`) currently means at least four unrelated relationships —
evidence-contradicts-claim, claim-contradicts-paper,
critique-contradicts-claim, memory-contradicts-memory — and the case for
separating them rests on that naming collision, not on row count. It holds
at zero hub↔hub rows; don't reopen it on "but there's no data."

**Census (prod, 2026-08-20; all 6 `contradicts` rows, directional — no
reciprocal or self-loop rows):**

| direction | n | the relationship it encodes |
|---|---|---|
| `finding` → `paper` | 2 | a finding contradicts a paper |
| `paper` → `finding` | 1 | a paper's result contradicts a claim |
| `finding` → `finding` | 1 | a **review note** contradicts a hub |
| `memory` → `memory` | 2 | unrelated subsystem |
| hub ↔ hub | **0** | *what the relation was designed for* |

The `finding`→`finding` row isn't a hub↔hub disagreement either:
`fi192706` → `fi191316`, the claim-adversary persona's critique
(`TAPROOT:review`-tagged, not `TAPROOT:claim`) — a fifth relationship on
the same slug, not the fourth. (One count in this heading was wrong twice
before this census — verify against a fresh read-only query, not a
remembered number, before citing it again.)

The zero is not 1,524 acts of self-censorship — no agent chooses to file a
hub↔hub `contradicts`; it is written automatically by
`taproot/hub.py::_mint_for_placement`'s `new_contradicts` branch whenever
`taproot/canon.py::place()` gets a `"contradicts"` verdict from an
unreviewed MEDIUM-tier LLM call during ingest. `place()` can only judge two
claims as contradicting if `block()` retrieved one as a candidate for the
other, and `block()` retrieved over `card_combined` — a chunk kind only
187/1,524 hubs (12.3%) carried an embedding for, against 1,524/1,524 for
`finding_body`. The judge was blind to 88% of the corpus and was almost
never asked a question it could answer. (Fixed separately — this file's
numbers below post-date the fix.)

**Schema note, relevant to any automated pass.** `links` has no unique
constraint on `(src_ref_id, dst_ref_id, relation)` — only a `link_id`
primary key. Harmless at 6 hand-written rows; a live hazard for any pass
writing edges at corpus scale, which must supply its own idempotency or
double every edge on a re-run. Filed separately:
`docs/backlog/links-no-unique-edge-constraint.md`.

**Near-neighbour measurement, post-fix (prod, 2026-08-20).** Cosine over
`finding_body`, pairs already joined by any link excluded: 9 pairs under
0.05, 23 under 0.10, 76 under 0.15, 205 under 0.20, 1,204 under 0.30. A
15-pair sample from the <0.15 band held **one** genuine disagreement
(`fi218623` ↔ `fi218626`, lateral vs vertical heterostructure FETs
outperforming vs falling orders of magnitude short); the rest were
restatements, paraphrases, and scope variants.

**Retrieval-shaped epistemics.** Both this near-neighbour pass and any
future opposition finder built on it are ANN-retrieval-first. That
structurally favours claims phrased like their neighbours and misses both
independent confirmation and genuine contradiction expressed in different
vocabulary — embedding proximity measures *topical* similarity, not
propositional opposition. "X enhances Y" and "X has no significant effect
on Y" can sit far apart in embedding space while two paraphrases sit at
0.03 cosine. So any retrieval-based opposition finder is structurally
biased against exactly the disagreements most worth finding: its recall is
a **floor**, never an estimate, and no coverage number it produces may be
reported as one. The 76-under-0.15 figure above is such a floor; the
1-in-15 hit rate is one positive in a plausible 1–20 range, wide enough
that it cannot alone decide build-vs-manual for a dedicated finder.

**What the near-neighbour band is actually good for.** It is dominated by
duplicates, not disputes — 23 pairs under 0.10 look like hubs that should
have converged and didn't, including two byte-identical sentences forked
on `scope` (`fi191179`/`fi191260`, `fi191192`/`fi191262`). That repair is
measurable, mechanical, and needs no new vocabulary — hence lead with
dedup.

**Confirmation asymmetry — fixed 2026-08-20.** `place()` used to
double-check a `"same"` verdict at low confidence with a second
`merge-confirm` LLM call, but acted on `"contradicts"` first-hit at any
confidence — reversible low-harm decision double-checked, irreversible
high-harm one single-shot. This became urgent once the `block()` retrieval
fix above was ready: shipping it would have switched on an unconfirmed
publication-blocking write path across all 1,524 hubs at once. `place()`
now filters `"contradicts"` on `confidence >= confidence_threshold`; a
sub-threshold verdict mints the hub **unlinked**, reason recorded on
`Placement.reason` only, and every existing consumer already groups `new`
with `new_contradicts` (`taproot/hub.py::apply_placement`,
`cli/taproot.py`'s result printer). **Not fixed:** there is still no
`merge-confirm`-equivalent second call for a high-confidence contradiction
— the threshold filters, it doesn't confirm. A `contradiction_confirm` at
BIG tier would make the two branches symmetric.

Every gate here is an admissibility gate — well-formed, sourced,
traceable. Admissible is not true. Claim-versus-claim disagreement is the
only truth-bearing mechanism in the system, and it has never actually run.
Independently reached by an external review
(`get(kind='perplexity-research', id='critique-the-design-of-a-scientific-claim-publication-pipeli')`):
the corpus is *"impeccably traced but epistemically flat."*

## The change

Split one relation into two, along who has decided:

| relation | who files it | blocks publication? | means |
|---|---|---|---|
| `disputes` | anyone, freely — agent, human, or the ingest LLM judge | **no** | "these two claims appear to conflict; someone should look" |
| `contradicts` | adjudication only | **yes** | "these do conflict, and it has been established" |

**Both columns describe the target, not today.** Today `contradicts` is
written by the ingest judge with no adjudication anywhere; `disputes`
doesn't exist. The single highest-value line of this change is therefore
**repointing `canon.place()`'s `"contradicts"` verdict at `disputes`** — an
unreviewed LLM call raises a question instead of silently blocking a
stranger's publication. That one repoint fixes the confirmation asymmetry
above without any of the adjudication machinery below. Filing becomes
free; only *resolution* is expensive. A `disputes` edge is a **question**,
not a verdict, and must render as one everywhere it appears — no demerit
against either hub.

## What carries the disagreement: an edge, or a claim about two claims?

Reto, 2026-08-20: *"we have one paper, a nanopub with one claim, and
another nanopub with an opposing claim, and a … nanopub that says A and B
are opposing?"* — yes, and that's the better structure. A `links` row
(`set_by` + a `meta` blob) has no sentence, evidence, author, signature, or
identity: it's a database fact about two rows, not a scientific statement.
**A statement about two claims is itself a claim** — standard
nanopublication practice (assertions whose subject is another
nanopublication, own trusty URI/provenance/signature); the micropublication
model's `supports`/`challenges` and CiTO's `cito:disagreesWith` are this
shape. Making the adjudication a first-class hub buys, for free, everything
hub machinery already does — authored sentence, `pub_id`, grounding,
review, minting, signing, anchoring — and one more property: **a dispute
can itself be disputed.** "A and B conflict" is falsifiable and often
wrong (`scope-mismatch` is the expected majority verdict); an edge can't
record that it was overturned, a claim can.

### The two tiers

Not competitors — two ends of one lifecycle:

| tier | carrier | who | cost | means |
|---|---|---|---|---|
| **flag** | `disputes` link | anyone, freely | ~zero | "these look like they conflict; someone should look" |
| **adjudication** | a **claim hub** whose sentence is about two hubs | a reviewer, with reasoning | full mint path | "these do/don't conflict, and here is why" |

Filing stays free because the flag is free; the nanopub appears only at
resolution. **`contradicts` becomes derived, not authored** — a live edge
exists because a signed adjudication hub with verdict `genuine-conflict`
says so, a far better warrant for refusing publication than an anonymous
row.

### The gate that has to change first

Every claim hub today grounds in a primary-source passage. An adjudication
hub grounds in *two other claims* plus, usually, a third source —
`nanopub/gates.py` has no notion of this second grounding mode; as-is, an
adjudication hub is rejected as unsourced. Resolve before building: either
admit `grounding.mode='claims'` explicitly, or the adjudication tier is
unmintable.

**Open question, deliberately unresolved:** should the adjudication hub's
`pub_id` hash the two claim ids alongside the sentence? Probably yes —
otherwise two adjudications of *different* pairs sharing a sentence
("These claims differ in measurement regime.") collide into one.

## Why this is safe to make free

`contradicts` blocking is sound reasoning applied to the wrong scope: we
must not publish a claim known to be *contested*. That reasoning covers
adjudicated conflict and nothing else — an unreviewed suspicion was never
grounds to block, and treating it as such produced the silence measured
above. Publication safety is fully preserved by the `contradicts` half.

## Adjudication verdicts

A `disputes` edge resolves into exactly one of five outcomes. Only the
last blocks:

- `same-claim` → attach evidence to the survivor, retire the duplicate
- `refines` → typed `refines` edge, `disputes` retired
- `scope-mismatch` → different functional / cell size / measurement
  regime; annotate scope on both, no edge. **Expected majority.**
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts`, plus a hunt for a third adjudicating
  source

## Scope of work

**Two shippable specs.** Part 2 is `blocked-by` part 1; don't start it
first.

### Part 1 — write path + vocabulary (ship this alone)

0. **Measure first.** Re-run placement over the corpus with the `block()`
   fix in, and count `contradicts`. Read-only; if the count moves sharply,
   revise this spec before building.
1. **Relation vocabulary** — add `disputes`. The relation set is a DB
   table (`relations`, PK'd on `slug`, FK'd from `links.relation` via
   `links_relation_fkey`) plus a hand-maintained `Literal` at
   `src/precis/store/types.py::Relation` — both need a migration and both
   need updating in lockstep (pattern: `migrations/0100_taproot_refines_relation.sql`).
1a. **Repoint the ingest judge** — `taproot/canon.py::place()`'s
   `"contradicts"` branch and `taproot/hub.py`'s `new_contradicts`
   placement action must emit `disputes`, not `contradicts`. Highest
   value-to-risk item in this document; untouched by the original draft.
2. **Publish gates — three call sites, confirmed, not one.**
   `nanopub/gates.py::check_contradicts` and
   `precis_web/nanopub_render.py` both read `HubBundle.contradicts`, built
   by `taproot/seniority.py::_fetch_evidence_rows` filtered to
   `EVIDENCE_SRC_KINDS = {paper, patent, edgar, datasheet}`
   (`taproot/hub.py`) — hub- and finding-sourced disputes are invisible to
   both. `nanopub/overview.py`'s `disputed` bucket/`hub_rows` query reads
   `l.relation = 'contradicts'` **unfiltered** and is the only one of the
   three that sees `fi192706 → fi191316`. So today: the mechanical gate
   blocks paper/patent-sourced disputes only; a finding-sourced one
   surfaces in the overview and holds at human review, not at the gate —
   `fi191316`'s hold (`docs/backlog/nanobud-nanopub-batch3.md`) was Reto's
   call via the overview page, not `check_contradicts` firing. Reconcile
   the three queries when building; decide whether the gate *should* see
   hub/finding sources here — that's this item's actual open question now.
3. **A write door with invariants.** `taproot/hub.py::link_claims`'s
   `CLAIM_LINK_RELATIONS = {"refines", "conjunct-of"}` excludes both
   `disputes` and manual `contradicts`; the generic MCP `link()` door
   (`handlers/_link_tag_ops.py::apply_link_ops`) has none of
   `link_claims`'s guards (live endpoint check, no self-link, idempotent).
   Decide which door files a `disputes` edge and give it the guards — open.
4. **Render** — the claim page and `view='nanopub'` currently show an
   UNMINTABLE warning for a contradicts edge. `disputes` gets its own
   visibly non-blocking treatment ("open question", counterpart hub
   linked), never the red banner.

### Part 2 — review workflow (blocked by part 1)

5. **Second grounding mode** — `grounding.mode='claims'` so an adjudication
   hub, whose subject is two other hubs, can pass admissibility. Without
   it the adjudication tier is unmintable.
6. **Skills** — `precis-taproot-help` and `precis-nanopub-help` must
   actively *invite* `disputes`: filing one is free, expected, harms
   neither claim. Add the five adjudication verdicts. No queue/dashboard
   for browsing newly-filed `disputes` edges is scoped yet —
   `nanopub/overview.py` has a `withheld_count` queue for evidence edges;
   nothing analogous exists for `disputes`.
7. **Reviewer persona** — `precis-adversarial-reviewer` cannot simply be
   adapted: `scripts/review-paper/run.sh` runs it against a single `paper:`
   handle, and `precis-common-reviewer.md` makes it explicitly read-only.
   Hub review needs pairwise comparison and a write capability, neither of
   which it has. Of its 7 categories only `unsupported-claim` and
   `overgeneralisation` plausibly transfer to a claim-hub sentence. Budget
   for a new persona or a real extension, not a rename.

## First run

Run over the dense topic neighbourhoods first — conflicts hide where
coverage is thickest: MOF conduction, DNA bricks, molecular switches.

Two seed cases already in hand that no automated gate caught:

- fi191120 vs fi218681 — possible genuine contradiction
- pa1992 — GPa/TPa unit error, off by ~10³

Success looks like the `disputes` count growing into the hundreds. **A
large `disputes` graph is the deliverable, not a regression** — it's the
map of where inquiry should go, subject to the retrieval-floor caveat
above.

## Related

- `docs/backlog/claim-review-mechanism.md` — the procedure this plugs into
- `docs/backlog/nanopub-corpus-remediation.md` Phase 4 — the original
  observation and the verdict list
