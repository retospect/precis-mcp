"""Generic guard: every ``put``/``edit`` handler kwarg must be exposed by
``tools/core.py``'s corresponding verb signature.

``tools/core.py::put`` and ``::edit`` each double as (a) the FastMCP-derived
JSON Schema advertised over MCP and (b) a hand-maintained dispatch payload
dict. A handler kwarg declared on ``FooHandler.put``/``.edit`` but missing
from either side of that verb function is unreachable over MCP with no
error at all — every handler carries a ``**_kw: Any`` catch-all, so the
dropped kwarg just vanishes. That is not hypothetical: it happened at
least three times before this guard existed (``put(kind='job',
parent_id=…)`` — ``test_mcp_put_parent_id.py``; the hypothesis-proposal
kwargs — ``test_mcp_put_hypothesis_kwargs.py``; ``put``'s missing
``wants``/``provenance`` + ``edit``'s missing ``doi``/``arxiv`` — gripe
262482 / gripe 250273, fixed alongside this test).

Those three tests each pin one already-discovered instance. This one is
the class-level guard the diagnoses on gr262482/gr250273 asked for: it
walks every registered handler's ``put``/``edit`` method via
``inspect.signature`` (the real handler registry, not a hardcoded kind
list — a hardcoded list would rot exactly like the hand-maintained schema
did) and fails loudly if any named parameter isn't in the matching
``tools/core.py`` verb's signature, instead of the silent ``**_kw`` drop.

It does not (and cannot, generically) verify the payload dict *forwards*
every declared parameter to ``_dispatch`` — that half of the bug needs a
value round-tripped through a live call. ``test_mcp_put_edit_kwarg_doors.py``
does that for the two doors this fix opens; the class guard here only
covers the schema half, which is where every recorded instance actually
broke.

**The ratchet.** Turning this guard on the first time found 71 pre-existing
instances across 25 kinds — real product debt the diagnoses didn't scope
this fix to fix. Rather than block on all 71 (a separate, judgment-heavy
job — see docs/backlog/mcp-verb-kwarg-parity.md for the triage) or silently
allowlist them into meaninglessness, ``_KNOWN_GAPS`` freezes exactly that
set. The test fails if the live gap set drifts either direction: grows (a
NEW gap — someone must fix it or make a deliberate ``_EXEMPT`` call) or
shrinks without the ledger being updated (a gap got fixed but the entry
was left behind, which would otherwise let the *next* regression on that
same kwarg sneak back in unnoticed). Entries leave ``_KNOWN_GAPS`` only by
being wired through (fixed) — never just deleted to silence a failure.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

import pytest

from precis.dispatch import Hub, boot
from precis.store import Store
from precis.tools import core as tools_core

# The two verbs whose tools/core.py function is a hand-maintained
# (signature, dispatch-payload) pair per kind — the recurring bug class.
# get/search/delete/tag/link don't carry this per-kind kwarg block.
_GUARDED_VERBS = ("put", "edit")

#: Deliberate, reviewed exemptions: handler kwargs that are legitimately
#: NOT reachable through the MCP put/edit surface. Keyed by
#: ``(kind, verb, param)``. Every entry here is a decision, not an
#: accident — adding to this set silences the guard, so do it with a
#: one-line reason in the comment, same discipline as an inline `noqa`.
#: Empty: the investigation for gr262482/gr250273 found no kwarg in this
#: class that's actually CLI-only or otherwise deliberately unreachable —
#: even the ``args=`` extras-tunnel candidates (``pcb.put``,
#: ``structure.put``/``.edit``) turned out to be real gaps, not a
#: different-but-legitimate door (see docs/backlog/mcp-verb-kwarg-parity.md
#: §"args= is not actually exempt"). Kept as a real (if empty) set so a
#: future deliberate exemption has a home that isn't _KNOWN_GAPS.
_EXEMPT: frozenset[tuple[str, str, str]] = frozenset()

#: Ratchet ledger: the 71 pre-existing (kind, verb, param) gaps this guard's
#: introduction (gr262482/gr250273) surfaced, beyond the four kwargs that
#: gripe pair asked to be fixed (``finding.put``'s ``wants``/``provenance``,
#: ``paper.edit``'s ``doi``/``arxiv`` — already wired and NOT in this set).
#: Triage (real gap / high-impact / unclear) lives in
#: docs/backlog/mcp-verb-kwarg-parity.md — this set is just the ledger.
#:
#: Entries may only be REMOVED, by wiring the kwarg through tools/core.py's
#: signature + dispatch payload dict (mirroring the surrounding kwarg
#: blocks) and deleting its line here. Never add an entry to silence a
#: NEW failure — that's what _EXEMPT is for, and only after a deliberate
#: look. The test below fails in both directions: a gap not listed here
#: (new regression) OR a listed gap that's no longer reproducible (fixed
#: but the ledger wasn't updated, which would let a *future* regression on
#: that exact kwarg slip back in unnoticed).
_KNOWN_GAPS: frozenset[tuple[str, str, str]] = frozenset(
    {
        # -- put: cache-decay refresh knob, five numeric-ref-ish kinds ----
        ("anki", "put", "auto_refresh_days"),
        ("concept", "put", "auto_refresh_days"),
        ("folder", "put", "auto_refresh_days"),
        ("memory", "put", "auto_refresh_days"),
        ("todo", "put", "auto_refresh_days"),
        # -- put: draft chunk creation (figure/table/narration fields) ---
        ("draft", "put", "copy_of"),
        ("draft", "put", "image"),
        ("draft", "put", "lang"),
        ("draft", "put", "mime"),
        ("draft", "put", "origin"),
        ("draft", "put", "permission"),
        ("draft", "put", "voice"),
        # -- put: diagram kinds ------------------------------------------
        ("figure", "put", "viewbox"),
        ("figure", "put", "vocab"),
        ("mermaid", "put", "viewbox"),
        ("mermaid", "put", "vocab"),
        # -- put: job submission gating -----------------------------------
        ("job", "put", "requires"),
        ("job", "put", "select"),
        # -- put: llm catalog mint (the whole variant-precise surface) ---
        ("llm", "put", "capability"),
        ("llm", "put", "model_id"),
        ("llm", "put", "offerings"),
        ("llm", "put", "served_by"),
        ("llm", "put", "tier_floor"),
        # -- put: memory D3-shortcut argument-graph kwargs ----------------
        ("memory", "put", "rule"),
        ("memory", "put", "warrant"),
        # -- put: message thread attachments -------------------------------
        ("message", "put", "attachments"),
        # -- put: paper acquire() conveniences -----------------------------
        ("paper", "put", "context_ref_id"),
        ("paper", "put", "verify"),
        # -- edit: the args= extras tunnel, still unwired on edit ---------
        # `put(args=)` was closed 2026-08-28 (gr267461): every pcb write
        # op — place, route, plane_net, pin_side — travels through `args`,
        # so the whole pcb write surface was unreachable from the MCP
        # tool. edit's args/ops tunnel was wired through 2026-08-31 when
        # the nm kind's edit surface would otherwise have grown two NEW
        # copies of the same gap — ("structure","edit","args") and
        # ("structure","edit","ops") left this ledger by being fixed
        # (tools/core.py::edit now declares + forwards both).
        # -- put: in-process planner state ---------------------------------
        ("plan", "put", "belief"),
        ("plan", "put", "status"),
        # -- put: protein (AlphaFold) mint ----------------------------------
        ("protein", "put", "engine"),
        ("protein", "put", "requested_by"),
        ("protein", "put", "seeds"),
        ("protein", "put", "sequence"),
        # -- put: reaction-route mint ---------------------------------------
        ("route", "put", "engine"),
        ("route", "put", "max_steps"),
        ("route", "put", "requested_by"),
        # -- put: structure (crystal/molecule) edit ops ---------------------
        ("structure", "put", "normalize"),
        # -- put: todo prio shortcut (the operational workaround is raw SQL)
        ("todo", "put", "prio"),
        # -- edit: bibliographic-metadata repair, paper-like kinds --------
        ("cfp", "edit", "abstract"),
        ("cfp", "edit", "entry_type"),
        ("cfp", "edit", "journal"),
        ("cfp", "edit", "year"),
        ("datasheet", "edit", "part_lcsc"),
        ("datasheet", "edit", "subtype"),
        ("datasheet", "edit", "vendor"),
        ("paper", "edit", "abstract"),
        ("paper", "edit", "entry_type"),
        ("paper", "edit", "journal"),
        ("paper", "edit", "year"),
        ("pres", "edit", "bibtex_type"),
        ("pres", "edit", "date"),
        ("pres", "edit", "note"),
        ("pres", "edit", "url"),
        ("pres", "edit", "venue"),
        # -- edit: draft metadata repair -----------------------------------
        # ("draft","edit","meta") / ("pres","edit","meta") fixed gr301897:
        # tools/core.py::edit now declares meta= and routes it through the
        # __extras__ accepted-kwargs gate.
        ("draft", "edit", "list_kind"),
        ("draft", "edit", "source"),
        ("draft", "edit", "style"),
        # -- edit: finding acquisition-mode's dead-end flip side ----------
        ("finding", "edit", "unacquirable_mode"),
        ("finding", "edit", "unacquirable_note"),
        # -- edit: memory D3-shortcut argument-graph kwargs ---------------
        ("memory", "edit", "rule"),
        ("memory", "edit", "warrant"),
        # -- edit: in-process planner state --------------------------------
        ("plan", "edit", "belief"),
        ("plan", "edit", "cursor"),
        ("plan", "edit", "status"),
    }
)


def _accepted_handler_kwargs(method: Any) -> set[str]:
    """Named kwargs ``method`` accepts, minus ``self`` and any catch-all.

    Mirrors ``DispatchMixin._accepted_kwargs`` (the same rule the live
    dispatcher uses to validate ``args=`` extras) — ``**_kw`` doesn't
    count, only real positional-or-keyword / keyword-only parameters.
    """
    sig = inspect.signature(method)
    return {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and name != "self"
    }


@pytest.fixture
def full_hub(store: Store) -> Iterator[Hub]:
    """Every store-backed handler registered — the real dispatch registry
    (:func:`precis.dispatch.boot`), not a hand-picked subset. Kinds gated
    on an optional dependency (sympy, habanero, ...) that isn't installed
    simply don't register — same as production; the guard only ever walks
    what's actually live."""
    yield boot(store=store)


