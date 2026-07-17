"""Totality + behaviour-preservation tests for the factory registry.

The registry (``precis.workers.registry``) is the single declarative
table that ``cli/worker.py`` derives its pass gating from and the
``/env`` inspector derives its agent list from
(docs/design/factory-console-and-scheduling.md, slice 1). These tests
are the CI guard that the table can no longer drift from the code:

* every pass *wired* in ``cli/worker.py`` (a ``_pass_enabled("X")``
  literal or an appended ``_X_pass`` closure) must have a spec, and
* every spec that claims to be a live ref-pass must be wired, and
* the derived ``system`` / ``agent`` profile sets must equal the exact
  literals they replaced (so slice 1 is provably a no-op refactor).

Parsing the module AST (mirroring
``test_ref_pass_priority_keys_match_registered_passes``) keeps this a
pure static check with no worker wiring or DB.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from precis.cli import worker as worker_mod
from precis.workers.registry import (
    SERVICES,
    SERVICES_BY_NAME,
    ServiceKind,
    agent_specs,
    service_names_for_profile,
)

# The frozen behaviour snapshot: exactly the pass names the old
# hand-written ``frozenset`` literals gated into each profile. If a
# future edit changes profile membership it must update this on purpose.
_EXPECTED_SYSTEM = frozenset(
    {
        "embed",
        "summarize",
        "chunk_keywords",
        "chase",
        "fetch",
        "gp_fetch",
        "tag_embeddings",
        "auto_check",
        "schedule",
        "nursery",
        "dispatch",
        "sweeper",
        "job_coordinator",
        "wake_runner",
        "job_ssh_node",
        "clusterize",
        "corpus_reconcile",
        "paper_reconcile",
    }
)
_EXPECTED_AGENT = frozenset(
    {"structural", "deep_review", "job_claude_inproc", "quota_check"}
)


def _worker_ast() -> ast.Module:
    source = Path(worker_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _passes_referenced_in_worker() -> set[str]:
    """Pass names wired in ``cli/worker.py``.

    Two wiring signals: a ``_pass_enabled("X")`` call (string literal),
    and a ``ref_passes.append(_X_pass)`` (closure named ``_X_pass``).
    The closure name maps to the pass name by stripping the ``_``/``_pass``
    fixture, matching the ``__name__``-keyed ``_REF_PASS_PRIORITY`` table.
    """
    tree = _worker_ast()
    names: set[str] = set()
    closure_re = re.compile(r"^_(?P<name>.+)_pass$")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # _pass_enabled("X")
        if (
            isinstance(func, ast.Name)
            and func.id == "_pass_enabled"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
        # ref_passes.append(_X_pass)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "append"
            and isinstance(func.value, ast.Name)
            and func.value.id == "ref_passes"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            m = closure_re.match(node.args[0].id)
            if m:
                names.add(m.group("name"))
    return names


def test_every_wired_pass_has_a_spec() -> None:
    """A pass referenced in cli/worker.py with no registry row fails CI."""
    referenced = _passes_referenced_in_worker()
    missing = sorted(referenced - set(SERVICES_BY_NAME))
    assert not missing, (
        "passes wired in cli/worker.py with no ServiceSpec "
        f"(add a row to workers/registry.py): {missing}"
    )


def test_every_ref_pass_spec_is_wired() -> None:
    """A spec marked ``ref_pass=True`` with no wiring site fails CI."""
    referenced = _passes_referenced_in_worker()
    ref_pass_specs = {s.name for s in SERVICES if s.ref_pass}
    dangling = sorted(ref_pass_specs - referenced)
    assert not dangling, (
        "ServiceSpec rows marked ref_pass=True but never wired in "
        f"cli/worker.py (stale spec, or unset ref_pass?): {dangling}"
    )


def test_derived_profiles_match_the_frozen_snapshot() -> None:
    """Slice 1 is a no-op refactor: derived sets == the old literals."""
    assert service_names_for_profile("system") == _EXPECTED_SYSTEM
    assert service_names_for_profile("agent") == _EXPECTED_AGENT


def test_enable_env_gates_cover_the_default_off_passes() -> None:
    """The passes that used an inline ``or env_flag(...)`` carry the flag."""
    expected = {
        "job_claude_docker": "PRECIS_SANDBOX_ENABLED",
        "classify": "PRECIS_CLASSIFY_ENABLED",
        "llm_reconcile": "PRECIS_LLM_RECONCILE_ENABLED",
        "paper_glossary": "PRECIS_PAPER_GLOSSARY_ENABLED",
        "briefing_audio": "PRECIS_BRIEFING_AUDIO_ENABLED",
        "cast_audio": "PRECIS_CAST_AUDIO_ENABLED",
        "backlog_groom": "PRECIS_BACKLOG_GROOM_ENABLED",
        "llm_summarize": "PRECIS_SUMMARIZE_LLM",
    }
    for name, env in expected.items():
        assert SERVICES_BY_NAME[name].enable_env == env, name


def test_agent_specs_are_the_introspect_bearing_rows() -> None:
    """The /env inspector list == the four claude -p agent passes."""
    names = {s.name for s in agent_specs()}
    assert names == {
        "dream_agent",
        "structural",
        "deep_review",
        "job_claude_inproc",
    }
    # every introspect-bearing spec is a PASS (the inspector reads plists)
    for s in agent_specs():
        assert s.kind is ServiceKind.PASS
        assert s.introspect is not None


def test_service_names_are_unique() -> None:
    names = [s.name for s in SERVICES]
    assert len(names) == len(set(names)), "duplicate ServiceSpec.name"
