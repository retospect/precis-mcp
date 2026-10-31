"""Tests for ``workers/card_describe.py`` (gr497688 S1).

DB-backed via the ``store``/``hub`` fixtures with an injected fake LLM
client (``.complete`` shape only) — no network, no real model, and the
worker never touches an embedder.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.cad import CadHandler
from precis.handlers.pcb import PcbHandler
from precis.handlers.structure import StructureHandler
from precis.workers.card_describe import run_card_describe_pass

_PD = (
    '{"cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [true, true, false]},'
    ' "ops": ['
    '{"op": "add_atom", "element": "Pd", "frac": [0.1, 0.1, 0.1]},'
    '{"op": "add_atom", "element": "Pd", "frac": [0.3, 0.1, 0.1]},'
    '{"op": "add_atom", "element": "O", "frac": [0.2, 0.2, 0.2]}'
    "]}"
)

_DESC = (
    "A palladium oxide slab with in-plane periodicity, likely a model "
    "catalyst surface for oxidation chemistry studies."
)


class _FakeClient:
    """Records calls; answers with a fixed description wrapped as the
    ``{"description": …}`` JSON object the contract demands (gr543828 —
    a compliant reply, so the describe path parses it). A test that
    needs raw wire text — the parse-shape cases — uses ``_RawClient``."""

    def __init__(self, text: str = _DESC) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    def _result(self) -> Any:
        return SimpleNamespace(
            text=json.dumps({"description": self.text}),
            data={"description": self.text},
            model="fake-sonnet",
            total_tokens=5,
        )

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return self._result()


class _RawClient(_FakeClient):
    """Answers with the reply text VERBATIM — a fenced block, bare prose,
    truncated JSON, a wrong key — so the parse-shape cases see the raw
    wire text, not a wrapped description."""

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self.text, model="fake-sonnet")


def _desc_chunks(store: Any, ref_id: int) -> list[tuple[int, str, dict]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, meta FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'card_description'",
            (ref_id,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2])) for r in rows]


def _put_structure(hub: Any, slug: str = "pdo") -> int:
    StructureHandler(hub=hub).put(id=slug, text=_PD)
    ref = hub.store.get_ref(kind="structure", id=slug)
    assert ref is not None
    return int(ref.id)


def test_missing_description_gets_exactly_one_chunk(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    client = _FakeClient()

    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1
    assert chunks[0][1] == _DESC
    meta = chunks[0][2]
    assert meta["generator"] == "card_describe"
    assert meta["model"] == "fake-sonnet"
    assert meta["source_sha"]


def test_rerun_is_a_noop(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    client = _FakeClient()
    run_card_describe_pass(store, client=client, batch_size=10)

    again = _FakeClient()
    r = run_card_describe_pass(store, client=again, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert again.calls == []
    assert len(_desc_chunks(store, ref_id)) == 1


def test_edited_card_regenerates_one_chunk(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    first = _desc_chunks(store, ref_id)[0]

    StructureHandler(hub=hub).edit(
        id="pdo", ops=[{"op": "add_atom", "element": "N", "frac": [0.4, 0.4, 0.1]}]
    )
    client2 = _FakeClient(
        "A nitrogen-doped palladium oxide slab, plausibly a doped "
        "catalyst surface for selective oxidation."
    )
    r = run_card_describe_pass(store, client=client2, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1  # DELETE+INSERT replaced, never a second row
    assert chunks[0][0] != first[0]  # replaced, not updated in place
    assert "nitrogen-doped" in chunks[0][1]
    assert chunks[0][2]["source_sha"] != first[2]["source_sha"]


def test_worker_never_embeds(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)

    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM chunk_embeddings ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id "
            "WHERE c.ref_id = %s AND c.chunk_kind = 'card_description'",
            (ref_id,),
        ).fetchone()
    assert n is not None and n[0] == 0


def test_search_finds_ref_by_description_only_phrase(store: Any, hub: Any) -> None:
    _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)

    # 'catalyst surface' appears only in the generated description, not
    # in the card text — a lexical hit means card_description is in
    # structure search's card_kinds.
    res = StructureHandler(hub=hub).search(q="catalyst", mode="lexical")
    assert "pdo" in res.body


def test_pre_watermark_card_is_described_after_edit(store: Any, hub: Any) -> None:
    """An old (pre-watermark) structure that is edited re-mints its
    ``card_combined`` after the watermark — DELETE+INSERT gives it a new
    created_at — so the edit claims it and it gets exactly one
    description (gr497688 ruling 3)."""
    ref_id = _put_structure(hub)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = "
            "(SELECT created_at FROM chunk_kinds WHERE slug = 'card_description')"
            " - interval '1 second' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()

    # Untouched old card: below the watermark, never claimed.
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0
    assert _desc_chunks(store, ref_id) == []

    # The edit re-mints card_combined (new created_at) -> claimed.
    StructureHandler(hub=hub).edit(
        id="pdo", ops=[{"op": "add_atom", "element": "N", "frac": [0.4, 0.4, 0.1]}]
    )
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1


def test_card_without_description_chunk_is_skipped_after_watermark(
    store: Any,
) -> None:
    """Refs whose card predates the `card_description` kind registration
    are S4's backfill, not this worker's — simulate by rewriting the
    card row's created_at to before the kind's."""
    with store.pool.connection() as conn:
        ref_id = conn.execute(
            "INSERT INTO refs (kind, set_by, title) VALUES ('structure', 'system', 'old')"
            " RETURNING ref_id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'agent', -1, 'card_combined', 'old card')",
            (ref_id,),
        )
        conn.execute(
            "UPDATE chunks SET created_at = "
            "(SELECT created_at FROM chunk_kinds WHERE slug = 'card_description')"
            " - interval '1 second' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()

    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0
    assert _desc_chunks(store, ref_id) == []


# ── S2: cad + pcb ─────────────────────────────────────────────────────

_FLANGE = """
component flange
plate     add  cyl:r25mmh8mm
hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm
bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm
"""

_PCB_DESIGN = {
    "components": [
        {"refdes": "U1", "label": "ewod driver", "pins": [{"name": "1"}]},
        {"refdes": "J1", "label": "conn", "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "VHV"}, {"name": "GND"}],
}


def _put_cad(store: Any) -> int:
    CadHandler(hub=Hub(store=store)).put(id="flange", text=_FLANGE)
    ref = store.get_ref(kind="cad", id="flange")
    assert ref is not None
    return int(ref.id)


def _put_pcb(store: Any, slug: str = "b1") -> int:
    PcbHandler(hub=Hub(store=store)).put(id=slug, args=dict(_PCB_DESIGN))
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_cad_description_lifecycle_and_search(store: Any) -> None:
    ref_id = _put_cad(store)
    client = _FakeClient(
        "A flanged hub plate with a central bore and a six-bolt circle, "
        "plausibly a shaft coupling or motor-mount interface."
    )
    r = run_card_describe_pass(store, client=client, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1

    # re-run is a no-op
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0

    # description-only phrase -> lexical hit via card_kinds
    res = CadHandler(hub=Hub(store=store)).search(q="coupling", mode="lexical")
    assert "flange" in res.body

    # edit (a re-put that changes the design) re-mints the card -> regen
    CadHandler(hub=Hub(store=store)).put(id="flange", text="plate add cyl:r10mmh5mm")
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 1
    assert len(_desc_chunks(store, ref_id)) == 1


def test_pcb_description_lifecycle_and_search(store: Any) -> None:
    ref_id = _put_pcb(store)
    client = _FakeClient(
        "A two-part board pairing an EWOD high-voltage driver with a "
        "connector, plausibly an electrowetting electrode controller."
    )
    r = run_card_describe_pass(store, client=client, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1

    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0

    res = PcbHandler(hub=Hub(store=store)).search(q="electrowetting", mode="lexical")
    assert "b1" in res.body

    # a put that changes the design re-mints the card -> regen
    PcbHandler(hub=Hub(store=store)).put(
        id="b1",
        args={
            "components": [
                {"refdes": "U1", "label": "ewod driver", "pins": [{"name": "1"}]},
                {"refdes": "J1", "label": "conn", "pins": [{"name": "1"}]},
                {"refdes": "C1", "label": "100nF", "pins": [{"name": "1"}]},
            ],
            "nets": [{"name": "VHV"}, {"name": "GND"}],
        },
    )
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 1
    assert len(_desc_chunks(store, ref_id)) == 1


def test_claude_p_json_reply_is_parsed(store: Any, hub: Any) -> None:
    """gr500371: CLAUDE_P only accepts a JSON block, surfaced as res.data;
    the description is its `description` field, not the raw envelope."""
    ref_id = _put_structure(hub)
    desc = "A PdO slab, plausibly a catalyst surface model for CO oxidation."

    class _JsonClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            return SimpleNamespace(
                text=json.dumps({"description": desc}),
                data={"description": desc},
                model="claude-sonnet-5",
            )

    client = _JsonClient()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert _desc_chunks(store, ref_id)[0][1] == desc
    assert "JSON" in client.calls[0][0]["content"]


def test_truncated_json_reply_fails_not_stored(store: Any, hub: Any) -> None:
    """A JSON-looking reply with no usable `description` (truncated, wrong
    shape) is a per-item failure to retry, never stored as the card text."""
    ref_id = _put_structure(hub)

    class _TruncClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            return SimpleNamespace(
                text='{"description": "A PdO slab, plausibly a cat',
                data=None,
                model="local",
            )

    r = run_card_describe_pass(store, client=_TruncClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert _desc_chunks(store, ref_id) == []


def test_failing_ref_is_braked_by_attempt_lease(store: Any, hub: Any) -> None:
    """gr508688: a persistently-failing ref (a reply that parses as JSON
    but never carries ``description`` — melchior's card_describe loop)
    brakes on the claim-time attempt lease: claimed at most once per
    cooldown, re-billing a dispatch every pass was the pre-fix shape."""
    ref_id = _put_structure(hub)

    class _BadShapeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            return SimpleNamespace(
                text='{"narrative": "..."}',
                data={"narrative": "..."},
                model="local",
            )

    r = run_card_describe_pass(store, client=_BadShapeClient(), batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert _desc_chunks(store, ref_id) == []

    # Leased: the next pass does not re-claim (no re-bill, no ERROR).
    again = _BadShapeClient()
    r = run_card_describe_pass(store, client=again, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert again.calls == []

    # Cooldown expiry self-heals (simulated, mirroring the bib_parse and
    # axis_pass lease tests — never waiting out the real TTL).
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 minute' "
            "WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1


# ── gr523669: the ERROR line itself names the failure reason ────────────


def test_failed_ref_error_line_carries_class_and_reason(
    store: Any, hub: Any, caplog: Any
) -> None:
    """gr523669: prod showed bare ``card_describe: ref_id=N failed`` lines —
    ``log.exception``'s reason lived only in exc_info, which the shipped
    log line drops. Assert on ``record.getMessage()`` (the line itself),
    never ``caplog.text`` — that renders the traceback and would pass
    without the fix."""
    import logging

    ref_id = _put_structure(hub)

    class _BadShapeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            return SimpleNamespace(
                text='{"narrative": "..."}',
                data={"narrative": "..."},
                model="local",
            )

    with caplog.at_level(logging.ERROR, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=_BadShapeClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    lines = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert lines == [
        f"card_describe: ref_id={ref_id} failed: ValueError: "
        "card_describe reply carried no usable 'description' field; "
        'reply={"narrative": "..."}; reply_chars=20'
    ]


def test_failure_line_carries_reply_chars_and_stop_reason(
    store: Any, hub: Any, caplog: Any
) -> None:
    """gr523669: the failure line also carries the reply's full length
    (the excerpt is capped at 200 chars) and the wire's finish/stop
    reason when the client exposes one — an output-cap cut reads as
    ``stop=length`` straight off the line."""
    import logging

    ref_id = _put_structure(hub)

    class _StopClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            return SimpleNamespace(
                text='{"narrative": "..."}',
                data=None,
                model="local",
                stop_reason="length",
            )

    with caplog.at_level(logging.ERROR, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=_StopClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    lines = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert lines == [
        f"card_describe: ref_id={ref_id} failed: ValueError: "
        "card_describe reply carried no usable 'description' field; "
        'reply={"narrative": "..."}; reply_chars=20 stop=length'
    ]


def test_error_line_bounds_exception_text_to_300_chars(
    store: Any, hub: Any, caplog: Any
) -> None:
    """The failure line carries at most 300 chars of exception text; a
    1000-char message is cut, the class name and ref_id stay."""
    import logging

    ref_id = _put_structure(hub)

    class _HugeErrClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            raise ValueError("x" * 1000)

    with caplog.at_level(logging.ERROR, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=_HugeErrClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    lines = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(lines) == 1
    assert (
        lines[0] == f"card_describe: ref_id={ref_id} failed: ValueError: " + "x" * 300
    )


def test_dispatch_failure_error_line_carries_class_and_reason(
    store: Any, hub: Any, caplog: Any
) -> None:
    """gr523669: same shape on the non-paused ``DispatchError`` branch —
    a genuine route failure must name itself too."""
    import logging

    from precis.utils.llm.router import DispatchError

    ref_id = _put_structure(hub)

    class _BoomClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            raise DispatchError("all BIG rungs declined", paused=False)

    with caplog.at_level(logging.ERROR, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=_BoomClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    lines = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert lines == [
        f"card_describe: ref_id={ref_id} dispatch failed: "
        "DispatchError: all BIG rungs declined"
    ]


#: The prod gr523669 sample verbatim: the model DID emit a
#: ``description`` key — an output cap cut the reply mid-string, so the
#: strict JSON parse rejected it. Salvage recovers the value to the cut
#: and trims at the last word boundary (the fragment has no sentence
#: break to cut at).
_TRUNC_MID_STRING_REPLY = (
    '{"description": "This 38-atom structure, labeled O~n2o, represents '
    "an intermediate along computational reaction pathway "
    "q164903cand-69ee52b673-rx-10eb01e369, generated within an "
    "autocatalytic pathway"
)
_TRUNC_MID_STRING_STORED = (
    "This 38-atom structure, labeled O~n2o, represents an intermediate "
    "along computational reaction pathway "
    "q164903cand-69ee52b673-rx-10eb01e369, generated within an "
    "autocatalytic"
)


@pytest.mark.parametrize(
    ("reply", "stored"),
    [
        pytest.param(
            '{"description": "A PdO slab, plausibly a cat',
            None,
            id="truncated-json-too-short-to-salvage",
        ),
        pytest.param(
            '{"description": ',
            None,
            id="truncated-before-value",
        ),
        pytest.param(
            '{"narrative": "A PdO slab, plausibly a catalyst surface model."}',
            None,
            id="wrong-key",
        ),
        pytest.param(_DESC, None, id="plain-prose-fails"),
        pytest.param(
            f'```json\n{{"description": "{_DESC}"}}\n```',
            _DESC,
            id="fenced-json",
        ),
        pytest.param(
            f'The description follows.\n\n{{"description": "{_DESC}"}}',
            _DESC,
            id="prose-before-json",
        ),
        pytest.param(
            _TRUNC_MID_STRING_REPLY,
            _TRUNC_MID_STRING_STORED,
            id="truncated-mid-string-salvage",
        ),
        pytest.param(
            f'{{"description": "{_DESC}", "confidence": 0.9, "lang": "en"}}',
            _DESC,
            id="extra-keys",
        ),
        pytest.param(f"```\n{_DESC}\n```", None, id="fenced-prose-fails"),
        pytest.param(
            f'Here is the description.\n\n```json\n{{"description": "{_DESC}"}}\n```',
            _DESC,
            id="prose-then-fenced-json",
        ),
        pytest.param(
            '{"description": "A palladium oxide slab with in-plane\n'
            'periodicity, likely a model catalyst surface."}',
            "A palladium oxide slab with in-plane\nperiodicity, likely a "
            "model catalyst surface.",
            id="raw-newline-in-json",
        ),
    ],
)
def test_reply_shapes_parse_or_fail_with_reason(
    store: Any, hub: Any, caplog: Any, reply: str, stored: str | None
) -> None:
    """gr523669: reply shapes the prod failures hid behind a bare reason.
    The incidental-strictness shapes (a wrapping fence, prose before the
    object, extra keys, a raw control char inside the string) parse, and
    a mid-string truncation salvages the description to the last
    sentence/word; a wrong key, a reply truncated before the value, a
    below-minimum fragment, and bare prose (gr543828 — no longer stored
    as the description) still fail — and the ERROR line carries the
    reply's head."""
    import logging

    ref_id = _put_structure(hub)

    with caplog.at_level(logging.ERROR, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=_RawClient(reply), batch_size=10)

    if stored is not None:
        assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
        assert _desc_chunks(store, ref_id)[0][1] == stored
        return

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert _desc_chunks(store, ref_id) == []
    lines = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(lines) == 1
    assert lines[0].startswith(
        f"card_describe: ref_id={ref_id} failed: ValueError: "
        "card_describe reply carried no usable 'description' field; reply="
    )
    # the reason names the reply's head, flattened to one line
    assert " ".join(reply.split())[:40] in lines[0]
    assert "\n" not in lines[0]


def test_salvaged_description_logs_an_info_line(
    store: Any, hub: Any, caplog: Any
) -> None:
    """gr523669: a truncation the salvage recovered is logged at info —
    ref id + the salvage reason + the reply telemetry — so prod can
    count recoveries instead of only counting failures."""
    import logging

    ref_id = _put_structure(hub)

    with caplog.at_level(logging.INFO, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(
            store, client=_RawClient(_TRUNC_MID_STRING_REPLY), batch_size=10
        )

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert _desc_chunks(store, ref_id)[0][1] == _TRUNC_MID_STRING_STORED
    lines = [
        rec.getMessage() for rec in caplog.records if "salvaged" in rec.getMessage()
    ]
    assert lines == [
        f"card_describe: ref_id={ref_id} description salvaged "
        f"(cut-mid-string; reply_chars={len(_TRUNC_MID_STRING_REPLY)})"
    ]


def test_consecutive_failures_bench_the_ref(store: Any, hub: Any, caplog: Any) -> None:
    """gr523669: after ``_PARSE_FAIL_BENCH`` consecutive failures the lease
    is re-stamped under the bench window — the ref stops re-claiming for a
    day instead of re-billing an LLM call every cooldown, and the bench is
    logged once (on the transition, not on every re-bench)."""
    import logging

    import precis.workers.card_describe as cd
    from precis.workers import ref_lease

    ref_id = _put_structure(hub)
    bad_reply = '{"narrative": "a shape the parser cannot use"}'

    def _expire_lease() -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags SET expires_at = now() - interval '1 minute' "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            conn.commit()

    def _lease() -> tuple[str, Any]:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT t.value, rt.expires_at - now() FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s AND t.namespace = %s",
                (ref_id, ref_lease.attempt_ns(cd._LEASE_NS)),
            ).fetchone()
        assert row is not None
        return str(row[0]), row[1]

    def _fail_pass() -> dict[str, Any]:
        r = run_card_describe_pass(store, client=_RawClient(bad_reply), batch_size=10)
        assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
        assert _desc_chunks(store, ref_id) == []
        return r

    with caplog.at_level(logging.WARNING, logger="precis.workers.card_describe"):
        # attempts 1..N-1: fail, burn the cooldown, re-claim, fail again —
        # the per-attempt lease only ever brakes for its own cooldown.
        for _ in range(cd._PARSE_FAIL_BENCH - 1):
            _fail_pass()
            _expire_lease()
        # attempt N: fails AND benches — the lease jumps to the long window.
        _fail_pass()

    value, ttl = _lease()
    assert value == str(cd._PARSE_FAIL_BENCH)
    assert ttl > timedelta(minutes=ref_lease.ATTEMPT_COOLDOWN_MIN)

    assert [
        rec.getMessage() for rec in caplog.records if "benched" in rec.getMessage()
    ] == [
        f"card_describe: ref_id={ref_id} benched after "
        f"{cd._PARSE_FAIL_BENCH} consecutive failures; "
        f"backing off {cd._BENCH_COOLDOWN_MIN} min"
    ]

    # Benched: the next pass does not re-claim — no re-bill, no ERROR.
    again = _RawClient(bad_reply)
    r = run_card_describe_pass(store, client=again, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert again.calls == []

    # The bench window itself expiring re-claims exactly once; another
    # failure re-benches at the next ordinal without a second bench line.
    caplog.clear()
    _expire_lease()
    _fail_pass()
    value, ttl = _lease()
    assert value == str(cd._PARSE_FAIL_BENCH + 1)
    assert ttl > timedelta(minutes=ref_lease.ATTEMPT_COOLDOWN_MIN)
    assert [rec for rec in caplog.records if "benched" in rec.getMessage()] == []


# ── gr500371: emptied chain defers the batch, log names cli_unavailable ──


def test_cli_unavailable_chain_defers_batch(
    store: Any, hub: Any, caplog: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """A claude-less host: the BIG chain's only rung (claude_p) drops at the
    availability gate → DispatchError(paused, cli_unavailable) → the pass
    defers with zero failed rows and the log line names cli_unavailable."""
    import logging

    from precis.utils.llm.router import DispatchClient, Tier

    ref_id = _put_structure(hub)
    empty = tmp_path / "no-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("PRECIS_CLAUDE_BIN", raising=False)
    # Force the ANTHROPIC backend so BIG resolves the claude_p rung.
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)

    client = DispatchClient(tier=Tier.BIG)
    with caplog.at_level(logging.WARNING, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert r.get("deferred") == "cli_unavailable"
    assert _desc_chunks(store, ref_id) == []
    assert "cli_unavailable" in caplog.text


# ── gr497688 S3: figure vision descriptions ─────────────────────────────

_FIGURE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80">'
    '<rect x="10" y="10" width="80" height="50" fill="#cde"/>'
    '<circle cx="50" cy="35" r="12" fill="#a33"/></svg>'
)
_FIGURE_SVG_2 = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80">'
    '<rect x="10" y="10" width="80" height="50" fill="#cde"/>'
    '<ellipse cx="50" cy="35" rx="20" ry="10" fill="#3a3"/></svg>'
)


def _put_figure(hub: Any, slug: str = "fig1", svg: str = _FIGURE_SVG) -> int:
    from precis.handlers.figure import FigureHandler

    FigureHandler(hub=hub).put(id=slug, title="Band alignment sketch", text=svg)
    ref = hub.store.get_ref(kind="figure", id=slug)
    assert ref is not None
    return int(ref.id)


def _fake_raster(monkeypatch: Any) -> None:
    import precis.workers.card_describe as cd

    monkeypatch.setattr(cd, "_svg_to_png", lambda svg: b"\x89PNG-fake")


def test_figure_gets_vision_description(store: Any, hub: Any, monkeypatch: Any) -> None:
    import base64 as _b64

    ref_id = _put_figure(hub)
    _fake_raster(monkeypatch)
    client = _FakeClient(
        "A band-alignment sketch: a filled rectangle with a red disc "
        "marking the alignment point, plausibly a device-stack schematic."
    )

    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1
    assert "band-alignment" in chunks[0][1]
    # The request carried an image block, not the raw SVG text.
    user = [m for m in client.calls[0] if m["role"] == "user"][0]
    blocks = user["content"]
    assert isinstance(blocks, list)
    img = [b for b in blocks if b.get("type") == "image"]
    assert len(img) == 1
    assert img[0]["source"]["media_type"] == "image/png"
    assert _b64.b64decode(img[0]["source"]["data"]) == b"\x89PNG-fake"
    text = [b for b in blocks if b.get("type") == "text"][0]["text"]
    assert "Band alignment sketch" in text


def test_figure_edit_replaces_description(
    store: Any, hub: Any, monkeypatch: Any
) -> None:
    from precis.handlers.figure import FigureHandler

    ref_id = _put_figure(hub)
    _fake_raster(monkeypatch)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    first = _desc_chunks(store, ref_id)[0]

    FigureHandler(hub=hub).edit(id="fig1", text=_FIGURE_SVG_2)
    client2 = _FakeClient(
        "A band-alignment sketch with an ellipse overlay, plausibly a "
        "revised device-stack schematic."
    )
    r = run_card_describe_pass(store, client=client2, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1
    assert chunks[0][0] != first[0]
    assert chunks[0][2]["source_sha"] != first[2]["source_sha"]


def test_empty_figure_canvas_not_claimed(store: Any, hub: Any) -> None:
    """A birth canvas (no drawable element) is never described."""
    ref_id = _put_figure(
        hub,
        slug="blank",
        svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
    )
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0
    assert _desc_chunks(store, ref_id) == []


# ── gr497688 S4: daily budget cap + dry-run counts ───────────────────────


def test_daily_budget_cap_defers_pass(store: Any, hub: Any, monkeypatch: Any) -> None:
    ref_id = _put_structure(hub)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (source, tier, cost_usd, ts) "
            "VALUES ('card_describe', 'big', 30.00, now())"
        )
        conn.commit()
    monkeypatch.setenv("PRECIS_CARD_DESCRIBE_DAILY_USD", "25")

    client = _FakeClient()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert r.get("deferred") == "budget"
    assert client.calls == []
    assert _desc_chunks(store, ref_id) == []


def test_dry_run_counts_per_kind_and_writes_nothing(
    store: Any, hub: Any, monkeypatch: Any
) -> None:
    from precis.workers.card_describe import claimable_counts

    _put_structure(hub)
    _put_figure(hub)
    with store.pool.connection() as conn:
        counts = claimable_counts(conn)
    assert counts.get("structure") == 1
    assert counts.get("figure") == 1
    # Nothing described, nothing written.
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM chunks WHERE chunk_kind = 'card_description'"
        ).fetchone()
    assert n is not None and n[0] == 0


