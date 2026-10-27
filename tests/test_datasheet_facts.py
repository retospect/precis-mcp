"""Datasheet-extracted part facts on the part ref (datasheet-facts-mesh).

The fixture datasheet is a small excerpt in the shape Marker's pipeline
produces for a real LDO datasheet (pin-functions table + absolute-max +
an I²C-style address/strap table); the facts are what a ``datasheet_facts``
extraction would write. Network/LLM: none — this file exercises the
write/validate/read surface only.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.part import PartHandler
from precis.pcb import catalog
from precis.pcb import datasheet_facts as df
from precis.pcb import datasheets as pcb_datasheets
from precis.store.types import ChunkInsert

# Excerpt shape a real LDO datasheet produces after Marker chunking:
# a pin-functions table, an absolute-maximum table, and a strap table.
_DS_TEXT = (
    "5 Pin Functions\n"
    "PIN  NO.  NAME  DESCRIPTION\n"
    "1    EN    Enable input. Drive high to enable; do not float.\n"
    "2    GND   Ground.\n"
    "3    IN    Input supply. A 1 uF X5R or better capacitor is required "
    "from IN to GND, placed within 5 mm of the pin.\n"
    "5    OUT   Regulated output. Requires 2.2 uF minimum output "
    "capacitance for stability.\n"
    "4    NC    No internal connection.\n\n"
    "Absolute Maximum Ratings: IN −0.3 V to 6.0 V; EN −0.3 V to 6.0 V; "
    "OUT −0.3 V to VIN + 0.3 V.\n"
    "Recommended: VIN 1.5 V to 5.5 V; EN VIH min 0.9 V, VIL max 0.4 V.\n"
    "ADDR pin strapping: ADDR=GND → 0x48; ADDR=VDD → 0x49; ADDR=SDA → "
    "0x4A; ADDR=SCL → 0x4B."
)


def _catalog(store, lcsc: str = "C5446") -> int:
    row = catalog.normalize_jlcparts_row(
        {
            "lcsc": int(lcsc[1:]),
            "manufacturer": "Acme",
            "mfr_part": "ACME-LDO33",
            "description": "3.3V 300mA LDO SOT-23-5",
            "basic": 1,
            "stock": 500,
            "package": "SOT-23-5",
            "datasheet": "https://example.invalid/ds.pdf",
        }
    )
    assert row is not None
    store.parts_import([row])
    return store.ensure_part_ref(lcsc)


def _datasheet(store) -> tuple[int, list[int]]:
    """A datasheet ref with three body chunks (page_first set as Marker
    records it); returns (ref_id, chunk_ids)."""
    ds = store.insert_ref(
        kind="datasheet", slug="acme-ldo33", title="ACME-LDO33 datasheet"
    )
    rows = store.chunks.insert_chunks(
        ds.id,
        [
            ChunkInsert(ord=0, text=_DS_TEXT[:120], meta={}),
            ChunkInsert(ord=1, text=_DS_TEXT[120:], meta={}),
            ChunkInsert(ord=2, text="Application circuit and layout notes.", meta={}),
        ],
    )
    ids = [r.id for r in rows]
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET page_first = 5, page_last = 5 WHERE chunk_id = ANY(%s)",
            (ids[:2],),
        )
        conn.execute(
            "UPDATE chunks SET page_first = 9, page_last = 9 WHERE chunk_id = %s",
            (ids[2],),
        )
    return ds.id, ids


def _payload(chunk_id: int) -> dict[str, Any]:
    return {
        "packages": {
            "SOT-23-5": {
                "pins": [
                    {
                        "pin": "1",
                        "name": "EN",
                        "function": "enable input",
                        "etype": "input",
                        "rail": "VIN",
                        "vih": 0.9,
                        "vil": 0.4,
                        "confidence": 0.9,
                        "page": 5,
                        "chunk_id": chunk_id,
                    },
                    {
                        "pin": "3",
                        "name": "IN",
                        "function": "input supply",
                        "etype": "power_in",
                        "rail": "VIN",
                        "max_voltage_v": 6.0,
                        "confidence": 0.95,
                        "page": 5,
                        "chunk_id": chunk_id,
                    },
                    {
                        "pin": "5",
                        "name": "OUT",
                        "etype": "power_out",
                        "confidence": 0.6,
                        "page": 5,
                        "chunk_id": chunk_id,
                    },
                ],
                "required_externals": [
                    {
                        "pins": ["3"],
                        "part_class": "capacitor",
                        "value": "1uF",
                        "min_dielectric": "X5R",
                        "max_distance_mm": 5.0,
                        "note": "input capacitor",
                        "confidence": 0.8,
                        "page": 5,
                        "chunk_id": chunk_id,
                    }
                ],
                "conditional_tables": [
                    {
                        "name": "i2c address",
                        "inputs": ["ADDR"],
                        "output_key": "address",
                        "rows": [
                            {"ADDR": "GND", "address": "0x48"},
                            {"ADDR": "VDD", "address": "0x49"},
                        ],
                        "confidence": 0.85,
                        "page": 5,
                        "chunk_id": chunk_id,
                    }
                ],
            }
        }
    }


def test_write_read_round_trip(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)

    res = df.write_facts(store, part_id, _payload(chunk_ids[0]), datasheet_ref_id=ds_id)
    assert res["rev"] == 1 and res["trims"] == 0
    assert df.facts_revision(store, part_id) == 1

    facts = df.part_facts(store, "C5446")
    assert facts is not None
    assert facts.datasheet_ref_id == ds_id
    pkg = facts.package()
    assert pkg is not None and pkg.package == "SOT-23-5"
    assert len(pkg.pins) == 3

    en = facts.pin("1")
    assert en is not None
    assert en.name == "EN" and en.etype == "input"
    assert en.vih == 0.9 and en.vil == 0.4
    assert en.source is not None
    assert en.source.chunk_id == chunk_ids[0] and en.source.page == 5
    assert en.source.datasheet_ref_id == ds_id

    ext = pkg.required_externals[0]
    assert ext.pins == ("3",) and ext.min_dielectric == "X5R"
    table = pkg.conditional_tables[0]
    assert table.inputs == ("ADDR",) and table.rows[1]["address"] == "0x49"


def test_rev_bumps_on_rewrite(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    df.write_facts(store, part_id, _payload(chunk_ids[0]), datasheet_ref_id=ds_id)
    rev = df.write_facts(store, part_id, _payload(chunk_ids[1]), datasheet_ref_id=ds_id)
    assert rev["rev"] == 2
    facts = df.part_facts(store, "C5446")
    assert facts is not None
    pin1 = facts.pin("1")
    assert pin1 is not None and pin1.source is not None
    assert pin1.source.chunk_id == chunk_ids[1]


def test_provenance_must_cite_the_declared_datasheet(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    other = store.insert_ref(kind="datasheet", slug="other-ds", title="other")
    other_chunk = store.chunks.insert_chunks(
        other.id, [ChunkInsert(ord=0, text="unrelated", meta={})]
    )[0].id

    with pytest.raises(BadInput, match="not a live chunk"):
        df.write_facts(store, part_id, _payload(other_chunk), datasheet_ref_id=ds_id)
    with pytest.raises(BadInput, match="needs datasheet_ref_id"):
        df.write_facts(store, part_id, _payload(chunk_ids[0]))


def test_unknown_fact_reads_as_missing_never_guessed(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    df.write_facts(store, part_id, _payload(chunk_ids[0]), datasheet_ref_id=ds_id)

    facts5 = df.part_facts(store, "C5446")
    assert facts5 is not None
    out = facts5.pin("5")
    assert out is not None
    # OUT's abs-max is not in the fixture row: unknown, and the review
    # must ask the user rather than infer one.
    assert out.max_voltage_v is None
    assert "max_voltage_v" in out.missing()
    assert "rail" in out.missing()


def test_no_facts_is_distinct_from_empty(store):
    _catalog(store)
    assert df.part_facts(store, "C5446") is None
    assert "no datasheet facts" in df.render_facts(None, lcsc="C5446")
    part_id = store.part_ref_id("C5446")
    assert df.facts_revision(store, part_id) == 0

    # A recorded failure alone is still "no facts" for the read API.
    df.record_extraction(
        store, part_id, {"status": "failed", "at": "2026-10-14T00:00:00Z"}
    )
    assert df.part_facts(store, "C5446") is None

    # A recorded-but-empty extraction IS a block — "read it, found nothing".
    store.update_ref(
        part_id,
        meta_patch={
            df.FACTS_META_KEY: {
                "rev": 1,
                "extracted_at": "2026-10-14T00:00:00Z",
                "packages": {},
            }
        },
    )
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.packages == {}
    assert "no package blocks" in df.render_facts(facts, lcsc="C5446")


def test_validation(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)

    def bad(mut):
        payload = _payload(chunk_ids[0])
        mut(payload)
        return payload

    with pytest.raises(BadInput, match="etype"):
        df.write_facts(
            store,
            part_id,
            bad(lambda p: p["packages"]["SOT-23-5"]["pins"][0].update(etype="mystery")),
            datasheet_ref_id=ds_id,
        )
    with pytest.raises(BadInput, match="duplicate pin"):
        df.write_facts(
            store,
            part_id,
            bad(
                lambda p: p["packages"]["SOT-23-5"]["pins"].append(
                    dict(p["packages"]["SOT-23-5"]["pins"][0])
                )
            ),
            datasheet_ref_id=ds_id,
        )
    with pytest.raises(BadInput, match="confidence"):
        df.write_facts(
            store,
            part_id,
            bad(lambda p: p["packages"]["SOT-23-5"]["pins"][0].update(confidence=1.7)),
            datasheet_ref_id=ds_id,
        )
    with pytest.raises(BadInput, match="packages"):
        df.write_facts(store, part_id, {"pins": []})


def test_facts_view_on_part_get(store):
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    df.write_facts(store, part_id, _payload(chunk_ids[0]), datasheet_ref_id=ds_id)

    h = PartHandler(hub=Hub(store=store))
    body = h.get(id="C5446", view="facts").body
    assert "rev 1" in body
    assert "pin 3" in body and "IN" in body
    assert "max_voltage_v=6" in body
    assert "vih=?" in body  # IN row declares no vih — unknown marker
    assert "required_external: capacitor" in body
    assert "conditional_table 'i2c address'" in body
    assert "p.5" in body

    # The selector's read: one pin by number through the store API.
    facts = df.part_facts(store, "C5446")
    assert facts is not None
    pin3 = df.pin_fact(facts, "3")
    assert pin3 is not None and pin3.name == "IN"
    assert df.pin_fact(facts, "99") is None


def test_facts_view_without_facts(store):
    _catalog(store)
    h = PartHandler(hub=Hub(store=store))
    assert "no datasheet facts" in h.get(id="C5446", view="facts").body
    with pytest.raises(BadInput, match="no view"):
        h.get(id="C5446", view="bogus")


def test_facts_view_without_id_carries_next(store):
    """gr494831 M3: a hint-less raise is a bug; the facts-view-without-id
    BadInput must name a copy-pasteable recovery call."""
    h = PartHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="requires id=") as ei:
        h.get(view="facts")
    assert ei.value.next and "get(kind='part', id='C" in str(ei.value.next)


# ── the part brief (gr520243) ─────────────────────────────────────────


def _brief(chunk_id: int) -> dict[str, Any]:
    return {
        "one_line": "3.3V 300mA LDO, SOT-23-5",
        "envelope": {"supply": "1.5–5.5 V", "current": "300 mA max"},
        "key_numbers": [
            {"text": "IN abs max 6.0 V", "page": 5, "chunk_id": chunk_id},
            {
                "text": "needs 1uF X5R on IN within 5 mm",
                "page": 5,
                "chunk_id": chunk_id,
            },
        ],
        "gotchas": [
            {"text": "EN must not float", "page": 5, "chunk_id": chunk_id},
        ],
        "typical_application": {
            "text": "2.2uF output capacitor for stability",
            "page": 5,
            "chunk_id": chunk_id,
        },
    }


def _payload_with_brief(chunk_id: int) -> dict[str, Any]:
    p = _payload(chunk_id)
    p["brief"] = _brief(chunk_id)
    return p


def test_brief_write_read_round_trip(store):
    """gr520243: the brief rides the same block; every cited item reads
    back with its datasheet provenance."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    df.write_facts(
        store, part_id, _payload_with_brief(chunk_ids[0]), datasheet_ref_id=ds_id
    )
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.brief is not None
    b = facts.brief
    assert b.one_line == "3.3V 300mA LDO, SOT-23-5"
    assert b.envelope["supply"] == "1.5–5.5 V"
    assert len(b.key_numbers) == 2
    kn = b.key_numbers[0]
    assert kn.source is not None
    assert kn.source.chunk_id == chunk_ids[0] and kn.source.page == 5
    assert kn.source.datasheet_ref_id == ds_id
    assert b.typical_application is not None and b.typical_application.source


