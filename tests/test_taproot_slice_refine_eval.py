"""hub_refine dry-run harness (`src/precis/taproot/slice_refine_eval.py`).

Offline unit test — no live model, no real DB. A fake store/conn/verify_fn
pins the read-only replay: attached/rejected papers are bucketed WITHOUT
ever reaching ``verify_fn``, a duplicate candidate paper is deduped before
verify, and every verdict lands in the right bucket. The fake store defines
no write method the harness could call by accident (see
``test_never_touches_a_write_method``).
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from precis.taproot.slice_refine_eval import eval_hub_slice

# ── fakes ────────────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, fetchone_value: Any = None, fetchall_value: Any = None) -> None:
        self._fetchone = fetchone_value
        self._fetchall = fetchall_value or []

    def fetchone(self) -> Any:
        return self._fetchone

    def fetchall(self) -> Any:
        return self._fetchall


class FakeConn:
    """Dispatches on the SQL shape — the three read-only queries
    ``slice_refine_eval`` issues per hub (fetch hub row, attached-paper
    ids, existing-edge count). Raises on anything else, so an accidental
    write query fails loud rather than silently no-oping."""

    def __init__(
        self,
        hub_rows: Mapping[int, tuple[str, dict[str, Any]] | None],
        attached_by_hub: dict[int, set[int]],
        existing_edges_by_hub: dict[int, int],
    ) -> None:
        self.hub_rows = hub_rows
        self.attached_by_hub = attached_by_hub
        self.existing_edges_by_hub = existing_edges_by_hub

    def execute(self, sql: str, params: Any = None) -> FakeResult:
        s = " ".join(sql.split()).lower()
        params = params or ()
        if "select title, meta from refs" in s:
            ref_id = params[0]
            return FakeResult(fetchone_value=self.hub_rows.get(ref_id))
        if "select distinct src_ref_id from links" in s:
            hub_ref_id = params[0]
            ids = self.attached_by_hub.get(hub_ref_id, set())
            return FakeResult(fetchall_value=[(pid,) for pid in ids])
        if "select count(*) from links" in s:
            hub_ref_id = params[0]
            return FakeResult(
                fetchone_value=(self.existing_edges_by_hub.get(hub_ref_id, 0),)
            )
        raise AssertionError(f"unexpected SQL issued by the harness: {sql!r}")


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


class FakeStore:
    """Deliberately defines NO write method — ``attach_evidence`` /
    ``update_ref`` raise if ever called, so a regression that starts
    writing fails the test immediately instead of silently mutating a
    fake (or, worse, a real store)."""

    blocks = property(
        lambda self: self
    )  # blocks carve: flat fake doubles as its own sub-store

    def __init__(
        self, conn: FakeConn, candidates: list[tuple[Any, Any, float]]
    ) -> None:
        self.pool = FakePool(conn)
        self._candidates = candidates

    def search_blocks(self, **kwargs: Any) -> list[tuple[Any, Any, float]]:
        return self._candidates

    def attach_evidence(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("slice_refine_eval must never write (attach_evidence)")

    def update_ref(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("slice_refine_eval must never write (update_ref)")


class FakeEmbedder:
    def embed_one(self, q: str) -> list[float]:
        return [0.1, 0.2, 0.3]


@dataclass(frozen=True)
class FakeRef:
    id: int
    slug: str | None


@dataclass(frozen=True)
class FakeBlock:
    id: int
    pos: int
    text: str


def _stub_verify(calls: list[str]):
    def verify(
        *,
        claim: str,
        scope: dict[str, Any],
        target_cite_key: str,
        target_chunk_ord: int,
        target_chunk_text: str,
    ) -> dict[str, Any] | None:
        calls.append(target_cite_key)
        if target_chunk_text == "SUPPORTS":
            return {"supports": "yes", "caveats": ["mild hedge"]}
        if target_chunk_text == "REJECT":
            return {"supports": "no", "caveats": []}
        if target_chunk_text == "FAIL":
            return None
        if target_chunk_text == "UNKNOWN":
            return {"supports": "maybe"}
        raise AssertionError(f"verify_fn called unexpectedly for {target_chunk_text!r}")

    return verify


# ── tests ────────────────────────────────────────────────────────────────

_HUB_ID = 42


def _make_store_and_candidates() -> tuple[FakeStore, list[str]]:
    hub_rows = {
        _HUB_ID: (
            "Pd/C catalyzes Suzuki coupling at room temperature",
            {
                "scope": {"material": "Pd/C"},
                "taproot_rejected": {"77": {"supports": "no"}},
            },
        )
    }
    attached_by_hub = {_HUB_ID: {10}}
    existing_edges_by_hub = {_HUB_ID: 3}
    conn = FakeConn(hub_rows, attached_by_hub, existing_edges_by_hub)

    candidates = [
        # already attached -> skipped_attached, never verified
        (FakeBlock(id=100, pos=0, text="ATTACHED"), FakeRef(id=10, slug="ref-10"), 0.1),
        # in the rejection memo -> skipped_rejected, never verified
        (
            FakeBlock(id=101, pos=1, text="REJECTEDMEMO"),
            FakeRef(id=77, slug="ref-77"),
            0.2,
        ),
        # verifies "yes" -> would_attach
        (FakeBlock(id=102, pos=2, text="SUPPORTS"), FakeRef(id=20, slug="ref-20"), 0.3),
        # same paper resurfacing on a different chunk -> deduped, verify_fn
        # called only once for ref-20
        (
            FakeBlock(id=103, pos=9, text="SUPPORTS"),
            FakeRef(id=20, slug="ref-20"),
            0.35,
        ),
        # verifies "no" -> would_reject
        (FakeBlock(id=104, pos=3, text="REJECT"), FakeRef(id=30, slug="ref-30"), 0.4),
        # verify_fn returns None (transient) -> verify_failed
        (FakeBlock(id=105, pos=4, text="FAIL"), FakeRef(id=40, slug="ref-40"), 0.5),
        # verdict outside {yes,partial,no} -> unexpected
        (FakeBlock(id=106, pos=5, text="UNKNOWN"), FakeRef(id=50, slug="ref-50"), 0.6),
        # the hub itself surfacing in its own search -> skipped outright
        (
            FakeBlock(id=107, pos=6, text="SELF"),
            FakeRef(id=_HUB_ID, slug="hub-slug"),
            0.05,
        ),
    ]
    store = FakeStore(conn, candidates)
    return store, []


def test_buckets_populate_correctly_and_dedup_by_paper() -> None:
    store, _ = _make_store_and_candidates()
    calls: list[str] = []
    verify_fn = _stub_verify(calls)

    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=verify_fn,
        progress=False,
    )

    assert len(report.hubs) == 1
    hub = report.hubs[0]
    assert hub.hub_ref_id == _HUB_ID
    assert hub.existing_edges == 3
    assert not hub.discovery_skipped

    assert [c.paper_ref_id for c in hub.would_attach] == [20]
    assert hub.would_attach[0].support == "yes"
    assert hub.would_attach[0].caveats == ["mild hedge"]
    assert hub.would_attach[0].source_handle == "pc102"

    assert [c.paper_ref_id for c in hub.would_reject] == [30]
    assert hub.would_reject[0].support == "no"

    assert hub.verify_failed == 1
    assert hub.unexpected == 1

    assert [c.paper_ref_id for c in hub.skipped_attached] == [10]
    assert [c.paper_ref_id for c in hub.skipped_rejected] == [77]

    # the attached paper and the memo-rejected paper are NEVER verified
    assert "ref-10" not in calls
    assert "ref-77" not in calls
    # the duplicate ref-20 candidate is deduped before verify -- called once
    assert calls.count("ref-20") == 1


def test_hub_itself_never_verified_as_its_own_candidate() -> None:
    store, _ = _make_store_and_candidates()
    calls: list[str] = []
    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=_stub_verify(calls),
        progress=False,
    )
    hub = report.hubs[0]
    all_paper_ids = {
        c.paper_ref_id
        for bucket in (
            hub.would_attach,
            hub.would_reject,
            hub.skipped_attached,
            hub.skipped_rejected,
        )
        for c in bucket
    }
    assert _HUB_ID not in all_paper_ids


def test_discovery_skipped_when_claim_sentence_is_blank() -> None:
    hub_rows: dict[int, tuple[str, dict[str, Any]] | None] = {_HUB_ID: ("   ", {})}
    conn = FakeConn(hub_rows, {}, {})
    store = FakeStore(conn, candidates=[])

    def _boom_verify(**kwargs: Any) -> None:
        raise AssertionError("verify_fn must not be called when discovery is skipped")

    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=_boom_verify,
        progress=False,
    )
    assert len(report.hubs) == 1
    hub = report.hubs[0]
    assert hub.discovery_skipped is True
    assert hub.would_attach == []
    assert hub.would_reject == []


def test_gone_hub_is_omitted_from_the_report() -> None:
    conn = FakeConn({_HUB_ID: None}, {}, {})
    store = FakeStore(conn, candidates=[])

    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=_stub_verify([]),
        progress=False,
    )
    assert report.hubs == []


def test_format_runs_without_error_for_every_bucket_shape() -> None:
    store, _ = _make_store_and_candidates()
    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=_stub_verify([]),
        progress=False,
    )
    text = report.format()
    assert "hub #42" in text
    assert "would_attach" in text
    assert "summary" in text


def test_never_touches_a_write_method() -> None:
    """Fidelity guard: the fake store's ``attach_evidence``/``update_ref``
    raise if ever invoked (see :class:`FakeStore`) -- a clean run over a
    slice with both would-attach and would-reject candidates must not
    trip either."""
    store, _ = _make_store_and_candidates()
    report = eval_hub_slice(
        store,
        [_HUB_ID],
        embedder=FakeEmbedder(),
        verify_fn=_stub_verify([]),
        progress=False,
    )
    assert report.hubs[0].would_attach and report.hubs[0].would_reject
