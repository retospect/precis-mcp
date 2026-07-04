"""Part B — dream lens seed loading, rotation, and rendering."""

from __future__ import annotations

from precis.utils import dream_seed as ds


def test_packaged_lenses_load():
    lenses = ds.load_lenses()
    ids = {l["id"] for l in lenses}
    # The 8 figure personas + the Disney process lens.
    assert {"feynman", "shannon", "disney"} <= ids
    assert len(lenses) >= 9
    # Every lens carries an injectable prompt (the loader drops any that don't).
    assert all(l.get("prompt") for l in lenses)


def test_select_lens_rotates_and_wraps():
    lenses = ds.load_lenses()
    n = len(lenses)
    # Deterministic: same bucket → same lens; +1 → next; wraps at N.
    assert ds.select_lens(lenses, bucket=0) is ds.select_lens(lenses, bucket=0)
    assert ds.select_lens(lenses, bucket=0) is not ds.select_lens(lenses, bucket=1)
    assert ds.select_lens(lenses, bucket=n) is ds.select_lens(lenses, bucket=0)
    # Full sweep covers every lens over N consecutive buckets.
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
    assert lenses["feynman"]["kind"] == "persona"
