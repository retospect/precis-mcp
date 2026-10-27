"""Paper cite-key edits share the web rename and preserve the PDF attachment."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from precis.corpus_layout import corpus_pdf_dest
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.cfp import CfpHandler
from precis.handlers.paper import PaperHandler
from precis.store import Store
from precis.utils.deadline import DeadlineExceeded
from precis_web.config import WebConfig
from precis_web.routes import papers

_SHA = "a" * 64
_REASON = "correct the author-year handle"


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed(store: Store, key: str) -> int:
    return store.insert_ref(
        kind="paper", slug=key, title=f"Title of {key}", provider="manual", meta={}
    ).id


def _attach_pdf(store: Store, ref_id: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\nrekey fixture\n")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, %s, %s)",
            (_SHA, _SHA, path.stat().st_size, str(path)),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (_SHA, ref_id)
        )


def _snapshot(store: Store, ref_id: int) -> tuple[Any, ...]:
    """Read all state the door may write, including metadata and audit rows."""
    with store.pool.connection() as conn:
        return (
            conn.execute("SELECT * FROM refs WHERE ref_id = %s", (ref_id,)).fetchall(),
            conn.execute(
                "SELECT * FROM ref_identifiers WHERE ref_id = %s "
                "ORDER BY id_kind, id_value",
                (ref_id,),
            ).fetchall(),
            conn.execute(
                "SELECT * FROM ref_events WHERE ref_id = %s ORDER BY event_id",
                (ref_id,),
            ).fetchall(),
            conn.execute(
                "SELECT * FROM pdfs WHERE pdf_sha256 = %s", (_SHA,)
            ).fetchall(),
        )


def test_rekey_replaces_old_key_and_audits_reason(
    store: Store, handler: PaperHandler
) -> None:
    ref_id = _seed(store, "smith2024")
    assert "cite_key" in PaperHandler.ARGS_KEYS["edit"]

    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "smith24", "reason": _REASON}
    )

    assert "smith2024" in response.body and "smith24" in response.body
    assert store.find_ref_by_identifier("cite_key", "smith2024") is None
    assert store.find_ref_by_identifier("cite_key", "smith24") == ref_id
    # The displaced key stays resolvable as a cite_key_alias redirect
    # (gr521515) — never by the live cite_key scheme.
    assert store.find_ref_by_identifier("cite_key_alias", "smith2024") == ref_id
    old = store.get_ref(kind="paper", id="smith2024")
    assert old is not None and old.id == ref_id
    renamed = store.get_ref(kind="paper", id="smith24")
    assert renamed is not None and renamed.id == ref_id
    assert store.fetch_refs_by_ids([ref_id])[ref_id].slug == "smith24"
    events = store.events_for(ref_id, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].payload == {
        "old": "smith2024",
        "new": "smith24",
        "reason": _REASON,
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_live_key_collision_refuses_without_changes(
    store: Store, handler: PaperHandler, dry_run: bool, tmp_path: Path
) -> None:
    ref_id = _seed(store, "smith2024")
    owner = _seed(store, "smith24")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    before = _snapshot(store, ref_id), _snapshot(store, owner)

    with pytest.raises(BadInput, match="already belongs") as exc:
        handler.edit(
            id=f"pa{ref_id}",
            args={"cite_key": "smith24", "reason": _REASON},
            dry_run=dry_run,
        )

    assert exc.value.next
    assert (_snapshot(store, ref_id), _snapshot(store, owner)) == before
    assert old_pdf.is_file()
    assert not corpus_pdf_dest("smith24", tmp_path).exists()


@pytest.mark.parametrize("dry_run", [True, "diff", "full"])
def test_dry_run_writes_nothing(
    store: Store, handler: PaperHandler, tmp_path: Path, dry_run: bool | str
) -> None:
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    before = _snapshot(store, ref_id)
    contents = old_pdf.read_bytes()

    response = handler.edit(
        id=f"pa{ref_id}",
        args={"cite_key": "jones24", "reason": _REASON},
        dry_run=dry_run,
    )

    assert "dry run" in response.body
    assert "smith2024" in response.body and "jones24" in response.body
    assert _snapshot(store, ref_id) == before
    assert old_pdf.read_bytes() == contents
    assert not corpus_pdf_dest("jones24", tmp_path).parent.exists()


@pytest.mark.parametrize("recorded_path", [True, False])
def test_stored_pdf_path_follows_key(
    store: Store,
    handler: PaperHandler,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_path: bool,
) -> None:
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    contents = old_pdf.read_bytes()
    monkeypatch.setenv("PRECIS_CORPUS_DIR", str(tmp_path))
    if not recorded_path:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE pdfs SET storage_path = '' WHERE pdf_sha256 = %s", (_SHA,)
            )

    handler.edit(id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON})

    new_pdf = corpus_pdf_dest("jones24", tmp_path)
    assert not old_pdf.exists()
    assert new_pdf.read_bytes() == contents
    assert store.pdf_storage_path(_SHA) == str(new_pdf)
    assert store.fetch_refs_by_ids([ref_id])[ref_id].pdf_sha256 == _SHA


@pytest.mark.parametrize(
    "key", ["", "Smith24", "smith-24", "../smith", "smith24\n", None, 24]
)
def test_invalid_key_refuses_with_next(
    store: Store, handler: PaperHandler, key: Any
) -> None:
    ref_id = _seed(store, "smith2024")
    before = _snapshot(store, ref_id)
    with pytest.raises(BadInput, match="invalid") as exc:
        handler.edit(id=f"pa{ref_id}", args={"cite_key": key, "reason": _REASON})
    assert exc.value.next
    assert _snapshot(store, ref_id) == before


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_missing_reason_refuses_with_next(
    store: Store, handler: PaperHandler, reason: str | None
) -> None:
    ref_id = _seed(store, "smith2024")
    before = _snapshot(store, ref_id)
    args = {"cite_key": "smith24"}
    if reason is not None:
        args["reason"] = reason
    with pytest.raises(BadInput, match="reason") as exc:
        handler.edit(id=f"pa{ref_id}", args=args)
    assert exc.value.next
    assert _snapshot(store, ref_id) == before


def test_rekey_cannot_mix_with_metadata(store: Store, handler: PaperHandler) -> None:
    ref_id = _seed(store, "smith2024")
    before = _snapshot(store, ref_id)
    with pytest.raises(BadInput, match="cannot be combined"):
        handler.edit(
            id=f"pa{ref_id}",
            title="changed",
            args={"cite_key": "smith24", "reason": _REASON},
        )
    assert _snapshot(store, ref_id) == before


def test_rekey_refuses_another_kind(store: Store, handler: PaperHandler) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="not a paper", meta={})
    before = _snapshot(store, ref.id)
    with pytest.raises(NotFound):
        handler.edit(id=ref.id, args={"cite_key": "smith24", "reason": _REASON})
    assert _snapshot(store, ref.id) == before


def test_rekey_same_key_is_a_noop(store: Store, handler: PaperHandler) -> None:
    ref_id = _seed(store, "smith24")
    before = _snapshot(store, ref_id)
    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "smith24", "reason": _REASON}
    )
    assert "no change" in response.body
    assert _snapshot(store, ref_id) == before


def test_audit_failure_rolls_back_key_and_leaves_pdf(
    store: Store,
    handler: PaperHandler,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    before = _snapshot(store, ref_id)

    def fail_audit(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "append_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        handler.edit(id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON})
    assert _snapshot(store, ref_id) == before
    assert old_pdf.is_file()
    assert not corpus_pdf_dest("jones24", tmp_path).exists()


def test_pdf_move_failure_keeps_storage_path_and_reports_it(
    store: Store,
    handler: PaperHandler,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)

    def fail_move(self: Path, target: Path) -> Path:
        raise OSError("read-only corpus")

    monkeypatch.setattr(Path, "rename", fail_move)
    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON}
    )
    assert "PDF not moved" in response.body
    assert store.find_ref_by_identifier("cite_key", "jones24") == ref_id
    assert store.pdf_storage_path(_SHA) == str(old_pdf)
    assert old_pdf.is_file()


def test_web_route_rekeys_and_moves_pdf(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    monkeypatch.setattr(papers, "get_store", lambda request: store)
    config = WebConfig(corpus_dir=tmp_path)
    monkeypatch.setattr(papers, "get_web_config", lambda request: config)
    app = FastAPI()
    app.include_router(papers.router)

    with TestClient(app) as client:
        response = client.post(
            f"/papers/{ref_id}/edit",
            data={"cite_key": "jones24"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/papers/{ref_id}?tab=Meta"
    assert store.find_ref_by_identifier("cite_key", "smith2024") is None
    assert store.find_ref_by_identifier("cite_key", "jones24") == ref_id
    assert not old_pdf.exists()
    assert corpus_pdf_dest("jones24", tmp_path).is_file()
    assert store.pdf_storage_path(_SHA) == str(corpus_pdf_dest("jones24", tmp_path))
    event = store.events_for(ref_id, event="cite_key_changed")[0]
    assert event.source == "web-edit"
    assert event.payload is not None and event.payload["reason"]


def test_rekey_accepts_cfp_via_mcp_door(store: Store, hub: Hub) -> None:
    """The doc family shares the cite_key door: cfp inherits the rekey."""
    ref_id = store.insert_ref(
        kind="cfp", slug="cfpcall24", title="Call for X", meta={}
    ).id

    response = CfpHandler(hub=hub).edit(
        id=ref_id, args={"cite_key": "cfpcall25", "reason": _REASON}
    )

    assert "cfpcall24" in response.body and "cfpcall25" in response.body
    assert store.find_ref_by_identifier("cite_key", "cfpcall25") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "cfpcall24") == ref_id
    events = store.events_for(ref_id, event="cite_key_changed")
    assert len(events) == 1 and events[0].payload["reason"] == _REASON


def test_rekey_accepts_cfp_via_web_route(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /cfp/{ident} meta panel posts its handle edit to /papers/{id}/edit."""
    ref_id = store.insert_ref(
        kind="cfp", slug="cfpcall24", title="Call for X", meta={}
    ).id
    monkeypatch.setattr(papers, "get_store", lambda request: store)
    config = WebConfig(corpus_dir=tmp_path)
    monkeypatch.setattr(papers, "get_web_config", lambda request: config)
    app = FastAPI()
    app.include_router(papers.router)

    with TestClient(app) as client:
        response = client.post(
            f"/papers/{ref_id}/edit",
            data={"cite_key": "cfpcall25"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert store.find_ref_by_identifier("cite_key", "cfpcall25") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "cfpcall24") == ref_id
    event = store.events_for(ref_id, event="cite_key_changed")[0]
    assert event.source == "web-edit"


def test_rekey_rejects_kind_outside_door(store: Store, hub: Hub) -> None:
    """A kind outside the caller's accepted set still refuses."""
    ref_id = store.insert_ref(
        kind="cfp", slug="cfpcall24", title="Call for X", meta={}
    ).id
    # PaperHandler only accepts papers — a cfp id misses its door.
    with pytest.raises(NotFound):
        PaperHandler(hub=hub).edit(
            id=ref_id, args={"cite_key": "cfpcall25", "reason": _REASON}
        )


def test_storage_path_db_failure_moves_pdf_back(
    store: Store,
    handler: PaperHandler,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB (non-OSError) failure after the rename restores the file:
    without the rollback the file sits at the new path while
    storage_path names the old, and a retry reports "no change" with
    the PDF orphaned."""
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)
    contents = old_pdf.read_bytes()

    def fail_storage_path(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("db write failed")

    monkeypatch.setattr(store, "set_pdf_storage_path", fail_storage_path)
    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON}
    )

    assert "PDF not moved" in response.body
    assert "old storage path kept" in response.body
    # The key change committed (it precedes the move); the file is back
    # where the recorded storage_path still finds it.
    assert store.find_ref_by_identifier("cite_key", "jones24") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "smith2024") == ref_id
    assert store.pdf_storage_path(_SHA) == str(old_pdf)
    assert old_pdf.read_bytes() == contents
    assert not corpus_pdf_dest("jones24", tmp_path).exists()


def test_stranded_pdf_note_when_rollback_rename_also_fails(
    store: Store,
    handler: PaperHandler,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the move lands but the storage-path write fails AND the
    rollback rename fails, the receipt must say where the file actually
    sits — not "old storage path kept"."""
    ref_id = _seed(store, "smith2024")
    old_pdf = corpus_pdf_dest("smith2024", tmp_path)
    _attach_pdf(store, ref_id, old_pdf)

    renames = {"n": 0}
    real_rename = Path.rename

    def fail_second_rename(self: Path, target: Path) -> Path:
        renames["n"] += 1
        if renames["n"] == 1:
            return real_rename(self, target)
        raise OSError("rollback denied")

    def fail_storage_path(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("db write failed")

    monkeypatch.setattr(Path, "rename", fail_second_rename)
    monkeypatch.setattr(store, "set_pdf_storage_path", fail_storage_path)
    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON}
    )

    new_pdf = corpus_pdf_dest("jones24", tmp_path)
    assert "stranded" in response.body
    assert str(new_pdf) in response.body
    assert "old storage path kept" not in response.body
    assert new_pdf.is_file()
    assert store.pdf_storage_path(_SHA) == str(old_pdf)


def test_concurrent_key_claim_maps_to_badinput(
    store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key claimed between the ownership probe and the tx's INSERT
    reports like the probe's own collision refusal, not a raw 500."""
    ref_id = _seed(store, "smith2024")

    def claimed(*args: Any, **kwargs: Any) -> bool:
        raise UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(store, "set_ref_identifier", claimed)
    with pytest.raises(BadInput, match="claimed by another ref") as exc:
        handler.edit(id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON})
    assert exc.value.next
    assert store.find_ref_by_identifier("cite_key", "smith2024") == ref_id


def test_mcp_rekey_timeout_surfaces_badinput(
    store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP door runs the rekey under the shared bound the web route
    uses; an expired bound (wedged corpus mount / DB lock) is a BadInput
    with a next=, not a hang."""
    ref_id = _seed(store, "smith2024")

    def wedged(fn: Any, *, deadline_s: float, **kwargs: Any) -> Any:
        raise DeadlineExceeded("wedged", thread=threading.Thread())

    monkeypatch.setattr("precis.utils.deadline.call_with_deadline", wedged)
    with pytest.raises(BadInput, match="did not finish") as exc:
        handler.edit(id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON})
    assert exc.value.next
    assert store.find_ref_by_identifier("cite_key", "smith2024") == ref_id


def test_rekey_without_local_pdf_says_so(store: Store, handler: PaperHandler) -> None:
    ref_id = _seed(store, "smith2024")
    response = handler.edit(
        id=f"pa{ref_id}", args={"cite_key": "jones24", "reason": _REASON}
    )
    assert "No local PDF found" in response.body
    assert store.find_ref_by_identifier("cite_key", "jones24") == ref_id
