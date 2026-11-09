"""Tests for ``offer_discovery`` — S9 (td568212): three-day lead discovery
plus the qualification path (dark account -> golden eval vs the incumbent
-> Reto's decision -> enabled). Real PG (the ``store`` fixture); ``refs``
is wiped per test but ``llm_account`` is not (conftest's keep-list) — any
``llm_account`` row this suite seeds is deleted explicitly in a
``finally`` so it never leaks into another test's ``list_accounts`` read.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from precis.store.types import Tag
from precis.workers import offer_discovery
from precis.workers.offer_discovery import (
    _LEAD_META_KEY,
    _measured_ordinals,
    _parse_vendor_lines,
    run_offer_discovery_pass,
    run_research_call,
)

#: One lead (groq), two vendors reporting "none" — the rigid three-line
#: shape the research prompt asks for.
_REPORT = "groq: groq-free-test-model\ncerebras: none\nopenrouter: none\n"


def _todo_titles(store: Any) -> list[str]:
    """Every live todo's title, oldest first — the lead AND any decision todo
    (a decision todo carries no lead meta, so ``_leads`` cannot see it)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT title FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "ORDER BY ref_id"
        ).fetchall()
    return [str(r[0]) for r in rows]


def _leads(store: Any) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta ? %s",
            (_LEAD_META_KEY,),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _one_lead(store: Any) -> tuple[int, dict[str, Any]]:
    leads = _leads(store)
    assert len(leads) == 1, leads
    return leads[0]


