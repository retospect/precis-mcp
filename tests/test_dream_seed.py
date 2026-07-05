"""Part B — dream PROCESS-lens seed loading + rendering.

The single-stance persona lenses moved to oracle traditions (see
``test_oracle_lens.py``); ``dream_lenses.yaml`` now carries only the
multi-phase *process* lenses (Disney's Dreamer → Realist → Critic).
"""

from __future__ import annotations

from precis.utils import dream_seed as ds


def test_packaged_lenses_are_process_only():
    lenses = ds.load_lenses()
    ids = {l["id"] for l in lenses}
    # Disney survives here; the personas emigrated to oracle.
    assert "disney" in ids
    assert "feynman" not in ids and "shannon" not in ids
    # Every remaining lens is a process shape with an injectable prompt.
    assert all(l.get("kind") == "process" for l in lenses)
    assert all(l.get("prompt") for l in lenses)


def test_select_lens_rotates_and_wraps():
    lenses = ds.load_lenses()
    n = len(lenses)
    # Deterministic: same bucket → same lens; wraps at N.
    assert ds.select_lens(lenses, bucket=0) is ds.select_lens(lenses, bucket=0)
    assert ds.select_lens(lenses, bucket=n) is ds.select_lens(lenses, bucket=0)
    swept = {ds.select_lens(lenses, bucket=b)["id"] for b in range(n)}
    assert swept == {l["id"] for l in lenses}


def test_select_lens_empty_is_none():
    assert ds.select_lens([], bucket=3) is None


def test_render_lens_block():
    lens = {"id": "feynman", "name": "Feynman", "prompt": "Dream as Feynman."}
    block = ds.render_lens_block(lens)
    assert block.startswith("## This cycle's lens: Feynman")
    assert "Dream as Feynman." in block


def test_disney_is_a_process_lens():
    lenses = {l["id"]: l for l in ds.load_lenses()}
    assert lenses["disney"]["kind"] == "process"
