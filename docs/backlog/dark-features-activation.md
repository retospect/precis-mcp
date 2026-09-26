# Dark features — activation steps not recorded elsewhere

Shipped-dark features whose flip steps live in no other backlog item or
`docs/conventions/dark-switches.md` (the rest are covered:
quest loop → `quest-loop-activation.md`, classify →
`classifier-corpus-enablement.md`, markup-first → `markup-first-ingest.md`,
chem engines → `chem-tools-integration.md`, patent FTO →
`patent-authoring-loop.md`, card_forge autonomy → `reading-prep-loop.md`,
groomer → `backlog-groomer-items-half.md`, cost ceiling →
`daily-cost-ceiling-tuning.md`). Compact: feature → switch → step.

- **Sandbox-run lane** (`sandbox_run` job_type, slices 1–4) →
  `PRECIS_SANDBOX_ENABLED=1` on the sandbox hosts (the `code-sandbox`
  container alone never registers the pass) → deploy, install podman on
  those hosts, set the §5 companions (`PRECIS_SANDBOX_ARTIFACT_ROOT` →
  shared NAS mount; `PRECIS_SANDBOX_READ_MCP=1` if `precis_access:read`
  callbacks are wanted — `semantic_rejection` fails closed without it).
- **Diagram-propose autonomous drawer** → `PRECIS_DIAGRAM_AGENTIC=1`
  (or unset ⇒ auto when an MCP config is present) → nothing dispatches
  the `diagram_propose` job_type yet: mint/schedule a todo that does.
  (Feature residuals: `diagram-editing-and-chunk-binding.md`.)

---

# Absorbed 2026-09-26

## Chunk-tag classifier (ADR 0047) — corpus enablement

_Grouped 2026-09-26; was `classifier-corpus-enablement`._

Cascade shipped + validated; the remaining steps are below.

- ~~Flip PRECIS_CLASSIFY_ENABLED=1 for the role3 corpus drain~~ — happening
  via the `derived_drain` classify band instead (materialize
  `PRECIS_SMALL_BAND_CLASSIFY`, live in prod): as of 2026-08-16, 1.98M
  chunks carry ROLE3 (own 795k / background 605k / furniture 586k), 163k
  remain, draining ~40k+/day since the 2026-08-15 SMALL cloud cutover.
  Still open from that bullet: optional tier-2 escalation
  (PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5, ~$200–400) for own-claim
  precision past 91%.
- The generic axis runner (`src/precis/workers/axis_pass.py`) has never been
  enabled for any of its ~10 axes. material/transport eval numbers are STALE
  (2026-07-25 vocab changes; material has no gold rows for its three new
  values) — re-run scripts/classify/eval-classifier + add gold rows first.
  The topic cascade has no gold at all (CLASSIFY_TOPICS_VERSION 3) —
  spot-check tier-1 precision on the new topics before a corpus sweep.
- BLOCKER before any chunk-level axis sweep (role, open-question): a per-axis
  failed-chunk_claims lease reaper — a failed LLM call leaves the lease, so
  the chunk never retries until a version bump. Ref-level axes self-retry.
- open-question on memory is a no-op until a ref-level path for a
  level:chunk axis exists (see data/axes/open-question.yaml note). Better
  table detection (pipe/tab/repeated-token heuristic) is polish.

## Markup-first ingest — JATS/LaTeX/HTML before PDF+OCR

_Grouped 2026-09-26; was `markup-first-ingest`._

Status: draft (built, dark)
Owner: reto

Shipped portion: see the `src/precis/ingest/markup.py` module
docstring; full design in git history. Built: the pure markup
producers (JATS / arXiv HTML / Elsevier XML / flattened LaTeX) →
Marker-shaped blocks → the existing `PaperToWrite` downstream, watcher
routing, the attach-only upgrade guard in `db_writer`, the `fetch_oa`
markup cascade behind `PRECIS_FETCH_MARKUP` (default-off), and
provenance (`source_format`). Fallback contract: any markup parse
failure falls back to Marker OCR — markup-first must never lose a
paper we could have OCR'd.

### Open scope

- **Decide the PDF-race before flipping `PRECIS_FETCH_MARKUP`** (the
  blocking residual): per-stub the markup pass runs first
  (best-effort, swallows its own errors), then the PDF cascade runs
  unconditionally after — which body wins when both succeed is
  undecided. Decide before enabling on any host. Owner:
  `src/precis/workers/fetch_oa.py::_run_markup_cascade` /
  `_markup_fetch_enabled`.
- **Rollout tail:** flip the flag default-on once the stub backlog
  has been exercised; ADR documenting the append-only punt for
  existing refs (no retro re-ingest — refs keep their OCR body until
  a natural re-ingest).
- **Surface `source_format` in paper views** (lean: yes, in the
  existing meta block) so the operator can see which refs got the
  good path.

### Decided constraints

- Heavier structural `.tex` parsing is out of scope — v1 flattens and
  chunks (the `.bbl`/`anc/` conventions make it robust; it cannot
  fail to parse).
- Springer leg lands silently no-op when `PRECIS_SPRINGER_API_KEY` is
  unset (same pattern as the Elsevier/Wiley PDF legs).
- The PDF is always kept as the printable; Marker is simply never
  invoked when markup succeeds.
