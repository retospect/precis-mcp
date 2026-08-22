---
status: draft
title: "ingest silently strips Greek glyphs from some PDFs — μm becomes mm, and the claim looks wrong instead of the source"
---

# The source text can be the thing that's broken

`pa494` — *A comparative study of field emission from NanoBuds, nanographite and
pure or N-doped single-wall carbon nanotubes*, phys. status solidi (b), 2010 —
contains **zero** micro signs (U+03BC, U+00B5) and **zero** Greek letters
(α-ω, Α-Ω) anywhere in its chunks, while `° ± × – — Å` survive intact (7
occurrences). The Greek range specifically did not make it through extraction.

The visible damage is a unit:

> "Threshold fields corresponding to the current density 10 mA/cm² for these
> films are in the range of 1–2 V/ **mm**."

The paper means V/μm. Three independent confirmations:

1. **The paper says so in prose**, in another chunk: *"threshold field values of
   few Volts per micron."*
2. **Physics.** 1–2 V/μm is the ordinary threshold for CNT/nanographene field
   emitters. 1–2 V/mm would beat every emitter ever reported by a thousandfold.
3. **The stray space.** The corrupt form is `V/ mm`, not `V/mm` — the glyph was
   dropped, and the space it left behind is the scar. Corpus-wide, `V/μm`
   appears 82 times against 25 stripped forms, so the corpus overwhelmingly
   knows the correct unit; this paper's PDF did not survive extraction.

Note the same paper's *legitimate* millimetre measurements — "0.1 mm thick"
membrane, "100 mm radius" anode tip, "100 mm inter-electrode distance" — are
physically sensible and untouched. The corruption is not "all mm are suspect";
it is "μ vanished, and where it vanished before an `m` the unit silently became
a different, valid-looking unit."

## Why this is worse than a wrong claim

It **inverts the repair**. A grounding audit reading passage against sentence
sees a claim asserting μm where the passage says mm and concludes the claim is
wrong by 1000×. That is exactly what happened (`fi192819`, in
`nanobud-grounding-audit-2026-08-20.md`), and acting on it would have edited a
*correct* claim into an incorrect one and re-hashed its `pub_id` to match.

Every gate passes: the evidence edge is real, the source is primary, the quote
verifies verbatim against the chunk. The chunk is just wrong. Nothing in the
pipeline asks whether the *source text* survived ingest.

A unit that silently changes to another valid unit of the same dimension is the
worst possible failure mode — µs→ms, µA→mA, µg→mg all have the same shape.
Greek variable names (α, β, λ, θ) vanishing outright is more visible but also
more likely to be silently paraphrased away at mint.

## Detect — and why the obvious query does not work

The obvious detector is "papers with zero Greek characters but some other
non-ASCII". **It was run 2026-08-20 and it is useless**: 3,750 of 15,917
papers/patents with ≥20 chunks match, ~24% of the corpus. The list is dominated
by papers that legitimately contain no Greek — Malthus's *Essay on the Principle
of Population*, Searle's *Minds, brains, and programs*, *Trading to Win: The
Psychology of Mastering the Markets*. Absence of Greek is normal. Recorded here
so nobody re-runs it and mistakes the base rate for a finding.

The sharpening pass fails the same way. 553 of those refs spell a Greek word
while containing no Greek glyph, but "alpha synuclein", "tumor necrosis factor
alpha" and "big theta" are how those terms are conventionally written — not
scars.

Likewise the unit-frequency scan: 17,975 chunks in the zero-Greek set contain
`N mg`, 9,404 contain `N mm`. Those are overwhelmingly real milligrams and
millimetres. Counting them as "μ→m candidates" is a base-rate error, not
evidence. (`V/ mm` is narrow enough to be meaningful — only 12 corpus-wide — but
the spacing tell is not reliable either: `pa494` carries both `V/ mm` and the
closed `V/mm`.)

**What actually caught `pa494` was context, not character inventory:** a unit
whose value is physically implausible, contradicted by the same paper spelling
the unit in prose. That does not generalise into a corpus-wide SQL sweep.

So detection belongs **at ingest, not in the text after the fact**. The
extraction step knows things the stored text has already lost: whether the PDF
declares a Symbol or non-Unicode-encoded font, and whether glyphs were dropped
rather than mapped. A per-document extraction-health record written at ingest
time is a real detector; a regex over the result is not.

Any figure here is derived at use — none of it is stored.

## Root cause, settled 2026-08-20 — the PDFs lie, and re-ingest does NOT help

