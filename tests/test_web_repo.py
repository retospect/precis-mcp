"""Tests for the web kind's source-repository snapshot mode (gr608719).

A GitHub/GitLab repo-root URL takes the archive path: the forge API
pins the commit, ``safe_stream`` downloads the tarball under a cap,
and licence/readme/manifest files land as chunks readable via
``<slug>~<path>`` / ``web:<id>~<path>`` / ``view='tree'``.

Network is faked at the three use-site seams:
``precis.handlers._web_repo.polite_safe_get`` (API calls),
``precis.handlers._web_repo.safe_stream`` (the archive), and
``precis.handlers.web.http_client`` (the client factory). The page
path's ``polite_safe_get`` (``precis.utils.http``) is faked too for
the not-a-repo fallback tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import tarfile
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers import _web_repo as repo_mod
from precis.handlers.web import WebHandler

SHA = "0123456789abcdef0123456789abcdef01234567"

_MIT = (
    "MIT License\n\nCopyright (c) 2024\n\nPermission is hereby granted, "
    "free of charge, to any person obtaining a copy of this software."
)
_PYPROJECT = '[project]\nname = "testrepo"\nversion = "1.0"\n'
_README = "# testrepo\n\nDoes things.\n"


def _tarball(files: dict[str, bytes], *, top: str = "testrepo-0123456") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(name=f"{top}/{path}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_ARCHIVE = _tarball(
    {
        "LICENSE": _MIT.encode("utf-8"),
        "README.md": _README.encode("utf-8"),
        "pyproject.toml": _PYPROJECT.encode("utf-8"),
        "src/testrepo/main.py": b"def main():\n    return 1\n",
        "vendor/other/LICENSE": b"vendored - not held\n",
    }
)


class _JsonResp:
    def __init__(self, data: Any, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self) -> Any:
        return self._data


class _TarResp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.status_code = status
        self._content = content
        self.headers: dict[str, str] = {}
        self.content = content

    def iter_bytes(self, chunk_size: int = 65536) -> Any:
        yield self._content

    def close(self) -> None:
        pass


class _HtmlResp:
    """Page-fetch stand-in for the not-a-repo fallback tests."""

    def __init__(self, status: int = 200) -> None:
        self.status_code = status
        self.headers = {"content-type": "text/html"}
        self.text = (
            "<html><head><title>gh page</title></head><body>"
            "<p>some readable content for the page path</p>"
            "</body></html>"
        )
        self.content = self.text.encode("utf-8")


@pytest.fixture(autouse=True)
def _fake_net(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake the network seams; tests steer via the ``state`` dict."""
    state: dict[str, Any] = {
        "api_calls": [],
        "project_status": 200,
        "commit_status": 200,
        "archive": _ARCHIVE,
        "archive_status": 200,
        "page": _HtmlResp(),
    }

    def fake_api(client: Any, url: str, **kw: Any) -> _JsonResp:
        state["api_calls"].append(url)
        if "/commits/" in url or url.endswith("/commits"):
            return _JsonResp({"sha": SHA}, status=state["commit_status"])
        if "/repos/" in url or "/projects/" in url:
            return _JsonResp({"default_branch": "main"}, status=state["project_status"])
        return _JsonResp(None, status=404)

    @contextlib.contextmanager
    def fake_stream(client: Any, method: str, url: str, **kw: Any) -> Any:
        yield _TarResp(state["archive"], status=state["archive_status"])

    def fake_page_get(client: Any, url: str, **kw: Any) -> Any:
        return state["page"]

    monkeypatch.setattr(repo_mod, "polite_safe_get", fake_api)
    monkeypatch.setattr(repo_mod, "safe_stream", fake_stream)
    monkeypatch.setattr(
        "precis.handlers.web.http_client",
        lambda **kw: contextlib.nullcontext(object()),
    )
    # The ordinary page path lazy-imports polite_safe_get from utils.http.
    monkeypatch.setattr("precis.utils.http.polite_safe_get", fake_page_get)
    return state


@pytest.fixture
def handler(hub: Hub) -> WebHandler:
    return WebHandler(hub=hub)


def _cache_row(handler: WebHandler, url: str) -> Any:
    cached = handler.store.get_cache_entry(
        provider="web", request_hash=handler._hash(url)
    )
    assert cached is not None
    return cached


# ── URL grammar ──────────────────────────────────────────────────────


def test_parse_repo_url_github() -> None:
    t = repo_mod.parse_repo_url("https://github.com/ur-whitelab/MDCrow")
    assert t is not None and t.forge == "github" and t.project == "ur-whitelab/MDCrow"
    assert t.ref is None and t.name == "MDCrow"


