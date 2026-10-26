"""``precis gripe-fix import`` — the file-era coordinator state onto gripes.

Dry run by default (a plan, no writes); ``--apply`` writes through
``GripeHandler.edit(mode='fix')`` and a second apply is a no-op
(gr519361, docs/backlog/gripe-fix-loop-on-the-gripe.md).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from precis.cli import gripe_fix
from precis.dispatch import Hub
from precis.handlers import _gripe_fix as gf
from precis.handlers.gripe import GripeHandler
from tests._refs import insert_gripe


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _state_dir(tmp_path: Path, ids: list[int]) -> Path:
    a, b, c, d = ids
    root = tmp_path / "state"
    (root / "briefs").mkdir(parents=True)
    (root / "census").mkdir()
    (root / "census" / "agg.py").write_text("print(1)\n", encoding="utf-8")
    (root / "batches.txt").write_text(
        "# name|gripes|files|lane|notes\n"
        f"tables-s2|{a} {b}|src/x.py|checked|two gripes\n"
        f"ops-a|{c}|scripts/deploy|remote|\n",
        encoding="utf-8",
    )
    (root / "briefs" / "tables-s2-start.md").write_text(
        "Task: tables slice 2. Rebase often.\n", encoding="utf-8"
    )
    # no brief file for ops-a → synthesized from the row
    # a brief with no batches.txt row: members inferred from the text
    (root / "briefs" / "feat-a-start.md").write_text(
        f"Gripes: gr{d}. Build the feature.\n", encoding="utf-8"
    )
    (root / "tables-s2-fix.md").write_text(
        "Fix round: make mypy green.\n", encoding="utf-8"
    )
    (root / "handoff-devin-7.md").write_text(
        "this tree now runs tables-s2\n\nHead abc1234 on worktree-tables-s2.\n",
        encoding="utf-8",
    )
    (root / "tables-s2-review.md").write_text(
        "# review\nVerdict: LAND. No must-fixes.\n", encoding="utf-8"
    )
    (root / "awaiting-deploy.txt").write_text(
        f"Close after deploy:\nab12cd34e: {c}\nPartial: {d} (slice 1 only)\n",
        encoding="utf-8",
    )
    (root / "close-batch-r104.txt").write_text(
        f"{d} 2633233b9 feat-a: landed the feature (r104)\n", encoding="utf-8"
    )
    (root / "queue.txt").write_text("narrative\n", encoding="utf-8")
    (root / "do.sh").write_text("echo\n", encoding="utf-8")
    # The plan applies in file-time order; a fixture written in one
    # second needs the real-life spacing stamped on.
    base = 1_700_000_000
    for i, name in enumerate(
        (
            "batches.txt",
            "briefs/tables-s2-start.md",
            "briefs/feat-a-start.md",
            "tables-s2-fix.md",
            "handoff-devin-7.md",
            "tables-s2-review.md",
            "awaiting-deploy.txt",
            "close-batch-r104.txt",
        )
    ):
        os.utime(root / name, (base + 60 * i, base + 60 * i))
    return root


def test_plan_maps_every_file_shape(tmp_path: Path) -> None:
    ids = [514001, 514002, 514003, 4]  # a small id works in an id column
    plan = gripe_fix.plan_import(_state_dir(tmp_path, ids))
    kinds = {(it.kind, it.batch, it.source) for it in plan.items}
    assert ("batch", "tables-s2", "briefs/tables-s2-start.md") in kinds
    assert ("batch", "ops-a", "batches.txt") in kinds
    assert ("batch", "feat-a", "briefs/feat-a-start.md") in kinds
    assert ("fix-brief", "tables-s2", "tables-s2-fix.md") in kinds
    assert ("handoff", "tables-s2", "handoff-devin-7.md") in kinds
    assert ("review", "tables-s2", "tables-s2-review.md") in kinds
    assert ("landed", None, "awaiting-deploy.txt") in kinds
    assert ("landed", "feat-a", "close-batch-r104.txt") in kinds
    feat = next(it for it in plan.items if it.batch == "feat-a" and it.kind == "batch")
    assert feat.inferred and feat.ids == (4,)
    review = next(it for it in plan.items if it.kind == "review")
    assert review.verdict == "land"
    assert review.patch() == {
        "review": "land",
        "findings": "# review\nVerdict: LAND. No must-fixes.",
    }
    synth = next(it for it in plan.items if it.batch == "ops-a")
    assert synth.text.startswith("[imported from batches.txt]")
    assert plan.gripes == set(ids)
    assert any(s.startswith("census/") for s in plan.skipped)
    assert "queue.txt" in plan.skipped and "do.sh" in plan.skipped
    report = plan.report()
    assert "8 items over 4 gripes" in report
    assert "tables-s2: 2 gripes" in report
    assert "not imported:" in report


def test_apply_writes_through_the_handler_and_is_idempotent(
    handler: GripeHandler, tmp_path: Path
) -> None:
    ids = [int(insert_gripe(handler.store, f"probe {i}").id) for i in range(4)]
    a, b, c, d = ids
    # d is already closed: reported, left alone
    handler.put(id=d, text="resolved: wontfix — gone", tags=["STATUS:wontfix"])
    plan = gripe_fix.plan_import(_state_dir(tmp_path, ids))
    out = gripe_fix.apply_plan(handler, plan)
    assert "closed 2" in out  # d appears in two items
    assert "written" in out and "failed" not in out

    def st(gid: int) -> gf.FixState:
        ref = handler.store.get_ref(kind="gripe", id=gid)
        assert ref is not None
        return gf.fix_state(handler.store, ref)

    sa = st(a)
    assert sa.batch == "tables-s2"
    assert [e.kind for e in sa.events] == [
        gf.BRIEF_KIND,
        gf.BRIEF_KIND,
        gf.HANDOFF_KIND,
        gf.REVIEW_KIND,
    ]
    assert sa.verdict == "land"
    assert sa.queue == "landing"
    assert st(b).rounds == 2
    sc = st(c)
    assert sc.batch == "ops-a" and sc.landed == "ab12cd34e"
    assert sc.queue == "awaiting-deploy"
    assert not st(d).in_loop

    again = gripe_fix.apply_plan(handler, plan)
    assert "written" not in again
    assert "already" in again
    assert st(a).rounds == 2  # no duplicate chunks


def test_assign_and_brief(handler: GripeHandler) -> None:
    ids = [int(insert_gripe(handler.store, f"probe {i}").id) for i in range(2)]
    handler.edit(id=list(ids), mode="fix", batch="wave", brief="the brief")
    out = gripe_fix.assign_batch(
        handler, "wave", host="h1", session="devin:wave", tree="wave", dry_run=False
    )
    assert "edited 2 gripe" in out
    assert gripe_fix.batch_members(handler, "wave") == sorted(ids)
    assert "no live gripe carries batch:none" in gripe_fix.assign_batch(
        handler, "none", host="h", session="s", tree=None, dry_run=True
    )
    body = handler.get(id="/batch", batch="wave").body
    assert "the brief" in body and "devin:wave@h1" in body
