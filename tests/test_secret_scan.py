"""Pre-write secret gate: each pattern, its near-miss, and the write paths."""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, _created_id, export_memory_nodes
from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.todo import TodoHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.utils import handle_registry
from precis.utils.secret_scan import find_secrets, mask_secrets

# Fixtures are assembled at runtime so the source holds no literal token.
_B = "aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5"  # 36 mixed-case alnum, digits
POSITIVES = {
    "anthropic key/token": "key " + "sk-" + "ant-" + "oat01-" + _B,
    "github token": "gh" + "p_" + _B,
    "github fine-grained token": "github_" + "pat_" + "11AAAAAAA0" + _B,
    "aws access key id": "id " + "AK" + "IA" + "ABCDEFGH12345678",
    "aws secret key": "aws_secret_"
    + "access_key = "
    + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "slack token": "x" + "oxb-" + "123456789012-abcdefghijkl",
    "private key block": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "bearer token": "Authorization: " + "Bearer " + _B,
    "url with inline password": ("pos" + "tgresql://app:")
    + "hunter2hunter@db.internal:5432/x",
    "password literal": "PASSWORD: " + "Tr0ub4dor&3xx",
    "high-entropy token": "token is " + _B + "Qz",
    "slack webhook": "https://hooks." + "slack.com/services/T0001/B0002/" + _B,
    "discord webhook": "https://discord." + "com/api/webhooks/1234567890/" + _B,
    "url api_key param": "GET https://content.openalex.org/x.pdf?"
    + "api_"
    + "key="
    + "FAKEKEY01"
    + ' "HTTP/1.1 200"',
    "bearer credential": "sending Bearer " + _B + " now",
}
NEGATIVES = {
    "git sha": "commit 50d2afe3d and 50d2afe3d1f5b3a6c7e8d9f0a1b2c3d4e5f60718",
    "uuid": "id 123e4567-e89b-12d3-a456-426614174000 done",
    "sha256 checksum": "sha256: "
    + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "user@host": "ssh reto@hephaestus and postgresql://reto@db:5432/x",
    "redacted url": "postgresql://u:***@h/db and postgres://u:<redacted>@h and x://u:$PW@h",
    "password placeholder": "password: $DB_PASSWORD, password=<secret>, password: required",
    "vault reference": "password lives in ~/.secrets/pw/DB_PASS",
    "ssh fingerprint": "SHA256:C6hMRQSpHtuRChf0MALenPMwpOyqpB30aLPDpR1/LX0",
    "url path id": "(https://www.example.com/design/" + _B + "). Root frame",
    "long path": "~/CascadeProjects/alphafold-setup/alphafold3/docker/run_x",
    "lan ip and host": "node-a at 192.168.1.20 and 100.64.1.2",  # secret-gate: allow — negative sample: addresses are not credentials
    "password read in code": "password = _read_password(args); pw = x\n"
    "password = self.store.secret_value",
    "dev default dsn": "postgresql://postgres:postgres@localhost:5432/x",
    "password type annotation": "def f(password: PasswordRecord, n: int)",
    "bare prefix": "keys look like sk-ant-… or ghp_ prefixed",
    "url param name near-miss": "GET https://api.example.com/s?"
    + "api_key_hint=x&keyword=y&per-page=25 200",
    "bearer prose": "the bearer of this note may redeem it",
}


@pytest.mark.parametrize("kind", sorted(POSITIVES))
def test_positive(kind: str) -> None:
    found = find_secrets("line one\n" + POSITIVES[kind])
    assert kind in {f.kind for f in found}, found
    assert {f.line for f in found} == {2}


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_negative(name: str) -> None:
    assert find_secrets(NEGATIVES[name]) == []


def test_url_query_value_still_scanned() -> None:
    # Only the scheme/host/path part of a URL is exempt: a credential-named
    # query param is masked whatever the value looks like, and a high-entropy
    # value under a non-credential name still hits the generic rule.
    hits = find_secrets("https://api.example.com/v1/x?token=" + _B + "Qz")
    assert [h.kind for h in hits] == ["url token param"]
    hits = find_secrets("https://api.example.com/v1/x?p=" + _B + "Qz")
    assert [h.kind for h in hits] == ["high-entropy token"]


