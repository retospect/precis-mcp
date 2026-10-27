"""The design code viewer — ``GET /{se,pcb,cad}/{slug}/code`` (gr516943).

FastAPI route tests over the real test DB (the folder lookup is plain
SQL — the shared FakeStore's no-parse ``execute`` can't exercise it),
plus one Basic-auth check on a stub store since the gate answers before
the handler runs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import html
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from precis.store import Store
from precis.store.types import ChunkInsert
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes import code_view

MAIN_PY = (
    "import util\n"
    "from pkg.helper import build\n"
    "\n"
    "def main():\n"
    "    return util.go(build())\n"
)

UTIL_PY = "def go(x):\n    return x\n"
HELPER_PY = "def build():\n    return 1\n"


@pytest.fixture
def code_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _design(store: Store, slug: str, *, kind: str = "se", ws_meta=None) -> int:
    meta = {"workspaces": ws_meta} if ws_meta is not None else {}
    ref = store.insert_ref(kind=kind, slug=slug, title=f"design {slug}", meta=meta)
    return int(ref.id)


def _folder(
    store: Store,
    *,
    ws: str,
    design: str | None,
    files: dict[str, str],
    part_of: int | None = None,
) -> int:
    """A workspace folder: ``meta.workspace`` marked, ``plaintext``
    children carrying ``harvest_orig_path`` and chunk bodies."""
    wmeta: dict = {"slug": ws, "created_by": "test"}
    if design is not None:
        wmeta["design"] = design
    folder = store.insert_ref(
        kind="folder",
        slug=None,
        title=f"workspace {ws}",
        meta={"workspace": wmeta},
    )
    if part_of is not None:
        store.add_link(
            src_ref_id=int(folder.id), dst_ref_id=part_of, relation="part-of"
        )
    for i, (path, text) in enumerate(files.items()):
        slug = f"cv-{ws}--" + path.replace("/", "-").replace(".", "-")
        child = store.insert_ref(
            kind="plaintext",
            slug=slug,
            title=path,
            parent_id=int(folder.id),
            meta={"harvest_orig_path": path},
        )
        store.chunks.insert_chunks(int(child.id), [ChunkInsert(ord=0, text=text)])
    return int(folder.id)


def test_code_page_404(code_client: TestClient) -> None:
    r = code_client.get("/se/no-such-design/code")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_code_page_empty_state(store: Store, code_client: TestClient) -> None:
    _design(store, "cv-empty-1")
    r = code_client.get("/se/cv-empty-1/code")
    assert r.status_code == 200
    assert "no code attached" in r.text.lower()


def test_code_page_renders_files_escaped(store: Store, code_client: TestClient) -> None:
    _design(
        store, "cv-esc-1", ws_meta={"cvws": {"run_job": 42, "script_sha": "deadbee5"}}
    )
    _folder(
        store,
        ws="cvws",
        design="cv-esc-1",
        files={
            "main.py": MAIN_PY,
            "util.py": UTIL_PY
            + "\n<script>alert(1)</script>\nx = '# <script>alert(2)</script>'\n",
            "pkg/helper.py": HELPER_PY,
        },
    )
    r = code_client.get("/se/cv-esc-1/code")
    assert r.status_code == 200
    # Stored code is escaped text — the <script> shows, never runs. The
    # bare-script line fragments across token spans; the in-string copy
    # stays one escaped token.
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in r.text
    assert "&lt;" in r.text and "alert" in r.text
    assert "<script>alert(1)</script>" not in r.text
    assert "<script>alert(2)</script>" not in r.text
    # Tabs per file, run info in the header, sibling imports linked.
    assert "main.py" in r.text and "util.py" in r.text
    assert "pkg/helper.py" in r.text
    assert "deadbee5" in r.text and "run job 42" in r.text
    assert 'href="?ws=cvws&amp;file=util.py"' in r.text
    assert 'href="?ws=cvws&amp;file=pkg/helper.py"' in r.text


def test_code_page_highlights_python(store: Store, code_client: TestClient) -> None:
    """Pygments output is the only ``|safe`` markup: token spans and the
    scoped ``.cv-code`` style block render; an unknown extension falls
    back to the TextLexer — escaped and unspanned."""
    _design(store, "cv-hl-1")
    _folder(
        store,
        ws="cvws",
        design="cv-hl-1",
        files={"main.py": MAIN_PY, "notes.zzz": "plain <b>text</b>\n"},
    )
    r = code_client.get("/se/cv-hl-1/code")
    assert r.status_code == 200
    assert '<span class="cv-kn">import</span>' in r.text
    assert '<span class="cv-k">def</span>' in r.text
    assert ".cv-code" in r.text
    assert "plain &lt;b&gt;text&lt;/b&gt;" in r.text


def test_code_page_selects_file_param(store: Store, code_client: TestClient) -> None:
    _design(store, "cv-sel-1")
    _folder(
        store,
        ws="cvws",
        design="cv-sel-1",
        files={"a.py": "A = 1\n", "b.py": "B = 2\n"},
    )
    r = code_client.get("/se/cv-sel-1/code?ws=cvws&file=b.py")
    assert r.status_code == 200
    assert "{ tab: &#34;f1&#34; }" in r.text


def test_code_page_file_param_selects_without_ws(
    store: Store, code_client: TestClient
) -> None:
    """``?file=`` alone selects across workspaces — the Selection box's
    board-script link names a file, not always a ws (gr520540)."""
    _design(store, "cv-selfile-1")
    _folder(store, ws="one", design="cv-selfile-1", files={"a.py": "A = 1\n"})
    _folder(store, ws="two", design="cv-selfile-1", files={"b.py": "B = 2\n"})
    r = code_client.get("/se/cv-selfile-1/code?file=b.py")
    assert r.status_code == 200
    assert "{ tab: &#34;f1&#34; }" in r.text


def test_code_page_file_param_first_match_across_workspaces(
    store: Store, code_client: TestClient
) -> None:
    """The same path in two workspaces, no ``ws=``: the first match is
    selected, not the last (gr520540)."""
    _design(store, "cv-seldup-1")
    _folder(store, ws="one", design="cv-seldup-1", files={"x.py": "X = 1\n"})
    _folder(store, ws="two", design="cv-seldup-1", files={"x.py": "X = 2\n"})
    r = code_client.get("/se/cv-seldup-1/code?file=x.py")
    assert r.status_code == 200
    assert "{ tab: &#34;f0&#34; }" in r.text


def test_code_page_line_anchors(store: Store, code_client: TestClient) -> None:
    """One empty ``<a id="L<n>">`` marker per line start so a ``#L<n>``
    fragment lands on the call site (gr520540)."""
    _design(store, "cv-ln-1")
    _folder(store, ws="cvws", design="cv-ln-1", files={"main.py": MAIN_PY})
    r = code_client.get("/se/cv-ln-1/code")
    assert r.status_code == 200
    assert 'id="L1"' in r.text
    assert 'id="L3"' in r.text
    assert 'id="L5"' in r.text
    assert 'id="L9"' not in r.text


def test_code_page_xdata_attribute_parses(
    store: Store, code_client: TestClient
) -> None:
    """``x-data="{ tab: {{ selected | tojson | forceescape }} }"`` — a
    raw ``tojson`` quote would close the double-quoted attribute early
    and Alpine never starts, hiding every panel. The browser decodes
    ``&#34;`` back to ``"`` before Alpine parses the expression."""
    _design(store, "cv-xdata-1")
    _folder(store, ws="cvws", design="cv-xdata-1", files={"a.py": "A = 1\n"})
    r = code_client.get("/se/cv-xdata-1/code")
    assert r.status_code == 200
    m = re.search(r'x-data="(\{ tab: [^"]*)"', r.text)
    assert m is not None
    assert html.unescape(m.group(1)) == '{ tab: "f0" }'


