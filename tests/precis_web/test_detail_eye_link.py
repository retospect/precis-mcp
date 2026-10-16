"""Every native detail page carries exactly one "Eye navigation" link to
``/eye/<handle>`` — the reverse of the eye focus page's "open in its
reader" (gr492464). The link is one shared partial
(``templates/_eye_link.html.j2``) rendered by ``base.html.j2`` when the
``eye_link`` context processor (``precis_web/eye_nav.py``) resolves
``eye_handle`` for the route.

The iteration is the registered stored kinds — every kind in the
``kinds`` vocab table whose ``<code><id>`` record handle ``/eye``
accepts — each seeded bare and fetched through its canonical
``ref_url``. Kinds whose canonical URL isn't a detail page (folder →
Drive, todo → the dashboard, profile → /account) drop out on the URL
shape; kinds with no refs-backed record code (codeless providers,
file-backed skill/python/md, other-table tag) never get a handle and
aren't in the set.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.ref_urls import ref_url

pytestmark = pytest.mark.db

#: Stored kinds whose ``<code><id>`` parses but does not address the
#: refs row: ``measure`` ids are ``measures.id`` (never ``resolve_handle``,
#: says handle_registry), so ``mx<ref_id>`` would point at an unrelated
#: measure — /refs/measure/<ref_id> is not a page a handle can back.
_NO_REFS_HANDLE = frozenset({"measure"})


@pytest.fixture
def client(runtime_with_store: Any) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def _stored_detail_kinds(store: Any, hub: Any) -> list[tuple[str, bool]]:
    """``(kind, is_numeric)`` for every kind that is stored in ``refs``,
    has a record handle ``/eye`` accepts, and is registered (in the
    ``kinds`` vocab table *and* handled by the running hub — a kind
    whose handler this build doesn't load can still render its stored
    card, but "registered kinds" is the spec's set)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT slug, is_numeric FROM kinds ORDER BY slug"
        ).fetchall()
    registered = set(hub.handlers)
    out = []
    for slug, is_numeric in rows:
        if slug not in registered or slug in _NO_REFS_HANDLE:
            continue
        probe = handle_registry.try_format(slug, 1)
        parsed = handle_registry.parse(probe) if probe else None
        if parsed is None or parsed[1]:
            continue
        out.append((slug, bool(is_numeric)))
    return out


def _seed_ref(store: Any, kind: str, is_numeric: bool) -> Any:
    """A bare ref of ``kind``. ``gripe`` is the one kind with a deferred
    constraint trigger (exactly one STATUS tag at COMMIT, migration
    0176), so it takes ref + tag in one connection."""
    slug = None if is_numeric else f"eye-probe-{kind}"
    title = f"{kind} eye-link probe"
    if kind == "gripe":
        from precis.store.types import Tag

        with store.pool.connection() as conn:
            ref = store.insert_ref(kind=kind, slug=slug, title=title, conn=conn)
            store.add_tag(
                ref.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn
            )
        return ref
    return store.insert_ref(kind=kind, slug=slug, title=title)


def test_every_native_detail_page_has_one_eye_link(
    client: TestClient, runtime_with_store: Any, store: Any
) -> None:
    failures: list[str] = []
    seen: list[str] = []
    for kind, is_numeric in _stored_detail_kinds(store, runtime_with_store.hub):
        try:
            ref = _seed_ref(store, kind, is_numeric)
        except Exception as e:
            # A registered stored kind that can't take a bare ref is a
            # seeding gap in this probe, not a pass — surface it.
            failures.append(f"{kind}: seed failed: {e}")
            continue
        url = ref_url(kind, ref.id, ref.slug)
        # The canonical address is a detail page only when the ref sits
        # in the URL path — ``/drive?folder=<id>``, ``/todo?focus=<id>``
        # and ``/account`` are list/dashboard surfaces, not detail pages.
        tail = url.split("?")[0]
        if not (
            tail.endswith(f"/{ref.id}") or (ref.slug and tail.endswith(f"/{ref.slug}"))
        ):
            continue
        handle = handle_registry.format_handle(kind, ref.id)
        page = client.get(url)
        if page.status_code != 200:
            failures.append(f"{kind} {url} -> {page.status_code}")
            continue
        links = re.findall(r'href="/eye/([^"]+)"', page.text)
        if links != [handle]:
            failures.append(f"{kind} {url}: eye links {links}, want [{handle}]")
        seen.append(kind)
    assert seen, "no stored kinds iterated — the probe is vacuous"
    assert not failures, (
        "detail pages missing or duplicating the eye link: " + "; ".join(failures)
    )


def test_non_detail_pages_carry_no_eye_link(client: TestClient, store: Any) -> None:
    """The inverse guard: list/dashboard pages stay clean (the partial is
    included from base.html.j2, so this proves the gate works)."""
    for path in ("/drive", "/tags", "/refs/memory", "/eye/"):
        page = client.get(path)
        assert page.status_code == 200
        assert not re.findall(r'href="/eye/[^"]+"', page.text), path