# ── gr524231: no transaction spans an LLM call ──────────────────────────


def test_llm_call_runs_with_no_lock_or_open_tx(store: Any, hub: Any) -> None:
    """gr524231: the pass used to hold its single-runner advisory xact
    lock for the whole pass — a dedicated backend sat idle-in-transaction
    across every LLM call (the prod 13:20Z block, other lanes queued
    behind it). The lock now scopes only the decide-and-claim
    transaction, so inside ``client.complete`` this pass's advisory key
    has no granted pg_locks row and no backend sits idle-in-transaction."""
    import precis.workers.card_describe as cd

    ref_id = _put_structure(hub)
    # pg_locks splits a one-int8 advisory key into two oid halves; compare
    # the halves separately (the reconstructed 64-bit pattern doesn't fit
    # a signed bigint).
    key64 = cd._LOCK_KEY & 0xFFFFFFFFFFFFFFFF
    key_hi, key_lo = key64 >> 32, key64 & 0xFFFFFFFF

    class _ProbeClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.held: list[int] = []
            self.idle_tx: list[int] = []

        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            with store.pool.connection() as conn:
                self.held.append(
                    int(
                        conn.execute(
                            "SELECT count(*) FROM pg_locks "
                            "WHERE locktype = 'advisory' AND granted "
                            "AND classid = %s AND objid = %s",
                            (key_hi, key_lo),
                        ).fetchone()[0]
                    )
                )
                self.idle_tx.append(
                    int(
                        conn.execute(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND state = 'idle in transaction' "
                            "AND pid <> pg_backend_pid()"
                        ).fetchone()[0]
                    )
                )
            return self._result()

    client = _ProbeClient()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert client.held == [0]
    assert client.idle_tx == [0]
    assert len(_desc_chunks(store, ref_id)) == 1