def test_brief_provenance_must_cite_the_declared_datasheet(store):
    """A dangling brief citation is the same write error as a dangling
    fact citation — the block never stores it."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    other = store.insert_ref(kind="datasheet", slug="other-ds", title="other")
    other_chunk = store.chunks.insert_chunks(
        other.id, [ChunkInsert(ord=0, text="unrelated", meta={})]
    )[0].id

    p = _payload_with_brief(chunk_ids[0])
    p["brief"]["key_numbers"][0]["chunk_id"] = other_chunk
    with pytest.raises(BadInput, match="not a live chunk"):
        df.write_facts(store, part_id, p, datasheet_ref_id=ds_id)

    p = _payload_with_brief(chunk_ids[0])
    with pytest.raises(BadInput, match="needs datasheet_ref_id"):
        df.write_facts(store, part_id, p)


def test_brief_view_and_default_get(store):
    """view='brief' renders the distillation; the default get shows it
    once it exists (gr520243 acceptance: opening the part shows a
    sourced brief)."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    df.write_facts(
        store, part_id, _payload_with_brief(chunk_ids[0]), datasheet_ref_id=ds_id
    )
    h = PartHandler(hub=Hub(store=store), live_lookup=lambda *a, **k: None)
    body = h.get(id="C5446", view="brief").body
    assert "3.3V 300mA LDO" in body
    assert "supply 1.5–5.5 V" in body
    assert "IN abs max 6.0 V" in body and "p.5" in body
    assert "gotcha: EN must not float" in body
    assert "typical application:" in body
    # the default get leads with the catalog table, then the brief
    default = h.get(id="C5446").body
    assert "# C5446 — 3.3V 300mA LDO" in default


