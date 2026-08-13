"""``draft_refresh_scan`` — the scanner cadence that mints ``draft_refresh``
jobs (docs/backlog/draft-refresh.md, Part 2). Fixture/factory shape mirrors
``tests/workers/test_draft_refresh_job.py`` (draft + section builders).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.store.store import Store
from precis.workers.draft_refresh_scan import _stalest_section, run_draft_refresh_scan

# ── draft/section builders (mirrors test_draft_refresh_job.py) ─────────────


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _seed_draft(draft: DraftHandler, hub: Hub, *, slug: str) -> dict[str, Any]:
    """A bare draft: just the title chunk."""
    proj = _proj(hub)
    draft.put(id=slug, title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_dc = hub.live_store.drafts.reading_order(ref.id)[0].dc
    return {"ref_id": ref.id, "title": title_dc}


def _add_section(
    draft: DraftHandler,
    *,
    slug: str,
    after: str,
    heading: str,
    paras: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Add a heading after ``after`` (a ``dc<id>`` anchor at the same level
    it should follow), with ``paras`` as its direct paragraph children."""
    r = draft.put(id=slug, chunk_kind="heading", text=heading, at={"after": after})
    sec_dc = _dc(r.body)
    para_dcs = []
    for text in paras:
        r = draft.put(id=slug, chunk_kind="paragraph", text=text, at={"into": sec_dc})
        para_dcs.append(_dc(r.body))
    return {"sec": sec_dc, "paras": para_dcs}


# ── raw-SQL fixture helpers ─────────────────────────────────────────────────


def _set_created_at(store: Store, dc: str, when: datetime) -> None:
    chunk_id = int(dc[2:])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = %s WHERE chunk_id = %s", (when, chunk_id)
        )
        conn.commit()


def _enable_refresh(
    store: Store, ref_id: int, *, staleness_days: int | None = None
) -> None:
    payload: dict[str, Any] = {"enabled": True}
    if staleness_days is not None:
        payload["staleness_days"] = staleness_days
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb({"draft_refresh": payload}), ref_id),
        )
        conn.commit()


def _draft_refresh_jobs(store: Store, *, slug: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "ORDER BY ref_id"
        ).fetchall()
    out = []
    for (meta,) in rows:
        meta = dict(meta or {})
        if (
            meta.get("job_type") == "draft_refresh"
            and (meta.get("params") or {}).get("draft") == slug
        ):
            out.append(meta)
    return out


#: Noon UTC, not "now" — the idem re-arm test compares a
#: ``strftime("%Y-%m-%d")`` computed here against one round-tripped
#: through the DB (a possibly non-UTC session timezone), so anchoring at
#: noon keeps any reasonable tz offset from shifting the calendar date.
_NOW = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


# ── (a) staleness = live direct paragraphs only ────────────────────────────


def test_stalest_section_ignores_preserved_table_staleness(
    store: Store, hub: Hub
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("P.",)
    )
    r = draft.put(
        id="nt",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        caption="Old table.",
        at={"into": sec["sec"]},
    )
    table_dc = _dc(r.body)

    # The table is ancient; the paragraph is recent.
    _set_created_at(store, table_dc, _NOW - timedelta(days=400))
    _set_created_at(store, sec["paras"][0], _NOW - timedelta(days=1))

    section = _stalest_section(store, seeded["ref_id"])
    assert section is not None
    assert section.heading_dc == sec["sec"]
    # The section's clock tracks the paragraph, not the table.
    assert section.min_created_at > _NOW - timedelta(days=2)


def test_scan_no_mint_when_only_preserved_content_is_old(
    store: Store, hub: Hub
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("P.",)
    )
    r = draft.put(
        id="nt",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        caption="Old table.",
        at={"into": sec["sec"]},
    )
    table_dc = _dc(r.body)
    _set_created_at(store, table_dc, _NOW - timedelta(days=400))
    _set_created_at(store, sec["paras"][0], _NOW - timedelta(days=1))
    _enable_refresh(store, seeded["ref_id"], staleness_days=180)

    result = run_draft_refresh_scan(store, 10)

    assert result.claimed == 0
    assert _draft_refresh_jobs(store, slug="nt") == []


# ── (c) opt-in gating ────────────────────────────────────────────────────


def test_scan_skips_draft_without_opt_in_meta(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("P.",)
    )
    _set_created_at(store, sec["paras"][0], _NOW - timedelta(days=365))
    # deliberately NOT calling _enable_refresh

    result = run_draft_refresh_scan(store, 10)

    assert result.claimed == 0
    assert _draft_refresh_jobs(store, slug="nt") == []


# ── (e) zero-paragraph sections (incl. title) never selected ───────────────


