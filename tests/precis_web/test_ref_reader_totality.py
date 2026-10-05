"""Real-store navigation across registered and Drive-offered kinds (gr469093)."""

from html.parser import HTMLParser
from types import SimpleNamespace

from fastapi.testclient import TestClient

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis.store.types import ChunkInsert, Tag
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.ref_urls import ref_url
from precis_web.routes.refs import _REFS_BROWSABLE_KINDS

AFFECTED = set(
    [
        "orcid",
        "news",
        "agentlog",
        "semanticscholar",
        "draft",
        "tex",
        "taxon",
        "se",
        "llm",
        "folder",
        "concept",
        "wikipedia",
        "figure",
        "cfp",
        "markdown",
        "cad",
        "mermaid",
        "plaintext",
        "plan",
        "protein",
        "make",
        "estimate",
    ]
)


class KindInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.kinds = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "k":
            self.kinds.add(attrs["value"])


def test_registered_and_drive_kinds_have_readers(store, tmp_path):
    hub = boot(store=store, precis_root=tmp_path)
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    with TestClient(
        create_app(runtime=runtime, web_config=WebConfig(corpus_dir=tmp_path))
    ) as client:
        inventory = KindInputs()
        inventory.feed(client.get("/drive").text)
        with store.pool.connection() as conn:
            numeric = dict(
                conn.execute("SELECT slug, is_numeric FROM kinds").fetchall()
            )
        kinds = set(numeric) | hub.kinds | inventory.kinds | AFFECTED
        assert hub.kinds <= inventory.kinds
        for kind in sorted(kinds):
            # Empty designs are valid reader fixtures; slug readers must be
            # exercised with an actual stored slug, not only generic fallback.
            slug = f"reader-totality-{kind}" if not numeric[kind] else None
            with store.tx() as conn:
                ref = store.insert_ref(
                    kind=kind, slug=slug, title=f"Reader totality {kind}", conn=conn
                )
                if kind == "gripe":
                    store.add_tag(ref.id, Tag.closed("STATUS", "open"), conn=conn)
            expected = ref_url(kind, ref.id, ref.slug)
            if kind in {"markdown", "plaintext", "tex"}:
                store.chunks.insert_chunks(
                    ref.id,
                    [ChunkInsert(ord=0, text="Stored snapshot survives missing file")],
                )
            response = client.get(f"/refs/{kind}/{ref.id}", follow_redirects=False)
            assert response.status_code in (200, 303), (kind, response.text[:300])
            if expected != f"/refs/{kind}/{ref.id}":
                assert response.status_code == 303
                assert response.headers["location"] == expected
            assert client.get(expected).status_code == 200, (kind, expected)
            if kind in {"markdown", "plaintext", "tex"}:
                assert "Stored snapshot survives missing file" in response.text
            # Exercise the rendered Drive row, not only its helper.
            drive = client.get(
                f"/drive?submitted=1&sort=created&k={kind}&state=all&paper_chunks=both"
            )
            assert drive.status_code == 200
            assert f'href="{expected}"' in drive.text, kind
            resolver = client.get(f"/r/{kind}/{ref.id}", follow_redirects=False)
            assert resolver.status_code == 303, (kind, resolver.text[:300])
            assert resolver.headers["location"] == expected
            pivot = client.get(f"/tags/refs?kind={kind}")
            assert pivot.status_code == 200
            assert f'href="{expected}"' in pivot.text, kind


def test_orcid_person_and_held_papers_are_read_only(client, runtime, monkeypatch):
    from tests.precis_web.conftest import make_ref

    author = make_ref(
        id=9001,
        kind="orcid",
        title="Ada Researcher",
        slug="orcid:0000-0002-1825-0097",
        meta={"orcid_id": "0000-0002-1825-0097"},
    )
    paper = make_ref(id=9002, kind="paper", title="Held author paper")
    refs = {author.id: author, paper.id: paper}
    monkeypatch.setattr(
        runtime.store,
        "fetch_refs_by_ids",
        lambda ids, **kw: {i: refs[i] for i in ids if i in refs},
    )
    monkeypatch.setattr(
        runtime.store,
        "links_for",
        lambda id, **kw: (
            [
                SimpleNamespace(src_ref_id=9001, dst_ref_id=9002),
                SimpleNamespace(src_ref_id=9001, dst_ref_id=9003),
            ]
            if kw.get("relation") == "authored"
            else []
        ),
    )
    response = client.get("/refs/orcid/9001")
    assert response.status_code == 200
    assert "Ada Researcher" in response.text
    assert 'href="https://orcid.org/0000-0002-1825-0097"' in response.text
    assert 'href="/papers/9002"' in response.text
    assert "Held author paper" in response.text
    assert "9003" not in response.text
    assert not runtime.calls


def test_browse_roster_is_not_a_detail_gate(client, runtime, monkeypatch):
    from tests.precis_web.conftest import make_ref

    ref = make_ref(kind="future-plugin", id=9000)
    assert ref.kind not in _REFS_BROWSABLE_KINDS
    monkeypatch.setattr(
        runtime.store,
        "fetch_refs_by_ids",
        lambda ids, **kw: {ref.id: ref} if ref.id in ids else {},
    )
    monkeypatch.setattr(
        runtime.store,
        "list_chunks_for_ref",
        lambda id, **kw: [SimpleNamespace(text="Optional plugin stored body")],
    )
    monkeypatch.setattr(
        runtime,
        "dispatch_with_status",
        lambda verb, args: ("[error:NotFound] unknown kind: future-plugin", True),
    )
    response = client.get("/refs/future-plugin/9000")
    assert response.status_code == 200
    assert "Optional plugin stored body" in response.text
    assert "[error:NotFound]" not in response.text
    assert client.get("/refs/wrong-kind/9000").status_code == 400


def test_orcid_includes_inverse_authorship_and_omits_retired_papers(
    runtime_with_store, tmp_path
):
    store = runtime_with_store.store
    author = store.insert_ref(
        kind="orcid", slug="orcid:0000-0002-1825-0097", title="Held researcher"
    )
    held = store.insert_ref(
        kind="paper", slug="held-authored-paper", title="Inverse authored paper"
    )
    retired = store.insert_ref(
        kind="paper", slug="retired-authored-paper", title="Retired authored paper"
    )
    store.add_link(
        src_ref_id=held.id,
        dst_ref_id=author.id,
        relation="authored-by",
        set_by="system",
    )
    store.add_link(
        src_ref_id=author.id,
        dst_ref_id=retired.id,
        relation="authored",
        set_by="system",
    )
    store.retire_ref(retired.id)
    with TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    ) as client:
        response = client.get(f"/refs/orcid/{author.id}")
    assert response.status_code == 200
    assert 'href="https://orcid.org/0000-0002-1825-0097"' in response.text
    assert f'href="/papers/{held.id}"' in response.text
    assert "Inverse authored paper" in response.text
    assert "Retired authored paper" not in response.text


def test_slug_reader_requires_and_escapes_stored_slug():
    assert ref_url("cad", 5) == "/refs/cad/5"
    assert ref_url("cad", 5, "a?b#c") == "/cad/a%3Fb%23c"
