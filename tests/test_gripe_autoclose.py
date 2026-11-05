"""gr519676: the gripe auto-close prod check (workers/gripe_autoclose.py).

A live gripe carrying ``meta.fix.landed`` + ``meta.fix.check`` is
evaluated once its sha is an ancestor of the recorded prod head (or, on
a clone-less worker, in the record's ``head``/``landed`` set) and the
head has stood 30 min: pass → ``resolved:`` + ``STATUS:done``; a first
fail records ``meta.fix.check_failed_at`` and a repeat failure an hour
later closes + refiles a linked ``derived-from`` triage gripe. No check
line or ``hold:no-autoclose`` → untouched; the pass is dark until
``service_config`` lights it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis import settings as psettings
from precis.errors import BadInput
from precis.plans import gates
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers import gripe_autoclose as gac
from precis.workers.gripe_autoclose import (
    HOLD_TAG,
    SERVICE_NAME,
    CheckError,
    CountCheck,
    ReadCheck,
    parse_check,
    run_gripe_autoclose_pass,
)
from precis.workers.scheduler import CADENCES
from precis.workers.service_config import set_service_prio
from tests._refs import insert_gripe

HEAD = "a" * 40
SHA = "b1b2b3b4b5b6b7b8b9b0b1b2b3b4b5b6b7b8b9b0"
T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
OLD_FAIL = "2026-10-31T00:00:00Z"  # a check_failed_at older than 1 h


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


def _record(
    store: Store,
    *,
    landed: list[str] | None = None,
    head: str = HEAD,
    deployed_at: str = "2026-10-01T00:00:00Z",
) -> None:
    rec = {
        "head": head,
        "deployed_at": deployed_at,
        "landed": [SHA[:12]] if landed is None else landed,
        "release": "r42",
    }
    psettings.set_setting(gates.PROD_HEAD_KEY, json.dumps(rec), store=store)
    psettings.invalidate(gates.PROD_HEAD_KEY)


def _enable(store: Store) -> None:
    set_service_prio(store, "*", SERVICE_NAME, 1, actor="test")


def _probe(store: Store) -> int:
    """The read-check target: a todo whose body carries ``needle``."""
    ref = store.insert_ref(kind="todo", slug=None, title="the probe", meta={})
    store.chunks.insert_chunks(
        int(ref.id), [ChunkInsert(ord=0, text="the needle body")]
    )
    return int(ref.id)


def _gripe(
    store: Store,
    *,
    check: str | None = 'read todo {probe} contains "needle"',
    probe: int | None = None,
    held: bool = False,
    landed: str | None = SHA,
    failed_at: str | None = None,
) -> int:
    fix: dict[str, Any] = {}
    if landed:
        fix["landed"] = landed
    if failed_at:
        fix["check_failed_at"] = failed_at
    ref = insert_gripe(
        store, "Area: the thing is broken", status="in_review", meta={"fix": fix}
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="the needle body")])
    gid = int(ref.id)
    if check:
        # update_ref's meta_patch is a shallow jsonb merge — rewrite the
        # whole ``fix`` key or ``landed`` would drop out.
        if probe is None and "{probe}" in check:
            probe = _probe(store)
        line = check.format(gid=gid, probe=probe)
        store.update_ref(gid, meta_patch={"fix": {**fix, "check": line}})
    if held:
        store.add_tag(gid, Tag.open(HOLD_TAG), set_by="agent")
    return gid


def _code_todo(
    store: Store,
    gid: int,
    *,
    check: str | None = 'read todo {probe} contains "needle"',
    probe: int | None = None,
    landed: str | list[str] | None = SHA[:9],
    status: str = "done",
) -> int:
    """The gripe's ``fixes``-linked code todo (td570413): ``landed:``
    comment(s) (land-notify's land record), STATUS and ``meta.fix.check``.

    The gripe itself carries no ``meta.fix`` — the todo path is where a
    new fix's check line lives.
    """
    from precis.handlers._comment_timeline import append_comment

    ref = store.insert_ref(kind="todo", slug=None, title="the code item", meta={})
    tid = int(ref.id)
    store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="fixes")
    store.add_tag(tid, Tag.closed("STATUS", status), set_by="agent")
    if landed:
        # The real write path (``put(id=T, text=…)`` → ``append_comment``)
        # so the land record is a genuine ``todo_comment`` chunk, ords
        # included — the candidate SQL filters on exactly that kind.
        # A list appends in order: the last one is the NEWEST landed:
        # comment, the one the pass reads.
        for text in [landed] if isinstance(landed, str) else landed:
            append_comment(
                store,
                ref,
                kind="todo",
                comment_kind="todo_comment",
                text=f"landed: {text} — feat(x): y (gr{gid}, td{tid})",
            )
    if check:
        if probe is None and "{probe}" in check:
            probe = _probe(store)
        line = check.format(gid=gid, probe=probe, tid=tid)
        store.update_ref(tid, meta_patch={"fix": {"check": line}})
    return tid


def _bare_gripe(store: Store) -> int:
    """A live gripe with no ``meta.fix`` — the todo-path candidate shape."""
    return int(insert_gripe(store, "Area: the thing is broken", status="in_review").id)


def _status(store: Store, gid: int) -> str | None:
    vals = [str(t) for t in store.tags_for(gid) if str(t).startswith("STATUS:")]
    return vals[0].split(":", 1)[1] if vals else None


def _comments(store: Store, gid: int) -> list[str]:
    return [
        c.text
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


def _gripes(store: Store) -> list[int]:
    return [
        int(r.id) for r in store.list_refs(kind="gripe", limit=50) if r.id is not None
    ]


def _fix_meta(store: Store, gid: int) -> dict[str, Any]:
    ref = store.get_ref(kind="gripe", id=gid)
    return dict((ref.meta or {}).get("fix") or {}) if ref is not None else {}


# ── the check grammar ────────────────────────────────────────────────


def test_parse_check_forms() -> None:
    c = parse_check('read gripe 12 contains "two words"')
    assert isinstance(c, ReadCheck)
    assert c.ref_id == 12 and c.needle == "two words" and c.cmp is None
    c = parse_check("read todo 3 count done >= 2")
    assert isinstance(c, ReadCheck)
    assert c.cmp == ">=" and c.n == 2 and c.needle == "done"
    c = parse_check(
        'count job with tag=STATUS:succeeded and meta.job_type="gripe_validate" >= 1'
    )
    assert isinstance(c, CountCheck)
    assert c.kind == "job"
    assert c.tags == ("STATUS:succeeded",)
    assert c.meta_eq == (("job_type", "gripe_validate"),)
    assert (c.cmp, c.n) == (">=", 1)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "delete refs where kind='gripe'",
        "read gripe x contains foo",
        "read gripe 1",
        "count job",
        "count job with tag= >= 1",
        "count job with tag=STATUS:done meta.a=b >= 1",
        "count job with meta.Bad Path=x >= 1",
        "count job =~ 1",
    ],
)
def test_parse_check_refuses(line: str) -> None:
    with pytest.raises((CheckError, BadInput)):
        parse_check(line)


@pytest.mark.parametrize(
    "line",
    [
        # unknown kind (a typo'd kind counts zero refs — a verdict the
        # fixer never meant); the gate needs the live kinds set.
        "count gripp >= 1",
        "read gripp 3 contains x",
        # always-true — can never fail.
        "count gripe >= 0",
        "count gripe > -1",
        "count gripe != -2",
        'read todo 3 count "x" >= 0',
        # always-false — can never pass.
        "count gripe = -1",
        "count gripe < 0",
        "count gripe <= -2",
        # an empty result satisfies these — vacuous without a predicate.
        "count gripe = 0",
        "count gripe < 3",
        "count gripe <= 5",
        "count gripe != 2",
    ],
)
def test_parse_check_rejects_vacuous_kinds_and_cmps(line: str) -> None:
    with pytest.raises(CheckError):
        parse_check(line, kinds={"gripe", "todo", "job"})


@pytest.mark.parametrize(
    "line",
    [
        "count gripp >= 1",  # kinds=None → grammar-only parse (the write path)
        "count gripe >= 1",
        "count gripe != 0",
        "count gripe with tag=STATUS:open = 0",
        "count gripe with meta.batch=s2 < 3",
        'read gripe 9 count "x" = 0',  # anchored by ref + needle
    ],
)
def test_parse_check_allows_anchored(line: str) -> None:
    parse_check(line)


def test_parse_check_kind_gate() -> None:
    with pytest.raises(CheckError):
        parse_check("count nosuch >= 1", kinds={"gripe", "todo"})
    parse_check("count nosuch >= 1")  # kinds=None stays grammar-only


# ── the pass ─────────────────────────────────────────────────────────


def test_dark_until_service_config_lights(store: Store) -> None:
    _record(store)
    gid = _gripe(store, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r == {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}
    assert _status(store, gid) == "in_review"
    _enable(store)
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"


def test_pass_closes_with_resolved_comment(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 1 and r["ok"] == 1 and r["failed"] == 0
    assert _status(store, gid) == "done"
    newest = _comments(store, gid)[-1]
    assert newest.startswith(f"resolved: {SHA} (r42)")
    assert "prod check passed" in newest


def test_fail_marks_then_confirmed_close_refiles(store: Store) -> None:
    """First failure writes ``meta.fix.check_failed_at`` only; a repeat
    failure >= 1 h later closes + refiles a linked triage gripe."""
    _enable(store)
    _record(store)
    probe = _probe(store)
    gid = _gripe(store, check='read todo {probe} contains "not-present"', probe=probe)
    r = run_gripe_autoclose_pass(store, now=T0)
    assert r["failed"] == 0 and r["skipped"] == 1
    assert _status(store, gid) == "in_review"
    assert _fix_meta(store, gid)["check_failed_at"] == T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _comments(store, gid) == []
    # A second failure inside the hour still waits.
    r = run_gripe_autoclose_pass(store, now=T0 + timedelta(minutes=30))
    assert r["failed"] == 0 and r["skipped"] == 1
    assert _status(store, gid) == "in_review"
    # An hour later the failure is confirmed: close + refile.
    r = run_gripe_autoclose_pass(store, now=T0 + timedelta(hours=1, seconds=1))
    assert r["failed"] == 1 and r["ok"] == 0
    assert _status(store, gid) == "done"
    others = [g for g in _gripes(store) if g != gid]
    assert len(others) == 1
    new = others[0]
    assert _status(store, new) == "triaged"
    # the title is a derived cut; the ruling's "first line" is the body's
    first = store.chunks.list_chunks_for_ref(new)[0].text.split("\n", 1)[0]
    assert SHA in first and f"gr{gid}" in first and "failed its prod check" in first
    with store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (new, gid),
        ).fetchone()
    assert edge is not None and edge[0] == "derived-from"
    comments = _comments(store, gid)
    assert "prod check FAILED" in comments[-2]
    assert comments[-1] == f"resolved: refiled as gr{new}"


def test_pass_between_failures_clears_marker(store: Store) -> None:
    """A pass clears ``check_failed_at``: the next failure starts the
    clock over instead of closing."""
    _enable(store)
    _record(store)
    gid = _gripe(
        store, check='read todo {probe} contains "absent"', probe=_probe(store)
    )
    r = run_gripe_autoclose_pass(store, now=T0)
    assert r["skipped"] == 1
    assert "check_failed_at" in _fix_meta(store, gid)
    # The check now passes — the marker clears and the gripe closes ok.
    store.update_ref(
        gid,
        meta_patch={
            "fix": {
                **_fix_meta(store, gid),
                "check": f'read todo {_probe(store)} contains "needle"',
            }
        },
    )
    r = run_gripe_autoclose_pass(store, now=T0 + timedelta(minutes=10))
    assert r["ok"] == 1
    assert _status(store, gid) == "done"
    assert "check_failed_at" not in _fix_meta(store, gid)


def test_no_check_line_is_untouched(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, check=None)
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"
    assert _comments(store, gid) == []


def test_hold_no_autoclose_is_untouched(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, held=True)
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_expired_hold_and_status_do_not_exclude(store: Store) -> None:
    """An exclusion tag honours ``ref_tags.expires_at`` the way
    ``build_tag_filter`` does: a lapsed ``hold:no-autoclose`` (and a
    lapsed terminal STATUS row) no longer holds the gripe out."""
    _enable(store)
    _record(store)
    past = datetime.now(UTC) - timedelta(hours=1)
    gid = _gripe(store, probe=_probe(store))
    store.add_tag(gid, Tag.open(HOLD_TAG), set_by="agent", expires_at=past)
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"
    # Same rule on the STATUS leg: a gripe whose only terminal STATUS
    # row has lapsed still reads as a candidate (the store sees the tag,
    # the filter sees the expiry).
    gid2 = _gripe(store, probe=_probe(store))
    store.add_tag(
        gid2,
        Tag.closed("STATUS", "done"),
        set_by="agent",
        replace_prefix=True,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 hour' "
            "FROM tags t WHERE ref_tags.tag_id = t.tag_id "
            "AND ref_tags.ref_id = %s AND t.namespace = 'STATUS'",
            (gid2,),
        )
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    newest = _comments(store, gid2)[-1]
    assert "prod check passed" in newest


def test_not_yet_deployed_waits(store: Store) -> None:
    _enable(store)
    _record(store, landed=["d" * 12])  # SHA not in the prod set
    gid = _gripe(store, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["pending"] == 1 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_fresh_head_waits_out_the_settle_window(store: Store) -> None:
    """A prod head younger than ``PRECIS_AUTOCLOSE_MIN_DEPLOY_AGE_S``
    defers every candidate — checks never run mid-rollout."""
    _enable(store)
    _record(store, deployed_at=T0.strftime("%Y-%m-%dT%H:%M:%SZ"))
    gid = _gripe(store, probe=_probe(store))
    r = run_gripe_autoclose_pass(store, now=T0 + timedelta(minutes=10))
    assert r["pending"] == 1 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"
    # Past the window the same gripe evaluates normally.
    r = run_gripe_autoclose_pass(store, now=T0 + timedelta(minutes=31))
    assert r["ok"] == 1


def test_unclean_landed_sha_is_not_evaluable(store: Store) -> None:
    """``meta.fix.landed`` goes through ``_clean_sha``: a hand-edited
    non-hex value makes the line not evaluable — dropped at candidate
    selection (never claimed), so gripe-validate stays free to close the
    gripe on the sha (td570413 r2's ruling)."""
    _enable(store)
    _record(store)
    gid = _gripe(store, landed="not-a-sha; drop table refs")
    r = run_gripe_autoclose_pass(store)
    assert r["skipped"] == 0 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repo_with_rollback(path: Path) -> tuple[str, str]:
    """``(base_sha, fix_sha)``: HEAD sits at ``base`` while ``fix``'s
    object still exists — the record's ``landed`` can list it while it
    is NOT an ancestor of head (the rolled-back fix)."""
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "f").write_text("a", encoding="utf-8")
    _git(path, "add", "f")
    _git(path, "commit", "-qm", "base")
    base = _git(path, "rev-parse", "HEAD")
    (path / "f").write_text("b", encoding="utf-8")
    _git(path, "commit", "-qam", "the fix")
    fix = _git(path, "rev-parse", "HEAD")
    _git(path, "reset", "-q", "--hard", base)  # rolled back; fix object stays
    return base, fix


def test_deployed_means_ancestor_of_head_not_landed_membership(
    store: Store, tmp_path: Path
) -> None:
    """A rolled-back sha still in the record's ``landed`` reads NOT
    deployed; a sha that aged out of ``landed`` but IS an ancestor of
    head reads deployed. Git is truth, not list membership."""
    _enable(store)
    base, fix = _repo_with_rollback(tmp_path / "repo")
    # Rolled back: head=base, landed still names the fix sha.
    _record(store, head=base, landed=[fix[:12]])
    gid = _gripe(store, landed=fix, probe=_probe(store))
    r = run_gripe_autoclose_pass(store, repo=tmp_path / "repo")
    assert r["pending"] == 1 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"
    # Aged out: head=fix, landed holds something else entirely — the
    # base sha is an ancestor of head, so it counts as deployed. gid's
    # sha now IS the head (a commit is its own ancestor) — it deploys
    # too, so both close.
    _record(store, head=fix, landed=["d" * 12])
    gid2 = _gripe(store, landed=base, probe=_probe(store))
    r = run_gripe_autoclose_pass(store, repo=tmp_path / "repo")
    assert r["ok"] == 2
    assert _status(store, gid) == "done"
    assert _status(store, gid2) == "done"


def test_record_membership_is_the_clone_less_fallback(
    store: Store, tmp_path: Path
) -> None:
    """No checkout that knows the shas → the record's ``landed`` list is
    the verdict, same as ``sha-in-prod``."""
    _enable(store)
    _record(store)  # landed=[SHA[:12]], head=HEAD — neither exists anywhere
    gid = _gripe(store, probe=_probe(store))
    # A path that is not a git repo: ``_ancestor_of`` declines (None) and
    # the record's membership decides.
    r = run_gripe_autoclose_pass(store, repo=tmp_path / "no-repo-here")
    assert r["ok"] == 1


def test_read_check_refuses_own_id(store: Store) -> None:
    """``read gripe <self>`` is tautological — the gripe's own text
    always carries whatever the fixer wrote. The refusal is at
    evaluability: not a candidate, so validate can still close it."""
    _enable(store)
    _record(store)
    gid = _gripe(store, check='read gripe {gid} contains "needle"')
    r = run_gripe_autoclose_pass(store)
    assert r["skipped"] == 0 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"
    assert _comments(store, gid) == []


def test_refused_close_files_no_refile(store: Store, monkeypatch: Any) -> None:
    """Close-first on a confirmed failure: a refused close files nothing,
    so a refusing gripe never piles up hourly duplicates."""
    from precis.cli import gripe as gripe_cli

    _enable(store)
    _record(store)
    gid = _gripe(
        store,
        check='read todo {probe} contains "absent"',
        probe=_probe(store),
        failed_at=OLD_FAIL,
    )
    monkeypatch.setattr(gripe_cli, "resolve_gripe", lambda *a, **k: False)
    r1 = run_gripe_autoclose_pass(store)
    r2 = run_gripe_autoclose_pass(store)
    assert r1["skipped"] == 1 and r2["skipped"] == 1
    assert _gripes(store) == [gid]
    assert _status(store, gid) == "in_review"


def test_confirmed_failure_reuses_existing_derived_gripe(store: Store) -> None:
    """A live gripe already linked ``derived-from`` is the refile target —
    the close comment names it and no new filing happens."""
    _enable(store)
    _record(store)
    gid = _gripe(
        store,
        check='read todo {probe} contains "absent"',
        probe=_probe(store),
        failed_at=OLD_FAIL,
    )
    prior = insert_gripe(store, "earlier failure", status="triaged", meta={})
    store.add_link(src_ref_id=int(prior.id), dst_ref_id=gid, relation="derived-from")
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    assert _status(store, gid) == "done"
    assert sorted(_gripes(store)) == sorted([gid, int(prior.id)])
    newest = _comments(store, gid)[-1]
    assert "prod check FAILED" in newest and f"gr{prior.id}" in newest


def test_check_timeout_is_skipped_not_failed(store: Store, monkeypatch: Any) -> None:
    """A ``statement_timeout`` cancel surfaces as ``skipped`` — never a
    verdict (no close, no marker)."""
    import psycopg.errors

    _enable(store)
    _record(store)
    gid = _gripe(store, check="count gripe with tag=STATUS:in_review >= 1")

    def _boom(store: Store, check: Any) -> tuple[bool, str]:
        raise psycopg.errors.QueryCanceled(
            "canceling statement due to statement timeout"
        )

    monkeypatch.setattr(gac, "_count_check", _boom)
    r = run_gripe_autoclose_pass(store)
    assert r["skipped"] == 1 and r["claimed"] == 0 and r["failed"] == 0
    assert _status(store, gid) == "in_review"
    assert "check_failed_at" not in _fix_meta(store, gid)


def test_rerun_is_a_noop(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, probe=_probe(store))
    r1 = run_gripe_autoclose_pass(store)
    assert r1["ok"] == 1
    r2 = run_gripe_autoclose_pass(store)
    assert r2 == {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}
    assert len(_comments(store, gid)) == 1


def test_count_check_and_malformed_check(store: Store) -> None:
    """An unparseable line is not evaluable (r2) — dropped at candidate
    selection, never counted, and validate may close it on the sha."""
    _enable(store)
    _record(store)
    ok_gid = _gripe(store, check="count gripe with tag=STATUS:in_review >= 1")
    bad_gid = _gripe(store, check="frobnicate the refs")
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1 and r["skipped"] == 0
    assert _status(store, ok_gid) == "done"
    assert _status(store, bad_gid) == "in_review"


def test_chain_cap_marks_third_refile_held(store: Store) -> None:
    """The refiled gripe of a third consecutive failure on one root
    carries ``hold:no-autoclose`` — the chain hands off to a human."""
    _enable(store)
    _record(store)
    root = _gripe(
        store,
        check='read todo {probe} contains "absent-1"',
        probe=_probe(store),
        failed_at=OLD_FAIL,
    )
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    mid = next(g for g in _gripes(store) if g != root)
    # mid's own fix lands and fails too — its refile is still plain.
    store.update_ref(
        mid,
        meta_patch={
            "fix": {
                "landed": SHA,
                "check": f'read todo {_probe(store)} contains "absent-2"',
                "check_failed_at": OLD_FAIL,
            }
        },
    )
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    third = next(g for g in _gripes(store) if g not in (root, mid))
    assert HOLD_TAG not in {str(t) for t in store.tags_for(third)}
    # third's fix failing makes the chain three deep — the refiled
    # fourth gripe is held out of any further auto-close.
    store.update_ref(
        third,
        meta_patch={
            "fix": {
                "landed": SHA,
                "check": f'read todo {_probe(store)} contains "absent-3"',
                "check_failed_at": OLD_FAIL,
            }
        },
    )
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    fourth = next(g for g in _gripes(store) if g not in (root, mid, third))
    assert HOLD_TAG in {str(t) for t in store.tags_for(fourth)}


def test_cadence_registered_dark_safe(store: Store) -> None:
    cad = next(c for c in CADENCES if c.name == "gripe_autoclose")
    assert 3600 <= cad.interval_s < 2 * 3600
    assert cad.service is None  # gate lives inside the pass, not the lease
    assert not cad.spends


# ── the code-todo path (td570413) ─────────────────────────────────────
#
# A gripe whose ``fixes``-linked code todo carries ``meta.fix.check`` is
# a candidate once the todo is ``STATUS:done`` and its newest ``landed:``
# comment names the sha — the gripe itself needs no ``meta.fix``.


def test_todo_path_candidate_closes_on_pass(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    _code_todo(store, gid, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 1 and r["ok"] == 1
    assert _status(store, gid) == "done"
    newest = _comments(store, gid)[-1]
    assert newest.startswith(f"resolved: {SHA[:9]} (r42)")
    assert "prod check passed" in newest


def test_todo_path_failure_marker_lives_on_the_todo(store: Store) -> None:
    """A first failure stamps ``meta.fix.check_failed_at`` on the code
    todo (the ref the check line lives on), never on the gripe."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    tid = _code_todo(
        store, gid, check='read todo {probe} contains "absent"', probe=_probe(store)
    )
    r = run_gripe_autoclose_pass(store, now=T0)
    assert r["skipped"] == 1 and r["failed"] == 0
    assert _status(store, gid) == "in_review"
    todo_ref = store.get_ref(kind="todo", id=tid)
    assert todo_ref is not None
    todo_fix = dict((todo_ref.meta or {}).get("fix") or {})
    assert todo_fix["check_failed_at"] == T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    gripe_ref = store.get_ref(kind="gripe", id=gid)
    assert "check_failed_at" not in dict(
        (gripe_ref.meta or {}).get("fix") or {} if gripe_ref is not None else {}
    )


def test_todo_path_confirmed_failure_closes_and_refiles(store: Store) -> None:
    """The todo-path marker drives the same confirmed-failure close +
    ``derived-from`` refile as the gripe-side one."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    tid = _code_todo(
        store, gid, check='read todo {probe} contains "absent"', probe=_probe(store)
    )
    tref = store.get_ref(kind="todo", id=tid)
    assert tref is not None
    store.update_ref(
        tid,
        meta_patch={
            "fix": {
                **dict((tref.meta or {}).get("fix") or {}),
                "check_failed_at": OLD_FAIL,
            }
        },
    )
    r = run_gripe_autoclose_pass(store, now=T0)
    assert r["failed"] == 1
    assert _status(store, gid) == "done"
    others = [g for g in _gripes(store) if g != gid]
    assert len(others) == 1 and _status(store, others[0]) == "triaged"
    comments = _comments(store, gid)
    assert "prod check FAILED" in comments[-2]
    assert comments[-1] == f"resolved: refiled as gr{others[0]}"


def test_todo_path_requires_done_status(store: Store) -> None:
    """A code todo still in build is not a candidate — its ``landed:``
    record only counts once land-notify flipped it ``done``."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    _code_todo(store, gid, status="doing", probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_todo_path_requires_landed_record(store: Store) -> None:
    """Done with a check line but no ``landed:`` comment: there is no
    sha to deploy-check against, so the gripe is not a candidate."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    _code_todo(store, gid, landed=None, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_todo_path_read_of_own_todo_is_not_evaluable(store: Store) -> None:
    """``read todo <the code todo>`` is as tautological as ``read gripe
    <self>``: the todo's ``landed:`` comment is always in its own text.
    Not evaluable → not a candidate → validate's to close."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    tid = _code_todo(store, gid, check='read todo {tid} contains "landed"')
    assert tid
    r = run_gripe_autoclose_pass(store)
    assert r["skipped"] == 0 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_todo_path_newest_landed_pending_is_not_evaluable(store: Store) -> None:
    """r2: an older ``landed: <sha>`` followed by a NEWER
    ``landed: pending`` is no usable land record — only the newest
    landed: comment counts, so the line is not evaluable and the gripe
    is validate's to close on the sha."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    _code_todo(store, gid, landed=[SHA[:9], "pending"], probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0 and r["skipped"] == 0
    assert _status(store, gid) == "in_review"


def test_todo_path_unparseable_check_is_not_evaluable(store: Store) -> None:
    """r2: a line ``parse_check`` rejects (planted straight into meta,
    past the write-time guard) cannot be evaluated — dropped at
    candidate selection, left for gripe-validate."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    tid = _code_todo(store, gid, check="count gripe >= 1")
    store.update_ref(tid, meta_patch={"fix": {"check": "this is not a check"}})
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0 and r["skipped"] == 0
    assert _status(store, gid) == "in_review"


def test_todo_check_wins_over_gripe_side(store: Store) -> None:
    """Both lines present → the code todo's is the one in force (the
    gripe-side ``meta.fix.check`` is legacy state). Here the gripe-side
    line would fail; the close proves the todo's ran."""
    _enable(store)
    _record(store)
    gid = _gripe(
        store, check='read todo {probe} contains "absent"', probe=_probe(store)
    )
    _code_todo(store, gid, probe=_probe(store))
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"


def test_inevaluable_todo_yields_to_an_evaluable_one(store: Store) -> None:
    """r3: several ``fixes`` todos — the first EVALUABLE one owns the
    gripe. The older todo is still in build so its line cannot run; the
    newer done+landed todo's line is the one evaluated (its sha is the
    one the close names)."""
    _enable(store)
    _record(store)
    gid = _bare_gripe(store)
    _code_todo(
        store,
        gid,
        check='read todo {probe} contains "absent"',
        status="doing",
        landed=None,
        probe=_probe(store),
    )
    probe2 = _probe(store)
    _code_todo(store, gid, probe=probe2)
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"
    newest = _comments(store, gid)[-1]
    assert f'read todo {probe2} contains "needle"' in newest


def test_evaluable_legacy_line_beats_inevaluable_todo(store: Store) -> None:
    """r3: no evaluable todo → the evaluable legacy ``meta.fix`` line
    stands. The todo is still in build (not evaluable); the gripe's own
    line names the probe that closes it."""
    _enable(store)
    _record(store)
    probe = _probe(store)
    gid = _gripe(store, probe=probe)  # legacy landed + passing check
    _code_todo(store, gid, status="doing", landed=None)
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"
    newest = _comments(store, gid)[-1]
    assert f'read todo {probe} contains "needle"' in newest
