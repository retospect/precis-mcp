# Status deck — working notes (v25, 2026-08-13)

Deck for Reto's boss, due Thursday 2026-08-13 (today). `slides.tex` = the
deliverable (44 frames, compiles clean). Paper draft (Digital Discovery) is
phase 2.

- v20→v21: fisheye sample box replaced with a real prod drill (honesty ledger).
- v21→v22: taproot split — "claims with receipts" (data model) + "trust at
  read time" (read side).
- v22→v23: two inventory slides added ("Inside précis: what the system holds"
  p6; "Inside Claude Code: the harness that writes it" p38); title tagline
  broadened past citations; nanobuds lesson added to the taproot data-model
  slide; ALL prod/git numbers refreshed to 2026-08-13.
- v23→v24: retraction downstream bullet \sidea→\sdone (search downrank +
  export gate both live); "six months" restored (repo history opens at
  v2.1.0); ANN spelled out as approximate nearest-neighbour on the
  one-substrate slide.
- v24→v25: dream example swapped me203573 → me205091 (in-realm + actionable);
  dream count 9,189 → 9,222; effort framing settled at FIVE months.

## Build

From this directory (persistent shell cwd is already here):

```sh
docker run --rm -v "$PWD":/work -w /work texlive/texlive:latest-medium \
  sh -c "pdflatex -interaction=nonstopmode slides.tex >/dev/null 2>&1; \
         pdflatex -interaction=nonstopmode slides.tex | tail -2"
grep '^!' slides.log            # error check (exit 1 when clean = fine)
# render-check one page:
docker run --rm -v "$PWD":/work -w /work texlive/texlive:latest-medium \
  gs -q -sDEVICE=png16m -r70 -dFirstPage=N -dLastPage=N -o check-N.png slides.pdf
```

Tectonic is broken (relay.fullyjustified.net down) — use the docker texlive
route. Gotcha: gs numbers `-o check-%d.png` from 1 regardless of -dFirstPage.

## Design (fixed, don't relitigate)

- Dark UL theme: ULGreen #00B140 / ULGreenBright #2BD877 on near-black #0E1412.
- Maturity dots: \sdone running · \srough built-rough · \sidea concept; footer legend.
- Per-slide `\citefield{}`; terse bullets; all claims grounded in repo code or
  live prod data ("no slop").
- UL·MACATAMO wordmark auto-replaced by `logos/ul-macatamo.png` when dropped in.
- TikZ gotcha: node style `step` collides with /tikz/step → named `stg`.
- Sample-box pattern: `\fcolorbox{MutedGray}{DarkBG}{\begin{minipage}{0.94\textwidth}\scriptsize\ttfamily …}`.

## Waiting on Reto

- Economics numbers: run `economics-probe-prompt.md` in a prod-access session,
  wire results into the own-silicon slide's \srough "Economics instrumentation"
  placeholder.
- Drop `logos/ul-macatamo.png`.
- Fill asks placeholder on the Next slide ("[hardware / time / student — fill in]").
- Review v24 (final pre-meeting read-through).

## Honesty ledger (things checked, don't regress)

- Retraction downstream wiring is \sdone as of 2026-08-13 (was \sidea):
  BOTH halves verified in code — search downrank
  (`handlers/_paper_search.py::_apply_retraction_downrank`) and the export
  citation-anchor gate (`export/retraction.py::BLOCKING_STATUSES`), landed
  together in 614d58cf (2026-08-11, deployed). Follow-up 605c09c8 (watch
  button walks a whole draft) is shipped but UNDEPLOYED — doesn't affect the
  slide claim.
- Injection scan: tier-0 deployed (shipped 2d8fb8ab, in deployed sha 1e3d6dce
  2026-08-11); email model-scored tier built but DARK (mail_poll OFF); papers/
  PDFs not scanned (backlog slices 2–4). Slide says exactly this.
