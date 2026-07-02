# ADR index

Architecture Decision Records — one file per decision, numbered in
the order they were taken. Files are never deleted; obsolete
decisions are marked **superseded** and kept for history.

Per AGENTS.md: "sorted by number; never delete, only supersede".

## By topic — current authoritative ADR

| Topic | Current ADR | Notes |
|---|---|---|
| Repo merge (acatome → precis) | [0001](./0001-merge-acatome-into-precis.md) | foundational |
| Tabular output format (TOON) | [0002](./0002-pub-id-and-toon.md) | TOON portion in force; identifier portion superseded |
| Shared tool registry | [0003](./0003-shared-tool-registry.md) | |
| Dockerfile layout | [0004](./0004-multi-stage-dockerfile.md) → [0009](./0009-dockerfile-relocation-container-first.md) | 0009 relocated the file; 0004 still describes the stage layering |
| Migration discipline (forward-only) | [0005](./0005-greenfield-migrations.md) | governs every `*.sql` edit |
| Install path (baseline snapshot) | [0031](./0031-baseline-snapshot-dual-track.md) | fresh DBs load `migrations/baseline/schema.sql`; dual-track, not a greenfield (cf. [0019](./0019-second-greenfield.md)) |
| Identifier scheme | [0036](./0036-universal-handles.md) | **draft/proposed**; one universal handle per record + chunk; supersedes 0006/0008 as the current address form, drops `pub_id`. Lineage: 0002 §id → 0006 → 0008 → 0036 |
| Derived queue pattern | [0017](./0017-derived-queue-family.md) | extends 0007 (chunk-level → family registry) |
| Database backend | [0010](./0010-postgres-pgvector-system-of-record.md) | |
| Dev image (Claude Code + UID/GID) | [0011](./0011-claude-in-dev-image.md) | |
| Model weights in runtime image | [0012](./0012-bake-models-into-runtime-image.md) | cold-build mitigation in [0019](./0019-premodels-build-context.md) |
| MCP session context | [0013](./0013-mcp-session-context-env-vars.md) | env-var triple |
| PDF metadata write-back | [0014](./0014-pdf-metadata-writeback.md) | |
| Marker memory leak | [0015](./0015-marker-leak-mitigation.md) | |
| Work-claim locking | [0016](./0016-advisory-lock-claims.md) | postgres advisory locks; replaces file-based |
| Discovery layer | **superseded** — see [F20 note in CLAUDE.md](../../CLAUDE.md) | [0018](./0018-persistent-discovery-layer.md) kept for history; per-chunk KeyBERT now lives in `src/precis/workers/chunk_keywords.py` |
| Embedder as a service | [0020](./0020-embedder-as-service.md) | **accepted**; client+service+CLI landed (CUDA image + launchd pending) |
| Image split (serve/worker/ingest) | [0021](./0021-image-split-serve-worker-ingest.md) | **accepted**; serve/worker/ingest/embedder targets + build-all landed |
| Independent worker queues | [0022](./0022-independent-worker-queues.md) | **proposed**; extends 0016/0017 |
| `view='dreamable'` (ANN ring, no clustering dep) | [0023](./0023-dreamable-no-clustering-dep.md) | **accepted** |
| Dream loop runtime | [0024](./0024-dream-loop-litellm-inprocess.md) | **superseded / reversed** — in-process litellm abandoned; dream runs the `claude` binary (`utils/claude_agent.py`) |
| In-place cluster reconcile (not a third greenfield) | [0025](./0025-in-place-reconcile-not-third-greenfield.md) | **accepted**; does not supersede 0019 |
| precis-web as sibling package | [0026](./0026-precis-web-surface.md) | **accepted**; browser UI over the handler layer |
| Reparent todos via `parent` link relation | [0027](./0027-reparent-via-parent-link.md) | **accepted**; supersedes the `parent_id` column path for reparenting |
| Host heartbeat telemetry (Status tab) | [0028](./0028-host-heartbeat-telemetry.md) | **accepted**; extends 0026 |
| Multi-root corpus for PDF serving | [0029](./0029-multi-root-corpus-pdf.md) | **accepted**; `PRECIS_CORPUS_DIR` accepts a list of roots |
| `job` / `finding` / `cron` stay separate from `todo` | [0030](./0030-job-finding-cron-stay-separate.md) | **accepted**; rejects collapsing the four kinds |
| Drafts as editable chunk-native documents | [0033](./0033-draft-chunks-editable-document.md) | **accepted**; the `draft` kind |
| Figure assets + data supplements + permission provenance | [0034](./0034-figure-assets-and-permission-provenance.md) | **draft/proposed**; figures as chunks, blobs in `chunk_blobs` |
| Computed chunks (payload + recipe), sandboxed execution, recompute boundary | [0035](./0035-computed-chunks-recipes-and-the-recompute-boundary.md) | **draft/proposed**; refines 0034 §3 — data/render recipes, `plots` the only reactive edge |
| Universal handles (one address system for every record + chunk) | [0036](./0036-universal-handles.md) | **draft/proposed**; flat type-prefixed Crockford handles; partially supersedes 0033 §1 (draft handle/sigil) |
| Heading styles (self-contained sections) + numbering lock + issues | [0037](./0037-heading-styles-and-numbering-lock.md) | **proposed**; per-heading style=skill, genre=root style, entity-bound numbering with pinned/lock, anchored `issue` loop; extends 0033/0034/0035, uses 0036 handles. Drafting catalogue: `docs/design/draft-section-styles.md` |
| Prompt assembly & prompt-engineering principles | [0038](./0038-prompt-assembly-and-principles.md) | **proposed**; one assembler + module library (markdown+frontmatter+`{{include}}`), cached/variable layers, agent/helper profiles, per-target adapters, the doc_context/tools/kinds/glossary tables, kind=code alias, conditional modules. Validation: `docs/design/prompt-assembly-shots.md` |
| ORCID author kind & network discovery | [0039](./0039-orcid-author-kind-and-network-discovery.md) | **proposed**; `kind='orcid'` durable author node (slug=iD, embedded card), `authored`/`authored-by` ref→ref links w/ position meta, missing-DOI diff auto-enqueues stubs into the existing fetch_oa pipeline, S2 author endpoint (`authors:`/`author:`) for paper→author→paper BFS; uses 0036 handles, feeds 0030 stub→fetch. Skills: `precis-orcid-help`, author-discovery |
| `cad` kind — analytic-IR solid design | [0041](./0041-cad-kind-analytic-ir.md) | **proposed (v2)**; own a curated analytic-primitive IR (probe/inspect analytically, **never mesh or merge to inspect**), OpenSCAD/OCCT as *export* backends only. mm/float64, **rigid-only transforms ⇒ everything exact**. Nodes = a DAG stored as a flat chunk-list (primitives + `merge`/`subtract`/`intersect`/`move`/`pattern`/`instance` operators); generalized **frustum** (box/cyl/cone/hex/pyramid/taper) + sphere + torus, **chamfers** for edges; the **membership contract** is the exclusion line (no hull/minkowski). Eyes = a full-DOF probe ladder (point/line/arc/section, interval arithmetic, TOON output — no pixels, no GL); feature-attributed sections; subtraction visible via op-fold; clearance/interference/translational-DOF as persisted **observers**; **datums** (named axes/planes). Handle code `ca`; mutable soft-deletable chunks (0033), `draft_export`-style worker, artifacts on 0029. Threads/gears/rotational-DOF/fillets/STEP-OCCT phase 2. Skill: `precis-cad-help` |
| The `pcb` kind: netlist + placement IR (JLCPCB-native) | [0042](./0042-pcb-kind-netlist-placement-ir.md) | **proposed**; electronics sibling of [0041] — own the netlist + part-selection + placement IR (LLM reads it as graph/ratsnest, not pixels), rent the autorouter (Freerouting/EasyEDA) + fab (JLCPCB) at export. `kind='pcb'`(`pb`) two-layer (logical nets / physical placement); **stored in dedicated relational tables, NOT chunks** (converges with 0041 Amendment 1 — dedicated tables + one card chunk; 0042 goes multi-table since a netlist wants SQL+FK integrity; design stays a ref with one card chunk); type/instance split (component-type owns pins; instances are placements) + netlist as `pcb_netconns`(net,instance,pin); soft "measure" rows (bypass/terminator/sensitive↔noisy intent on a hard↔soft↔gauge spectrum, the "measuring tapes," re-evaluated like 0041 observers); `note` on every row (the why-of-a-wire); `fixed` mark (screw holes/status LED); `parts` catalog table JLCPCB-assemblable+high-turnover selector; BOM/CPL as views; datasheets as a thin `datasheet`(`da`) PaperHandler sibling (capped: one kind for the whole electronics-doc family, machinery shared); crossing-minimizing auto-place + route-feasibility estimate; phase-2 LLM-piloted "shove router" (own routing, rent gerbers). Builds on 0041(converges on storage w/ its Amdt 1)/0033/0035/0036. Skills: `precis-pcb-help`, `precis-part-select-help`, `precis-net-class-help`, `precis-measures-help`, decoupling/i2c/spi/datasheet |
| The `structure` kind: atomistic cell + bond-graph IR | [0043](./0043-structure-kind-atomistic-ir.md) | **shipped**; materials sibling of 0041/0042 — own a legible cell+atoms+bond-graph IR the LLM reads as structure not pixels, rent the relaxer (MACE/GPAW) at the energy-rung ladder via the §23.16 run-cube cache; typed ops + in-memory probes; cursors/measures on `struct_measures`; `derived-from` lineage + `StructureHandler.derive`; web viewer + instruction box. Skill: `precis-structure-help` |
| The derived-job lane: a job parents on its subject, not a todo | [0044](./0044-derived-job-lane.md) | **accepted (2026-07-02)**; a `kind='job'` parent is polymorphic — a `todo` (intent lane: rotation + `child-failed` bubble + `child_job_succeeded`) or a build subject (`structure`/`cad`/`draft`, compute lane: idempotent, cache-fillable, owned by the artifact). The lane is **emergent from the parent kind**, not a declared flag. An intentful requester that wants to block links `requested`→job (migration 0046, inverse `requested-by`); `derived_job_succeeded` evaluator closes it on success, the failure-bubble follows the link to tag it on failure. Removes 0043's "relax needs a parent todo" requirement; extends 0007's derived-artifact philosophy to cross-host compute. |
| The `folder` kind: placement, kind roles, spanning search | [0045](./0045-folder-kind-placement-and-roles.md) | **accepted**; extrinsic single-parent containment on `refs.parent_id` (derivation ≠ placement); generalizes 0027's virtual `parent` façade beyond todo; `KindSpec.role` (artifact/corpus/stream/system) gates placeability + default search scope; todo roots may sit in folders (kind-aware root predicate); virtual Unfiled, refuse-non-empty delete, shallow-folder discipline; `folder=` subtree scope on cross-kind search. Skill: `precis-folder-help` |
| LLM routing layer — one seam for model + transport + result | [0046](./0046-llm-routing-layer.md) | **proposed**; unit 4a+4b landed (`src/precis/utils/llm/router.py`). Consolidates ~a-dozen scattered `os.environ.get` model reads into one `resolve_model(tier)` table (cloud-super/opus, cloud-mid/sonnet, cloud-small/haiku, local-small/summarizer, local-big/qwen-heavy — the `PRECIS_MODEL_*` triad pins); one `dispatch(LlmRequest)` seam over the three transports (`claude_agent`/`claude_p`/litellm `LlmClient`) with transport chosen by `(tier, tools_needed)` aligned to the 0038 `Profile` (AGENT↔tools↔claude_agent, HELPER↔claude_p/litellm); one normalized `LlmResult` unifying the JSON-block / stream-json / OpenAI-choices shapes. Rogue subprocess sites (plan_tick/fix_gripe/tex_llm_fix) route model via the resolver + plan_tick gains a budget cap (4b). `local-big + MCP tools` is the **documented next step** (re-lands the reversed [0024] in-process-litellm-with-`tools=` path behind the seam). Extends [0038] |

