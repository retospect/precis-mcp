"""Tests for :mod:`precis.taxonomy.freeze` — stage 5, freeze/write/read/diff
of a versioned ``list.vN.yaml``.

DB-free and network-free: everything here is dataclasses and a temp
directory.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from precis.taxonomy.config import PROCEDURE_VERSION
from precis.taxonomy.freeze import (
    diff_lists,
    freeze,
    next_version,
    read_list,
    write_list,
)
from precis.taxonomy.types import DimensionSpec, ListEntry, Snapshot, Thresholds

# ── fixtures ──


def _snapshot(row_count: int = 100, sha256: str = "abc123") -> Snapshot:
    return Snapshot(
        source="test",
        row_count=row_count,
        sha256=sha256,
        pulled_at="2026-09-28T00:00:00Z",
        text_field="text",
        ref_field="ref_id",
    )


def _dim(si_vector: str = "0,0,0,0,0,0,0") -> DimensionSpec:
    return DimensionSpec(kind="si", si_vector=si_vector)


def _entry(
    key: str = "applied-potential",
    *,
    canonical_unit: str | None = "V",
    hub_count: int = 40,
    required_conditions: tuple[str, ...] = ("ph",),
    dimension: DimensionSpec | None = None,
    reference_state: str = "rhe",
    convention: str | None = None,
) -> ListEntry:
    return ListEntry(
        key=key,
        label=key.replace("-", " "),
        dimension=dimension or _dim(),
        canonical_unit=canonical_unit,
        convention=convention,
        allowed_reference_states=(reference_state,),
        required_conditions=required_conditions,
        hub_count=hub_count,
        paper_count=5,
        contributing_hubs=(1, 2, 3),
    )


# ── write_list / read_list round trip ──


def test_write_then_read_round_trips_equal(tmp_path: Path) -> None:
    lst = freeze(
        [_entry(), _entry(key="overpotential", canonical_unit="mV")],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
        stability=0.9,
        rejected=(("unused-term", "below hub threshold"),),
    )
    path = write_list(lst, tmp_path)
    reloaded = read_list(path)
    assert reloaded == lst


def test_read_list_defaults_the_probe_ratio_for_a_list_frozen_before_it(
    tmp_path: Path,
) -> None:
    lst = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
        stability=0.9,
    )
    path = write_list(lst, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["thresholds"].pop("min_probe_ratio") == 0.60
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert read_list(path).thresholds == Thresholds()


def test_write_then_read_round_trips_convention_field(tmp_path: Path) -> None:
    """``ListEntry`` gained ``convention`` alongside the ``HubCount`` move;
    ``_entry_from_json`` must read it back or a convention split silently
    loses which convention an entry is on across a write/read cycle."""
    lst = freeze(
        [_entry("onset-potential", convention="iupac")],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    path = write_list(lst, tmp_path)
    reloaded = read_list(path)
    assert reloaded == lst
    assert reloaded.entries[0].convention == "iupac"


def test_written_yaml_is_sorted_and_parses_with_safe_load(tmp_path: Path) -> None:
    lst = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    path = write_list(lst, tmp_path)
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    # sort_keys=True: top-level document keys appear in sorted order.
    top_keys = list(data.keys())
    assert top_keys == sorted(top_keys)


# ── write_list refuses to overwrite; next_version ──


def test_write_list_raises_when_target_exists(tmp_path: Path) -> None:
    lst = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    write_list(lst, tmp_path)
    try:
        write_list(lst, tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("write_list did not raise on an existing target")


def test_write_list_refuses_preexisting_file_leaving_it_byte_identical(
    tmp_path: Path,
) -> None:
    """Reviewer finding 7: ``exists()`` then ``write_text()`` is a TOCTOU
    gap that also risks truncating a file that was already there (e.g. a
    hand-placed or foreign file at that path). A refusal must be a pure
    refusal — the pre-existing bytes must be untouched."""
    path = tmp_path / "list.v1.yaml"
    path.write_text("not a real list\n", encoding="utf-8")
    before = path.read_bytes()

    lst = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    try:
        write_list(lst, tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("write_list did not raise on a pre-existing file")
    assert path.read_bytes() == before

    # No leaked temp file either.
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_next_version_empty_dir_is_one(tmp_path: Path) -> None:
    assert next_version(tmp_path / "nonexistent") == 1
    tmp_path.mkdir(exist_ok=True)
    assert next_version(tmp_path) == 1


def test_next_version_after_v1_and_v2_is_three(tmp_path: Path) -> None:
    lst1 = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    write_list(lst1, tmp_path)
    lst2 = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=2,
    )
    write_list(lst2, tmp_path)
    assert next_version(tmp_path) == 3


# ── written document contents ──


def test_document_contains_provenance_and_thresholds(tmp_path: Path) -> None:
    snapshot = _snapshot(row_count=1664, sha256="e4fa6046")
    thresholds = Thresholds()
    lst = freeze(
        [_entry()],
        campaign="norr-her-meta",
        snapshot=snapshot,
        thresholds=thresholds,
        version=1,
    )
    path = write_list(lst, tmp_path, census_digest="census-sha-xyz")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["snapshot"]["sha256"] == "e4fa6046"
    assert data["snapshot"]["row_count"] == 1664
    assert data["snapshot"]["pulled_at"] == "2026-09-28T00:00:00Z"
    for key in (
        "min_papers",
        "min_hubs",
        "min_join_side",
        "min_stability",
        "min_probe_ratio",
        "max_escape_rate",
        "require_both_halves",
        "require_single_dimension",
    ):
        assert key in data["thresholds"]
    assert data["procedure_version"] == PROCEDURE_VERSION
    assert data["generated_at"].endswith("Z")
    assert data["census_digest"] == "census-sha-xyz"


def test_census_digest_omitted_when_not_supplied(tmp_path: Path) -> None:
    lst = freeze(
        [_entry()],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    path = write_list(lst, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "census_digest" not in data


# ── AC4: diff_lists on a grown snapshot ──


def test_diff_lists_ac4_one_extra_entry_on_a_grown_snapshot(tmp_path: Path) -> None:
    v1_entries = [
        _entry("applied-potential"),
        _entry("overpotential", canonical_unit="mV"),
    ]
    v1 = freeze(
        v1_entries,
        campaign="test",
        snapshot=_snapshot(row_count=100),
        thresholds=Thresholds(),
        version=1,
    )
    v1_path = write_list(v1, tmp_path)
    v1_bytes_before = v1_path.read_bytes()

    v2_entries = [*v1_entries, _entry("tafel-slope", canonical_unit="mV/dec")]
    v2 = freeze(
        v2_entries,
        campaign="test",
        snapshot=_snapshot(row_count=110),
        thresholds=Thresholds(),
        version=next_version(tmp_path),
    )
    write_list(v2, tmp_path)

    diff = diff_lists(v1, v2)
    assert diff.added == ("tafel-slope",)
    assert diff.removed == ()
    assert diff.changed == ()
    assert set(diff.unchanged) == {"applied-potential", "overpotential"}
    assert "tafel-slope" in diff.render()

    # v1 on disk was not modified by writing v2.
    assert v1_path.read_bytes() == v1_bytes_before
    assert read_list(v1_path) == v1


def test_diff_lists_key_collision_does_not_drop_a_sibling_entry() -> None:
    """Reviewer finding 4: two entries share a key (``applied-potential``)
    but differ in reference state — the `TOF`-style split AC5 requires.
    Keying the diff's internal maps by ``entry.key`` collapses both onto one
    dict slot and drops one silently; a rerun that changes only the dropped
    entry then reports an empty diff. Assert the change is caught and named
    with enough to tell the two siblings apart."""
    rhe = _entry("applied-potential", reference_state="rhe", hub_count=40)
    ag_agcl = _entry("applied-potential", reference_state="ag-agcl", hub_count=20)
    old = freeze(
        [rhe, ag_agcl],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    ag_agcl_grown = _entry("applied-potential", reference_state="ag-agcl", hub_count=25)
    new = freeze(
        [rhe, ag_agcl_grown],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=2,
    )
    diff = diff_lists(old, new)
    assert diff.added == ()
    assert diff.removed == ()
    assert len(diff.changed) == 1
    name, what = diff.changed[0]
    assert "applied-potential" in name
    assert "ag-agcl" in name  # names which of the two siblings moved
    assert "hub_count 20 -> 25" in what
    # The unchanged rhe sibling must still be reported, not silently dropped.
    assert len(diff.unchanged) == 1
    assert "applied-potential" in diff.unchanged[0]
    assert "rhe" in diff.unchanged[0]
    rendered = diff.render()
    assert "ag-agcl" in rendered


def test_diff_lists_detects_changed_unit_and_required_conditions() -> None:
    old = freeze(
        [
            _entry(
                "yield-rate",
                canonical_unit="mol h-1 cm-2",
                required_conditions=("feed",),
            )
        ],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=1,
    )
    new = freeze(
        [
            _entry(
                "yield-rate",
                canonical_unit="mol h-1 g-1",
                required_conditions=("feed", "electrolyte"),
            )
        ],
        campaign="test",
        snapshot=_snapshot(),
        thresholds=Thresholds(),
        version=2,
    )
    diff = diff_lists(old, new)
    assert diff.added == ()
    assert diff.removed == ()
    assert len(diff.changed) == 1
    key, what = diff.changed[0]
    assert key == "yield-rate"
    assert "canonical_unit" in what
    assert "required_conditions" in what