- Fisheye sample on the fisheye slide is REAL (swapped in v21, 2026-08-12):
  prod paper pa47100 = Wang et al. 2026, Angew. Chem. Int. Ed.,
  DOI 10.1002/anie.202524612 ("Catalysis AI Agent … Cu-Based Single-Atom
  Alloy Catalysts for CO2 Electroreduction", 144 chunks). Quotes verbatim
  from chunks pc1618082/pc1618083 (= pa47100~17..18); muted periphery lines
  = real TOC keywords (~15,~16,~19); drill path (144 chunks → 3 clusters →
  ~15..20 → read 2) is the actual read sequence. Read-only verbs only.
- Dream slide example SWAPPED 2026-08-13 to **me205091** (was me203573).
  Reto: 203573 was "funky interesting but non-actionable" (its analogy partner
  was a DNA ternary-adder patent). me205091 stays firmly in NO catalysis on
  both sides: NrfA/ccNiR (cytochrome-c nitrite reductase) wins six-electron
  nitrite→NH3 selectivity by compartmentalising delivery — secluded pocket on
  a lysine-ligated heme + four hemes as electron relay [pa202554, pa3039] —
  which is the solved biological form of the hidden-μ_H critique its own
  earlier memories (me204664/me204665) aimed at the quest's Pd-slab Pareto.
  Actionable: argues for the confined-water / proton-shuttle overlayer as the
  synthetic analogue. Carries tier:synthetic-insight, so "survived its own
  review" stays true.
  ALTERNATES if Reto wants a different flavour (both verified, both in-realm):
  · me202765 — "all four NO→NH3 lanes share μ_H": route diversity is
    engineering-real but mechanism-illusory. Sharpest self-critique of the
    project's own rubric. tier:synthetic-insight ✓
  · me203287 — Pd@MoS₂ yields NH₂OH, not NH₃; the same Pd in Pd(111) yields
    NH₃, so structural form is the selectivity switch. Most concretely
    chemical + a two-stage cathode proposal. ⚠ NO tier:synthetic-insight tag,
    so drop "(survived its own review)" from the box if you use it.
- Live numbers, REFRESHED 2026-08-13 (deck-wide): 19,651 papers ingested /
  28,309 tracked / 2.6M chunks / 101 patents; gripes 391 filed all-time,
  275 in last 30d (242 of those already closed), **29 open now**;
  backlog 198 open; 143 skills; 19 agent roles; 78 tables / 121 migrations;
  1,989 commits / 22 weeks; DB 55 GB total, 24 GB indexes (chunk_embeddings
  33 GB); 65,312 active refs over 39 kinds; 49,403 typed links; 9,663
  memories (9,222 dreamt); 2,202 todos (120 open). Both repos GPL-3.0-or-later.
  ⚠ GRIPE COUNTING TRAP: closed gripes are SOFT-DELETED, so
  `WHERE kind='gripe' AND deleted_at IS NULL` = 61 live rows; "open" = the 29
  of those not STATUS:done/wontfix. Counting without the deleted_at filter
  gives a nonsense 379.
  ⚠ CHUNK COUNTING TRAP: raw `count(*) FROM chunks` = 2.76M includes 133k
  soft-deleted refs' orphans; live-substrate figure (join refs, deleted_at
  IS NULL) = 2.61M — that's the 2.6M on the deck.
- Effort framing: **five months** (Reto's call, 2026-08-13 — I briefly flipped
  it to six and back; final = FIVE). Slide title "Five months of effort" +
  "Five months of daily LLM-written commits". Repo span agrees: first commit
  2026-03-18 → 22 weeks ≈ 5.1 months, 1,989 commits. (Aside: history opens at
  v2.1.0, so some pre-repo work exists — not claimed on the deck.) Sparkline
  regenerated from git log (22 bars, W12–W33).
- Nanobuds lesson on the taproot data-model slide (2 \sidea bullets):
  evidence must come from the meat of a paper ("X doped with Y, measured Z"),
  section categorizer to strike prior-work/review sections from admissible
  sources; the incident (claim → hub → lit-review section whose own sources
  didn't support it) is stated as a found-the-hard-way bullet. NOT built —
  backlog candidate.
- Title tagline broadened 2026-08-13: was "No claim without a citation…"
  (too narrow — the deck now covers simulation + human grounding too), now
  "Grounded claims, not fluent guesses — every assertion traced to a
  paragraph, a calculation, or a measurement, and an untiring machine to
  keep checking."
- References slide DOIs verified: Shumailov 10.1038/s41586-024-07566-y ·
  Farquhar 10.1038/s41586-024-07421-0 · Gottweis 10.1038/s41586-026-10644-y ·
  Gerstgrasser arXiv:2404.01413 (COLM 2024).
- Agent-facing paper-references list is \srough — no MCP view lists a paper's
  own references yet (web-only); paper view='bibliography' is the INVERSE
  direction. Backlog-worthy.

## Open ideas filed on slides as \sidea (candidates for docs/backlog/)

- Fisheye: resolved cites as one-line summaries, not bare names.
- Corpus-wide PDF injection scan.
- **Evidence-section gating (nanobuds lesson)**: categorizer strikes
  prior-work / review sections from the admissible-evidence pool; require a
  results-section passage ("X doped with Y, measured Z"). Highest-value of
  the three — it closed a real false-verification incident.

## Phase 2 — the paper (after Thursday)

Living precis draft seeded from the deck. Agreed 8-section outline:
problem → bounded-context symmetry → literature grounding → simulation
grounding → human grounding → orchestration → case study → limitations.
Authoring via dev-DB precis (session prod MCP is READ-ONLY dogfood).
autocatpath JOSS companion after.

## Prod actions flagged for Reto (not deck)

- Promote want-papers pa172815, pa179601, pa58341, pa53085.
- Nudge discovery-layer over pa203165.
