"""Tests for ``precis.sim.verify`` (sim-harness slice 1, item 4).

Coverage:

* **scan / query / coerce** — pure unit tests: which entries get flagged,
  the lit-search query shape, and the judge-verdict bias-safety.
* **plan_verify** — with injected fake search/judge: a flip happens only when
  the judge clears an entry *and* its ``citation_ref`` resolves to a real hit
  (a hallucinated handle degrades to no flip).
* **writeback** — the targeted YAML text edit preserves comments and yields a
  minimal, per-entry diff.
* **verify_sim --dry-run (AC #4)** — records + the exact YAML diff are produced
  while the file, git, and precis are all left untouched.
* **verify_sim live** — the write side flips the YAML on disk, commits it on a
  ``precis-verify/<date>`` branch, mints a ``material``, and appends a quest
  deed (AC #5 shape, driven offline via fakes).
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.sim.manifest import SimManifest
from precis.sim.registry import SimEntry
from precis.sim.verify import (
    FlaggedEntry,
    JudgeVerdict,
    SearchHit,
    _coerce_verdict,
    _material_slug,
    build_query,
    plan_verify,
    render_writebacks,
    scan_entries,
    verify_sim,
)
from precis.store import Store

# ── fixtures ────────────────────────────────────────────────────────────

_MATERIALS_YAML = """\
# materials library — every entry unverified until checked
materials:

  - id: al_6061_t6
    name: Aluminum 6061-T6      # common structural alloy
    tier: realistic
    E_GPa: 68.9
    rho_kg_m3: 2700
    source: "MatWeb hint"
    verified: false

  - id: unobtainium
    name: Unobtainium
    tier: speculative
    E_GPa: 999
    source: "guess"
    verified: false

  - id: already_done
    name: Steel A36
    E_GPa: 200
    source: ["asm~4"]
    verified: true
