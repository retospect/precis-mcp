---
status: in-progress
title: bring the 139 claim hubs behind the nanobud draft above board
prio: high
---

# Nanobud claim remediation (dr173020)

In-flight. Full phase plan + rationale:
`~/.claude/plans/greedy-gliding-anchor.md`. This file is the durable
resume pointer — the measured state and the decisions already made.

**Target is `dr173020`** ("WC:…", 180 body chunks, 139 claim hubs).
**Not `dr43020`** — that one has *zero* claim hubs (its 100 cites are
paper-level) despite a later `updated_at`; the taproot work is all on
173020.

## Measured state (2026-08-29, prod)

139 claim hubs cited; **108 (78%) clean on both axes**, 31 are not:

| problem | n |
|---|---|
| fail the approve-time blocking lint | 18 |
| live `contradicts` edge | 4 |
| zero verdicts (15 withheld-only, **2 with no evidence edges at all**) | 17 |
| overlap lint ∩ evidence | 8 |

**Those counts are raw query output; the sections below revise what they
mean.** Investigation on the same day found the 4 `contradicts` edges
contain no evidence conflict at all, and 1 of the 2 "no evidence" hubs is
a compound whose atoms *are* corroborated. Read the tallies as "rows the
query flagged", not "claims that are wrong".

Zero refuted. All 3 `signed` hubs in the whole corpus are nanobud hubs
and all 3 are clean. The draft also touches 42 non-hub chase findings —
no posture, no gate, invisible to every check above.

The 18 lint-failers:

```
189535 189536 189542 189543 190987 191014 191169 191260 191307
191318 192836 192855 211522 269443 269509 269510 269543 269548
```

(fi189542 is also disputed, so `reword.py::_COHORT_SQL` excludes it —
expect 17 to actually process.)

The 2 with no evidence edges at all: **fi211522** and **fi191014** —
both resolved under *Phase 3* below. Neither is the superlative-to-cut
this table originally implied.

## The ordering constraint

**Reword before verify.** `taproot/hub.py::refine_claim_sentence` never
touches `links.meta`; `nanopub/preflight.py::withheld_edges` compares
`meta.verified_claim_sha` against `claim_sha(live refs.title)` at *read*
time and withholds on mismatch. So rewording a hub stales every verdict
it already had. Verify-first pays for the same LLM work twice.

## The 4 disputed — none is an evidence conflict

Investigated 2026-08-29. **All four dissolve on inspection**; the
original read ("only fi189542 is real") was wrong.

Three are contradicted by **review findings against the draft's own
prose**, not by opposing evidence — their titles literally begin
`dc<chunk>: fi<hub>`:

- fi191315 &larr; fi255164 *"citation overstates beam-damage artifact as
  'engineering'"* (chunk dc2445944). Source attributes the transformation
  to beam-induced energy input, and the fullerene collapses entirely.
- fi191316 &larr; fi192706 *"claim-strength inflation — 'will ultimately
  require' vs source's 'could be used'"* (dc2445944).
- fi191329 &larr; fi255165 *"doesn't cover 'two distinct methods / CO
  disproportionation' clause"* (dc2445957). The clause **is** supported —
  by pc209495 in the same paper, currently uncited. Attach, don't cut.

Those are text fixes. See
`docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` for why
fixing the prose will *not* clear the hub's `disputed` posture on its own.

### fi189542's contradicts edge is simply wrong

Not a human judgement call after all — the two passages **agree**.

- Hub: nanocone opening angles "approximately 19°, 39°, 60°, 85°, 113°",
  corroborated by pc64732 (`sin(θ/2) = 1 − P/6`).
- Supposed contradictor pc972022 (pa5828) states `θ = 2·arcsin(1 − n/6)`.
  Evaluating n = 1…5 gives **112.9°, 83.6°, 60°, 38.9°, 19.2°** — the same
  discrete set, same relation, rearranged.

The edge (link_id 999192, `set_by='agent'`, 2026-08-14) carries meta
`{"source_handle": "pc972022"}` — **no recorded rationale**. Nothing
justifies it and the arithmetic refutes it.

