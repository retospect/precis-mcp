"""S3 batch 6 (gr493222): per-request pacing for client libraries.

``paced_requests`` meters every ``requests`` call a library makes to one host
(``mp_api``, ``youtube_transcript_api``), parks the row on a 429/503
Retry-After and swaps the bare ``python-requests`` UA for the honest one.

Mocks only: a canned transport adapter and a recorder over
``rate_limit.require`` / ``park``. No network, no sleeping, no threads.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import requests

from precis.handlers import youtube
from precis.utils import http
from precis.utils.http import limiter_pacer, paced_requests, reset_circuit
from precis.workers.job_types import pourbaix_bulk as pb_job


@dataclass
class Limiter:
    requires: list[str] = field(default_factory=list)
    parks: list[tuple[str, float]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _isolated():
    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> Limiter:
    rec = Limiter()
    monkeypatch.setattr(
        "precis.utils.rate_limit.require",
        lambda provider, **_kw: rec.requires.append(provider),
    )
    monkeypatch.setattr(
        "precis.utils.rate_limit.park",
        lambda provider, seconds: rec.parks.append((provider, seconds)),
    )
    monkeypatch.setattr(http, "_default_sleep", lambda _s: None)
    return rec


class _Canned(requests.adapters.BaseAdapter):
    def __init__(self, *statuses: int, retry_after: str | None = None) -> None:
        super().__init__()
        self.statuses = list(statuses)
        self.retry_after = retry_after
        self.seen: list[requests.PreparedRequest] = []

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        self.seen.append(request)
        resp = requests.Response()
        resp.status_code = self.statuses.pop(0) if self.statuses else 200
        if self.retry_after is not None:
            resp.headers["Retry-After"] = self.retry_after
        resp.request = request
        return resp

    def close(self) -> None:
        return None


def _session(adapter: _Canned) -> requests.Session:
    s = requests.Session()
    s.mount("https://", adapter)
    return s


# ── paced_requests ─────────────────────────────────────────────────────────


def test_each_request_to_the_host_takes_a_token_other_hosts_do_not(
    limiter: Limiter,
) -> None:
    adapter = _Canned()
    sess = _session(adapter)
    with paced_requests("api.example.test", service="materialsproject"):
        sess.get("https://api.example.test/a")
        sess.get("https://sub.api.example.test/b")
        sess.get("https://other.example.test/c")
    assert limiter.requires == ["materialsproject"] * 2
    assert len(adapter.seen) == 3


def test_the_patch_is_removed_after_the_block_even_on_error(limiter: Limiter) -> None:
    original = requests.Session.send
    with pytest.raises(RuntimeError):
        with paced_requests("api.example.test", service="materialsproject"):
            assert requests.Session.send is not original
            raise RuntimeError("boom")
    assert requests.Session.send is original


def test_nested_blocks_keep_the_patch_until_the_last_exits(limiter: Limiter) -> None:
    original = requests.Session.send
    adapter = _Canned()
    sess = _session(adapter)
    with paced_requests("a.example.test", service="materialsproject"):
        with paced_requests("b.example.test", service="youtube", provider="yt-row"):
            sess.get("https://b.example.test/x")
        assert requests.Session.send is not original
        sess.get("https://a.example.test/y")
    assert requests.Session.send is original
    assert limiter.requires == ["yt-row", "materialsproject"]


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_on_429_or_503_parks_the_row(limiter: Limiter, status: int) -> None:
    sess = _session(_Canned(status, retry_after="9"))
    with paced_requests("api.example.test", service="materialsproject"):
        resp = sess.get("https://api.example.test/a")
    assert resp.status_code == status  # the library still sees the failure
    assert limiter.parks == [("materialsproject", 9.0)]


def test_a_500_or_200_does_not_park(limiter: Limiter) -> None:
    sess = _session(_Canned(500, 200, retry_after="9"))
    with paced_requests("api.example.test", service="materialsproject"):
        sess.get("https://api.example.test/a")
        sess.get("https://api.example.test/a")
    assert limiter.parks == []


def test_only_the_default_python_requests_ua_is_replaced(limiter: Limiter) -> None:
    adapter = _Canned()
    sess = _session(adapter)
    ua = http.polite_user_agent()
    with paced_requests("api.example.test", service="materialsproject", user_agent=ua):
        sess.get("https://api.example.test/a")
        sess.get("https://api.example.test/b", headers={"User-Agent": "mp-api/1"})
        sess.get("https://other.example.test/c")
    assert adapter.seen[0].headers["User-Agent"] == ua
    assert adapter.seen[1].headers["User-Agent"] == "mp-api/1"
    assert str(adapter.seen[2].headers["User-Agent"]).startswith("python-requests")


def test_limiter_pacer_takes_one_token_per_call(limiter: Limiter) -> None:
    pace = limiter_pacer("materialsproject")
    pace()
    pace()
    assert limiter.requires == ["materialsproject"] * 2


# ── Materials Project ──────────────────────────────────────────────────────


def _engine(monkeypatch: pytest.MonkeyPatch, adapter: _Canned) -> list[Any]:
    paces: list[Any] = []

    def fetch_entries(
        api_key: str,
        chemsys: str,
        *,
        user_agent: str | None = None,
        pace: Any = None,
    ) -> Any:
        paces.append(pace)
        sess = _session(adapter)
        for path in ("summary", "thermo", "pourbaix"):  # mp-api's own requests
            sess.get(f"https://api.materialsproject.org/{path}")
        return ["entry"]

    def database_version(api_key: str) -> str:
        _session(adapter).get("https://api.materialsproject.org/version")
        return "2026.1"

    engine = types.SimpleNamespace(
        fetch_entries=fetch_entries, database_version=database_version
    )
    monkeypatch.setitem(sys.modules, "precis_dft.pourbaix_bulk", engine)
    return paces


def test_mp_fetch_paces_every_inner_request_plus_the_attempt(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter
) -> None:
    adapter = _Canned()
    paces = _engine(monkeypatch, adapter)
    assert pb_job._mp_fetch("key", "Cu") == ["entry"]
    assert len(adapter.seen) == 3
    assert limiter.requires == ["materialsproject"] * 4  # attempt + 3 requests
    assert callable(paces[0])


def test_mp_version_paces_its_inner_request(
    monkeypatch: pytest.MonkeyPatch, limiter: Limiter
) -> None:
    adapter = _Canned()
    _engine(monkeypatch, adapter)
    assert pb_job._mp_version("key") == "2026.1"
    assert limiter.requires == ["materialsproject"] * 2


def test_ion_reference_pages_after_the_first_are_paced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_dft import pourbaix_bulk as engine

    paced: list[int] = []
    pages = iter([1, 2, 3])

    def fake_get(url: str, **kw: Any) -> httpx.Response:
        assert kw["params"]["page"] == next(pages)
        return httpx.Response(
            200,
            json={"data": [], "total_pages": 3},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    engine.fetch_ion_reference_data("k", pace=lambda: paced.append(1))
    assert paced == [1, 1]


# ── YouTube transcripts ────────────────────────────────────────────────────


def test_youtube_library_requests_are_paced_on_the_host_row_with_honest_ua(
    limiter: Limiter,
) -> None:
    adapter = _Canned()

    def fetch(video_id: str) -> list[str]:
        sess = _session(adapter)
        for path in ("watch", "youtubei", "timedtext"):
            sess.get(f"https://www.youtube.com/{path}")
        return [video_id]

    assert youtube._paced_call(fetch, "abc") == ["abc"]
    assert limiter.requires == ["host:www.youtube.com"] * 3
    for req in adapter.seen:
        http.check_user_agent(str(req.headers["User-Agent"]))
