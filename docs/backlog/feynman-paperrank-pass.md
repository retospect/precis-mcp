# PaperRank-style paper-priority score pass

Port the *rubric* (not the code) of feynman's PaperRank: score papers on
citation impact, methodological rigor, reproducibility, and provenance,
written by a registered synthesis pass (card variant / property on
`kind='paper'`). Feeds reading-queue and quest-frontier ordering — review
tiers judge claim quality, not reading priority, so this is complementary.

Source: `companion-inc/feynman` (MIT), `src/rank/paper-rank.ts` — explicit
`DEFAULT_SCORE_WEIGHTS` per component, ranked `PAPER_RANK_SOURCES`,
citation-expansion limits. https://github.com/companion-inc/feynman

Owner: `precis.workers` (new pass). Test: pass fills score on a fixture
paper; ordering query returns high-rigor paper first.
