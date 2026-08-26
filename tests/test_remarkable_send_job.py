"""remarkable_send job_type — registration, the target-folder resolution,
and the plugin dispatch guards against a fake DispatchContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import remarkable_send as rs


def test_remarkable_send_registered() -> None:
    spec = get_job_type("remarkable_send")
    assert spec is not None
    assert spec.dispatch is not None  # runs via plugin dispatch, not claude
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires
    assert "remarkable_send" in known_job_types()


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "remarkable_send"
    events: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    meta_set: dict[str, Any] = field(default_factory=dict)

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)


def _project_and_draft(hub: Hub) -> str:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    DraftHandler(hub=hub).put(id="d1", title="T", project=pid)
    DraftHandler(hub=hub).put(
        id="d1", chunk_kind="paragraph", text="prose.", at={"last": True}
    )
    return "d1"


def test_target_folder_default_setting_and_override(hub: Hub) -> None:
    from precis.budget.settings import set_setting

    ctx = _FakeCtx(store=hub.live_store, meta={})
    # no setting → default
    assert rs._target_folder(ctx, {}) == "/Precis"
    # app_settings value wins over the default
    set_setting(hub.live_store, rs.TARGET_FOLDER_KEY, "/Reading")
    assert rs._target_folder(ctx, {}) == "/Reading"
    # an explicit params.folder wins over the setting
    assert rs._target_folder(ctx, {"folder": "/Inbox"}) == "/Inbox"


def test_dispatch_fails_without_credential(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("credential" in f for f in ctx.failures), ctx.failures


def test_dispatch_fails_without_latexmk(hub: Hub, monkeypatch: Any) -> None:
    # Credential present (so we get past the gate), but the test host has no
    # latexmk → the send fails cleanly before any upload is attempted.
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", "definitely-not-a-real-latexmk-bin")
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("latexmk" in f for f in ctx.failures), ctx.failures


def test_dispatch_fails_on_unknown_draft(hub: Hub) -> None:
    spec = get_job_type("remarkable_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": "nope"}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("no draft" in f for f in ctx.failures)


def test_dispatch_fails_without_credential_mentions_account_pairing(
    hub: Hub, monkeypatch: Any
) -> None:
    """The failure message points at self-service pairing, not just the vault."""
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_send")
    ctx = _FakeCtx(
        store=hub.live_store, meta={"params": {"draft": slug, "user": "reto"}}
    )
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("/account" in f for f in ctx.failures), ctx.failures


def test_dispatch_threads_the_user_param_into_the_credential_gate(
    hub: Hub, monkeypatch: Any
) -> None:
    """A per-user vault credential (no global one) must satisfy the gate
    when ``params.user`` names that login — proves the job threads
    ``params.user`` into ``remarkable_configured(..., login=...)``."""
    from precis import secrets as vault_mod

    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    box = {"REMARKABLE_RMAPI_CONFIG:reto": "devicetoken: t\n"}
    monkeypatch.setattr(
        vault_mod,
        "get_secret",
        lambda n, *, store=None, default=None: box.get(n, default),
    )
    # Latexmk is not installed on the test host, so dispatch fails a step
    # *after* the credential gate — proving the gate itself passed.
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", "definitely-not-a-real-latexmk-bin")
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_send")
    ctx = _FakeCtx(
        store=hub.live_store, meta={"params": {"draft": slug, "user": "reto"}}
    )
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("latexmk" in f for f in ctx.failures), ctx.failures
    assert not any("credential" in f for f in ctx.failures), ctx.failures
