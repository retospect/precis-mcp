"""Config env-var binding regression tests.

These tests pin the mapping between documented ``PRECIS_*`` env
vars and :class:`precis.config.PrecisConfig` fields.

**Why this file exists**: pydantic-settings derives env var names
as ``env_prefix + FIELD_NAME_UPPER``. If a field is accidentally
named with the prefix baked in (``precis_root`` under
``env_prefix='PRECIS_'``), it silently looks for ``PRECIS_PRECIS_ROOT``
instead of ``PRECIS_ROOT``. The bug is completely silent — no
warning, no error, just a ``None`` field that disables whatever
feature the env var gates.

The May 2026 ``PRECIS_ROOT`` bug (``precis_root`` field →
``PRECIS_PRECIS_ROOT`` env var) effectively disabled the prose-file
kinds for every deployment because no operator set the
double-prefixed form. Regression: iterate every documented env
var and assert the field it populates.
"""

from __future__ import annotations

import pytest

from precis.config import PrecisConfig

# (env_var, field_name, value_to_set) — every row is one documented
# env var and the PrecisConfig field it must populate.
_ENV_VAR_BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("PRECIS_DATABASE_URL", "database_url", "postgresql:///test"),
    ("PRECIS_ROOT", "root", "/tmp/precis-root-under-test"),
    ("PRECIS_PYTHON_ROOTS", "python_roots", "alias:/tmp/repo"),
    ("PRECIS_EMBEDDER", "embedder", "mock"),
    ("PRECIS_LOG_LEVEL", "log_level", "DEBUG"),
    ("PRECIS_DEFAULT_CORPUS", "default_corpus", "my-corpus"),
)


@pytest.mark.parametrize(
    ("env_var", "field", "value"),
    _ENV_VAR_BINDINGS,
    ids=[row[0] for row in _ENV_VAR_BINDINGS],
)
def test_env_var_populates_field(
    env_var: str,
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every documented ``PRECIS_*`` env var must populate its
    PrecisConfig field directly. If this test fails, the field is
    probably named with the ``PRECIS_`` prefix baked in — rename
    it so pydantic-settings derives the right env var."""
    # Scrub any pre-existing env so pydantic doesn't pick up real values.
    for var, _, _ in _ENV_VAR_BINDINGS:
        monkeypatch.delenv(var, raising=False)
    # Also scrub the double-prefixed form to confirm nobody's relying
    # on it as a side channel.
    monkeypatch.delenv(f"PRECIS_{env_var}", raising=False)

    monkeypatch.setenv(env_var, value)
    cfg = PrecisConfig()
    assert getattr(cfg, field) == value, (
        f"PRECIS env var {env_var!r} did not populate field "
        f"{field!r}. Did you accidentally name the field with the "
        f"'PRECIS_' prefix baked in? pydantic-settings would then "
        f"look for 'PRECIS_{env_var}' instead."
    )


def test_unset_env_var_leaves_field_at_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent env var must leave each optional field at its
    default (``None`` for the three gate-fields that disable
    features when unset)."""
    for var, _, _ in _ENV_VAR_BINDINGS:
        monkeypatch.delenv(var, raising=False)
    cfg = PrecisConfig()
    assert cfg.database_url is None
    assert cfg.root is None
    assert cfg.python_roots is None


def test_double_prefixed_form_is_not_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PRECIS_PRECIS_ROOT`` was the *accidental* env var name the
    old ``precis_root`` field bound to. After the rename, nothing
    should pick it up — confirming the fix didn't merely add a
    second binding alongside the broken one."""
    for var in ("PRECIS_ROOT", "PRECIS_PRECIS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PRECIS_PRECIS_ROOT", "/should/be/ignored")
    cfg = PrecisConfig()
    assert cfg.root is None, (
        "PRECIS_PRECIS_ROOT should not populate the root field — "
        "that was the exact silent-bug mode we renamed to fix."
    )
