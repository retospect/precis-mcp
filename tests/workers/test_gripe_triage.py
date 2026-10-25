"""``gripe_triage`` — gripe system batch F slice 4's scheduled triage pass.

The suggestion rules are pinned on hand-built :class:`StaleGripe` rows and
a tmp thread directory; the DB tests scope every assertion to the gripes
they create (the shared ``precis_test`` clone carries sibling tests' rows).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers import gripe_triage
from precis.workers.gripe_triage import StaleGripe, Thread, ThreadIndex
from tests._refs import insert_gripe


def _cand(
    *,
    ref_id: int = 101,
    title: str = "x",
    body: str = "",
    prio: int | None = None,
    open_tags: frozenset[str] = frozenset(),
    linked: frozenset[str] = frozenset(),
) -> StaleGripe:
    return StaleGripe(
        ref_id=ref_id,
        title=title,
        body=body,
        prio=prio,
        open_tags=open_tags,
        linked=linked,
    )


def _index(*threads: Thread) -> ThreadIndex:
    return ThreadIndex(threads=threads, sha="abc1234")


def _thread(
    slug: str, pointers: frozenset[str] | set[str] = frozenset(), terms: str = ""
) -> Thread:
    return Thread(
        slug=slug,
        pointers=frozenset(pointers),
        terms=gripe_triage._terms(terms),
    )


# ── suggestion rules ──────────────────────────────────────────────────────


def test_prio_suggestion_is_7_for_origin_tagged_else_5() -> None:
    auto = gripe_triage.suggest(
        _cand(open_tags=frozenset({"origin:call-errors", "thread:x"})), _index()
    )
    human = gripe_triage.suggest(_cand(open_tags=frozenset({"thread:x"})), _index())
    assert (
        auto is not None
        and "suggest PRIO 7 (system-filed, tagged origin:call-errors)" in auto
    )
    assert human is not None and "suggest PRIO 5 (human-filed" in human
    assert "prio=5)" in human


def test_nothing_missing_means_no_comment() -> None:
    assert (
        gripe_triage.suggest(_cand(prio=4, open_tags=frozenset({"thread:x"})), _index())
        is None
    )


def test_set_prio_is_not_re_suggested() -> None:
    text = gripe_triage.suggest(_cand(prio=3), _index())
    assert text is not None
    assert "PRIO" not in text
    assert "no owning thread matched among 0 thread files" in text


def test_thread_naming_the_gripe_wins_over_links_and_terms() -> None:
    idx = _index(
        _thread("alpha", {"td9"}, "router pours"),
        _thread("beta", {"gr101"}),
    )
    text = gripe_triage.suggest(
        _cand(prio=5, title="router pours fail", linked=frozenset({"td9"})), idx
    )
    assert text is not None
    assert "suggest thread beta (threads/beta.md names gr101)" in text
    assert "add=['thread:beta']" in text


def test_linked_ref_named_by_a_thread_is_the_second_rule() -> None:
    idx = _index(_thread("alpha", {"td9"}), _thread("beta", {"gr5"}))
    text = gripe_triage.suggest(_cand(prio=5, linked=frozenset({"td9"})), idx)
    assert text is not None
    assert "suggest thread alpha (it links td9, which threads/alpha.md names)" in text


def test_term_overlap_needs_two_shared_terms_and_picks_the_most() -> None:
    idx = _index(
        _thread("pcb-platform", terms="pcb router copper pours"),
        _thread("chemistry", terms="router catalyst"),
    )
    one = gripe_triage.suggest(_cand(prio=5, title="router stalls"), idx)
    two = gripe_triage.suggest(
        _cand(prio=5, title="copper pours vanish", body="the router drops them"), idx
    )
    assert one is not None and "no owning thread matched" in one
    assert (
        two is not None and "suggest thread pcb-platform (shares copper, pours" in two
    )


def test_comment_names_the_thread_sha_and_never_closes() -> None:
    text = gripe_triage.suggest(_cand(), _index())
    assert text is not None
    assert text.startswith(gripe_triage.COMMENT_PREFIX)
    assert "Thread files read at abc1234; this pass never closes or retags." in text


def test_load_threads_reads_pointers_and_status_skipping_index_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("gr1 gr2\n", encoding="utf-8")
    (tmp_path / "INDEX.md").write_text("gr3\n", encoding="utf-8")
    (tmp_path / "ship-gate-ci.md").write_text(
        "---\nroster: active\n---\n# Ship gate\n\n"
        "**Status:** ends when the merge gate is trustworthy.\n\n"
        "## Do next\n\n1. **gr42** — x\n2. **td7** — y\n",
        encoding="utf-8",
    )
    idx = gripe_triage.load_threads(tmp_path)
    assert [t.slug for t in idx.threads] == ["ship-gate-ci"]
    (t,) = idx.threads
    assert t.pointers == frozenset({"gr42", "td7"})
    assert {"ship", "gate", "merge", "trustworthy"} <= t.terms
    assert idx.sha  # a short sha or "unknown", never empty


def test_missing_threads_dir_is_an_empty_index(tmp_path: Path) -> None:
    idx = gripe_triage.load_threads(tmp_path / "absent")
    assert idx.threads == ()


# ── DB: selection, gate, posting ──────────────────────────────────────────


def _gripe(store: Store, title: str, *, age_days: float, body: str = "") -> int:
    ref = insert_gripe(store, title)
    if body:
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})]
        )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (age_days * 86400, ref.id),
        )
    return int(ref.id)


def _comments(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'gripe_comment' ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _light(store: Store, prio: int = 1) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('test-host', 'gripe_triage', %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (prio,),
        )


def test_candidates_pick_old_untriaged_gripes_only(store: Store) -> None:
    old = _gripe(store, "old one", age_days=4, body="the body")
    young = _gripe(store, "young one", age_days=1)
    done = _gripe(store, "old but complete", age_days=5)
    store.set_prio(done, 5)
    store.add_tag(done, Tag.open("thread:ship-gate-ci"), set_by="agent")
    closed = _gripe(store, "old but closed", age_days=6)
    store.add_tag(
        closed, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )
    other = insert_gripe(store, "linked neighbour")
    store.add_link(src_ref_id=old, dst_ref_id=int(other.id))

    got = {c.ref_id: c for c in gripe_triage.candidates(store, datetime.now(UTC))}
    assert old in got
    assert young not in got and done not in got and closed not in got
    assert got[old].body == "the body"
    assert got[old].linked == frozenset({f"gr{other.id}"})


def test_dark_without_a_service_config_row(store: Store) -> None:
    gid = _gripe(store, "dark gate", age_days=4)
    _light(store, prio=0)
    r = gripe_triage.run_gripe_triage_pass(store, threads=_index(), limit=10_000)
    assert (r.claimed, r.ok, r.failed) == (0, 0, 0)
    assert _comments(store, gid) == []


def test_lit_pass_comments_once_per_three_days(store: Store) -> None:
    gid = _gripe(store, "lit pass", age_days=4)
    _light(store)
    idx = _index(_thread("ship-gate-ci", {f"gr{gid}"}))

    gripe_triage.run_gripe_triage_pass(store, threads=idx, limit=10_000)
    first = _comments(store, gid)
    assert len(first) == 1
    assert first[0].startswith("triage: suggest PRIO 5")
    assert (
        f"suggest thread ship-gate-ci (threads/ship-gate-ci.md names gr{gid})"
        in first[0]
    )

    gripe_triage.run_gripe_triage_pass(store, threads=idx, limit=10_000)
    assert len(_comments(store, gid)) == 1, "repeat within 3 days"

    later = datetime.now(UTC) + timedelta(days=gripe_triage.REPEAT_DAYS, hours=1)
    gripe_triage.run_gripe_triage_pass(store, now=later, threads=idx, limit=10_000)
    assert len(_comments(store, gid)) == 2


def test_pass_never_closes_or_retags(store: Store) -> None:
    gid = _gripe(store, "untouched", age_days=4)
    _light(store)
    gripe_triage.run_gripe_triage_pass(store, threads=_index(), limit=10_000)
    with store.pool.connection() as conn:
        tags = conn.execute(
            "SELECT t.namespace, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
            (gid,),
        ).fetchall()
        prio = conn.execute(
            "SELECT prio, retired_at FROM refs WHERE ref_id = %s", (gid,)
        ).fetchone()
    assert [tuple(t) for t in tags] == [("STATUS", "open")]
    assert prio is not None and tuple(prio) == (None, None)


# ── wiring ────────────────────────────────────────────────────────────────


def test_cadence_is_daily_host_agnostic_and_free() -> None:
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "gripe_triage")
    assert 24 * 3600 < cad.interval_s < 25 * 3600
    assert cad.host_affinity is None and cad.eligible is None
    assert cad.spends is False


def test_service_spec_has_no_default_profile() -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["gripe_triage"]
    assert spec.default_profiles == frozenset()
    assert spec.ref_pass is False
    assert spec.enable_env is None
