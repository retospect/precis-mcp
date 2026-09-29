"""Checkout watchdog + the two things one shared long-lived server breaks.

The session MCP moves from one stdio container per session to one
streamable-http process serving every session
(``docs/backlog/session-mcp-http-server.md``). Three consequences pinned
here:

- the watchdog that bounces it must key on the checkout's HEAD sha, not
  mtimes, or every editor save restarts everyone's server;
- its exit must drain in-flight tool calls, via a threading counter — the
  obvious ``anyio.Semaphore`` route is unreachable from a daemon thread and
  would pass a drain test vacuously;
- the exit breadcrumb's "exactly once" has to mean once per MCP session,
  not once per process, or eleven of twelve sessions get the silent restart
  gr341515 exists to prevent.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from precis import inflight, install_watchdog, serve_ledger
from precis.install_watchdog import (
    CheckoutWatchdog,
    checkout_fingerprint,
    consume_last_exit_breadcrumb,
    start_checkout_watchdog,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture(autouse=True)
def _no_watchdog_outlives_its_test() -> Iterator[None]:
    """Same reaping contract as ``test_install_watchdog.py`` (gr347099): a
    leaked poll thread calls the *real* ``os._exit(0)`` on an xdist worker
    once monkeypatch has restored the real module state."""
    yield
    for thread in threading.enumerate():
        if isinstance(thread, CheckoutWatchdog):
            thread.stop()
    assert not [t for t in threading.enumerate() if isinstance(t, CheckoutWatchdog)]


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep breadcrumbs out of the real ``~/.cache/precis`` (a persistent
    gate volume) and reset the process-wide in-flight/breadcrumb state both
    sides of each test."""
    monkeypatch.setattr(
        install_watchdog, "_breadcrumb_path", lambda: tmp_path / "last-exit.json"
    )
    install_watchdog._reset_breadcrumb_state_for_tests()
    inflight._reset_for_tests()
    yield
    install_watchdog._reset_breadcrumb_state_for_tests()
    inflight._reset_for_tests()