def test_brief_view_without_brief(store):
    _catalog(store)
    h = PartHandler(hub=Hub(store=store), live_lookup=lambda *a, **k: None)
    assert "no brief" in h.get(id="C5446", view="brief").body


def test_malformed_brief_drops_but_facts_land(store):
    """A brief the schema can't take must not sink the pin facts it
    rode in with — the write keeps the facts, drops the brief, and the
    reply carries one warning naming why."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    p = _payload_with_brief(chunk_ids[0])
    p["brief"] = {"key_numbers": [{"text": "x"}]}  # no one_line
    res = df.write_facts(store, part_id, p, datasheet_ref_id=ds_id)
    assert res["brief"] is False
    assert len(res["warnings"]) == 1 and "one_line" in res["warnings"][0]
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.brief is None
    pkg = facts.package()
    assert pkg is not None and len(pkg.pins) == 3

    # A brief that isn't even an object salvages the same way.
    p = _payload_with_brief(chunk_ids[0])
    p["brief"] = "garbage"
    res = df.write_facts(store, part_id, p, datasheet_ref_id=ds_id)
    assert res["brief"] is False and len(res["warnings"]) == 1
    assert df.part_facts(store, "C5446") is not None


def test_brief_item_without_chunk_id_renders_uncited(store):
    """An extraction may leave ``chunk_id`` off an item — it stays
    accepted, but renders ``[uncited]`` so a reader knows there is no
    chunk to open."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    p = _payload_with_brief(chunk_ids[0])
    p["brief"]["key_numbers"][0].pop("chunk_id")  # page-only cite
    p["brief"]["gotchas"][0] = {"text": "watch the dropout at low VIN"}
    p["brief"]["typical_application"].pop("chunk_id")
    df.write_facts(store, part_id, p, datasheet_ref_id=ds_id)
    body = (
        PartHandler(hub=Hub(store=store), live_lookup=lambda *a, **k: None)
        .get(id="C5446", view="brief")
        .body
    )
    assert "IN abs max 6.0 V [uncited]" in body
    assert "gotcha: watch the dropout at low VIN [uncited]" in body
    assert "typical application: 2.2uF output capacitor" in body
    assert "capacitor for stability [uncited]" in body
    # a fully-cited item still carries its chunk cite
    assert "needs 1uF X5R on IN within 5 mm [p.5" in body


