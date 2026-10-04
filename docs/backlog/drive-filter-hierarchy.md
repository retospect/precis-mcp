---
status: draft
pillar: platform
---
# Drive filter hierarchy

Reto: the many small filter chips on `/drive` intimidate new users. Approved
layout (2026-10-04): Discover, Return and Showcase task entry points with a
shared searchable kind/filter picker. Search, active chips and clear reset
remain visible; the full roster is disclosed on demand. Web owns implementation.

## Checked premise

`src/precis_web/templates/drive/index.html.j2` renders Mine/Sources/Machine
presets above always-visible Source/Author/Design/Work chip rows and any
deep-linked Other kinds. Search, sort, state, dates and folder controls share
the top row. Grouping already exists, but does not reduce visible choices.
`routes/drive.py::index` gives explicit `k=` precedence over saved selection;
only `submitted=1` writes the selection cookie.

## Proposed bounds

- Discover defaults to newly created work across relevant kinds and folders,
  including findings; recently modified is a separately named alternative.
- Return uses personal viewing history; Showcase uses a curated collection
  (approved 2026-10-04). Storage/API, membership and sharing details remain
  subject to owning-spec review; layout approval authorizes no schema change.
- Preserve explicit URLs, saved selection, folder scope, tags and `cited_by`
  through every interaction. A collapsed group must retain selected kinds.
- Every supported kind remains reachable. Recompute coverage from current
  declarations; `kind-taxonomy-audit.md` contains dated counts and unverified
  claims. A presentation change does not require retiring or merging kinds.
- Keyboard access, visible selected counts and a clear reset path are part
  of the same change. No schema or kind-API change is proposed.

## Acceptance

From a fresh session, find a known design (for example the unicycle) without
exposing every filter first; then expand a group and refine it. Repeat via a
deep link and a saved selection. Compare visible-choice count before/after;
verify all kind groups, folder scope and selection survive navigation.
Record the deployed SHA and object handle in the dogfood result.

Pending: independent browser review of the implementation and remaining
storage/API design. Clouds, rings and fingerprint icons remain separate R&D.
Related: `drive-presenter-completeness.md`, `kind-taxonomy-audit.md`;
scale sorting is separately tracked in `drive-characteristic-scale.md`.
