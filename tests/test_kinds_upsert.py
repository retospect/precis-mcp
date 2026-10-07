"""Tests for the boot-time ``kinds`` + ``kind_provider`` upsert.

The boot of :func:`precis.dispatch.boot` is exercised end-to-end in
``test_dispatch.py``; here we narrow to the pure-store behaviour:

* :meth:`Store.upsert_kinds` is idempotent and overwrites
  title/description on re-run.
* :meth:`Store.upsert_kind_providers` keys on
  ``(slug, host, process)`` and refreshes ``last_seen``.
* :meth:`Store.find_kind_providers` filters by freshness.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.store import Store
from precis.store._kinds_ops import boot_process_identity


@dataclass(frozen=True)
class _FakeSpec:
    """Minimal stand-in for KindSpec — only the fields upsert_kinds reads."""

    kind: str
    is_numeric: bool
    title: str
    description: str


def _kinds_row(store: Store, slug: str) -> tuple[bool, str, str] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT is_numeric, title, description FROM kinds WHERE slug = %s",
            (slug,),
        ).fetchone()
    if row is None:
        return None
    return (bool(row[0]), str(row[1]), str(row[2]))


def test_upsert_kinds_inserts_new(store: Store) -> None:
    spec = _FakeSpec(
        kind="testkind-a",
        is_numeric=False,
        title="Test Kind A",
        description="An invented kind for the upsert test.",
    )
    n = store.upsert_kinds([spec])
    assert n == 1
    got = _kinds_row(store, "testkind-a")
    assert got == (False, "Test Kind A", "An invented kind for the upsert test.")


def test_upsert_kinds_overwrites_metadata(store: Store) -> None:
    first = _FakeSpec(
        kind="testkind-b",
        is_numeric=False,
        title="First title",
        description="First description",
    )
    store.upsert_kinds([first])
    second = _FakeSpec(
        kind="testkind-b",
        is_numeric=False,
        title="Second title",
        description="Updated description",
    )
    store.upsert_kinds([second])
    assert _kinds_row(store, "testkind-b") == (
        False,
        "Second title",
        "Updated description",
    )


def test_upsert_kinds_no_specs_returns_zero(store: Store) -> None:
    assert store.upsert_kinds([]) == 0


def test_upsert_kind_providers_records_host_process(store: Store) -> None:
    spec = _FakeSpec(kind="testkind-c", is_numeric=False, title="C", description="c")
    store.upsert_kinds([spec])
    store.upsert_kind_providers([spec], host="alpha", process="precis-test")
    hosts = store.find_kind_providers("testkind-c")
    assert hosts == ["alpha"]


def test_upsert_kind_providers_dedups_by_pk(store: Store) -> None:
    spec = _FakeSpec(kind="testkind-d", is_numeric=False, title="D", description="d")
    store.upsert_kinds([spec])
    store.upsert_kind_providers([spec], host="alpha", process="precis-test")
    store.upsert_kind_providers([spec], host="alpha", process="precis-test")
    # second upsert refreshes last_seen on the same PK; one host returned.
    assert store.find_kind_providers("testkind-d") == ["alpha"]


def test_upsert_kind_providers_skips_unknown_process(store: Store) -> None:
    """gr452084 defect 4: a ``process="unknown"`` boot (PRECIS_PROCESS unset —
    throwaway container / local dev) writes no rows, so it can't accrue as
    ballast or fold into a bogus kind-shrinkage pair."""
    spec = _FakeSpec(kind="testkind-u", is_numeric=False, title="U", description="u")
    store.upsert_kinds([spec])
    n = store.upsert_kind_providers([spec], host="070b9620ec08", process="unknown")
    assert n == 0
    assert store.find_kind_providers("testkind-u", max_age_seconds=24 * 3600) == []


def test_upsert_kind_providers_skips_container_id_host(store: Store) -> None:
    """gr461595: a 12-hex container-id host upserts nothing; a normal
    hostname (even hex-ish but not 12 chars) still does."""
    spec = _FakeSpec(kind="testkind-h", is_numeric=False, title="H", description="h")
    store.upsert_kinds([spec])
    assert (
        store.upsert_kind_providers([spec], host="070b9620ec08", process="precis-test")
        == 0
    )
    assert store.find_kind_providers("testkind-h", max_age_seconds=24 * 3600) == []
    store.upsert_kind_providers([spec], host="melchior", process="precis-test")
    assert store.find_kind_providers("testkind-h") == ["melchior"]


def test_prune_kind_providers_drops_aged_rows(store: Store) -> None:
    """gr452084 defect 4: rows for a (host, process) that stopped booting age
    past the retention window and get pruned; a fresh row stays."""
    fresh = _FakeSpec(kind="testkind-f", is_numeric=False, title="F", description="f")
    gone = _FakeSpec(kind="testkind-g", is_numeric=False, title="G", description="g")
    store.upsert_kinds([fresh, gone])
    store.upsert_kind_providers([fresh], host="live", process="precis-test")
    store.upsert_kind_providers([gone], host="dead", process="precis-test")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE kind_provider SET last_seen = now() - interval '40 days' "
            "WHERE slug = %s",
            ("testkind-g",),
        )
        conn.commit()
    # Global count (the shared precis_test DB may hold other aged rows), so
    # assert on the specific slugs rather than the exact delete total.
    deleted = store.prune_kind_providers(retention_days=30)
    assert deleted >= 1
    assert store.find_kind_providers("testkind-g", max_age_seconds=10**9) == []
    assert store.find_kind_providers("testkind-f", max_age_seconds=10**9) == ["live"]
    # A disabled window is a no-op.
    assert store.prune_kind_providers(retention_days=0) == 0


def test_find_kind_providers_filters_stale(store: Store) -> None:
    spec = _FakeSpec(kind="testkind-e", is_numeric=False, title="E", description="e")
    store.upsert_kinds([spec])
    store.upsert_kind_providers([spec], host="beta", process="precis-test")
    # Backdate the row so the freshness cutoff drops it.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE kind_provider SET last_seen = now() - interval '2 hours' "
            "WHERE slug = %s",
            ("testkind-e",),
        )
        conn.commit()
    assert store.find_kind_providers("testkind-e", max_age_seconds=3600) == []
    # Generous cutoff returns it.
    assert store.find_kind_providers("testkind-e", max_age_seconds=24 * 3600) == [
        "beta"
    ]


def test_boot_process_identity_falls_back_to_hostname(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    monkeypatch.delenv("PRECIS_HOST_NAME", raising=False)
    host, process = boot_process_identity()
    assert host  # hostname is non-empty
    assert process == "unknown"
