"""Log-record secret scrubbing: masking, pre-check superset, recursion, cost."""

from __future__ import annotations

import logging
import time

import pytest

from precis.utils import log_scrub
from precis.utils.log_scrub import SecretScrubFilter
from precis.utils.secret_scan import find_secrets, might_contain_secret
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


def test_install_is_idempotent() -> None:
    log_scrub.install_log_scrub()
    f1 = logging.getLogRecordFactory()
    log_scrub.install_log_scrub()
    assert logging.getLogRecordFactory() is f1


def test_overhead_on_ordinary_lines_is_negligible() -> None:
    """100k ordinary records: the filter's added cost per record."""
    flt = SecretScrubFilter()
    lines = [
        (f"claimed job {i} on host node-{i % 7} in 12ms (queue=%s)", ("default",))
        for i in range(100_000)
    ]
    recs = [_record(m, *a) for m, a in lines]
    t0 = time.perf_counter()
    for r in recs:
        flt.filter(r)
    filt = time.perf_counter() - t0
    t0 = time.perf_counter()
    for r in recs:
        r.getMessage()
    fmt = time.perf_counter() - t0
    per_record_us = filt / len(recs) * 1e6
    print(
        f"log_scrub: 100k ordinary lines filter={filt:.3f}s "
        f"({per_record_us:.2f} us/record) vs getMessage={fmt:.3f}s"
    )
    # Generous CI bound: well under 20 us/record (a handler emit costs 10-50 us).
    assert per_record_us < 20
