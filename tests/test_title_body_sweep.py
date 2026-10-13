"""scripts/_title_body_sweep.py --multi-article — candidate listing (gr353777)."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_title_body_sweep.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_title_body_sweep", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verdict(mod: ModuleType, head: float, tail: float) -> str | None:
    return mod.multi_verdict(head, tail, agree=0.5, disagree=0.3)


def test_foreign_lead_and_tail_are_flagged() -> None:
    mod = _load()
    assert _verdict(mod, 0.0, 0.8) == "lead"
    assert _verdict(mod, 0.8, 0.1) == "tail"


def test_both_ends_agreeing_or_failing_are_not_candidates() -> None:
    mod = _load()
    assert _verdict(mod, 0.7, 0.9) is None
    # Both ends below the misfit ceiling: a plain title/body swap, already
    # covered by the main ranking — not a multi-article candidate.
    assert _verdict(mod, 0.1, 0.2) is None
    # A mid-band score on one side is not a confident split.
    assert _verdict(mod, 0.4, 0.9) is None


def test_corpus_scan_excludes_already_tagged_but_ref_mode_does_not() -> None:
    mod = _load()
    scan = mod._select_multi("r.ref_id > 0", 6, 500, skip_tagged=True)
    assert "tg.value = 'multi-article'" in scan
    assert "rt.expires_at IS NULL OR rt.expires_at > now()" in scan
    recheck = mod._select_multi("r.ref_id = ANY", 6, 3, skip_tagged=False)
    assert "multi-article" not in recheck
    # Head and tail windows come from the same paragraph ordering.
    assert "ORDER BY ord LIMIT 6" in recheck
    assert "ORDER BY ord OFFSET 6" in recheck
    assert "chunk_kind = 'paragraph'" in recheck


def _csv(rows: list[tuple]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


_TITLE = "Graphitic cones and the nucleation of curved carbon surfaces"
_FOREIGN = (
    "We report oscillations of superfluid helium through a weak link. "
    "The Josephson current-phase relation governs the observed period. "
) * 6
_OWN = (
    "Graphitic cones nucleate curved carbon surfaces; the cone angle "
    "follows from the wrapped graphene sheet of carbon. " * 6
)


def _args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        multi_article=True,
        chunks=12,
        head=6,
        agree=0.5,
        disagree=0.3,
        limit=6,
        top=40,
        ref=[],
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_multi_lists_foreign_lead_and_tail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    rows = _csv(
        [
            (207748, _TITLE, _FOREIGN, _OWN),  # foreign lead (the known case)
            (44468, _TITLE, _OWN, _FOREIGN),  # foreign tail
            (1, _TITLE, _OWN, _OWN),  # normal: not a candidate
            (2, _TITLE, _FOREIGN, _FOREIGN),  # full mismatch: not a candidate
            (3, "Untitled", _FOREIGN, _OWN),  # placeholder title: skipped
            (4, _TITLE, "short", _OWN),  # head under the char floor: skipped
        ]
    )
    monkeypatch.setattr(mod, "_psql", lambda sql: rows)
    mod._multi(_args())
    out = capsys.readouterr().out
    assert "lead  pa207748" in out and "tail  pa44468" in out
    for absent in ("pa1 ", "pa2 ", "pa3 ", "pa4 "):
        assert absent not in out
    assert "candidates 2" in out and "skipped 1" in out
    assert "placeholder titles 1" in out


def test_multi_empty_side_is_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_psql", lambda sql: _csv([(9, _TITLE, _OWN, "")]))
    mod._multi(_args(limit=1))
    out = capsys.readouterr().out
    assert "candidates 0" in out and "skipped 1" in out
    assert "pa9" not in out


def test_explicit_refs_skip_the_tag_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load()
    seen: list[str] = []

    def fake_psql(sql: str) -> str:
        seen.append(sql)
        return ""

    monkeypatch.setattr(mod, "_psql", fake_psql)
    list(mod.fetch_multi(6, None, [207748, 44468]))
    assert len(seen) == 1
    assert "multi-article" not in seen[0]
    assert "r.ref_id = ANY(ARRAY[207748,44468]::bigint[])" in seen[0]