def test_concurrent_passes_never_describe_the_same_ref(store: Any, hub: Any) -> None:
    """gr524231: with the lock claim-scoped, two overlapping passes both
    run — per-ref exclusion is now the attempt lease, so both make
    progress yet never describe the same ref. ``batch_size=1`` forces
    each pass onto one ref; the barrier inside ``complete`` forces the
    two LLM phases to overlap (under the old whole-pass hold the second
    pass skipped at the lock and the barrier starved)."""
    import threading

    _put_structure(hub, slug="cd-a")
    _put_structure(hub, slug="cd-b")
    barrier = threading.Barrier(2)
    in_llm = threading.Event()

    class _SyncClient(_FakeClient):
        def __init__(self, text: str = _DESC, *, signal: bool = False) -> None:
            super().__init__(text)
            self.signal = signal

        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            if self.signal:
                in_llm.set()  # past the claim — the lock is already gone
            barrier.wait(timeout=15)
            return self._result()

    clients = [_SyncClient(signal=True), _SyncClient()]
    results: list[dict[str, Any]] = []

    def _run(i: int) -> None:
        results.append(run_card_describe_pass(store, client=clients[i], batch_size=1))

    # Pass 1 first; pass 2 starts only once pass 1 is parked inside its
    # LLM call — its claim then runs while pass 1 holds nothing, so it
    # must claim the OTHER ref via the lease, not skip at the lock.
    t1 = threading.Thread(target=_run, args=(0,), daemon=True)
    t1.start()
    assert in_llm.wait(timeout=15)
    t2 = threading.Thread(target=_run, args=(1,), daemon=True)
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive()

    # Each pass described exactly one ref, and the two refs differ — the
    # lease, not the pass lock, kept them apart.
    assert sum(len(c.calls) for c in clients) == 2
    described = {
        str(c.calls[0][1]["content"]).split(":", 1)[1].split("\n")[0].strip()
        for c in clients
        if c.calls
    }
    assert described == {"cd-a", "cd-b"}
    assert sorted(int(r["ok"]) for r in results) == [1, 1]


