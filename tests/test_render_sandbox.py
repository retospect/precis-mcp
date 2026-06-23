"""Phase-1 render isolation (ADR 0035 §3) — the stripped subprocess that runs
untrusted figure code. These tests pin the *security-critical* behaviour: the
child sees a scrubbed env, a hostile loop is killed on the wall clock, a raising
render fails cleanly, and a render that produces nothing is reported — never a
silent or in-process execution."""

from __future__ import annotations

import pytest

from precis.render.sandbox import render_python

# Minimal PNG signature — the sandbox only checks bytes are produced / bounded,
# not that they decode, so tests may emit just the magic.
_PNG = r"open(out, 'wb').write(b'\x89PNG\r\n\x1a\n' + b'x' * 16)"


def test_writes_output_png() -> None:
    r = render_python(_PNG)
    assert r.ok, r.stderr
    assert r.png is not None and r.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert r.error is None


def test_data_reaches_the_child() -> None:
    code = "assert data['n'] == 3, data\n" + _PNG
    r = render_python(code, inputs={"n": 3})
    assert r.ok, r.stderr


def test_env_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Secrets in the parent must NOT reach the child (no creds, no SSH).
    monkeypatch.setenv("PRECIS_SECRET", "topsecret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    code = (
        "import os\n"
        "leaked = (os.environ.get('PRECIS_SECRET', '')"
        " + os.environ.get('SSH_AUTH_SOCK', '')"
        " + os.environ.get('AWS_SECRET_ACCESS_KEY', ''))\n"
        "assert leaked == '', 'env leaked: ' + leaked\n" + _PNG
    )
    r = render_python(code)
    assert r.ok, r.stderr  # the in-child assert would have failed it


def test_home_is_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    # HOME points at the throwaway workdir, not the real home.
    real_home = "/Users/somebody-real"
    monkeypatch.setenv("HOME", real_home)
    code = (
        "import os\n"
        f"assert os.environ['HOME'] != {real_home!r}, os.environ['HOME']\n"
        "assert 'precis-render-' in os.environ['HOME'], os.environ['HOME']\n" + _PNG
    )
    r = render_python(code)
    assert r.ok, r.stderr


def test_wallclock_timeout_kills_hostile_loop() -> None:
    r = render_python("while True:\n    pass\n", timeout_s=1.5)
    assert not r.ok
    assert r.error == "timeout"
    assert r.png is None


def test_raising_code_fails_cleanly_with_traceback() -> None:
    r = render_python("raise RuntimeError('boom')\n")
    assert not r.ok
    assert r.error is not None and r.error.startswith("exit:")
    assert "boom" in r.stderr


def test_no_output_is_reported() -> None:
    r = render_python("x = 1 + 1\n")  # produces no file, no figure
    assert not r.ok
    assert r.error == "no-output"


def test_matplotlib_autosave(monkeypatch: pytest.MonkeyPatch) -> None:
    # When matplotlib is available, a drawn-but-unsaved figure is saved for the
    # author. Skipped where the render lane's [plot] extra isn't installed.
    import subprocess
    import sys

    have = subprocess.run(
        [sys.executable, "-c", "import matplotlib"], capture_output=True
    )
    if have.returncode != 0:
        pytest.skip("matplotlib not installed (the [plot] extra)")

    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.scatter(data['x'], data['y'])\n"
        "# deliberately do not savefig — the harness should\n"
    )
    r = render_python(code, inputs={"x": [1, 2, 3], "y": [1, 4, 9]})
    assert r.ok, r.stderr
    assert r.png is not None and r.png.startswith(b"\x89PNG\r\n\x1a\n")
