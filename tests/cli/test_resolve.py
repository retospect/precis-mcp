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

import argparse
from typing import Any

import pytest

from precis.cli.resolve import _lookup_finding, _resolve_text, add_parser, run
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry

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


# ── Taproot slice A2 — authorial cite pinning ─────────────────────────
#
# A hub cite is a *living default* — resolves to the current derived
# `establishes` set. An author can pin it inline: `[<pub_id>>...]`
# (replace) / `[<pub_id>+...]` (supplement). Purely syntactic — no
# storage, no draft-side edge.


def _paper_chunk(store: Any, ref_id: int, *, ord: int = 0) -> int:
    """Insert a minimal body chunk directly (no ingest pipeline needed) so
    a `pc<chunk_id>` passage handle has something real to resolve to."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, %s, 'paragraph', %s) RETURNING chunk_id",
            (ref_id, ord, "a grounded passage"),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _hub_with_derived_originator(
    store: Any, *, origin_key: str, follow_key: str
) -> tuple[str, int]:
    """A hub whose derived `establishes` originator is the paper
    `origin_key` — mirrors the A1 fixture shape the divergence/replace/
    supplement tests all start from. Returns ``(pub_id, origin_ref_id)``."""
    hub = mint_hub(store, _CLAIM)
    pub_id = _hub_pub_id(store, hub)
    origin = _paper(store, cite_key=origin_key, title="Original report", year=2001)
    follow = _paper(store, cite_key=follow_key, title="Follow-up", year=2005)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    _cites(store, src=follow, dst=origin)  # follow cites origin -> origin is originator
    return pub_id, origin


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_parser(sub)
    return parser


def test_pin_replace_cites_pinned_handle_not_derived_originator(store: Any) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig30a", follow_key="foll30a"
    )
    pinned = _paper(store, cite_key="pin30a", title="Author's pick")
    handle = handle_registry.format_handle("paper", pinned)

    out, summary = _resolve(store, f"[{pub_id}>{handle}]", format="plain")

    assert "[pin30a]" in out
    assert "orig30a" not in out
    assert summary.resolved_count == 1


def test_pin_supplement_adds_to_derived_originators(store: Any) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig31a", follow_key="foll31a"
    )
    pinned = _paper(store, cite_key="pin31a", title="Extra evidence")
    handle = handle_registry.format_handle("paper", pinned)

    out, summary = _resolve(store, f"[{pub_id}+{handle}]", format="plain")

    assert "[orig31a; pin31a]" in out
    assert summary.resolved_count == 1
    # A supplement is purely additive — its handle set (just pin31a) always
    # differs from the full derived originator set (orig31a), and that must
    # NOT be flagged as a divergence (supplement has no divergence concept).
    assert summary.pin_divergences == []
    assert summary.diverged_pub_ids == []


def test_pin_supplement_never_diverges_even_when_handles_differ_from_derived(
    store: Any,
) -> None:
    """Regression for the reviewed bug: a supplement pin whose own handles
    differ from the full derived `establishes` set is correct usage, not a
    divergence — `+` never fires the advisory, only `>` does."""
    pub_id, origin = _hub_with_derived_originator(
        store, origin_key="orig41a", follow_key="foll41a"
    )
    pinned = _paper(store, cite_key="pin41a", title="Extra evidence")
    handle = handle_registry.format_handle("paper", pinned)

    _out, summary = _resolve(store, f"[{pub_id}+{handle}]", format="plain")

    assert summary.pin_divergences == []
    assert summary.diverged_pub_ids == []
    assert origin > 0  # sanity: the derived originator really differs from pin41a


def test_strict_pins_does_not_exit_on_supplement_pin(store: Any) -> None:
    """A `--strict-pins` run over a legitimate supplement pin must exit 0 —
    supplement never diverges, so it never trips the gate."""
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig42a", follow_key="foll42a"
    )
    pinned = _paper(store, cite_key="pin42a", title="Extra evidence")
    handle = handle_registry.format_handle("paper", pinned)

    ns = _parser().parse_args(
        [
            "resolve",
            "--text",
            f"[{pub_id}+{handle}]",
            "--strict-pins",
            "--database-url",
            store.pool.conninfo,
        ]
    )
    run(ns)  # must not raise SystemExit


def test_pin_supplement_dedups_when_pin_matches_derived(store: Any) -> None:
    pub_id, origin = _hub_with_derived_originator(
        store, origin_key="orig32a", follow_key="foll32a"
    )
    handle = handle_registry.format_handle("paper", origin)

    out, summary = _resolve(store, f"[{pub_id}+{handle}]", format="plain")

    assert out.count("orig32a") == 1  # not duplicated
    assert summary.resolved_count == 1


def test_pin_passage_handle_resolves_to_parent_paper(store: Any) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig33a", follow_key="foll33a"
    )
    pinned = _paper(store, cite_key="pin33a", title="Grounded passage source")
    chunk_id = _paper_chunk(store, pinned)
    handle = f"pc{chunk_id}"

    out, summary = _resolve(store, f"[{pub_id}>{handle}]", format="plain")

    assert "[pin33a]" in out
    assert summary.resolved_count == 1


def test_pin_divergence_advisory_fires_when_pinned_differs_from_derived(
    store: Any,
) -> None:
    pub_id, origin = _hub_with_derived_originator(
        store, origin_key="orig34a", follow_key="foll34a"
    )
    pinned = _paper(store, cite_key="pin34a", title="Author's pick")
    handle = handle_registry.format_handle("paper", pinned)

    _out, summary = _resolve(store, f"[{pub_id}>{handle}]", format="plain")

    assert summary.diverged_pub_ids == [pub_id]
    assert len(summary.pin_divergences) == 1
    message = summary.pin_divergences[0]
    assert pub_id in message
    assert "reconsider" in message
    assert handle in message
    assert handle_registry.format_handle("paper", origin) in message


def test_pin_matching_derived_originator_no_divergence(store: Any) -> None:
    pub_id, origin = _hub_with_derived_originator(
        store, origin_key="orig35a", follow_key="foll35a"
    )
    handle = handle_registry.format_handle("paper", origin)

    _out, summary = _resolve(store, f"[{pub_id}>{handle}]", format="plain")

    assert summary.diverged_pub_ids == []
    assert summary.pin_divergences == []


def test_strict_pins_exits_nonzero_on_divergence(store: Any) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig36a", follow_key="foll36a"
    )
    pinned = _paper(store, cite_key="pin36a", title="Author's pick")
    handle = handle_registry.format_handle("paper", pinned)

    ns = _parser().parse_args(
        [
            "resolve",
            "--text",
            f"[{pub_id}>{handle}]",
            "--strict-pins",
            "--database-url",
            store.pool.conninfo,
        ]
    )
    with pytest.raises(SystemExit) as exc:
        run(ns)
    assert exc.value.code == 3


def test_without_strict_pins_divergence_is_advisory_only(
    store: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig37a", follow_key="foll37a"
    )
    pinned = _paper(store, cite_key="pin37a", title="Author's pick")
    handle = handle_registry.format_handle("paper", pinned)

    ns = _parser().parse_args(
        [
            "resolve",
            "--text",
            f"[{pub_id}>{handle}]",
            "--database-url",
            store.pool.conninfo,
        ]
    )
    run(ns)  # no SystemExit — advisory only

    err = capsys.readouterr().err
    assert "reconsider" in err


def test_pin_unresolvable_handle_falls_back_to_derived_hub_resolution(
    store: Any,
) -> None:
    pub_id, _origin = _hub_with_derived_originator(
        store, origin_key="orig38a", follow_key="foll38a"
    )
    bogus_handle = "pa9999999"  # no such ref_id

    out, summary = _resolve(store, f"[{pub_id}>{bogus_handle}]", format="plain")

    assert "[orig38a]" in out
    assert summary.resolved_count == 1
    skip_notes = [
        w for w in summary.warnings if w[0] == pub_id and "did not resolve" in w[2]
    ]
    assert skip_notes, f"missing pin-skip warning; got {summary.warnings}"
    fallback_notes = [
        w for w in summary.warnings if w[0] == pub_id and "falling back" in w[2]
    ]
    assert fallback_notes, f"missing pin-fallback warning; got {summary.warnings}"


def test_pin_ignored_on_non_hub_finding(store: Any) -> None:
    import re as _re

    from precis.dispatch import Hub as DispatchHub
    from precis.handlers.finding import FindingHandler
    from precis.store.types import Tag

    paper_ref = store.insert_ref(
        kind="paper", slug="plain39a", title="a plain paper", meta={}
    )
    handler = FindingHandler(hub=DispatchHub(store=store))
    resp = handler.put(title="t", body="b", scope={}, cited_in="plain39a")
    ref_id = int(_re.search(r"id=(\d+)", resp.body).group(1))
    pub_id = _re.search(r"pub_id=(\w+)", resp.body).group(1)
    store.update_ref(ref_id, meta_patch={"primary_cite_key": "plain39a"})
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", "established"),
        set_by="chase",
        replace_prefix=True,
    )

    handle = handle_registry.format_handle("paper", paper_ref.id)
    out, summary = _resolve(store, f"[{pub_id}>{handle}]", format="plain")

    assert "[plain39a]" in out  # resolved normally, pin ignored
    assert summary.resolved_count == 1
    ignore_notes = [
        w for w in summary.warnings if w[0] == pub_id and w[1] == "pin-ignored"
    ]
    assert ignore_notes, f"missing pin-ignored warning; got {summary.warnings}"


def test_plain_pub_id_unaffected_by_extended_pin_regex(store: Any) -> None:
    """Regression: the extended ``PLACEHOLDER_RE`` (Taproot slice A2)
    still parses a bare, unpinned ``[pub_id]`` exactly as before."""
    pub_id, origin = _hub_with_derived_originator(
        store, origin_key="orig40a", follow_key="foll40a"
    )

    out, summary = _resolve(store, f"[{pub_id}]", format="plain")

    assert "[orig40a]" in out
    assert summary.resolved_count == 1
    assert summary.pin_divergences == []
    assert summary.diverged_pub_ids == []
    assert origin > 0  # sanity
