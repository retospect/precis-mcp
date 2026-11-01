"""gr520323 — the pcb batch upload door (``docs/backlog/pcb-upload-door.md``):
``put(kind='pcb', args={'op': 'batch_upload_link'})`` mints a single-use
URL, ``precis door pcb`` stages the file, ``put(args={'batch_ref': sha})``
runs the same judged batch put on it.

The web half (``POST /files/upload_batch/{ref_id}``) is covered in
``tests/precis_web/test_files.py``; here the staging is faked by writing
``<ref_id>-<sha>.json`` directly — the file IS the record the route leaves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from precis import batch_door, file_links
from precis.cli import door
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.ingest import pdf_upload

#: A two-part batch carrying every row family a board script emits that
#: the identity check below can compare (parts, nets, wiring, a measure,
#: the outline).
_BATCH: dict[str, Any] = {
    "components": [
        {
            "refdes": "R1",
            "label": "R 10k",
            "x": 5.0,
            "y": 5.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
        {
            "refdes": "R2",
            "label": "R 10k",
            "x": 12.0,
            "y": 5.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "A"}, {"name": "B"}],
    "connections": [
        {"net": "A", "refdes": "R1", "pin": "1"},
        {"net": "A", "refdes": "R2", "pin": "1"},
        {"net": "B", "refdes": "R1", "pin": "2"},
        {"net": "B", "refdes": "R2", "pin": "2"},
    ],
    "measures": [
        {
            "metric": "near",
            "operands": ["R1", "R2"],
            "bound": 9.0,
            "hard": True,
            "weight": 1.0,
        },
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 10], [0, 10]]}},
    ],
}


def _bytes(batch: dict[str, Any]) -> bytes:
    return json.dumps(batch, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- batch_door: the shared gates (no DB) ---------------------------------


def test_batch_keys_are_declared_put_args() -> None:
    """A staged batch reads through the put's ArgsView — every batch key
    must be a declared put key or the expansion raises UndeclaredArgsKey."""
    assert PcbHandler.ARGS_KEYS["put"] >= batch_door.BATCH_KEYS
    assert "batch_ref" in PcbHandler.ARGS_KEYS["put"]


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"\xff{}", "not UTF-8"),
        (b"{not json", "not JSON"),
        (b'{"x": NaN}', "not strict JSON"),
        (b"[1, 2]", "a JSON list"),
        (b"[" * 100_000 + b"]" * 100_000, "nested too deeply"),
    ],
)
def test_parse_batch_json_refuses(data: bytes, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        batch_door.parse_batch_json(data)


def test_parse_batch_json_reads_an_object() -> None:
    assert batch_door.parse_batch_json(_bytes(_BATCH)) == _BATCH


def test_key_refusal_names_planes_fix() -> None:
    assert batch_door.key_refusal(_BATCH) is None
    cause, hint = batch_door.key_refusal({**_BATCH, "planes": []}, slug="s") or ("", "")
    assert "planes" in cause
    assert "plane_net" in hint and "id='s'" in hint
    cause, hint = batch_door.key_refusal({"op": "place"}) or ("", "")
    assert "'op'" in cause
    assert "batch_ref" in hint


def test_is_sha256_hex() -> None:
    assert batch_door.is_sha256_hex("ab" * 32)
    assert not batch_door.is_sha256_hex("AB" * 32)
    assert not batch_door.is_sha256_hex("ab" * 31)


# -- the handler: link op and batch_ref put (DB) ---------------------------


@pytest.fixture
def link_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "door-test-key")


@pytest.fixture
def staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The deploy shape: staging a sibling of the watch inbox."""
    inbox, staging = tmp_path / "inbox", tmp_path / "staging"
    inbox.mkdir()
    staging.mkdir()
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(inbox))
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    return staging


