"""Thin shim around `python-epo-ops-client` for the ``patent`` kind.

Why a shim:

* ``epo_ops`` is an optional dependency. The shim's import-side is
  intentionally lazy so ``import precis.handlers.patent`` works even
  when the package isn't installed; the registry hides the kind in
  that case before any client method is called.
* The handler needs a small, testable surface (four methods); the
  upstream library exposes a much wider one. Wrapping it gives us a
  ``Protocol`` to mock in unit tests without spinning up a fake HTTP
  server.

Public surface:

    OpsClient(key, secret, *, user_agent=None)
        .biblio(docdb)        -> bytes (XML)
        .description(docdb)   -> bytes (XML)
        .claims(docdb)        -> bytes (XML)
        .search(cql, *, range=...) -> bytes (XML)

All methods return raw XML bytes — ST.36 / SMI XML for the document
endpoints, OPS search response XML for the search endpoint. Parsing
lives in ``_patent_xml.py``.

Errors:
    OpsAuthError   — bad/expired credentials.
    OpsHttpError   — any other non-2xx, with status + body preview.
    OpsQuotaError  — fair-use quota exceeded (X-Throttling-Control).

The live integration test (``PRECIS_PATENT_TEST_LIVE=1``) hits real
OPS; unit tests use a ``FakeOpsClient`` from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OpsError(Exception):
    """Base class for OPS-shim errors."""


class OpsAuthError(OpsError):
    """Bad/expired EPO_OPS credentials."""


class OpsQuotaError(OpsError):
    """OPS reports fair-use quota exceeded (HTTP 403 with throttling)."""


class OpsHttpError(OpsError):
    """Any other OPS HTTP failure."""

    def __init__(self, status: int, body_preview: str) -> None:
        super().__init__(f"OPS HTTP {status}: {body_preview[:200]}")
        self.status = status
        self.body_preview = body_preview


class OpsNotFound(OpsError):
    """OPS returned 404 — the patent doesn't exist (or isn't published)."""


# ---------------------------------------------------------------------------
# Result type for search()
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpsSearchResponse:
    """Search response — raw XML + cheap byte count for fair-use accounting."""

    xml: bytes
    bytes_out: int  # response size for $WEEK quota tracking


# ---------------------------------------------------------------------------
# Protocol — what the handler depends on
# ---------------------------------------------------------------------------


class OpsClientProto(Protocol):
    """Subset of the live OPS client used by the handler.

    Tests pass an instance of ``FakeOpsClient`` (below) implementing
    the same shape. Live code constructs ``OpsClient`` (also below)
    which wraps ``python-epo-ops-client``.
    """

    def biblio(self, docdb: str) -> bytes: ...

    def description(self, docdb: str) -> bytes: ...

    def claims(self, docdb: str) -> bytes: ...

    def search(
        self, cql: str, *, range_start: int = 1, range_end: int = 25
    ) -> OpsSearchResponse: ...


# ---------------------------------------------------------------------------
# Live client — lazy import of the upstream library
# ---------------------------------------------------------------------------


class OpsClient:
    """Real client. Lazy-imports ``epo_ops`` on first use.

    The constructor doesn't talk to OPS — that happens on first
    method call (the upstream ``Client`` is also lazy on its own
    OAuth dance).
    """

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        user_agent: str | None = None,
    ) -> None:
        if not key or not secret:
            raise OpsAuthError(
                "EPO_OPS_CLIENT_KEY and EPO_OPS_CLIENT_SECRET must be set"
            )
        self._key = key
        self._secret = secret
        self._user_agent = user_agent or "precis-mcp/6.0 (patent kind)"
        self._inner: Any | None = None

    # -- lazy bootstrap -------------------------------------------------

    def _client(self) -> Any:
        if self._inner is not None:
            return self._inner
        try:
            import epo_ops  # type: ignore[import-not-found]
        except ImportError as e:
            raise OpsError(
                "python-epo-ops-client is not installed; "
                "install with `pip install precis-mcp[patent]`"
            ) from e

        self._inner = epo_ops.Client(
            key=self._key,
            secret=self._secret,
            accept_type="xml",
            middlewares=[
                epo_ops.middlewares.Dogpile(),
                epo_ops.middlewares.Throttler(),
            ],
        )
        return self._inner

    # -- public methods -------------------------------------------------

    def biblio(self, docdb: str) -> bytes:
        return self._published_data(docdb, endpoint="biblio")

    def description(self, docdb: str) -> bytes:
        return self._published_data(docdb, endpoint="description")

    def claims(self, docdb: str) -> bytes:
        return self._published_data(docdb, endpoint="claims")

    def search(
        self,
        cql: str,
        *,
        range_start: int = 1,
        range_end: int = 25,
    ) -> OpsSearchResponse:
        # ``self._client()`` is responsible for surfacing a missing
        # ``epo_ops`` package (it lazy-imports the top-level module).
        # ``search`` itself doesn't need anything from ``epo_ops.models``.
        client = self._client()
        try:
            response = client.published_data_search(
                cql=cql,
                range_begin=range_start,
                range_end=range_end,
            )
        except Exception as e:
            raise self._wrap_exc(e) from e
        body = bytes(response.content)
        return OpsSearchResponse(xml=body, bytes_out=len(body))

    # -- helpers --------------------------------------------------------

    def _published_data(self, docdb: str, *, endpoint: str) -> bytes:
        try:
            import epo_ops.models  # type: ignore[import-not-found]
        except ImportError as e:
            raise OpsError("python-epo-ops-client missing") from e

        # Split lowercased docdb slug back into the parts OPS wants.
        # We import here rather than at module scope to keep the
        # split helper next to its only caller.
        from precis.handlers._patent_slug import parse_docdb_id

        parsed = parse_docdb_id(docdb)
        client = self._client()
        try:
            response = client.published_data(
                reference_type="publication",
                input=epo_ops.models.Docdb(
                    parsed.number,
                    parsed.country.upper(),
                    parsed.kind_full.upper(),
                ),
                endpoint=endpoint,
            )
        except Exception as e:
            raise self._wrap_exc(e) from e
        return bytes(response.content)

    @staticmethod
    def _wrap_exc(exc: BaseException) -> OpsError:
        """Translate upstream errors into our OpsError subclasses.

        ``python-epo-ops-client`` doesn't expose a typed exception
        hierarchy; it raises generic Exception with a string body
        and (sometimes) attaches the HTTP status. We sniff for
        common failure modes by string content + response code.
        """
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        body = str(exc)
        if status == 401 or "auth" in body.lower():
            return OpsAuthError(f"OPS auth failed: {body[:200]}")
        if status == 403 and "throttl" in body.lower():
            return OpsQuotaError(f"OPS quota exceeded: {body[:200]}")
        if status == 404 or "not found" in body.lower():
            return OpsNotFound(f"OPS not found: {body[:200]}")
        if isinstance(status, int):
            return OpsHttpError(status, body)
        return OpsError(f"OPS error: {body[:200]}")


# ---------------------------------------------------------------------------
# Fake client — used by unit tests
# ---------------------------------------------------------------------------


class FakeOpsClient:
    """Pre-loaded responses keyed by ``(endpoint, docdb)`` / ``(search, cql)``.

    Tests construct one with a dict of canned bytes; calls miss the
    network entirely. Use ``raises`` to bind an exception to a key
    instead of bytes.
    """

    def __init__(
        self,
        *,
        biblio: dict[str, bytes] | None = None,
        description: dict[str, bytes] | None = None,
        claims: dict[str, bytes] | None = None,
        searches: dict[str, bytes] | None = None,
        raises: dict[tuple[str, str], OpsError] | None = None,
    ) -> None:
        self._biblio = dict(biblio or {})
        self._description = dict(description or {})
        self._claims = dict(claims or {})
        self._searches = dict(searches or {})
        self._raises = dict(raises or {})
        self.calls: list[tuple[str, str]] = []  # (endpoint, key) — for assertions

    def biblio(self, docdb: str) -> bytes:
        return self._lookup("biblio", docdb, self._biblio)

    def description(self, docdb: str) -> bytes:
        return self._lookup("description", docdb, self._description)

    def claims(self, docdb: str) -> bytes:
        return self._lookup("claims", docdb, self._claims)

    def search(
        self,
        cql: str,
        *,
        range_start: int = 1,
        range_end: int = 25,
    ) -> OpsSearchResponse:
        body = self._lookup("search", cql, self._searches)
        return OpsSearchResponse(xml=body, bytes_out=len(body))

    def _lookup(
        self,
        endpoint: str,
        key: str,
        bag: dict[str, bytes],
    ) -> bytes:
        self.calls.append((endpoint, key))
        if (endpoint, key) in self._raises:
            raise self._raises[(endpoint, key)]
        try:
            return bag[key]
        except KeyError as e:
            raise OpsNotFound(
                f"FakeOpsClient has no {endpoint!r} response for {key!r}"
            ) from e


__all__ = [
    "FakeOpsClient",
    "OpsAuthError",
    "OpsClient",
    "OpsClientProto",
    "OpsError",
    "OpsHttpError",
    "OpsNotFound",
    "OpsQuotaError",
    "OpsSearchResponse",
]