def test_code_page_truncation(store: Store, code_client: TestClient) -> None:
    big = "x" * (200 * 1024) + "\nMARKER_END\n"
    _design(store, "cv-big-1")
    _folder(store, ws="cvws", design="cv-big-1", files={"big.py": big})
    r = code_client.get("/se/cv-big-1/code")
    assert r.status_code == 200
    assert "MARKER_END" not in r.text
    assert "truncated" in r.text.lower()


def test_code_page_foreign_folder_not_listed(
    store: Store, code_client: TestClient
) -> None:
    """A folder bound to a different design (or no design) is out of
    scope — its files must not appear on this design's page."""
    _design(store, "cv-mine-1")
    _design(store, "cv-other-1")
    _folder(store, ws="mine", design="cv-mine-1", files={"main.py": "CV_MINE\n"})
    _folder(store, ws="other", design="cv-other-1", files={"main.py": "CV_OTHER\n"})
    _folder(store, ws="stray", design=None, files={"main.py": "CV_STRAY\n"})
    r = code_client.get("/se/cv-mine-1/code")
    assert r.status_code == 200
    assert "CV_MINE" in r.text
    assert "CV_OTHER" not in r.text and "CV_STRAY" not in r.text
    assert ">mine<" in r.text
    assert ">other<" not in r.text and ">stray<" not in r.text


def test_code_page_part_of_folder(store: Store, code_client: TestClient) -> None:
    """A workspace folder linked ``part-of`` the design lists too — the
    link, not only ``meta.workspace.design``, binds a folder."""
    design_id = _design(store, "cv-pof-1")
    _folder(
        store,
        ws="pof",
        design=None,
        part_of=design_id,
        files={"main.py": "POF\n"},
    )
    r = code_client.get("/se/cv-pof-1/code")
    assert r.status_code == 200
    assert "POF" in r.text


