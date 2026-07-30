"""Taproot slice A1 — ``precis resolve``'s living-citation expansion.

A ``[pub_id]`` that resolves to a ``TAPROOT:claim`` hub (`src/precis/
taproot/hub.py::mint_hub`) expands to the hub's *current* derived
``establishes`` originator(s) — `src/precis/taproot/seniority.py::
derive_evidence` — rather than a stored ``primary_cite_key``. Because
the split is recomputed on every run, a later-discovered originator or
a claim merge improves the ``.bib`` output on the next ``resolve``, no
hand-editing required.

Regular (non-hub) findings are untouched — covered already by
``tests/test_verify.py``'s ``TestStrictVerified`` — this file only
adds one regression case to pin that the hub branch doesn't leak into
the ordinary ``primary_cite_key`` path.
"""

from __future__ import annotations

from typing import Any

from precis.cli.resolve import _lookup_finding, _resolve_text
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)

_slug_counter = 0


def _slug() -> str:
    global _slug_counter
    _slug_counter += 1
    return f"resv{_slug_counter}"


def _paper(
    store: Any, *, cite_key: str | None, title: str, year: int | None = None
) -> int:
    slug = cite_key or _slug()
    ref = store.insert_ref(kind="paper", slug=slug, title=title, year=year, meta={})
    if cite_key is None:
        # `insert_ref` always seeds a `cite_key` alias for a paper
        # slug — strip it back out so this paper genuinely has none,
        # exercising the "supporter with no cite_key" skip path.
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = 'cite_key'",
                (ref.id,),
            )
            conn.commit()
    return ref.id


def _cites(store: Any, *, src: int, dst: int) -> None:
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites")


def _hub_pub_id(store: Any, hub_ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (hub_ref_id,),
        ).fetchone()
    assert row is not None, f"no pub_id minted for hub ref_id={hub_ref_id}"
    return str(row[0])


def _resolve(store: Any, text: str, *, format: str = "plain") -> tuple[str, Any]:
    return _resolve_text(
        text,
        store=store,
        format=format,
        ascii_mode=True,
        keep_id=False,
    )


# ── hub detection ────────────────────────────────────────────────────


def test_lookup_finding_flags_taproot_hub(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)

    finding = _lookup_finding(store, pub_id)

    assert finding is not None
    assert finding["is_hub"] is True
    assert finding["ref_id"] == hub


# ── originators present → cite them ─────────────────────────────────


