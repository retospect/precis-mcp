"""Registry-vocabulary lint — every coined registry name appears in a skill.

When code coins a name an agent will see — an alert rule id or
fingerprint, a ``job_type``, a ``STATUS:`` value, a watchdog/health check
name, a worker service — at least one skill must name it, so
``search(kind='skill', q='<that name>')`` can find the doc. Names like
``child-failed-parked``, ``lane-skipping`` and ``pass-dead`` lived in code
with no skill section until agents kept re-searching them
(skill-search audit 2026-10; ruling: mechanical enforcement).

**What counts as "appears".** ``handlers.skill._content_tokens`` indexes
each alnum run (``[a-z0-9]+``, ≥3 chars, minus stopwords) *and* each
unsplit hyphenated run (``child-failed-parked``). A name therefore counts
as documented when it appears verbatim, case-insensitive, bounded by
non-alphanumerics, anywhere in a skill's raw text — body, headings,
``keywords:`` or ``answers:`` front matter all live in the same file. The
``(?<![a-z0-9])…(?![a-z0-9])`` boundary mirrors the tokenizer's split on
``-``/``_``: ``pass-dead`` inside ``pass-dead-on-host`` still emits the
``pass``/``dead`` parts plus a verbatim-normalized substring hit, so it
counts; ``dead-gen-claims`` does not match ``dead-generation-claims``
(different names), and ``embed`` does not match ``embedder``.

**Registries swept** (the name an agent sees → where it's coined):

* ``alert_ids.ALERT_RULES`` — every literal segment of each rule's
  ``<source>/<fingerprint>`` pattern (``{var}`` placeholders removed):
  the hyphenated compounds (``child-failed-parked``) and snake_case keys
  (``papers_ingested``) an alert's ``meta.rule_id`` and fingerprint show.
* ``workers.job_types.known_job_types`` built-ins (AST-read from the
  function's literal list, like ``test_alert_ids``) plus the declared
  ``[project.entry-points."precis.job_types"]`` names — the
  ``meta.job_type`` tokens a ``kind='job'`` shows.
* ``store.types`` status vocabularies — ``_CLOSED_VOCAB["STATUS"]`` plus
  the per-kind ``_KIND_STATUS_VALUES`` subsets (job/gripe/todo/quest/alert
  lifecycle values) — and the *other* closed-vocab axes + values in the
  same ``_CLOSED_VOCAB`` table, all agent-writable tag vocabulary.
* ``workers.health_digest`` check names — ``_FRESHNESS_CHECKS`` /
  ``_BACKLOG_CHECKS`` keys plus every literal ``CheckResult(group, name)``
  the module can emit (AST-read); ``workers.conditions.CONDITIONS`` names;
  ``workers.nursery._DETECTORS`` categories (the ``_NO_ALERT`` ones too —
  they still name log lines an agent reads).
* ``workers.registry.SERVICES`` — ``name`` and ``log_handler`` (the
  ``--only`` / ``service_config`` / ``worker_logs.handler`` tokens;
  ``ServiceSpec.doc_skill`` is the designed doc pointer, so these are
  agent-visible by construction).
* ``alerts.STATE_OPEN`` / ``STATE_RESOLVED`` — the ``alert-state:``
  lifecycle tags.

Deliberately skipped (not coined agent-facing names): alert severities
(``info``/``warn``/``critical`` — plain English), ``Profile`` /
``ServiceKind`` / router ``Tier`` enums (operator-facing), 2-char handle
prefixes (``fi``/``pa``/``me`` — documented in
``precis-addressing-help``, below the 3-char token floor anyway).

**Allowlist.** ``_ALLOWLIST`` is for names that are internal and never
shown to agents — each entry carries a one-line reason. It is *not* a
make-the-test-pass valve: a name agents can see gets a doc line (or an
H2) in the skill that owns it instead.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from tests._skill_roots import ROOT, skill_files

_VAR_RE = re.compile(r"\{[^{}]*\}")
_NAME_BOUNDARY = r"[a-z0-9]"

#: Names that are coined in a swept registry but never shown to agents —
#: each with the reason they stay undocumented. Keep this short.
_ALLOWLIST: dict[str, str] = {}


def _skill_texts() -> dict[str, str]:
    """``{slug → raw text}`` for every shipped skill (built-in + plugins)."""
    return {p.stem: p.read_text(encoding="utf-8") for p in skill_files()}


def _documented_in(name: str, texts: dict[str, str]) -> list[str]:
    """Slugs whose raw text carries ``name`` as a whole, case-insensitive,
    non-alphanumeric-bounded token (see module docstring for why this
    mirrors skill-search tokenization)."""
    rx = re.compile(
        r"(?<!"
        + _NAME_BOUNDARY
        + ")"
        + re.escape(name)
        + r"(?!"
        + _NAME_BOUNDARY
        + ")",
        re.IGNORECASE,
    )
    return sorted(slug for slug, text in texts.items() if rx.search(text))


def _literal_segments(pattern: str) -> set[str]:
    """The coined literal segments of an id pattern: ``{var}`` placeholders
    and the ``:``/``/`` scaffolding removed, then each remaining run
    (``child-failed-parked``, ``papers_ingested``) under the same ≥3-char
    floor the tokenizer applies."""
    without_vars = _VAR_RE.sub("/", pattern)
    out = set()
    for seg in re.split(r"[/:]", without_vars):
        seg = seg.strip("-_")
        if len(seg) >= 3:
            out.add(seg)
    return out


def _alert_rule_names() -> dict[str, set[str]]:
    """Every literal segment of every ``ALERT_RULES`` id → its rule ids."""
    from precis.alert_ids import ALERT_RULES

    out: dict[str, set[str]] = {}
    for rule in ALERT_RULES:
        for seg in _literal_segments(rule.rule) | _literal_segments(rule.subject):
            out.setdefault(seg, set()).add(rule.id)
    return out


def _job_type_names() -> dict[str, str]:
    """``{job_type name → 'builtin'|'plugin'}`` — the built-in list read
    statically from ``known_job_types`` (mirrors ``test_alert_ids``' AST
    style: no import side effects, no installed-dist drift) plus the
    ``precis.job_types`` entry-point names this repo's pyproject declares."""
    from precis.workers import job_types

    tree = ast.parse(Path(job_types.__file__).read_text(encoding="utf-8"))
    builtins: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "known_job_types":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.List):
                    builtins = [
                        str(e.value)
                        for e in sub.value.elts
                        if isinstance(e, ast.Constant)
                    ]
    assert builtins, "could not AST-read the builtins list in known_job_types"

    pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugins = sorted(pyproj["project"]["entry-points"]["precis.job_types"])
    names = {n: "builtin" for n in builtins}
    names.update({n: "plugin" for n in plugins})
    return names