def test_url_query_secret_mask_keeps_name_and_other_params() -> None:
    line = (
        "GET https://h.example/p?filter=doi:10.1&" + "api_" + "key=FAKEKEY01&page=2 200"
    )
    out = mask_secrets(line, warn=False)
    assert "FAKEKEY01" not in out
    assert "<redacted:url api_key param>" in out
    assert "filter=doi:10.1" in out and "&page=2" in out


def test_url_query_secret_short_value_still_masked() -> None:
    # The generic rule needs 32+ chars; the param-name rule does not — a
    # short key in a credential-named param is still a credential.
    for name in ("token", "key", "access_token", "sig", "code"):
        line = f"https://h.example/x?a=1&{name}=s3cr"
        assert find_secrets(line), name
        assert "s3cr" not in mask_secrets(line, warn=False)


def test_mailto_and_email_params_masked() -> None:
    line = "https://api.crossref.org/works?rows=1&mail" + "to=ops@example.org"
    out = mask_secrets(line, warn=False)
    assert "ops@example.org" not in out and "rows=1" in out


def test_bearer_without_authorization_prefix_masked() -> None:
    tok = "Bearer " + _B
    assert [h.kind for h in find_secrets("hdr " + tok)] == ["bearer credential"]
    assert _B not in mask_secrets("hdr " + tok, warn=False)


def test_filesystem_path_segment_not_masked() -> None:
    # macOS temp dirs: a random 28-char segment inside a long path.
    path = "/private/var/folders/zz/aB3dE5gH7jK9mN1pQ3sT5vX7yZ0/T/pytest-of-u/x.py"
    assert find_secrets(path) == []
    assert mask_secrets(f"watching {path}", warn=False) == f"watching {path}"


def test_real_token_still_masked_next_to_a_path() -> None:
    tok = _B
    assert [h.kind for h in find_secrets(f"/tmp/x {tok}")] == ["high-entropy token"]
    assert [h.kind for h in find_secrets(f"key={tok}")] == ["high-entropy token"]
    assert "high-entropy token" in mask_secrets(f"/tmp/x {tok}", warn=False)


def test_slash_leading_base64_and_attached_token_still_masked() -> None:
    b64 = "/" + _B[:10] + "/" + _B[10:] + "+"
    assert [h.kind for h in find_secrets(b64)] == ["high-entropy token"]
    tok = _B
    assert [h.kind for h in find_secrets(f"/a/b/{tok}")] == ["high-entropy token"]


def test_excerpt_is_masked() -> None:
    (f,) = find_secrets("tok " + "gh" + "p_" + _B)
    assert f.excerpt_masked == "ghp_…"


def _verb(runtime_with_store: PrecisRuntime, verb: str, **args: object) -> str:
    return runtime_with_store.dispatch(verb, args)


def test_put_refuses_and_masks(runtime_with_store: PrecisRuntime) -> None:
    out = _verb(
        runtime_with_store,
        "put",
        kind="memory",
        text="ok\nlogin " + POSITIVES["github token"],
        title="t",
    )
    assert "BadInput" in out and _B not in out, out
    assert "text line 2" in out and "github token" in out and "ghp_…" in out
    assert "~/.secrets/pw/<NAME>" in out


def test_verbs_refuse_hook_title_and_todo_gripe(
    runtime_with_store: PrecisRuntime,
) -> None:
    hook = {"hook": POSITIVES["password literal"]}
    cases: list[tuple[str, dict[str, object]]] = [
        ("put", {"kind": "memory", "text": "fine", "meta": hook}),
        ("put", {"kind": "memory", "text": "fine", "title": POSITIVES["slack token"]}),
        ("put", {"kind": "todo", "text": "x " + POSITIVES["anthropic key/token"]}),
        ("put", {"kind": "gripe", "text": "x " + POSITIVES["anthropic key/token"]}),
        (
            "edit",
            {
                "kind": "todo",
                "id": 1,
                "mode": "replace",
                "body": POSITIVES["github token"],
            },
        ),
    ]
    for verb, args in cases:
        out = runtime_with_store.dispatch(verb, args)
        assert "BadInput" in out and "refusing to store" in out, (verb, args, out)
        assert _B not in out