def test_brief_text_is_capped_not_refused(store):
    """A rambling extraction can't bloat the block: over-cap text
    truncates with an ellipsis, extra items drop, and the write reply
    counts the trims — a 50k one_line and 5k gotchas still store."""
    part_id = _catalog(store)
    ds_id, chunk_ids = _datasheet(store)
    p = _payload_with_brief(chunk_ids[0])
    p["brief"]["one_line"] = "x" * 50_000
    p["brief"]["key_numbers"] = [
        {"text": "n" * 400, "page": 5, "chunk_id": chunk_ids[0]}
    ] * 12
    p["brief"]["gotchas"] = [
        {"text": f"gotcha {i}", "page": 5, "chunk_id": chunk_ids[0]}
        for i in range(5_000)
    ]
    p["brief"]["envelope"] = {f"k{i}": "v" * 200 for i in range(20)} | {
        "k" * 60: "long key"
    }
    res = df.write_facts(store, part_id, p, datasheet_ref_id=ds_id)
    assert res["brief"] is True and res["trims"] > 0
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.brief is not None
    b = facts.brief
    assert len(b.one_line) <= 160 and b.one_line.endswith("…")
    assert len(b.key_numbers) == 10
    assert all(len(i.text) <= 300 for i in b.key_numbers)
    assert len(b.gotchas) == 10
    assert len(b.envelope) <= 8
    assert all(len(k) <= 40 for k in b.envelope)
    assert all(len(v) <= 120 for v in b.envelope.values())