def test_parse_repo_url_ref_pin() -> None:
    t = repo_mod.parse_repo_url("https://github.com/o/r?ref=v1.2.3")
    assert t is not None and t.ref == "v1.2.3"


def test_parse_repo_url_gitlab_nested() -> None:
    t = repo_mod.parse_repo_url("https://gitlab.com/group/sub/repo")
    assert t is not None and t.forge == "gitlab" and t.project == "group/sub/repo"


def test_parse_repo_url_rejects_non_repo() -> None:
    assert repo_mod.parse_repo_url("https://github.com/o/r/issues") is None
    assert repo_mod.parse_repo_url("https://github.com/o") is None
    assert repo_mod.parse_repo_url("https://gitlab.com/o/r/-/tree/main") is None
    assert repo_mod.parse_repo_url("https://example.com/o/r") is None


def test_is_held_path() -> None:
    for p in (
        "LICENSE",
        "LICENSE-MIT",
        "licence.txt",
        "COPYING",
        "NOTICE",
        "README.md",
        "readme",
        "CITATION.cff",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "environment.yml",
        "package.json",
        "requirements/base.txt",
    ):
        assert repo_mod.is_held_path(p), p
    for p in (
        "src/pkg/main.py",
        "vendor/other/LICENSE",
        "docs/requirements.md",
        ".github/workflows/ci.yml",
        "environmental-impact.pdf",
    ):
        assert not repo_mod.is_held_path(p), p


# ── fetch + hold ─────────────────────────────────────────────────────


def test_repo_url_holds_license_and_manifests(handler: WebHandler) -> None:
    resp = handler.get(id="https://github.com/testorg/testrepo")
    assert "testorg/testrepo" in resp.body
    ref, cache = _cache_row(handler, "https://github.com/testorg/testrepo")
    assert ref.slug == "github-com-testorg-testrepo"
    assert cache.meta["extractor"] == "repo-tarball"
    assert cache.meta["commit_sha"] == SHA
    assert "LICENSE" in cache.meta["files_held"]
    assert "pyproject.toml" in cache.meta["files_held"]
    slugs = {(b.slug or "") for b in handler.store.chunks.list_chunks_for_ref(ref.id)}
    assert {"LICENSE", "README.md", "pyproject.toml", "tree"} <= slugs
    # A vendored deeper LICENSE stays out of the held set.
    assert "vendor/other/LICENSE" not in slugs


def test_repo_head_resolved_via_default_branch(
    handler: WebHandler, _fake_net: Any
) -> None:
    handler.get(id="https://github.com/testorg/testrepo")
    calls = _fake_net["api_calls"]
    assert calls[0].endswith("/repos/testorg/testrepo")
    assert calls[1].endswith("/repos/testorg/testrepo/commits/main")


def test_license_chunk_reads_whole_file(handler: WebHandler) -> None:
    handler.get(id="https://github.com/testorg/testrepo")
    resp = handler.get(id="github-com-testorg-testrepo~LICENSE")
    assert "MIT License" in resp.body
    assert "Permission is hereby granted" in resp.body


def test_license_chunk_via_numeric_ref_id(handler: WebHandler) -> None:
    """``web:<id>~LICENSE`` — the numeric prefix resolves the ref id."""
    handler.get(id="https://github.com/testorg/testrepo")
    ref, _ = _cache_row(handler, "https://github.com/testorg/testrepo")
    resp = handler.get(id=f"{ref.id}~LICENSE")
    assert "MIT License" in resp.body


def test_license_chunk_case_insensitive(handler: WebHandler) -> None:
    handler.get(id="https://github.com/testorg/testrepo")
    resp = handler.get(id="github-com-testorg-testrepo~license")
    assert "MIT License" in resp.body


def test_view_tree_lists_every_path(handler: WebHandler) -> None:
    handler.get(id="https://github.com/testorg/testrepo")
    resp = handler.get(id="github-com-testorg-testrepo", view="tree")
    assert "src/testrepo/main.py" in resp.body
    assert "vendor/other/LICENSE" in resp.body  # listed though not held


