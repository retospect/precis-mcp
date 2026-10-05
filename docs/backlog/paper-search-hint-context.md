# Preserve same-search paper hint context

Owner: paper search. Gripe: gr469183. Base: fetched origin/main
0bf2ca6f28c89b420109a421d9acd71d532e4ee3. Reto authorized this repair;
independent source review precedes coordinator integration/release.

## Premise

R14 native search scoped to pa1120 in lexical mode offered a larger-page
hint that dropped scope/mode. Following it returned global hybrid results,
including pc483145 from pa3965. Both same-search hint forms omit context;
BlockSearchResult carries scope but not mode. Triage is in fleet-state
inbox/paper-r14-hint-triage.md/.json; no fix exists in the checked base.

## Contract and acceptance

- Carry requested mode through BlockSearchResult; omitted mode retains the
  existing default. Serialize original scope and explicit mode alongside
  q/queries/answers/per_paper in same-search hints.
- Next-page actions retain page_size and advance page. Larger-page actions
  explicitly advertise a restart, request page=1 and page_size=10.
- Canonical handler regressions parse generated calls and execute their
  arguments: scoped lexical single-hit restart and multi-page continuation
  must stay inside the source paper and never call the mock embedder.
  Preserve broad arguments and omitted-mode/default behavior.
- Focused scripts/test, scoped container types, Ruff/format/diff. Coordinator
  owns version/full gate. Publish branch for independent Codex review.

No retrieval, ranking, caps, cards, unique_per or generic widening-recovery
redesign. Card-preference/card-only native fixtures remain UNVERIFIED.
No production scientific writes, models, ingestion, exports or signing.
No merge/deploy/gripe closure. Postdeploy replay: rerun the original
pa1120/lexical/per_paper=2/page_size=1 search, then follow its literal hint;
expect scope pa1120, mode lexical, page=1 and page_size=10, with no pa3965 hit.
