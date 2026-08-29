# Topological place+route paper — lives in precis, not here

The paper is a precis `draft`. That is the canonical, editable copy; there
is deliberately no markdown mirror in this repo, because two copies of a
living document diverge the moment either is touched.

| | |
|---|---|
| draft | `topo-place-route` (root heading `dc3365172`) |
| project todo | `td266176` |
| structure | §1–§9, plus Appendix A (verified bibliography) and Appendix B (planned nanopublications) |

```
get(kind='draft', id='topo-place-route')              # outline
get(kind='draft', project=266176)                     # same, via the project
search(kind='draft', q='<term>', scope='topo-place-route')
```

Export to LaTeX/PDF/Word when a submission copy is needed — see
`get(kind='skill', id='precis-draft-help~17')`. Do not re-import an export
back into this directory.

## What the paper describes

The `pcb` kind's LLM-guided topological place-and-route system in
`src/precis/pcb/`. The design spec and live build status are in
`docs/backlog/pcb-guided-place-route.md`, which stays the authority on what
is shipped, inert, or unbuilt — the paper quotes that state as of writing
and will go stale against it.

## Open threads

- Evaluation is not run. Benchmark pointers and the three traps that will
  bite are in `~/benchmark.txt` (PCBWorld arXiv:2607.05915 as primary, the
  UCSD `PCB-Benchmarks` 11 sets as secondary).
- Nanopublications cannot be minted until the preprint has a DOI and is
  ingested as a `paper` ref; Appendix B records the ordering and the gate
  constraints.
- Appendix A flags every citation that is snippet-only or unverified. Do
  not promote one to a bare citation without fetching it first.
