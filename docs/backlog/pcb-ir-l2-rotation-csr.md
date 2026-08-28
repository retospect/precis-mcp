---
status: ready
title: pcb IR L2 rotation storage unbuilt — set_rotation can never write (slice-2 qland debt)
prio: medium
---

# Size the rotation CSR so set_rotation has slots to write

`precis.pcb.ir.from_graph` constructs `rotation_index` as an all-zeros CSR
with empty `rotation_darts` (and `test_from_graph_leaves_l1_l2_l3_unset`
pins that birth state), so `PcbIR.set_rotation` finds `lo == hi == 0` for
every pin and raises "pin N has 0 incident darts" — the L2
propose→apply→validate contract cannot execute at all. Born failing in the
ungated slice-2 qland (f04c107b); found 2026-08-28 when the quest-fixes
ship ran the full gate over integrated main.

Three tests in `tests/test_pcb_ir.py` encoding the intended contract are
marked strict xfail under the `_L2_UNBUILT` marker
(`test_propose_then_apply_then_validate_round_trips`,
`test_validate_embedding_catches_a_move_without_mutating_storage`,
`test_unconnected_items_clean_fixture_reports_nothing`).

Fix: size the CSR by per-pin incident-dart counts at `from_graph` time
(darts = segment endpoints), allocate `rotation_darts` with an unset
sentinel, reconcile with the `rotation_darts.shape[0] == 0` birth
assertion (either drop it or represent "unset" differently), then remove
the `_L2_UNBUILT` marker — its `strict=True` will flag the moment the
machinery works.

Also fixed in the same ship (not open): `from_graph` defaulted
`stackup=[]`, making every layer mutator raise — now defaults
`DEFAULT_STACKUP`.

DoD: the three xfail tests pass un-marked; `_L2_UNBUILT` deleted.