Action: delete link_id 999192 (or re-point it as `corroborates`). This is
a prod mutation and needs the user. Deleting it also drops fi189542 from
the disputed cohort, so `reword-sweep` will then accept it.

## Phase 3 — the two "no evidence" hubs, resolved

**fi211522 is not an evidence gap.** It is a *compound* hub; all three
conjunct atoms (fi211519/20/21) are corroborated by pc42017 (Lee et al.
2008). Posture simply doesn't roll up — filed as
`docs/backlog/compound-hub-posture-ignores-conjunct-evidence.md`. Its
real defect is the malformed title: unclosed paren, no terminal period.
Reword only.

**fi191014 is a genuine gap, and the number looks wrong.** Claim: "The
original aerosol CVD process yields a broad distribution spanning
0.7–2 nm" (scope: fullerene size). It has zero evidence edges — its only
link is the draft's own `cites`. Searching the held corpus for the
fullerene-size distribution turns up the opposite shape:

- pc209507 (Nasibulin 2007, MALDI-TOF, *the* original aerosol CVD paper):
  main peaks are **C₆₀ and C₄₂** derivatives.
- pc110255 / pc110256 (pa1209, HR-TEM statistics): majority are C₄₂ and
  C₆₀ derivatives; C₆₀-sized buds most abundant at ~25%, with significant
  *smaller* populations.
- pc258863 fixes the scale: C₆₀ diameter = **0.72 nm**.

So 0.7 nm is the *modal* size, not a lower bound, and nothing in the
corpus reports 2 nm fullerenes — that upper figure looks like a
conflation with SWNT diameter (pc258863 gives SWNT distributions peaked
at 0.9 and 1.3 nm). Do **not** simply attach a supporter: the sentence
asserts a range the evidence contradicts. Correct the claim to the
C₄₂–C₆₀ distribution and attach pc209507, or cut the parenthetical from
dc2445891.

## Phase 6 recon — read the prose before editing it

Two surprises on reading the cited chunks verbatim (2026-08-29):

**fi191316's prose defect is already fixed.** fi192706 quotes the draft
as "will ultimately require"; dc2445944 now reads "**may** ultimately
require". Someone already softened it. Only the stale `disputed` posture
remains — exactly the failure mode
`contradicts-conflates-evidence-and-prose-misuse.md` describes. Do not
re-edit this sentence.

**The draft contradicts itself on the fullerene size range.** dc2445944
says "a broad **0.4–2 nm** size distribution [dc2445891]"; dc2445891
itself says "roughly **0.7–2 nm** [fi191014]". Neither figure is
supported (see Phase 3). One prose fix must settle both sites — and
dc2445944 cites a *draft chunk*, not a hub, so no claim gate ever looked
at it.

Still genuinely open in the prose:

- dc2445944 sentence 1 — the fi191315 beam-damage overstatement. Real,
  unfixed. Suggested: "…observed that electron-beam irradiation can
  transform an attached fullerene into a tube-like intermediate before it
  collapses, indicating nanobud geometry is not fixed after synthesis" —
  drops "engineered", names the mechanism, keeps the finding.
- dc2445957 — the fi191329 compound sentence. The uncovered clause is
  **supported** by pc209495 (same paper, uncited). Preferred fix is to
  mint/attach a hub for it and cite alongside, not to cut the clause.

## Decisions already taken

- Uncited-assertion hunt: **full adversarial pass** over the draft body
  (`quest/review_fanout.py::_FANOUT_ONLY_BRIEFS['adversarial']`, opus).
  No mechanical detector exists — every hygiene check is token-anchored
  and only inspects citations already present.
- A claim with no hub *and* no supporting passage in the held corpus:
  **soften or cut the prose**, don't go acquire. Report every cut.
- Reach `refine_claim_sentence` only through `reword-sweep` — the freeze
  guard lives in the cohort SQL, and the manual retitle door
  (`edit(kind='finding', title=…)`) has **no** freeze check at all.
