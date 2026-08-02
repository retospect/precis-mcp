"""Contract tests for ``precis stats`` — observability CLI.

Exercises the STATUS-count + stub-backlog summaries via the
underlying query helpers. The CLI dispatch layer is thin
(argparse → ``run()``); we test the queries against real DB
state rather than re-mocking the argparse wiring.
"""

from __future__ import annotations

import re

import pytest

from precis.cli.stats import (
    _query_argument_caveats,
    _query_argument_contradictions,
    _query_argument_stale,
    _query_findings,
    _query_stubs,
)
from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.handlers.memory import MemoryHandler
from precis.store.types import BlockInsert, Tag
from tests.conftest import id_of


def _make_handler(store):
    return FindingHandler(hub=Hub(store=store))


def _seed_paper(store, *, cite_key: str, pdf_sha256: str | None = None) -> int:
    """Insert a paper ref; pass ``pdf_sha256=None`` to leave it as a stub."""
    ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=f"Test paper {cite_key}",
        meta={},
    )
    store.insert_blocks(
        ref.id,
        [BlockInsert(pos=0, text=f"Body of {cite_key}.", meta={})],
    )
    if pdf_sha256 is not None:
        with store.pool.connection() as conn:
            # ``pdfs`` is FK-referenced; insert a minimal row so
            # the UPDATE has a valid target. The schema requires
            # ``content_hash`` (the body-text canonical hash); we
            # derive a deterministic placeholder from the sha so
            # repeat fixture runs don't violate the UNIQUE.
            content_hash = ("c" + pdf_sha256[1:]).ljust(64, "0")[:64]
            conn.execute(
                "INSERT INTO pdfs "
                "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (pdf_sha256, content_hash, 1, 1, f"test-stats/{pdf_sha256}.pdf"),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (pdf_sha256, ref.id),
            )
    return ref.id


def _seed_finding(store, *, cite_key: str, status: str) -> int:
    """Seed a paper + finding, then stamp the given STATUS value."""
    _seed_paper(store, cite_key=cite_key, pdf_sha256=cite_key.ljust(64, "0"))
    h = _make_handler(store)
    resp = h.put(
        title=f"claim {cite_key}",
        body=f"body for {cite_key}",
        scope={},
        cited_in=cite_key,
    )
    id_m = re.search(r"id=(\d+)", resp.body)
    assert id_m is not None, f"create-ack missing id=; got {resp.body!r}"
    rid = int(id_m.group(1))
    # put() always lands STATUS:tracing; flip only when caller asked
    # for something else.
    if status != "tracing":
        store.add_tag(
            rid,
            Tag.closed("STATUS", status),
            set_by="chase",
            replace_prefix=True,
        )
    return rid


# ── findings summary ────────────────────────────────────────────────


class TestQueryFindings:
    def test_empty_corpus_returns_empty(self, store) -> None:
        assert _query_findings(store) == []

    def test_counts_by_status(self, store) -> None:
        _seed_finding(store, cite_key="a", status="tracing")
        _seed_finding(store, cite_key="b", status="tracing")
        _seed_finding(store, cite_key="c", status="established")
        _seed_finding(store, cite_key="d", status="multi_candidate")

        rows = _query_findings(store)
        as_dict = {r["status"]: r["count"] for r in rows}
        assert as_dict == {
            "tracing": 2,
            "established": 1,
            "multi_candidate": 1,
        }

    def test_ordering_count_desc(self, store) -> None:
        """Rows come back highest-count first so the busiest state
        is what an operator scans for at the top."""
        _seed_finding(store, cite_key="a", status="tracing")
        _seed_finding(store, cite_key="b", status="tracing")
        _seed_finding(store, cite_key="c", status="tracing")
        _seed_finding(store, cite_key="d", status="established")

        rows = _query_findings(store)
        assert rows[0]["status"] == "tracing"
        assert rows[0]["count"] == 3

    def test_deleted_findings_excluded(self, store) -> None:
        """Soft-deleted findings drop out of the summary."""
        kept = _seed_finding(store, cite_key="a", status="established")
        gone = _seed_finding(store, cite_key="b", status="established")
        store.soft_delete_ref(gone)

        rows = _query_findings(store)
        assert {r["status"]: r["count"] for r in rows} == {"established": 1}
        # The kept ref is still visible.
        del kept


