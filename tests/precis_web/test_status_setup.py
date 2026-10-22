"""``/status?tab=setup`` — the doctor check table for admins.

Renders the same ``precis.doctor.run_checks`` sweep the CLI prints.
Against the route-suite FakeStore most DB checks degrade to FAIL rows
(the fake doesn't answer real SQL) — the page must still render the
table, which is the contract this pins.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from precis.doctor import FAIL, run_checks
from precis_web.routes.status import _setup_ctx


def test_setup_tab_renders_check_table(client: Any) -> None:
    resp = client.get("/status?tab=setup")
    assert resp.status_code == 200
    body = resp.text
    assert "Install checks" in body
    # One row per check — the fake store fails most of them, but every
    # name still lands in the table.
    for name in ("python", "database", "extensions", "roster", "kinds", "web"):
        assert f">{name}<" in body, name


def test_setup_ctx_returns_checks_not_exception(runtime: Any) -> None:
    ctx = _setup_ctx(runtime.store, None)
    checks = ctx["doctor_checks"]
    assert checks, "run_checks must always yield the table"
    assert any(c.level == FAIL for c in checks)


def test_run_checks_accepts_caller_store(store: Any) -> None:
    checks = run_checks(store=store)
    assert next(c for c in checks if c.name == "database").level == "ok"