## Supersession graph

```
0002 (identifier §)  ──→  0006  ──→  0008  ──→  0036   # slug → universal handles
0002 (TOON §)         (in force)
0033 §1 (draft handle/¶ sigil)  ──→  0036          # absorbed into universal handles
0007  ──→  0017                                    # derived queue
0007  ──→  0044            # derived-artifact philosophy → cross-host compute (job parents on its subject)
0043  ──→  0044            # structure relax no longer requires a parent todo (derived-job lane)
0007  ──┐
        ├──→  0018  ──→  F20 (CLAUDE.md, not an ADR)
0017  ──┘                # discovery layer — superseded post-ADR
0004  ──→  0009          # Dockerfile move (extends, not replaces)
0012  ──→  0019          # premodels build context (extends)
0024 (reversed)          # dream loop: in-process litellm → back to claude binary
0024  ┄┄→  0046          # local-big+tools: reversed litellm-with-tools re-scoped as the routing seam's next step
0038  ──→  0046          # prompt-assembly Profile → routing seam (model/transport/result); AGENT/HELPER ↔ Tier/Transport
0026  ──→  0028          # precis-web surface → host-heartbeat Status tab (extends)
0033  ──→  0034  ──→  0035  # editable-document model: figures, computed chunks (each extends)
0033  ──→  0037            # heading styles / numbering / issues (extends; 0037 reframes design/patent-drafting-merge.md)
0033 §8 (editor prompt) ──→ 0038   # prompt assembly: one assembler + modules (uses 0036/0037)
0041  ──→  0042            # CAD: solids → electronics/PCB sibling (extends keystone; CONVERGES on storage w/ 0041 Amendment 1 — dedicated tables + 1 card chunk; 0042 goes multi-table relational)
```

## Conventions

- New ADRs get the next number; never reuse a number.
- An ADR can be **superseded** (replaced wholesale), **partially
  superseded** (one section replaced; others in force), or
  **extended** (later ADR builds on it without invalidating).
- The header of the *older* ADR should name its successor; the
  successor's header should name what it supersedes.
- When a feature ships outside the ADR process (e.g. F20), update
  the affected ADR's status line to point at the live code path
  and leave the ADR body intact.