def test_stalest_section_none_when_no_live_paragraphs(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")

    assert _stalest_section(store, seeded["ref_id"]) is None


def test_scan_no_mint_for_title_only_draft(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    _enable_refresh(store, seeded["ref_id"], staleness_days=1)

    result = run_draft_refresh_scan(store, 10)

    assert result.claimed == 0
    assert _draft_refresh_jobs(store, slug="nt") == []


# ── (b) selection: stalest wins; tie-break deeper depth, then reading order ─


def test_stalest_section_picks_oldest_of_two_siblings(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec_a = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("Pa.",)
    )
    sec_b = _add_section(
        draft, slug="nt", after=sec_a["sec"], heading="Section B", paras=("Pb.",)
    )
    _set_created_at(store, sec_a["paras"][0], _NOW - timedelta(days=30))
    _set_created_at(store, sec_b["paras"][0], _NOW - timedelta(days=10))

    section = _stalest_section(store, seeded["ref_id"])

    assert section is not None
    assert section.heading_dc == sec_a["sec"]  # the older one


def test_stalest_section_tie_break_prefers_deeper_heading(
    store: Store, hub: Hub
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec_a = _add_section(
        draft,
        slug="nt",
        after=seeded["title"],
        heading="Section A",
        paras=("Outer para.",),
    )
    sub_b = _add_section(
        draft, slug="nt", after=sec_a["paras"][0], heading="Sub B", paras=("Inner.",)
    )
    same_time = _NOW - timedelta(days=30)
    _set_created_at(store, sec_a["paras"][0], same_time)
    _set_created_at(store, sub_b["paras"][0], same_time)

    section = _stalest_section(store, seeded["ref_id"])

    assert section is not None
    assert section.heading_dc == sub_b["sec"]  # deeper wins the tie


def test_stalest_section_tie_break_prefers_earlier_reading_order(
    store: Store, hub: Hub
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec_a = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("Pa.",)
    )
    sec_b = _add_section(
        draft, slug="nt", after=sec_a["sec"], heading="Section B", paras=("Pb.",)
    )
    same_time = _NOW - timedelta(days=30)
    _set_created_at(store, sec_a["paras"][0], same_time)
    _set_created_at(store, sec_b["paras"][0], same_time)

    section = _stalest_section(store, seeded["ref_id"])

    assert section is not None
    assert section.heading_dc == sec_a["sec"]  # earlier in reading order wins


# ── (f) one job per draft per fire, even with two stale sections ───────────


def test_scan_mints_exactly_one_job_per_draft(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec_a = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("Pa.",)
    )
    sec_b = _add_section(
        draft, slug="nt", after=sec_a["sec"], heading="Section B", paras=("Pb.",)
    )
    _set_created_at(store, sec_a["paras"][0], _NOW - timedelta(days=30))
    _set_created_at(store, sec_b["paras"][0], _NOW - timedelta(days=20))
    _enable_refresh(store, seeded["ref_id"], staleness_days=1)

    result = run_draft_refresh_scan(store, 10)

    assert result.claimed == 1
    jobs = _draft_refresh_jobs(store, slug="nt")
    assert len(jobs) == 1
    assert jobs[0]["params"]["scope"] == sec_a["sec"]  # the older section


# ── (d) idem re-arm after a rewrite; unchanged chunks dedup ────────────────


def test_scan_idem_key_rearms_after_rewrite_but_dedups_when_unchanged(
    store: Store, hub: Hub
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_draft(draft, hub, slug="nt")
    sec = _add_section(
        draft, slug="nt", after=seeded["title"], heading="Section A", paras=("Old.",)
    )
    old_time = _NOW - timedelta(days=10)
    _set_created_at(store, sec["paras"][0], old_time)
    _enable_refresh(store, seeded["ref_id"], staleness_days=1)

    first = run_draft_refresh_scan(store, 10)
    assert first.claimed == 1
    jobs = _draft_refresh_jobs(store, slug="nt")
    assert len(jobs) == 1
    idem_key_1 = jobs[0]["idem_key"]
    assert old_time.strftime("%Y-%m-%d") in idem_key_1

    # Re-scanning with nothing changed dedups silently.
    second = run_draft_refresh_scan(store, 10)
    assert second.claimed == 0
    assert len(_draft_refresh_jobs(store, slug="nt")) == 1

    # Simulate the job's own apply: retire the old paragraph, insert a
    # fresh one (a DIFFERENT backdated timestamp, still past threshold,
    # so the re-arm is directly observable this tick rather than waiting
    # for a future scan). ``retire_chunk`` only accepts the legacy
    # ``chunks.handle`` (base58) form, not the universal ``dc<id>``
    # address — same as the job's own dispatch (``draft_refresh.py``
    # retires via ``c.handle``, not ``c.dc``).
    old_para = store.drafts.get_draft_chunk(sec["paras"][0])
    assert old_para is not None
    store.drafts.retire_chunk(old_para.handle, mode="cascade")
    new_chunks = store.drafts.add_chunks(
        ref_id=seeded["ref_id"],
        chunk_kind="paragraph",
        text="New.",
        at={"into": sec["sec"]},
    )
    new_time = _NOW - timedelta(days=5)
    _set_created_at(store, new_chunks[0].dc, new_time)

    third = run_draft_refresh_scan(store, 10)
    assert third.claimed == 1
    jobs = _draft_refresh_jobs(store, slug="nt")
    assert len(jobs) == 2
    idem_key_2 = next(j["idem_key"] for j in jobs if j["idem_key"] != idem_key_1)
    assert idem_key_2 != idem_key_1
    assert new_time.strftime("%Y-%m-%d") in idem_key_2
