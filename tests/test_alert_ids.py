"""Totality gate for the alert failure-id registry (``precis.alert_ids``).

Modelled on ``tests/test_worker_registry.py``: the registry is the
single enumerable table of failure ids, so these tests keep it from
drifting from the producers in both directions —

* every ``raise_alert`` call site in ``src/`` supplies a ``source=`` that
  resolves (constant, module constant, local f-string, or a helper's
  returned f-string) to a registered rule's source pattern, and every
  registered source pattern has a raise site;
* every fixed-name ``health_digest`` check is pinned to a fingerprint in
  ``_PINNED_FINGERPRINTS`` that resolves to a registered rule, so a check
  rename is a reviewed id change rather than a silent new id + a silent
  auto-resolve of the old one (spec defect 2);
* nursery detector categories and condition-registry finding keys match
  their rules.

Pure static checks — AST over the source tree, no DB.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from precis import alert_ids
from precis.alert_ids import ALERT_RULES, RULES_BY_ID, AlertRule
from precis.alerts import SEVERITIES
from precis.workers import conditions, health_digest, nursery
from precis.workers.registry import SERVICES_BY_NAME

_SRC = Path(__file__).resolve().parents[1] / "src"
_VAR = re.compile(r"\{[^{}]*\}")


def _pattern_of(node: ast.AST | None) -> str | None:
    """A string literal or f-string → pattern with ``{var}`` placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                out.append("{" + ast.unparse(part.value) + "}")
        return "".join(out)
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            pat = _pattern_of(node.value)
            if isinstance(tgt, ast.Name) and pat is not None:
                out[tgt.id] = pat
    return out


