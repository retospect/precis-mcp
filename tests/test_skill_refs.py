"""Lazily minted skill anchor refs: link target, usage stamp, concern banner,
misled-footer, usage view, family axis. The markdown file stays the source of
truth; the ``refs(kind='skill')`` row is only an anchor."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.fixer.intake import _render_gripe_spec, _TimelineEntry
from precis.handlers.skill import SkillHandler, _list_skills, _skill_family_of
from precis.runtime import PrecisRuntime
from precis.store import Store

SKILL = "precis-gripe-help"


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def _skill_row(store: Store) -> tuple[Any, ...] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id, r.last_recalled_at FROM refs r "
            "JOIN ref_identifiers ri ON ri.ref_id = r.ref_id "
            "AND ri.id_kind = 'cite_key' AND ri.id_value = %s "
            "WHERE r.kind = 'skill'",
            (SKILL,),
        ).fetchone()
    return tuple(row) if row is not None else None


def _gripe(rt: PrecisRuntime, text: str, **kw: Any) -> str:
    return rt.dispatch("put", {"kind": "gripe", "text": text, **kw})


def _forget_recalls(rt: PrecisRuntime, store: Store) -> None:
    rt.__dict__.pop("_recall_seen", None)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET last_recalled_at = NULL WHERE kind = 'skill'")


def test_mint_on_link_and_unknown_skill_refused(
    runtime: PrecisRuntime, store: Store
) -> None:
    assert _skill_row(store) is None
    out = _gripe(
        runtime, "it misled me", link=f"skill:{SKILL}", rel="raises-concern-about"
    )
    assert "created gripe" in out, out
    row = _skill_row(store)
    assert row is not None
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE dst_ref_id = %s "
            "AND relation = 'raises-concern-about'",
            (row[0],),
        ).fetchone()
    assert n is not None and n[0] == 1

    bad = _gripe(
        runtime, "x", link="skill:no-such-skill-xyz", rel="raises-concern-about"
    )
    assert "no skill" in bad and "created gripe" not in bad, bad
    with store.pool.connection() as conn:
        c = conn.execute(
            "SELECT count(*) FROM ref_identifiers WHERE id_value = 'no-such-skill-xyz'"
        ).fetchone()
    assert c is not None and c[0] == 0


def test_read_mints_and_section_and_toc_reads_stamp_parent(
    runtime: PrecisRuntime, store: Store
) -> None:
    assert _skill_row(store) is None
    runtime.dispatch("get", {"kind": "skill", "id": SKILL})
    row = _skill_row(store)
    assert row is not None and isinstance(row[1], datetime)

    for ident in (f"{SKILL}~1", f"{SKILL}/toc"):
        _forget_recalls(runtime, store)
        runtime.dispatch("get", {"kind": "skill", "id": ident})
        stamped = _skill_row(store)
        assert stamped is not None and stamped[1] is not None, ident
        assert stamped[0] == row[0]  # same anchor, no second row

    # synthesised ids and search hits never mint or stamp
    _forget_recalls(runtime, store)
    runtime.dispatch("get", {"kind": "skill", "id": "toc"})
    runtime.dispatch("search", {"kind": "skill", "q": "gripe"})
    after = _skill_row(store)
    assert after is not None and after[1] is None
    with store.pool.connection() as conn:
        n = conn.execute("SELECT count(*) FROM refs WHERE kind = 'skill'").fetchone()
    assert n is not None and n[0] == 1


def test_footer_and_open_concern_banner(runtime: PrecisRuntime, store: Store) -> None:
    out = runtime.dispatch("get", {"kind": "skill", "id": SKILL, "full": True})
    assert out.rstrip().splitlines()[-1].startswith("misled by this skill? put(")
    assert f"link='skill:{SKILL}', rel='raises-concern-about')" in out
    assert not any(ln.startswith("⚠") for ln in out.splitlines())

    for i in range(4):
        _gripe(
            runtime,
            f"concern number {i}\nmore",
            link=f"skill:{SKILL}",
            rel="raises-concern-about",
        )
    out = runtime.dispatch("get", {"kind": "skill", "id": SKILL, "full": True})
    banner = next(ln for ln in out.splitlines() if ln.startswith("⚠"))
    assert "4 open concern(s)" in banner and "(+1 more)" in banner
    assert banner.count(" — concern number") == 3
    assert out.index(banner) < out.index("misled by this skill?")

    # a closed gripe stops counting
    with store.pool.connection() as conn:
        gid = conn.execute(
            "SELECT min(ref_id) FROM refs WHERE kind = 'gripe'"
        ).fetchone()
    assert gid is not None
    runtime.dispatch(
        "tag", {"kind": "gripe", "id": int(gid[0]), "add": ["STATUS:wontfix"]}
    )
    out = runtime.dispatch("get", {"kind": "skill", "id": SKILL, "full": True})
    assert "3 open concern(s)" in out


def test_usage_view_orders_coldest_first_and_toc_has_read_column(
    runtime: PrecisRuntime, store: Store
) -> None:
    runtime.dispatch("get", {"kind": "skill", "id": SKILL})
    out = runtime.dispatch("get", {"kind": "skill", "view": "usage"})
    lines = [ln for ln in out.splitlines() if SKILL in ln or "never" in ln]
    assert SKILL in lines[-1]  # the only skill ever read sorts last
    assert "never" in lines[0]
    assert str(datetime.now(UTC).year)[:2] in lines[-1]  # a date, not 'never'
    assert "family" in out and "open_concerns" in out

    toc = runtime.dispatch("get", {"kind": "skill", "id": "toc"})
    assert (
        "read"
        in toc.splitlines()[
            next(i for i, ln in enumerate(toc.splitlines()) if "synopsis" in ln)
        ]
    )
    assert "<1h" in toc


def test_skills_render_without_a_database() -> None:
    h = SkillHandler(hub=Hub())
    body = h.get(id=SKILL, full=True).body
    assert "misled by this skill?" in body and not any(
        ln.startswith("⚠") for ln in body.splitlines()
    )
    assert "usage needs the database" in h.get(view="usage").body
    assert h.get(id="toc").body  # toc without the read column still renders
    assert "read" not in next(
        ln for ln in h.get(id="toc").body.splitlines() if "synopsis" in ln
    )


def test_fixer_brief_names_skill_file_and_quotes_notes() -> None:
    entries = [
        _TimelineEntry("gripe_body", 0, "the skill says X\nbut it is Y"),
        _TimelineEntry("gripe_comment", 1, "DIAGNOSIS: stale"),
    ]
    spec = _render_gripe_spec("t", entries, [SKILL])
    assert f"src/precis/data/skills/{SKILL}.md" in spec
    assert "> the skill says X" in spec and "> but it is Y" in spec
    assert spec.index("src/precis/data/skills/") < spec.index("## comment 1")
    assert "src/precis/data/skills" not in _render_gripe_spec("t", entries)


# ── family axis ────────────────────────────────────────────────────


def test_family_default_override_and_filter() -> None:
    assert _skill_family_of("precis-gripe-help") == "work"
    assert _skill_family_of("sci-methods") == "drafting"
    assert _skill_family_of("precis-doi-resolution") == "paper"  # frontmatter override
    h = SkillHandler(hub=Hub())
    se = {s for s in _list_skills() if _skill_family_of(s) == "se"}
    assert len(se) >= 5
    body = h.search(q="schema", family="se", page_size=25).body
    all_skills = set(_list_skills())
    hits = {
        ln.split()[0]
        for ln in body.splitlines()
        if ln.split()[:1] and ln.split()[0] in all_skills
    }
    assert hits and hits <= se
    # combinable with tag
    h.search(q="schema", family="se", tag="design")
    with pytest.raises(BadInput) as ei:
        h.search(q="schema", family="nope")
    assert "se" in (ei.value.options or [])


def test_toc_groups_by_family_and_filters() -> None:
    h = SkillHandler(hub=Hub())
    grouped = h.get(id="toc", by="family").body
    assert "## se (" in grouped and "## paper (" in grouped
    only = h.get(id="toc", family="pcb").body
    assert "precis-i2c-help" in only and "precis-se-" not in only
    with pytest.raises(BadInput):
        h.get(id="toc", by="nonsense")
    usage = h.get(view="usage", family="pcb").body
    assert "usage needs the database" in usage  # no store: fail-soft message


# ── discoverability hints ──────────────────────────────────────────


def test_skill_search_family_hint_only_when_it_narrows() -> None:
    h = SkillHandler(hub=Hub())
    q = "how do I draft and cite a paper"
    wide = h.search(q=q, page_size=25).body
    assert "families in these hits:" in wide
    assert "narrow to a family" in wide and "args={'family':" in wide
    top = wide.split("families in these hits: ")[1].split(" ")[0]
    narrowed = h.search(q=q, family=top).body  # the suggested call is accepted
    assert "families in these hits:" not in narrowed  # already filtered
    empty = h.search(q="zzqqxx nonsense", page_size=5).body
    assert "families in these hits" not in empty


def test_skill_render_header_and_toc_family_line() -> None:
    h = SkillHandler(hub=Hub())
    body = h.get(id="precis-se-atomic-help", full=True).body
    assert body.startswith("family: se")
    assert "siblings: get(kind='skill', id='toc', args={'family': 'se'})" in body
    solo = h.get(id="precis-gripe-help", full=True).body
    assert solo.startswith("family: work") and "siblings:" in solo
    toc = h.get(id="toc").body
    assert any(
        ln.startswith("families: ") and "args={'family':" in ln
        for ln in toc.splitlines()
    )


def _mem(rt: PrecisRuntime, text: str, *tags: str) -> int:
    out = rt.dispatch("put", {"kind": "memory", "text": text, "tags": list(tags)})
    m = re.search(r"id=(\d+)|\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1) or m.group(2))


def test_memory_narrowing_hints(runtime: PrecisRuntime, store: Store) -> None:
    hub = _mem(runtime, "Zebra hub node", "section:index")
    inside = [_mem(runtime, f"quokka fact {i}", "section:gotchas") for i in range(4)]
    outside = _mem(runtime, "quokka stray fact")
    for m in inside:
        store.add_link(src_ref_id=m, dst_ref_id=hub, relation="part-of")

    args = {"kind": "memory", "q": "quokka", "mode": "lexical", "page_size": 10}
    out = runtime.dispatch("search", args)
    assert f"args={{'under': 'me{hub}'}}" in out, out
    assert "tags=['section:gotchas']" in out

    scoped = runtime.dispatch("search", {**args, "under": f"me{hub}"})
    assert "quokka stray" not in scoped and "quokka fact 0" in scoped
    assert "narrow to" not in scoped  # all hits already in one hub / type

    single = runtime.dispatch(
        "search", {"kind": "memory", "q": "stray", "mode": "lexical"}
    )
    assert "narrow to" not in single


def test_families_are_a_closed_set_with_real_groups() -> None:
    from collections import Counter

    from precis.handlers._skill_common import SKILL_FAMILIES

    counts = Counter(_skill_family_of(s) for s in _list_skills())
    assert set(counts) <= set(SKILL_FAMILIES), set(counts) - set(SKILL_FAMILIES)
    small = {f: n for f, n in counts.items() if n < 3}
    assert not small and set(SKILL_FAMILIES) <= set(counts), (small, counts)
    assert 10 <= len(SKILL_FAMILIES) <= 15
