"""se profiles (gr415062): a part's ``profile`` key resolves to the
tabulated section properties; unknown/ambiguous names fail legibly."""

from __future__ import annotations

import dataclasses

import pytest

from precis.errors import BadInput
from precis_se import profiles


def test_part_profile_resolves_to_tabulated_properties() -> None:
    p = profiles.part_profile({"name": "rail", "profile": "40x40"})
    assert p is not None
    assert p.part_number == "HFS8-4040"
    assert (p.area_mm2, p.ix_mm4, p.iy_mm4, p.mass_kg_per_m) == (
        640,
        104000,
        104000,
        1.73,
    )
    assert p.area_m2 == pytest.approx(6.4e-4)
    assert p.ix_m4 == pytest.approx(1.04e-7)


def test_part_without_profile_is_none() -> None:
    assert profiles.part_profile({"name": "rail"}) is None


def test_part_number_case_insensitive() -> None:
    assert profiles.resolve("hfs6-3030").part_number == "HFS6-3030"


def test_all_series_seeded_and_unpublished_is_none() -> None:
    sizes = {p.width_mm for p in profiles.profiles()}
    assert sizes == {20, 30, 40, 45, 60, 80}
    assert profiles.resolve("45x45").bore_diameter_mm is None


def test_unknown_profile_errors_legibly() -> None:
    with pytest.raises(BadInput, match=r"unknown profile '50x50'.*HFS8-4040"):
        profiles.resolve("50x50")


def test_ambiguous_alias_refused() -> None:
    base = profiles.resolve("40x40")
    other = dataclasses.replace(base, part_number="X-4040", slot_width_mm=8)
    with pytest.raises(BadInput, match=r"ambiguous.*HFS8-4040, X-4040"):
        profiles.resolve_in("40x40", (base, other))
    assert profiles.resolve_in("X-4040", (base, other)) is other
