"""Track-A range-content guard: `range: ... sha=` on reads, `base_sha=` on edits."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.python import PythonHandler

SRC = textwrap.dedent(
    """\
    import os


    def boot():
        x = 1
        return x


    def other():
        y = 2
        return y


    PATH = os.sep
    """
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text('"""pkg."""\n', encoding="utf-8")
    (tmp_path / "pkg" / "m.py").write_text(SRC, encoding="utf-8")
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


def _file(repo: Path) -> str:
    return (repo / "pkg" / "m.py").read_text(encoding="utf-8")


def _sha(body: str) -> str:
    m = re.search(r"^range: L\d+-L?\d+ sha=([0-9a-f]{8})$", body, re.M)
    assert m, body
    return m.group(1)


def _insert_above(repo: Path, n: int = 3) -> None:
    p = repo / "pkg" / "m.py"
    p.write_text("# pad\n" * n + _file(repo), encoding="utf-8")


def test_read_emits_range_sha(handler: PythonHandler) -> None:
    body = handler.get(id="r/pkg/m.py~L5-L6").body
    assert re.search(r"^range: L5-6 sha=[0-9a-f]{8}$", body, re.M)
    sha = _sha(body)
    # Same bytes -> same sha; documented definition.
    import hashlib

    want = hashlib.sha256(b"    x = 1\n    return x\n").hexdigest()[:8]
    assert sha == want


def test_symbol_source_read_emits_range_sha(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.m.boot", view="source").body
    assert "range: L4-6 sha=" in body
    assert _sha(body) == _sha(handler.get(id="r/pkg/m.py~L4-L6").body)


def test_matching_base_sha_applies(handler: PythonHandler, repo: Path) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5").body)
    r = handler.edit(
        id="r/pkg/m.py~L5", mode="replace", text="    x = 10", base_sha=sha
    ).body
    assert "x = 10" in _file(repo)
    assert "relocated" not in r
    assert "hint: pass base_sha" not in r
    assert re.search(r"^range: L5 sha=|^range: L5-5 sha=", r, re.M)


def test_shifted_lines_are_relocated(handler: PythonHandler, repo: Path) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L10-L11").body)
    _insert_above(repo, 3)
    r = handler.edit(
        id="r/pkg/m.py~L10-L11",
        mode="replace",
        text="    y = 20\n    return y",
        base_sha=sha,
    ).body
    assert "relocated: L10-L11 -> L13-L14 (content moved by 3 lines down)" in r
    f = _file(repo)
    assert "y = 20" in f and "y = 2\n" not in f
    assert "x = 1" in f  # boot untouched
    assert "range: L13-14 sha=" in r


def test_changed_content_refused_file_untouched(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L10-L11").body)
    p = repo / "pkg" / "m.py"
    p.write_text(_file(repo).replace("y = 2", "y = 3"), encoding="utf-8")
    before = _file(repo)
    with pytest.raises(BadInput) as ei:
        handler.edit(
            id="r/pkg/m.py~L10-L11", mode="replace", text="    pass", base_sha=sha
        )
    msg = str(ei.value)
    assert "changed since read" in msg and "current sha=" in msg
    assert "r::pkg.m.other" in (ei.value.next or "") or "r::pkg.m.other" in msg
    assert _file(repo) == before


def test_duplicate_content_refused_with_candidates(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5-L6").body)
    p = repo / "pkg" / "m.py"
    p.write_text(
        _file(repo) + "\n\ndef boot2():\n    x = 1\n    return x\n", encoding="utf-8"
    )
    # Disturb the original so the address no longer matches; two copies of
    # the old content are impossible there, so duplicate it instead.
    p.write_text(
        "# pad\n" + _file(repo).replace("def boot2", "def boot3"), encoding="utf-8"
    )
    before = _file(repo)
    with pytest.raises(BadInput) as ei:
        handler.edit(
            id="r/pkg/m.py~L5-L6", mode="replace", text="    pass", base_sha=sha
        )
    msg = str(ei.value)
    assert "matches 2 places" in msg and "L6-L7" in msg
    assert _file(repo) == before


def test_no_base_sha_applies_with_hint(handler: PythonHandler, repo: Path) -> None:
    r = handler.edit(id="r/pkg/m.py~L5", mode="replace", text="    x = 7").body
    assert "x = 7" in _file(repo)
    assert "hint: pass base_sha=" in r


def test_qualname_edit_has_no_hint(handler: PythonHandler) -> None:
    r = handler.edit(
        id="r::pkg.m.boot",
        mode="replace",
        text="def boot():\n    return 1",
    ).body
    assert "hint: pass base_sha" not in r and "range:" not in r


def test_base_sha_on_qualname_id_rejected(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.edit(
            id="r::pkg.m.boot", mode="replace", text="def boot(): ...", base_sha="0" * 8
        )


def test_dry_run_reports_relocation_without_writing(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L10-L11").body)
    _insert_above(repo, 2)
    before = _file(repo)
    r = handler.edit(
        id="r/pkg/m.py~L10-L11",
        mode="replace",
        text="    y = 20\n    return y",
        base_sha=sha,
        dry_run=True,
    ).body
    assert "relocated: L10-L11 -> L12-L13" in r
    assert _file(repo) == before


def test_find_replace_and_insert_are_guarded(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5-L6").body)
    _insert_above(repo, 1)
    r = handler.edit(
        id="r/pkg/m.py~L5-L6",
        mode="find-replace",
        find="x = 1",
        text="x = 5",
        base_sha=sha,
    ).body
    assert "relocated: L5-L6 -> L6-L7" in r
    assert "x = 5" in _file(repo)
    sha2 = _sha(r)
    r2 = handler.edit(
        id="r/pkg/m.py~L6-L7",
        mode="insert",
        find="return x",
        text="    # done",
        where="after",
        base_sha=sha2,
    ).body
    assert "relocated" not in r2
    assert "# done" in _file(repo)


def test_chained_edit_uses_sha_from_previous_response(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5-L6").body)
    r1 = handler.edit(
        id="r/pkg/m.py~L5-L6",
        mode="replace",
        text="    x = 1\n    x += 1\n    return x",
        base_sha=sha,
    ).body
    assert "range: L5-7 sha=" in r1
    sha2 = _sha(r1)
    # Something shifts the file between the two edits.
    _insert_above(repo, 2)
    r2 = handler.edit(
        id="r/pkg/m.py~L5-L7",
        mode="replace",
        text="    return 42",
        base_sha=sha2,
    ).body
    assert "relocated: L5-L7 -> L7-L9" in r2
    assert "return 42" in _file(repo)
    assert "range: L7 sha=" in r2 or "range: L7-7 sha=" in r2


# ── delete ──────────────────────────────────────────────────────────


def test_delete_matching_base_sha_applies(handler: PythonHandler, repo: Path) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5").body)
    r = handler.delete(id="r/pkg/m.py~L5", base_sha=sha).body
    assert "x = 1" not in _file(repo)
    assert "relocated" not in r and "hint: pass base_sha" not in r


def test_delete_shifted_lines_are_relocated(handler: PythonHandler, repo: Path) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5").body)
    _insert_above(repo, 3)
    r = handler.delete(id="r/pkg/m.py~L5", base_sha=sha).body
    assert "relocated: L5-L5 -> L8-L8" in r
    f = _file(repo)
    assert "x = 1" not in f and "return x" in f and f.startswith("# pad\n" * 3)


def test_delete_changed_content_refused_file_untouched(
    handler: PythonHandler, repo: Path
) -> None:
    sha = _sha(handler.get(id="r/pkg/m.py~L5").body)
    (repo / "pkg" / "m.py").write_text(
        _file(repo).replace("x = 1", "x = 9"), encoding="utf-8"
    )
    before = _file(repo)
    with pytest.raises(BadInput) as ei:
        handler.delete(id="r/pkg/m.py~L5", base_sha=sha)
    assert "changed since read" in str(ei.value)
    assert _file(repo) == before


def test_delete_without_base_sha_hints(handler: PythonHandler, repo: Path) -> None:
    r = handler.delete(id="r/pkg/m.py~L5").body
    assert "hint: pass base_sha=" in r
    assert "x = 1" not in _file(repo)


def test_delete_qualname_unchanged_and_rejects_base_sha(
    handler: PythonHandler, repo: Path
) -> None:
    with pytest.raises(BadInput):
        handler.delete(id="r::pkg.m.other", base_sha="0" * 8)
    r = handler.delete(id="r::pkg.m.other").body
    assert "hint: pass base_sha" not in r
    assert "def other" not in _file(repo)


# ── post-format range ───────────────────────────────────────────────


def test_range_survives_ruff_removing_unused_import_above(
    handler: PythonHandler, repo: Path
) -> None:
    p = repo / "pkg" / "m.py"
    p.write_text("import sys\n" + _file(repo), encoding="utf-8")  # unused import
    sha = _sha(handler.get(id="r/pkg/m.py~L6-L7").body)
    r = handler.edit(
        id="r/pkg/m.py~L6-L7",
        mode="replace",
        text="    x = 1\n    x += 1\n    return x",
        base_sha=sha,
    ).body
    assert "import sys" not in _file(repo)  # ruff dropped it: lines shifted up
    m = re.search(r"^range: L(\d+)-(\d+) sha=([0-9a-f]{8})$", r, re.M)
    assert m, r
    a, b, got = int(m[1]), int(m[2]), m[3]
    fresh = handler.get(id=f"r/pkg/m.py~L{a}-L{b}").body
    assert _sha(fresh) == got
    assert "x += 1" in fresh and "return x" in fresh and b - a == 2


def test_range_unknown_when_unmappable(handler: PythonHandler, repo: Path) -> None:
    from precis.handlers import python as pymod

    guard = pymod._Guard(5, 6, 14, [])
    pymod._LAST_FORMAT.set(_file(repo))
    # post shares nothing with the pre-format buffer -> never a guessed sha
    out = PythonHandler._post_edit_range("a = 1\nb = 2\n", guard)
    assert out == "range: unknown after formatting — re-read"


def test_range_unknown_without_format_record() -> None:
    from precis.handlers import python as pymod

    pymod._LAST_FORMAT.set(None)
    out = PythonHandler._post_edit_range("a = 1\n", pymod._Guard(1, 1, 1, []))
    assert out == "range: unknown after formatting — re-read"


def test_range_deleted_when_region_emptied() -> None:
    from precis.handlers import python as pymod

    pymod._LAST_FORMAT.set("a = 1\nc = 3\n")  # region L2-L3 removed from 4 lines
    out = PythonHandler._post_edit_range("a = 1\nc = 3\n", pymod._Guard(2, 3, 4, []))
    assert out == "range: (deleted)"


def test_range_found_by_text_search_when_edges_ambiguous() -> None:
    from precis.handlers import python as pymod

    # The formatter inserts a line inside the region: diff edges are not
    # mappable, so the replacement text is located in the final file instead.
    pre = "a\nx = 1\ny = 2\nb\n"
    post = "a\nq\nx = 1\ny = 2\nb\n"
    pymod._LAST_FORMAT.set(pre)
    out = PythonHandler._post_edit_range(post, pymod._Guard(2, 3, 4, []))
    assert re.match(r"range: L3-L?4 sha=[0-9a-f]{8}$", out), out
