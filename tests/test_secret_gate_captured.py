"""Captured output (job transcripts, agentlog, alerts, doctor reports) is
masked on store -- the record is kept, the credential is not."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from precis import agentlog
from precis.alerts import raise_alert
from precis.store import Store
from precis.utils.secret_scan import mask_secrets_deep
from precis.workers.executors._common import append_chunk, set_meta

# Runtime-assembled so the source holds no literal credential.
SECRET = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")


def test_mask_deep_walks_containers_and_keeps_clean_values() -> None:
    val = {"a": ["x " + SECRET, 3, {"b": "fine"}], "n": None, "t": ("ok",)}
    out = mask_secrets_deep(val)
    assert SECRET not in repr(out)
    assert "<redacted:github token>" in out["a"][0]
    assert out["a"][1:] == [3, {"b": "fine"}]
    assert out["n"] is None
    clean = {"k": "plain"}
    assert mask_secrets_deep(clean) == clean


def _job(store: Store) -> int:
    return int(store.insert_ref(kind="job", slug=None, title="j").id)


def test_set_meta_masks_transcript(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    jid = _job(store)
    with caplog.at_level(logging.WARNING, logger="precis.utils.secret_scan"):
        with store.tx() as conn:
            set_meta(
                conn,
                jid,
                transcript='{"type":"assistant","text":"export T=' + SECRET + '"}',
                wall_seconds=3,
            )
    ref = store.get_ref(kind="job", id=jid)
    assert ref is not None
    assert SECRET not in str(ref.meta)
    assert "<redacted:" in ref.meta["transcript"]
    assert ref.meta["wall_seconds"] == 3
    assert SECRET not in caplog.text  # the warning names the kind only


def test_set_meta_clean_transcript_unchanged(store: Store) -> None:
    jid = _job(store)
    with store.tx() as conn:
        set_meta(conn, jid, transcript="did the thing, 12 tools")
    ref = store.get_ref(kind="job", id=jid)
    assert ref is not None
    assert ref.meta["transcript"] == "did the thing, 12 tools"


def test_append_chunk_masks_job_event_text(store: Store) -> None:
    jid = _job(store)
    append_chunk(store, jid, "job_result", "token " + SECRET)
    texts = [c.text for c in store.chunks.list_chunks_for_ref(jid)]
    assert texts and SECRET not in "".join(texts)
    assert "<redacted:" in "".join(texts)


def test_agentlog_prompt_and_result_masked(store: Store) -> None:
    lid = agentlog.open_log(
        store, source="plan_tick", title="t", prompt="env " + SECRET
    )
    agentlog.finalize_log(store, log_id=lid, status="ok", result="leaked " + SECRET)
    agentlog.stash_checkpoint(store, log_id=lid, checkpoint={"raw": SECRET})
    ref = store.get_ref(kind="agentlog", id=lid)
    assert ref is not None
    assert SECRET not in str(ref.meta)
    assert ref.meta["status"] == "ok"


def test_alert_title_detail_and_extra_masked(store: Store) -> None:
    aid, _ = raise_alert(
        store,
        source="nursery:spin-loop",
        fingerprint="spin-loop:77",
        title="boom " + SECRET,
        detail="argv: --token " + SECRET,
        extra_meta={"cmd": SECRET},
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta FROM refs WHERE ref_id = %s", (aid,)
        ).fetchone()
    assert row is not None
    title, meta = row
    assert SECRET not in title + str(meta)
    assert "<redacted:" in meta["detail"]


def test_fleet_report_payload_is_masked_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis import fleet
    from precis.handlers.fleet import FleetHandler

    seen: dict[str, object] = {}

    class _Res:
        def summary(self, host: object) -> str:
            return "ok"

    def fake_apply(store: object, *, host: str, report: dict) -> _Res:
        seen["report"] = report
        return _Res()

    monkeypatch.setattr(fleet, "apply_report", fake_apply)
    h = object.__new__(FleetHandler)
    h.store = cast(Any, object())
    h.put(
        mode="report",
        host="h1",
        report={"host": "h1", "rows": [{"screen": "export T=" + SECRET}]},
    )
    assert SECRET not in str(seen["report"])
    assert "<redacted:" in str(seen["report"])
