"""First-class glyph-damage correction records (gr724751).

``put(kind='paper', id=<pa>, args={'correction': {...}})`` writes one
``chunk_corrections`` row anchored on the paper plus the quoted extracted
span — not on the chunk handle, so a body re-extraction keeps the record.
The record renders under the passage on chunk reads and is honoured by the
passage glyph warning, the pin term check, the draft glyph-cite hint and
the measure ``glyph_suspected`` sanity flag. The stored body stays as
extracted throughout.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.nanopub import pin_lint
from precis.store import ChunkInsert, Store
from precis.store._corrections_ops import augment_text
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis.utils.glyph_health import passage_glyph_caveat, span_in_text

#: The Kleshch-2010 shape from the gripe: extraction lost every μ.
DAMAGED = (
    "The current density was 10 mA/cm2 in the range of 1–2 V/ mm, "
    "with less than 1 V/mm usually observed."
)
CORRECTED = "10 µA/cm²"


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed(store: Store, slug: str = "kleshch2010", text: str = DAMAGED) -> int:
    ref = store.insert_ref(
        kind="paper", slug=slug, title=f"Title of {slug}", provider="manual", meta={}
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=text), ChunkInsert(ord=1, text="clean block")]
    )
    return ref.id


def _spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "extracted_span": "10 mA/cm2",
        "corrected_span": CORRECTED,
        "tier": "human-read",
        "reader": "reto",
        "approved_by": "reto",
        "source_file": "kleshch2010.pdf",
        "page": 4,
    }
    spec.update(over)
    return spec


# ── the put door ───────────────────────────────────────────────────────


def test_record_correction_happy_path(handler: PaperHandler, store: Store) -> None:
    ref_id = _seed(store)
    resp = handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    assert "CORRECTED (human-read)" in resp.body
    assert "'10 mA/cm2' reads '10 µA/cm²'" in resp.body
    assert "read by reto" in resp.body
    assert "kleshch2010.pdf p. 4" in resp.body
    assert "approved by reto" in resp.body
    assert "view='corrections'" in resp.body
    (corr,) = store.corrections.for_ref(ref_id)
    assert corr.extracted_span == "10 mA/cm2"
    assert corr.corrected_span == CORRECTED
    assert corr.tier == "human-read"
    assert corr.reader == "reto"


def test_record_correction_survives_body_reextract(
    handler: PaperHandler, store: Store
) -> None:
    """gr690836: the anchor is paper + quoted span, not the chunk handle —
    deleting the body and re-inserting the same text (fresh chunk ids, as
    re-extraction does) keeps the record rendering."""
    ref_id = _seed(store)
    handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))
        conn.commit()
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=0, text=DAMAGED), ChunkInsert(ord=1, text="clean block")],
    )
    out = handler.get(id=f"pa{ref_id}~0").body
    assert "CORRECTED (human-read)" in out
    assert CORRECTED in out
    # the advisory chunk pointer died with the old chunk; the span didn't.
    (corr,) = store.corrections.for_ref(ref_id)
    assert corr.chunk_id is None


@pytest.mark.parametrize(
    "over",
    [
        {"extracted_span": ""},
        {"corrected_span": " "},
        {"extracted_span": "10 mA/cm2", "corrected_span": "10 mA/cm2"},
        {"tier": "corrected"},
        {"tier": "human-read", "reader": None},
        {"tier": "confirmed-misprint", "reader": ""},
        {"approved_by": None},
        {"source_file": None},
        {"page": None},
        {"tier": "confirmed-misprint", "corrected_span": "x", "note": None},
    ],
)
def test_record_correction_refuses_bad_specs(
    handler: PaperHandler, store: Store, over: dict[str, Any]
) -> None:
    ref_id = _seed(store)
    with pytest.raises(BadInput):
        handler.put(id=f"pa{ref_id}", args={"correction": _spec(**over)})


def test_record_correction_refuses_span_absent_from_body(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    with pytest.raises(BadInput, match="occurs in no body chunk"):
        handler.put(
            id=f"pa{ref_id}",
            args={"correction": _spec(extracted_span="99 kA/cm2")},
        )


def test_record_correction_refuses_combined_doors(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    with pytest.raises(BadInput, match="does not combine"):
        handler.put(
            id=f"pa{ref_id}",
            text="a note",
            args={"correction": _spec()},
        )
    with pytest.raises(BadInput, match="must be a dict"):
        handler.put(id=f"pa{ref_id}", args={"correction": "not a dict"})


def test_record_correction_validates_page_and_chunk(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    with pytest.raises(BadInput, match="page"):
        handler.put(id=f"pa{ref_id}", args={"correction": _spec(page="four")})
    # ord-1 block does not carry the span
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 1", (ref_id,)
        ).fetchone()
    assert row is not None
    (chunk_id,) = row
    # ord-1 block does not carry the span
    with pytest.raises(BadInput, match="does not carry"):
        handler.put(
            id=f"pa{ref_id}",
            args={"correction": _spec(chunk=f"pc{chunk_id}")},
        )
    # a bogus handle is refused
    with pytest.raises(BadInput, match="pc chunk handle"):
        handler.put(id=f"pa{ref_id}", args={"correction": _spec(chunk="pa1")})


def test_corrections_view_lists_records(handler: PaperHandler, store: Store) -> None:
    ref_id = _seed(store)
    empty = handler.get(id=f"pa{ref_id}", view="corrections").body
    assert "no corrections" in empty
    handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    handler.put(
        id=f"pa{ref_id}",
        args={
            "correction": _spec(
                extracted_span="1–2 V/ mm",
                corrected_span="1–2 V/μm",
                tier="inferred",
                reader=None,
                approved_by=None,
                note="settled against the abstract's µm scale",
            )
        },
    )
    out = handler.get(id=f"pa{ref_id}", view="corrections").body
    assert out.count("CORRECTED") == 2
    assert "inferred from context" in out
    assert "settled against the abstract" in out


# ── read-side rendering ────────────────────────────────────────────────


def test_chunk_read_renders_record_under_the_passage(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    out = handler.get(id=f"pa{ref_id}~0").body
    # the record renders beneath the stored passage text, and the body
    # itself stays as extracted.
    assert "CORRECTED (human-read)" in out
    body, tail = out.split("usually observed.", 1)
    assert "10 mA/cm2 in the range" in body
    assert "CORRECTED" not in body
    assert "CORRECTED" in tail
    # the clean block carries no record
    assert out.count("CORRECTED") == 1


def test_passage_glyph_warning_suppressed_when_span_covered() -> None:
    """A correction whose extracted span covers every damage signature
    resolves the generic passage warning; an uncovered signature keeps it."""
    damaged = "The temperature was 2,000 8C and v ¼ 84:68."
    assert passage_glyph_caveat(damaged) != ""
    covered = passage_glyph_caveat(damaged, corrected_spans=["2,000 8C", "v ¼ 84:68"])
    assert covered == ""
    partial = passage_glyph_caveat(damaged, corrected_spans=["2,000 8C"])
    assert partial != ""


def test_passage_glyph_warning_kept_when_repeated_token_half_covered() -> None:
    """A damage token repeated in the passage stays flagged while any one
    occurrence lies outside the corrected span."""
    damaged = "At 2,000 8C the film held; later, at 2,000 8C it failed."
    covered = passage_glyph_caveat(damaged, corrected_spans=["At 2,000 8C the film"])
    assert covered != ""


def test_chunk_read_suppresses_banner_but_keeps_record(
    handler: PaperHandler, store: Store
) -> None:
    text = "The temperature was 2,000 8C and nothing else."
    ref_id = _seed(store, slug="legacy-glyph", text=text)
    assert "GLYPH-DAMAGED SOURCE" in handler.get(id=f"pa{ref_id}~0").body
    handler.put(
        id=f"pa{ref_id}",
        args={
            "correction": _spec(
                extracted_span="2,000 8C",
                corrected_span="2,000 °C",
            )
        },
    )
    out = handler.get(id=f"pa{ref_id}~0").body
    assert "GLYPH-DAMAGED SOURCE" not in out
    assert "'2,000 8C' reads '2,000 °C'" in out


# ── the pure helpers ───────────────────────────────────────────────────


def test_span_in_text_is_whitespace_tolerant() -> None:
    assert span_in_text("10 mA/cm2", "was\n10  mA/cm2 at")
    assert span_in_text("10 mA/cm2", "10 mA/cm2")
    assert not span_in_text("10 mA/cm2", "10 mA /cm2 extra words no")
    assert not span_in_text("", "anything")


def test_augment_text_projects_only_matching_corrections(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    (corr,) = store.corrections.for_ref(ref_id)
    projected = augment_text(DAMAGED, [corr])
    assert CORRECTED in projected
    assert "10 mA/cm2" not in projected  # the superseded reading is gone
    assert augment_text("clean block", [corr]) == "clean block"


# ── consumers ──────────────────────────────────────────────────────────


def test_pin_term_check_honours_the_corrected_span(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim quoting the corrected value covers a pin on the damaged
    passage once the correction is recorded — and warns before it is.
    Number terms are the checked shape here: extraction glued the decimal
    away (``8468°`` for a printed ``84.68°``), so the claim's number is
    uncovered against the raw passage and covered against the corrected
    span."""
    from precis.nanopub.keys import generate_keypair
    from tests.test_nanopub_gates_mint import _seed_paper

    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    passage = "The static water contact angle reached 8468° after treatment."
    paper, chunk, _sha = _seed_paper(store, chunk_text=passage)
    hub = mint_hub(
        store,
        CanonicalClaim(
            sentence="The static water contact angle reached 84.68°.", scope={}
        ),
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk}"},
        check_retraction=False,
    )
    fi = handle_registry.format_handle("finding", hub)
    text = f"The static water contact angle reached 84.68° [{fi}>pc{chunk}]."
    (finding,) = pin_lint.export_findings(store, text)
    assert finding.uncovered, "the corrected number must start out uncovered"
    store.corrections.add(
        ref_id=paper,
        extracted_span="8468°",
        corrected_span="84.68°",
        tier="human-read",
        reader="reto",
    )
    assert pin_lint.export_findings(store, text) == []


