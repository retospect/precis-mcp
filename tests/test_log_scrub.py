"""Log-record secret scrubbing: masking, pre-check superset, recursion, cost."""

from __future__ import annotations

import logging
import statistics
import time

import pytest

from precis.utils import log_scrub
from precis.utils.log_scrub import SecretScrubFilter
from precis.utils.secret_scan import find_secrets, might_contain_secret
from tests._timing import budget
from tests.test_secret_scan import NEGATIVES, POSITIVES

# Runtime-assembled so the source holds no literal credential.
SECRET = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")


def _record(msg: object, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args or None, None)


def test_message_with_secret_is_masked() -> None:
    rec = _record("calling with token " + SECRET)
    assert SecretScrubFilter().filter(rec) is True
    assert SECRET not in rec.getMessage()
    assert "<redacted:github token>" in rec.getMessage()


def test_percent_args_are_formatted_then_masked() -> None:
    rec = _record("auth %s for %d hosts", SECRET, 3)
    SecretScrubFilter().filter(rec)
    out = rec.getMessage()
    assert SECRET not in out
    assert out.endswith("for 3 hosts")


def test_secret_in_non_string_arg_is_masked() -> None:
    rec = _record("failed: %s", RuntimeError("boom " + SECRET))
    SecretScrubFilter().filter(rec)
    assert SECRET not in rec.getMessage()


@pytest.mark.parametrize(
    "line",
    [
        "worker started in 12ms",
        "claimed job 123 host=%s",
        "commit 50d2afe3d1f5b3a6c7e8d9f0a1b2c3d4e5f60718 deployed",
        "GET https://example.org/a/b?x=1 200",
    ],
)
def test_ordinary_lines_pass_unchanged(line: str) -> None:
    rec = _record(line, "h1") if "%s" in line else _record(line)
    before = rec.getMessage()
    SecretScrubFilter().filter(rec)
    assert rec.getMessage() == before


def test_trigger_covers_every_pattern() -> None:
    for kind, text in POSITIVES.items():
        assert find_secrets(text), kind
        assert might_contain_secret(text), f"pre-check misses {kind}"
    # negatives may or may not trigger, but must never be altered by the filter
    for name, text in NEGATIVES.items():
        rec = _record(text.replace("%", "%%"))
        SecretScrubFilter().filter(rec)
        assert rec.getMessage() == text, name


def test_own_warning_does_not_recurse(caplog: pytest.LogCaptureFixture) -> None:
    from precis.utils.secret_scan import mask_secrets

    log_scrub.install_log_scrub()
    with caplog.at_level(logging.WARNING):
        assert SECRET not in mask_secrets("x " + SECRET)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("masked a github token" in m for m in msgs)
    assert SECRET not in caplog.text
    assert len(msgs) == 1  # one warning, no cascade


def test_installed_factory_masks_end_to_end(caplog: pytest.LogCaptureFixture) -> None:
    log_scrub.install_log_scrub()
    lg = logging.getLogger("precis.test_log_scrub")
    with caplog.at_level(logging.INFO, logger=lg.name):
        lg.info("leaked %s", SECRET)
        lg.info("ordinary %s", "line")
    assert SECRET not in caplog.text
    assert "ordinary line" in caplog.text


def test_exc_info_traceback_is_masked_via_exc_text() -> None:
    """gr502250 follow-on: an httpx-style exception folds the full request
    URL — ``api_key=<key>`` — into the traceback's last line. The filter
    pre-renders a masked ``exc_text`` so no handler (file, journald, DB)
    can emit the key even though ``exc_info`` itself is untouched."""
    key = "FAKE" + "KEY123"  # via a var so the traceback's source line is clean
    try:
        raise ValueError(f"fetch https://h.example/x?api_key={key}")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "t",
            logging.ERROR,
            __file__,
            1,
            "download failed",
            None,
            sys.exc_info(),
        )
    SecretScrubFilter().filter(rec)
    assert rec.exc_text is not None
    assert key not in rec.exc_text
    assert "download failed" in rec.getMessage()


