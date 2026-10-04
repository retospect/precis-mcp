"""Grounding freshness at sign (``precis.nanopub.freshness``): supporting
evidence linked after approve froze the grounding blocks ``mint.sign``
(gate ``grounding-stale``) unless ``accept_newer_evidence`` — and signing
over it leaves an audit trail on the hub. DB-backed; keys from env."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.nanopub import evidence, freshness, mint, preflight
from precis.nanopub.keys import generate_keypair
from precis.taproot.hub import attach_evidence
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper

_SENTENCE = "DFT shows MOFs can be anisotropic up to 400:1."


@pytest.fixture(autouse=True)
def _bot_key(monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)


def _approved(store: Any) -> tuple[int, int, int]:
    """An approved (reviewed) hub: ``(hub, paper, chunk)``."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    return hub, paper, chunk


def _attach(
    store: Any,
    hub: int,
    *,
    role: str = "establishes",
    title: str = "A later paper",
    chunk_text: str = "A passage that arrived after approval.",
    paper_level: bool = False,
) -> tuple[int, int, int]:
    """A new paper + one evidence edge onto ``hub``; ``(paper, chunk, link)``."""
    paper, chunk, _sha = _seed_paper(store, title=title, chunk_text=chunk_text)
    meta = {} if paper_level else {"source_handle": f"pc{chunk}"}
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role=role,
        meta=meta,
        check_retraction=False,
    )
    with store.pool.connection() as conn:
        link = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper, hub),
        ).fetchone()
    return paper, chunk, int(link[0])


def _row(store: Any, hub: int) -> Any:
    row = store.nanopub_publish_row(hub)
    assert row is not None
    return row


def _reground_log(store: Any, hub: int) -> list[dict[str, Any]]:
    from precis.taproot.hub import META_REGROUND_LOG

    meta = store.fetch_refs_by_ids([hub])[hub].meta or {}
    return list(meta.get(META_REGROUND_LOG) or [])