def test_part_neighbor_label_prefers_brief_one_line(store):
    """gr520243: the brief's one_line is the part's fisheye link label —
    a ring naming C-numbers reads what each part is."""
    from types import SimpleNamespace

    from precis.utils.eye_render import _neighbor_label

    ref = SimpleNamespace(
        kind="part",
        slug="C5446",
        title="ACME-LDO33 — 3.3V 300mA LDO SOT-23-5",
        meta={"datasheet_facts": {"brief": _brief(1)}},
    )
    assert "3.3V 300mA LDO, SOT-23-5" in _neighbor_label(ref, 42)
    assert "ACME-LDO33" not in _neighbor_label(ref, 42)
    # no brief -> catalog title unchanged
    ref.meta = {}
    assert "ACME-LDO33" in _neighbor_label(ref, 42)


def test_extract_records_and_stores_brief(store):
    _part_id, _ds_id, chunk_ids = _linked(store)
    record = fj.extract(
        store,
        "C5446",
        job_ref_id=1,
        llm=lambda *a, **k: _reply(_payload_with_brief(chunk_ids[0])),
    )
    assert record["status"] == "ok" and record["brief"] is True
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.brief is not None
    assert facts.brief.one_line.startswith("3.3V")


def test_prompt_asks_for_the_brief(store):
    assert '"brief"' in fj._PROMPT and "one_line" in fj._PROMPT