def test_edit_modes_refuse_at_the_verb(runtime_with_store: PrecisRuntime) -> None:
    out = _verb(
        runtime_with_store, "put", kind="memory", text="plain body here", title="t"
    )
    ref = _created_id(Response(body=out))
    bad = POSITIVES["bearer token"]
    for kwargs in (
        {"mode": "replace", "text": bad},
        {"mode": "find-replace", "find": "plain", "text": bad},
        {"mode": "insert", "find": "plain", "where": "after", "text": bad},
        {"mode": "replace", "meta": {"hook": bad}},
        {"mode": "replace", "warrant": bad},
        {"mode": "find-replace", "find": "plain", "text": "ok", "reason": bad},
    ):
        out = _verb(runtime_with_store, "edit", kind="memory", id=ref, **kwargs)
        assert "refusing to store" in out and _B not in out, (kwargs, out)
    assert "plain body here" in _verb(runtime_with_store, "get", kind="memory", id=ref)
    ok = _verb(
        runtime_with_store,
        "edit",
        kind="memory",
        id=ref,
        mode="replace",
        text="clean now",
    )
    assert "refusing" not in ok


def test_handlers_mask_for_automated_writers(hub: Hub) -> None:
    """A worker calling the handler directly keeps the record, minus the secret."""
    dsn = "db is " + POSITIVES["url with inline password"] + "\nnext line"
    mem = MemoryHandler(hub=hub)
    ref = _created_id(mem.put(text=dsn, title="t"))
    body = mem.get(id=ref).body
    assert "<redacted:url with inline password>" in body
    assert "hunter2hunter" not in body and "next line" in body
    mem.edit(id=ref, mode="replace", text="again " + POSITIVES["github token"])
    body = mem.get(id=ref).body
    assert "<redacted:github token>" in body and _B not in body
    assert mask_secrets("clean text") == "clean text"
    assert mask_secrets(None) is None
    todo = TodoHandler(hub=hub).put(text="boom " + POSITIVES["anthropic key/token"])
    assert _B not in todo.body
    gr = GripeHandler(hub=hub).put(text="log: " + POSITIVES["slack token"])
    assert "refusing" not in gr.body


def test_review_todo_one_call_and_manifest(hub: Hub, tmp_path) -> None:
    handler = MemoryHandler(hub=hub)
    ref = _created_id(handler.put(text="stale advice", title="Stale", tags=[SPACE_TAG]))
    other = _created_id(handler.put(text="fine", title="Fine", tags=[SPACE_TAG]))
    resp = TodoHandler(hub=hub).put(
        text=f"review memory {handle_registry.try_format('memory', ref)}: stale?",
        body="Misled me: x. Found so far: y.",
        tags=["memory-review"],
        meta={"llm_tier": "opus"},
        link=f"memory:{ref}",
        rel="raises-concern-about",
    )
    assert "error" not in resp.body.lower(), resp.body
    export_memory_nodes(hub.live_store, tmp_path / "n")
    rows = {
        r[0]: r
        for r in (
            ln.split("\t")
            for ln in (tmp_path / "n" / "_sections.tsv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    h = handle_registry.try_format
    cell = rows[h("memory", ref)][12]
    assert cell.startswith("td") and ":" in cell, cell
    assert rows[h("memory", other)][12] == ""


def test_edit_reason_lands_in_the_log(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    ref = _created_id(handler.put(text="old claim here", title="t"))
    handler.edit(
        id=ref,
        mode="find-replace",
        find="old",
        text="new",
        reason="misled: claim was stale, found via git log",
    )
    log = handler.get(id=ref, view="log").body
    assert "reason='misled: claim was stale, found via git log'" in log, log
