"""``/python/<alias>::<qualname>`` — the code eye page (gr499107): the
python kind's ``get(extent=…)`` rendered in the browser through the
same dispatcher, with every caller/callee/test/member linked to its own
page so a click re-centres the eye.

DB-free — the python kind is in-memory; the runtime boots with
``python_roots`` on a tmp repo and no store.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis_web.app import create_app
from precis_web.config import WebConfig


def _write(repo: Path, rel: str, content: str) -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Same shape as ``test_python_eye``'s fixture: a target with a
    production caller, a callee, package siblings and a direct test."""
    _write(tmp_path, "pkg/__init__.py", '"""pkg — the sample package."""\n')
    _write(
        tmp_path,
        "pkg/runner.py",
        '''
        """The runner module."""

        def helper(x: int) -> int:
            """Small helper."""
            return x + 1


        def target(x: int) -> int:
            """Run the thing under a ceiling.

            Longer body of the docstring — context the eye shows in
            full while the rings stay at one line each.
            """
            return helper(x) * 2


        def other(x: int) -> int:
            """Unrelated."""
            return x - 1
        ''',
    )
    _write(
        tmp_path,
        "pkg/dispatch.py",
        '''
        """The job-type module."""

        from pkg.runner import target

        NAME = "sample_job"


        def _dispatch(x: int) -> int:
            """Dispatch the claimed job."""
            return target(x)
        ''',
    )
    _write(
        tmp_path,
        "pkg/mates.py",
        '''
        """The second-hop file."""

        from pkg.dispatch import _dispatch


        def scalar() -> int:
            """The file-mate the 2-hop surfaces."""

            return 0


        def via_dispatch() -> int:
            return _dispatch(1)
        ''',
    )
    _write(
        tmp_path,
        "tests/test_target.py",
        """
        from pkg.runner import target


        def test_target_direct():
            assert target(1) == 4
        """,
    )
    return tmp_path


@pytest.fixture
def runtime(repo: Path) -> PrecisRuntime:
    return PrecisRuntime(config=PrecisConfig(), hub=boot(python_roots=f"r:{repo}"))


@pytest.fixture
def client(runtime: PrecisRuntime) -> TestClient:
    app = create_app(runtime=runtime, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def test_focus_page_renders_the_mcp_eye_with_links(
    client: TestClient, runtime: PrecisRuntime
) -> None:
    """The body is the MCP ``get(extent='fisheye+1hop')`` output; every
    caller/callee/test/member it names is a link that re-centres the eye."""
    page = client.get("/python/r::pkg.runner.target")
    assert page.status_code == 200
    mcp_body, err = runtime.dispatch_with_status(
        "get",
        {"kind": "python", "id": "r::pkg.runner.target", "extent": "fisheye+1hop"},
    )
    assert not err and "— ring 1" in mcp_body
    # ring 1 renders, with the caller and callee as eye links
    assert "ring 1" in page.text
    assert 'href="/python/r::pkg.dispatch._dispatch?extent=fisheye%2B1hop"' in page.text
    assert 'href="/python/r::pkg.runner.helper?extent=fisheye%2B1hop"' in page.text
    # the module span's member rows link through their L<line> markers
    assert 'href="/python/r::pkg.runner.other?extent=fisheye%2B1hop"' in page.text
    # the referencing test's nodeid links to the test symbol's own page
    assert (
        'href="/python/r::test_target.test_target_direct?extent=fisheye%2B1hop"'
        in page.text
    )
    # the rung selector names every rung, the current one marked
    for rung in ("fisheye", "fisheye+2hop", "verbatim"):
        assert f">{rung}</a>" in page.text
    # the source toggle is offered
    assert "?view=source" in page.text


def test_focus_page_takes_the_rung(client: TestClient) -> None:
    fisheye = client.get("/python/r::pkg.runner.target", params={"extent": "fisheye"})
    assert fisheye.status_code == 200
    # the module span renders — its module name wrapped in a link
    assert "— in " in fisheye.text and ">pkg.runner</a>" in fisheye.text
    assert "ring 1" not in fisheye.text  # edges land at +1hop
    two = client.get("/python/r::pkg.runner.target", params={"extent": "fisheye+2hop"})
    assert two.status_code == 200 and "ring 2" in two.text
    # ring-2's bare qualnames link too — the second hop walks on
    assert 'href="/python/r::pkg.mates.via_dispatch' in two.text
    bad = client.get("/python/r::pkg.runner.target", params={"extent": "fisheye+9hop"})
    assert "error" in bad.text


def test_module_focus_links_importers(client: TestClient) -> None:
    page = client.get("/python/r::pkg.runner")
    assert page.status_code == 200
    assert "Importers:" in page.text
    assert 'href="/python/r::pkg.dispatch?extent=fisheye%2B1hop"' in page.text
    # sibling modules of the package span are links as well
    assert 'href="/python/r::pkg.mates' in page.text


def test_source_toggle_shows_raw_source(client: TestClient) -> None:
    page = client.get("/python/r::pkg.runner.target", params={"view": "source"})
    assert page.status_code == 200
    assert "return helper(x) * 2" in page.text
    assert "range: L" in page.text
    # raw source is never linkified
    assert 'class="eye-handle"' not in page.text


def test_view_dispatch_surfaces_handler_errors(client: TestClient) -> None:
    page = client.get("/python/r::pkg.runner.target", params={"view": "nosuch"})
    assert page.status_code == 200 and "[error:" in page.text


def test_dead_ref_renders_the_handler_error(client: TestClient) -> None:
    page = client.get("/python/r::pkg.runner.nope")
    assert page.status_code == 404 and "[error:NotFound]" in page.text
    assert client.get("/python/nosuch::x").status_code == 404
    # a BadInput (selector on a :: id) shows the handler's error, not a 404
    bad = client.get("/python/r::pkg.runner.target~L1")
    assert bad.status_code == 200 and "[error:" in bad.text


def test_index_redirects_an_id_and_searches_a_query(client: TestClient) -> None:
    r = client.get(
        "/python/", params={"q": "r::pkg.runner.target"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/python/r::pkg.runner.target"
    hits = client.get("/python/", params={"q": "runner"})
    assert hits.status_code == 200
    assert 'href="/python/r::pkg.runner' in hits.text
    assert 'href="/python/r::pkg.runner.target' in hits.text


def test_bare_alias_renders_the_repo_overview(client: TestClient) -> None:
    page = client.get("/python/r")
    assert page.status_code == 200
    assert "repo overview" in page.text and "Top-level packages" in page.text
    # the Next: drill-in links to the package's own page
    assert 'href="/python/r::pkg' in page.text
