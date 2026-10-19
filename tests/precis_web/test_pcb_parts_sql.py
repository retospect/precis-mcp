"""Real-PG companion for ``precis_web/pcb_parts.py``'s raw SQL.

``thumbnail`` persists its resolved (or missed) thumbnail onto the lazily
minted part ref with a raw ``UPDATE refs SET meta=%s WHERE ref_id=%s``.
The FakeStore suite never parses SQL (the conftest caveat that spawns
every ``test_*_sql.py``), so that statement — and the
``ensure_part_ref``/``get_ref`` path around it — runs here against the
live ``store`` fixture. ``fetch`` is injected; production's
``_fetch_thumbnail`` (the network path) is exercised by the polite-call
ratchet and test_pcb.py's seam check instead.
"""

from __future__ import annotations

import base64

from precis_web import pcb_parts

_LCSC = "C246810"


def _catalogue(store, lcsc: str = _LCSC) -> None:
    """A ``parts`` row, so ``ensure_part_ref`` may mint the part ref."""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO parts (lcsc, mfr, mfr_part, description, package, stock)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (lcsc, "Yageo", "RC0402FR-0710KL", "10k resistor", "0402", 5000),
        )


def test_thumbnail_writes_and_reads_back_meta_on_real_pg(store) -> None:
    """The hit path: mint the ref, run the UPDATE, serve the cache."""
    _catalogue(store)
    calls: list[str] = []

    def fetch(lcsc: str) -> tuple[bytes, str]:
        calls.append(lcsc)
        return (b"\x89PNG-thumb", "image/png")

    assert pcb_parts.thumbnail(store, _LCSC, fetch=fetch) == (
        b"\x89PNG-thumb",
        "image/png",
    )
    ref_id = store.part_ref_id(_LCSC)
    assert ref_id is not None
    meta = store.get_ref(kind="part", id=ref_id).meta
    assert meta["thumb"]["mime"] == "image/png"
    assert base64.b64decode(meta["thumb"]["b64"]) == b"\x89PNG-thumb"

    # The UPDATE persisted: the second ask reads meta and never refetches.
    assert pcb_parts.thumbnail(store, _LCSC, fetch=fetch) == (
        b"\x89PNG-thumb",
        "image/png",
    )
    assert calls == [_LCSC]


def test_thumbnail_caches_a_miss_on_real_pg(store) -> None:
    """The miss path runs the same UPDATE with ``{"miss": true}``."""
    _catalogue(store)

    def no_fetch(lcsc: str) -> None:
        raise AssertionError("miss was not cached")

    assert pcb_parts.thumbnail(store, _LCSC, fetch=lambda lcsc: None) is None
    ref_id = store.part_ref_id(_LCSC)
    assert ref_id is not None
    assert store.get_ref(kind="part", id=ref_id).meta["thumb"] == {"miss": True}
    assert pcb_parts.thumbnail(store, _LCSC, fetch=no_fetch) is None


def test_thumbnail_uncatalogued_lcsc_touches_nothing(store) -> None:
    """No ``parts`` row → ``ensure_part_ref`` raises NotFound → no ref, no
    UPDATE, and the fetch seam is never reached."""
    assert pcb_parts.thumbnail(store, "C000001", fetch=lambda lcsc: None) is None
    assert store.part_ref_id("C000001") is None
