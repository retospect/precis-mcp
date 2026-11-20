"""Grounding on the read surfaces (gr634490 slices 2 and 3).

The ``get`` footer and the fisheye header carry the one-line coverage
summary; ``view='grounding'`` is the drill-down: per-claim status and
evidence, the kept audit history, and the consumers a stale audit reaches.
"""

from __future__ import annotations

import json
import re
from typing import Any

from precis.graph.garden import grounding as g
from precis.runtime import PrecisRuntime
from precis.store import Store

S1 = "The ingest worker stores every body chunk before the embedder runs."
S2 = "A chunk is the smallest addressable text unit."
BODY = f"{S1} {S2}"


def _reply(*entries: dict[str, Any]) -> g.Call:
    def call(prompt: str) -> tuple[str, str | None]:
        return json.dumps({"claims": list(entries)}), None

    return call


def _claim(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "i": i,
        "quote": (S1, S2)[i],
        "kind": "empirical",
        "status": "supported",
        "evidence": [],
    }
    return {**base, **kw}


def _put(runtime: PrecisRuntime, store: Store, text: str = BODY) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1))


def _audit(
    store: Store,
    mid: int,
    *entries: dict[str, Any],
    body: str = BODY,
    rollups: tuple[tuple[int, str], ...] = (),
) -> None:
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    sha = store.target_sha("ref", mid)
    assert sha is not None
    rec = g.audit_body(body, ref.title or "", sha, call=_reply(*entries))
    for idx, verdict in rollups:
        rec.claims[idx].rollup = verdict
    assert g.write_audit(store, mid, rec) is not None


def _grounding_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("grounding:")]
    assert len(lines) == 1, text
    return lines[0]


def test_get_memory_shows_grounding_line(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    unaudited = _put(runtime_with_store, store)
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": unaudited})
    assert "grounding: not audited" in out
    # Rollup-stamped claims — the pipeline shape of passage-verified
    # empiricals: classifier-only suggestions would read unresolved.
    _audit(
        store,
        unaudited,
        _claim(0),
        _claim(1),
        rollups=((0, "supported"), (1, "supported")),
    )
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": unaudited})
    line = _grounding_line(out)
    assert line.startswith("grounding: 2+ claims - 2 supported")
    assert "- audited 20" in line  # 2026-… audit date
    # Classifier-only coverage is a lower bound, so the line says so.
    assert line.endswith("- current - truncated scan")
    # The trust line stays last, the grounding line before it.
    assert out.rstrip().endswith(")")  # the trust edit recipe closes last
    assert out.rstrip().find(line) < out.rstrip().rfind("trust:")


def test_unresolved_hypothesis_not_rendered_as_supported(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put(runtime_with_store, store)
    _audit(
        store,
        mid,
        _claim(0, kind="hypothesis", status="unresolved", reason="no evidence yet"),
        _claim(1, kind="hypothesis", status="unresolved", reason="no evidence yet"),
    )
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": mid})
    line = _grounding_line(out)
    assert "2+ claims" in line  # the classifier's count is a lower bound
    assert "2 unresolved" in line
    assert "supported" not in line
    # Displaying the hypothesis upgrades nothing: the count is the audit's
    # own, not a certificate.


def test_fisheye_header_carries_the_same_line(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put(runtime_with_store, store)
    _audit(store, mid, _claim(0, kind="definition"), _claim(1, kind="definition"))
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "extent": "fisheye"}
    )
    assert _grounding_line(out) == (g.summary_line(g.grounding_summary(store, mid)))


def test_view_grounding_lists_claims_and_history(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put(runtime_with_store, store)
    _audit(store, mid, _claim(0, kind="definition"), _claim(1, kind="definition"))
    first = g.grounding_summary(store, mid)
    assert first.freshness == "current"
    # A quest depends on the memory; then the memory is edited, staling the
    # audit and queueing the consumer.
    quest = store.insert_ref(kind="quest", slug=None, title="Q depends on it")
    store.add_link(
        src_ref_id=quest.id,
        dst_ref_id=mid,
        relation="cites",
        set_by="system",
    )
    # The memory is edited through the verb: the audit no longer binds.
    runtime_with_store.dispatch(
        "edit",
        {"kind": "memory", "id": mid, "mode": "replace", "text": BODY + " edited."},
    )
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "view": "grounding"}
    )
    assert f"# me{mid} grounding" in out
    assert "## claims (2)" in out
    claim_lines = [line for line in out.splitlines() if re.match(r"^\[\d+:\d+\]", line)]
    assert len(claim_lines) == 2
    assert any("status: supported" in line for line in out.splitlines()) is False
    assert any(line.startswith("  status: unresolved") for line in out.splitlines())
    # The stale audit stays inspectable in the history.
    assert "## audit history (newest first)" in out
    history = [line for line in out.splitlines() if line.startswith("- review ")]
    assert len(history) == 1
    assert "stale" in history[0]
    # The dependent consumer is listed because the audit is stale.
    assert "## affected consumers" in out
    assert f"qu{quest.id}" in out
    # Re-audit: current again, both review acts kept newest-first, and the
    # consumers section disappears.
    _audit(store, mid, _claim(0, kind="definition"), _claim(1, kind="definition"))
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "view": "grounding"}
    )
    assert "## affected consumers" not in out
    assert g.grounding_summary(store, mid).freshness == "current"
    history = [line for line in out.splitlines() if line.startswith("- review ")]
    assert len(history) == 2
    assert "current" in history[0] and "stale" in history[1]
    matches = [re.search(r"· (\d{4}-\d{2}-\d{2}) ·", line) for line in history]
    assert all(matches)
    times = [m.group(1) for m in matches if m]
    assert times[0] >= times[1]


