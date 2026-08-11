# codereview: kind-set derivation — two residual product calls

The mechanical work shipped: `utils/kind_facts.py` derives kind sets from
`KindSpec`, `tests/test_kind_totality.py` pins them, taproot's evidence set
is single-sourced. Two sets were deliberately kept hand-maintained because
deriving them changes behavior — each is pinned by an invariant test that
documents the exact divergence, awaiting Reto's call:

1. `taproot/hub.py::EVIDENCE_SRC_KINDS` is `{paper, patent}`; a pure
   `corpus_role='evidence'` derivation would ADD `datasheet` and `edgar` as
   valid claim-hub evidence sources. Scope call (see taproot.md open #15).
2. `utils/eye_render.py::_DOC_KINDS` — derivation would REMOVE `web`
   (doc-shaped but no corpus_role); separately `edgar` looks like a real
   gap (an `eg<id>` eye falls through to the note renderer today).

Decide, then either flip to derivation or bless the hand list in place.