def test_put_and_edit_handler_kwarg_gap_set_matches_the_ratchet(
    full_hub: Hub,
) -> None:
    """The live (kind, verb, param) gap set must equal ``_KNOWN_GAPS``
    exactly — no new silent-drop instance, and no stale ledger entry for a
    gap that's already been fixed."""
    core_params = {
        "put": set(inspect.signature(tools_core.put).parameters),
        "edit": set(inspect.signature(tools_core.edit).parameters),
    }
    live_gaps: set[tuple[str, str, str]] = set()
    for verb in _GUARDED_VERBS:
        for kind in sorted(full_hub.handlers):
            handler = full_hub.handlers[kind]
            if not handler.spec.supports(verb):
                continue
            method = getattr(handler, verb, None)
            if method is None:
                continue
            for name in _accepted_handler_kwargs(method):
                key = (kind, verb, name)
                if key in _EXEMPT:
                    continue
                if name not in core_params[verb]:
                    live_gaps.add(key)

    new_gaps = sorted(live_gaps - _KNOWN_GAPS)
    stale_ledger = sorted(_KNOWN_GAPS - live_gaps)

    problems: list[str] = []
    if new_gaps:
        fmt = ", ".join(f"{k}.{v}({p}=)" for k, v, p in new_gaps)
        problems.append(
            "NEW handler kwargs silently dropped by tools/core.py's verb "
            "signature (add to the signature + dispatch payload dict, or "
            "to _EXEMPT with a reason if deliberate): " + fmt
        )
    if stale_ledger:
        fmt = ", ".join(f"{k}.{v}({p}=)" for k, v, p in stale_ledger)
        problems.append(
            "STALE _KNOWN_GAPS entries — already fixed but not removed "
            "from the ledger (remove the line, and update "
            "docs/backlog/mcp-verb-kwarg-parity.md): " + fmt
        )
    assert not problems, "\n".join(problems)