def test_view_tree_needs_repo_snapshot(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    with pytest.raises(BadInput, match="repository snapshot"):
        handler.get(id="example-com-article", view="tree")


def test_view_tree_missing_is_notfound(handler: WebHandler) -> None:
    with pytest.raises(NotFound, match="not cached yet"):
        handler.get(id="https://github.com/testorg/neverfetched", view="tree")


def test_ref_pin_caches_separately(handler: WebHandler) -> None:
    handler.get(id="https://github.com/testorg/testrepo?ref=v1.0")
    ref, cache = _cache_row(handler, "https://github.com/testorg/testrepo?ref=v1.0")
    digest = hashlib.sha256(b"v1.0").hexdigest()[:8]
    assert ref.slug == f"github-com-testorg-testrepo-ref-v1-0-{digest}"
    assert "ref 'v1.0'" in cache.meta["ref"]


def test_ref_pins_colliding_readable_slug_keep_separate_rows(
    handler: WebHandler,
) -> None:
    """``v1.0`` and ``v1-0`` sanitise to the same readable slug.

    The ref-hash tail keeps them on distinct rows — without it the
    second fetch would overwrite the first snapshot's body in place.
    """
    handler.get(id="https://github.com/testorg/testrepo?ref=v1.0")
    handler.get(id="https://github.com/testorg/testrepo?ref=v1-0")
    ref_dot, cache_dot = _cache_row(
        handler, "https://github.com/testorg/testrepo?ref=v1.0"
    )
    ref_dash, cache_dash = _cache_row(
        handler, "https://github.com/testorg/testrepo?ref=v1-0"
    )
    assert ref_dot.id != ref_dash.id
    assert ref_dot.slug != ref_dash.slug
    assert ref_dot.slug.startswith("github-com-testorg-testrepo-ref-v1-0-")
    assert ref_dash.slug.startswith("github-com-testorg-testrepo-ref-v1-0-")
    assert "ref 'v1.0'" in cache_dot.meta["ref"]
    assert "ref 'v1-0'" in cache_dash.meta["ref"]


def test_same_slug_different_url_refuses_overwrite(handler: WebHandler) -> None:
    """A second URL deriving an already-taken slug refuses rather than
    refresh that row in place — the residual collision class the
    ref-hash tail cannot separate (here: a kept extra query param)."""
    handler.get(id="https://github.com/testorg/testrepo?ref=v1.0")
    with pytest.raises(BadInput, match="Refusing to overwrite"):
        handler.get(id="https://github.com/testorg/testrepo?ref=v1.0&extra=1")
    ref, cache = _cache_row(handler, "https://github.com/testorg/testrepo?ref=v1.0")
    assert "ref 'v1.0'" in cache.meta["ref"]


def test_ref_pin_resolves_via_commits_ref(handler: WebHandler, _fake_net: Any) -> None:
    handler.get(id="https://github.com/testorg/testrepo?ref=v1.0")
    assert _fake_net["api_calls"] == [
        "https://api.github.com/repos/testorg/testrepo/commits/v1.0"
    ]


def test_archive_over_cap_refuses(handler: WebHandler, _fake_net: Any) -> None:
    _fake_net["archive"] = b"x" * (repo_mod.ARCHIVE_MAX_BYTES + 1)
    with pytest.raises(Upstream, match="MiB cap"):
        handler.get(id="https://github.com/testorg/toobig")


def test_non_tarball_refuses(handler: WebHandler, _fake_net: Any) -> None:
    _fake_net["archive"] = b"<html>not a tarball</html>"
    with pytest.raises(Upstream, match="readable .tar.gz"):
        handler.get(id="https://github.com/testorg/badtar")


def test_unknown_ref_refuses(handler: WebHandler, _fake_net: Any) -> None:
    _fake_net["commit_status"] = 404
    with pytest.raises(Upstream, match="ref 'v9.9' not found"):
        handler.get(id="https://github.com/testorg/testrepo?ref=v9.9")


def test_not_a_repo_falls_back_to_page(handler: WebHandler, _fake_net: Any) -> None:
    """A 404 project lookup hands the URL to the ordinary page fetch."""
    _fake_net["project_status"] = 404
    _fake_net["page"] = _HtmlResp(status=404)
    with pytest.raises(Upstream, match="HTTP 404"):
        handler.get(id="https://github.com/testorg/privaterepo")


def test_refresh_re_runs_repo_path(handler: WebHandler) -> None:
    handler.get(id="https://github.com/testorg/testrepo")
    ref, _ = _cache_row(handler, "https://github.com/testorg/testrepo")
    handler.get(id=ref.slug, mode="refresh")
    ref2, cache2 = _cache_row(handler, "https://github.com/testorg/testrepo")
    assert ref2.id == ref.id
    assert cache2.meta["extractor"] == "repo-tarball"


# ── dispatch level ───────────────────────────────────────────────────


def test_web_colon_id_section_dispatch(runtime_with_store: Any) -> None:
    """``get(id='web:<ref_id>~LICENSE')`` routes through the colon split."""
    handler = WebHandler(hub=runtime_with_store.hub)
    handler.get(id="https://github.com/testorg/testrepo")
    ref, _ = _cache_row(handler, "https://github.com/testorg/testrepo")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "web", "id": f"web:{ref.id}~LICENSE"}
    )
    assert not is_error
    assert "MIT License" in body
