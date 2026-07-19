"""Send a compiled draft PDF to the reMarkable cloud (send-to-tablet).

A thin, deterministic wrapper over the ``rmapi`` CLI (the maintained
``ddvk/rmapi`` fork) — a single Go binary that speaks the reMarkable sync
protocol and uploads a PDF non-interactively (``rmapi put <pdf>
<folder>``). We shell out (mirroring how ``compile.py`` drives latexmk)
rather than reimplement the moving-target cloud protocol in Python, and
because a bundled binary needs no Python client that breaks on the next
sync-protocol bump.

Auth — the device credential lives in the secrets vault (ADR 0055) or the
environment, never in plaintext ``app_settings``:

* ``REMARKABLE_RMAPI_CONFIG`` — the body of an ``rmapi`` config file
  (produced once by interactive ``rmapi`` registration; at minimum
  ``devicetoken: <token>`` — rmapi refreshes the short-lived usertoken
  itself). Written verbatim to a temp file pointed at by ``RMAPI_CONFIG``.
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis import secrets

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Secret holding the full ``rmapi`` config body (preferred).
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


def _rmapi_bin() -> str:
    """The rmapi binary — overridable via ``PRECIS_RMAPI_BIN`` (a stub
    binary in tests, like ``PRECIS_LATEXMK_BIN``)."""
    return os.environ.get("PRECIS_RMAPI_BIN", "rmapi")


def have_rmapi() -> bool:
    """True when the rmapi binary is resolvable on PATH."""
    return shutil.which(_rmapi_bin()) is not None


def remarkable_configured(store: Store | None = None) -> bool:
    """True when a reMarkable credential is available (vault/env). This is
    the gate for the web button and CLI — a bare token or a full config
    both count. Does **not** check the binary (report that separately so a
    misconfigured host gives a precise error, not a silent no-op)."""
    return secrets.is_available(_CONFIG_SECRET, store=store) or secrets.is_available(
        _TOKEN_SECRET, store=store
    )


def _config_body(store: Store | None) -> str | None:
    """The rmapi config file body to write, from the vault/env. The full
    config wins; otherwise a bare device token is wrapped into a minimal
    one. ``None`` when neither is configured."""
    body = secrets.get_secret(_CONFIG_SECRET, store=store)
    if body:
        return body if "token" in body else f"devicetoken: {body.strip()}\n"
    token = secrets.get_secret(_TOKEN_SECRET, store=store)
    if token:
        return f"devicetoken: {token.strip()}\n"
    return None


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
    timeout_s: int | None = None,
) -> SendResult:
    """Upload a compiled PDF to the reMarkable cloud under ``folder``.

    Never raises on an upload failure — returns ``ok=False`` with the
    process output (the worker records it on the task page). ``skipped=True``
    when the binary or credential is missing (a configuration gap, not a
    failed upload). ``display_name`` sets the document's visible title on the
    tablet (defaults to the PDF's stem); the file is staged under that name
    so the tablet doesn't show ``main``.
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
    body = _config_body(store)
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
        # Best-effort create the destination folder (root always exists);
        # a "already exists" failure here is fine — the put is what matters.
        if folder not in ("", "/"):
            _run([_rmapi_bin(), "mkdir", folder], env, timeout_s)
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
    "SendResult",
    "build_container_argv",
    "have_rmapi",
    "remarkable_configured",
    "send_pdf",
    "send_via_container",
]
