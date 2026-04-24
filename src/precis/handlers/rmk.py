"""rmk: scheme — push PDFs and ebooks to a reMarkable tablet.

The reMarkable is an e-ink reader tablet (rM1 / rM2 / rMPro) designed
for PDFs and ebooks, with a Marker pen for highlights and handwritten
notes.  This handler lets precis push documents onto the tablet so
they land in the agent's reading library alongside papers fetched
from the open web.

Transport: cloud mode (reMarkable Connect subscription).  Gated on
``REMARKABLE_TOKEN`` — the kind is hidden from the agent enum when
the token isn't set, via :class:`~precis.protocol.KindSpec`'s
``requires`` field.  The underlying client code comes from the
``remarkable-mcp`` package, which is pulled in via the optional
``[remarkable]`` extra.

Scope: ``put(mode='push')`` only.  Read-side helpers (highlight
extraction, handwriting OCR, tablet-library listing) are deliberately
out of scope for this release; see ``remarkable_mcp.extract`` for the
library code that will back them in a later phase.

Agent usage::

    put(id='rmk:/path/to/paper.pdf', mode='push')
    put(id='rmk:/path/book.epub', text='Custom Display Name', mode='push')
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# Only the formats the reMarkable Cloud API natively accepts.  EPUB and
# PDF are the two document types the tablet renders; other formats
# (DOCX, MOBI, AZW, TXT) would need conversion first — out of scope.
_SUPPORTED_EXT: dict[str, str] = {".pdf": "pdf", ".epub": "epub"}


class RmkHandler(Handler):
    """Handler for ``rmk:`` — push a PDF or EPUB to the reMarkable cloud.

    The reMarkable is a paper-like e-ink tablet marketed for reading
    PDFs and ebooks and annotating them with a Marker pen.  This
    handler is write-only; the agent's verb of interest is::

        put(id='rmk:/<absolute-local-path>', mode='push')

    A second argument ``text`` overrides the on-tablet display name
    (default: the file stem).  On success the response includes the
    tablet-side document ID so the agent can link the tablet copy back
    to whatever source ref (paper, book, …) prompted the push.
    """

    scheme = "rmk"
    writable = True
    views: ClassVar[set[str]] = {"help"}
    allowed_modes: ClassVar[set[str]] = {"push"}

    def __init__(self) -> None:
        self._client: Any = None  # RemarkableClient | None, lazy

    # ---- Client init (lazy, gated) ----------------------------------

    def _get_client(self) -> Any:
        """Return a RemarkableClient, building it on first use.

        Raises :class:`PrecisError` with ``KIND_UNAVAILABLE`` when the
        optional dependency is missing or the token env var is unset,
        so the agent sees a unified error rather than a stacktrace.
        Note that :func:`~precis.registry.visible_kinds` already hides
        the kind from the tool-schema enum when ``REMARKABLE_TOKEN`` is
        absent — this check is defence-in-depth for direct URI calls
        that bypass the enum.
        """
        if self._client is not None:
            return self._client
        try:
            from remarkable_mcp.sync import load_client_from_token
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "remarkable-mcp package not installed. "
                "Install with: pip install 'precis-mcp[remarkable]'",
            ) from exc
        token = os.environ.get("REMARKABLE_TOKEN", "").strip()
        if not token:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "REMARKABLE_TOKEN environment variable is not set. "
                "Register a device with `remarkable-mcp --register <code>` "
                "after getting a one-time code from "
                "https://my.remarkable.com/device/desktop/connect.",
            )
        self._client = load_client_from_token(token)
        return self._client

    # ---- Core read (onboarding only) --------------------------------

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        """Write-only kind — reads return the onboarding help text."""
        return _help_text()

    # ---- Write surface ----------------------------------------------

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs: Any,
    ) -> str:
        """Push a local PDF/EPUB to the reMarkable cloud.

        ``path`` is the absolute local filesystem path of the file to
        push (with or without a leading ``/`` — both are accepted, but
        the resolved path must be absolute).  ``text`` is an optional
        display-name override; when empty, the tablet shows the file
        stem.  ``mode`` must be ``'push'``.
        """
        if mode != "push":
            raise PrecisError(
                ErrorCode.MODE_UNSUPPORTED,
                cause=f"rmk: mode must be 'push', got {mode!r}",
                next=(
                    "put(id='rmk:/absolute/path/to/file.pdf', mode='push') "
                    "— upload PDF/EPUB to the reMarkable cloud"
                ),
            )

        # The scheme strips the ``rmk:`` prefix; ``path`` is what comes
        # after.  URIs of the form ``rmk:/foo/bar.pdf`` arrive here as
        # ``/foo/bar.pdf`` already — no extra normalisation needed.
        raw = (path or "").strip()
        if not raw:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="rmk: path is required",
                next="put(id='rmk:/absolute/path/to/file.pdf', mode='push')",
            )

        src = Path(raw).expanduser()
        if not src.is_absolute():
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rmk: path must be absolute, got {raw!r}",
                next="put(id='rmk:/absolute/path/to/file.pdf', mode='push')",
            )
        if not src.exists():
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"rmk: file not found: {src}",
            )
        if not src.is_file():
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rmk: not a regular file: {src}",
            )

        ext = src.suffix.lower()
        file_type = _SUPPORTED_EXT.get(ext)
        if file_type is None:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rmk: unsupported extension {ext!r} (file {src.name!r})",
                next="rmk: accepts .pdf and .epub only",
                options=sorted(_SUPPORTED_EXT),
            )

        display_name = (text or "").strip() or src.stem
        data = src.read_bytes()

        client = self._get_client()  # KIND_UNAVAILABLE if token missing
        try:
            doc = client.upload(name=display_name, data=data, file_type=file_type)
        except Exception as exc:
            # upload() wraps HTTP errors in its own exceptions; flatten
            # them to the shared upstream-error code so the agent can
            # retry or surface the failure uniformly.
            raise PrecisError(
                ErrorCode.UPSTREAM_ERROR,
                f"reMarkable cloud upload failed: {exc}",
            ) from exc

        return _format_upload_result(doc, src, display_name, file_type, len(data))


# ---------------------------------------------------------------------------
# Response formatting helpers — kept module-level so tests can exercise them
# directly without standing up a full Handler instance.
# ---------------------------------------------------------------------------


def _help_text() -> str:
    """Return the onboarding help body for ``get(id='rmk:')``."""
    return (
        "# rmk: — push to reMarkable tablet\n"
        "\n"
        "Push PDFs and EPUBs to your reMarkable e-ink tablet via the\n"
        "reMarkable Cloud API.  Documents arrive in the tablet's root\n"
        "folder and become available for reading + annotation on next\n"
        "cloud sync.\n"
        "\n"
        "## Usage\n"
        "\n"
        "```\n"
        "put(id='rmk:/absolute/path/to/paper.pdf', mode='push')\n"
        "put(id='rmk:/path/book.epub', text='Custom Title', mode='push')\n"
        "```\n"
        "\n"
        "## Requirements\n"
        "\n"
        "- `REMARKABLE_TOKEN` env var — obtain via\n"
        "  `remarkable-mcp --register <one-time-code>` after fetching\n"
        "  a one-time code from\n"
        "  https://my.remarkable.com/device/desktop/connect\n"
        "- `precis-mcp[remarkable]` extra installed\n"
        "- Files must be `.pdf` or `.epub` (reMarkable's native formats)\n"
        "- Paths must be absolute on the host where precis runs\n"
        "\n"
        "## Background\n"
        "\n"
        "The reMarkable is an e-ink reader tablet (rM1 / rM2 / rMPro)\n"
        "designed for PDFs and ebooks, with a Marker pen for highlights\n"
        "and handwritten notes.  Pulling highlights + OCR'd notes back\n"
        "from the tablet is planned for a later release; see\n"
        "`remarkable_mcp.extract` for the underlying library.\n"
    )


def _format_upload_result(
    doc: Any,
    src: Path,
    display_name: str,
    file_type: str,
    size_bytes: int,
) -> str:
    """Render the success response for a completed push."""
    return (
        "# Pushed to reMarkable\n"
        "\n"
        f"- **File:** `{src.name}` ({file_type.upper()}, "
        f"{size_bytes / 1024:.1f} KB)\n"
        f"- **Display name:** {display_name}\n"
        f"- **Document ID:** `{doc.id}`\n"
        f"- **Source:** `{src}`\n"
        "\n"
        "The document is now on your reMarkable tablet and will sync\n"
        "to the device on next wake or cloud refresh.\n"
    )
