# codereview: kind-set derivation — one residual product call

The mechanical work shipped: `utils/kind_facts.py` derives kind sets from
`KindSpec`, `tests/test_kind_totality.py` pins them, taproot's evidence set
is single-sourced. Reto's calls so far: `edgar` joined
`taproot/hub.py::EVIDENCE_SRC_KINDS` (claim-hub evidence source) and
`utils/eye_render.py::_DOC_KINDS` (cluster-TOC eye renderer) — both applied,
invariant pins updated.

Remaining call:

1. `datasheet` as a valid claim-hub evidence source? A pure
   `corpus_role='evidence'` derivation would add it to
   `EVIDENCE_SRC_KINDS`; today it is deliberately excluded and the
   divergence is pinned by
   `test_taproot_evidence_src_kinds_is_a_corpus_role_evidence_subset`.
   Scope call (see taproot.md open #15).

Decide, then either flip to derivation or bless the hand list in place.