def _answer(store: Any, ref_id: int, text: str) -> None:
    """Comment ``text`` on a todo, then close it STATUS:done — the
    ``precis-work-help`` answer shape the qualification path waits on."""
    from precis.handlers import _comment_timeline as tl

    ref = store.get_ref(kind="todo", id=ref_id)
    tl.append_comment(store, ref, kind="todo", comment_kind="todo_comment", text=text)
    store.add_tag(
        ref_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def _delete_groq_accounts(store: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM llm_account WHERE vendor = 'groq'")
        conn.commit()


def _stub_eval_dispatch(req: Any) -> Any:
    """Answers every gold task the same way — never errors, so every wired
    axis scores (not void) and a real ordinal records. The score itself
    doesn't matter to these tests: they check state transitions, not
    eval quality."""
    return SimpleNamespace(text="stub response", data=None, error=None)


def _void_eval_dispatch(req: Any) -> Any:
    """A vendor with no profile/key wired yet: every call is an error, so every
    axis comes back void (while ``EvalReport.ordinals`` still lists it, at 1)."""
    return SimpleNamespace(text="", data=None, error="no key wired for this vendor")


class TestResearchCall:
    def test_goes_through_the_hubs_sibling_not_a_handler_import(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker must not import ``precis.handlers`` (import-linter): the
        Perplexity handler is reached with ``Hub.sibling``."""
        from precis.dispatch import Hub

        asked: list[str] = []
        calls: list[dict[str, Any]] = []

        class _Handler:
            def get(self, **kw: Any) -> Any:
                calls.append(kw)
                return SimpleNamespace(body="groq: from-the-stub\n")

        def sibling(self: Any, kind: str) -> Any:
            asked.append(kind)
            return _Handler()

        monkeypatch.setattr(Hub, "sibling", sibling)
        assert run_research_call(store, "q?") == "groq: from-the-stub\n"
        assert asked == ["perplexity-reasoning"]
        assert calls == [{"q": "q?", "refresh": True}]


class TestParseVendorLines:
    @pytest.mark.parametrize(
        "line",
        [
            "**Groq:** llama-x",
            "**Groq**: llama-x",
            "- **Groq:** `llama-x`",
            "> groq: **llama-x**",
            "GROQ :   llama-x",
            "groq: llama-x",
        ],
    )
    def test_markdown_emphasis_round_the_label_still_yields_the_lead(
        self, line: str
    ) -> None:
        assert _parse_vendor_lines(line) == [("groq", "llama-x")]

    @pytest.mark.parametrize(
        "value",
        [
            "No free tier",
            "no free tier at the moment",
            "No",
            "none",
            "None.",
            "N/A",
            "**None**",
            "null",
            "-",
        ],
    )
    def test_a_negative_answer_is_not_a_lead(self, value: str) -> None:
        assert _parse_vendor_lines(f"cerebras: {value}") == []
        assert _parse_vendor_lines(f"- **Cerebras:** {value}") == []

    def test_only_the_real_lead_survives_a_mixed_answer(self) -> None:
        text = "**Groq:** llama-x\ncerebras: No free tier\n**OpenRouter:** N/A\n"
        assert _parse_vendor_lines(text) == [("groq", "llama-x")]

    def test_an_empty_value_does_not_swallow_the_next_line(self) -> None:
        # ``\s`` after the colon let ``groq:`` read the NEXT line as its value.
        assert _parse_vendor_lines("groq:\ncerebras: foo-model\n") == [
            ("cerebras", "foo-model")
        ]

    def test_a_model_id_with_a_colon_suffix_is_kept_whole(self) -> None:
        assert _parse_vendor_lines("openrouter: meta/llama-x:free") == [
            ("openrouter", "meta/llama-x:free")
        ]


class TestMeasuredOrdinals:
    def test_void_axes_are_not_measured(self) -> None:
        from precis.llm_eval.harness import AxisResult, EvalReport

        report = EvalReport(
            model="m",
            results=[
                AxisResult(axis="a", n=3, mean_score=0.9, ordinal=5, recorded=True),
                AxisResult(axis="b", n=0, mean_score=0.0, ordinal=1, errors=2),
            ],
            skipped=[],
        )
        # The premise: a void axis still shows up in ``ordinals``, at 1.
        assert report.ordinals == {"a": 5, "b": 1}
        assert _measured_ordinals(report, recorded=True) == {"a": 5}
        assert _measured_ordinals(report, recorded=False) == {"a": 5}

    def test_the_candidate_needs_the_ordinal_on_its_card(self) -> None:
        from precis.llm_eval.harness import AxisResult, EvalReport

        report = EvalReport(
            model="m",
            results=[AxisResult(axis="a", n=3, mean_score=0.9, ordinal=5)],
            skipped=[],
        )
        assert _measured_ordinals(report, recorded=True) == {}
        assert _measured_ordinals(report, recorded=False) == {"a": 5}


class TestDiscovery:
    def test_discovers_a_lead_and_writes_no_account(self, store: Any) -> None:
        res = run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, meta = _one_lead(store)
        lead = meta[_LEAD_META_KEY]
        assert lead["vendor"] == "groq"
        assert lead["model_id"] == "groq-free-test-model"
        assert lead["state"] == "pending"
        assert res.claimed >= 1
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM llm_account WHERE vendor = 'groq'"
            ).fetchone()
        assert row is None

    def test_rerun_files_no_duplicate_decision_row(self, store: Any) -> None:
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        assert len(_leads(store)) == 1

    def test_no_report_text_discovers_nothing(self, store: Any) -> None:
        res = run_offer_discovery_pass(store, force=True, research_fn=lambda s: None)
        assert _leads(store) == []
        assert res.claimed == 0

    def test_known_model_is_not_a_lead(self, store: Any) -> None:
        from precis import llm_catalog

        llm_catalog.upsert_card(
            store, model_id="groq-free-test-model", text="already known."
        )
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        assert _leads(store) == []

    def test_non_scoped_vendor_is_not_a_lead(self, store: Any) -> None:
        report = "groq: none\ncerebras: none\nopenrouter: none\n"
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: report)
        assert _leads(store) == []

    def test_no_free_tier_sentence_files_no_lead(self, store: Any) -> None:
        # "cerebras: No free tier" used to file a lead named "No".
        report = "groq: none\ncerebras: No free tier\nopenrouter: N/A\n"
        res = run_offer_discovery_pass(store, force=True, research_fn=lambda s: report)
        assert _leads(store) == []
        assert res.claimed == 0

    def test_markdown_labelled_answer_files_the_lead(self, store: Any) -> None:
        # "**Groq:** x" used to file nothing.
        report = (
            "**Groq:** groq-free-test-model\n**Cerebras:** None\n**OpenRouter:** n/a\n"
        )
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: report)
        _, meta = _one_lead(store)
        assert meta[_LEAD_META_KEY]["vendor"] == "groq"
        assert meta[_LEAD_META_KEY]["model_id"] == "groq-free-test-model"


class TestThrottle:
    def test_window_is_stamped_before_discovery_so_a_raise_costs_one_call(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paid_calls: list[int] = []

        def research(_store: Any) -> str:
            paid_calls.append(1)
            return _REPORT

        def boom(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("todo insert failed")

        monkeypatch.setattr(offer_discovery, "_discover_leads", boom)
        with pytest.raises(RuntimeError, match="todo insert failed"):
            run_offer_discovery_pass(store, force=True, research_fn=research)
        assert paid_calls == [1]
        assert store.get_setting(offer_discovery._STATE_KEY)
        # The next (unforced) cycle is inside the window: no second paid call.
        run_offer_discovery_pass(store, research_fn=research)
        assert paid_calls == [1]


class TestQualificationPath:
    def test_pursue_seeds_a_dark_account_and_card_then_awaits_decision(
        self, store: Any
    ) -> None:
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: pursue — worth a look")
        try:
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            lead = meta[_LEAD_META_KEY]
            account = lead["account"]
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT enabled FROM llm_account WHERE name = %s", (account,)
                ).fetchone()
            assert row is not None
            assert row[0] is False  # dark
            card = store.find_ref_by_meta(
                kind="llm", key="model_id", value="groq-free-test-model"
            )
            assert card is not None
            cap = (card.meta or {}).get("capability") or {}
            assert cap, "the stub dispatch should have recorded at least one ordinal"
            # eager: the same pass that seeds the account also runs the eval.
            assert lead["state"] == "awaiting_decision"
            assert lead["decision_todo_id"]
        finally:
            _delete_groq_accounts(store)

    def test_void_eval_files_no_decision_and_the_lead_stays_qualifying(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from precis.llm_eval.harness import run_eval
        from precis.utils.llm.router import Tier

        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: pursue")
        try:
            # The premise this test stands on: an all-void report still carries
            # an ordinal (1) for every axis, so "has ordinals" proves nothing.
            report = run_eval(
                store,
                model="groq-free-test-model",
                tier=Tier("medium"),
                record=False,
                dispatch_fn=_void_eval_dispatch,
            )
            assert report.ordinals
            assert all(r.void for r in report.results)

            with caplog.at_level(logging.WARNING, logger=offer_discovery.log.name):
                res = run_offer_discovery_pass(
                    store,
                    force=True,
                    research_fn=lambda s: None,
                    eval_dispatch_fn=_void_eval_dispatch,
                )
            # Seeded dark, evaluated, nothing measured: the lead has not moved
            # past "qualifying" and no "enable?" todo was filed.
            _, meta = _one_lead(store)
            lead = meta[_LEAD_META_KEY]
            assert lead["state"] == "qualifying"
            assert "decision_todo_id" not in lead
            assert [t for t in _todo_titles(store) if "enable" in t] == []
            assert len(_todo_titles(store)) == 1  # the lead's own todo
            card = store.find_ref_by_meta(
                kind="llm", key="model_id", value="groq-free-test-model"
            )
            assert card is not None
            assert not (card.meta or {}).get("capability")
            assert any(
                r.levelno == logging.WARNING
                and "no recorded eval axis" in r.getMessage()
                for r in caplog.records
            )
            # pending -> qualifying counted (1); the eval step added nothing
            assert res.claimed == 1

            # The key gets wired: the very same lead advances on the next cycle.
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            assert meta[_LEAD_META_KEY]["state"] == "awaiting_decision"
            assert any("enable" in t for t in _todo_titles(store))
        finally:
            _delete_groq_accounts(store)

    def test_unpinned_candidate_eval_pins_the_account_and_its_refusal_retries(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No eval_dispatch_fn: the candidate eval pins itself to the lead's
        dark account (gr572168). The seeded account is unprovisioned
        (adapter 'api', no profile), so resolve_account_pin refuses before
        any request — the refusal is the "not yet routable" state and the
        lead stays 'qualifying' for the next pass."""
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: pursue")
        try:
            with caplog.at_level(logging.WARNING, logger=offer_discovery.log.name):
                run_offer_discovery_pass(store, force=True, research_fn=lambda s: None)
            _, meta = _one_lead(store)
            lead = meta[_LEAD_META_KEY]
            assert lead["state"] == "qualifying"
            assert lead["account"]  # seeded, still dark
            assert "decision_todo_id" not in lead
            assert [t for t in _todo_titles(store) if "enable" in t] == []
            card = store.find_ref_by_meta(
                kind="llm", key="model_id", value="groq-free-test-model"
            )
            assert card is not None
            assert not (card.meta or {}).get("capability")
            assert not (card.meta or {}).get("account_evals")
            assert any(
                r.levelno == logging.WARNING and "eval refused" in r.getMessage()
                for r in caplog.records
            )
        finally:
            _delete_groq_accounts(store)

    def test_pursue_keeps_an_existing_cards_text(self, store: Any) -> None:
        from precis import llm_catalog

        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        # The card shows up AFTER the lead was filed (a hand-written card, a
        # seed): the dark-card step must not overwrite its prose.
        curated = "Hand-written card: fast and cheap, weak on long context."
        card_id, _ = llm_catalog.upsert_card(
            store, model_id="groq-free-test-model", text=curated
        )
        _answer(store, ref_id, "answer: pursue")
        try:
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            card = store.find_ref_by_meta(
                kind="llm", key="model_id", value="groq-free-test-model"
            )
            assert card is not None
            assert card.id == card_id
            assert card.title == curated
            with store.pool.connection() as conn:
                combined = conn.execute(
                    "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
                    (card_id,),
                ).fetchone()
            assert combined is not None and combined[0] == curated
            # ...while the eval the pass owns still landed on that same card.
            assert (card.meta or {}).get("capability")
            _, meta = _one_lead(store)
            assert meta[_LEAD_META_KEY]["state"] == "awaiting_decision"
        finally:
            _delete_groq_accounts(store)

    def test_skip_resolves_with_no_account(self, store: Any) -> None:
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: skip — not interesting")
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: None)
        _, meta = _one_lead(store)
        lead = meta[_LEAD_META_KEY]
        assert lead["state"] == "resolved"
        assert lead["resolution"] == "skipped"
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM llm_account WHERE vendor = 'groq'"
            ).fetchone()
        assert row is None

    def test_enable_answer_flips_the_account_on(self, store: Any) -> None:
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: pursue")
        try:
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            decision_id = meta[_LEAD_META_KEY]["decision_todo_id"]
            _answer(store, decision_id, "answer: enable — ordinals look sound")
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            lead = meta[_LEAD_META_KEY]
            assert lead["state"] == "resolved"
            assert lead["resolution"] == "enabled"
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT enabled FROM llm_account WHERE name = %s",
                    (lead["account"],),
                ).fetchone()
            assert row is not None
            assert row[0] is True
        finally:
            _delete_groq_accounts(store)

    def test_reject_answer_leaves_the_account_dark(self, store: Any) -> None:
        run_offer_discovery_pass(store, force=True, research_fn=lambda s: _REPORT)
        ref_id, _meta = _one_lead(store)
        _answer(store, ref_id, "answer: pursue")
        try:
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            decision_id = meta[_LEAD_META_KEY]["decision_todo_id"]
            _answer(store, decision_id, "answer: reject — do not enable")
            run_offer_discovery_pass(
                store,
                force=True,
                research_fn=lambda s: None,
                eval_dispatch_fn=_stub_eval_dispatch,
            )
            _, meta = _one_lead(store)
            lead = meta[_LEAD_META_KEY]
            assert lead["state"] == "resolved"
            assert lead["resolution"] == "rejected"
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT enabled, entitlement FROM llm_account WHERE name = %s",
                    (lead["account"],),
                ).fetchone()
            assert row is not None
            assert row[0] is False
            assert row[1]["qualification_status"] == "rejected"
        finally:
            _delete_groq_accounts(store)