def test_view_grounding_shows_unpinned_passages_and_disputes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The drill-down names what the summary line cannot: citations that
    never resolved, the decisive passage verified against the live body,
    and disagreement already on record as ``disputes`` edges."""
    body = f"{S1[:-1]} (pc424242). {S2}"
    mid = _put(runtime_with_store, store, body)
    _audit(
        store,
        mid,
        _claim(0, quote=f"{S1[:-1]} (pc424242).", evidence=["pc424242"]),
        _claim(1),
        body=body,
        rollups=((1, "supported"),),
    )
    other = store.insert_ref(kind="memory", slug=None, title="Disputing node")
    store.add_link(
        src_ref_id=other.id,
        dst_ref_id=mid,
        relation="disputes",
        set_by="system",
    )
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "view": "grounding"}
    )
    assert "unpinned citations: pc424242" in out
    assert "passage: matches the live body at this span" in out
    assert "## known disputes" in out
    assert f"me{other.id}" in out
    # The unpinned citation keeps the claim out of the supported bucket on
    # the read line too.
    line = _grounding_line(out)
    assert "1 supported" in line and "1 unresolved" in line


def test_index_bullet_carries_the_grounding_chip(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The session-start recall path — ``search(view='index')`` bullets —
    shows coverage too; a bullet that hides grounding was the complaint."""
    mid = _put(runtime_with_store, store)
    out = runtime_with_store.dispatch("search", {"kind": "memory", "view": "index"})
    assert "grounding: not audited" in out
    _audit(
        store,
        mid,
        _claim(0),
        _claim(1),
        rollups=((0, "supported"), (1, "supported")),
    )
    out = runtime_with_store.dispatch("search", {"kind": "memory", "view": "index"})
    assert "grounding 2/2+ supported" in out
    # And the q-path of the index view chips the same way.
    out = runtime_with_store.dispatch(
        "search", {"kind": "memory", "q": "ingest worker", "view": "index"}
    )
    assert "grounding 2/2+ supported" in out


def test_view_grounding_audit_selector_renders_prior_judgment(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``args={'audit': <review_id>}`` re-renders one kept audit act: the
    recorded statuses and pins — a stale row stays inspectable instead of
    flattening to a date and a count."""
    mid = _put(runtime_with_store, store)
    _audit(store, mid, _claim(0, kind="definition"), _claim(1, kind="definition"))
    rec = g.latest_audit(store, mid)
    assert rec is not None
    rid_review = rec.review_id
    # Edit the memory: the row goes stale but the judgment is kept.
    runtime_with_store.dispatch(
        "edit",
        {"kind": "memory", "id": mid, "mode": "replace", "text": BODY + " edited."},
    )
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "memory",
            "id": mid,
            "view": "grounding",
            "__extras__": {"audit": rid_review},
        },
    )
    assert f"# me{mid} grounding · audit review {rid_review}" in out
    assert "now stale" in out
    assert "recorded: provisional" in out or "recorded: supported" in out
    # A review id that is not an audit of this memory is a BadInput.
    try:
        runtime_with_store.dispatch(
            "get",
            {
                "kind": "memory",
                "id": mid,
                "view": "grounding",
                "__extras__": {"audit": 999999999},
            },
        )
    except Exception as exc:
        assert "no grounding audit" in str(exc) or "999999999" in str(exc)


def test_view_grounding_lists_unchecked_text_ranges(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A claim whose quote covers only part of its sentence leaves the
    remainder unchecked: the drill-down names the exact body range, so one
    supported sentence cannot mask unchecked text."""
    compound = f"{S1} and more besides."
    mid = _put(runtime_with_store, store, compound)
    _audit(
        store,
        mid,
        _claim(0, quote=S1, kind="definition"),
        body=compound,
    )
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "view": "grounding"}
    )
    assert "## unchecked text (1 ranges)" in out
    assert "and more besides." in out
    line = _grounding_line(out)
    assert "2+ claims" in line and "truncated scan" in line