# ── the datasheet_facts extraction job ───────────────────────────────

import json

from precis.utils.llm.router import LlmResult, Tier
from precis.workers.job_types import datasheet_facts as fj
from precis.workers.job_types import get_job_type


def _reply(payload: dict) -> LlmResult:
    return LlmResult(
        text=json.dumps(payload),
        cost_usd=0.02,
        turns_used=None,
        model="test-model",
        tier=Tier.BIG,
    )


def _linked(store, lcsc: str = "C5446"):
    """Part ref + a chunked datasheet linked ``datasheet-of`` it."""
    part_id = _catalog(store, lcsc)
    ds_id, chunk_ids = _datasheet(store)
    store.add_link(
        src_ref_id=ds_id, dst_ref_id=part_id, relation="datasheet-of", set_by="system"
    )
    return part_id, ds_id, chunk_ids


def test_extract_writes_facts_with_provenance(store):
    part_id, ds_id, chunk_ids = _linked(store)
    record = fj.extract(
        store,
        "C5446",
        job_ref_id=999,
        llm=lambda *a, **k: _reply(_payload(chunk_ids[0])),
    )
    assert record["status"] == "ok" and record["pins"] == 3 and record["rev"] == 1

    facts = df.part_facts(store, "C5446")
    assert facts is not None
    assert facts.datasheet_ref_id == ds_id and facts.job_ref_id == 999
    pin3 = facts.pin("3")
    assert pin3 is not None and pin3.max_voltage_v == 6.0
    ref = store.get_ref(kind="part", id=part_id)
    ex = ref.meta["datasheet_facts"]["extraction"]
    assert ex["status"] == "ok" and ex["model"] == "test-model"


def test_extract_no_datasheet_is_a_recorded_state(store):
    part_id = _catalog(store)
    record = fj.extract(store, "C5446", llm=lambda *a, **k: _reply({}))
    assert record["status"] == "failed" and record["reason"] == "no_datasheet"
    ex = store.get_ref(kind="part", id=part_id).meta["datasheet_facts"]["extraction"]
    assert ex["status"] == "failed"


def test_extract_bad_payload_is_recorded_not_raised(store):
    part_id, _ds, chunk_ids = _linked(store)
    bad = _payload(chunk_ids[0])
    bad["packages"]["SOT-23-5"]["pins"][0]["chunk_id"] = 424242
    record = fj.extract(store, "C5446", llm=lambda *a, **k: _reply(bad))
    assert record["status"] == "failed" and record["reason"] == "bad_payload"
    assert df.part_facts(store, "C5446") is None


def test_extract_llm_failure_records_and_raises_for_dispatch(store):
    _linked(store)
    res = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="",
        tier=Tier.BIG,
        error="upstream 500",
        reason_class="transport",
    )
    with pytest.raises(fj.LlmCallFailed):
        fj.extract(store, "C5446", llm=lambda *a, **k: res)
    ex = store.get_ref(kind="part", id=store.part_ref_id("C5446")).meta[
        "datasheet_facts"
    ]["extraction"]
    assert ex["status"] == "failed" and ex["reason"] == "llm:transport"


def test_extract_empty_sheet_stores_an_empty_block(store):
    _linked(store)
    record = fj.extract(store, "C5446", llm=lambda *a, **k: _reply({"packages": {}}))
    assert record["status"] == "ok" and record["pins"] == 0
    facts = df.part_facts(store, "C5446")
    assert facts is not None and facts.packages == {}


