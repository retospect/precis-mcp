"""Local supersession: real PG transactions and unsigned RDF, no keys/network."""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.errors import RaiseException, UniqueViolation
from rdflib import Dataset, URIRef

from precis.cli import nanopub as cli
from precis.errors import BadInput
from precis.nanopub import assemble, evidence, mint, mirror, state
from precis.nanopub.supersede import supersede
from precis.nanopub.vocab import NPX
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper
from tests.workers._helpers import seed_ref


def _anchor(store: Any, row: Any) -> Any:
    """Synthetic artifact/proof rows only: no cryptography or calendar calls."""
    if row.state == "candidate":
        assert store.nanopub_approve(
            row.id,
            approved_title="Synthetic claim.",
            claim_sha="ab" * 32,
            aida_uri="http://purl.org/aida/Synthetic.",
            grounding={"passages": []},
        )
    uri = "https://w3id.org/np/RA" + f"{row.id:043d}"
    artifact = store.nanopub_insert_artifact(
        publish_id=row.id,
        claim_ref_id=row.claim_ref_id,
        artifact_type="claim",
        trig_bytes=f"# synthetic artifact {row.id}".encode(),
        trusty_uri=uri,
        aida_uri="http://purl.org/aida/Synthetic.",
        claim_sha="ab" * 32,
        signer="test:signer",
        key_fingerprint="test-fingerprint",
        dois=[],
    )
    assert store.nanopub_record_signed(
        row.id,
        trusty_uri=uri,
        artifact_id=artifact,
        dependency_codes={},
    )
    batch = store.nanopub_create_batch(
        merkle_root=f"{row.id:064x}",
        construction="synthetic",
        calendar_url="test:calendar",
        leaves=[(artifact, 0, "cd" * 32, b"leaf")],
        pending_proof=b"pending proof",
    )
    assert store.nanopub_set_batch(row.id, batch)
    return store.nanopub_publish_row_by_id(row.id)


def _old(store: Any) -> Any:
    hub = seed_ref(store, title="Synthetic claim.", kind="finding")
    return _anchor(store, store.nanopub_create_publish_row(hub))


def _persisted(store: Any, old: Any) -> tuple[Any, ...]:
    # Physical fresh connection, independent of pool transaction visibility.
    with psycopg.connect(store.pool.conninfo) as conn:
        return (
            conn.execute(
                "SELECT id, state FROM nanopub_publish WHERE claim_ref_id = %s ORDER BY id",
                (old.claim_ref_id,),
            ).fetchall(),
            conn.execute(
                "SELECT predecessor_id, successor_id FROM nanopub_supersessions ORDER BY predecessor_id"
            ).fetchall(),
            conn.execute("SELECT * FROM nanopub_artifacts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM nanopub_ots_batches ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM nanopub_ots_leaves ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM nanopub_ots_proofs ORDER BY id").fetchall(),
        )


def test_atomic_staging_preserves_predecessor_and_proof(store: Any) -> None:
    old = _old(store)
    before = _persisted(store, old)
    successor = supersede(store, old.claim_ref_id, interactive=True)
    after = _persisted(store, old)
    assert after[0] == [(old.id, "superseded"), (successor.id, "candidate")]
    assert after[1] == [(old.id, successor.id)]
    assert before[2:] == after[2:]
    previous = asdict(store.nanopub_publish_row_by_id(old.id))
    expected = asdict(old)
    for key in ("state", "updated_at"):
        previous.pop(key)
        expected.pop(key)
    assert previous == expected
    assert successor.grounding == {} and successor.artifact_id is None
    state.check_transition("anchored", "superseded")


def test_insert_failure_rolls_back_terminalization(store: Any) -> None:
    old = _old(store)
    before = _persisted(store, old)
    with store.pool.connection() as conn:
        conn.execute("""CREATE FUNCTION test_refuse_supersession() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic insert failure'; END $$""")
        conn.execute("""CREATE TRIGGER test_refuse_supersession BEFORE INSERT ON
        nanopub_supersessions FOR EACH ROW EXECUTE FUNCTION test_refuse_supersession()""")
    try:
        with pytest.raises(RaiseException, match="synthetic insert failure"):
            store.nanopub_supersede(old.id)
        assert _persisted(store, old) == before
    finally:
        with store.pool.connection() as conn:
            conn.execute(
                "DROP TRIGGER test_refuse_supersession ON nanopub_supersessions"
            )
            conn.execute("DROP FUNCTION test_refuse_supersession()")