@pytest.fixture
def pcb(store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _ref_id(pcb: PcbHandler, slug: str) -> int:
    from precis.handlers._slug_ref_shared import resolve_live_slug_ref

    return int(resolve_live_slug_ref(pcb.store, kind="pcb", id=slug).id)


def _stage(staging: Path, ref_id: int, data: bytes) -> str:
    sha = _sha(data)
    (staging / pdf_upload.staged_batch_name(ref_id, sha)).write_bytes(data)
    return sha


def _normalised(pcb: PcbHandler, slug: str) -> dict[str, Any]:
    """The stored design with row ids dropped — what must match between
    the door put and the inline put of the same batch."""

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: strip(x)
                for k, x in v.items()
                if k != "id" and not k.endswith("_id") and k != "board"
            }
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    ref_id = _ref_id(pcb, slug)
    return strip(
        {
            "graph": pcb.store.pcb_graph(ref_id),
            "measures": pcb.store.pcb_measures_list(ref_id),
            "features": pcb.store.pcb_features_list(ref_id),
        }
    )


def test_link_needs_an_existing_design(pcb, link_key, staging) -> None:
    with pytest.raises(NotFound) as exc:
        pcb.put(id="door-new", args={"op": batch_door.LINK_OP})
    assert "title=" in str(exc.value.next)


def test_link_mints_a_ref_bound_single_use_url(pcb, link_key, staging) -> None:
    pcb.put(id="door-a", title="door a")
    resp = pcb.put(id="door-a", args={"op": batch_door.LINK_OP})
    ref_id = _ref_id(pcb, "door-a")
    line = next(ln for ln in resp.body.splitlines() if ln.startswith("url: "))
    assert f"/files/upload_batch/{ref_id}?t=" in line
    token = line.split("?t=", 1)[1]
    claims = file_links.verify(token, op="put", sha=file_links.UPLOAD_BATCH_SHA)
    assert claims.ref_id == ref_id
    assert "precis door pcb" in resp.body
    assert batch_door.put_call("door-a") in resp.body


def test_link_refuses_other_args_and_title(pcb, link_key, staging) -> None:
    pcb.put(id="door-b", title="door b")
    with pytest.raises(BadInput, match="takes no other args"):
        pcb.put(id="door-b", args={"op": batch_door.LINK_OP, "components": []})
    with pytest.raises(BadInput, match="writes nothing"):
        pcb.put(id="door-b", title="t", args={"op": batch_door.LINK_OP})


def test_batch_ref_put_stores_what_inline_stores(pcb, link_key, staging) -> None:
    """The exit criterion: a door put and an inline put of one batch store
    identical rows (modulo slug and row ids)."""
    pcb.put(id="door-inline", title="t", args=json.loads(_bytes(_BATCH)))
    pcb.put(id="door-staged", title="t")
    sha = _stage(staging, _ref_id(pcb, "door-staged"), _bytes(_BATCH))
    pcb.put(id="door-staged", args={"batch_ref": sha})
    assert _normalised(pcb, "door-staged") == _normalised(pcb, "door-inline")
    # A staging is read, never moved: the same sha can be put again.
    assert pdf_upload.staged_batch_path(sha, ref_id=_ref_id(pcb, "door-staged"))


def test_batch_ref_bound_to_another_design_refuses(pcb, link_key, staging) -> None:
    pcb.put(id="door-c", title="c")
    pcb.put(id="door-d", title="d")
    sha = _stage(staging, _ref_id(pcb, "door-c"), _bytes(_BATCH))
    with pytest.raises(BadInput, match="no staged batch") as exc:
        pcb.put(id="door-d", args={"batch_ref": sha})
    assert batch_door.LINK_OP in str(exc.value.next)


def test_batch_ref_tampered_staging_refuses(pcb, link_key, staging) -> None:
    pcb.put(id="door-e", title="e")
    ref_id = _ref_id(pcb, "door-e")
    sha = _stage(staging, ref_id, _bytes(_BATCH))
    (staging / pdf_upload.staged_batch_name(ref_id, sha)).write_bytes(b"{}")
    with pytest.raises(BadInput, match="now hashes to"):
        pcb.put(id="door-e", args={"batch_ref": sha})