# ── stubs summary ───────────────────────────────────────────────────


class TestQueryStubs:
    def test_empty_corpus_returns_empty(self, store) -> None:
        assert _query_stubs(store) == []

    def test_awaiting_when_never_attempted(self, store) -> None:
        """A stub with no fetcher event lands in the 'awaiting' bucket."""
        _seed_paper(store, cite_key="stub-a", pdf_sha256=None)
        _seed_paper(store, cite_key="stub-b", pdf_sha256=None)
        rows = _query_stubs(store)
        assert rows == [{"state": "awaiting", "count": 2}]

    def test_retry_when_fetcher_event_present(self, store) -> None:
        """A stub that's been attempted at least once (fetcher event
        row exists) lands in the 'retry' bucket."""
        rid_a = _seed_paper(store, cite_key="stub-a", pdf_sha256=None)
        _seed_paper(store, cite_key="stub-b", pdf_sha256=None)
        # Plant a single fetcher event on stub-a so it crosses into
        # 'retry'. Schema mirrors how the fetcher worker writes.
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (rid_a, "fetcher:unpaywall", "no_oa", "{}"),
            )

        rows = _query_stubs(store)
        as_dict = {r["state"]: r["count"] for r in rows}
        assert as_dict == {"awaiting": 1, "retry": 1}

    def test_non_stub_paper_excluded(self, store) -> None:
        """A paper whose pdf_sha256 is set is no longer a stub and
        drops out of the count."""
        _seed_paper(store, cite_key="have-pdf", pdf_sha256="a" * 64)
        _seed_paper(store, cite_key="stub", pdf_sha256=None)

        rows = _query_stubs(store)
        assert rows == [{"state": "awaiting", "count": 1}]


# ── argument-graph corpus report (ADR 0054, build order step 5) ────


def _memory_handler(store) -> MemoryHandler:
    return MemoryHandler(hub=Hub(store=store))


