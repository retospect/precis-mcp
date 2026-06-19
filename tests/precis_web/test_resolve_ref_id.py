"""Resolver mapping for the ``/r/{kind}/{id}`` click-through.

Covers the numeric-ref_id fallback for slug kinds (papers), which the
``/clusters`` word/grid links depend on: they hand the resolver the bare
``cluster_assignments.ref_id`` rather than a cite_key slug.
"""

from __future__ import annotations

from precis.store import Store
from precis_web.routes.preview import _resolve_ref_id


def _make_paper(store: Store, title: str, cite_key: str | None = None) -> int:
    with store.pool.connection() as conn:
        ref_id = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', %s) RETURNING ref_id",
            (title,),
        ).fetchone()[0]
        if cite_key is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value) "
                "VALUES (%s, 'cite_key', %s)",
                (ref_id, cite_key),
            )
        conn.commit()
    return int(ref_id)


def test_resolves_paper_by_cite_key_slug(store: Store) -> None:
    ref_id = _make_paper(store, "Attention", cite_key="vaswani2017")
    assert _resolve_ref_id(store, "paper", "vaswani2017") == ref_id


def test_resolves_paper_by_numeric_ref_id(store: Store) -> None:
    # The /clusters links pass the numeric ref_id, not a slug — this is
    # the path that 404'd with "no such paper:<id>" before the fallback.
    ref_id = _make_paper(store, "Diffusion", cite_key="ho2020")
    assert _resolve_ref_id(store, "paper", str(ref_id)) == ref_id


def test_numeric_fallback_requires_existing_paper(store: Store) -> None:
    assert _resolve_ref_id(store, "paper", "99999999") is None


def test_numeric_fallback_is_kind_scoped(store: Store) -> None:
    # A numeric id that is a paper must not resolve under a different
    # slug kind — the fallback is verified against refs.kind.
    ref_id = _make_paper(store, "Graphs")
    assert _resolve_ref_id(store, "patent", str(ref_id)) is None


def test_unknown_slug_returns_none(store: Store) -> None:
    assert _resolve_ref_id(store, "paper", "no-such-slug") is None