Both PDFs were re-extracted locally with both current extractors (PyMuPDF and
pypdfium2, Marker's backend). **Greek does not come back. Zero Greek/micro
codepoints in either document, either extractor** — byte-identical to what is
stored. There is no extractor-version regression: `git log -S` on the relevant
line returns one commit (`3bdc3018`, the module vendoring) predating all the
data. Do not spend GPU on a re-ingest pass; it returns the same bytes.

The source PDFs misdeclare their own encoding — Elsevier/Advent-3B2 `Adv*`
subset fonts — in **two distinct ways**:

**(a) Lying `ToUnicode` — `pa494`.** The μ is a single-character span in its own
font `KKLGAD+AdvP7DA6`, whose `/Differences` names the glyph `/m` and whose
`/ToUnicode` stream reads `<6d> <006D>`. The PDF *asserts* its μ is U+006D. No
conformant extractor can do better; our code never sees a μ to lose. Output is
clean ASCII `V/mm` with **no residue whatsoever**. This is the majority mode and
the hard one.

**(b) No `ToUnicode` + our own strip — `pa47024`.** Font `IBDHKG+AdvP4C4E51` has
no `ToUnicode` and a meaningless subset glyph name (`/C22`), so extractors fall
back to the raw code and emit **U+0002**. ftfy correctly leaves it alone. Then
`src/precis/ingest/marker.py:163` —
`re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)` — deletes it. Verified by
running the exact config on real extractor output: `6 \x02m` → `6 m`,
byte-identical to prod. **That line is where a detectable corruption marker
becomes an undetectable deletion.**

The `≤` → `G` degradation is a third font (`IBEEBP+AdvP0004`) declaring
`/WinAnsiEncoding` and `Flags=34` (*Nonsymbolic*) while being a symbol font —
i.e. the fonts lie in the flags too, so a flags-based detector is a supporting
signal only.

**The glyphs are physically present.** Rendered at 500 dpi, `pa47024`'s line
reads `7.3 μm/s`. Only the text layer is poisoned. Marker never OCRs these
because the text layer *looks* healthy — pure ASCII, no U+FFFD. That is why this
was silent for years.

## Not a bounded cohort — it is ongoing

Papers >5 kB with zero Greek but other non-ASCII present, by ingest month
(**old detector — inflated ~28% by LaTeX-escaped papers; the trend is sound, the
levels are not**): 2026-05 → 73/123 (59%), 2026-06 → 2480/8209 (30%),
2026-07 → 1534/5760 (27%), 2026-08 → 1072/3998 (27%). The `≤`→`G` probe
corroborates in every month. Flat
rate, no step change, no correlation with any code change — it tracks the share
of the corpus published with Advent-3B2 symbol fonts, a property of publishers.
**Every new ingest of such a PDF is still corrupted today.**

## Masking risk — HIGH, and it decides the fix order

The tempting fix is to relax `marker.py:163` to preserve or substitute U+0002.
That would visibly improve `pa47024` and leave `pa494` **completely untouched
and still wrong** — with no residue, no marker, nothing to find — now behind a
commit that reads "fixed the Greek bug". Mode (a) is the majority.

**Therefore: never ship the control-char fix without the mode-(a) detector in the
same change.** The regression test that enforces this is the `pa494`-shaped one:
a document with a lying `ToUnicode` and zero control-char residue must still be
flagged. A control-char-only patch fails it.

## Fix order

1. **Detect at write time, from the FONTS.** A per-document `glyph_health`
   record on the paper's `meta`, written during extraction. The two exact
   signatures, both deterministic `fitz` tests with no base-rate problem:
   - **mode (a)** — a `/ToUnicode` that maps a code to the same character its
     glyph is *named* (`<6d>` → `<006D>` on a glyph named `m` that draws μ).
     Self-referential lie.
   - **mode (b)** — a font with **no `/ToUnicode` and no `/Encoding`**, whose
     glyphs carry positional names (`C22`).

   Plus the cheap supporting counts: pre-strip C0 control-char count,
   orphan-single-char-span count (a lone span whose font differs from both line
   neighbours — 100% precise on both samples), Greek-codepoint count. All
   obtainable inside `_marker_extract` / `_fitz_fallback` with no extra pass.

   **This is the finding that inverts the earlier "mode (a) is undetectable"
   conclusion.** Mode (a) is undetectable *in the text* — it produces clean
   ASCII with no residue — but it is trivially detectable *in the font*. Every
   text-level detector tried so far has been a base-rate failure (see §Detect
   and `grounding-verification-rubric.md`); the font-level test is not a
   heuristic at all.
2. **Stop destroying evidence** at `marker.py:163` — count and record before
   deleting. Ship only with (1).
3. **Recovery is not OCR.** See the ruling below — tested and falsified.

Highest-value near-zero-cost signal, worth having regardless: **prose contains
`micron|micrometer|nanometer` while the text contains no `μ`.** `pa494` says
"few Volts per micron" three sentences from `V/mm`.

## Ruling on recovery — OCR is ruled OUT, tested 2026-08-20

An earlier version of this ruling rationed OCR to on-demand use on the
assumption it would work but cost too much. **Both halves were wrong.** OCR was
tested directly on both scarred PDFs with `ocrmypdf 17.10.0` / `tesseract
5.5.3`:

| | stored extraction | `--force-ocr -l eng` |
|---|---|---|
| `pa47024` p5 | `7.3 \x02m/s`, `6 \x02m` | `7.3 pum/s`, `6 wm` |
| `pa494` p2 | `1–2 V/mm`, `1 V/mm` | `1-2 V/wzm`, **`1 V/m`** |

Zero Greek/micro codepoints in either output; `pa494` **regressed**, losing a
character outright.

**The reason is structural, not tuning.** `eng.traineddata`'s LSTM unicharset
holds 112 symbols whose entire non-ASCII inventory is
`’ € ™ “ — ” » ® £ « ° © § ¥ ¢ ‘ é`. **Greek and U+00B5 are not in the output
alphabet.** tesseract-eng cannot emit `μ` at any confidence, on any page, at any
dpi. Adding `-l eng+ell` as a steelman produced `Ὑ/μπι` — one correct μ wrapped
in two new errors — and did nothing for `pa47024`. Erratic, not a fix.

**Forcing OCR is worse than useless — it is destructive.** After `--force-ocr`
the only font left is `MPDFAA+NotoSans`: the publisher's vector text layer is
rasterized and gone. That layer's glyph outlines are the **only surviving ground
truth** in either file. It would also erase the `\x02` residue — mode (b)'s one
visible marker — replacing it with plausible ASCII (`pum`, `wm`) that no
heuristic will ever flag, while leaving mode (a) equally wrong. The corpus would
scan clean and be *less* recoverable than before.

**Do not route this to `--force-ocr` on the strength of "the infra already
exists."** It does not exist, and it would not help. See below.

### The corpus was never OCR'd at all

Believed otherwise at the time. Checked, and the belief does not survive: no
`GlyphLessFont` on any page of either PDF, no page using render mode `3 Tr`
(tesseract's invisible-text layer), no `ocrmypdf`/`tesseract`/`ABBYY`/`Acrobat
Capture` string in either XMP. Producers are `Acrobat Distiller` +
`3B2 Total Publishing System 8.07e/W` and `John Wiley` / `PDFlib PLOP` — the
iText stamps are DOI-metadata passes. A repo-wide grep for
`ocrmypdf|tesseract|force_ocr|skip_text|redo_ocr` returns nothing outside surya
memory caps in deploy templates, and the NAS `ingest.log` has no OCR pass (every
`ocr` match is incidental — `senocrate`, `endocrine`, `nanocrack`).

`marker.py` constructs `PdfConverter(artifact_dict=create_model_dict())` with
**no config dict**, so Marker runs stock: `force_ocr=False`,
`strip_existing_ocr=False`. It trusts any page whose text layer looks good — and
these look perfect, being pure ASCII.

Worth keeping: the *mechanism* behind the "pre-OCR would have skipped these"
hypothesis is real. `ocrmypdf` refuses these files by default with
`PriorOcrFoundError: page already has text! - aborting`. So a `--skip-text` pass
elsewhere in the stack **would** silently skip exactly the poisoned pages and
report success. If a pre-OCR pass is ever found to have run on some other
subset, assume it skipped the scars.

Recovery, if pursued, is **glyph-outline fingerprinting** (hash the CFF
charstring, match against known μ outlines — deterministic and lossless, but
per-font-family work) or a **VLM pass over rendered regions** (general, but
probabilistic, needs a confidence gate). Choosing between them, and whether
repair runs at ingest or as a backfill, is an open architecture call. Either way
body chunks are append-only (`ord >= 0`), so replacement is DELETE + INSERT
through a registered synthesis pass with the embedding/summary cascade re-run.

`gr228652` carries the root cause; `gr228594` the detector gap; `gr228699` the
OCR falsification.

## Interim guard, worth having regardless

A grounding check that flags **claim contains a Greek character AND its
grounding chunk's paper contains none** as *scar suspected*, not
*contradiction*. It costs one aggregate per source paper, and it is the
difference between the audit above filing a false contradiction and filing an
ingest bug. Wherever the re-verification of attached evidence lands — the
`hub_refine` machinery already has `_verify_support_with_caveats` making this
class of judgment for *candidate* evidence — this check belongs in front of it.

## Urgency: low, and measured

The question that sets urgency — does a scarred passage ground anything already
signed — was answered 2026-08-20 against prod. Taking the (over-broad) zero-Greek
set as a deliberate upper bound, **572** strict claim hubs are grounded in one of
those papers: 516 with no `nanopub_publish` row, **56 `candidate`**, and **zero
`reviewed`, `signed` or `published`**. Nothing signed rests on a scarred passage,
and the over-broad denominator means the true figure is smaller still. This is a
correctness bug to fix before publication, not an incident.

## Unknowns

~~Whether the scar is a marker/extractor setting or per-PDF font encoding.~~
**Settled:** per-PDF font encoding, in the two modes above. Re-ingest does not
help; neither does OCR.

A cheap deterministic repair is also ruled out for mode (a): the lying font's
glyph **name is also `m`**, so the CFF charset lies in the same direction as the
`/ToUnicode`. There is no name-level information to recover from — only the
vector outline survives.

How many papers are *genuinely* affected is unknown and, per the section above,
not answerable by querying the stored text; it needs the extraction step to
report. Whether the scar correlates with publisher or vintage is likewise open.

## Second confirmed scar — `pa47024`, and it is 10⁶×

Found 2026-08-20 while scoping `dr42995`. *"Biomedical Applications of Untethered
Mobile Milli/Microrobots"* (2015) has lost **every** `μ`, and because the unit is
bare metres rather than millimetres the error is a **million-fold**, not a
thousand:

> "all dimensions less than 1 mm and larger than **1 m**"
> "vessels smaller than arterioles (**G 150 m**)" — 150 μm, and `≤` degraded to `G`
> "microparticles with **6 m** diameter was up to **7.3 m/s**"
> "bubble height is approximately **35 m** for a cavity radius of **75 m**"
> "*E. coli* is **30 m** s⁻¹"

Note "1 mm and larger than 1 m" is *internally* incoherent — the scar is visible
without any outside knowledge. It grounds `fi176409`, a qualitative claim, so it
probably did not propagate this time; the source is nonetheless poisoned for any
future quantitative extraction.

Two confirmed instances with different magnitudes (10³ and 10⁶) establish this as
a **class**, not an anomaly. It is no longer a curiosity to schedule; it is a
precondition for minting from any exposed source.

## A detector that actually works

The zero-Greek precondition is useless alone (§ above), but it is a good
*filter*. Combined with a positive tell it becomes precise: **zero Greek in the
paper, AND a bare `[0-9] ?(mm|ms|mg|mA)\M` quantity, AND a spelled-out
"micron"/"micrometre"/"microsecond"/"microgram" somewhere in the same paper.**

Measured over `dr42995`'s 381 source papers: 117 are zero-Greek; the combined
detector narrows those to **5**, of which **1** is a confirmed scar and the other
4 are legitimate on inspection (real "10 mA cm⁻²", real "0.75 mm thick" gels).
That is a workable review load.

Do **not** use a numeric-plus-bare-letter detector without the zero-Greek
precondition — it fires on surface areas ("2000 m²/g") and bibliography initials
("24 L. Piantanida").

## Exposure, measured

> **⚠ The corrected detector is still not sufficient — criterion (3) is a
> NEGATIVE test.** "No LaTeX Greek macro anywhere in the document" is equally
> consistent with *the paper is corrupt* and *the paper never needed Greek*.
> Measured during the dr42995 audit: the three-criterion screen fired on **8 of
> 54** sources in one shard — a software-ecosystems study, a Loihi abstract,
> caDNAno — every one of which renders cleanly with intact math and correct
> sub/superscript markup.
>
> **The figures below are therefore upper bounds, not counts.** This is the
> third detector in this family to fail the same way (24% → 28% → this), and the
> pattern is always the same: a signal read as evidence of damage without
> measuring how often it occurs in undamaged documents.
>
> Before the flag is actionable it needs a **positive** corruption signature —
> a stranded unit where a Greek prefix is required (`1–2 V/ mm` for `V/μm`, a
> bare `m` where `μm` is meant), or the font-level test in §Fix order, which is
> the only deterministic one available.

Re-derived 2026-08-20 with the LaTeX discriminator, all regexes proven against
positive controls first (a known α-chunk, a known °-chunk, and pa47024's one
genuine `$\mu$m` span) so no zero-count is an escaping bug:

| scope | old detector (a+b) | **corrected (a+b+c)** | false-positive share |
|---|---|---|---|
| whole paper corpus (20,640 papers with live chunks) | 7,309 | **5,258** | 28.1% |
| `dr42995` evidence sources (352 with live chunks) | 118 | **41** | **65.3%** |

**Use 41 of 352, not the old 117 of 381.** The cohort's false-positive rate is
more than double the corpus-wide rate, so the over-count was worst exactly where
it was being used to make decisions.

The earlier claim-level figure — *"325 of 922 hubs grounded in an exposed source,
313 exclusively"* — is **withdrawn**. It could not be reconciled with any
(a)/(b)/(c) combination on re-derivation, and its denominator (922) predates the
seven merges. Re-derive from the 41-paper set before quoting a hub-level number.

`gr228594` tracks the detector gap.