def _function_returns(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    pat = _pattern_of(sub.value)
                    if pat is not None:
                        out[node.name] = pat
    return out


def _local_assignment(func: ast.AST, name: str) -> str | None:
    for sub in ast.walk(func):
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            tgt = sub.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == name:
                pat = _pattern_of(sub.value)
                if pat is not None:
                    return pat
    return None


def _raise_sites() -> list[tuple[str, str]]:
    """``(file, source pattern)`` for every ``raise_alert(`` call in src."""
    sites: list[tuple[str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "alerts.py" and path.parent.name == "precis":
            continue
        text = path.read_text(encoding="utf-8")
        if "raise_alert(" not in text:
            continue
        tree = ast.parse(text)
        consts = _module_constants(tree)
        returns = _function_returns(tree)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for func in funcs:
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                fname = (
                    fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                )
                if fname not in ("raise_alert", "_raise_alert"):
                    continue
                kw = next((k for k in call.keywords if k.arg == "source"), None)
                assert kw is not None, f"{path}: raise_alert without source="
                pat = _pattern_of(kw.value)
                if pat is None and isinstance(kw.value, ast.Name):
                    pat = _local_assignment(func, kw.value.id) or consts.get(
                        kw.value.id
                    )
                if pat is None and isinstance(kw.value, ast.Call):
                    callee = kw.value.func
                    if isinstance(callee, ast.Name):
                        pat = returns.get(callee.id)
                assert pat is not None, (
                    f"{path}:{call.lineno}: cannot statically resolve source= "
                    f"({ast.unparse(kw.value)}) — use a constant, a local "
                    "f-string, or a helper returning one"
                )
                sites.append((str(path.relative_to(_SRC)), pat))
    return sites


def _site_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(".+".join(re.escape(p) for p in _VAR.split(pattern)))


def test_every_raise_site_source_is_registered() -> None:
    sites = _raise_sites()
    assert sites, "no raise_alert call sites found — resolver broken?"
    registered = {r.rule for r in ALERT_RULES}
    unregistered = sorted(
        {
            f"{file}: {pat}"
            for file, pat in sites
            if not any(_site_regex(pat).fullmatch(src) for src in registered)
        }
    )
    assert not unregistered, (
        "raise_alert call sites whose source= matches no registered AlertRule "
        f"(add one to precis/alert_ids.py): {unregistered}"
    )


def test_every_registered_source_has_a_raise_site() -> None:
    patterns = {pat for _file, pat in _raise_sites()}
    dangling = sorted(
        {
            r.rule
            for r in ALERT_RULES
            if not any(_site_regex(pat).fullmatch(r.rule) for pat in patterns)
        }
    )
    assert not dangling, (
        "AlertRule sources with no raise_alert call site (stale rule, or the "
        f"raiser's source= is no longer resolvable): {dangling}"
    )


# ── registry hygiene ─────────────────────────────────────────────────────


def test_rule_ids_unique_and_well_formed() -> None:
    assert len(RULES_BY_ID) == len(ALERT_RULES)
    for r in ALERT_RULES:
        assert "/" not in r.rule, f"{r.id}: a source may not contain '/'"
        assert r.subject, f"{r.id}: subject must be non-empty (fingerprint is)"
        assert r.severity in SEVERITIES, r.id
        assert r.one_line.strip(), r.id


def test_service_pointers_name_registered_services() -> None:
    bad = [r.id for r in ALERT_RULES if r.service and r.service not in SERVICES_BY_NAME]
    assert not bad, f"AlertRule.service not in SERVICES_BY_NAME: {bad}"


def test_every_critical_rule_carries_a_triage_line() -> None:
    missing = [r.id for r in ALERT_RULES if r.severity == "critical" and not r.triage]
    assert not missing, f"critical rules need a triage line: {missing}"


# ── resolve(): pattern matching ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "fingerprint", "expected"),
    [
        ("watchdog:discovery", "embed", "watchdog:discovery/embed"),
        (
            "nursery:nas-denied",
            "nas-denied:melchior:precis-worker",
            "nursery:nas-denied/nas-denied:{host}:{process}",
        ),
        (
            "nursery:nas-denied",
            "nas-denied:melchior",
            "nursery:nas-denied/nas-denied:{host}",
        ),
        (
            "watchdog:cadence",
            "materialize/never-seeded",
            "watchdog:cadence/{cadence}/never-seeded",
        ),
        ("watchdog:cadence", "materialize", "watchdog:cadence/{cadence}"),
        (
            "watchdog:condition",
            "llm-degraded:z-ai/glm-4.7-flash/openai_compat/cloud",
            "watchdog:condition/llm-degraded:{model}/{transport}/{tier}",
        ),
        ("disk_check", "caspar:/", "disk_check/{host}:{path}"),
        (
            "review:empty:structural",
            "structural:empty-pass",
            "review:empty:{reviewer}/{reviewer}:empty-pass",
        ),
        ("budget:quota", "quota-day", "budget:quota/quota-{window}"),
    ],
)
def test_resolve_picks_the_most_specific_rule(
    source: str, fingerprint: str, expected: str
) -> None:
    rule = alert_ids.resolve(source, fingerprint)
    assert rule is not None and rule.id == expected


def test_resolve_unregistered_is_none() -> None:
    assert alert_ids.resolve("watchdog:mygroup", "flaky-check") is None
    assert alert_ids.rule_id("nope", "x") is None


def test_split_id_splits_on_first_slash_only() -> None:
    assert alert_ids.split_id("watchdog:condition/pass-dead:h/p/x") == (
        "watchdog:condition",
        "pass-dead:h/p/x",
    )
    assert alert_ids.split_id("disk_check/caspar:/") == ("disk_check", "caspar:/")


# ── health_digest: pinned ids ────────────────────────────────────────────


def _health_digest_fixed_checks() -> set[tuple[str, str]]:
    """Every constant ``(group, name)`` a ``CheckResult`` in health_digest
    can be a *finding* under (status not a constant other than "stale"),
    plus the two tabular check sets."""
    tree = ast.parse(Path(health_digest.__file__).read_text(encoding="utf-8"))
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CheckResult"
        ):
            continue
        args: dict[str, ast.AST] = {}
        for i, a in enumerate(node.args[:3]):
            args[("group", "name", "status")[i]] = a
        for k in node.keywords:
            if k.arg in ("group", "name", "status"):
                args[k.arg] = k.value
        g, n, st = args.get("group"), args.get("name"), args.get("status")
        if isinstance(st, ast.Constant) and st.value != "stale":
            continue
        if isinstance(g, ast.Constant) and isinstance(n, ast.Constant):
            pairs.add((str(g.value), str(n.value)))
    for key, group, *_rest in health_digest._FRESHNESS_CHECKS:
        pairs.add((group, key))
    for key, group, *_rest in health_digest._BACKLOG_CHECKS:
        pairs.add((group, key))
    return pairs


def test_every_fixed_health_digest_check_is_pinned_to_a_registered_id() -> None:
    pairs = _health_digest_fixed_checks()
    assert pairs
    pinned = health_digest._PINNED_FINGERPRINTS
    unpinned = sorted(p for p in pairs if p not in pinned)
    assert not unpinned, (
        "health_digest checks with no pinned fingerprint (add to "
        f"_PINNED_FINGERPRINTS — a rename must be a reviewed id change): {unpinned}"
    )
    unregistered = sorted(
        f"watchdog:{g}/{fp}"
        for (g, _n), fp in pinned.items()
        if alert_ids.resolve(f"watchdog:{g}", fp) is None
    )
    assert not unregistered, f"pinned ids not in precis.alert_ids: {unregistered}"
    stale_pins = sorted(p for p in pinned if p not in pairs)
    assert not stale_pins, f"_PINNED_FINGERPRINTS entries for no check: {stale_pins}"


def test_every_singleton_watchdog_rule_is_a_pinned_check() -> None:
    pinned_ids = {
        f"watchdog:{g}/{fp}"
        for (g, _n), fp in health_digest._PINNED_FINGERPRINTS.items()
    }
    derived_groups = {"watchdog:cadence", "watchdog:coherence", "watchdog:condition"}
    orphans = sorted(
        r.id
        for r in ALERT_RULES
        if r.rule.startswith("watchdog:")
        and r.rule not in derived_groups
        and r.id not in pinned_ids
    )
    assert not orphans, f"watchdog rules no health_digest check emits: {orphans}"


def test_tabular_check_budgets_and_severities_match_the_registry() -> None:
    rows = [
        (group, key, budget, sev)
        for key, group, _sql, budget, sev, _what in health_digest._FRESHNESS_CHECKS
    ] + [
        (group, key, budget, sev)
        for key, group, budget, sev, _what in health_digest._BACKLOG_CHECKS
    ]
    for group, key, budget_hours, severity in rows:
        rule = alert_ids.resolve(
            f"watchdog:{group}", health_digest._PINNED_FINGERPRINTS[(group, key)]
        )
        assert rule is not None
        assert rule.budget == f"{budget_hours:g}h", (rule.id, rule.budget, budget_hours)
        assert rule.severity == severity, rule.id


def test_check_fingerprint_is_pinned_not_derived() -> None:
    """Spec defect 2: the emitted id follows the pin, not ``CheckResult.name``."""
    c = health_digest.CheckResult("discovery", "embed", "stale", "x", "warn")
    assert health_digest.check_fingerprint(c) == "embed"
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            health_digest._PINNED_FINGERPRINTS,
            ("discovery", "chunks_embedded"),
            "embed",
        )
        renamed = health_digest.CheckResult(
            "discovery", "chunks_embedded", "stale", "x", "warn"
        )
        assert health_digest.check_fingerprint(renamed) == "embed"
    derived = health_digest.CheckResult("cadence", "materialize", "stale", "x", "warn")
    assert health_digest.check_fingerprint(derived) == "materialize"