def test_parse_payload_fences_and_prose(store):
    assert fj.parse_payload('```json\n{"packages": {"SOT-23": {}}}\n```') == {
        "packages": {"SOT-23": {}}
    }
    assert fj.parse_payload('here it is: {"packages": {}} done') == {"packages": {}}
    with pytest.raises(ValueError):
        fj.parse_payload("no json here")


class _FakeCtx:
    def __init__(self, store, ref_id, params):
        self.store = store
        self.ref_id = ref_id
        self.title = "facts"
        self.meta = {"params": params}
        self.chunks: list = []
        self.status = None
        self.meta_set: dict = {}
        self.failure = None
        self.deferred: list[str] = []
        self.defer = None

    def set_status(self, s):
        self.status = s

    def append_chunk(self, kind, text):
        self.chunks.append((kind, text))

    def set_meta(self, **kw):
        self.meta_set.update(kw)

    def record_failure(self, msg, **kw):
        self.failure = msg

    def is_cancel_requested(self):
        return False

    def park(self, wake, note):
        self.deferred.append(note)
        return True


def _payload_from_prompt(store, prompt):
    """Answer the extraction with facts citing the chunk ids the prompt
    actually carried — the round-trip proves the citation lands."""
    import re as _re

    m = _re.search(r"\[chunk (\d+),", prompt)
    assert m is not None
    cid = int(m.group(1))
    return _payload(cid)


def test_dispatch_full(store, monkeypatch):
    _linked(store)
    monkeypatch.setattr(
        fj,
        "_llm",
        lambda prompt, **k: _reply(_payload_from_prompt(store, prompt)),
    )
    ctx = _FakeCtx(store, 7, {"lcsc": "C5446"})
    fj._dispatch(ctx, fj.SPEC)
    assert ctx.status == "succeeded"
    assert ctx.meta_set["facts_status"] == "ok" and ctx.meta_set["facts_rev"] == 1
    facts = df.part_facts(store, "C5446")
    assert facts is not None
    pin = df.pin_fact(facts, "3")
    assert pin is not None and pin.source is not None
    assert pin.source.chunk_id is not None  # cited a real chunk


def test_dispatch_rejects_bad_lcsc(store):
    ctx = _FakeCtx(store, 7, {"lcsc": "nope"})
    fj._dispatch(ctx, fj.SPEC)
    assert ctx.failure is not None


def test_registered(store):
    spec = get_job_type("datasheet_facts")
    assert spec is not None and "claude_inproc" in spec.compatible_executors


def _job_handler(store):
    from precis.handlers.job import JobHandler

    return JobHandler(hub=Hub(store=store))


def _board_with(store, lcsc: str = "C5446") -> int:
    """A pcb ref whose live instance carries ``lcsc`` (the enqueue door's
    ``live_lcscs`` needs a real board, not a bare ref). The put itself
    already runs ``_queue_datasheets`` — pull first, then facts."""
    from precis.handlers.pcb import PcbHandler

    resp = PcbHandler(hub=Hub(store=store)).put(
        id="b1",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "ldo",
                    "footprint": "SOT-23-5",
                    "part": lcsc,
                    "x": 10.0,
                    "y": 10.0,
                    "pins": [{"name": "1"}, {"name": "3"}],
                }
            ]
        },
    )
    ref = store.get_ref(kind="pcb", id="b1")
    assert ref is not None, resp.body
    return ref.id


def _facts_job(store):
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT ref_id, parent_id, meta->'params'->>'lcsc' FROM refs "
            "WHERE kind='job' AND meta->>'job_type'='datasheet_facts'"
        ).fetchone()


def test_put_enqueues_facts_next_to_pull(store):
    """A put with a C-number queues the pull AND the extraction; the facts
    job is parented on the board and idempotent on the lcsc."""
    _catalog(store)
    board_id = _board_with(store)
    row = _facts_job(store)
    assert row is not None
    assert row[1] == board_id and row[2] == "C5446"
    job = _job_handler(store)
    assert pcb_datasheets.enqueue_facts(store, job, board_id) == 0