def _status_names() -> set[str]:
    """``STATUS`` values — the union vocabulary plus every per-kind
    subset (todo/gripe/quest lifecycle)."""
    from precis.store.types import _CLOSED_VOCAB, _KIND_STATUS_VALUES

    out = set(_CLOSED_VOCAB["STATUS"])
    for vals in _KIND_STATUS_VALUES.values():
        out |= vals
    return out


def _closed_vocab_names() -> set[str]:
    """Non-STATUS closed-vocab axes and their values (``AUDIT``,
    ``DREAM``, ``WATCH``, ``TAPROOT``, …) — agent-writable tag
    vocabulary, minus the STATUS axis covered separately."""
    from precis.store.types import _CLOSED_VOCAB

    out: set[str] = set()
    for axis, values in _CLOSED_VOCAB.items():
        if axis == "STATUS":
            continue
        out.add(axis)
        out |= set(values)
    return out


def _health_check_names() -> set[str]:
    """Fixed ``health_digest`` check names: the two tabular key tuples
    plus every literal ``CheckResult(group, name)`` emitted by a bespoke
    check (AST-read — the names are string literals, not constants)."""
    from precis.workers import health_digest

    names = {k for k, *_rest in health_digest._FRESHNESS_CHECKS}
    names |= {k for k, *_rest in health_digest._BACKLOG_CHECKS}
    tree = ast.parse(Path(health_digest.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CheckResult"
        ):
            continue
        args: dict[str, ast.AST] = {}
        for i, a in enumerate(node.args[:2]):
            args[("group", "name")[i]] = a
        for k in node.keywords:
            if k.arg in ("group", "name"):
                args[k.arg] = k.value
        n = args.get("name")
        if isinstance(n, ast.Constant):
            names.add(str(n.value))
    return names


def _condition_names() -> set[str]:
    """``conditions.CONDITIONS`` names — the declarative probe registry."""
    from precis.workers.conditions import CONDITIONS

    return {c.name for c in CONDITIONS}


def _nursery_categories() -> set[str]:
    """``nursery._DETECTORS`` category names — including ``_NO_ALERT``
    rows, which still write category-named log lines agents read."""
    from precis.workers import nursery

    return {cat for cat, _detect in nursery._DETECTORS}


def _service_names() -> set[str]:
    """``registry.SERVICES`` ``name`` + ``log_handler`` — the ``--only`` /
    ``service_config.service`` / ``worker_logs.handler`` tokens an agent
    sees in alert triage and the /status console."""
    from precis.workers.registry import SERVICES

    return {s.name for s in SERVICES} | {s.log_handler for s in SERVICES}


def _alert_lifecycle_names() -> set[str]:
    """The ``alert-state:`` open-tag lifecycle values."""
    from precis.alerts import STATE_OPEN, STATE_RESOLVED

    return {STATE_OPEN, STATE_RESOLVED}


def _registries() -> dict[str, Mapping[str, object] | set[str]]:
    """Registry label → its name set. ``alert rule ids`` and
    ``job_type names`` carry name→detail maps; the rest are plain sets."""
    return {
        "alert rule ids (alert_ids.ALERT_RULES)": _alert_rule_names(),
        "job_type names (workers.job_types)": _job_type_names(),
        "STATUS values (store.types)": _status_names(),
        "closed-vocab axes+values (store.types)": _closed_vocab_names(),
        "health_digest check names": _health_check_names(),
        "conditions.CONDITIONS names": _condition_names(),
        "nursery detector categories": _nursery_categories(),
        "worker service names (registry.SERVICES)": _service_names(),
        "alert lifecycle tags (alerts)": _alert_lifecycle_names(),
    }


def test_every_registry_name_appears_in_a_skill() -> None:
    texts = _skill_texts()
    assert texts, "no skills found — _skill_roots broken?"
    missing: list[str] = []
    for registry, names in _registries().items():
        for name in sorted(names):
            if name in _ALLOWLIST:
                continue
            hits = _documented_in(name, texts)
            if not hits:
                missing.append(f"  [{registry}] {name}")
    assert not missing, (
        f"{len(missing)} registry name(s) appear in no skill — "
        "search(kind='skill', q=<name>) can't reach the doc. Document each "
        "in the skill that owns it (a short line or H2), or allowlist it "
        "here with a reason if it is genuinely internal:\n" + "\n".join(missing)
    )


def test_allowlist_entries_are_real_and_reasoned() -> None:
    names: set[str] = set()
    for registered in _registries().values():
        names |= set(registered)
    for name, reason in _ALLOWLIST.items():
        assert name in names, f"_ALLOWLIST entry {name!r} is not in any registry"
        assert reason and reason.strip(), (
            f"_ALLOWLIST entry {name!r} must carry a one-line reason"
        )
