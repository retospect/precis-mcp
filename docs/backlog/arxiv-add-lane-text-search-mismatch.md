# `precis add --arxiv` ingests the wrong paper (S2 text search, unverified hit)

Live mis-ingest (2026-08-25, ~09:35): `precis add --arxiv 2405.20258`
(Weinberg et al., "Static Subspace Approximation for RPA Correlation
Energies: Implementation and Performance", JCTC 2024, DOI
10.1021/acs.jctc.4c00807) inserted **rosenbloom19, ref 254174** — a 2019
paper. The lane queries S2 with a free-text search
(`paper/search?query=arxiv:2405.20258`), got a 429, retried, then took the
top text hit without checking that `externalIds.ArXiv` matches the
requested id. Fix: use the S2 direct-lookup endpoint
(`/graph/v1/paper/arXiv:<id>`) instead of search, and reject any result
whose ArXiv externalId ≠ requested id. Owner: `src/precis/cli/add.py`
(S2/arXiv lane).

Cleanup rider: ref **254174 (rosenbloom19)** is the orphaned wrong insert
and `tools delete` lists the `paper` kind as unsupported, so it needs the
SQL-layer soft-delete (or whatever the sanctioned paper-retire path is).
Context: it was meant to be the 15th of the BEAST eChem corpus batch
(requested from the catpath session); the CrossRef `--doi` lane had missed
on 10.1021/acs.jctc.4c00807 — api.crossref.org was timing out entirely at
the time, so that miss was almost certainly transient, and the DOI retry
is the way to land the intended paper.

Test: `add --arxiv` for an id whose text-search top hit differs from the
direct lookup (2405.20258 reproduces today) ingests the correct paper or
fails loudly; never a silent wrong insert.
