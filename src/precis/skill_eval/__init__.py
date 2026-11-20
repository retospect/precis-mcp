"""Per-skill eval harness — does an agent *given* a skill complete the task
the skill teaches? (convention: ``docs/conventions/skill-evals.md``.)

Skills are the product's runtime docs and ship on prose review alone; every
other feedback loop is post-hoc. This harness closes the loop pre-ship: an
eval case per skill = a task prompt a prod agent would plausibly face + a
checkable success criterion over the verb calls the agent makes, the
record it produces, and the detours it must not take.

Shape, by module:

- :mod:`.cases` — the YAML corpus under ``src/precis/data/skill-evals/
  <skill>.yaml`` (:func:`load_cases`) plus the discovery corpus under
  ``skill-evals/discovery/<family>.yaml`` (:func:`load_discovery_cases`),
  the :class:`EvalCase` / :class:`Criterion` model and the
  :class:`Transcript` a runner returns. A case may declare ``subtasks:``
  (MDCrow-style graded depth); the executed runner's report buckets
  accuracy by it.
- :mod:`.criteria` — :func:`check`: deterministic verdict of a transcript
  against a criterion (expected calls as subset/regex patterns, forbidden
  calls and answer text, a record predicate).
- :mod:`.runners` — the :class:`Runner` seam and its three
  implementations. :class:`FakeRunner` replays the case's scripted
  ``fake:`` transcript and records every prompt it was handed (tests, CI,
  the corpus's own self-check). :class:`LiveClaudeRunner` is a
  **planned-call dry run** on the host: tool-free ``claude -p`` reads the
  injected skill(s) and the task and declares the verb calls it would
  make as JSON — it never touches a database, so write-path criteria are
  checked on the declared record, not on prod.
  :class:`ExecutedClaudeRunner` is the **executed** run: ``claude -p``
  with a precis MCP bound to a scratch DSN (``scripts/dev``'s
  ``precis_dev``; ``--dsn`` for another test stack — never prod), the
  transcript reconstructed from stream-json ``tool_use`` events, and
  ``expect.record`` judged on the row that actually landed. Budgeted per
  case and per run; a missing/unauthenticated ``claude`` or an MCP that
  never connects skips the whole run instead of faking a pass.
- :mod:`.report` — :func:`run_cases` drives cases through a runner and
  builds the :class:`Report` (JSON file + short table + accuracy by
  subtask count per model). Every runner carries a ``condition``
  (``supplied`` default, ``discovery`` = no skill text or slug); a paired
  run stamps each failed pair with a failure class via
  :func:`classify_failure` — ``retrieval`` (passes when supplied, so the
  agent never found the guidance), ``guidance`` (fails even supplied,
  clean calls), ``tool`` (calls errored) or ``unattributed`` (gr634773).
- :mod:`.discovery_review` — the advisory recurring sampler behind
  ``scripts/skill-discovery-review``: bounded case selection (changed
  skills, then previous failures, then under-tested families), a paired
  executed run, retained reports and one repair brief per failure.
  Git lookups have a wall-clock bound so a stalled checkout cannot hang
  the sampler. A failed history lookup stops selection, while unavailable
  revision metadata is recorded as missing.

Advisory like ``scripts/mutate-diff``: ``scripts/skill-eval`` is never a
``scripts/ship`` gate stage (``tests/test_skill_eval.py`` pins that).
"""

from __future__ import annotations

from precis.skill_eval.cases import (
    CallPattern,
    Criterion,
    EvalCase,
    Transcript,
    VerbCall,
    default_cases_dir,
    default_discovery_dir,
    discovery_metrics,
    load_cases,
    load_discovery_cases,
)
from precis.skill_eval.criteria import Verdict, check
from precis.skill_eval.report import (
    CaseResult,
    Report,
    classify_failure,
    classify_pairs,
    run_cases,
)
from precis.skill_eval.runners import (
    ExecutedClaudeRunner,
    FakeRunner,
    LiveClaudeRunner,
    Runner,
)

__all__ = [
    "CallPattern",
    "CaseResult",
    "Criterion",
    "EvalCase",
    "ExecutedClaudeRunner",
    "FakeRunner",
    "LiveClaudeRunner",
    "Report",
    "Runner",
    "Transcript",
    "VerbCall",
    "Verdict",
    "check",
    "classify_failure",
    "classify_pairs",
    "default_cases_dir",
    "default_discovery_dir",
    "discovery_metrics",
    "load_cases",
    "load_discovery_cases",
    "run_cases",
]