@pytest.mark.parametrize(
    "posture",
    [
        "published",
        "published_at",
        "registry_url",
        "candidate",
        "reviewed",
        "signed",
        "wrong_artifact",
        "wrong_batch",
        "no_proof",
    ],
)
def test_refuses_unsafe_predecessor(store: Any, posture: str) -> None:
    old = _old(store)
    with store.pool.connection() as conn:
        if posture == "published_at":
            conn.execute(
                "UPDATE nanopub_publish SET published_at = now() WHERE id = %s",
                (old.id,),
            )
        elif posture == "registry_url":
            conn.execute(
                "UPDATE nanopub_publish SET registry_url = 'test:registry' WHERE id = %s",
                (old.id,),
            )
        elif posture in ("wrong_artifact", "wrong_batch"):
            other = _old(store)
            col = "artifact_id" if posture == "wrong_artifact" else "batch_id"
            conn.execute(
                f"UPDATE nanopub_publish SET {col} = %s WHERE id = %s",
                (getattr(other, col), old.id),
            )
        elif posture == "no_proof":
            conn.execute(
                "UPDATE nanopub_publish SET batch_id = NULL WHERE id = %s", (old.id,)
            )
        else:
            conn.execute(
                "UPDATE nanopub_publish SET state = %s WHERE id = %s", (posture, old.id)
            )
    before = _persisted(store, old)
    with pytest.raises(BadInput):
        store.nanopub_supersede(old.id)
    assert _persisted(store, old) == before


def test_noninteractive_and_missing_refuse(store: Any) -> None:
    old = _old(store)
    before = _persisted(store, old)
    with pytest.raises(PermissionError):
        supersede(store, old.claim_ref_id)
    with pytest.raises(BadInput, match="no publish row"):
        store.nanopub_supersede(-1)
    with pytest.raises(BadInput, match="no live publish row"):
        supersede(store, -1, interactive=True)
    assert _persisted(store, old) == before


def _race(store: Any, hub: int, call: Any) -> list[Any]:
    barrier = Barrier(2)

    def run() -> Any:
        barrier.wait(timeout=10)
        try:
            return call()
        except (BadInput, UniqueViolation) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Hold the actual serialization lock until BOTH writers are observed
        # waiting in PG; a start barrier alone could execute them sequentially.
        with psycopg.connect(store.pool.conninfo) as blocker:
            blocker.execute(
                "SELECT ref_id FROM refs WHERE ref_id = %s FOR NO KEY UPDATE", (hub,)
            )
            futures = [pool.submit(run) for _ in range(2)]
            deadline = time.monotonic() + 10
            observed = False
            while time.monotonic() < deadline:
                rows = blocker.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query LIKE "
                    "'SELECT ref_id FROM refs WHERE ref_id = % FOR NO KEY UPDATE%'"
                ).fetchall()
                if len(rows) == 2:
                    observed = True
                    break
                # Clear PG's transaction-local stats snapshot before retrying.
                blocker.execute("SELECT pg_stat_clear_snapshot()")
                time.sleep(0.01)
        outcomes = [future.result(timeout=15) for future in futures]
        assert observed, "both writers must block on the real PG lock"
        return outcomes


def test_competing_supersede_has_one_winner(store: Any) -> None:
    old = _old(store)
    outcomes = _race(store, old.claim_ref_id, lambda: store.nanopub_supersede(old.id))
    winners = [row for row in outcomes if not isinstance(row, Exception)]
    assert len(winners) == 1
    after = _persisted(store, old)
    assert after[0] == [(old.id, "superseded"), (winners[0].id, "candidate")]
    assert after[1] == [(old.id, winners[0].id)]


