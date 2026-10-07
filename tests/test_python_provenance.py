"""Read provenance fixtures: exact indexed bytes, explicit limits, isolated roots."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers import _python_provenance as provenance
from precis.handlers.python import PythonHandler
from precis.python_index import RepoCache
from precis.python_index import cache as cache_mod
from precis.python_index.indexer import index_module
from precis.python_index.types import RepoIndex


def _handler(root: Path, **others: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": root, **others})


def _source(root: Path, text: str = "def hello(): return 1\n") -> Path:
    path = root / "m.py"
    path.write_text(text, encoding="utf-8")
    return path


def test_raw_crlf_digest_and_same_snapshot_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "m.py"
    raw = b"def hello():\r\n    return 1\r\n"
    path.write_bytes(raw)
    handler = _handler(tmp_path)
    snapshot = handler.cache.get(tmp_path)
    mod = snapshot.file("m.py")
    assert mod is not None
    assert mod.bytes_sha256 == hashlib.sha256(raw).hexdigest()
    assert mod.sha256 == hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    assert mod.sha256 != mod.bytes_sha256
    # Simulate a concurrent disk change after indexing but before rendering.
    monkeypatch.setattr(handler.cache, "get", lambda root: snapshot)
    path.write_text("def other(): return 2\n", encoding="utf-8")
    for id in ("r/m.py", "r/m.py~L1-L2", "r::m.hello"):
        body = handler.get(id=id, view="source").body
        assert "return 1" in body and "return 2" not in body
        assert mod.bytes_sha256 in body
    assert "bytes_sha256=" in handler.search(q="hello", scope="r").body


def test_inventory_no_index_and_deleted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    handler = _handler(root)

    def forbidden(root: Path) -> RepoIndex:
        raise AssertionError("inventory must not index")

    monkeypatch.setattr(handler.cache, "get", forbidden)
    assert "not indexed by this call" in handler.get().body
    root.rmdir()
    assert "available=False" in handler.get().body
    monkeypatch.undo()
    with pytest.raises(NotFound, match="unavailable.*freshness unknown"):
        handler.get(id="r")


def test_root_guard_before_index_and_no_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler(tmp_path)

    def forbidden(root: Path) -> RepoIndex:
        raise AssertionError("mismatch must be rejected before indexing")

    monkeypatch.setattr(handler.cache, "get", forbidden)
    for call in (
        lambda: handler.get(id="r", expected_root=str(tmp_path / "unregistered")),
        lambda: handler.search(
            q="hello", scope="r/m.py", expected_root=str(tmp_path / "other")
        ),
    ):
        with pytest.raises(BadInput, match="root mismatch"):
            call()
    for call in (
        lambda: handler.get(expected_root=str(tmp_path)),
        lambda: handler.search(q="hello", expected_root=str(tmp_path)),
    ):
        with pytest.raises(BadInput, match="explicit alias"):
            call()
    assert list(handler.roots) == ["r"]
    monkeypatch.undo()
    _source(tmp_path)
    assert "alias='r'" in handler.get(id="r", expected_root=str(tmp_path)).body
    assert (
        "hello"
        in handler.search(q="hello", scope="r::m", expected_root=str(tmp_path)).body
    )


@pytest.mark.parametrize("view", [None, "toc", "outline", "entries", "callgraph"])
def test_views_and_no_hits_include_all_consulted_roots(
    tmp_path: Path, view: str | None
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _source(root)
    _source(other, "def there(): pass\n")
    handler = _handler(root, other=other)
    kw = {"entry": "m:hello", "cross_repo": True} if view == "callgraph" else {}
    body = handler.get(id="r", view=view, **kw).body
    assert "Python provenance: alias='r'" in body
    if view == "callgraph":
        assert "Python provenance: alias='other'" in body
    body = handler.search(q="not-present-anywhere").body
    assert "Python provenance: alias='r'" in body
    assert "Python provenance: alias='other'" in body
    assert "reused content-not-revalidated" in body
    assert "non-atomic tree observation" in body


@pytest.mark.parametrize("view", ["callers", "imports", "importers"])
def test_reverse_views_provenance(tmp_path: Path, view: str) -> None:
    _source(tmp_path)
    body = (
        _handler(tmp_path)
        .get(id="r::m.hello" if view == "callers" else "r::m", view=view)
        .body
    )
    assert "Python provenance: alias='r'" in body


def test_entry_metadata_separate_from_python_corpus(tmp_path: Path) -> None:
    _source(tmp_path, "def hello(): pass\nif __name__ == '__main__': hello()\n")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project.scripts]\nhello="m:hello"\n', encoding="utf-8")
    handler = _handler(tmp_path)
    first = handler.get(id="r", view="entries").body
    assert "Non-Python observation:" in first
    assert hashlib.sha256(pyproject.read_bytes()).hexdigest() in first
    assert "excluded from Python corpus fingerprint" in first
    corpus = provenance.corpus_fingerprint(handler.cache.get(tmp_path))
    pyproject.write_text('[project.scripts]\nchanged="m:hello"\n', encoding="utf-8")
    assert corpus == provenance.corpus_fingerprint(handler.cache.get(tmp_path))
    assert "changed" in handler.get(id="r", view="entries").body
    assert (
        "Non-Python observation:"
        in handler.get(id="r", view="callgraph", entry="changed").body
    )


def test_file_identity_replacement_with_same_size_mtime(tmp_path: Path) -> None:
    path = _source(tmp_path)
    cache = RepoCache()
    first = cache.get(tmp_path)
    st = path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_text("def hello(): return 2\n", encoding="utf-8")
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    replacement.replace(path)
    second = cache.get(tmp_path)
    assert first.file("m.py") is not second.file("m.py")
    mod = second.file("m.py")
    assert mod is not None and mod.source == "def hello(): return 2\n"


def test_reused_signature_explicitly_does_not_revalidate_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _source(tmp_path)
    cache = RepoCache()
    first = cache.get(tmp_path)
    signature = cache_mod._signature(path)
    actual_signature = cache_mod._signature
    monkeypatch.setattr(
        cache_mod,
        "_signature",
        lambda p: signature if p == path else actual_signature(p),
    )
    path.write_text("def hello(): return 2\n", encoding="utf-8")
    second = cache.get(tmp_path)
    assert first.file("m.py") is second.file("m.py")
    body = (
        PythonHandler(hub=Hub(), roots={"r": tmp_path}, cache=cache)
        .get(id="r/m.py", view="source")
        .body
    )
    assert "return 1" in body and "content-not-revalidated" in body


@pytest.mark.parametrize("recover", [True, False])
def test_bounded_read_race_never_stamps_old_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recover: bool
) -> None:
    path = _source(tmp_path)
    cache = RepoCache()
    cache.get(tmp_path)
    path.write_text("def hello(): return 2\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    real_parse = cache_mod.index_module
    calls = 0

    def racing(path: Path, **kw):
        nonlocal calls
        calls += 1
        module = real_parse(path, **kw)
        if not recover or calls == 1:
            st = path.stat()
            path.write_text(f"def hello(): return {calls + 2}\n", encoding="utf-8")
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        return module

    monkeypatch.setattr(cache_mod, "index_module", racing)
    idx = cache.get(tmp_path)
    assert calls == 2
    assert idx.observation is not None
    if recover:
        mod = idx.file("m.py")
        assert mod is not None and mod.source == path.read_text(encoding="utf-8")
        assert not idx.observation.issues
    else:
        assert idx.file("m.py") is None
        assert "unstable read; omitted" in idx.observation.issues[0]


def test_unreadable_decode_and_parse_limitations(tmp_path: Path) -> None:
    _source(tmp_path)
    handler = _handler(tmp_path)
    handler.get(id="r")
    (tmp_path / "m.py").write_bytes(b"\xff")
    (tmp_path / "broken.py").write_text("def (", encoding="utf-8")
    body = handler.get(id="r").body
    assert "UnicodeDecodeError; omitted" in body
    assert "partial/unstable" in body
    assert "Parse-error files represented with limited symbols: 1" in body
    assert handler.cache.get(tmp_path).file("m.py") is None


def test_corpus_hash_framing_paths_and_unknown_digest(tmp_path: Path) -> None:
    path = _source(tmp_path)
    mod = index_module(path, qualname="m", file_relative="a\n.py")
    first = RepoIndex.build(tmp_path, [mod])
    second = RepoIndex.build(tmp_path, [replace(mod, file="a.py")])
    assert provenance.corpus_fingerprint(first) != provenance.corpus_fingerprint(second)
    assert (
        provenance.corpus_fingerprint(
            RepoIndex.build(tmp_path, [replace(mod, bytes_sha256=None)])
        )
        == "unknown"
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    ).stdout.strip()


def test_two_git_worktrees_commits_dirty_untracked_and_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A known clean/dirty fixture must not inherit runner Git filters. The
    # separate filter tests verify explicit unknowns and nonexecution.
    config = tmp_path / "isolated.gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    if shutil.which("git") is None:
        pytest.skip("Git not installed")
    one = tmp_path / "one"
    one.mkdir()
    _git(one, "init")
    _git(one, "config", "user.name", "Fixture")
    _git(one, "config", "user.email", "fixture@example.invalid")
    _source(one)
    _git(one, "add", "m.py")
    _git(one, "commit", "-m", "fixture one")
    head_one = _git(one, "rev-parse", "HEAD")
    two = tmp_path / "two"
    _git(one, "worktree", "add", "-b", "fixture-two", str(two))
    _source(two, "def hello(): return 2\n")
    _git(two, "commit", "-am", "fixture two")
    head_two = _git(two, "rev-parse", "HEAD")
    _git(two, "checkout", "--detach")
    _source(two, "def hello(): return 3\n")
    (two / "untracked.py").write_text("value = 4\n", encoding="utf-8")
    handler = PythonHandler(hub=Hub(), roots={"one": one, "two": two})
    first = handler.get(id="one").body
    second = handler.get(id="two").body
    assert head_one in first and "dirty='no'" in first, first
    assert head_two in second and "dirty='yes'" in second, second
    assert "branch='detached'" in second and "files=2" in second
    assert "indexed bytes are not attested as HEAD" in second
    assert provenance.corpus_fingerprint(
        handler.cache.get(one)
    ) != provenance.corpus_fingerprint(handler.cache.get(two))
    # Global filters also make dirty status explicitly unknown, even when
    # no attribute selects them in this fixture's tracked Python files.
    config.write_text('[filter "ambient"]\n\tclean = cat\n', encoding="utf-8")
    inherited = handler.get(id="one").body
    assert head_one in inherited and "dirty='unknown'" in inherited, inherited
    assert "executable filters" in inherited


def test_git_unavailable_timeout_and_no_per_hit_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(tmp_path, "def first(): pass\ndef second(): pass\n")
    for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 2)):

        def fail(*args, failure=error, **kwargs):
            raise failure

        monkeypatch.setattr(provenance.subprocess, "run", fail)
        body = _handler(tmp_path).search(q="function").body
        assert "head='unknown'" in body and "unavailable/partial" in body
    monkeypatch.undo()
    calls = []

    def observe(root: Path) -> str:
        calls.append(root)
        return "Git fixture unknown"

    monkeypatch.setattr(provenance, "_observe_git", observe)
    body = _handler(tmp_path).search(q="m", page_size=100).body
    assert "m.first" in body and "m.second" in body
    assert calls == [tmp_path]


def test_partial_walk_retains_explicitly_unverified_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _source(tmp_path)
    cache = RepoCache()
    first = cache.get(tmp_path)

    def partial(root: Path):
        yield path
        raise PermissionError("fixture")

    monkeypatch.setattr(cache_mod, "_walk_python_files", partial)
    handler = PythonHandler(hub=Hub(), roots={"r": tmp_path}, cache=cache)
    body = handler.get(id="r").body
    assert "partial/unstable" in body
    assert "retained entries unverified" in body
    assert cache.get(tmp_path).file("m.py") is first.file("m.py")


def test_failed_reparse_evicts_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _source(tmp_path)
    cache = RepoCache()
    cache.get(tmp_path)
    path.write_text("def changed_name(): pass\n", encoding="utf-8")

    def denied(*args, **kwargs):
        raise PermissionError("fixture")

    monkeypatch.setattr(cache_mod, "index_module", denied)
    idx = cache.get(tmp_path)
    assert idx.file("m.py") is None
    assert idx.observation is not None
    assert "PermissionError; omitted" in idx.observation.issues[0]


def test_root_disappearing_during_walk_drops_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _source(root)
    cache = RepoCache()
    cache.get(root)

    def vanished(root: Path):
        (root / "m.py").unlink()
        root.rmdir()
        yield from ()

    monkeypatch.setattr(cache_mod, "_walk_python_files", vanished)
    with pytest.raises(NotADirectoryError, match="disappeared"):
        cache.get(root)
    assert root not in cache.known_roots()


def test_aliases_share_one_git_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(tmp_path)
    calls: list[Path] = []

    def observe(root: Path) -> str:
        calls.append(root)
        return "Git unknown fixture"

    monkeypatch.setattr(provenance, "_observe_git", observe)
    handler = PythonHandler(hub=Hub(), roots={"one": tmp_path, "two": tmp_path})
    assert "alias='two'" in handler.search(q="hello").body
    assert calls == [tmp_path]


def test_git_partial_observation_scoped_budget_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source(tmp_path)
    monkeypatch.setenv("GIT_DIR", "/unrelated")
    calls = []

    def git(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs["timeout"] <= 2
        assert "GIT_DIR" not in kwargs["env"]
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        if "--show-toplevel" in args:
            return subprocess.CompletedProcess(args, 0, str(tmp_path), "")
        if "config" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 128, "", "fixture")

    monkeypatch.setattr(provenance.subprocess, "run", git)
    body = _handler(tmp_path).search(q="hello").body
    assert "partial observation" in body and "head='unknown'" in body
    assert len(calls) == 5
    assert calls[-1][-2:] == ["--", "."]


def test_git_time_budget_exhaustion_stays_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter((0.0, 3.0))
    monkeypatch.setattr(provenance.time, "monotonic", lambda: next(times))
    body = provenance._observe_git(tmp_path)
    assert "TimeoutExpired" in body and "head='unknown'" in body


def test_index_without_observation_is_unknown_and_collectors_are_call_local(
    tmp_path: Path,
) -> None:
    path = _source(tmp_path)
    idx = RepoIndex.build(tmp_path, [index_module(path, qualname="m")])
    # Outside a read context, collectors have no side effects.
    provenance.note_index("r", idx)
    provenance.note_file(idx, idx.modules["m"])
    provenance.note_external(path, b"", "fixture")
    assert provenance.indexed_file("r", "m.py") is None

    class Standalone:
        roots = {"r": tmp_path}

        @provenance.with_provenance
        def get(self, **kwargs):
            from precis.response import Response

            provenance.note_index("r", idx)
            return Response(body="fixture", cost="preserved", transient=True)

    response = Standalone().get(id="r")
    assert "unknown (no cache observation)" in response.body
    assert response.cost == "preserved" and response.transient


def test_runtrace_provenance_does_not_attest_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import _python_runtrace as runtrace

    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    _source(root)
    _source(other)
    monkeypatch.setenv("PRECIS_PYTHON_ALLOW_EXEC", "1")
    result = runtrace.TraceResult(
        ok=True, events=(), truncated=False, exit_code=0, elapsed_s=0.0
    )
    monkeypatch.setattr(runtrace, "run_trace", lambda **kwargs: result)
    body = (
        _handler(root, other=other)
        .get(id="r", view="runtrace", entry="m:hello", cross_repo=True)
        .body
    )
    assert "alias='other'" in body
    assert "does not attest executed bytes" in body


def test_navigation_never_executes_git_filters_or_monitors(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git not installed")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    _source(tmp_path)
    _git(tmp_path, "add", "m.py")
    _git(tmp_path, "commit", "-m", "fixture")
    sentinel = tmp_path / "must-not-execute"
    _git(tmp_path, "config", "core.fsmonitor", f"touch '{sentinel}'")
    _git(tmp_path, "config", "filter.fixture.clean", f"touch '{sentinel}'")
    _git(tmp_path, "config", "filter.fixture.process", f"touch '{sentinel}'")
    (tmp_path / ".gitattributes").write_text("*.py filter=fixture\n", encoding="utf-8")
    _source(tmp_path, "def changed(): return 2\n")
    body = _handler(tmp_path).get(id="r").body
    assert "dirty='unknown'" in body and "executable filters" in body
    assert not sentinel.exists()


def test_large_source_first_page_keeps_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis._pagination import PaginationCache

    _source(tmp_path, "value = 1\n" * 2000)
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "4096")
    body = _handler(tmp_path).get(id="r/m.py", view="source").body
    page, cursor = PaginationCache().split(body, kind="python")
    assert cursor is not None
    assert page.startswith(body.splitlines()[0])
    assert "Python provenance: alias='r'" in page
    assert "Indexed file: 'm.py'; bytes_sha256=" in page
    assert "Git separately observed:" in page
    assert "Python content:" in body


def test_aliases_of_mutable_root_keep_independent_file_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _source(tmp_path)
    old_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    handler = PythonHandler(hub=Hub(), roots={"one": tmp_path, "two": tmp_path})
    get = handler.cache.get
    calls = 0

    def change_between_aliases(root: Path) -> RepoIndex:
        nonlocal calls
        idx = get(root)
        calls += 1
        if calls == 1:
            path.write_text("def changed_name(): return 2\n", encoding="utf-8")
        return idx

    monkeypatch.setattr(handler.cache, "get", change_between_aliases)
    body = handler.search(q="m", page_size=100).body
    new_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    one_block = body.split("Python provenance: alias='one'", 1)[1].split(
        "Python provenance: alias='two'", 1
    )[0]
    two_block = body.split("Python provenance: alias='two'", 1)[1].split(
        "Python content:", 1
    )[0]
    assert old_digest in one_block and new_digest not in one_block
    assert new_digest in two_block and old_digest not in two_block
    assert "one::m.hello" in body and "two::m.changed_name" in body