def test_enqueue_facts_skips_extracted(store):
    _with_facts(store)
    board_id = _board_with(store)
    job = _job_handler(store)
    # Facts already extracted for this part -> no job minted.
    assert pcb_datasheets.enqueue_facts(store, job, board_id) == 0


def test_facts_job_parks_while_pull_in_flight(store):
    """A facts job that wins the race against its pull defers instead of
    recording no_datasheet."""
    part_id = _catalog(store)
    _board_with(store)  # the put queued a datasheet_pull for C5446
    assert pcb_datasheets.pull_in_flight(store, "C5446")

    ctx = _FakeCtx(store, 7, {"lcsc": "C5446"})
    ctx.defer = ctx.park
    fj._dispatch(ctx, fj.SPEC)
    assert ctx.deferred and "in flight" in ctx.deferred[0]
    # nothing recorded on the part — the state is a wait, not a failure
    ref = store.get_ref(kind="part", id=part_id)
    assert "datasheet_facts" not in (ref.meta or {})


# ── the part:<C#>~<pin> link selector ────────────────────────────────

from precis.utils.link_target import parse_link_target


def _with_facts(store):
    part_id, ds_id, chunk_ids = _linked(store)
    df.write_facts(store, part_id, _payload(chunk_ids[0]), datasheet_ref_id=ds_id)
    return part_id, ds_id, chunk_ids


def test_pin_selector_resolves_to_datasheet_chunk(store):
    _part_id, ds_id, _chunk_ids = _with_facts(store)
    t = parse_link_target("part:C5446~3", store=store)
    assert t.kind == "datasheet" and t.ref_id == ds_id and t.pos == 0


def test_pin_selector_unknown_pin_quotes_valid(store):
    _with_facts(store)
    with pytest.raises(BadInput, match="no pin '99'"):
        parse_link_target("part:C5446~99", store=store)


def _two_packages(store):
    """The same pin table under two package names — pin 3 is in both."""
    part_id, ds_id, chunk_ids = _linked(store)
    payload = _payload(chunk_ids[0])
    pkg = payload["packages"]["SOT-23-5"]
    payload["packages"]["SC-70-5"] = {"pins": [dict(p) for p in pkg["pins"]]}
    df.write_facts(store, part_id, payload, datasheet_ref_id=ds_id)
    facts = df.part_facts(store, "C5446")
    assert facts is not None and len(facts.packages) == 2
    return facts


def test_pin_fact_refuses_pin_ambiguous_across_packages(store):
    facts = _two_packages(store)
    with pytest.raises(BadInput, match="several packages"):
        df.pin_fact(facts, "3")
    # naming the package disambiguates
    pin = df.pin_fact(facts, "3", package="SC-70-5")
    assert pin is not None and pin.name == "IN"
    assert df.pin_fact(facts, "99") is None


def test_pin_selector_refuses_pin_ambiguous_across_packages(store):
    _two_packages(store)
    with pytest.raises(BadInput, match="several packages"):
        parse_link_target("part:C5446~3", store=store)


def test_pin_selector_without_facts(store):
    _catalog(store)
    with pytest.raises(BadInput, match="no extracted datasheet facts"):
        parse_link_target("part:C5446~3", store=store)


def test_pin_selector_link_end_to_end(store):
    """The whole walk: link a memory to a pin; the edge lands on the
    datasheet chunk that prints the row."""
    _part_id, _ds_id, chunk_ids = _with_facts(store)
    note = store.insert_ref(kind="memory", slug=None, title="about EN")
    t = parse_link_target("part:C5446~1", store=store)
    store.add_link(
        src_ref_id=note.id, dst_ref_id=t.ref_id, dst_pos=t.pos, relation="related-to"
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT dst_chunk_id FROM links WHERE src_ref_id = %s", (note.id,)
        ).fetchone()
    assert row[0] == chunk_ids[0]
