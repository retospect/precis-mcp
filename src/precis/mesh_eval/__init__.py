"""Mesh upkeep evals — slice 0 of ``docs/backlog/local-mesh-upkeep.md``.

The question the package answers: can a *local* model do graph-memory
upkeep well enough that a bigger model's review is cheap, or well enough
to apply outright? One graded task set per upkeep lane, built read-only
from prod (or from frozen run dumps) by the ``scripts/llm_eval/
build_mesh_*`` builders into JSONL fixtures under the gitignored
``scripts/llm_eval/gold_set/local/`` — prod-derived text never enters the
public repo.

Lanes (the slice-0 set): ``categorise`` (name the measurand key for each
marked mention in a hub — the taxonomy-bootstrap discovery prompt, gold =
the frontier run's folded keys), ``link-add`` (propose typed edges to a
node's embedding neighbours; gold = verified edges plus neighbours with
none), ``finding`` (state the claims a paper chunk supports; gold = the
verified corroborates hubs), ``merge`` (hub pair, same or different; gold
= recorded ``refines`` merges vs unmerged near neighbours) and
``summarise`` (the production summariser path, reusing
:mod:`precis.llm_eval`'s ``summary`` scorer).

Layout:

- :mod:`.sets` — the frozen-fixture model (:class:`MeshItem`,
  :class:`EvalSet`, JSONL load/dump, the reserved human holdout shape).
- :mod:`.scorers` — deterministic per-lane scorers.
- :mod:`.runner` — run a set against any OpenAI-compatible endpoint and
  report per-lane accuracy with a Wilson CI plus the two bars.
- :mod:`.retest` — the ported ``compare_runs.py`` folded-key agreement
  between two discovery runs (the frontier test-retest reference).
- :mod:`.accept` — sampled batch acceptance: a stratified sample goes to a
  judge callable, the error rate's Wilson interval decides
  accept/reject/escalate.
- :mod:`.usefulness` — the mesh-usefulness metric (orphans, duplicate
  candidates, stale nodes, next-hop follow-through) as read-only queries.

Two bars (``local-mesh-upkeep.md`` §1, Reto 2026-10-03): **auto-apply**
needs agreement with gold at or above the frontier's own test-retest on
the same set *and* a clean human-holdout read — categorise and link-add
only, never merge; **reviewed** needs (local + review) cost per accepted
item under the frontier's generation cost per accepted item.
"""