def _checkout(root: Path, *, sha: str = SHA_A, branch: str = "main") -> Path:
    """A minimal but real ``.git`` layout: HEAD -> loose ref -> sha."""
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (git / "refs" / "heads" / branch).write_text(sha + "\n", encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    return root


# --- fingerprinting --------------------------------------------------------


def test_fingerprint_resolves_a_loose_ref(tmp_path: Path) -> None:
    assert checkout_fingerprint(_checkout(tmp_path)) == SHA_A


def test_fingerprint_resolves_a_detached_head(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    (root / ".git" / "HEAD").write_text(SHA_B + "\n", encoding="utf-8")
    assert checkout_fingerprint(root) == SHA_B


def test_fingerprint_falls_back_to_packed_refs(tmp_path: Path) -> None:
    """A gc'd or freshly-cloned tree has no loose ref file at all."""
    root = _checkout(tmp_path)
    (root / ".git" / "refs" / "heads" / "main").unlink()
    (root / ".git" / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n"
        f"{SHA_B} refs/heads/main\n"
        f"^{SHA_A}\n",
        encoding="utf-8",
    )
    assert checkout_fingerprint(root) == SHA_B


def test_fingerprint_follows_a_gitdir_pointer_file(tmp_path: Path) -> None:
    """Worktrees (this repo's own workflow) have ``.git`` as a *file*."""
    real = _checkout(tmp_path / "real")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {real / '.git'}\n", encoding="utf-8")
    assert checkout_fingerprint(tree) == SHA_A


def test_fingerprint_is_none_for_a_non_checkout(tmp_path: Path) -> None:
    assert checkout_fingerprint(tmp_path) is None


def test_fingerprint_ignores_mtime_churn(tmp_path: Path) -> None:
    """The whole reason this arm exists rather than reusing the install
    arm: ``_fingerprint_for`` refuses source trees because every checkout
    touches mtimes. A sha must not move when only content does."""
    root = _checkout(tmp_path)
    before = checkout_fingerprint(root)
    (root / "src" / "edited.py").write_text("# an editor save\n", encoding="utf-8")
    (root / ".git" / "index").write_text("staged\n", encoding="utf-8")
    assert checkout_fingerprint(root) == before


# --- arming ----------------------------------------------------------------


def test_not_armed_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off everywhere by default — the cluster daemons and the CLI must be
    byte-identical to before this arm existed."""
    monkeypatch.delenv("PRECIS_CHECKOUT_WATCHDOG", raising=False)
    assert start_checkout_watchdog() is None


def test_not_armed_when_watchdogs_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(_checkout(tmp_path)))
    monkeypatch.setenv("PRECIS_INSTALL_WATCHDOG", "0")
    assert start_checkout_watchdog() is None


def test_not_armed_for_a_path_that_is_not_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(tmp_path))
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    assert start_checkout_watchdog() is None


def test_arms_on_a_real_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(_checkout(tmp_path)))
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    thread = start_checkout_watchdog(interval_s=3600.0)
    assert thread is not None
    thread.stop()


def test_wired_into_server_main() -> None:
    """A module nobody calls fixes nothing — ``server.main`` must arm it."""
    from precis import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "start_checkout_watchdog()" in source


# --- the bounce ------------------------------------------------------------


def _run_until_exit(
    thread: CheckoutWatchdog, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[int], threading.Event]:
    """Start ``thread`` with ``os._exit`` stubbed; returns (codes, fired)."""
    codes: list[int] = []
    fired = threading.Event()

    def _fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()
        raise SystemExit  # end the poll thread in place of the process

    monkeypatch.setattr(install_watchdog.os, "_exit", _fake_exit)
    thread.start()
    return codes, fired


def test_editor_save_does_not_bounce_but_a_checkout_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6. One test, both halves — the value is in the contrast: the same
    watchdog must sit through file churn and fire on a HEAD move."""
    root = _checkout(tmp_path)
    thread = CheckoutWatchdog(
        root=root, baseline=SHA_A, interval_s=0.02, drain_timeout_s=1.0
    )
    codes, fired = _run_until_exit(thread, monkeypatch)

    for i in range(20):
        (root / "src" / f"save{i}.py").write_text(f"# {i}\n", encoding="utf-8")
        time.sleep(0.01)
    assert not fired.is_set(), "an editor save bounced the shared server"

    (root / ".git" / "refs" / "heads" / "main").write_text(
        SHA_B + "\n", encoding="utf-8"
    )
    assert fired.wait(10.0), "a checkout did not bounce the server"
    assert codes == [0]  # clean exit — a supervisor must restart, not back off


def test_unreadable_head_is_a_race_not_a_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-checkout the ref can be briefly absent. Bouncing on that would
    fire on transient states the next poll resolves."""
    root = _checkout(tmp_path)
    thread = CheckoutWatchdog(
        root=root, baseline=SHA_A, interval_s=0.02, drain_timeout_s=1.0
    )
    _codes, fired = _run_until_exit(thread, monkeypatch)
    (root / ".git" / "refs" / "heads" / "main").unlink()
    time.sleep(0.3)
    assert not fired.is_set()


def test_bounce_waits_for_in_flight_calls_to_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7. A bounce while a slow call runs must let that call finish.

    Discriminating against the mechanism that *looks* right: draining
    ``server._get_tool_semaphore()`` from this daemon thread is an
    unawaited coroutine — a silent no-op — so a semaphore-based
    implementation would sail past both waits and set ``fired`` at once.
    The assertion that it is still unset while a call is registered is
    what fails against that version.
    """
    root = _checkout(tmp_path)
    thread = CheckoutWatchdog(
        root=root, baseline=SHA_A, interval_s=0.02, drain_timeout_s=30.0
    )
    codes, fired = _run_until_exit(thread, monkeypatch)

    inflight.enter()  # a slow search, mid-dispatch
    (root / ".git" / "refs" / "heads" / "main").write_text(
        SHA_B + "\n", encoding="utf-8"
    )
    assert not fired.wait(0.6), "exited while a tool call was still in flight"

    inflight.leave()  # the search returns its result
    assert fired.wait(10.0), "never exited after dispatch drained"
    assert codes == [0]


def test_bounce_gives_up_on_a_wedged_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain is bounded on purpose: one wedged call must not hold the
    bounce open forever, which would leave every session on stale code."""
    root = _checkout(tmp_path)
    thread = CheckoutWatchdog(
        root=root, baseline=SHA_A, interval_s=0.02, drain_timeout_s=0.2
    )
    codes, fired = _run_until_exit(thread, monkeypatch)

    inflight.enter()  # never left — a wedged call
    (root / ".git" / "refs" / "heads" / "main").write_text(
        SHA_B + "\n", encoding="utf-8"
    )
    assert fired.wait(10.0), "a wedged call blocked the bounce indefinitely"
    assert codes == [0]


def test_watchdog_never_reaches_for_the_tool_semaphore() -> None:
    """Guard on the trap itself. ``anyio.Semaphore.acquire`` is ``async
    def`` and only ever awaited on the FastMCP event-loop thread; calling
    it from this daemon thread returns an unawaited coroutine and the exit
    proceeds regardless. Keep the quiesce on threading primitives."""
    source = Path(install_watchdog.__file__).read_text(encoding="utf-8")
    assert "_get_tool_semaphore" not in source
    assert "anyio" not in source


def test_bounce_breadcrumb_names_both_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written before the exit, on the watchdog's own thread — once the
    process is gone there is no later chance to explain why."""
    root = _checkout(tmp_path)
    thread = CheckoutWatchdog(
        root=root, baseline=SHA_A, interval_s=0.02, drain_timeout_s=1.0
    )
    _codes, fired = _run_until_exit(thread, monkeypatch)
    (root / ".git" / "refs" / "heads" / "main").write_text(
        SHA_B + "\n", encoding="utf-8"
    )
    assert fired.wait(10.0)

    crumb = json.loads(install_watchdog._breadcrumb_path().read_text(encoding="utf-8"))
    assert crumb["reason"] == "checkout-changed"
    assert SHA_A[:12] in str(crumb["detail"])
    assert SHA_B[:12] in str(crumb["detail"])


def test_precis_status_renders_the_checkout_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reason precis-status can't name renders as a bare repr — the
    operator-facing half of the bounce has to know this one."""
    from precis.handlers.skill import _render_last_exit_note

    install_watchdog._breadcrumb_path().parent.mkdir(parents=True, exist_ok=True)
    install_watchdog._breadcrumb_path().write_text(
        json.dumps(
            {
                "reason": "checkout-changed",
                "written_at": "2026-09-29T20:00:00Z",
                "detail": "/src HEAD aaaaaaaaaaaa→bbbbbbbbbbbb",
            }
        ),
        encoding="utf-8",
    )
    note = _render_last_exit_note()
    assert note is not None
    assert "source checkout moved" in note
    assert "aaaaaaaaaaaa" in note


# --- breadcrumb is per-session, not per-process ----------------------------


class _Session:
    """Stand-in for ``mcp.server.session.ServerSession`` — the real key is
    the session object itself, and a bare ``object()`` isn't weakref-able."""


def _write_crumb() -> None:
    path = install_watchdog._breadcrumb_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"reason": "checkout-changed", "written_at": "2026-09-29T20:00:00Z"}
        ),
        encoding="utf-8",
    )