# ── nursery + conditions ─────────────────────────────────────────────────


def _nursery_rules() -> dict[str, list[AlertRule]]:
    out: dict[str, list[AlertRule]] = {}
    for r in ALERT_RULES:
        if r.rule.startswith("nursery:"):
            out.setdefault(r.rule.removeprefix("nursery:"), []).append(r)
    return out


def test_every_alerting_nursery_category_has_a_rule_and_vice_versa() -> None:
    alerting = {cat for cat, _d in nursery._DETECTORS if cat not in nursery._NO_ALERT}
    rules = set(_nursery_rules())
    assert alerting == rules, (
        f"nursery categories without a rule: {sorted(alerting - rules)}; "
        f"rules without a detector: {sorted(rules - alerting)}"
    )


def test_nursery_severities_match_the_registry() -> None:
    for cat, rules in _nursery_rules().items():
        for r in rules:
            assert r.severity == nursery._SEVERITY.get(cat, "warn"), r.id


def _keyword_patterns(module_file: str, keyword: str) -> set[str]:
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for k in node.keywords:
                if k.arg == keyword:
                    pat = _pattern_of(k.value)
                    if pat is not None:
                        out.add(pat)
    return out


def test_every_nursery_explicit_fingerprint_key_resolves() -> None:
    """The explicit ``fingerprint_key=`` spellings resolve to a rule when
    their ``{var}`` placeholders are fed through literally (``.+`` matches
    the brace text) — so a new key shape needs a registry row."""
    pats = _keyword_patterns(nursery.__file__, "fingerprint_key")
    assert pats
    for pat in pats:
        cat = pat.split(":", 1)[0]
        if cat in nursery._NO_ALERT:
            continue
        assert alert_ids.resolve(f"nursery:{cat}", pat) is not None, pat


def test_every_condition_finding_key_resolves() -> None:
    pats = _keyword_patterns(conditions.__file__, "key")
    assert len(pats) >= len(conditions.CONDITIONS)
    unresolved = sorted(
        pat for pat in pats if alert_ids.resolve("watchdog:condition", pat) is None
    )
    assert not unresolved, f"condition finding keys with no rule: {unresolved}"
    cond_rules = [r for r in ALERT_RULES if r.rule == "watchdog:condition"]
    assert len(cond_rules) == len(conditions.CONDITIONS)
