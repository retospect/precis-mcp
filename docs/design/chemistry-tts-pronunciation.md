# Proposal: chemistry-aware narration (the audio "struggles with chemistry")

_Status: proposal (not yet implemented). Owner: audio-feed / narration
subsystem. See `docs/design/audio-feed.md`,
`docs/design/reading-prep-loop.md`, and the `precis-voice` skill._

## Problem

The daily casts (morning brief, evening meditation) and the news briefing
narrate cleanly for prose but mangle chemistry. A brief that discusses
`MoS₂`, `CO₂`, `NH₃`, `Pd(111)`, or a reaction like `NO → NH₃` comes out of
the TTS as letters-and-digits ("em oh ess two", "en aitch three"), silence,
or noise for the arrow. The active quest layer (e.g. quest 164903, "minimise
the rate-limiting barrier for NO→NH₃") is exactly the content that reads worst.

## Why it happens (grounded in the pipeline)

The narration path is `precis.draft.narrate` →
`precis.export.audio`/`precis.tts.render` → `precis.tts.kokoro` (Kokoro-onnx,
misaki G2P for zh/ja, espeak otherwise). Two stages decide what the ear gets,
and both drop or mishandle chemistry:

1. **`narrate.speakable()`** (`src/precis/draft/narrate.py::speakable`) is the
   markup-stripper. It:
   - **drops inline math entirely** — `_INLINE_MATH.sub("", t)` deletes `$…$`.
     A formula authored as `$\mathrm{MoS_2}$` is **silenced**, not spoken.
   - collapses display math `$$…$$` to the literal cue " equation, ".
   - strips `<sub>`/`<sup>` tags and bare `^` carets, and deletes runs of
     backslashes (so a leaked `\mathrm` reads as nothing).
   - leaves a bare `MoS₂` / `CO₂` (Unicode) or ASCII `CO2` untouched → handed
     to espeak, which voices it letter-by-letter-plus-digit.
   - leaves reaction arrows (`→`, `⇌`, `->`) untouched → espeak reads noise or
     nothing.

2. **The synth** (`KokoroSynth.synthesize`) does no chemistry normalisation; it
   phonemises whatever characters it receives. espeak has no notion that `NH3` is
   "ammonia" or that `₂` is a subscript "two".

The **one** existing lever is the pronunciation **lexicon**
(`narrate.apply_lexicon` + `resolve_lexicon` + `load_personal_lexicon`):
whole-word, case-insensitive, longest-first `{surface → respelling}`, layered
personal (`PRECIS_LEXICON_FILE`) under per-draft `meta.pronunciation`. It works
but is **manual and per-surface** — nobody has taught it chemistry, and it
can't reach formulae hidden inside `$…$` (those are already deleted before the
lexicon runs).

The `precis-voice` skill already tells the composing model to "write for the
ear — relationships not formulas, expand abbreviations". That is the cheapest
and best fix when it's followed, but (a) models don't always comply, and
(b) source titles / quoted findings the cast pulls in carry raw formulae the
model didn't author.

## Options

### A. Compose-time only (strengthen the voice skill)
Add an explicit chemistry section to `precis-voice`: never speak a formula,
spell the compound ("molybdenum disulfide"), read reactions as words
("nitric oxide converting to ammonia"), give oxidation states in words.

