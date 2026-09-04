"""gr311344 / gr311679: bare ``get`` on a heavily-linked numeric ref hung.

``NumericRefHandler.get`` appended ``_render_links_section(ref)`` with
no cap, so ``render_links_section``'s default ``limit=None`` rendered
*every* link on the request thread — a live quest with ~1900 links
made a bare ``get`` unbounded work. Fixed by capping the bare-``get``
callsite the same way ``handlers/paper.py``'s overview append already
does (``DEFAULT_LINK_ROW_CAP``, ``priority=True``), keeping the existing
overflow affordance ("+N more · view='links'") so the full graph stays
reachable via ``get(..., view='links')``. gr311679 named the shared
literal ``DEFAULT_LINK_ROW_CAP`` (bumped 12 -> 20) so both capped
callsites can't drift apart.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers._links_render import DEFAULT_LINK_ROW_CAP
from precis.handlers.memory import MemoryHandler
from precis.store import Store


def _mk_memory(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


class TestNumericRefBareGetLinksCap:
    def test_bare_get_caps_links_section_and_shows_overflow(self, hub: Hub) -> None:
        store = hub.live_store
        subject = _mk_memory(store, "subject note")
        # 40 outbound + 40 inbound links — well over the limit=12 cap in
        # both directions, mirroring gr311344's ~1900-link quest.
        for i in range(40):
            target = _mk_memory(store, f"outbound target {i}")
            store.add_link(src_ref_id=subject, dst_ref_id=target, relation="related-to")
        for i in range(40):
            source = _mk_memory(store, f"inbound source {i}")
            store.add_link(src_ref_id=source, dst_ref_id=subject, relation="related-to")

        handler = MemoryHandler(hub=hub)
        resp = handler.get(id=subject)

        # Capped header, not the plain "Links:" — the truncation is
        # visible rather than silent.
        assert f"Links ({DEFAULT_LINK_ROW_CAP} of 80):" in resp.body
        # The overflow line points at the full-graph escape hatch.
        assert f"+{80 - DEFAULT_LINK_ROW_CAP} more ·" in resp.body
        assert "view='links')" in resp.body
        # Only a fraction of the 80 targets/sources are enumerated —
        # bare get must not render all of them.
        rendered_titles = sum(
            1
            for i in range(40)
            if f"outbound target {i}" in resp.body or f"inbound source {i}" in resp.body
        )
        assert rendered_titles <= DEFAULT_LINK_ROW_CAP

    def test_bare_get_under_cap_keeps_plain_header(self, hub: Hub) -> None:
        store = hub.live_store
        subject = _mk_memory(store, "subject note under cap")
        target = _mk_memory(store, "one linked note")
        store.add_link(src_ref_id=subject, dst_ref_id=target, relation="related-to")

        handler = MemoryHandler(hub=hub)
        resp = handler.get(id=subject)

        assert "Links:" in resp.body
        assert "Links (" not in resp.body  # no truncation header
        assert "more ·" not in resp.body
        assert "one linked note" in resp.body

    def test_view_links_still_reaches_the_full_graph(self, hub: Hub) -> None:
        store = hub.live_store
        subject = _mk_memory(store, "subject note full graph")
        target_ids = []
        for i in range(20):
            target_ids.append(_mk_memory(store, f"linked note {i}"))
            store.add_link(
                src_ref_id=subject, dst_ref_id=target_ids[-1], relation="related-to"
            )

        handler = MemoryHandler(hub=hub)
        resp = handler.get(id=subject, view="links")

        # NumericRefHandler's own ``view='links'`` (handle-only rows, no
        # title teaser column) is not capped — every link is still
        # reachable by handle, just not via the default bare get.
        for target_id in target_ids:
            assert f"me{target_id}" in resp.body

    def test_view_raw_links_section_stays_uncapped(self, hub: Hub) -> None:
        """``view='raw'`` is documented as "hides nothing" — it must keep
        rendering every link, not just the bare-get cap."""
        store = hub.live_store
        subject = _mk_memory(store, "subject note raw view")
        for i in range(20):
            target = _mk_memory(store, f"raw-linked note {i}")
            store.add_link(src_ref_id=subject, dst_ref_id=target, relation="related-to")

        handler = MemoryHandler(hub=hub)
        resp = handler.get(id=subject, view="raw")

        assert "Links (" not in resp.body  # no truncation header
        for i in range(20):
            assert f"raw-linked note {i}" in resp.body
