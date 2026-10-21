"""``_chase_llm._verify_supports_batch`` — the batched chase:verify call
(gr499038): several citations of ONE claim judged per dispatch, per-item
verdicts keyed by ITEM number, and a per-item fallback to the single call
whenever the batch reply misses or malforms an item. The router is faked
(``precis.workers._chase_llm.route``) — no dispatch runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from precis.workers._chase_llm import (
    VerifyItem,
    _parse_batch_verdicts,
    _verify_batch_prompt,
    _verify_item_block,
    _verify_item_kwargs,
    _verify_support_with_caveats,
    _verify_supports_batch,
)


class _Res:
    def __init__(
        self,
        data: Any = None,
        request_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        self.data = data
        self.text = ""
        self.error = error
        self.request_hash = request_hash


def _item(n: int, **kw: Any) -> VerifyItem:
    base: dict[str, Any] = dict(
        target_cite_key=f"pa{n}",
        target_chunk_ord=n,
        target_chunk_text=f"chunk text {n}",
    )
    return VerifyItem(**{**base, **kw})


def _verdict(supports: str = "yes") -> dict[str, Any]:
    return {
        "supports": supports,
        "support_reason": "r",
        "caveats": [],
        "same_setup": True,
        "contradicts": False,
        "cited_others": [],
        "terminal": True,
    }


def _batch_reply(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"verdicts": [{"item": i + 1, **v} for i, v in enumerate(verdicts)]}


def test_batch_of_n_returns_n_verdicts_in_order() -> None:
    items = [_item(1), _item(2), _item(3)]
    reply = _batch_reply([_verdict("yes"), _verdict("partial"), _verdict("no")])
    with patch("precis.workers._chase_llm.route", return_value=_Res(reply)) as r:
        out = _verify_supports_batch(claim="X", scope={}, items=items)
    assert r.call_count == 1  # one dispatch for the whole batch
    assert [(v or {})["supports"] for v in out] == ["yes", "partial", "no"]
    # the per-item "item" tag never leaks into a stored verdict
    assert all("item" not in (v or {}) for v in out)


def test_batch_prompt_labels_items_and_shares_claim_once() -> None:
    items = [_item(1, neighbours=["NB1"]), _item(2, source_kind="patent")]
    with patch(
        "precis.workers._chase_llm.route",
        return_value=_Res(_batch_reply([_verdict()] * 2)),
    ) as r:
        _verify_supports_batch(
            claim="X is 5 nm.", scope={}, items=items, claim_source_text="ORIGIN"
        )
    prompt = r.call_args[0][0].prompt
    assert prompt.count("CLAIM:\nX is 5 nm.") == 1  # claim side paid once
    assert prompt.count("ORIGIN") == 1
    assert "ITEM 1" in prompt and "ITEM 2" in prompt
    assert "chunk text 1" in prompt and "chunk text 2" in prompt
    assert "NB1" in prompt
    assert '"verdicts"' in prompt and '"item"' in prompt
    # the patent reading rules ride inside the patent item only
    assert "PATENT-SPECIFIC READING RULES" in prompt
    assert prompt.index("PATENT-SPECIFIC") > prompt.index("chunk text 2")


def test_missing_item_falls_back_to_a_single_call() -> None:
    items = [_item(1), _item(2), _item(3)]
    # reply covers items 1 and 3 only — item 2 is absent
    reply = {
        "verdicts": [
            {"item": 1, **_verdict("yes")},
            {"item": 3, **_verdict("no")},
        ]
    }
    calls: list[Any] = []

    def _route(req: Any) -> _Res:
        calls.append(req)
        if "ITEM 1" in req.prompt:
            return _Res(reply)
        # the fallback is a single-call prompt — no ITEM markers
        assert "ITEM " not in req.prompt
        return _Res(_verdict("partial"), request_hash="fb-hash")

    with patch("precis.workers._chase_llm.route", side_effect=_route):
        out = _verify_supports_batch(claim="X", scope={}, items=items)
    assert [(v or {})["supports"] for v in out] == ["yes", "partial", "no"]
    assert len(calls) == 2  # batch + exactly one fallback


def test_malformed_item_falls_back_never_guessed() -> None:
    items = [_item(1), _item(2)]
    reply = {
        "verdicts": [
            {"item": 1, "supports": "bogus"},  # out-of-enum: malformed
            {"item": 2, **_verdict("no")},
        ]
    }
    calls: list[Any] = []

    def _route(req: Any) -> _Res:
        calls.append(req)
        return _Res(reply) if "ITEM 1" in req.prompt else _Res(_verdict("yes"))

    with patch("precis.workers._chase_llm.route", side_effect=_route):
        out = _verify_supports_batch(claim="X", scope={}, items=items)
    # item 1's verdict came from the fallback single call, not the bad enum
    assert [(v or {})["supports"] for v in out] == ["yes", "no"]
    assert len(calls) == 2


def test_unparseable_batch_reply_falls_back_per_item() -> None:
    items = [_item(1), _item(2)]
    calls: list[Any] = []

    def _route(req: Any) -> _Res:
        calls.append(req)
        if "ITEM 1" in req.prompt:
            return _Res({"not": "the contract"})
        return _Res(_verdict("no"))

    with patch("precis.workers._chase_llm.route", side_effect=_route):
        out = _verify_supports_batch(claim="X", scope={}, items=items)
    assert [(v or {})["supports"] for v in out] == ["no", "no"]
    assert len(calls) == 3  # batch + two fallbacks


def test_router_error_behaves_like_today() -> None:
    """A batch dispatch error = today's N independent single calls: each
    may succeed or fail on its own, and the mixed result stands."""
    items = [_item(1), _item(2), _item(3)]
    calls: list[Any] = []

    def _route(req: Any) -> _Res:
        calls.append(req)
        if "ITEM 1" in req.prompt:
            return _Res(error="upstream timeout")
        # per-item singles: item 2's call fails, the rest succeed
        return (
            _Res(error="boom")
            if "chunk text 2" in req.prompt
            else _Res(_verdict("yes"))
        )

    with patch("precis.workers._chase_llm.route", side_effect=_route):
        out = _verify_supports_batch(claim="X", scope={}, items=items)
    assert len(calls) == 4  # batch + three singles (today: three calls)
    assert [None if v is None else (v or {})["supports"] for v in out] == [
        "yes",
        None,
        "yes",
    ]


def test_batch_of_one_is_the_single_call() -> None:
    with patch(
        "precis.workers._chase_llm.route", return_value=_Res(_verdict("no"))
    ) as r:
        out = _verify_supports_batch(claim="X", scope={}, items=[_item(1)])
    assert r.call_count == 1
    assert "ITEM 1" not in r.call_args[0][0].prompt  # the single prompt
    assert out == [_verdict("no")]
    assert _verify_supports_batch(claim="X", scope={}, items=[]) == []


def test_batch_verdicts_carry_the_batch_request_hash() -> None:
    items = [_item(1), _item(2)]
    reply = _batch_reply([_verdict("yes"), _verdict("no")])

    def _route(req: Any) -> _Res:
        # item 2 missing from the batch reply → its fallback hash wins
        if "ITEM 1" in req.prompt:
            return _Res({"verdicts": [{"item": 1, **_verdict("yes")}]}, "batch-hash")
        return _Res(_verdict("no"), request_hash="fb-hash")

    with patch("precis.workers._chase_llm.route", side_effect=_route):
        out = _verify_supports_batch(
            claim="X", scope={}, items=items, with_request_hash=True
        )
    assert (out[0] or {})["_llm_request_hash"] == "batch-hash"
    assert (out[1] or {})["_llm_request_hash"] == "fb-hash"


def test_batch_request_pins_a_scaling_completion_cap() -> None:
    items = [_item(1), _item(2), _item(3), _item(4)]
    with patch(
        "precis.workers._chase_llm.route",
        return_value=_Res(_batch_reply([_verdict()] * 4)),
    ) as r:
        _verify_supports_batch(claim="X", scope={}, items=items)
    req = r.call_args[0][0]
    assert req.max_tokens is not None and req.max_tokens > 4 * 1500
    assert req.source == "chase:verify"


def test_parse_batch_verdicts_accepts_a_map_and_rejects_bad_items() -> None:
    assert _parse_batch_verdicts({"verdicts": {"1": _verdict("yes")}}, 2) == [
        _verdict("yes"),
        None,
    ]
    assert _parse_batch_verdicts({"verdicts": {"3": _verdict("yes")}}, 2) == [
        None,
        None,
    ]  # out of range
    assert _parse_batch_verdicts({"verdicts": [{"item": "x"}]}, 1) == [None]
    assert _parse_batch_verdicts(None, 1) == [None]
    assert _parse_batch_verdicts({"verdicts": [{"item": 1, "supports": "no"}]}, 1) == [
        {"supports": "no"}
    ]


def test_item_block_is_the_single_prompts_item_span() -> None:
    """``_verify_item_block`` output appears verbatim in the single prompt —
    the batch prompt's ITEM bodies are the same text a single call shows."""
    ident = _item(1).source_identity
    block = _verify_item_block(
        source_kind="paper",
        cite_key="pa1",
        chunk_ord=1,
        chunk_text="chunk {x}",
        section_path="S {x}",
        neighbours=["n {x}"],
        source_identity=ident,
    )
    with patch(
        "precis.workers._chase_llm.route", return_value=_Res({"supports": "no"})
    ) as r:
        _verify_support_with_caveats(
            claim="c",
            scope={},
            target_cite_key="pa1",
            target_chunk_ord=1,
            target_chunk_text="chunk {x}",
            section_path="S {x}",
            neighbours=["n {x}"],
            source_identity=ident,
        )
    single_prompt = r.call_args[0][0].prompt
    assert block in single_prompt  # braces render literally, block verbatim
    # and the batch prompt embeds the same block under an ITEM marker
    batch = _verify_batch_prompt("CB", [block])
    assert f"ITEM 1\n\n{block}" in batch


def test_item_kwargs_map_onto_the_single_call() -> None:
    item = _item(5)
    kw = _verify_item_kwargs(item)
    with patch(
        "precis.workers._chase_llm.route", return_value=_Res(_verdict("yes"))
    ) as r:
        _verify_support_with_caveats(claim="c", scope={}, **kw)
    assert r.call_count == 1
    assert "chunk text 5" in r.call_args[0][0].prompt