"""


def _git(path: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *cmd], cwd=path, check=True, capture_output=True, text=True
    )


def _init_git_repo(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed fixture sim")


@pytest.fixture
def sim_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-sim"
    repo.mkdir()
    (repo / "materials.yaml").write_text(_MATERIALS_YAML, encoding="utf-8")
    _init_git_repo(repo)
    return repo


@pytest.fixture
def manifest() -> SimManifest:
    return SimManifest(
        run="python run.py",
        outputs=(),
        verify=("materials.yaml",),
        writeup="fixture-writeup",
    )


def _entry(sim_repo: Path, *, quest: str | None = None) -> SimEntry:
    return SimEntry(
        slug="fixture-sim",
        path=sim_repo,
        git_remote=None,
        manifest=Path("precis.sim.yaml"),
        quest=quest,
    )


def _hit(handle: str = "matweb06~3") -> SearchHit:
    return SearchHit(
        handle=handle,
        quote="6061-T6: E = 68.9 GPa, density 2700 kg/m^3",
        ref_slug="matweb06",
        source_kind="paper",
        score=1.0,
    )


def _search_fn(hits: list[SearchHit]) -> Any:
    def _search(query: str) -> list[SearchHit]:
        return list(hits)

    return _search


def _judge_fn(clear_ids: set[str], *, ref: str | None = "matweb06~3") -> Any:
    def _judge(entry: FlaggedEntry, found: list[SearchHit]) -> JudgeVerdict:
        if entry.entry_id in clear_ids and found:
            return JudgeVerdict(True, ref, "supported by the excerpt")
        return JudgeVerdict(False, None, "no support found")

    return _judge


# ── scan ────────────────────────────────────────────────────────────────


def test_scan_flags_unverified_and_low_confidence_only(
    sim_repo: Path, manifest: SimManifest
) -> None:
    flagged = scan_entries(_entry(sim_repo), manifest)
    ids = {f.entry_id for f in flagged}
    # verified:false entries are flagged; verified:true is not.
    assert ids == {"al_6061_t6", "unobtainium"}
    assert all(f.reason == "verified:false" for f in flagged)
    al = next(f for f in flagged if f.entry_id == "al_6061_t6")
    assert al.name == "Aluminum 6061-T6"
    assert al.rel_file == "materials.yaml"


def test_scan_confidence_floor(tmp_path: Path) -> None:
    repo = tmp_path / "sim"
    repo.mkdir()
    (repo / "db.yaml").write_text(
        "rows:\n"
        "  - id: a\n    confidence: 0.5\n"
        "  - id: b\n    confidence: 0.95\n"
        "  - id: c\n    name: plain\n",  # no scheme -> not flagged
        encoding="utf-8",
    )
    manifest = SimManifest(run="x", outputs=(), verify=("db.yaml",), writeup="w")
    flagged = scan_entries(_entry(repo), manifest, floor=0.8)
    assert {f.entry_id for f in flagged} == {"a"}
    assert flagged[0].reason == "confidence<0.8"


def test_scan_missing_verify_file_is_skipped(sim_repo: Path) -> None:
    manifest = SimManifest(
        run="x", outputs=(), verify=("nope.yaml", "materials.yaml"), writeup="w"
    )
    flagged = scan_entries(_entry(sim_repo), manifest)
    assert {f.entry_id for f in flagged} == {"al_6061_t6", "unobtainium"}


# ── query + coerce ────────────────────────────────────────────────────────


def test_build_query_uses_name_and_props(sim_repo: Path, manifest: SimManifest) -> None:
    flagged = scan_entries(_entry(sim_repo), manifest)
    al = next(f for f in flagged if f.entry_id == "al_6061_t6")
    q = build_query(al)
    assert "Aluminum 6061-T6" in q
    assert "realistic" in q
    assert "E_GPa" in q  # a numeric property key is anchored in


def test_coerce_verdict_is_bias_safe() -> None:
    hits = [_hit("matweb06~3")]
    # non-dict -> unverified
    assert _coerce_verdict(None, hits).value_ok is False
    # citation not among hits -> dropped to None, and value_ok forced False
    v = _coerce_verdict(
        {"value_ok": True, "citation_ref": "ghost~9", "note": "n"}, hits
    )
    assert v.value_ok is False
    assert v.citation_ref is None
    # value_ok with a real handle survives
    ok = _coerce_verdict(
        {"value_ok": True, "citation_ref": "matweb06~3", "note": "n"}, hits
    )
    assert ok.value_ok is True
    assert ok.citation_ref == "matweb06~3"


# ── plan_verify ────────────────────────────────────────────────────────────


def test_plan_verify_flips_only_on_resolvable_citation(
    sim_repo: Path, manifest: SimManifest
) -> None:
    flagged = scan_entries(_entry(sim_repo), manifest)
    records = plan_verify(
        flagged,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}),
    )
    by_id = {r.entry: r for r in records}
    assert by_id["al_6061_t6"].will_flip is True
    assert by_id["al_6061_t6"].citation_quote  # resolved from the hit
    assert by_id["unobtainium"].will_flip is False


def test_plan_verify_hallucinated_handle_does_not_flip(
    sim_repo: Path, manifest: SimManifest
) -> None:
    flagged = scan_entries(_entry(sim_repo), manifest)
    # judge "clears" al_6061_t6 but names a handle not in the hits
    records = plan_verify(
        flagged,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}, ref="ghost~9"),
    )
    al = next(r for r in records if r.entry == "al_6061_t6")
    assert al.will_flip is False


# ── writeback rendering ────────────────────────────────────────────────────


def test_render_writeback_is_minimal_and_preserves_comments(
    sim_repo: Path, manifest: SimManifest
) -> None:
    flagged = scan_entries(_entry(sim_repo), manifest)
    records = plan_verify(
        flagged,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}),
    )
    diffs = render_writebacks(records)
    assert len(diffs) == 1
    diff = diffs[0].diff
    # only the two changed lines for the one flipped entry
    assert "+    verified: true" in diff
    assert "-    verified: false" in diff
    assert "matweb06~3" in diff  # citation folded into source
    assert "MatWeb hint" in diff  # original hint preserved in the list
    # only al_6061_t6's lines are *changed* — unobtainium may appear as
    # unified-diff context, but never as an added/removed line.
    changed = [
        ln
        for ln in diff.splitlines()
        if ln[:1] in {"+", "-"} and not ln.startswith(("+++", "---"))
    ]
    assert not any("unobtainium" in ln for ln in changed)
    assert sum(1 for ln in changed if "verified: true" in ln) == 1
    # the file's comments + untouched entries survive
    assert "# common structural alloy" in diffs[0].new_text
    assert "already_done" in diffs[0].new_text


# ── verify_sim --dry-run (AC #4) ───────────────────────────────────────────


def test_verify_sim_dry_run_writes_nothing(
    sim_repo: Path, manifest: SimManifest
) -> None:
    before = (sim_repo / "materials.yaml").read_text(encoding="utf-8")
    entry = _entry(sim_repo)

    outcome = verify_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=manifest,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}),
        dry_run=True,
    )

    # AC #4: record shape + the exact YAML diff, one flip.
    assert outcome.applied is False
    assert outcome.branch is None
    assert outcome.flagged == 2
    assert outcome.verified == 1
    assert {r.entry for r in outcome.records} == {"al_6061_t6", "unobtainium"}
    assert len(outcome.diffs) == 1
    assert "verified: true" in outcome.diffs[0].diff

    # No file write.
    assert (sim_repo / "materials.yaml").read_text(encoding="utf-8") == before
    # No git side effect: still on the seed commit, clean tree, no verify branch.
    assert _git(sim_repo, "status", "--porcelain").stdout == ""
    branches = _git(sim_repo, "branch", "--list", "precis-verify/*").stdout
    assert branches.strip() == ""


def test_verify_sim_dry_run_no_flips_when_judge_rejects_all(
    sim_repo: Path, manifest: SimManifest
) -> None:
    outcome = verify_sim(
        slug="fixture-sim",
        entry=_entry(sim_repo),
        manifest=manifest,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn(set()),  # clears nothing
        dry_run=True,
    )
    assert outcome.verified == 0
    assert outcome.diffs == ()


# ── verify_sim live write side (AC #5 shape, offline via fakes) ─────────────


def test_verify_sim_live_flips_commits_and_mints(
    store: Store, hub: Hub, sim_repo: Path, manifest: SimManifest
) -> None:
    # Seed a quest and link it via the registry entry (AC #6 linkage).
    from precis.handlers.quest import QuestHandler

    resp = QuestHandler(hub=Hub(store=store)).put(
        text="Keep the fixture-sim material library verified"
    )
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    qid = int(m.group(1))

    entry = _entry(sim_repo, quest=str(qid))

    outcome = verify_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=manifest,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}),
        dry_run=False,
        store=store,
        hub=hub,
        today=_dt.date(2026, 1, 2),
    )

    assert outcome.applied is True
    assert outcome.verified == 1
    assert outcome.branch == "precis-verify/2026-01-02"

    # YAML flipped on disk.
    text = (sim_repo / "materials.yaml").read_text(encoding="utf-8")
    assert "verified: true" in text
    assert "matweb06~3" in text

    # Committed on the verify branch (not the default branch).
    head_branch = _git(sim_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head_branch == "precis-verify/2026-01-02"
    log = _git(sim_repo, "log", "-1", "--pretty=%s").stdout
    assert "fixture-sim" in log
    # The flip is committed, so the tree is clean again.
    assert _git(sim_repo, "status", "--porcelain").stdout == ""

    # material entity minted.
    mat = store.get_ref(kind="material", id=_material_slug("al_6061_t6"))
    assert mat is not None
    assert mat.title == "Aluminum 6061-T6"

    # quest deed appended (a milestone in the logbook).
    blocks = store.list_blocks_for_ref(qid)
    deeds = [
        b
        for b in blocks
        if (b.meta or {}).get("entry_type") == "milestone"
        and "sim verify fixture-sim" in b.text
    ]
    assert len(deeds) == 1
    assert (deeds[0].meta or {}).get("by") == "system"


def test_verify_sim_live_no_quest_skips_deed_gracefully(
    store: Store, hub: Hub, sim_repo: Path, manifest: SimManifest
) -> None:
    outcome = verify_sim(
        slug="fixture-sim",
        entry=_entry(sim_repo, quest=None),
        manifest=manifest,
        search_fn=_search_fn([_hit("matweb06~3")]),
        judge_fn=_judge_fn({"al_6061_t6"}),
        dry_run=False,
        store=store,
        hub=hub,
        today=_dt.date(2026, 1, 2),
    )
    assert outcome.applied is True
    assert any("no quest linked" in msg for msg in outcome.messages)