class TestQueryArgumentStale:
    def test_empty_corpus_returns_empty(self, store) -> None:
        assert _query_argument_stale(store) == []

    def test_lists_ripple_tagged_inference(self, store) -> None:
        m = _memory_handler(store)
        paper = _seed_paper(store, cite_key="root")
        lemma = id_of(m.put(text="claims X", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=lemma, dst_ref_id=paper, relation="cites")
        infer = id_of(m.put(text="from X, Y", tags=["kind:inference"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=lemma, relation="derived-from")

        notice = store.insert_ref(kind="paper", slug="notice", title="n", meta={}).id
        store.add_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")

        rows = _query_argument_stale(store)
        assert [r["id"] for r in rows] == [infer]


class TestQueryArgumentCaveats:
    def test_empty_corpus_returns_empty(self, store) -> None:
        assert _query_argument_caveats(store) == []

    def test_lists_inference_with_inherited_caveat(self, store) -> None:
        m = _memory_handler(store)
        lemma = id_of(m.put(text="claims X", tags=["kind:lemma"]).body)
        infer = id_of(m.put(text="from X, Y", tags=["kind:inference"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=lemma, relation="derived-from")
        caveat = id_of(m.put(text="only n<100", tags=["kind:caveat"]).body)
        store.add_link(src_ref_id=caveat, dst_ref_id=lemma, relation="qualifies")

        rows = _query_argument_caveats(store)
        assert [r["id"] for r in rows] == [infer]

    def test_inference_without_caveat_excluded(self, store) -> None:
        m = _memory_handler(store)
        lemma = id_of(m.put(text="claims X", tags=["kind:lemma"]).body)
        infer = id_of(m.put(text="from X, Y", tags=["kind:inference"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=lemma, relation="derived-from")
        assert _query_argument_caveats(store) == []


class TestQueryArgumentContradictions:
    def test_empty_corpus_returns_empty(self, store) -> None:
        assert _query_argument_contradictions(store) == []

    def test_lists_contradicting_lemma_pair(self, store) -> None:
        m = _memory_handler(store)
        a = id_of(m.put(text="X is true", tags=["kind:lemma"]).body)
        b = id_of(m.put(text="X is false", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")

        rows = _query_argument_contradictions(store)
        assert rows == [
            {"a_id": a, "a_title": "X is true", "b_id": b, "b_title": "X is false"}
        ]

    def test_contradiction_between_non_argument_nodes_excluded(self, store) -> None:
        m = _memory_handler(store)
        a = id_of(m.put(text="plain note A").body)  # no kind:lemma tag
        b = id_of(m.put(text="plain note B").body)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")
        assert _query_argument_contradictions(store) == []


# ── integration: CLI dispatch ───────────────────────────────────────


class TestCli:
    def test_default_runs_both_sections(
        self, store, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``precis stats`` (no flags) prints both findings + stubs."""
        import sys

        from precis.cli.main import main as cli_main

        _seed_finding(store, cite_key="a", status="established")
        _seed_paper(store, cite_key="stub", pdf_sha256=None)

        dsn = store.pool.conninfo
        monkeypatch.setattr(sys, "argv", ["precis", "stats", "--database-url", dsn])
        cli_main()
        out = capsys.readouterr().out
        assert "# findings" in out
        assert "established" in out
        assert "# stubs" in out
        assert "awaiting" in out

    def test_findings_flag_isolates_section(
        self, store, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``--findings`` suppresses the stubs half."""
        import sys

        from precis.cli.main import main as cli_main

        _seed_finding(store, cite_key="a", status="established")
        _seed_paper(store, cite_key="stub", pdf_sha256=None)

        dsn = store.pool.conninfo
        monkeypatch.setattr(
            sys,
            "argv",
            ["precis", "stats", "--findings", "--database-url", dsn],
        )
        cli_main()
        out = capsys.readouterr().out
        assert "# findings" in out
        assert "# stubs" not in out

    def test_argument_flag_isolates_section(
        self, store, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``--argument`` shows only the three argument-graph sections."""
        import sys

        from precis.cli.main import main as cli_main

        m = _memory_handler(store)
        paper = _seed_paper(store, cite_key="root")
        lemma = id_of(m.put(text="claims X", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=lemma, dst_ref_id=paper, relation="cites")
        infer = id_of(m.put(text="from X, Y", tags=["kind:inference"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=lemma, relation="derived-from")
        notice = store.insert_ref(kind="paper", slug="notice", title="n", meta={}).id
        store.add_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")

        dsn = store.pool.conninfo
        monkeypatch.setattr(
            sys, "argv", ["precis", "stats", "--argument", "--database-url", dsn]
        )
        cli_main()
        out = capsys.readouterr().out
        assert "# argument-stale-premise" in out
        assert "from X, Y" in out
        assert "# argument-unaddressed-caveats" in out
        assert "# argument-open-contradictions" in out
        assert "# findings" not in out
        assert "# stubs" not in out

    def test_default_includes_argument_sections(
        self, store, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """No flags = all sections, including the argument report."""
        import sys

        from precis.cli.main import main as cli_main

        dsn = store.pool.conninfo
        monkeypatch.setattr(sys, "argv", ["precis", "stats", "--database-url", dsn])
        cli_main()
        out = capsys.readouterr().out
        assert "# argument-stale-premise" in out
        assert "# argument-unaddressed-caveats" in out
        assert "# argument-open-contradictions" in out
