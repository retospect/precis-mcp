"""``view='parameterize'`` — the parameter-extracting rewrite (gr521589).

The pure core ``precis_se.parameterize.parameterize`` turns
``(script, call-site line, arg name)`` into a unified diff plus a
one-line summary; the se view resolves ``frame:<key>`` against the
design's trace and the workspace folder's script, then renders the
proposal — nothing is written.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.parameterize import parameterize

SCRIPT = (
    "import precis_geom.out as out\n"
    "\n"
    "\n"
    "def build(o):\n"
    '    o.block("plate", "p add box:w40mmd20mmh10mm")\n'
    '    o.ray("r1", block="plate", length=0.028)\n'
)


def test_int_literal() -> None:
    rw = parameterize(
        "def build(o):\n    o.ray('r', count=28)\n",
        2,
        "count",
        filename="main.py",
    )
    assert rw.name == "COUNT"
    assert rw.default == "28"
    assert rw.scope == "main.build"
    assert "+def build(o, COUNT=28):" in rw.diff
    assert "+    o.ray('r', count=COUNT)" in rw.diff


def test_float_with_units_keeps_the_unit() -> None:
    rw = parameterize(
        "def plate(o):\n    o.ray('r', length=2.5*mm)\n",
        2,
        "length",
        filename="main.py",
    )
    assert rw.name == "LENGTH"
    assert rw.default == "2.5"
    assert "+def plate(o, LENGTH=2.5):" in rw.diff
    assert "+    o.ray('r', length=LENGTH*mm)" in rw.diff


def test_already_parameterized_refuses() -> None:
    with pytest.raises(BadInput) as exc:
        parameterize(
            "def build(o):\n    H = 12\n    o.ray('r', k=H)\n",
            3,
            "k",
            filename="main.py",
        )
    assert "already a variable" in str(exc.value)
    assert exc.value.next


def test_duplicate_literals_listed_not_rewritten() -> None:
    script = (
        "def build(o):\n"
        "    a = o.ray('r', length=0.028)\n"
        "    b = o.ray('s', length=0.028)\n"
    )
    rw = parameterize(script, 2, "length", filename="main.py")
    assert "+    a = o.ray('r', length=LENGTH)" in rw.diff
    assert "     b = o.ray('s', length=0.028)" in rw.diff
    assert "o.ray('s', length=LENGTH)" not in rw.diff
    assert len(rw.candidates) == 1
    assert rw.candidates[0].line == 3
    assert "other occurrence" in rw.summary


def test_module_scope_inserts_an_assignment() -> None:
    script = "with out.run('x') as o:\n    o.ray('r', length=0.028)\n"
    rw = parameterize(script, 2, "length", filename="main.py")
    assert rw.scope == "<module>"
    assert "+LENGTH = 0.028\n" in rw.diff
    assert "+    o.ray('r', length=LENGTH)" in rw.diff


def test_kwargs_splat_keeps_parameter_in_front() -> None:
    rw = parameterize(
        "def build(o, **kw):\n    o.ray('r', length=4)\n",
        2,
        "length",
        filename="main.py",
    )
    assert "+def build(o, LENGTH=4, **kw):" in rw.diff


def test_positional_arg_found_by_recorded_value() -> None:
    rw = parameterize(
        "def build(o):\n    o.ray('r', 'plate', 0.028)\n",
        2,
        "length",
        value=0.028,
        filename="main.py",
    )
    assert "+def build(o, LENGTH=0.028):" in rw.diff
    assert "+    o.ray('r', 'plate', LENGTH)" in rw.diff


def test_src_relocates_a_drifted_line() -> None:
    script = (
        "# comment added after the run\n"
        "def build(o):\n"
        "    a = o.ray('r', length=0.028)\n"
    )
    rw = parameterize(
        script,
        1,
        "length",
        src="a = o.ray('r', length=0.028)",
        filename="main.py",
    )
    assert rw.literal_line == 3
    assert "length=LENGTH" in rw.diff


def test_moved_call_refuses() -> None:
    with pytest.raises(BadInput, match="moved or was removed"):
        parameterize(
            "def build(o):\n    pass\n",
            2,
            "length",
            src="a = o.ray('r', length=0.028)",
            filename="main.py",
        )


def test_expression_arg_refuses() -> None:
    with pytest.raises(BadInput) as exc:
        parameterize(
            "def build(o):\n    o.ray('r', length=base + 2)\n",
            2,
            "length",
            filename="main.py",
        )
    assert "not a literal" in str(exc.value)


# ── the se view ──────────────────────────────────────────────────


def _se(store: Store) -> SeHandler:
    return SeHandler(hub=Hub(store=store))


_TRACE: dict[str, Any] = {
    "schema": 3,
    "run": {"workspace": "ws-param"},
    "frames": [
        {
            "id": 0,
            "key": "root0000",
            "parent": None,
            "qual": "main",
            "kind": "root",
            "file": "main.py",
            "line": 1,
            "line_in_def": 0,
            "src": None,
            "occ": 0,
            "args": {},
        },
        {
            "id": 1,
            "key": "build001",
            "parent": "root0000",
            "qual": "main.build",
            "kind": "user",
            "file": "main.py",
            "line": 4,
            "line_in_def": 0,
            "src": None,
            "occ": 0,
            "args": {},
            "made": {},
            "touched": {},
        },
        {
            "id": 2,
            "key": "blk00002",
            "parent": "build001",
            "qual": "out.block",
            "kind": "core",
            "file": "main.py",
            "line": 5,
            "line_in_def": 1,
            "src": 'o.block("plate", "p add box:w40mmd20mmh10mm")',
            "occ": 0,
            "args": {"name": "plate", "geom": "p add box:w40mmd20mmh10mm"},
            "made": {"blocks": 1},
            "touched": {},
        },
        {
            "id": 3,
            "key": "ray00003",
            "parent": "build001",
            "qual": "out.ray",
            "kind": "core",
            "file": "main.py",
            "line": 6,
            "line_in_def": 2,
            "src": 'o.ray("r1", block="plate", length=0.028)',
            "occ": 0,
            "args": {"name": "r1", "block": "plate", "length": 0.028},
            "made": {},
            "touched": {},
        },
    ],
}


def _import(store: Store, *, files: dict[str, str] | None = None) -> None:
    se = _se(store)
    se.put(id="ws-param", text="{}")
    se.edit(
        id="ws-param",
        ops=[
            {
                "op": "import_workspace",
                "workspace": "ws-param",
                "run": {
                    "manifest.json": json.dumps(
                        {
                            "schema": 1,
                            "blocks": [
                                {
                                    "name": "plate",
                                    "file": "blocks/plate.cad",
                                    "scale": "macro",
                                    "frame": 2,
                                }
                            ],
                        }
                    ),
                    "blocks/plate.cad": "p add box:w40mmd20mmh10mm\n",
                    "trace.json": json.dumps(_TRACE),
                },
                **({"files": files} if files is not None else {}),
            }
        ],
    )


def test_view_proposes_the_diff(store: Store) -> None:
    _import(store, files={"main.py": SCRIPT})
    body = (
        _se(store)
        .get(
            id="ws-param",
            view="parameterize",
            args={"frame": "ray00003", "arg": "length"},
        )
        .body
    )
    assert "LENGTH = 0.028" in body
    assert "def build(o, LENGTH=0.028)" in body
    assert "length=LENGTH" in body
    assert "--- a/main.py" in body
    assert "+++ b/main.py" in body
    assert "ws-ws-param--main-py" in body


def test_view_needs_frame_and_arg(store: Store) -> None:
    _import(store, files={"main.py": SCRIPT})
    with pytest.raises(BadInput) as exc:
        _se(store).get(id="ws-param", view="parameterize", args={"arg": "x"})
    assert exc.value.next


def test_view_unknown_frame_and_arg(store: Store) -> None:
    _import(store, files={"main.py": SCRIPT})
    se = _se(store)
    with pytest.raises(BadInput, match="not in any"):
        se.get(
            id="ws-param",
            view="parameterize",
            args={"frame": "nosuchkey", "arg": "length"},
        )
    with pytest.raises(BadInput, match="no 'bogus'"):
        se.get(
            id="ws-param",
            view="parameterize",
            args={"frame": "ray00003", "arg": "bogus"},
        )


def test_view_no_workspace_folder(store: Store) -> None:
    _import(store)
    with pytest.raises(BadInput) as exc:
        _se(store).get(
            id="ws-param",
            view="parameterize",
            args={"frame": "ray00003", "arg": "length"},
        )
    assert "files" in str(exc.value.next or "")
    assert re.search(r"import_workspace", str(exc.value.next))


# ── gr521589 fix round 1 ────────────────────────────────────────────────


def test_dispatch_accepts_the_arg_key(runtime_with_store) -> None:
    """gr521589 fix 1 — through the REAL dispatcher (not ``handler.get``):
    ``args={'frame':…,'arg':…}`` must pass the ARGS gate — 'arg' was
    missing from the se get key set, so the gate refused before the view
    ever ran."""
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "se",
            "__extras__": {
                "view": "parameterize",
                "frame": "ray00003",
                "arg": "length",
            },
        },
    )
    assert "not accepted by se.get" not in out


def test_non_ascii_on_the_line_no_longer_breaks_the_splice() -> None:
    """gr521589 fix 2 — ``ast`` col offsets are UTF-8 BYTE offsets; a µ/°/Ø
    earlier on the line made the literal's char span one column short and
    the rewrite stopped parsing."""
    rw = parameterize(
        "def f(o):\n    x = 'µµ°Ø'; o.ray('r', length=0.028)\n",
        2,
        "length",
        filename="main.py",
    )
    assert "+def f(o, LENGTH=0.028):" in rw.diff
    assert "length=LENGTH" in rw.diff


def test_trailing_comment_gets_the_parameter_before_it() -> None:
    """gr521589 fix 3a — a signature tail ending in a comment used to take
    `, NAME=d` after the comment, drowning it in comment text."""
    rw = parameterize(
        "def f(o, a  # keep me\n    ):\n    o.ray('r', length=3)\n",
        3,
        "length",
        filename="main.py",
    )
    # the comma lands after `a` (before the comment), and the parameter
    # takes its own line above `)` — both edits keep the source parseable.
    assert "+def f(o, a,  # keep me" in rw.diff
    assert "+LENGTH=3,\n" in rw.diff
    assert "length=LENGTH" in rw.diff


def test_unparseable_rewrite_is_badinput_not_syntaxerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr521589 fix 3b — the final ``ast.parse(new)`` proof can still fail
    on a splice edge the proposer cannot see; it must surface as BadInput
    with a next=, never a raw SyntaxError."""
    import ast as _ast

    import precis_se.parameterize as pm

    real_parse = _ast.parse
    calls = {"n": 0}

    def flaky(src, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise SyntaxError("boom")
        return real_parse(src, *a, **k)

    monkeypatch.setattr(pm.ast, "parse", flaky)
    with pytest.raises(BadInput) as exc:
        parameterize(
            "def f(o):\n    o.ray('r', length=0.028)\n",
            2,
            "length",
            filename="main.py",
        )
    assert exc.value.next


def test_identical_call_lines_resolve_through_line_in_def() -> None:
    """gr521589 fix 4 — two identical call lines: the stored src matches
    both, so the frame's own def + ``line_in_def`` disambiguate via
    ``traceview.reanchor`` instead of refusing 'matches N lines'."""
    script = (
        "def a(o):\n    o.ray('r', length=3)\n\ndef b(o):\n    o.ray('r', length=3)\n"
    )
    rw = parameterize(
        script,
        1,  # the stored line is wrong/drifted — identical text on 2 lines
        "length",
        src="o.ray('r', length=3)",
        filename="main.py",
        frame={"file": "main.py", "qual": "main.b", "line_in_def": 1},
    )
    assert rw.literal_line == 5
    assert rw.scope == "main.b"


def test_name_for_avoids_def_class_and_import_names() -> None:
    """gr521589 fix 5 — a parameter named after a def/class/import rebinding
    would hijack the module name; ``_name_for`` skips them."""
    script = "import THING\n\ndef f(o):\n    o.ray('r', thing=3)\n"
    rw = parameterize(script, 4, "thing", filename="main.py")
    assert rw.name == "THING_2"
