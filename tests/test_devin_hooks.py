"""Generated .devin/hooks.v1.json must track .claude/settings.json.

Devin's Claude importer rejects all of .claude/settings.json when any hook
event is outside its allow-list (gr515151), so the project hook set is kept
as a generated Devin-format mirror. These tests pin both directions: the
committed file equals a fresh render (no drift), and it can only ever
contain events Devin accepts.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_loader = SourceFileLoader(
    "devin_hooks", str(REPO / "scripts" / "lib" / "devin_hooks.py")
)
_spec = importlib.util.spec_from_loader("devin_hooks", _loader)
assert _spec is not None
dh = importlib.util.module_from_spec(_spec)
_loader.exec_module(dh)


def _parse(path: Path) -> dict:
    # hooks.v1.json must be STRICT json — the Devin hooks-file parser rejects
    # JSONC comments ("expected value at line 1 column 1"), so no generated
    # header line: parse the file raw to keep it that way.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "hooks.v1.json must be a non-empty object"
    return data


def test_committed_file_equals_fresh_render() -> None:
    fresh = dh.render(dh.load())
    have = dh.OUT.read_text(encoding="utf-8")
    assert have == fresh, (
        ".devin/hooks.v1.json is stale: run scripts/gen-devin-hooks render"
    )


def test_only_devin_allowed_events() -> None:
    data = _parse(dh.OUT)
    assert set(data) <= dh.DEVIN_EVENTS, set(data) - dh.DEVIN_EVENTS


def test_dropped_claude_events_are_not_in_devin_events() -> None:
    # Guard the guard: if .claude/settings.json grows an event we already
    # translate, or Devin grows support for one we drop, the mirror's event
    # set is exactly source ∩ allow-list — nothing silently appears/vanishes.
    source_events = set(dh.load()["hooks"])
    data = _parse(dh.OUT)
    assert set(data) == source_events & dh.DEVIN_EVENTS


def test_render_check_is_the_check_verdict(tmp_path: Path, monkeypatch) -> None:
    """`render --check` is the --quick lane's spelling: fresh mirror exits 0
    and writes nothing; a stale one exits 1 and names the render command
    (gr517403)."""
    out = tmp_path / "hooks.v1.json"
    monkeypatch.setattr(dh, "OUT", out)
    out.write_text(dh.render(dh.load()), encoding="utf-8")
    assert dh.main(["render", "--check"]) == 0
    out.write_text("{}\n", encoding="utf-8")
    assert dh.main(["render", "--check"]) == 1


def test_check_names_the_render_command(tmp_path: Path, monkeypatch, capsys) -> None:
    out = tmp_path / "hooks.v1.json"
    monkeypatch.setattr(dh, "OUT", out)
    out.write_text("{}\n", encoding="utf-8")
    assert dh.main(["check"]) == 1
    assert "next=scripts/gen-devin-hooks render" in capsys.readouterr().err
