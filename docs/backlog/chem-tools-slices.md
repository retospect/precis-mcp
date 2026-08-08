# chem-tools (ADR 0056) — remaining slices

route/protein shipped; deploy, verification, and new-kind slices remain.

- Deploy slice 2: rebuild the aizynth image on spark
  (playbooks/43-aizynth.yml) so the shim emits route.json; scripts/deploy for
  the precis-side parse_syngraph / view='metrics'.
- Slice 3 ASKCOS live-verify: stand up v2 (PRECIS_ASKCOS_URL) + verify the
  Tree-Builder request/response schema against the instance's /docs — the
  one unverified surface, flagged in `src/precis_chem/askcos.py`.
- Slice 4c ColabFold MSA engine: needs-decision (containerize + pick the MSA
  source; de-novo single-seq accuracy is low).
- Slice 5 `sequence` kind: engines chosen (Boltz-2 + LigandMPNN;
  torch-cuda base image path proven on the GB10) — ready to build as
  adapters + roles/* mirrors of roles/alphafold.
- Slice 6 plan_tick executor: deferred (the generic planner already drives).
- MCP-surface design review over route/protein/structure/sequence: view=
  naming, dark/plugin-kind discovery, and the CLI/repl put arg-allowlist gap
  that rejects plugin kwargs (only runtime.dispatch / MCP JSON-RPC works).