def test_approve_reopen_discard_and_concurrent_restaging(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    old = _anchor(store, store.nanopub_create_publish_row(hub))
    candidate = store.nanopub_supersede(old.id)
    payload = _payload(chunk, sha)
    payload["supersedes"] = "https://forged.invalid/not-provenance"
    reviewed = mint.approve(store, hub, payload=payload, interactive=True)
    inp, _ = mint._mint_input(store, reviewed, evidence.load_bundle(store, hub))
    assert inp.supersedes == old.trusty_uri
    assert store.nanopub_reopen(reviewed.id)
    assert store.nanopub_predecessor_artifact(candidate.id).trusty_uri == old.trusty_uri
    assert store.nanopub_discard_candidate(candidate.id)
    assert _persisted(store, old)[1] == [(old.id, None)]
    outcomes = _race(store, hub, lambda: store.nanopub_create_publish_row(hub))
    winners = [row for row in outcomes if not isinstance(row, Exception)]
    assert len(winners) == 1
    new = winners[0]
    assert _persisted(store, old)[1] == [(old.id, new.id)]
    reviewed = mint.approve(store, hub, payload=payload, interactive=True)
    assert store.nanopub_predecessor_artifact(reviewed.id).trusty_uri == old.trusty_uri


def test_rejected_successor_cannot_drop_history_on_restaging(store: Any) -> None:
    old = _old(store)
    new = store.nanopub_supersede(old.id)
    assert store.nanopub_approve(
        new.id,
        approved_title="Synthetic.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )
    assert store.nanopub_transition(new.id, to_state="rejected", expect=("reviewed",))
    before = _persisted(store, old)
    with pytest.raises(BadInput, match="explicit resolution"):
        store.nanopub_create_publish_row(old.claim_ref_id)
    assert _persisted(store, old) == before


def test_two_generations_use_immediate_predecessor(store: Any) -> None:
    old = _old(store)
    proof = _persisted(store, old)[2:]
    middle = _anchor(store, store.nanopub_supersede(old.id))
    last = store.nanopub_supersede(middle.id)
    assert store.nanopub_predecessor_artifact(middle.id).trusty_uri == old.trusty_uri
    assert store.nanopub_predecessor_artifact(last.id).trusty_uri == middle.trusty_uri
    assert _persisted(store, old)[1] == [(old.id, middle.id), (middle.id, last.id)]
    after = _persisted(store, old)[2:]
    assert all(
        current[: len(original)] == original
        for current, original in zip(after, proof, strict=True)
    )


def test_pubinfo_round_trip_through_existing_mirror(store: Any) -> None:
    old = _old(store)
    new = store.nanopub_supersede(old.id)
    assert store.nanopub_approve(
        new.id,
        approved_title="Synthetic.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={"supersedes": "test:forged"},
    )
    row = store.nanopub_publish_row_by_id(new.id)
    inp, _ = mint._mint_input(
        store,
        row,
        evidence.HubBundle(
            hub_ref_id=row.claim_ref_id,
            sentence="Synthetic.",
            artifact_type="claim",
            sources=[],
            grounding_chunks=[],
        ),
    )
    data = assemble.draft_trig(inp)
    ds = Dataset()
    ds.parse(data=data, format="trig")
    triples = list(
        ds.graph(assemble.DRAFT_NS["pubinfo"]).triples((None, NPX.supersedes, None))
    )
    assert triples == [
        (
            URIRef(str(assemble.DRAFT_NS).removesuffix("#")),
            NPX.supersedes,
            URIRef(old.trusty_uri),
        )
    ]
    parsed = mirror.index_bytes("DRAFT", data.encode())
    assert (old.trusty_uri.rsplit("/", 1)[-1], "supersedes") in parsed.edges
    assert not parsed.verified  # unsigned fixture, not a validity/signature claim


@pytest.mark.parametrize("same_key", [False, True])
def test_sign_checks_predecessor_key_before_signing(
    store: Any, monkeypatch: Any, same_key: bool
) -> None:
    old = _old(store)
    new = store.nanopub_supersede(old.id)
    assert store.nanopub_approve(
        new.id,
        approved_title="Synthetic.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )
    monkeypatch.setattr(
        mint.evidence,
        "load_bundle",
        lambda *a: SimpleNamespace(sentence="Synthetic.", conjunct_atoms=[]),
    )
    monkeypatch.setattr(mint.gates, "run_mint_gates", lambda *a, **kw: [])
    monkeypatch.setattr(mint.gates, "check_drift", lambda *a: None)
    monkeypatch.setattr(mint.freshness, "check_grounding_fresh", lambda *a: (None, []))
    monkeypatch.setattr(
        mint, "load_profile", lambda *a, **kw: SimpleNamespace(public_key="stub")
    )
    monkeypatch.setattr(
        mint,
        "fingerprint",
        lambda key: "test-fingerprint" if same_key else "other-fingerprint",
    )

    def forbidden(inp: Any, *args: Any) -> None:
        assert same_key, "wrong key must be rejected before building/signing"
        assert inp.supersedes == old.trusty_uri
        raise RuntimeError("unsigned test stop")

    monkeypatch.setattr(mint, "_build_and_sign", forbidden)
    before = _persisted(store, old)
    expected = RuntimeError if same_key else BadInput
    message = "unsigned test stop" if same_key else "predecessor's signing key"
    with pytest.raises(expected, match=message):
        mint.sign(store, old.claim_ref_id)
    assert _persisted(store, old) == before


def test_cli_parser_and_real_local_staging(store: Any, capsys: Any) -> None:
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    old = _old(store)
    args = parser.parse_args(["nanopub", "supersede", f"fi{old.claim_ref_id}"])
    cli._supersede(args, store)
    assert "review and sign separately" in capsys.readouterr().out
    assert store.nanopub_publish_row(old.claim_ref_id).state == "candidate"
    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["nanopub", "supersede", "--help"])
    assert help_exit.value.code == 0
    assert "anchored, unpublished" in capsys.readouterr().out


def test_forward_migration_dry_run_and_legacy_data_preservation(
    fresh_db: str, tmp_path: Any, capsys: Any
) -> None:
    import shutil
    from pathlib import Path

    from precis.cli import migrate as migrate_cli
    from precis.store import Migrator, Store

    source = Path(__file__).parents[1] / "src/precis/migrations"
    name = "0190_nanopub_supersessions.sql"
    legacy = tmp_path / "legacy"
    shutil.copytree(source, legacy, ignore=shutil.ignore_patterns(name))
    Migrator(fresh_db, legacy, baseline=legacy / "baseline/schema.sql").apply_all()
    previous_store = Store.connect(fresh_db)
    try:
        hub = seed_ref(previous_store, title="Synthetic legacy claim.", kind="finding")
        with previous_store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO nanopub_publish (claim_ref_id) VALUES (%s)", (hub,)
            )
        old = _anchor(previous_store, previous_store.nanopub_publish_row(hub))
        frozen = asdict(old)
        artifact = previous_store.nanopub_artifact(old.artifact_id)
        proof = previous_store.nanopub_latest_proof(old.batch_id)
        migration = Migrator(fresh_db, source)
        assert migration.pending() == [("precis", name.removesuffix(".sql"))]
        migrate_cli.run(
            argparse.Namespace(database_url=fresh_db, dry_run=True, from_scratch=False)
        )
        output = capsys.readouterr().out
        assert "would apply 1 migration(s)" in output
        assert "precis/0190_nanopub_supersessions" in output
        with psycopg.connect(fresh_db) as conn:
            assert (
                conn.execute("SELECT to_regclass('nanopub_supersessions')").fetchone()[
                    0
                ]
                is None
            )
        assert migration.apply_all() == [("precis", name.removesuffix(".sql"))]
        assert migration.pending() == []
        assert asdict(previous_store.nanopub_publish_row_by_id(old.id)) == frozen
        assert previous_store.nanopub_artifact(old.artifact_id) == artifact
        assert previous_store.nanopub_latest_proof(old.batch_id) == proof
        successor = previous_store.nanopub_supersede(old.id)
        assert previous_store.nanopub_predecessor_artifact(successor.id) == artifact
    finally:
        previous_store.close()


def test_live_publish_holds_supersede_until_published(
    store: Any, monkeypatch: Any
) -> None:
    from threading import Event

    from precis.nanopub import registry
    from precis.nanopub.preflight import PreflightIssue

    old = _old(store)
    entered_post, release_post = Event(), Event()
    posted: list[bytes] = []

    def preflight(*args: Any, row: Any) -> list[PreflightIssue]:
        return (
            [] if row.state == "anchored" else [PreflightIssue("state", "not anchored")]
        )

    def post(url: str, body: bytes) -> None:
        posted.append(body)
        entered_post.set()
        assert release_post.wait(timeout=10)

    monkeypatch.setattr(registry, "publish_preflight", preflight)
    with ThreadPoolExecutor(max_workers=2) as executor:
        published = executor.submit(
            registry.publish,
            store,
            old.claim_ref_id,
            live=True,
            interactive=True,
            post=post,
        )
        assert entered_post.wait(timeout=10)
        superseded = executor.submit(store.nanopub_supersede, old.id)
        try:
            blocked = False
            with psycopg.connect(store.pool.conninfo, autocommit=True) as observer:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not superseded.done():
                    blocked = bool(
                        observer.execute(
                            "SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE "
                            "'SELECT ref_id FROM refs WHERE ref_id = % FOR NO KEY UPDATE%'"
                        ).fetchone()
                    )
                    if blocked:
                        break
                    time.sleep(0.01)
            assert blocked, "supersede must wait while a registry POST is in flight"
            assert not superseded.done()
        finally:
            release_post.set()
        assert published.result(timeout=10).live
        with pytest.raises(BadInput, match="unpublished"):
            superseded.result(timeout=10)
    assert posted == [store.nanopub_artifact(old.artifact_id).trig_bytes]
    assert _persisted(store, old)[0] == [(old.id, "published")]
    assert _persisted(store, old)[1] == []


def test_publish_rechecks_after_supersede_and_never_posts(
    store: Any, monkeypatch: Any
) -> None:
    from precis.nanopub import registry
    from precis.nanopub.preflight import PreflightIssue

    old = _old(store)
    successor = store.nanopub_supersede(old.id)

    def preflight(*args: Any, row: Any) -> list[PreflightIssue]:
        assert row.id == successor.id
        return [PreflightIssue("state", "candidate is not anchored")]

    monkeypatch.setattr(registry, "publish_preflight", preflight)

    def post(*args: Any) -> None:
        pytest.fail("a superseded predecessor must never be posted")

    with pytest.raises(registry.PublishBlocked, match="not anchored"):
        registry.publish(
            store, old.claim_ref_id, live=True, interactive=True, post=post
        )
