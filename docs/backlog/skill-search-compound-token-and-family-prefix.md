# Skill search: hyphenated compound terms and family-prefix words under-rank their skill

From the 2026-10-02 skill-search-review. Matcher change in `src/precis/handlers/skill.py` (`_content_tokens`, the title/header boost block); not a vocabulary edit.

- **Hyphenated compounds split into generic words.** `child-failed-parked` becomes `child`, `failed`, `parked`; live top-3 is memory-help ("Park an unresolved problem"), math-help ("When math fails"), gripe-help ("Park a gripe"). `precis-job-help` (17 mentions of `child-failed`) lands 5th. Prod agents re-ran this query family about 10 times in the window (`child-failed-parked`, `lane-skipping`, `pass-dead`), each a reformulation. Idea: also index the unsplit hyphenated token (and the `a-b` bigram) in the lexical leg and weight it above its parts.
- **Family-prefix word with several owners.** `cad units mm primitives shell hollow frame lattice pattern` ranks firstline-help, se-chain-help, doi-extract-help, se-print-help, then `precis-cad-help` 5th. `cad` is in three slugs, so the DF==1 relief from gr259665 does not fire. Idea: let a query token equal to a slug's family root (`precis-cad-help` over `-build-`/`-assembly-`) pin that root skill.
- **Not ranking, content gaps** (zero skills mention them): `pass-dead` watchdog condition, `lane-skipping`, OpenTimestamps/OTS batch mechanics (only a clause in the nanopub-help summary). Three prod jobs searched OTS in three phrasings and got nothing relevant. Needs a documenting owner, then a skill H2.

Evidence: 141 executed searches (111 prod job, 30 local) over 40 days; numbers in the Log line of `docs/runbooks/skill-search-review.md`. Related: gr259665 (fixed 2026-09-09), gr461598 (searches stalling >120s).

test: `search(kind='skill', q='child-failed-parked')` returns precis-job-help in the top 2; the cad query returns precis-cad-help in the top 2.