def test_hub_resolves_to_derived_originator_latex(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    origin = _paper(store, cite_key="orig01a", title="Original report", year=2001)
    follow = _paper(store, cite_key="foll05a", title="Follow-up", year=2005)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    _cites(store, src=follow, dst=origin)  # follow cites origin -> origin is originator

    out, summary = _resolve(store, f"see [{pub_id}].", format="latex")

    assert r"\cite{orig01a}" in out
    assert pub_id not in out
    assert summary.resolved_count == 1
    assert summary.inflight_pub_ids == []


def test_hub_resolves_to_derived_originator_plain(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    origin = _paper(store, cite_key="orig02a", title="Original report", year=2001)
    follow = _paper(store, cite_key="foll06a", title="Follow-up", year=2006)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    _cites(store, src=follow, dst=origin)

    out, summary = _resolve(store, f"see [{pub_id}].", format="plain")

    assert "[orig02a]" in out
    assert summary.resolved_count == 1


def test_hub_multiple_originators_render_multi_key(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    a = _paper(store, cite_key="alpha01", title="A — first report", year=2001)
    b = _paper(store, cite_key="beta02", title="B — second report", year=2002)
    citer = _paper(store, cite_key="citer09", title="Citer", year=2009)
    for p in (a, b, citer):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    _cites(store, src=citer, dst=a)
    _cites(store, src=citer, dst=b)

    latex_out, _ = _resolve(store, f"[{pub_id}]", format="latex")
    plain_out, _ = _resolve(store, f"[{pub_id}]", format="plain")

    # derive_evidence orders originators by year asc then ref_id — a
    # (2001) before b (2002).
    assert r"\cite{alpha01,beta02}" in latex_out
    assert "[alpha01; beta02]" in plain_out


# ── no derived originator → fall back to corroborators ──────────────


def test_hub_falls_back_to_corroborators_when_no_originator_derived(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    a = _paper(store, cite_key="corA01", title="A", year=2001)
    b = _paper(store, cite_key="corB02", title="B", year=2002)
    # No intra-set `cites` edges -> derive_evidence yields zero
    # originators (seniority undetermined), both stay corroborators.
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=a, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=b, role="corroborates")

    out, summary = _resolve(store, f"[{pub_id}]", format="plain")

    assert "[corA01; corB02]" in out
    assert summary.resolved_count == 1
    assert summary.inflight_pub_ids == []
    fallback_notes = [
        w for w in summary.warnings if w[0] == pub_id and "corroborator" in w[2]
    ]
    assert fallback_notes, f"missing corroborator-fallback note; got {summary.warnings}"


# ── no supporters at all / no cite_keys → in-flight ─────────────────


def test_hub_with_no_supporters_is_inflight(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)

    text = f"see [{pub_id}]."
    out, summary = _resolve(store, text, format="plain")

    assert pub_id in out
    assert summary.resolved_count == 0
    assert pub_id in summary.inflight_pub_ids


def test_hub_with_supporters_but_no_cite_keys_is_inflight(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    no_key = _paper(store, cite_key=None, title="No cite_key paper", year=2001)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=no_key, role="corroborates")

    out, summary = _resolve(store, f"[{pub_id}]", format="plain")

    assert pub_id in out
    assert summary.resolved_count == 0
    assert pub_id in summary.inflight_pub_ids
    skipped_notes = [
        w for w in summary.warnings if w[0] == pub_id and "no cite_key" in w[2]
    ]
    assert skipped_notes, f"missing no-cite_key skip warning; got {summary.warnings}"


def test_hub_inflight_counts_toward_strict_exit(store: Any) -> None:
    """``--strict`` semantics live in ``run()``, but the summary it
    gates on (`inflight_pub_ids`) must include an evidence-less hub —
    pin the contract at the `_resolve_text` level."""
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)

    _out, summary = _resolve(store, f"[{pub_id}]", format="plain")

    assert summary.inflight_pub_ids == [pub_id]


# ── originator skipped for missing cite_key, corroborator fallback used ──


def test_hub_originator_missing_cite_key_falls_back_to_corroborator(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    origin_no_key = _paper(store, cite_key=None, title="Originator, no key", year=2001)
    follow = _paper(store, cite_key="foll10a", title="Follow-up", year=2005)
    corroborator = _paper(
        store, cite_key="side03a", title="Independent corroborator", year=2003
    )
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=origin_no_key, role="corroborates"
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=corroborator, role="corroborates"
    )
    _cites(store, src=follow, dst=origin_no_key)  # origin_no_key is the sole originator

    out, summary = _resolve(store, f"[{pub_id}]", format="plain")

    # The only originator has no cite_key -> falls through to *all*
    # corroborators (follow stayed a corroborator since it wasn't
    # cited by anything in S; ordered by year: corroborator (2003)
    # before follow (2005)).
    assert "[side03a; foll10a]" in out
    assert summary.resolved_count == 1
    skipped_notes = [
        w for w in summary.warnings if w[0] == pub_id and "originator" in w[2]
    ]
    assert skipped_notes, f"missing originator-skip warning; got {summary.warnings}"


# ── regression: non-hub finding still resolves via primary_cite_key ──


def test_non_hub_finding_still_uses_primary_cite_key(store: Any) -> None:
    from precis.dispatch import Hub as DispatchHub
    from precis.handlers.finding import FindingHandler
    from precis.store.types import Tag

    paper_ref = store.insert_ref(
        kind="paper", slug="plain23a", title="a plain paper", meta={}
    )
    handler = FindingHandler(hub=DispatchHub(store=store))
    resp = handler.put(title="t", body="b", scope={}, cited_in="plain23a")
    import re as _re

    ref_id = int(_re.search(r"id=(\d+)", resp.body).group(1))
    pub_id = _re.search(r"pub_id=(\w+)", resp.body).group(1)
    store.update_ref(ref_id, meta_patch={"primary_cite_key": "plain23a"})
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", "established"),
        set_by="chase",
        replace_prefix=True,
    )

    finding = _lookup_finding(store, pub_id)
    assert finding is not None
    assert finding["is_hub"] is False

    out, summary = _resolve(store, f"[{pub_id}]", format="plain")
    assert "[plain23a]" in out
    assert summary.resolved_count == 1
    assert paper_ref.id > 0  # sanity: paper actually landed
