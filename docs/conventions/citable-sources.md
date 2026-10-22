# Citable sources in specs and docs

Reto 2026-09-16: "As a rule, be sure to maintain citable sources please."
A spec claim without a source becomes an unsourced claim in a draft.

- Every theorem, algorithm or prior-art statement in `docs/backlog/*`, spec
  files, ADR-style docs and skills carries a reference.
- Search `kind='paper'` first and cite the store id. If the paper is absent,
  import by DOI (`put kind='paper' mode='import'`, a sanctioned prod write),
  then cite the id.
- Every citation carries the DOI (when one exists) and a full reference next
  to the precis id: readers of the public repo cannot resolve our ids.
- A claim you cannot source gets an explicit "source needed" marker, never a
  plausible-looking reference written from memory.
- Keep a Sources section per doc.
