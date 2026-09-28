# Photo capture of projected slides → `kind='pres'`

IDEA. Live conference note-taking currently loses everything that is only on
the screen. References, formulas, axis labels and plot values get transcribed
by ear into a draft, with predictable results: this session alone produced a
mis-heard journal volume (Nature Materials 23 heard as a 2026 issue), a
mis-heard year (PRL 104 is 2010, heard as 2018), a mis-heard page number
turned into a DOI that did not resolve, and several author names that took a
Crossref round-trip each to pin down. A photograph of the slide contains all
of it correctly. Précis has no path from that photograph into the corpus.

## What already exists, and why it does not cover this

`src/precis/ingest/pres.py` is a complete PDF → `kind='pres'` pipeline: one
chunk per slide with `chunk_kind='pres_slide'`, Marker for extraction (which
carries Surya OCR — see the `SuryaOCRConfig` patch in
`src/precis/ingest/marker.py`), `subtype:slides` tag, no DOI cascade,
idempotency keyed on `pdf_sha256`.

So OCR is already in the stack. The gap is the *input*. A photo of a projected
slide is not a PDF page:

- **Perspective.** Shot from a seat, not head-on. Needs quadrilateral
  detection and a homography rectification before OCR sees it.
- **Photometry.** Projector glare, hot spots, a dim room, the presenter's
  shadow, rolling-shutter banding off the projector refresh.
- **No document structure.** No text layer, no page boxes, no embedded fonts —
  everything Marker normally leans on for a born-digital PDF is absent.
- **No stable identity.** `pdf_sha256` is the idempotency key; two photos of
  the same slide differ in every byte. Burst-shooting one slide is the normal
  case, so near-duplicate collapse is required, not optional.
- **Ordering.** Capture order is the only sequence signal, and it is
  unreliable — slides get re-photographed when the first shot is blurred.

## Shape

1. **Capture side.** Photos land in a watched directory or are pushed from a
   phone. No new UI needed for a first cut.
2. **Rectify.** Detect the slide quadrilateral, warp to a rectangle,
   normalise illumination. This is the only genuinely new image-processing
   step; OpenCV covers it.
3. **Deduplicate the burst.** Perceptual hash plus a similarity threshold to
   collapse repeat shots of one slide, keeping the sharpest. This replaces
   `pdf_sha256` as the identity mechanism and is the part most likely to be
   got wrong.
4. **Extract.** Hand the rectified image to the existing Marker/Surya path and
   write `pres_slide` chunks through the existing `write_pres`.
5. **Formulas.** Marker emits LaTeX for equations it recognises. Slide
   photographs are a harder case than a paper PDF and this will be the
   weakest link; it should degrade to an image reference plus whatever text
   was recovered, never to a silently wrong formula.
6. **Reference harvesting — the actual payoff.** Slide citations follow rigid
   patterns ("Author et al., Journal vol, page (year)"). A pass over the OCR
   text that pulls candidate references and runs them through the existing
   Crossref resolution would have caught every mis-transcription listed above.
   This is the step that justifies the item; without it the feature is just
   image storage.

## Link to the live draft

The note-taking draft and the slide deck are two records of one talk. A
`pres` ref minted from the photos should link to the draft section for that
talk (`documents` or a new relation), so that a claim in the notes can be
checked against the slide it came from. Related:
`docs/backlog/figure-kind-slices.md` for how an image becomes a citable
object.

## Open questions

- Does this become a new ingest entry point, or a pre-processing stage in
  front of the existing `extract_pres`? The latter reuses far more, but
  `extract_pres` is written around a `Path` to a PDF.
- Is rectification worth doing when phone camera apps increasingly ship a
  document-scan mode that does it on-device? Possibly the right first cut is
  to require a pre-rectified image and add detection only if that proves
  annoying.
- Conference slides are the presenter's copyright and are often marked "do not
  photograph". Stored photos must inherit the figure-clearance machinery
  (`src/precis/utils/figure_clearance.py`) so a deck can never leak into an
  export. Default should be un-cleared.
- Is OCR'd slide text good enough to embed and search, or does it pollute the
  index? Suggest tagging it distinctly and keeping it out of evidence-grade
  retrieval until measured.

## Owner

`src/precis/ingest/pres.py`, `src/precis/ingest/marker.py`.
