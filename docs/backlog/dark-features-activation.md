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
