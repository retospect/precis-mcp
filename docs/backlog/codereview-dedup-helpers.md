# codereview: Divergent duplicate helpers — pick canonical, consolidate

**Cite-key minting, patent export, and provenance matching disagree on the same paper.**

## _first_author_surname ×5

Locations:
- `precis/identity.py` (full impl, **canonical**)
- `utils/short_cite.py`
- `export/_patent_cite.py` (skips ASCII folding)
- `ingest/provenance.py::_first_author_surname` (returns whole name when no comma; also `_from_authors`)

~60 LOC true duplication. Impact: cite-key minting fragmentation.

## Abbreviation short-form regex ×3

Locations:
- `export/docx.py::_Ctx.short_pattern` → `\b…(s?)\b`
- `export/latex.py` ~L379 → `(?<![\w-])…(s)?(?![\w-])`
- `precis_web/linkify.py` ~L952 → `(?<![\w-])…(?:s|es|'s|'s)?(?!\w)`

Manifestation: hyphenated compounds highlight in the web reader but not the PDF.

Canonical home: `utils/abbreviations.py` (has find/substitute, lacks a pattern builder).

## _lease_seconds ×2

Locations:
- `workers/executors/ssh_node.py` reads `params.resources.wall_seconds`, floor `_LEASE_FLOOR_S`
- `claude_docker.py` reads `params.wall_seconds`, floor `_LEASE_MARGIN_S`

Impact: a job carrying the other shape silently gets the floor lease. Needs a decision on the canonical meta shape.

## Module-kept copies by convention ×N

Documented in docstrings as "each module keeps its own copy" — causes real drift.

Examples:
- Taproot claim predicates: `_is_claim_hub` (byte-identical in `taproot/hub.py` + `taproot/seniority.py`); `_is_compound_hub` (`taproot/hub.py` + `workers/hub_refine.py`); conjunct-of relation string ×5 modules; no test pins copies together.
- `_cosine` ×5: `skill_index/index.py` · `utils/segmentation.py::zip(strict=True)` raises · `quest/gaps.py` silently truncates · `quest/placement.py` + `quest/tick.py` numpy

Re-decision needed: retire the convention or make it enforceable (e.g., via a code-sync test asserting copy parity).

## utils/llm/router.py::dispatch — inline gate chain

~15 sequential gate/filter steps (placement filter, cloud throttle, unserved-local-rung skip, failover wrap, breaker, admit, local-serving slot, hosted-small remap) each with a multi-line inline-comment block; several already have `_apply_*`/`_skip_*` sibling helpers. Extract each gate to a named helper so its comment lives once at the helper.

## `_label`/`_head` "handle — title" formatter ×2

`utils/refeye.py` and `utils/eye_render.py` duplicate a near-identical truncate-at-90/80-chars formatter — one shared formatter.
