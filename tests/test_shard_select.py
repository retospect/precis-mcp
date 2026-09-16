"""``--shard K/N`` (tests/conftest.py) — the CI fan-out must be an exact,
stable partition: every test lands in exactly one shard, and the same test
lands in the same shard on every machine and in every xdist worker."""

from __future__ import annotations

import itertools
import zlib

import pytest

from tests.conftest import parse_shard, shard_index

_IDS = [
    "tests/test_cad_probe.py::test_ray_hits_sphere",
    "tests/test_worker_cli.py::TestRefPassPriority::test_real_work_outranks_background_fetch",
    "tests/precis_web/test_routes.py::test_index[light]",
    "tests/test_se_atomic_angstrom_seam.py::test_kernel_scale[1e-09-True]",
    "tests/test_alerts.py::test_open_alert_query",
]


@pytest.mark.parametrize("n", [1, 2, 6, 7])
def test_shards_partition_exactly(n: int) -> None:
    buckets = [shard_index(i, n) for i in _IDS]
    assert all(0 <= b < n for b in buckets)
    # Every id is in exactly one shard: the per-shard keep-sets are disjoint
    # and their union is the whole list.
    kept = [[i for i in _IDS if shard_index(i, n) == k] for k in range(n)]
    assert sorted(itertools.chain.from_iterable(kept)) == sorted(_IDS)


def test_shard_index_is_crc32_not_salted_hash() -> None:
    # A salted str hash() would move tests between shards per process and
    # break xdist's collection-consistency check; crc32 is the contract.
    nodeid = "tests/test_x.py::test_y"
    assert shard_index(nodeid, 6) == zlib.crc32(nodeid.encode("utf-8")) % 6


def test_parse_shard_accepts_k_of_n() -> None:
    assert parse_shard("1/6") == (1, 6)
    assert parse_shard("6/6") == (6, 6)
    assert parse_shard("1/1") == (1, 1)


@pytest.mark.parametrize("bad", ["0/6", "7/6", "1/0", "1", "a/b", "2/"])
def test_parse_shard_rejects_out_of_range(bad: str) -> None:
    with pytest.raises(pytest.UsageError):
        parse_shard(bad)