def test_glyph_cite_hint_names_the_record(store: Store, hub: Hub) -> None:
    from precis.draft.lint import glyph_cite_hint

    ref_id = _seed(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = "
            '\'{"glyph_health": {"suspected": true}}\'::jsonb WHERE ref_id = %s',
            (ref_id,),
        )
        conn.commit()
    text = f"Field emission draws current [pa{ref_id}]."
    plain = glyph_cite_hint(store, text)
    assert "glyphs" in plain
    assert "correction" not in plain
    store.corrections.add(
        ref_id=ref_id,
        extracted_span="10 mA/cm2",
        corrected_span=CORRECTED,
        tier="human-read",
        reader="reto",
    )
    hinted = glyph_cite_hint(store, text)
    assert "1 passage correction(s) on record" in hinted
    assert f"get(id='pa{ref_id}', view='corrections')" in hinted


def test_measure_glyph_flag_stands_down_when_chunk_corrected(
    store: Store,
) -> None:
    """The glyph_suspected sanity flag fires for a measure anchored on a
    glyph-suspected paper, then stands down only once a correction record
    covers the measure's own anchored reading (gr724751)."""
    from precis.store._measures_ops import MeasureAnchor, MeasureSpec
    from precis.taxonomy.nodes import slugify

    def _measurand(name: str) -> int:
        ref = store.insert_ref(
            kind="taxon",
            slug=None,
            title=name,
            meta={
                "name": name,
                "norm_name": name.lower(),
                "slug": slugify(name),
                "definition": f"{name}.",
                "aliases": [],
                "status": "proposed",
                "canonical_unit": "eV",
            },
        )
        return ref.id

    text = "The barrier was 1 eV. The current density was 10 mA/cm2 at 1 V."
    ref_id = _seed(store, slug="barrier2010", text=text)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (ref_id,)
        ).fetchone()
        assert row is not None
        (chunk_id,) = row
        conn.execute(
            "UPDATE refs SET meta = "
            '\'{"glyph_health": {"suspected": true}}\'::jsonb WHERE ref_id = %s',
            (ref_id,),
        )
        conn.commit()

    def _run(name: str, literal: str, span: str) -> list[dict[str, Any]]:
        run = store.insert_measure(
            MeasureSpec(
                _measurand(name),
                literal,
                ref_id,
                reported_unit="eV",
                tier="measured",
                anchor=MeasureAnchor(ref_id, chunk_id, "sentence", span),
            ),
            actor="reader",
        )
        with store.pool.connection() as conn:
            fetched = conn.execute(
                "SELECT meta FROM measures WHERE id = %s", (run.output_id,)
            ).fetchone()
        assert fetched is not None
        (meta,) = fetched
        return list(meta.get("escalation") or [])

    (flag,) = _run("emission barrier a", "10", "s1")
    assert flag["rule"] == "glyph_suspected"
    store.corrections.add(
        ref_id=ref_id,
        extracted_span="10 mA/cm2",
        corrected_span=CORRECTED,
        tier="human-read",
        reader="reto",
    )
    # the measure whose own anchored reading is corrected stands down ...
    assert _run("emission barrier b", "10", "s1") == []
    # ... an unrelated value in another sentence of the chunk does not ...
    (other,) = _run("emission barrier c", "1", "s0")
    assert other["rule"] == "glyph_suspected"
    # ... nor an unrelated value in the corrected sentence.
    (same,) = _run("emission barrier d", "1", "s1")
    assert same["rule"] == "glyph_suspected"


def test_sentences_view_renders_record_beneath_sentences(
    handler: PaperHandler, store: Store
) -> None:
    ref_id = _seed(store)
    handler.put(id=f"pa{ref_id}", args={"correction": _spec()})
    out = handler.get(id=f"pa{ref_id}~0", view="sentences").body
    assert out.index("usually observed") < out.index("CORRECTED (human-read)")