def test_approve_stamps_frozen_at_from_the_db_clock(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    row = _row(store, hub)
    stamp = row.grounding["frozen_at"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    # Same now() as updated_at (one UPDATE statement).
    assert abs((parsed - row.updated_at).total_seconds()) < 0.001
    assert freshness.frozen_at(row) == parsed


def test_caller_supplied_frozen_at_is_overwritten(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    payload = _payload(chunk, sha, frozen_at="2001-01-01T00:00:00.000Z")
    mint.approve(store, hub, payload=payload, interactive=True)
    assert _row(store, hub).grounding["frozen_at"] != "2001-01-01T00:00:00.000Z"


def test_no_newer_edges_signs(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []
    assert mint.sign(store, hub).state == "signed"
    assert _reground_log(store, hub) == []


def test_newer_establishes_edge_blocks_sign(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    paper2, chunk2, link = _attach(store, hub)

    edges = freshness.stale_grounding(store, hub, _row(store, hub))
    assert [e.link_id for e in edges] == [link]
    assert edges[0].relation == "establishes"
    assert edges[0].chunk_handle == f"pc{chunk2}"
    assert edges[0].paper_ref_id == paper2

    with pytest.raises(mint.MintGateError) as exc:
        mint.sign(store, hub)
    stale = [v for v in exc.value.violations if v.gate == "grounding-stale"]
    assert len(stale) == 1
    assert f"link {link}" in stale[0].message
    assert f"pc{chunk2}" in stale[0].message
    assert "precis nanopub reopen" in stale[0].message
    assert "--accept-newer-evidence" in stale[0].message
    assert _row(store, hub).state == "reviewed"  # nothing signed


def test_accept_newer_evidence_signs_and_logs(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    paper2, chunk2, link = _attach(store, hub)
    _paper3, _chunk3, link3 = _attach(
        store, hub, role="corroborates", title="Another later paper"
    )

    signed = mint.sign(store, hub, accept_newer_evidence=True)
    assert signed.state == "signed"

    entries = [
        e
        for e in _reground_log(store, hub)
        if e.get("action") == "signed over newer evidence"
    ]
    assert {e["edge"] for e in entries} == {f"link:{link}", f"link:{link3}"}
    by_link = {e["edge"]: e for e in entries}
    first = by_link[f"link:{link}"]
    assert first["relation"] == "establishes"
    assert first["src_ref_id"] == paper2
    assert first["src_chunk_id"] == chunk2
    assert first["sha"] == signed.claim_sha


def test_establishes_listed_before_corroborates(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    # corroborates arrives FIRST, establishes second: establishes still leads.
    _p, _c, corro = _attach(store, hub, role="corroborates", title="Corro paper")
    _p, _c, estab = _attach(store, hub, role="establishes", title="Estab paper")
    edges = freshness.stale_grounding(store, hub, _row(store, hub))
    assert [e.link_id for e in edges] == [estab, corro]


def test_edge_added_before_freeze_is_ignored(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    # A second supporter linked BEFORE approve, but not quoted in the payload.
    _attach(store, hub, title="Pre-freeze paper")
    mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []
    assert mint.sign(store, hub).state == "signed"


def test_edge_on_a_grounded_chunk_is_ignored(store: Any) -> None:
    hub, paper, chunk = _approved(store)
    # Re-attach the already-grounded paper+chunk later (a second role edge).
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        meta={"source_handle": f"pc{chunk}"},
        check_retraction=False,
    )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []


def test_paper_level_edge_from_grounded_source_is_ignored(store: Any) -> None:
    hub, paper, _chunk = _approved(store)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        meta={},
        check_retraction=False,
    )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []


def test_paper_level_edge_from_new_source_is_flagged(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    paper2, _chunk2, link = _attach(store, hub, paper_level=True)
    edges = freshness.stale_grounding(store, hub, _row(store, hub))
    assert [e.link_id for e in edges] == [link]
    assert edges[0].chunk_handle is None
    assert edges[0].paper_ref_id == paper2


def test_other_chunk_of_a_grounded_paper_is_flagged(store: Any) -> None:
    hub, paper, chunk = _approved(store)
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', 1, 'paragraph', "
            "'The TEM caption.', %s) RETURNING chunk_id",
            (paper, ["Results"]),
        ).fetchone()
    other = int(row[0])
    assert other != chunk
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        meta={"source_handle": f"pc{other}"},
        check_retraction=False,
    )
    edges = freshness.stale_grounding(store, hub, _row(store, hub))
    assert [e.chunk_id for e in edges] == [other]


def test_disputes_edge_is_ignored(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    paper2, chunk2, _sha = _seed_paper(store, title="Disputing paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, relation, "
            "set_by) VALUES (%s, %s, %s, 'disputes', 'agent')",
            (paper2, chunk2, hub),
        )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []
    assert mint.sign(store, hub).state == "signed"


def test_envelope_without_frozen_at_falls_back_to_updated_at(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    _p, _c, newer = _attach(store, hub, title="Post-approval paper")
    # An edge that predates the row's updated_at (back-dated an hour).
    _p, _c, older = _attach(store, hub, title="Back-dated paper")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET created_at = created_at - interval '1 hour' "
            "WHERE link_id = %s",
            (older,),
        )
        # An approved-before-this-shipped row: no frozen_at in the envelope.
        conn.execute(
            "UPDATE nanopub_publish SET grounding = grounding - 'frozen_at' "
            "WHERE claim_ref_id = %s",
            (hub,),
        )
    row = _row(store, hub)
    assert "frozen_at" not in row.grounding
    assert freshness.frozen_at(row) == row.updated_at
    edges = freshness.stale_grounding(store, hub, row)
    assert [e.link_id for e in edges] == [newer]


def test_frozen_at_does_not_reach_the_artifact_input(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    row = _row(store, hub)
    assert "frozen_at" in row.grounding
    bundle = evidence.load_bundle(store, hub)
    bare = dataclasses.replace(
        row,
        grounding={k: v for k, v in row.grounding.items() if k != "frozen_at"},
    )
    assert mint._mint_input(store, row, bundle) == mint._mint_input(store, bare, bundle)
    # claim_sha is a hash of the approved title alone.
    from precis.taproot.canon import claim_sha

    assert row.claim_sha == claim_sha(row.approved_title)


def test_preflight_reports_the_stale_edges_only_while_reviewed(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    _p, _c, link = _attach(store, hub)
    issues = [
        i
        for i in preflight.publish_preflight(store, hub)
        if i.check == "grounding-stale"
    ]
    assert len(issues) == 1 and issues[0].blocking
    assert f"link {link}" in issues[0].message

    mint.sign(store, hub, accept_newer_evidence=True)
    assert not [
        i
        for i in preflight.publish_preflight(store, hub)
        if i.check == "grounding-stale"
    ]


def test_sign_stamps_checked_at_and_keeps_it_out_of_the_artifact(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    before = _row(store, hub)
    assert "checked_at" not in before.grounding
    bundle = evidence.load_bundle(store, hub)
    inp_before, _ = mint._mint_input(store, before, bundle)

    signed = mint.sign(store, hub)
    stamp = signed.grounding["checked_at"]
    assert stamp.endswith("Z")
    assert (
        abs(
            (
                datetime.fromisoformat(stamp.replace("Z", "+00:00")) - signed.updated_at
            ).total_seconds()
        )
        < 0.001
    )
    assert signed.grounding["frozen_at"] == before.grounding["frozen_at"]
    inp_after, _ = mint._mint_input(store, signed, bundle)
    assert inp_after == inp_before  # same artifact input with the stamp present


def test_approve_drops_a_caller_supplied_checked_at(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    payload = _payload(chunk, sha, checked_at="2999-01-01T00:00:00.000Z")
    mint.approve(store, hub, payload=payload, interactive=True)
    assert "checked_at" not in _row(store, hub).grounding


def test_resign_after_dependency_flip_flags_only_edges_since_last_sign(
    store: Any,
) -> None:
    hub, _paper, _chunk = _approved(store)
    _p, _c, link_l = _attach(store, hub, title="Edge L paper")
    signed = mint.sign(store, hub, accept_newer_evidence=True)
    assert signed.state == "signed"

    _p, _c, link_m = _attach(store, hub, title="Edge M paper")
    # The topo re-mint dirty flip (check_dependency_drift's write).
    assert store.nanopub_transition(signed.id, to_state="reviewed", expect=("signed",))

    edges = freshness.stale_grounding(store, hub, _row(store, hub))
    assert [e.link_id for e in edges] == [link_m]  # L was confirmed at sign 1
    with pytest.raises(mint.MintGateError) as exc:
        mint.sign(store, hub)
    msg = next(v.message for v in exc.value.violations if v.gate == "grounding-stale")
    assert f"link {link_m}" in msg and f"link {link_l}" not in msg

    assert mint.sign(store, hub, accept_newer_evidence=True).state == "signed"
    logged = {
        e["edge"]
        for e in _reground_log(store, hub)
        if e.get("action") == "signed over newer evidence"
    }
    assert logged == {f"link:{link_l}", f"link:{link_m}"}


def test_resign_with_nothing_new_is_not_blocked_by_its_own_stamp(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    _attach(store, hub)
    signed = mint.sign(store, hub, accept_newer_evidence=True)
    assert store.nanopub_transition(signed.id, to_state="reviewed", expect=("signed",))
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []
    assert mint.sign(store, hub).state == "signed"


def test_edge_pinned_to_a_retired_chunk_is_ignored(store: Any) -> None:
    hub, _paper, _chunk = _approved(store)
    _p, chunk2, _link = _attach(store, hub)
    assert len(freshness.stale_grounding(store, hub, _row(store, hub))) == 1
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (chunk2,)
        )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []


def test_retired_grounding_chunk_still_covers_its_paper(store: Any) -> None:
    hub, paper, chunk = _approved(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (chunk,)
        )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        meta={},
        check_retraction=False,
    )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []


def test_string_chunk_id_in_the_grounding_counts_as_grounded(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    payload = _payload(chunk, sha)
    payload["passages"][0]["chunk_id"] = str(chunk)
    mint.approve(store, hub, payload=payload, interactive=True)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        meta={"source_handle": f"pc{chunk}"},
        check_retraction=False,
    )
    assert freshness.stale_grounding(store, hub, _row(store, hub)) == []


def test_audit_append_failure_does_not_unsign(store: Any, monkeypatch: Any) -> None:
    import precis.taproot.hub as hub_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("log store down")

    hub, _paper, _chunk = _approved(store)
    _attach(store, hub)
    monkeypatch.setattr(hub_mod, "append_reground_log", _boom)
    signed = mint.sign(store, hub, accept_newer_evidence=True)
    assert signed.state == "signed" and signed.trusty_uri
    assert _reground_log(store, hub) == []


def _cli_args(*argv: str) -> Any:
    import argparse

    from precis.cli import nanopub as cli

    top = argparse.ArgumentParser()
    cli.add_parser(top.add_subparsers(dest="cmd", required=True))
    return top.parse_args(["nanopub", *argv])


def test_cli_sign_requires_accept_flag_over_newer_evidence(
    store: Any, capsys: Any
) -> None:
    from precis.cli import nanopub as cli

    hub, _paper, _chunk = _approved(store)
    _p, _c, link = _attach(store, hub)

    with pytest.raises(mint.MintGateError) as exc:
        cli._sign(_cli_args("sign", f"fi{hub}"), store)
    assert "grounding-stale" in str(exc.value)
    assert _row(store, hub).state == "reviewed"

    args = _cli_args("sign", f"fi{hub}", "--accept-newer-evidence")
    assert args.accept_newer_evidence is True
    cli._sign(args, store)
    assert f"signed fi{hub}" in capsys.readouterr().out
    assert _row(store, hub).state == "signed"
    assert any(
        e["edge"] == f"link:{link}"
        for e in _reground_log(store, hub)
        if e.get("action") == "signed over newer evidence"
    )


def test_cli_check_reports_the_stale_edges(
    store: Any, capsys: Any, tmp_path: Any
) -> None:
    import json

    from precis.cli import nanopub as cli

    hub, _paper, _chunk = _approved(store)
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_row(store, hub).grounding), encoding="utf-8")
    args = _cli_args("check", f"fi{hub}", "--payload", str(payload_file))
    cli._check(args, store)
    assert "all mint gates pass" in capsys.readouterr().out

    _p, _c, link = _attach(store, hub)
    with pytest.raises(SystemExit) as exit_:
        cli._check(args, store)
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "[grounding-stale]" in out and f"link {link}" in out


def test_message_caps_the_list_at_ten(store: Any) -> None:
    now = datetime.now(UTC)
    edges = [
        freshness.NewerEdge(
            link_id=i,
            relation="establishes",
            source=f"pa{i}",
            source_title="t",
            paper_ref_id=i,
            chunk_handle=f"pc{i}",
            chunk_id=i,
            created_at=now,
        )
        for i in range(1, 14)
    ]
    text = freshness.format_edges(edges)
    assert text.count("link ") == 10
    assert text.endswith("+3 more")
