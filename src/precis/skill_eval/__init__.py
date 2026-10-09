"""Per-skill eval harness — does an agent *given* a skill complete the task
the skill teaches? (``docs/backlog/skill-eval-harness.md``, Reto 2026-09-09.)

Skills are the product's runtime docs and ship on prose review alone; every
other feedback loop is post-hoc. This harness closes the loop pre-ship: an
eval case per skill = a task prompt a prod agent would plausibly face + a
checkable success criterion over the verb calls the agent makes, the
record it produces, and the detours it must not take.

Shape, by module:

- :mod:`.cases` — the YAML corpus under ``src/precis/data/skill-evals/
  <skill>.yaml`` (:func:`load_cases`), the :class:`EvalCase` /
  :class:`Criterion` model and the :class:`Transcript` a runner returns.
- :mod:`.criteria` — :func:`check`: deterministic verdict of a transcript
  against a criterion (expected calls as subset/regex patterns, forbidden
  calls and answer text, a record predicate).
- :mod:`.runners` — the :class:`Runner` seam and its two implementations.
  :class:`FakeRunner` replays the case's scripted ``fake:`` transcript and
  records every prompt it was handed (tests, CI, the corpus's own
  self-check). :class:`LiveClaudeRunner` is a **planned-call dry run** on
  the host: tool-free ``claude -p`` reads the injected skill(s) and the
  task and declares the verb calls it would make as JSON — it never
  touches a database, so write-path criteria are checked on the declared
  record, not on prod. Budgeted per case (``--max-budget-usd``) and per
  run; a missing or unauthenticated ``claude`` skips the whole run instead
  of faking a pass (the container ``claude`` exits 0 with a logged-out
  banner — memory ``live-model-tests-need-host-claude``).
- :mod:`.report` — :func:`run_cases` drives cases through a runner and
  builds the :class:`Report` (JSON file + short table).

Why a dry run and not an executed one: executing against the dev DB needs
``precis serve`` plus MCP wiring per run, which is the corpus-growth
remainder in the backlog file; the dry run already catches the failure the
spec names (a skill misleading on one of its ``answers:`` questions sends
the planned calls to the wrong verb/kind). Advisory like
``scripts/mutate-diff``: ``scripts/skill-eval`` is never a ``scripts/ship``
gate stage (``tests/test_skill_eval.py`` pins that).
"""

from __future__ import annotations

from precis.skill_eval.cases import (
    CallPattern,
    Criterion,
    EvalCase,
    Transcript,
    VerbCall,
    default_cases_dir,
    load_cases,
)
from precis.skill_eval.criteria import Verdict, check
from precis.skill_eval.report import CaseResult, Report, run_cases
from precis.skill_eval.runners import FakeRunner, LiveClaudeRunner, Runner

__all__ = [
    "CallPattern",
    "CaseResult",
    "Criterion",
    "EvalCase",
    "FakeRunner",
    "LiveClaudeRunner",
    "Report",
    "Runner",
    "Transcript",
    "VerbCall",
    "Verdict",
    "check",
    "default_cases_dir",
    "load_cases",
    "run_cases",
]