- **Pro:** zero code; the right place for judgement ("say the name a chemist
  would say aloud").
- **Con:** non-deterministic; does nothing for formulae in pulled-in source
  text; regresses silently when a model ignores it.

### B. A deterministic chemistry normaliser in `speakable()`
A pre-synth pass (`narrate.chemify` or similar) that rewrites common chemistry
notation to spoken words **before** the lexicon and synth:

- subscripts/superscripts → words: `₂`/`<sub>2</sub>`/`_2` → " two ";
  `²⁺` → " two plus"; charge `^-` → " minus".
- reaction/equilibrium arrows → words: `→`/`->` → " yields ";
  `⇌` → " in equilibrium with "; `+` between species → " plus ".
- a curated **built-in chemistry lexicon** of the highest-frequency
  compounds/ions/elements (CO₂→carbon dioxide, NH₃→ammonia, H₂O→water,
  H₂→hydrogen, O₂→oxygen, N₂→nitrogen, NO/NO₂/N₂O, CH₄→methane, MoS₂,
  Pd/Pt/Cu/Ni/… element names, common ions), shipped as a default layer
  **under** the personal and per-draft lexicons so a user can still override.
- surface-site notation `Pd(111)` → "palladium one one one surface" (or keep
  as an author-time concern — see open questions).

Crucially, this must also stop **silencing** chemistry: when a `$…$` span is
*all* chemistry (element symbols + digits + arrows, no real math operators),
route it through the normaliser instead of deleting it. A genuine equation
(`$k_\mathrm{obs}=2$`) still collapses to the " equation " cue.

- **Pro:** deterministic, testable (pure string pass, mirrors the existing
  `speakable` unit tests), covers pulled-in source text, no model dependency.
- **Con:** a general formula→name mapping is open-ended; we deliberately curate
  a high-frequency table and fall back to a "spoken tokens" reading
  (`MoS₂` → "M o S two") for the long tail rather than pretending to name
  everything. Risk of a false rewrite of a non-chemistry token (e.g. "CO" as a
  company) — mitigated by requiring formula-shaped context (adjacent digits /
  within a chem span) before naming, and by longest-first matching.

### C. Both, layered (recommended)
A is the primary lever (compose clean audio), B is the deterministic backstop
for what leaks through. This mirrors the LaTeX exporter's posture: the
composer should write well, but `export/latex.py` still deterministically
handles the Unicode/CJK/sub-sup the author left in, so a bad input never
produces broken output.

## Recommendation

Adopt **C**. Concretely, in dependency order:

1. **`precis-voice` chemistry section** (docs-only, ship first): the judgement
   layer, immediately effective for freshly composed casts.
2. **`narrate.chemify` normaliser + built-in chem lexicon** (`src/precis/draft/`
   + a data file), wired into `speakable()`/`speakable_markdown()` before
   `apply_lexicon`, with the sub/sup and arrow rules and the "chem-only `$…$`
   is spoken, not deleted" branch. Pure, unit-tested next to the existing
   `speakable`/`apply_lexicon` tests.
3. **Personal/per-draft override precedence** unchanged — the built-in table is
   the *lowest* layer, so a user's `meta.pronunciation` or `PRECIS_LEXICON_FILE`
   always wins.

No new top-level dependency (rules-based; no chemistry NLP lib). If the long
tail ever justifies real name lookup, `pubchempy`/a local formula→name table
would need an ADR per the no-new-dep rule — explicitly out of scope here.

## Open questions

- **Scope of the built-in table.** Start with the ~30 compounds/ions + the
  element names that actually appear in the catalysis/DFT corpus (the quest
  layer), or a broader general-chemistry set? (Lean: start narrow, grow from
  observed cast transcripts.)
- **Surface/site notation** (`Pd(111)`, `fcc(100)`): normalise in `chemify`, or
  leave to the composer (voice skill) since the spoken form is a judgement call?
- **Math-vs-chem discrimination** heuristic: what exactly counts as an
  "all-chemistry" `$…$` span safe to speak rather than drop? Proposed: contains
  ≥1 element symbol and no LaTeX math operator/relation macro; otherwise treat
  as an equation.
- **Numbers with units** (already partly handled for the ear?): confirm the
  normaliser doesn't fight any existing number/unit reading.

## Test plan (when implemented)

- `speakable("uptake of $\\mathrm{MoS_2}$ rose")` speaks "molybdenum
  disulfide", not silence.
- `chemify("NO → NH₃")` → "nitric oxide yields ammonia".
- sub/sup: `CO₂` / `CO2` / `CO<sub>2</sub>` all → "carbon dioxide"; `Ca²⁺` →
  "calcium two plus".
- override precedence: a `meta.pronunciation` entry for `MoS₂` beats the
  built-in table.
- a genuine equation `$k_\\mathrm{obs}=2$` still collapses to the " equation "
  cue (no chem misfire).
- non-chemistry false-positive guard: prose "the CO of the company" is not
  renamed to "carbon monoxide".