def test_batch_ref_rides_alone_and_takes_batch_keys_only(
    pcb, link_key, staging
) -> None:
    pcb.put(id="door-f", title="f")
    ref_id = _ref_id(pcb, "door-f")
    sha = _stage(staging, ref_id, _bytes(_BATCH))
    with pytest.raises(BadInput, match="rides alone"):
        pcb.put(id="door-f", args={"batch_ref": sha, "nets": []})
    planes = _stage(staging, ref_id, _bytes({**_BATCH, "planes": [{"net": "A"}]}))
    with pytest.raises(BadInput, match="planes") as exc:
        pcb.put(id="door-f", args={"batch_ref": planes})
    assert "plane_net" in str(exc.value.next)
    with pytest.raises(BadInput, match="sha256 the batch upload answered"):
        pcb.put(id="door-f", args={"batch_ref": "not-a-sha"})


# -- precis door pcb (no network: the POST is stubbed) ---------------------


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body


def _args(path: Path, url: str) -> argparse.Namespace:
    return argparse.Namespace(door_kind="pcb", path=str(path), url=url, timeout=5.0)


_URL = "https://web.example/files/upload_batch/7?t=v1.put.upload_batch.7.1.sig"


def test_door_cli_posts_and_prints_the_put(tmp_path, monkeypatch, capsys) -> None:
    f = tmp_path / "board.json"
    f.write_bytes(_bytes(_BATCH))
    sent: dict[str, Any] = {}

    def fake_post(url: str, data: bytes, sha: str, *, timeout: float) -> _Resp:
        sent.update(url=url, data=data, sha=sha)
        return _Resp(200, {"sha256": sha, "next": "put(...)"})

    monkeypatch.setattr(door, "_post", fake_post)
    door.run(_args(f, _URL))
    assert sent == {"url": _URL, "data": f.read_bytes(), "sha": _sha(f.read_bytes())}
    out = capsys.readouterr().out
    assert f"sha256: {_sha(f.read_bytes())}" in out
    assert "next: put(...)" in out


@pytest.mark.parametrize(
    ("body", "url", "why"),
    [
        (b"[1]", _URL, "one JSON object"),
        (_bytes({"op": "place"}), _URL, "keys a batch put does not take"),
        (_bytes(_BATCH), "/files/upload_batch/7?t=x", "absolute http"),
        (
            _bytes(_BATCH),
            "https://web.example/files/upload/7?t=x",
            "not a batch upload link",
        ),
    ],
)
def test_door_cli_local_refusals_never_post(
    tmp_path, monkeypatch, capsys, body: bytes, url: str, why: str
) -> None:
    f = tmp_path / "board.json"
    f.write_bytes(body)

    def no_post(*_a: Any, **_k: Any) -> _Resp:
        raise AssertionError("a local refusal must not spend the link")

    monkeypatch.setattr(door, "_post", no_post)
    with pytest.raises(SystemExit) as exc:
        door.run(_args(f, url))
    assert exc.value.code == door.EXIT_LOCAL
    assert why in capsys.readouterr().err


def test_door_cli_door_refusal_names_the_fix(tmp_path, monkeypatch, capsys) -> None:
    f = tmp_path / "board.json"
    f.write_bytes(_bytes(_BATCH))
    monkeypatch.setattr(
        door,
        "_post",
        lambda *_a, **_k: _Resp(409, {"detail": "upload link already used"}),
    )
    with pytest.raises(SystemExit) as exc:
        door.run(_args(f, _URL))
    assert exc.value.code == door.EXIT_DOOR
    err = capsys.readouterr().err
    assert "HTTP 409" in err and batch_door.LINK_OP in err


def test_door_cli_sha_mismatch_is_a_door_fault(tmp_path, monkeypatch, capsys) -> None:
    f = tmp_path / "board.json"
    f.write_bytes(_bytes(_BATCH))
    monkeypatch.setattr(
        door, "_post", lambda *_a, **_k: _Resp(200, {"sha256": "ab" * 32})
    )
    with pytest.raises(SystemExit) as exc:
        door.run(_args(f, _URL))
    assert exc.value.code == door.EXIT_DOOR
    assert "staged sha256" in capsys.readouterr().err