def test_lease_renews_mid_batch_so_second_pass_cannot_reclaim(
    store: Any, hub: Any
) -> None:
    """gr524231 review: the claim stamp brakes only for
    ``ATTEMPT_COOLDOWN_MIN`` — a batch whose earlier describes outlast
    it leaves later refs claimable to a second host, which re-bills them.
    The pass now re-stamps each ref's lease (same ordinal, fresh TTL)
    just before its LLM call. Simulate the cooldown passing during the
    first describe; inside the second describe — the ref's lease just
    renewed — a whole second pass must find nothing claimable."""
    _put_structure(hub, slug="cd-a")
    ref_b = _put_structure(hub, slug="cd-b")
    inner_results: list[dict[str, Any]] = []

    class _ExpireThenRerun(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            if len(self.calls) == 1:
                # The cooldown passing mid-batch: every claim stamp ages
                # out while the batch is still working.
                with store.pool.connection() as conn:
                    conn.execute(
                        "UPDATE ref_tags SET expires_at = now() - interval '1 minute'"
                    )
                    conn.commit()
            elif len(self.calls) == 2:
                # cd-b's lease was just renewed and cd-a's chunk is
                # written — a whole second pass must claim nothing.
                inner = _FakeClient(
                    "A second host re-describing an already-billed ref."
                )
                inner_results.append(
                    run_card_describe_pass(store, client=inner, batch_size=10)
                )
            return self._result()

    r = run_card_describe_pass(store, client=_ExpireThenRerun(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (2, 2, 0)
    assert inner_results == [{"claimed": 0, "ok": 0, "failed": 0}]
    # cd-b's single description is the first pass's write, not a re-bill.
    assert [c[1] for c in _desc_chunks(store, ref_b)] == [_DESC]


# ── gr543828: chat-agent replies buy ONE nudge retry, never a loop ─────

#: The three real prod reply heads (gr543828): the ``claude -p`` rung
#: answering as a chat agent — asking for the file it thinks is missing —
#: instead of returning the JSON object the contract demands.
_CHAT_AGENT_HEADS = [
    "I don't see an actual structure file attached—just the identifier "
    "and description. Let me check if there's a related file in the "
    "working directory.",
    "I don't have any prior context about this structure — no file has "
    "been provided or shared in this conversation, and I don't have "
    "access to a database of named atomistic structures…",
    "I don't see an actual structure file or a specific question "
    "attached—just the label/card text. Could you clarify what you'd "
    "like me to do here?",
]


def _chat_then_json(head: str) -> _FakeClient:
    """A client that answers the first dispatch with a chat-agent prose
    head, then — nudged — returns the JSON object."""

    class _ChatThenJson(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            if len(self.calls) == 1:
                return SimpleNamespace(text=head, model="claude-sonnet-5")
            return SimpleNamespace(
                text=json.dumps({"description": _DESC}),
                data={"description": _DESC},
                model="claude-sonnet-5",
            )

    return _ChatThenJson()


@pytest.mark.parametrize("head", _CHAT_AGENT_HEADS)
def test_chat_agent_reply_retries_once_then_parses(
    store: Any, hub: Any, head: str
) -> None:
    """Each prod head parses as unparseable and triggers exactly one
    retry — the second dispatch carries the JSON-only nudge as a new
    user turn, and its JSON reply is stored."""
    ref_id = _put_structure(hub)
    client = _chat_then_json(head)

    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(client.calls) == 2  # exactly one retry
    nudge = client.calls[1][-1]
    assert nudge["role"] == "user"
    assert "only the JSON object" in str(nudge["content"])
    assert _desc_chunks(store, ref_id)[0][1] == _DESC


def test_persistent_prose_fails_after_exactly_one_retry(store: Any, hub: Any) -> None:
    """A model that stays chatty through the nudge fails the ref — one
    retry, never a loop, and the prose is never stored."""
    ref_id = _put_structure(hub)
    client = _RawClient(_CHAT_AGENT_HEADS[0])

    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert len(client.calls) == 2
    assert _desc_chunks(store, ref_id) == []


def test_unparseable_dispatch_error_retries_once(store: Any, hub: Any) -> None:
    """The prod shape end-to-end: the ``claude -p`` rung's own JSON
    extraction rejects the reply, so the failure surfaces as a
    ``DispatchError`` classified ``unparseable`` — the same nudge retry
    covers it (the router stamps the class, no message sniffing)."""
    from precis.utils.llm.router import DispatchError

    ref_id = _put_structure(hub)

    class _UnparseableThenJson(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            if len(self.calls) == 1:
                raise DispatchError(
                    "claude -p returned no parseable JSON block",
                    reason_class="unparseable",
                )
            return SimpleNamespace(
                text=json.dumps({"description": _DESC}),
                data={"description": _DESC},
                model="claude-sonnet-5",
            )

    client = _UnparseableThenJson()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(client.calls) == 2
    assert _desc_chunks(store, ref_id)[0][1] == _DESC


def test_other_dispatch_error_does_not_retry(store: Any, hub: Any) -> None:
    """A non-``unparseable`` route failure is not nudgeable — a transport
    or quota miss propagates without a second call."""
    from precis.utils.llm.router import DispatchError

    ref_id = _put_structure(hub)

    class _BoomClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            raise DispatchError("all BIG rungs declined", paused=False)

    client = _BoomClient()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert len(client.calls) == 1
    assert _desc_chunks(store, ref_id) == []


def test_retry_is_logged_as_a_retry(store: Any, hub: Any, caplog: Any) -> None:
    """The nudge retry records itself: one warning naming the ref and the
    retry — the countable prod signal for the residual flake rate."""
    import logging

    ref_id = _put_structure(hub)

    with caplog.at_level(logging.WARNING, logger="precis.workers.card_describe"):
        r = run_card_describe_pass(
            store, client=_chat_then_json(_CHAT_AGENT_HEADS[1]), batch_size=10
        )

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    lines = [
        rec.getMessage()
        for rec in caplog.records
        if "retrying once" in rec.getMessage()
    ]
    assert len(lines) == 1
    assert f"ref_id={ref_id}" in lines[0]
    assert "JSON-only nudge" in lines[0]


def test_prompt_states_self_contained_json_only(store: Any, hub: Any) -> None:
    """The card-kinds' built prompt carries the whole contract in the
    USER message — the ``claude_p`` rung drops the system prompt, so the
    no-file/JSON-only instruction must live where the model reads it:
    the card is the complete input, and the schema plus a minimal
    example come last (gr543828)."""
    _put_structure(hub)
    client = _FakeClient()

    run_card_describe_pass(store, client=client, batch_size=10)

    user = str(client.calls[0][1]["content"])
    assert "complete input" in user
    assert "no file" in user
    assert "nothing else" in user
    assert '{"description"' in user
    # Schema + example are the tail — the last thing the model reads.
    assert user.index("Card:") < user.index("Example:")
    assert user.rstrip().endswith("}")
