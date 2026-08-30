"""Send a compiled draft PDF to the reMarkable cloud (send-to-tablet).

A thin, deterministic wrapper over the ``rmapi`` CLI (the maintained
``ddvk/rmapi`` fork) — a single Go binary that speaks the reMarkable sync
protocol and uploads a PDF non-interactively (``rmapi put <pdf>
<folder>``). We shell out (mirroring how ``compile.py`` drives latexmk)
rather than reimplement the moving-target cloud protocol in Python, and
because a bundled binary needs no Python client that breaks on the next
sync-protocol bump.

Auth — per-user first, deployment-wide fallback, both in the secrets vault
or the environment, never in plaintext ``app_settings``:

* ``REMARKABLE_RMAPI_CONFIG:<login>`` — one signed-in user's own paired
  device, self-service from ``/account`` (:func:`register_device` exchanges
  a one-time pairing code for this; the account page's "advanced" box also
  accepts a pasted config/token for when pairing has drifted). Checked
  first when a ``login`` is given — a user who paired their own tablet is
  never silently overridden by the deployment-wide device.
* ``REMARKABLE_RMAPI_CONFIG`` — the body of an ``rmapi`` config file
  (produced once by interactive ``rmapi`` registration; at minimum
  ``devicetoken: <token>`` — rmapi refreshes the short-lived usertoken
  itself). Written verbatim to a temp file pointed at by ``RMAPI_CONFIG``.
  Deployment-wide fallback for users who haven't paired their own.
* ``REMARKABLE_TOKEN`` — fallback: a bare device token (the
  cluster-provisioned ``vault_remarkable_token``), wrapped into a minimal
  config.

Container path — when ``PRECIS_REMARKABLE_IMAGE`` is set, :func:`send_pdf`
delegates to :func:`send_via_container`: it stages the PDF + a tiny params
blob on a bind mount, passes the credential to a one-shot ``docker run`` of
the ``precis-remarkable`` image **by env key** (never on argv), and parses
the container's ``result.json``. This keeps the foreign ``rmapi`` binary +
its cloud egress off the worker host (docker/remarkable). Unset ⇒ the
in-process on-PATH ``rmapi`` path below (dev + tests, via the stub binary).

The web button / CLI gate on :func:`remarkable_configured` (no credential
→ no affordance); the upload itself runs off the request in a worker job
(a slow network op).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis import secrets

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Secret holding the full ``rmapi`` config body (preferred), deployment-wide.
_CONFIG_SECRET = "REMARKABLE_RMAPI_CONFIG"
#: Fallback secret: a bare device token (cluster ``vault_remarkable_token``).
_TOKEN_SECRET = "REMARKABLE_TOKEN"

#: A reMarkable cloud folder path we'll accept as an upload destination.
#: Absolute, and restricted to a safe character set — the call is an arg
#: list (no shell), but we still reject odd paths rather than surprise the
#: device with them.
_FOLDER_RE = re.compile(r"^/[A-Za-z0-9 _/-]*$")

#: A document's visible name on the tablet — sanitised from the draft title.
_NAME_SANITISE = re.compile(r"[^A-Za-z0-9 _.-]+")

#: reMarkable's one-time-code → device-token registration endpoint. Fixed
#: (not agent-supplied), overridable via ``PRECIS_RMAPI_REGISTER_URL`` for
#: tests and the day this URL drifts — which it already did once:
#: ``my.remarkable.com/token/json/2/device/new`` started answering 405 to
#: POST (2026-08-30); ``webapp-prod`` is what ``rmapi`` itself registers
#: against.
_REGISTER_URL_DEFAULT = (
    "https://webapp-prod.cloud.remarkable.engineering/token/json/2/device/new"
)

#: The 8-character one-time pairing code from
#: https://my.remarkable.com/device/apps/connect — case-insensitive.
_PAIRING_CODE_RE = re.compile(r"^[a-z0-9]{8}$")


class PairingError(RuntimeError):
    """A :func:`register_device` exchange failed.

    The message is written to be shown to the user directly (rendered
    inline on ``/account``, never a 500) — a rejected or expired one-time
    code, or the registration endpoint being unreachable.
    """


def user_config_secret(login: str) -> str:
    """Vault name holding one user's own paired ``rmapi`` config body.

    Same colon-prefixed-per-login shape as
    :func:`precis.users.feed_token_secret_name`: no shell exports a name
    with a colon in it, so a per-user credential can't be shadowed by a
    stray environment variable of the deployment-wide name.
    """
    return f"{_CONFIG_SECRET}:{login}"


def _rmapi_bin() -> str:
    """The rmapi binary — overridable via ``PRECIS_RMAPI_BIN`` (a stub
    binary in tests, like ``PRECIS_LATEXMK_BIN``)."""
    return os.environ.get("PRECIS_RMAPI_BIN", "rmapi")


def have_rmapi() -> bool:
    """True when the rmapi binary is resolvable on PATH."""
    return shutil.which(_rmapi_bin()) is not None


def remarkable_configured(
    store: Store | None = None, *, login: str | None = None
) -> bool:
    """True when a reMarkable credential is available (vault/env). This is
    the gate for the web button and CLI — a bare token or a full config
    both count, and so does ``login``'s own paired device when one is
    given. Does **not** check the binary (report that separately so a
    misconfigured host gives a precise error, not a silent no-op)."""
    if login and secrets.is_available(user_config_secret(login), store=store):
        return True
    return secrets.is_available(_CONFIG_SECRET, store=store) or secrets.is_available(
        _TOKEN_SECRET, store=store
    )


def user_remarkable_configured(store: Store | None, login: str) -> bool:
    """True when ``login`` has paired their *own* device — no deployment-wide
    fallback. For ``/account``'s status line, which must say "paired" only
    when this user actually did the pairing, not when the shared device
    happens to be covering for them."""
    return secrets.is_available(user_config_secret(login), store=store)


def _config_body(store: Store | None, login: str | None = None) -> str | None:
    """The rmapi config file body to write, from the vault/env.

    ``login``'s own paired device wins when given and present; then the
    deployment-wide full config; then a bare deployment-wide device token
    wrapped into a minimal one. ``None`` when nothing is configured."""
    if login:
        body = secrets.get_secret(user_config_secret(login), store=store)
        if body:
            return body if "token" in body else f"devicetoken: {body.strip()}\n"
    body = secrets.get_secret(_CONFIG_SECRET, store=store)
    if body:
        return body if "token" in body else f"devicetoken: {body.strip()}\n"
    token = secrets.get_secret(_TOKEN_SECRET, store=store)
    if token:
        return f"devicetoken: {token.strip()}\n"
    return None


def set_user_config(store: Store, login: str, body: str) -> None:
    """Store ``login``'s own paired device credential.

    ``body`` may be a full ``rmapi`` config or a bare device token — pasted
    from the "advanced" box on ``/account`` or produced by
    :func:`register_device`; normalised into a config body the same way
    :func:`_config_body` resolves a bare token, so both shapes work.
    """
    normalised = body if "token" in body else f"devicetoken: {body.strip()}\n"
    secrets.set_secret(user_config_secret(login), normalised, store=store)


def clear_user_config(store: Store, login: str) -> bool:
    """Unpair ``login``'s own device — remove the vault entry.

    Returns ``False`` on a vault outage, mirroring
    :func:`precis.users.forget_feed_token`: say so rather than let a
    "revoked" credential silently keep working.
    """
    try:
        secrets.delete_secret(user_config_secret(login), store=store)
    except Exception:  # pragma: no cover - vault outage
        log.warning(
            "remarkable: paired device for %s cleared but still in the vault",
            login,
            exc_info=True,
        )
        return False
    return True


def register_device(code: str, *, timeout_s: float = 15.0) -> str:
    """Exchange a one-time pairing code for a reMarkable device-config body.

    ``code`` is the 8-character code shown at
    https://my.remarkable.com/device/apps/connect — validated (8
    alphanumeric characters), lowercased, and stripped before use. POSTs to
    the reMarkable device-registration endpoint (a FIXED module constant,
    not agent-supplied input — this is not an SSRF surface, so a direct
    ``httpx`` call is used rather than ``safe_fetch``) with an empty bearer
    token, as ``rmapi``'s own registration does. The header value is
    ``"Bearer"`` with **no** trailing space: ``rmapi`` (Go) sends
    ``"Bearer "``, but h11 rejects a trailing-whitespace header value
    outright (``LocalProtocolError: Illegal header value b'Bearer '``), and
    a compliant server strips trailing OWS anyway — the two are identical
    on the wire once parsed.

    The reMarkable sync protocol churns and this repo does not attempt to
    speak the rest of it — this one registration call is the only piece we
    speak natively. If it drifts, the "advanced" paste-a-config-or-token
    box on ``/account`` still works (register elsewhere, paste the result
    here).

    Raises :class:`PairingError` with a message safe to show the user on a
    non-200 response or a network error. One-time codes are single-use and
    expire quickly, so a rejected code most often means: generate a fresh
    one and retry promptly.
    """
    normalised = code.strip().lower()
    if not _PAIRING_CODE_RE.match(normalised):
        raise PairingError(
            "that doesn't look like an 8-character pairing code — copy a "
            "fresh one from https://my.remarkable.com/device/apps/connect"
        )
    url = os.environ.get("PRECIS_RMAPI_REGISTER_URL") or _REGISTER_URL_DEFAULT
    import httpx

    try:
        resp = httpx.post(
            url,
            json={
                "code": normalised,
                "deviceDesc": "desktop-linux",
                "deviceID": str(uuid.uuid4()),
            },
            headers={"Authorization": "Bearer"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise PairingError(
            f"could not reach the reMarkable pairing service: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise PairingError(
            f"reMarkable rejected that code (status {resp.status_code}) — "
            "one-time codes are single-use and expire within a few minutes; "
            "generate a fresh one and try again."
        )
    token = resp.text.strip()
    if not token:
        raise PairingError("reMarkable's pairing service returned an empty token")
    return f"devicetoken: {token}\n"


def _safe_name(name: str) -> str:
    """A tablet-visible document name from a draft title (sanitised, bounded)."""
    cleaned = _NAME_SANITISE.sub(" ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "draft")[:120]


@dataclass
class SendResult:
    """Outcome of one send-to-reMarkable attempt."""

    ok: bool
    folder: str
    name: str
    returncode: int
    output: str
    skipped: bool = False  # no binary / no credential → not attempted
    error: str = ""


def send_pdf(
    pdf_path: Path | str,
    *,
    folder: str = "/",
    display_name: str | None = None,
    store: Store | None = None,
    login: str | None = None,
    timeout_s: int | None = None,
) -> SendResult:
    """Upload a compiled PDF to the reMarkable cloud under ``folder``.

    Never raises on an upload failure — returns ``ok=False`` with the
    process output (the worker records it on the todo page). ``skipped=True``
    when the binary or credential is missing (a configuration gap, not a
    failed upload). ``display_name`` sets the document's visible title on the
    tablet (defaults to the PDF's stem); the file is staged under that name
    so the tablet doesn't show ``main``. ``login``, when given, resolves
    that user's own paired device first (see :func:`_config_body`) before
    falling back to the deployment-wide credential.
    """
    pdf_path = Path(pdf_path)
    name = _safe_name(display_name or pdf_path.stem)
    # Validate the payload + destination first — shared by both paths.
    if not pdf_path.is_file():
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            error=f"pdf not found: {pdf_path}",
        )
    if not _FOLDER_RE.match(folder):
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            error=f"unsafe reMarkable folder: {folder!r}",
        )
    body = _config_body(store, login)
    if body is None:
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            skipped=True,
            error="no reMarkable credential configured",
        )
    if timeout_s is None:
        timeout_s = int(os.environ.get("PRECIS_RMAPI_TIMEOUT_S", "120"))

    # Container path: an image is configured → run rmapi in a throwaway box so
    # the foreign binary + cloud egress never touch the worker host.
    image = _remarkable_image()
    if image:
        return send_via_container(
            pdf_path,
            folder=folder,
            name=name,
            body=body,
            image=image,
            timeout_s=timeout_s,
        )

    # In-process path: rmapi must be on PATH (dev + tests, via the stub).
    if not have_rmapi():
        log.warning("send_pdf: rmapi not on PATH; skipping (install ddvk/rmapi)")
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            skipped=True,
            error="rmapi binary not installed",
        )

    with tempfile.TemporaryDirectory(prefix="rmapi-") as td:
        tmp = Path(td)
        cfg = tmp / "rmapi.conf"
        cfg.write_text(body, encoding="utf-8")
        cfg.chmod(0o600)
        # Stage the PDF under the tablet-visible name (rmapi names the doc
        # after the uploaded file's stem).
        staged = tmp / f"{name}.pdf"
        shutil.copyfile(pdf_path, staged)
        env = {**os.environ, "RMAPI_CONFIG": str(cfg)}
        # Best-effort create the destination folder (root always exists).
        # rmapi's mkdir is NOT recursive, so a nested folder like
        # "/Precis/173020" needs each ancestor created in turn; an "already
        # exists" failure at any step is fine — the put is what matters.
        if folder not in ("", "/"):
            parts = [p for p in folder.strip("/").split("/") if p]
            for i in range(len(parts)):
                _run(
                    [_rmapi_bin(), "mkdir", "/" + "/".join(parts[: i + 1])],
                    env,
                    timeout_s,
                )
        proc = _run([_rmapi_bin(), "put", str(staged), folder], env, timeout_s)

    if proc is None:
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            error=f"rmapi timed out after {timeout_s}s",
        )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0
    return SendResult(
        ok=ok,
        folder=folder,
        name=name,
        returncode=proc.returncode,
        output=out[-2000:],
        error="" if ok else "rmapi upload failed",
    )


def _run(
    cmd: list[str], env: dict[str, str], timeout_s: int
) -> subprocess.CompletedProcess[str] | None:
    """Run an rmapi subcommand; ``None`` on timeout. Never raises."""
    log.info("rmapi: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None


# ── Container path (docker/remarkable) ─────────────────────────────


def _remarkable_image() -> str | None:
    """The ``precis-remarkable`` image to run the upload in, or ``None`` to use
    the in-process on-PATH rmapi. Set ``PRECIS_REMARKABLE_IMAGE`` on the worker
    to route the send through the container."""
    return (os.environ.get("PRECIS_REMARKABLE_IMAGE") or "").strip() or None


def _container_bin() -> str:
    """The container CLI: explicit ``PRECIS_CONTAINER_BIN`` / ``PRECIS_PODMAN_BIN``
    wins (even if not on PATH — it still goes in the argv), else the detected
    runtime (docker/OrbStack on the Macs, podman on Linux), else ``podman``.
    Mirrors ``workers.executors.agent_container._container_bin``."""
    explicit = os.environ.get("PRECIS_CONTAINER_BIN") or os.environ.get(
        "PRECIS_PODMAN_BIN"
    )
    if explicit:
        return explicit
    try:
        from precis.workers.capability_probe import container_runtime

        return container_runtime() or "podman"
    except Exception:  # pragma: no cover — never break a send on a probe hiccup
        return "podman"


def build_container_argv(
    container_bin: str,
    *,
    image: str,
    in_dir: Path,
    out_dir: Path,
    network: str | None = None,
) -> list[str]:
    """The ``docker/podman run`` argv for one send (pure — asserted by tests).

    Invariants: ``--rm``; the ``in`` mount read-only + the ``out`` mount
    writable; the credential passed ``--env REMARKABLE_RMAPI_CONFIG`` **by KEY
    only** (no ``=value`` — the value is inherited from the run's env, never in
    argv / ref_events); then the ``image`` (default ``CMD`` runs the
    entrypoint). No ``--network none`` — the upload needs cloud egress; an
    explicit ``network`` (e.g. a named bridge) is appended when given.
    """
    argv = [
        container_bin,
        "run",
        "--rm",
        "-v",
        f"{in_dir}:/work/in:ro",
        "-v",
        f"{out_dir}:/work/out",
        "--env",
        _CONFIG_SECRET,  # KEY only — the value rides the run env, not argv
    ]
    if network:
        argv += ["--network", network]
    argv.append(image)
    return argv


def send_via_container(
    pdf_path: Path | str,
    *,
    folder: str,
    name: str,
    body: str,
    image: str,
    timeout_s: int,
) -> SendResult:
    """Upload a PDF by running the ``precis-remarkable`` image one-shot.

    Stages the PDF + a params blob under a scratch ``in``/``out`` pair (under
    ``PRECIS_REMARKABLE_SCRATCH`` when set — a colima-shared path on macOS),
    runs the container with the credential passed by key, and parses
    ``out/result.json`` into a :class:`SendResult`. Never raises — a missing
    result / non-zero exit becomes ``ok=False`` with the captured output.
    """
    pdf_path = Path(pdf_path)
    scratch_root = os.environ.get("PRECIS_REMARKABLE_SCRATCH") or None
    network = (os.environ.get("PRECIS_REMARKABLE_NETWORK") or "").strip() or None
    with tempfile.TemporaryDirectory(prefix="rm-send-", dir=scratch_root) as td:
        root = Path(td)
        in_dir = root / "in"
        out_dir = root / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        shutil.copyfile(pdf_path, in_dir / "doc.pdf")
        (in_dir / "params.json").write_text(
            json.dumps({"folder": folder, "name": name, "timeout_s": timeout_s}),
            encoding="utf-8",
        )
        argv = build_container_argv(
            _container_bin(),
            image=image,
            in_dir=in_dir,
            out_dir=out_dir,
            network=network,
        )
        # Pass the credential by key: put it in the run's env so ``--env KEY``
        # (built above) inherits the value — it never lands on the command line.
        env = {**os.environ, _CONFIG_SECRET: body}
        # Allow the container run a little longer than the inner rmapi timeout
        # (image start-up + the bind-mount round-trip).
        proc = _run(argv, env, timeout_s + 30)
        res_path = out_dir / "result.json"
        data: dict[str, object] | None = None
        if res_path.is_file():
            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:  # pragma: no cover — a truncated blob is a failure
                data = None

    if proc is None:
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=-1,
            output="",
            error=f"container timed out after {timeout_s + 30}s",
        )
    if data is None:
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return SendResult(
            ok=False,
            folder=folder,
            name=name,
            returncode=proc.returncode,
            output=out[-2000:],
            error="container produced no result.json",
        )
    ok = bool(data.get("ok"))
    return SendResult(
        ok=ok,
        folder=str(data.get("folder") or folder),
        name=str(data.get("name") or name),
        returncode=int(data.get("returncode", proc.returncode)),  # type: ignore[call-overload]
        output=str(data.get("output") or "")[-2000:],
        error="" if ok else str(data.get("error") or "rmapi upload failed"),
    )


__all__ = [
    "PairingError",
    "SendResult",
    "build_container_argv",
    "clear_user_config",
    "have_rmapi",
    "register_device",
    "remarkable_configured",
    "send_pdf",
    "send_via_container",
    "set_user_config",
    "user_config_secret",
    "user_remarkable_configured",
]
