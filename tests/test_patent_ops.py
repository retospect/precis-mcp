"""Tests for the ``OpsClient`` shim — upstream-error mapping.

gr648500: on the *search* endpoint EPO OPS answers a zero-hit query
with HTTP 404 (its server-side error name is literally
``OpsNotFound``). That is a result, not a failure — ``search()``
must return an empty ``OpsSearchResponse`` so callers see a real
no-match answer instead of an infra-looking ``Upstream`` error.
Document endpoints keep ``OpsNotFound`` (a 404 there genuinely
means "no such patent").

No network: a stub inner client stands in for ``epo_ops.Client``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests

from precis.patent.ops import (
    OpsAuthError,
    OpsClient,
    OpsError,
    OpsHttpError,
    OpsNotFound,
    OpsQuotaError,
)
from precis.patent.xml import parse_search_response


class _StubInner:
    """Stands in for ``epo_ops.Client`` — canned raise/response."""

    def __init__(
        self, *, exc: Exception | None = None, content: bytes = b"<x/>"
    ) -> None:
        self.exc = exc
        self.content = content

    def published_data_search(self, **_kwargs: Any) -> Any:
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.content)


def _ops(*, exc: Exception | None = None, content: bytes = b"<x/>") -> OpsClient:
    client = OpsClient(key="k", secret="s")
    client._inner = _StubInner(exc=exc, content=content)
    return client


def _http_error(status: int) -> requests.HTTPError:
    """A ``requests.HTTPError`` the way ``raise_for_status()`` builds it:
    the status lives on ``.response``, not on the exception itself."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


class TestSearchZeroHit404:
    """A 404 on ``published-data/search`` is OPS's zero-hits answer."""

    def test_404_returns_empty_response(self) -> None:
        ops = _ops(exc=_http_error(404))
        r = ops.search('cpc="B01J27/24"')
        assert r.xml == b""
        assert r.bytes_out == 0
        # …and the empty body parses as a clean no-hit result.
        assert parse_search_response(r.xml) == ([], 0)

    def test_404_body_sniff_without_status(self) -> None:
        # Exceptions that only *say* "not found" (no response
        # attached) hit the same mapping.
        ops = _ops(exc=Exception("OpsNotFound: no results"))
        r = ops.search("ti=test")
        assert r.xml == b""

    def test_non_404_still_raises(self) -> None:
        with pytest.raises(OpsHttpError):
            _ops(exc=_http_error(500)).search("ti=test")
        with pytest.raises(OpsAuthError):
            _ops(exc=_http_error(401)).search("ti=test")


class TestWrapExc:
    """``_wrap_exc`` — status extraction + error-class mapping."""

    def test_status_comes_from_response(self) -> None:
        # ``requests.HTTPError`` has no ``.status_code``; the code
        # lives on ``.response.status_code``.
        assert isinstance(OpsClient._wrap_exc(_http_error(404)), OpsNotFound)
        err = OpsClient._wrap_exc(_http_error(503))
        assert isinstance(err, OpsHttpError)
        assert err.status == 503

    def test_quota_exception_names_map(self) -> None:
        # Upstream's quota exceptions are HTTPError subclasses whose
        # message carries no "throttl" marker — the class name is the
        # signal (``IndividualQuotaPerHourExceeded`` et al.).
        class IndividualQuotaPerHourExceeded(requests.HTTPError):
            pass

        err = IndividualQuotaPerHourExceeded(
            "403 Client Error: Forbidden", response=requests.Response()
        )
        assert err.response is not None
        err.response.status_code = 403
        assert isinstance(OpsClient._wrap_exc(err), OpsQuotaError)

    def test_plain_403_is_http_error_not_quota(self) -> None:
        err = OpsClient._wrap_exc(_http_error(403))
        assert isinstance(err, OpsHttpError)
        assert err.status == 403

    def test_untyped_exception_is_ops_error(self) -> None:
        assert isinstance(OpsClient._wrap_exc(Exception("boom")), OpsError)