def test_installed_factory_masks_traceback(caplog: pytest.LogCaptureFixture) -> None:
    log_scrub.install_log_scrub()
    lg = logging.getLogger("precis.test_log_scrub_exc")
    key = "FAKE" + "KEY456"
    try:
        raise ValueError(f"GET https://h.example/x?api_key={key}")
    except ValueError:
        with caplog.at_level(logging.ERROR, logger=lg.name):
            lg.exception("fetch failed")
    assert key not in caplog.text
    assert "fetch failed" in caplog.text


def test_clean_exc_info_leaves_exc_text_unset() -> None:
    try:
        raise ValueError("plain boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "x", None, sys.exc_info()
        )
    SecretScrubFilter().filter(rec)
    assert rec.exc_text is None


def test_install_is_idempotent() -> None:
    log_scrub.install_log_scrub()
    f1 = logging.getLogRecordFactory()
    log_scrub.install_log_scrub()
    assert logging.getLogRecordFactory() is f1


def test_overhead_on_ordinary_lines_is_negligible() -> None:
    """100k ordinary records: the filter's added cost per record.

    Timed as the MEDIAN of 5 rounds: a single measurement on a saturated
    shared runner spikes without meaning the filter got slower — the
    2026-10-30 coverage-db nightly read 32 us/record once and redded the
    job. (A getMessage-relative bound can't work: the honest ratio is
    already ~30x — the trigger pre-check is real work vs a tuple format.)"""
    flt = SecretScrubFilter()
    lines = [
        (f"claimed job {i} on host node-{i % 7} in 12ms (queue=%s)", ("default",))
        for i in range(100_000)
    ]
    recs = [_record(m, *a) for m, a in lines]
    filt_runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        for r in recs:
            flt.filter(r)
        filt_runs.append(time.perf_counter() - t0)
    t0 = time.perf_counter()
    for r in recs:
        r.getMessage()
    fmt = time.perf_counter() - t0
    filt = statistics.median(filt_runs)
    per_record_us = filt / len(recs) * 1e6
    print(
        f"log_scrub: 100k ordinary lines median filter={filt:.3f}s "
        f"({per_record_us:.2f} us/record) vs getMessage={fmt:.3f}s "
        f"(rounds: {', '.join(f'{t:.2f}' for t in filt_runs)})"
    )
    # Negligible means well under a handler emit (10-50 us). The median
    # bound is 40 us — 5x nominal — so a real regression (masking every
    # line, a lost trigger pre-check) still fails while scheduling jitter
    # on a loaded -n6 runner does not.
    assert per_record_us < budget(40)


def test_httpx_url_arg_params_masked_with_shared_token() -> None:
    """The record-creation scrub runs before logger filters: an httpx.URL
    arg's credential params are rewritten element-wise to ``REDACTED`` — the
    same token the supply adapters' httpx filter writes — so ``record.args``
    stays a tuple and the emitted line reads ``?apiKey=REDACTED``."""
    import httpx

    url = httpx.URL(
        "https://supplier.example/x?" + "api" + "Key=" + "sek" + "ret123&page=2"
    )
    rec = _record('HTTP Request: %s %s "%s"', "POST", url, "HTTP/1.1 200 OK")
    SecretScrubFilter().filter(rec)
    out = rec.getMessage()
    assert "sekret123" not in out
    assert "apiKey=REDACTED" in out and "page=2" in out
    assert isinstance(rec.args, tuple)


def test_masked_record_keeps_tuple_args_for_uvicorn_access_formatter() -> None:
    """gr479780: args=None crashed uvicorn's AccessFormatter on every URL
    carrying a token ("--- Logging error ---", line lost)."""
    from uvicorn.logging import AccessFormatter

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", "GET", "/podcast/feed.xml?t=" + SECRET, "1.1", 200),
        None,
    )
    SecretScrubFilter().filter(rec)
    assert isinstance(rec.args, tuple) and len(rec.args) == 5
    out = AccessFormatter("%(asctime)s %(levelprefix)s %(message)s").format(rec)
    assert SECRET not in out
    assert "<redacted:" in out
    assert out[:4].isdigit()  # asctime present
