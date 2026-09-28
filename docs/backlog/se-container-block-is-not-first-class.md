---
status: draft
title: "se: a block that has both its own envelope and children has no first-class 'container' marker, so three subsystems each guess differently"
prio: high
---

# se: a block that has both its own envelope and children has no first-class 'container' marker, so three subsystems each guess differently

## Motivation / why

In an `se` block tree, a block can have BOTH its own envelope geometry AND child blocks. The `unicycle-c1` root block is exactly this: an overall envelope plus 15 children. Nothing in the model marks it as "a container/assembly boundary" versus "a solid part", so every consumer invents its own answer, and all three were wrong in a different way:

1. **Renderer** (`src/precis_web/blocktree_3d.py`, `build_shapes_node`, the `kind == "shape"` branch with non-empty `visible_kids`): drew the container's own mesh as a fully opaque leaf, so the root envelope was an opaque box hiding all 15 children. Fixed 2026-09-28 in commit 554aeb54 — container leaves are now labelled `"<name> (envelope)"`, their paths collected into `Assembly3D.container_paths`/`Scene3D.container_paths`, and the viewer renders them translucent by default with a solid/hidden mode selector. But that fix works by *detecting* containers structurally (own shape + visible children) rather than reading a declared flag.

2. **Tool access** (`src/precis_se/toolaccess.py`) and **fastener stack-up** (`src/precis_se/fasten.py`): treated the container's envelope as a solid obstacle. On `unicycle-c1` all five `no_tool_access` findings said the tool was "stopped by 'unicycle'" — i.e. stopped by the assembly's own bounding envelope, which is not a real obstruction. Fixed 2026-09-28 in the same commit by skipping ancestors of the fastener, importing `_is_ancestor` from `precis_se.validate`. Precedent cited was `validate.envelope_overlaps`, whose docstring already says it excludes ancestor/descendant pairs because "a child inside its parent module's envelope is containment, not interference" — so `validate` had independently worked this out and the other two had not.

3. **Order/BOM view**: per gripe gr450524, `view='order'` drops any block with children from the to-make table entirely — so a container that IS a real machined part never appears in what you have to make.

The point of this item: all three are symptoms of one missing concept. Each was patched at its own site with a different heuristic, so the next consumer will get it wrong again.

## In scope

Make explicit a declared marker on the block that says whether a block's own envelope is a real part, a nominal assembly boundary, or both — and have the renderer, toolaccess/fasten, validate, and the order view all read that one marker instead of each guessing.

Concretely:
- Define the marker (name and mechanism deliberately left open — see open questions below for the three candidates).
- Audit the three existing consumers (renderer, toolaccess/fasten/validate, order view) to name the specific heuristics they currently use, and verify each reads the new marker correctly instead.
- Propose a migration path for existing designs: default behavior on unmarked blocks (backward compatibility).
- Write the validation rule that checks for inconsistent or invalid marker values.

## Explicitly NOT in scope

- Deciding *which* mechanism the marker uses (flag, `kind` variant, derived from envelope properties) — that is an open question below.
- Changing the semantic meaning of the marker once chosen (this item documents what it should mean; a later item enforces it across all consumers in one go if needed).
- Fixing the Order/BOM view's child-drop behavior — that is gripe gr450524, tracked separately. This item is the prerequisite that makes the fix possible across all three subsystems at once.

## Acceptance criteria

- A clear prose statement (in this backlog item) of what the marker means: "container envelope = container boundary only" vs. "container envelope = solid part" vs. "both" or equivalent terminology.
- The marker's storage schema, including how it appears in the `se_blocks` table (or related).
- Each of the three consumers (renderer, toolaccess/fasten, order view) audited and documented for its current heuristic; no heuristic remains implicit.
- A proposed default value for existing/unmarked blocks, with justification.
- Open questions resolved into decisions (see below).

## Target + blast radius

- `src/precis_se/` — the data model (likely in `__init__.py` or a new migration).
- `src/precis_web/blocktree_3d.py::build_shapes_node` — currently detects containers by `kind == "shape"` and non-empty `visible_kids`; audit to see if it reads a marker instead.
- `src/precis_se/toolaccess.py` and `src/precis_se/fasten.py` — currently skip ancestors; verify they read the marker.
- `src/precis_se/validate.py::envelope_overlaps` — already excludes ancestor/descendant; verify it reads a marker if one exists.
- Order/BOM view route (exact location TBD, but see gripe gr450524).
- The `precis-se-help` skill's op/view rosters if a new op is added.

## Open questions / decisions log

- **(a) Storage mechanism:** Is it a flag (e.g., `is_assembly_container: bool`), a `kind` variant (e.g., `kind='container'` or `kind='part'`), or derived from whether the envelope has a material/process assigned? Each has trade-offs: a flag is explicit and easy to migrate; a `kind` reuses the existing taxonomy but may overload its meaning; derivation is implicit but may be surprising on re-read.

- **(b) Backward compatibility and migration:** What should unmarked blocks default to? Option 1: inherit from a design-level setting (e.g., `unicycle-c1`'s root is "assembly container" by default, inner blocks are "parts"). Option 2: all unmarked blocks are inferred as "both" (most conservative, least surprising, but carries the ambiguity forward). Option 3: per-context inference (e.g., the renderer infers from structure, but `toolaccess` treats it differently). Current lean: Option 2 for now; a design-level setting can be added later if needed.

- **(c) Relationship to the existing `validate.envelope_overlaps` rule:** That docstring already articulates the ancestor/descendant exclusion. Should we re-express it in terms of the new marker, or keep the two separate? Current lean: refactor `envelope_overlaps` to use the marker if one exists, but keep the ancestor check as a fallback for unmarked blocks.
