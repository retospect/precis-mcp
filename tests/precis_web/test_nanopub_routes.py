"""The /nanopub workbench + the merged claim page's review-and-sign
section: claim forest, interactive doors (approve/sign/signoff), and
exact-bytes TriG serving. ``/nanopub/fi<id>`` is the workbench DEEP LINK
(full workbench, review pane preloaded); the claim content itself lives
at ``/claim/fi<id>`` (nanopub-light-up merge), which content assertions
here hit directly. POST doors stay at ``/nanopub/fi<id>/...``.
Real DB-backed store (the routes run real SQL through the nanopub
mixin + overview/preflight modules)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.nanopub.keys import generate_keypair
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper
from tests.workers._helpers import seed_ref


@pytest.fixture
def client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _store(runtime: Any) -> Any:
    return runtime.store


def test_index_is_the_three_pane_tree(client: TestClient, runtime_with_store) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A queue-table claim.", paper, chunk)
    resp = client.get("/nanopub")
    assert resp.status_code == 200
    assert f"fi{hub}" in resp.text
    assert "unminted" in resp.text
    # Hub rows load the review pane. The panes are DIVS in this document,
    # not iframes: every mechanism a framed pane needed (named targets,
    # window.parent reach, ?embed=1 threading, Basic-auth replay into a
    # subframe) is one Safari drops or refuses, and each was a dead click.
    assert f'data-src="/claim/fi{hub}"' in resp.text
    assert 'id="np-review"' in resp.text
    assert 'id="np-paper"' in resp.text
    assert "<iframe" not in resp.text
    # No pane URL carries ?embed=1 any more — a pane asks for chrome-less
    # HTML with a header, so nothing has to be threaded through the URL.
    assert "embed=1" not in resp.text
    # Pane links are driven by the page's own loader. data-pane names the
    # destination; there is no target= to be dropped.
    assert "const loadPane" in resp.text
    assert 'data-pane="np-review"' in resp.text
    assert 'target="np-review"' not in resp.text
    # The anchor href is the workbench deep link (cmd-click keeps the nav
    # context); data-src is what a plain click swaps into the pane.
    assert f'data-pane="np-review" href="/nanopub/fi{hub}"' in resp.text
    assert "a.dataset.src" in resp.text
    # The browser URL follows the review pane, and back/forward work.
    assert "history.pushState" in resp.text
    assert 'window.addEventListener("popstate"' in resp.text
    assert resp.text.count("data-placeholder=") == 2
    # Claim titles wrap to a two-line clamp instead of a one-line truncate
    # (a 500px pane showed only the first few words of a claim sentence).
    assert 'class="np-title' in resp.text
    assert ".np-title" in resp.text
    # The old tree URL redirects home.
    tree = client.get("/nanopub/tree", follow_redirects=False)
    assert tree.status_code == 307 and tree.headers["location"] == "/nanopub"
    # /nanopub/fi<id> is the workbench DEEP LINK: the full workbench with
    # the review pane preloaded on that claim (nav + tree included).
    deep = client.get(f"/nanopub/fi{hub}", follow_redirects=False)
    assert deep.status_code == 200
    # The preload is an URL the shell swaps in on load, not an iframe src;
    # the bare index has none.
    assert f'data-preload="/claim/fi{hub}"' in deep.text
    assert "data-preload=" not in resp.text
    assert 'id="np-review"' in deep.text


def test_an_htmx_request_gets_the_chromeless_fragment(
    client: TestClient, runtime_with_store
) -> None:
    """A workbench pane asks for a claim with a header, not a URL flag.

    Branching on ``HX-Request`` rather than on ``?embed=1`` (or a distinct
    /fragment URL) is what keeps the permalink resolvers working: /c/<handle>
    and /r/paper/<id> 303 INTO the claim and paper routes, and the browser
    replays the header across a same-origin redirect where a query param
    would have to be threaded by hand through every hop.
    """
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "An embedded claim.", paper, chunk)
    full = client.get(f"/claim/fi{hub}")
    frag = client.get(f"/claim/fi{hub}", headers={"HX-Request": "true"})
    assert "<header" in full.text
    assert "<header" not in frag.text
    assert "<html" not in frag.text
    # Same content either way — the fragment IS the full page's body.
    assert "An embedded claim." in frag.text
    assert "An embedded claim." in full.text
    # The paper reader answers the same way, and drops its own <script src>
    # in pane mode (the shell loads paper-viewer.js once).
    paper_frag = client.get(f"/papers/{paper}", headers={"HX-Request": "true"})
    assert "<header" not in paper_frag.text
    assert "/static/paper-viewer.js" not in paper_frag.text
    assert "/static/paper-viewer.js" in client.get(f"/papers/{paper}").text
    # ?embed=1 is gone: it must not suppress the chrome any more. (Not an
    # exact-text compare — each render mints fresh popover ids.)
    assert "<header" in client.get(f"/claim/fi{hub}?embed=1").text
    # The named-window opener still rides EVERY page — it serves
    # target="precis-paper" links outside the workbench (the smartdraft
    # reader, a claim page in its own tab), where Safari silently drops an
    # <a target="name"> navigation to a window that doesn't exist yet.
    assert "window.open(a.href, t)" in full.text
    assert 'window.open(a.href, "_blank")' in full.text
    # …but it no longer knows anything about pane names.
    assert "np-review" not in full.text


def test_hub_page_shows_state_and_action(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A reviewable claim.", paper, chunk)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert "A reviewable claim." in resp.text
    assert "Approve" in resp.text  # unminted → approve action
    # Routing by DESTINATION now lives in the workbench itself, because the
    # panes are its own divs: paper-destined links (a precis-paper target,
    # /papers/, and the permalink resolvers /c/<handle> and /r/paper/<id>,
    # which 303 onto a document) swap the paper pane; claim links swap the
    # review pane. Nothing is emitted onto the claim page to make this work.
    workbench = client.get("/nanopub")
    assert 'u.pathname.startsWith("/papers/")' in workbench.text
    assert 'u.pathname.startsWith("/c/")' in workbench.text
    assert 'u.pathname.startsWith("/r/paper/")' in workbench.text
    assert 'u.pathname.startsWith("/claim/")' in workbench.text
    assert 'target === "precis-paper"' in workbench.text
    # The claim page carries no pane-routing script of its own — that
    # duplication (routing on claim pages only) is what left the paper
    # pane's own pa/pc links dead.
    assert "np-paper" not in resp.text
    # Non-hub → the claim page's own friendly "no claim hub" stub (200),
    # not a 404 — the merged page's degrade-gracefully policy, not an error.
    other = _seed_paper(store)[0]
    resp = client.get(f"/claim/fi{other}")
    assert resp.status_code == 200
    assert "No claim hub" in resp.text


def test_no_page_reaches_across_a_frame_boundary(
    client: TestClient, runtime_with_store
) -> None:
    """The whole class of cross-frame mechanism is gone, not just its bugs.

    Framed panes needed named-target navigation, ``window.parent`` reach,
    and a self-correcting placement pass to undo misroutes (gr264349 —
    a claim landing in the paper pane read as the review column
    duplicated). None of it survives a single-document workbench, and this
    pins that none of it comes back.
    """
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A misrouted claim.", paper, chunk)
    pages = [
        client.get("/nanopub"),
        client.get(f"/nanopub/fi{hub}"),
        client.get(f"/claim/fi{hub}"),
        client.get(f"/papers/{paper}"),
    ]
    for page in pages:
        assert "window.parent" not in page.text
        assert "window.name" not in page.text
        assert "about:blank" not in page.text
        assert "srcdoc" not in page.text
    # The workbench itself frames nothing at all now. (The one iframe that
    # legitimately remains is the vendored pdf.js viewer on the paper
    # reader, and only when a PDF is actually on disk — the seeded paper
    # here has none, so test_papers_nav.py pins that one.)
    assert "<iframe" not in client.get("/nanopub").text


def test_approve_prefill_suggests_quote_and_unique_snip(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A prefilled claim.", paper, chunk)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    # The candidate quote is the first citation-marker-free sentence of
    # the grounding chunk ("Tensorial analysis." is too short), and the
    # snip is a validated-unique token window — not empty placeholders.
    assert "This anisotropy can reach a 400:1 ratio" in resp.text
    assert "this anisotropy can reach a 400 1 ratio" in resp.text


def test_prefill_doi_comes_from_ref_identifiers(
    client: TestClient, runtime_with_store
) -> None:
    """DOIs live in ``ref_identifiers(id_kind='doi')`` — in prod,
    ``refs.meta['doi']`` is a legacy spot 3 of ~33k papers carry. The
    evidence bundle must read the identifiers table or every prefill
    passage ships ``"doi": ""`` and the mint gate (which refuses
    DOI-less passages) blocks publishing entirely."""
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store, doi="10.5555/regress.1")
    hub = _seed_hub(store, "A doi-carrying claim.", paper, chunk)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    # The approve prefill's passage carries the identifiers-table DOI…
    assert "10.5555/regress.1" in resp.text
    # …and so does the evidence DAG's source-node DOI link.
    assert "https://doi.org/10.5555/regress.1" in resp.text


def test_prefill_doi_falls_back_to_legacy_meta(
    client: TestClient, runtime_with_store
) -> None:
    """The 3-of-33k prod shape: no identifiers row, DOI parked on
    ``refs.meta['doi']``. The fallback must still surface it."""
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id=%s AND id_kind='doi'",
            (paper,),
        )
        conn.execute(
            "UPDATE refs SET meta = coalesce(meta, '{}'::jsonb) || "
            "jsonb_build_object('doi', '10.9999/legacy.7') WHERE ref_id=%s",
            (paper,),
        )
    hub = _seed_hub(store, "A legacy-doi claim.", paper, chunk)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert "10.9999/legacy.7" in resp.text


def test_prefill_covers_derived_from_lineage_anchor(
    client: TestClient, runtime_with_store
) -> None:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    store = _store(runtime_with_store)
    paper, _chunk, _sha = _seed_paper(store)
    hub = mint_hub(
        store, CanonicalClaim(sentence="A lineage-grounded claim.", scope={})
    )
    # No inbound evidence edge — the ONLY grounding is the outbound
    # lineage pin (dst_pos resolves to the chunk), fi19981's shape.
    store.add_link(src_ref_id=hub, dst_ref_id=paper, relation="derived-from", dst_pos=0)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert "This anisotropy can reach a 400:1 ratio" in resp.text


def test_prefill_skips_heading_residue_and_ranks_by_claim(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(
        store,
        chunk_text=(
            "**1. Introduction**\n\nThe debate over such matters remains deeply "
            "contentious among practitioners. The anisotropy ratio doubles "
            "roughly every two years in layered crystals."
        ),
    )
    hub = _seed_hub(store, "Anisotropy ratio doubles every two years.", paper, chunk)
    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    # The heading fragment is disqualified and the claim-relevant sentence
    # beats the earlier meta-discourse one.
    assert "The anisotropy ratio doubles roughly every two years" in resp.text
    assert "Introduction**" not in resp.text


def _seed_hypothesis(
    store: Any, sentence: str, motivator_ref: int, motivator_chunk: int
) -> tuple[int, int]:
    """A minted hypothesis hub (via the real ``put(hypothesis=True, …)``
    door), plus a second motivating paper. Returns ``(hub, other_paper)``.

    ``sentence`` must lint clean — the door runs `check_claim_sentence`
    before it writes anything, so it needs an evidence verb AND an
    epistemic-mode token or nothing is minted at all.
    """
    from precis.dispatch import Hub
    from precis.handlers.finding import FindingHandler

    other = seed_ref(store, title="A second motivating paper", kind="paper")
    resp = FindingHandler(hub=Hub(store=store)).put(
        title=sentence,
        hypothesis=True,
        motivation="Both sources attribute the effect to the same mechanism; "
        "the transfer here is untested.",
        testable_by="an experiment discriminating the two candidate mechanisms",
        motivated_by=[f"pc{motivator_chunk}", f"pa{other}"],
        llm_models=["test-model"],
    )
    hub = int(resp.body.split("fi", 1)[1].split()[0])
    return hub, other


def test_approve_prefill_uses_the_proposed_payload_for_a_hypothesis(
    client: TestClient, runtime_with_store
) -> None:
    """A human opening ``/claim/fi<id>`` for an agent-proposed hypothesis
    finds the approve textarea already filled in from
    ``refs.meta.proposed_payload`` — not the ordinary passage-candidate
    derivation (a hypothesis has no grounding chunks to derive from)."""
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub, _other = _seed_hypothesis(
        store, "DFT predicts a 12% modulus rise under uniaxial strain.", paper, chunk
    )

    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    # Quotes are autoescaped in the textarea (test_approve_gate_failure_is_a_400
    # precedent) — assert on quote-free content from the parked payload.
    assert "the transfer here is untested" in resp.text
    assert "an experiment discriminating the two candidate mechanisms" in resp.text


def test_approve_prefill_frozen_payload_wins_over_proposed(
    client: TestClient, runtime_with_store
) -> None:
    """Once a publish row carries a frozen ``grounding`` payload, it still
    wins over the parked proposal — the proposal is a starting point, not a
    ledger. The approve-form textarea only renders while the row is still
    ``candidate`` (state 'reviewed'+ swaps the whole action panel to Sign),
    so this writes ``grounding`` directly rather than going through
    ``nanopub_approve`` (which also flips the state)."""
    from psycopg.types.json import Jsonb

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub, _other = _seed_hypothesis(
        store, "Raman shows a G-band shift in the annealed films.", paper, chunk
    )
    row = store.nanopub_create_publish_row(hub, artifact_type="hypothesis")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE nanopub_publish SET grounding = %s WHERE id = %s",
            (Jsonb({"passages": [], "fields": {}, "frozen-marker": True}), row.id),
        )

    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert "frozen-marker" in resp.text
    # The proposed payload's motivation prose is gone — the frozen payload
    # fully replaced it in the textarea.
    assert "the transfer here is untested" not in resp.text


def test_claim_page_labels_a_proposed_hub_hypothesis(
    client: TestClient, runtime_with_store
) -> None:
    """`bundle.artifact_type` can only ever be ``claim``/``compound``
    (:func:`precis.nanopub.evidence.load_bundle`) — the DAG's hub-node
    label reads the durable meta marker instead, so a proposed hypothesis
    with no publish row yet still shows up as one."""
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub, _other = _seed_hypothesis(
        store, "TEM observes lattice fringes at the junction interface.", paper, chunk
    )

    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert "hypothesis · unminted" in resp.text


def test_approve_sign_and_serve_trig(
    client: TestClient, runtime_with_store, monkeypatch: Any
) -> None:
    import json

    store = _store(runtime_with_store)
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT finds the web-signed claim holds.", paper, chunk)

    resp = client.post(
        f"/nanopub/fi{hub}/approve",
        data={
            "title": "DFT finds the web-signed claim holds.",
            "payload": json.dumps(_payload(chunk, sha)),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert store.nanopub_publish_row(hub).state == "reviewed"

    resp = client.post(f"/nanopub/fi{hub}/sign", follow_redirects=False)
    assert resp.status_code == 303
    row = store.nanopub_publish_row(hub)
    assert row.state == "signed" and row.trusty_uri

    code = row.trusty_uri.rsplit("/", 1)[-1]
    trig = client.get(f"/np/{code}")
    assert trig.status_code == 200
    assert trig.headers["content-type"].startswith("application/trig")
    artifact = store.nanopub_artifact(row.artifact_id)
    assert trig.content == artifact.trig_bytes  # the exact frozen bytes
    assert client.get("/np/RAnope").status_code == 404
    # LIKE-wildcard probe: a bare '%' must 404, never match "any artifact".
    assert client.get("/np/%25").status_code == 404


def test_attesting_sign_refuses_without_an_account_orcid(
    client: TestClient, runtime_with_store, monkeypatch: Any
) -> None:
    """``attest=1`` with nobody signed in is a stop, not a bot signature.

    An attestation names a person. This app runs with the gate off, so
    there is no account and therefore no iD to attribute the claim to —
    the same refusal a signed-in user with an empty ``/account`` ORCID
    field gets.
    """
    import json

    store = _store(runtime_with_store)
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_ATTESTING_PRIVATE_KEY", priv)
    monkeypatch.setenv(
        "NANOPUB_ATTESTING_ORCID", "https://orcid.org/0000-0002-1825-0097"
    )
    paper, chunk, sha = _seed_paper(store)
    title = "DFT finds the unattributed claim holds."
    hub = _seed_hub(store, title, paper, chunk)
    approved = client.post(
        f"/nanopub/fi{hub}/approve",
        data={"title": title, "payload": json.dumps(_payload(chunk, sha))},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    resp = client.post(f"/nanopub/fi{hub}/sign", data={"attest": "1"})
    assert resp.status_code == 400
    assert "ORCID" in resp.text
    # Refused before the key was opened: the row is untouched, so the
    # claim can still be signed once an identity exists.
    assert store.nanopub_publish_row(hub).state == "reviewed"


def test_claim_page_shows_review_section_with_publish_row(
    client: TestClient, runtime_with_store
) -> None:
    """The merged ``/claim/fi<id>`` page (not ``/nanopub/fi<id>``) carries
    the review-and-sign section: state header, frozen ladder, DAG, and the
    publish-row panel — for a hub that already has a publish row, not just
    a fresh unminted one."""
    import json

    store = _store(runtime_with_store)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT finds the claim-page-reviewed claim holds.", paper, chunk
    )

    resp = client.post(
        f"/nanopub/fi{hub}/approve",
        data={
            "title": "DFT finds the claim-page-reviewed claim holds.",
            "payload": json.dumps(_payload(chunk, sha)),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/claim/fi{hub}"

    resp = client.get(f"/claim/fi{hub}")
    assert resp.status_code == 200
    assert 'id="review"' in resp.text
    assert "Review &amp; sign" in resp.text
    assert "reviewed" in resp.text
    assert "frozen: string" in resp.text
    assert (
        "DFT finds the claim-page-reviewed claim holds." in resp.text
    )  # approved title
    assert 'name="attest"' in resp.text  # the Sign action's form


def test_claim_page_shows_maturity_ladder_and_full_gate_list(
    client: TestClient, runtime_with_store
) -> None:
    """The flow graph (candidate → … → published, left to right, hover
    tips) tops the claim page, and the Gates panel lists EVERY gate with
    a status — not just failures: mint gates read passed the moment the
    state left candidate (approve refuses on any violation), preflight
    checks read the live issue list."""
    import json

    store = _store(runtime_with_store)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT finds the ladder claim holds.", paper, chunk)

    resp = client.get(f"/claim/fi{hub}")
    assert 'aria-label="claim maturity"' in resp.text
    for step in ("candidate", "reviewed", "signed", "anchored", "published"):
        assert step in resp.text
    # Hovering the reviewed rung explains what approve checked.
    assert "EVERY Layer-A mint gate ran and passed" in resp.text
    # The gates panel names both groups and the individual gates…
    assert "Publish preflight (live)" in resp.text
    assert "[primary-source]" in resp.text
    assert "[quote-verbatim]" in resp.text
    assert "[withheld-edge]" in resp.text
    # …and pre-approve the mint gates are a live DRY-RUN against the
    # current sentence + prefilled grounding — real per-gate standing
    # ("how does this candidate stack up?"), not a wall of pending; only
    # the publish-preflight rows stay pending (○) until a row exists.
    assert "Mint gates — live dry-run" in resp.text
    assert "passing (dry-run)" in resp.text
    assert "✓" in resp.text
    assert "○" in resp.text
    # The dry-run ran against the REAL prefilled grounding (quote + snip
    # derived from the seeded chunk), so this well-grounded hub shows no
    # failing gate at all — an empty/wrong payload would red grounding
    # and schema-lint here.
    assert "✖" not in resp.text

    approved = client.post(
        f"/nanopub/fi{hub}/approve",
        data={
            "title": "DFT finds the ladder claim holds.",
            "payload": json.dumps(_payload(chunk, sha)),
        },
        follow_redirects=False,
    )
    assert approved.status_code == 303
    resp = client.get(f"/claim/fi{hub}")
    # Reviewed: the dry-run framing is gone — every mint gate reads
    # passed for real (✓, "run at approve") and the live preflight still
    # blocks on state (not anchored yet) — the summary says so.
    assert "Mint gates (run at approve)" in resp.text
    assert "Mint gates — live dry-run" not in resp.text
    assert "passing (dry-run)" not in resp.text
    assert "✓" in resp.text
    assert "blocking" in resp.text


def test_approve_gate_failure_is_a_400_not_a_500(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A gate-failing claim.", paper, chunk)
    resp = client.post(
        f"/nanopub/fi{hub}/approve",
        data={"title": "", "payload": '{"passages": [], "hand-trimmed-marker": 1}'},
        follow_redirects=False,
    )
    assert resp.status_code == 400  # no-source-no-atom gate fires
    # The refusal re-renders the form with the reviewer's edits intact —
    # a gate failure must not eat a hand-trimmed payload. (Quotes are
    # autoescaped in the textarea, so assert on a quote-free marker.)
    assert "hand-trimmed-marker" in resp.text
    assert "✖" in resp.text  # the violation banner
    assert f"/nanopub/fi{hub}/approve" in resp.text  # the form is back


def test_signoff_door_from_the_web(client: TestClient, runtime_with_store) -> None:
    from precis.nanopub.preflight import withheld_edges

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A signoff claim.", paper, chunk)
    edges = withheld_edges(store, hub)
    assert len(edges) == 1
    # Note required — refused loudly.
    resp = client.post(
        f"/nanopub/fi{hub}/signoff/{edges[0].link_id}",
        data={"note": " "},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/nanopub/fi{hub}/signoff/{edges[0].link_id}",
        data={"note": "read it, checks out"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert withheld_edges(store, hub) == []


def test_evidence_add_and_remove_doors(client: TestClient, runtime_with_store) -> None:
    from precis.nanopub.preflight import withheld_edges

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A curated claim.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)

    resp = client.post(
        f"/nanopub/fi{hub}/evidence/add",
        data={
            "source": f"pa{paper2}",
            "chunk": f"pc{chunk2}",
            "relation": "corroborates",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    edges = withheld_edges(store, hub)
    assert len(edges) == 2  # seeded edge + the new one, both un-vouched
    new = next(e for e in edges if e.paper_ref_id == paper2)

    resp = client.post(
        f"/nanopub/fi{hub}/evidence/{new.link_id}/remove", follow_redirects=False
    )
    assert resp.status_code == 303
    assert [e.paper_ref_id for e in withheld_edges(store, hub)] == [paper]
    # Gone means gone — a second remove is a 400, not a silent 303.
    resp = client.post(
        f"/nanopub/fi{hub}/evidence/{new.link_id}/remove", follow_redirects=False
    )
    assert resp.status_code == 400

    # Junk source and a chunk from the wrong paper refuse with 400.
    for data in (
        {"source": "nope", "relation": "corroborates"},
        {"source": f"pa{paper2}", "chunk": f"pc{chunk}", "relation": "corroborates"},
    ):
        resp = client.post(
            f"/nanopub/fi{hub}/evidence/add", data=data, follow_redirects=False
        )
        assert resp.status_code == 400


def test_tree_nests_conjunct_atom_under_compound(
    client: TestClient, runtime_with_store
) -> None:
    from precis.nanopub.overview import hub_tree
    from precis.taproot.hub import link_claims

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    compound = _seed_hub(store, "A compound tree claim.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    atom = _seed_hub(store, "An atomic tree claim.", paper2, chunk2)
    assert link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )

    roots = hub_tree(store)
    root_ids = {n.row.ref_id for n in roots}
    assert compound in root_ids and atom not in root_ids  # atom is nested
    node = next(n for n in roots if n.row.ref_id == compound)
    assert [c.row.ref_id for c in node.children] == [atom]
    assert node.children[0].relation == "conjunct-of"
    # Evidence papers hang as leaves on both nodes.
    assert {e.relation for e in node.evidence} == {"corroborates"}
    assert node.children[0].evidence

    resp = client.get("/nanopub")
    assert resp.status_code == 200
    assert f"fi{compound}" in resp.text and f"fi{atom}" in resp.text
    assert "A compound tree claim." in resp.text
    # Evidence leaves target the paper pane via the paper reader, not the
    # kindless /refs/<id> shape (which 400s).
    assert f'data-src="/papers/{paper}"' in resp.text
    assert f"/refs/{paper}" not in resp.text
    # Both hubs load the review pane.
    assert f'data-src="/claim/fi{compound}"' in resp.text
    assert f'data-src="/claim/fi{atom}"' in resp.text


def test_tree_cycle_is_cut_not_recursed(client: TestClient, runtime_with_store) -> None:
    from precis.taproot.hub import link_claims

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    a = _seed_hub(store, "Cycle claim A.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    b = _seed_hub(store, "Cycle claim B.", paper2, chunk2)
    assert link_claims(store, from_hub_ref_id=a, to_hub_ref_id=b, relation="refines")
    assert link_claims(store, from_hub_ref_id=b, to_hub_ref_id=a, relation="refines")

    resp = client.get("/nanopub")  # must terminate, not recurse forever
    assert resp.status_code == 200
    assert f"fi{a}" in resp.text and f"fi{b}" in resp.text


def _seed_draft(store: Any, title: str = "A citing draft") -> int:
    return seed_ref(store, title=title, kind="draft")


def test_draft_filter_scopes_forest_and_tally(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    cited = _seed_hub(store, "A cited-by-draft claim.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    uncited = _seed_hub(store, "An uncited claim.", paper2, chunk2)
    draft = _seed_draft(store)
    # sync_draft_links' own shape: a chunk-grounded outbound cites edge,
    # draft --cites--> hub.
    store.add_link(src_ref_id=draft, dst_ref_id=cited, relation="cites")

    unfiltered = client.get("/nanopub")
    assert f"fi{cited}" in unfiltered.text and f"fi{uncited}" in unfiltered.text

    resp = client.get(f"/nanopub?draft=dr{draft}")
    assert resp.status_code == 200
    assert f"fi{cited}" in resp.text
    assert f"fi{uncited}" not in resp.text
    assert f"draft dr{draft}" in resp.text and "1 claim" in resp.text
    assert "2 hub" not in resp.text  # tally scoped, not the global count

    # Bare numeric (no 'dr' prefix) parses the same way.
    bare = client.get(f"/nanopub?draft={draft}")
    assert f"fi{cited}" in bare.text and f"fi{uncited}" not in bare.text


def test_draft_filter_keeps_full_subtree_of_a_cited_compound(
    client: TestClient, runtime_with_store
) -> None:
    from precis.taproot.hub import link_claims

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    compound = _seed_hub(store, "A cited compound claim.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    atom = _seed_hub(store, "Its uncited-directly atom.", paper2, chunk2)
    assert link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )
    draft = _seed_draft(store)
    # The draft cites only the compound — never the atom directly.
    store.add_link(src_ref_id=draft, dst_ref_id=compound, relation="cites")

    resp = client.get(f"/nanopub?draft=dr{draft}")
    assert resp.status_code == 200
    # The compound's full subtree stays visible — reviewing everything
    # under what the draft invokes, not just the literal cite target.
    assert f"fi{compound}" in resp.text and f"fi{atom}" in resp.text
    # And the tally counts the DISPLAYED set: the retained atom is real
    # sign work (atoms publish before their compound), so the chip says
    # 2 claims, not 1.
    assert "— 2 claims" in resp.text


def test_resolvers_thread_nothing_and_answer_htmx_with_a_fragment(
    client: TestClient, runtime_with_store
) -> None:
    """The paper pane routes evidence clicks through /r and /c, and those
    resolvers now carry NOTHING on their 303s.

    The pane bit rides the request instead: an ``HX-Request`` header is
    replayed by the browser across a same-origin redirect, so following the
    chain end-to-end lands on the chrome-less reader. That is what makes it
    safe to delete the query-param threading these two routes used to do by
    hand at every hop.
    """
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hx = {"HX-Request": "true"}

    resp = client.get(f"/r/paper/{paper}", follow_redirects=False)
    assert resp.status_code == 303
    assert "embed" not in resp.headers["location"]

    resp = client.get(f"/c/pc{chunk}", follow_redirects=False)
    assert resp.status_code == 303
    assert "embed" not in resp.headers["location"]

    # End-to-end through the redirect chain: chrome-less either way in.
    for url in (f"/r/paper/{paper}", f"/c/pc{chunk}"):
        followed = client.get(url, headers=hx)
        assert followed.status_code == 200
        assert "<header" not in followed.text
    # …and full pages without the header.
    assert "<header" in client.get(f"/r/paper/{paper}").text


def test_draft_filter_unknown_id_is_a_friendly_notice_not_a_500(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "An unfiltered claim.", paper, chunk)

    resp = client.get("/nanopub?draft=dr999999999")
    assert resp.status_code == 200
    assert "showing all claims" in resp.text  # notice text, apostrophe HTML-escaped
    assert f"fi{hub}" in resp.text  # degrades to the unfiltered view

    junk = client.get("/nanopub?draft=not-an-id")
    assert junk.status_code == 200
    assert "showing all claims" in junk.text
