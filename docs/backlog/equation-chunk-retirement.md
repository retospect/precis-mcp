# Retire the equation chunk kind — papers remain

Decided north star: no dedicated equation kind; math is $…$/$$…$$ in prose,
KaTeX-rendered on read. Drafts (278) are done. Papers (~54.6k chunks,
Marker-minted, deliberately un-embedded via SKIP_EMBED_TYPES) need the embed
policy decided first (strip-to-placeholder? keep skipping? a math-marker
paragraph the embedder skips?), then the Marker classification change + a
throttled batch migration (append-only body chunks ⇒ DELETE+INSERT cascade at
scale). Shared work: a KaTeX-safe normalizer (strip \label/\tag,
align→aligned, pure tested fn + gold set); numbering/\ref decision; LaTeX
export of $$…$$. Interim if unscheduled: make equation *render* (wrap bodies
in $$). Owner `src/precis/ingest/marker.py`, `pipeline.py`, `literature.py`.