- Attach evidence via MCP `put(kind='finding', supporters=[…])`, not
  `direct-mint --apply` — see
  `docs/backlog/direct-mint-apply-rerolls-the-reviewed-sentence.md`.

## Done 2026-08-30 (prod)

- **Deleted link_id 999192** (user-approved) — the unjustified
  `pc972022 --contradicts--> fi189542` edge. fi189542 now reads clean:
  corroborated by pc64732, no dispute. Disputed cohort 4 → 3.
- **dc2445891** — replaced the unsupported "0.7–2 nm [fi191014]" with the
  C₄₂/C₆₀/C₂₀ distribution, re-cited to **fi269510** (already corroborated
  by pc209498, same Nasibulin 2007 paper). *fi191014 was a duplicate of a
  claim the corpus already states correctly and with evidence* — that is
  why no reword or new supporter was warranted.
- **dc2445944** — "0.4–2 nm" → the same C₂₀–C₆₀ range, killing the draft's
  internal contradiction; and rewrote the fi191315 sentence to name the
  beam-induced mechanism and drop "engineered" (fi255164's fix).
- **fi272040 minted** — "Aerosol synthesis experiments show that NanoBuds
  can be selectively produced by two distinct one-step continuous
  methods…", grounded in pc209495. Sentence pre-validated against
  `lint_claim_sentence` + `lint_notation` locally: **zero blocking
  codes**. **dc2445957** now cites [fi191329] and [fi272040] separately,
  closing fi255165.

### Residual: fi191014 is now orphaned

It has **no links at all** — no evidence, no citation. It asserts a range
(0.7–2 nm) the corpus contradicts, and fi269510 supersedes it. It should
be retired; that is an unapproved prod write, so it is left standing.

### Lint-validation shortcut worth reusing

`lint_claim_sentence` / `lint_notation` are pure regex and import fine
locally — `uv run python -c` against a candidate sentence gives the exact
blocking set with no LLM and no prod round-trip. Note `_BLOCKING_LINT_CODES`
holds bare codes while the linters return `code: description`, so split on
`:` before intersecting or the check silently passes everything. Useful
tokens are narrow: `EPISTEMIC_MODE_TOKENS` has 66 entries and includes no
synthesis/CVD term, so a synthesis claim needs `experiments`/`analysis`/
`imaging` to satisfy `no-epistemic-mode`. `past-tense` is advisory;
`past-passive` is blocking.

## Phase 1 RESULT 2026-08-30 — the sweep is the wrong instrument here

Ran the full 18-hub dry sweep locally (see *Running it* below). Tally:

| status | n |
|---|---|
| `no-reword` | 11 |
| `rejected` | 3 |
| `reworded` | 4 |

**Only 1 of the 4 proposals was faithful.** Reviewed individually:

- **fi191307 — CORRUPTION, do not apply.** "Fixed" `hyphen-numeric-range`
  by swapping the ASCII hyphen for an en-dash: `B3LYP/6-311G(d,p)` →
  `B3LYP/6–311G(d,p)`. That is not a valid basis-set designation. It
  passes `_post_validate` because numerics survive and the lint clears —
  the validator cannot know a proper noun was mangled to satisfy a false
  positive. See
  `docs/backlog/hyphen-numeric-range-fires-on-pople-basis-sets.md`.
- **fi269510** — flattened "the size distribution also *suggesting the
  presence of* C₂₀" into a bare assertion, dropping the hedge.
  Rewritten by hand to keep it; applied.
- **fi269543** — silently dropped the whole trailing clause ("bond-to-ring
  binding likewise becoming less favourable as fullerene size
  increases"). Legitimate as single-assertion narrowing, but the dropped
  finding then has no hub. **Left alone**; wants splitting into two hubs.
- **fi269548** — faithful; applied verbatim.

### Why 11 refused, and why that is correct

Every `no-reword` reason is the same shape: *"No method or technique is
named in the sentence or scope; cannot assign an evidence verb without
inventing the epistemic mode."* The model is right to refuse.

The structural cause: `reword.py::propose_reword` is handed only
`(sentence, scope, lint_codes)`. But `no-epistemic-mode` — the dominant
failure, 14 of 18 — can only be fixed by naming the **method**, which
lives in the hub's *evidence chunk*, which the prompt never sees. **The
sweep is constitutionally unable to fix its own most common lint.** The 3
`rejected` rows are the model guessing anyway ("Structural and energetic
mapping", "via conversion") and failing, because invented phrases are not
in `EPISTEMIC_MODE_TOKENS` (66 entries, no synthesis/CVD term).

Fixing these properly = read each hub's corroborating passage, name the
actual technique, validate locally. That is what was done for fi272040.

fi191169 ("Canatu Oyj supplies carbon NanoBud™ films to major OEMs")
surfaced a different question: it is a supply-chain statement, not an
empirical claim, and arguably should not be a claim hub at all.

### `--apply` re-rolls — never use it to write a reviewed sentence

`_reword_one` calls `propose_fn` unconditionally *then* writes, so
`reword-sweep --apply` re-runs the MEDIUM proposal and may write a
different sentence than the dry run showed — the same trap as
`direct-mint-apply-rerolls-the-reviewed-sentence.md`. Apply reviewed text
through `refine_claim_sentence` directly (`/tmp/apply_reword.py` pattern:
read the DSN as `scripts/prod-precis` does, `Store.connect`, call the
door). Both writes returned `alias_kept=True`.

### Running it — `scripts/prod-precis`, not ssh

`scripts/prod-precis` reads the DSN from
`~/.secrets/pw/PRECIS_DATABASE_URL` in-process and runs `uv run precis`
**locally**, where `claude` is authenticated — so it sidesteps melchior's
keychain entirely. That file was absent on this workstation; populate it
from the `com.precis.web` plist (mode 600). **Sync the worktree first**:
a stale tree predating `c6c386a3` queries `refs.deleted_at`, which prod
no longer has.

## Phase 2 DONE for the two reworded hubs (2026-08-30)

`verify-edges --apply` re-stamped both after the reword invalidated their
`verified_claim_sha`. Both `supports: yes`, `contradicts: false`,
`action: stamped`.

- **fi269510** (link 1599156 ← pc209498) — the verdict text independently
  confirms the hedge was worth preserving: *"explicitly states the size
  distribution **suggests** C₂₀"*. The sweep's proposal had flattened
  that into a bare assertion; the hand-written sentence kept it.
- **fi269548** (link 1600554 ← pc35564) — right verdict, **wrong reason**.
  The chunk is OCR-corrupted (`V/ mm` for `V/µm`), so the verifier
  computed a threshold 1000× too small and reasoned *"0.001 V/µm, well
  below the claimed 1 V/µm"*. The claim is the faithful reading; the
  corrupted reason is now durable provenance on the edge. Filed as
  `docs/backlog/pdf-extraction-drops-micro-sign-in-units.md`.

## Hand-fix pass 2026-08-30 — 18 failing → 11

Fixed by naming the method from each hub's OWN evidence, written by hand,
`_post_validate`-checked locally, applied through `refine_claim_sentence`
(all `alias_kept=True`):

| hub | what changed |
|---|---|
| fi189536 | SCC-DFTB named (pc2412082). **Narrowed** — the old sentence asserted bilayer-film deposition and physical blending, which its adsorption evidence never supported. |
| fi211522 | compound: unclosed paren + missing period fixed, method (nanoindentation/AFM) pulled from its own conjunct atoms; `~` → `≈` on a notation advisory. |
| fi269443 | mass spectroscopy / infrared spectroscopy / X-ray diffraction named (pc3119725). |
| fi269510 | HR-TEM named, C₂₀ hedge preserved. |
| fi269548 | applied verbatim from the sweep (the one faithful proposal). |
| fi269543 | em-dashes stripped, fits the 250-char budget. |
| fi191014 | retired (orphaned, wrong figure, superseded by fi269510). |

### The premise that failed: evidence often does NOT name a method

The plan assumed every hub's corroborating passage names the technique,
so `no-epistemic-mode` could always be fixed faithfully. **Measured, that
holds for only some.** Of five checked:

- pc2412082 → SCC-DFTB ✓
- pc3119725 → mass spec / IR / electron + X-ray diffraction ✓
- **pc972025 (fi189543), pc404391 (fi190987), pc279174 (fi269509) → NO
  METHOD NAMED.** Each of those hubs has exactly one evidence edge, so
  there is no other passage to consult. The sources report procedures and
  results without naming an analytical technique.

For those three the lint is **unsatisfiable without inventing a method** —
precisely the false attribution `_ARTIFACT_LINT_EXEMPTIONS` cites as its
reason for exempting `hypothesis`.

**Sampling trap:** fi269443 first looked method-less because only one of
its four evidence chunks was read, and that one was procedural. Read
*every* evidence chunk before concluding a hub has no method.

## Remaining 11, by why they fail

1. **No method in their own evidence** — fi189543, fi190987, fi269509.
   Cannot be fixed by rewording. Needs the artifact-exemption route or a
   `repair-evidence` pass that attaches a passage naming the technique.
2. **Not empirical claims at all** — fi189535 (definition), fi191169 and
   fi191260 (commercial/supply-chain), fi192855 (vague generalization).
   Wrong *shape* for an empirical-claim lint.
3. **Needs an evidence read before deciding** — fi189542, fi191318,
   fi192836.
4. **Detector bug** — fi191307. FIXED 2026-08-31: the
   `hyphen-numeric-range` regex now carries a fourth guard for Pople
   basis sets, verified by a 119,279-sentence corpus dry run.

## The artifact-exemption route is bigger than one line

`resolve_artifact_type` returns a closed set of three
(`claim | compound | hypothesis`); `claim`/`compound` derive from edges,
`hypothesis` from the mint payload. A new `definition`/`context` type
needs: the exemption entry, `resolve_artifact_type` support, a persisted
marker, `reword.py::_blocking_codes` to stop hardcoding
`artifact_type="claim"`, and a cohort-SQL exclusion beside
`_NOT_HYPOTHESIS_SQL`.

The gates docstring also sets a bar: `compound` was deliberately left
strict "pending a decision, because ... the failure mode of exempting it
has not been measured against the corpus." A new type inherits that bar —
measure first, then ship. It is its own change, not part of this pass.

## Phase 2 owed again

fi189536, fi211522, fi269443 and fi269543 were reworded after their last
verification, so their `verified_claim_sha` is stale. `verify-edges`
those four. (fi269510/fi269548 were re-stamped already.)

## RESUME HERE (2026-08-31, second pass)

State: **18 blocking-lint failures → 4**; 12 hubs fixed, fi191014 retired,
fi272040 minted.

The four that remain are exactly the non-empirical bucket — fi189535
(definition), fi191169 and fi191260 (commercial/supply-chain), fi192855
(vague generalization). No reword can fix them honestly, because the
lint asks for an epistemic mode a non-empirical sentence does not have.
They are step 4 below.

**The lint axis is no longer the blocker. Two data/code defects are.**

**1. ~~Ship the `hyphen-numeric-range` detector fix~~ — DONE 2026-08-31.**
The guard is `(?![3-6]-\d{2,3}G(?![A-Za-z]))`, matching the whole Pople
shape rather than the trailing `G`: the corpus dry run (119,279
sentences) showed a bare-`G` guard also swallowed a real `24-25G` memory
range. Two rows changed verdict, both fi191307's. The same regex backs
`normalize_notation`, so this also stopped the canon rewriter silently
corrupting `6-311G` to `6–311G`.

**2. ~~Read the evidence~~ / ~~try repair-evidence before exemption~~ —
DONE 2026-08-31. All six were fixable, none needed an exemption.**
The premise held: every source paper named a way of knowing somewhere,
even when the *pinned* passage did not. Sourced by reading the pinned
chunk plus a keyword sweep over the same paper's other chunks:

| hub | mode found | where |
|---|---|---|
| fi189542 | cone-wall **modelling** | pinned chunk itself |
| fi189543 | Euler's-rule **analysis** | pinned chunk itself |
| fi190987 | **molecular dynamics** simulations (AI-REBO) | pc404380/86/87 |
| fi191318 | **density functional theory** | pc379916 |
| fi192836 | transmission/sheet-resistivity **measurements** | pc172364/67 |
| fi269509 | **TEM** ("Philips CM200 FEG") | pc279168 |

Two lessons worth keeping. First, `EPISTEMIC_MODE_TOKENS` accepts
generic heads (`analysis`, `calculations`, `measurements`, `modelling`,
`imaging`), so a claim whose paper is theoretical or industrial can
still be attributed honestly without naming an instrument that was
never used. Second, `report` is **not** an accepted evidence verb —
`show` is; that cost one dry-run round trip.

Three sentences deliberately dropped an unsupported trailing clause
(fi190987's mechanical-behaviour claim, fi192836's touch-sensor
application, fi191318's "tunable electronic character"). Each is a
separate mint job, listed below — the old sentences were multi-assertion,
so splitting is the correct shape, not a loss.

**4. Then the artifact-type change for the four non-empirical hubs**
(fi189535 definition, fi191169/fi191260 commercial, fi192855
generalization) — with the corpus measurement the gates docstring
demands. Its own change, its own `/go`. Design sketch above.

### Mint jobs these passes created

From the 2026-08-31 second pass, each a real finding dropped from a
multi-assertion sentence:

- **fi190987** — the mechanical behaviour of the C₆₀-bombardment hybrid
  nanostructures (paper 3479 examines it; the pinned passage does not).
- **fi192836** — Carbon NanoBud films applied in projected-capacitive
  touch sensors with low haze and reflectivity (paper 1771 §3.1).
- **fi191318** — the interpretive step from size-dependent binding
  energies to a *tunable* electronic character. Check whether the draft
  prose leans on this before minting; it may be an assertion to soften
  rather than a claim to support.

From the first pass:

- **fi269543's dropped clause** ("bond-to-ring binding likewise becoming
  less favourable as fullerene size increases") has no hub. It was a real
  finding in the old multi-assertion sentence.
- **fi269443 should split.** Naming three analytical techniques *and* the
  synthesis parameters in one sentence means no single passage supports
  all of it: `verify-edges` returned `partial` on pc3119725 and pc3119715
  and **`no` on pc3119709**. Two hubs (synthesis route; identification
  methods) would each verify cleanly. Lesson: a claim sentence should
  name the method(s) its *own pinned passage* carries.

### BLOCKER 1 — 186 hubs cannot be re-verified at all

`verify-edges` has no cohort for an edge that carries a `verified_by`
but **no** `verified_claim_sha`, so it reports a bare `0 edge(s)
processed`. Corpus-wide that is **311 edges across 186 live claim
hubs**, permanently withheld with no CLI path to re-stamp them.

Six of the seven nanobud hubs re-checked today are in it, including all
five whose sentences I just reworded — so those rewords cannot be
re-verified until this ships. Only fi269509 was reachable, and it came
back `supports: yes`, stamped.

FIXED 2026-08-31. `_UNVERIFIED_STAMPED_CLAUSE` now reads "a `support`
value this sweep cannot stand behind" — no `verified_by` **or** no
`verified_claim_sha`:

```sql
AND l.meta->>'support' IS NOT NULL
AND NOT (l.meta ? 'verified_by' AND l.meta ? 'verified_claim_sha')
```

They belong in that cohort rather than the default one because
strip-on-non-corroboration is the correct write: a live `support` value
is asserting something, and if it no longer holds it must come off.
Backfilling the sha instead would assert that a verdict on an unknown
earlier sentence applies to today's — the exact staleness the sha exists
to catch.

`taproot/authoring.py` *does* stamp `verified_claim_sha` on its
mint-time trio, so this is a historical cohort, not an ongoing leak — no
backfill guard needed once these 311 are re-verified.

Re-verified 2026-08-31 immediately after the fix, all six reachable:

| hub | verdict | note |
|---|---|---|
| fi189542 | `yes` | stamped |
| fi190987 | `yes` | energies match the figure caption exactly |
| fi191318 | `yes` | stamped |
| fi192836 | `yes` | verbatim numeric match |
| fi269509 | `yes` | stamped (reached before the fix) |
| fi189543 | `partial` | paper says θ≈20°, the sentence says 19° |
| **fi189536** | **`no`** | **support STRIPPED** |

### fi189536 overreaches its passage — open

The sentence asserts that C₆₀ adsorbs *noncovalently* onto graphene
"without site-specific covalent attachment". The verifier: pc2412082
"describes the SCC-DFTB study of C₆₀-graphene adsorption and measurement
objectives but does not state the bonding nature". The support was
stripped, so the hub is back behind the publish gate — correctly.

This is a claim written to satisfy a *lint*, not to match its evidence.
It is lint-clean and unsupported, which is the worse failure of the two.
Fix by narrowing to what pc2412082 does state, or by attaching a passage
from paper 170574 that actually reports the bonding character. Do not
re-stamp it by hand.

The θ≈20°/19° gap on fi189543 is smaller but the same species: 19° came
from fi189542's paper (783), not from fi189543's own (5828). A claim
sentence should carry the number its *own* pinned passage carries.

(This also subsumes the old "fi189536 is born-stamped" note — that
diagnosis was wrong. The edge is sha-less, not born-stamped.)

### BLOCKER 2 — fi269509 cites the wrong paper

`ref 2615` binds an aerosol-CVD NanoBud paper's chunks to a 2022
mining-engineering DOI, and carries two different `pdf_sha256` values.
fi269509 is lint-clean and verified `yes`, but publishing it would emit
a false citation. Spec: `docs/backlog/ref-2615-is-a-mis-bound-record.md`.

## Grounding checks shipped 2026-09-01 (`taproot/reword.py`)

The reword path validated the proposal against the *previous sentence*
only (`_post_validate(old, new)`) — self-consistency, never grounding.
Two checks now read the hub's own pinned passages:

* **numeric grounding**, blocking — a quantity-shaped digit run absent
  from every pinned passage rejects the reword.
* **mode grounding**, advisory (`HubReword.warnings`, `warned=` in the
  CLI line) — an epistemic-mode token the passages never name.

Measured on this cohort before shipping. Numeric grounding fires on
exactly one of the seven reworded hubs — **fi189543's `19`**, the number
carried over from fi189542's paper — and on none of the other six.

Mode grounding warns on four, and every warning is the same real gap:
**the pinned passage does not carry the method the sentence names.**

| hub | claims | its pinned passage |
|---|---|---|
| fi190987 | molecular dynamics simulations | a figure caption; names only ion-beam *imaging* as content |
| fi191318 | density functional theory calculations | binding-energy trend prose, no method |
| fi192836 | measurements | the paper's "Impact" section, no method |
| fi269509 | transmission electron microscopy | reactor-temperature prose, no method |

**These four are mint jobs, not text jobs:** attach the paper's methods
passage as a second evidence edge. Both `refine_claim_sentence` and the
sweep leave `links` untouched, so nothing here has degraded — the
warning names work that was always owed.

Corpus dry run over all 1,230 live strict claim hubs with a pinned
passage: numeric grounding would block **10.8%**. Eight of those were
eyeballed; six were true (the number is genuinely not in the passage)
and the two false ones drove canon fixes now in the code — scientific
notation (`10¹¹` vs a passage's `100,000,000,000`) and decimal precision
(`0.7` vs `0.70`, `1.3` vs `1.33`). Mode grounding would warn on 45%,
which is why it is advisory: promote it only after someone measures
whether that rate is real debt or noise.

### Also open
- Phase 5 (adversarial pass for uncited assertions) never started.
- `docs/backlog/pdf-extraction-drops-micro-sign-in-units.md` and
  `compound-hub-posture-ignores-conjunct-evidence.md` are unshipped.

## Blocker — `claude` on melchior is logged out

The 18-hub `reword-sweep --dry-run` **ran** on 2026-08-30 (the classifier
let it through once the user asked for it directly). The cohort logic was
confirmed: 17 hubs processed, fi189542 excluded as predicted. But **every
one returned `status: "llm-failed"`**, zero rewords proposed:

```
rung 0 (cloud, claude-haiku-4-5) failed: claude -p exited 1 ... "Not logged in - Please run /login"
rung 1 (cloud, claude-haiku-4-5) failed: claude -p (agent) exited 1 ... (terminal_reason=api_error)
taproot reword-sweep: 1 hub(s) processed -- llm-failed=1, applied=0
```

Both configured rungs are `claude -p`, so there is **no non-Claude
fallback** — the whole sweep fails closed. This is auto-memory
`live-model-tests-need-host-claude` biting the reword path.

**It is NOT a bad or expired API key** (checked 2026-08-30):

- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`
  are **unset** on melchior, and neither `com.precis.worker` nor
  `com.precis.web` sets any Anthropic/OpenRouter key in
  `EnvironmentVariables` — so there is no long-lived key to have gone bad.
  Auth is OAuth, and on macOS Claude Code keeps it in the **login
  keychain**.
- In a non-interactive SSH session `security list-keychains` returns only
  `/Library/Keychains/System.keychain` — the login keychain is never
  unlocked, so `claude` cannot see its credentials and says "Not logged
  in". `~/.claude/.credentials.json` does not exist (that is the
  Linux/file-backed path, not macOS).
- The credential itself is **fine**: `llm_call_log` shows **1,586
  successful `claude_p` calls in the last 24 h** (plus 137,951
  `openai_compat`, 36 `claude_agent`). MEDIUM tier is 439/439 successful
  over 3 days, zero errors.

So the failure is scoped to *headless SSH invocation*, not to the account.
`source='taproot:reword'` has **zero** rows in 3 days — the failed calls
never reached the log, so this outage is invisible to the usage ledger.

Unblock, in preference order:

1. **Give MEDIUM a non-Claude rung.** `_default_chain(MEDIUM)` is two
   `claude_p` rungs, so a host without keychain access fails closed with
   no degradation. `live_config.chain_override` /
   `llm.chain.medium` already supports an `openai_compat` rung, and
   `PRECIS_LLM_BASE_URL` is configured on every node. Read
   `router.py`'s tool-filter warning first — that override is what sent
   agentic planner ticks to a tool-less wire for days. Reword is a
   one-shot JSON call with no tools, so it is a safe consumer.
2. **Run the sweep where `claude` is authenticated** — a GUI-attached
   session on melchior, or dispatch it as a worker job (the daemons
   evidently have a working context). Plain `ssh melchior …` will
   **not** work, including for a human: an interactive SSH login does
   not unlock the macOS login keychain either.
3. **Run it from reto's workstation against the prod DSN.** `claude -p`
   succeeds there and pgbouncer `100.126.127.107:6432` is directly
   reachable. Blocked for the agent only because the worktree guard
   refuses the nested `ssh …` command substitution needed to fetch the
   DSN, and there is no local `~/.pgpass` entry for `precis_prod`.

Open question: whether those 1,586 `claude_p` successes ran on melchior
or on another node — if melchior's own daemons are succeeding as user
`deploy` without a GUI session, there is a working headless path worth
copying.

The dry run was not wasted: it returned all 17 `old` sentences with exact
lint codes. Dominant failures are `no-evidence-verb` + `no-epistemic-mode`
(the sentences state a result without naming how it was established).
Several are trivially hand-fixable (`no-terminal-period` on fi190987,
fi191260, fi211522; `hyphen-numeric-range` on fi191307).