def test_code_page_bare_design_slug_is_se_only(
    store: Store, code_client: TestClient
) -> None:
    """A bare ``meta.workspace.design`` slug binds only ``se`` designs:
    slugs are global cite_keys, so a bare value always names an se
    design — a pcb page must need the ``pcb:<slug>`` spelling."""
    _design(store, "cv-se-1")
    _design(store, "cv-pcb-1", kind="pcb")
    _folder(store, ws="sews", design="cv-se-1", files={"a.py": "SE_WS\n"})
    _folder(store, ws="bare", design="cv-pcb-1", files={"b.py": "BARE_WS\n"})
    _folder(store, ws="spelled", design="pcb:cv-pcb-1", files={"c.py": "PCB_WS\n"})
    r = code_client.get("/se/cv-se-1/code")
    assert r.status_code == 200
    assert "SE_WS" in r.text
    assert "BARE_WS" not in r.text and "PCB_WS" not in r.text
    r = code_client.get("/pcb/cv-pcb-1/code")
    assert r.status_code == 200
    assert "PCB_WS" in r.text
    assert "BARE_WS" not in r.text and "SE_WS" not in r.text


def test_code_page_entities_not_split_by_sibling_names(
    store: Store, code_client: TestClient
) -> None:
    """Files named ``quot``/``39`` must not link inside ``&quot;`` /
    ``&#39;`` — the entities html/pygments escaping emits for quotes —
    or the displayed code corrupts."""
    _design(store, "cv-ent-1")
    _folder(
        store,
        ws="cvws",
        design="cv-ent-1",
        files={
            "quot": "",
            "39": "",
            "q.py": "x = \"a\" + 'b'\n",
        },
    )
    r = code_client.get("/se/cv-ent-1/code")
    assert r.status_code == 200
    assert "&quot;" in r.text
    # The only ``quot``/``39`` strings on the page sit inside entities;
    # a tab link to either file means an entity was split.
    assert "file=quot" not in r.text
    assert "file=39" not in r.text


def test_code_page_byte_budget_lists_skipped(
    store: Store, code_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_view, "_MAX_PAGE_BYTES", 100)
    _design(store, "cv-budget-1")
    _folder(
        store,
        ws="cvws",
        design="cv-budget-1",
        files={
            "a.py": "A" * 60 + "\n",
            "b.py": "B" * 60 + "\n",
            "c.py": "MARKER_C\n",
        },
    )
    r = code_client.get("/se/cv-budget-1/code")
    assert r.status_code == 200
    assert "MARKER_C" not in r.text
    assert "over the page byte budget" in r.text
    assert "c.py" in r.text


def test_code_page_links_only_shown_files(
    store: Store, code_client: TestClient
) -> None:
    """Past ``_MAX_FILES`` a path isn't rendered — and must not be a
    link target either (the alternation covers shown files only)."""
    _design(store, "cv-many-1")
    files = {f"m{i:03}.py": f"V{i} = {i}\n" for i in range(130)}
    files["aaa.py"] = "import zzz\n"
    files["zzz.py"] = "ZZZ = 1\n"
    _folder(store, ws="cvws", design="cv-many-1", files=files)
    r = code_client.get("/se/cv-many-1/code")
    assert r.status_code == 200
    assert "file=zzz.py" not in r.text
    assert "more file(s) not shown" in r.text


def test_scoped_links_fences_alias_resolution() -> None:
    """``_ScopedLinks`` lets only ``main`` and the page's own ``ws-*``
    aliases reach the handler — a stored file mentioning another
    ``ws-*`` (or ``wt-*``) alias must not stage a workspace on view."""
    calls: list[str] = []
    sentinel = object()

    def _resolve(alias: str) -> Path:
        calls.append(alias)
        return Path("/nonexistent")

    handler = SimpleNamespace(
        _resolve_alias=_resolve, _index=lambda alias, root: sentinel
    )
    links = code_view._ScopedLinks(handler, {"main", "ws-mine"})
    assert links.index("ws-other") is None
    assert links.index("wt-foo") is None
    assert links.index("repo2") is None
    assert links.index(None) is None
    assert calls == []
    assert links.index("ws-mine") is sentinel
    assert links.index("main") is sentinel
    assert calls == ["ws-mine", "main"]
    # memoised — a second resolve never re-enters the handler
    assert links.index("ws-mine") is sentinel
    assert calls == ["ws-mine", "main"]


def test_code_requires_auth() -> None:
    """The route sits behind the same Basic gate as every page."""
    store = SimpleNamespace(count_web_users=lambda *, enabled_only=True: 1)
    app = create_app(
        runtime=SimpleNamespace(store=store),
        web_config=WebConfig(corpus_dir=None, auth_required=True),
    )
    r = TestClient(app).get("/se/x/code", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic realm=")