def test_every_session_sees_the_breadcrumb_once(tmp_path: Path) -> None:
    """AC8. Delete-on-read was per *process*, which meant per client only
    while a process served one. Shared, it would tell the first of twelve
    sessions why the server bounced and leave eleven with the silent
    restart gr341515 exists to prevent."""
    _write_crumb()
    sessions = [_Session() for _ in range(12)]

    for session in sessions:
        with serve_ledger.session_scope(session):
            crumb = consume_last_exit_breadcrumb()
        assert crumb is not None, "a session got no explanation for the bounce"
        assert crumb["reason"] == "checkout-changed"

    # …and exactly once each: a second precis-status in the same session
    # must not repeat it.
    for session in sessions:
        with serve_ledger.session_scope(session):
            assert consume_last_exit_breadcrumb() is None


def test_breadcrumb_file_is_taken_off_disk_on_first_read(tmp_path: Path) -> None:
    """Per-session must not become per-boot-forever: the file still has to
    disappear, or the *next* server would replay a stale explanation."""
    _write_crumb()
    path = install_watchdog._breadcrumb_path()
    with serve_ledger.session_scope(_Session()):
        assert consume_last_exit_breadcrumb() is not None
    assert not path.exists()


def test_sessionless_callers_keep_once_per_process(tmp_path: Path) -> None:
    """The CLI and direct handler calls bind no session; for them "once per
    session" and "once per process" are the same thing, and the old
    behaviour is what they should keep."""
    _write_crumb()
    assert consume_last_exit_breadcrumb() is not None
    assert consume_last_exit_breadcrumb() is None


def test_a_breadcrumb_written_after_an_empty_read_is_still_found() -> None:
    """Regression: the reader must not latch "there was none".

    Memoising the absent case made this order-dependent — whichever test
    in an xdist worker looked first blinded every later one, which is how
    ``test_skill.py::test_status_breadcrumb_is_consumed_not_repeated``
    went red on CI while passing locally. Production only boots once so
    it never saw this, but an order-dependent reader is a latent bug
    either way.
    """
    assert consume_last_exit_breadcrumb() is None  # nothing on disk yet

    _write_crumb()
    with serve_ledger.session_scope(_Session()):
        crumb = consume_last_exit_breadcrumb()
    assert crumb is not None, "a breadcrumb written after an empty read was missed"


def test_a_newer_breadcrumb_reopens_it_to_sessions_already_served() -> None:
    """A fresh file supersedes the held one — a session that already saw
    the old explanation must get the new one, not be told nothing
    happened."""
    _write_crumb()
    session = _Session()
    with serve_ledger.session_scope(session):
        assert consume_last_exit_breadcrumb() is not None
        assert consume_last_exit_breadcrumb() is None  # same crumb, once

    _write_crumb()
    with serve_ledger.session_scope(session):
        assert consume_last_exit_breadcrumb() is not None


def test_no_breadcrumb_stays_none_for_every_session(tmp_path: Path) -> None:
    with serve_ledger.session_scope(_Session()):
        assert consume_last_exit_breadcrumb() is None
    with serve_ledger.session_scope(_Session()):
        assert consume_last_exit_breadcrumb() is None
