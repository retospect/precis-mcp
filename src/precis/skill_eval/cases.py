"""Eval-case model + YAML loader for the skill eval harness.

One file per skill, ``src/precis/data/skill-evals/<skill>.yaml``::

    skill: precis-paper-help
    cases:
      - id: open-by-doi
        prompt: |
          I have the DOI 10.1038/nature10352. Read its abstract.
        skills: [precis-paper-help]        # injected; default [skill]
        expect:
          calls:                           # each must match >= 1 call made
            - {verb: get, kind: paper, args: {id: "10.1038/nature10352"}}
          record: {kind: draft}            # optional subset match
        forbid:
          calls: [{verb: search, kind: web}]
          text: ["SELECT "]                # answer substrings that mark a detour
        fake:                              # what FakeRunner replays
          calls: [{verb: get, kind: paper, args: {id: "10.1038/nature10352"}}]
          record: null
          answer: "..."

``args`` values match a transcript call's args as a subset: a string
value ``re:<pattern>`` is a full-match regex against ``str(actual)``, a
dict recurses, a list requires every expected element to be present, and
anything else compares by ``==`` or by ``str()``. Every case carries a
``fake:`` transcript so the shipped corpus is self-checking under the fake
runner (``scripts/skill-eval --runner fake`` is green by construction;
``tests/test_skill_eval.py`` pins it).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, get_args

import yaml

from precis.errors import BadInput
from precis.protocol import Verb

VERBS: frozenset[str] = frozenset(get_args(Verb))
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_RE_PREFIX = "re:"


@dataclass(frozen=True, slots=True)
class VerbCall:
    """One precis verb call an agent made (or declared it would make)."""

    verb: str
    kind: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        parts = [f"kind={self.kind!r}"] if self.kind else []
        parts += [f"{k}={v!r}" for k, v in self.args.items()]
        return f"{self.verb}({', '.join(parts)})"


@dataclass(frozen=True, slots=True)
class Transcript:
    """What a runner hands back for one case: the calls, the record the
    agent produced (or declared), its final answer and what it cost."""

    calls: tuple[VerbCall, ...] = ()
    record: dict[str, Any] | None = None
    answer: str = ""
    cost_usd: float | None = None


def value_matches(expected: Any, actual: Any) -> bool:
    """Subset/regex match of an expected criterion value against an actual."""
    if isinstance(expected, str) and expected.startswith(_RE_PREFIX):
        return re.fullmatch(expected[len(_RE_PREFIX) :], str(actual), re.S) is not None
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and value_matches(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(value_matches(e, a) for a in actual) for e in expected
        )
    return bool(expected == actual) or str(expected) == str(actual)


@dataclass(frozen=True, slots=True)
class CallPattern:
    """A verb/kind/args shape a transcript call either must or must not fit."""

    verb: str
    kind: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def matches(self, call: VerbCall) -> bool:
        if call.verb != self.verb:
            return False
        if self.kind is not None and call.kind != self.kind:
            return False
        return value_matches(self.args, call.args)

    def render(self) -> str:
        return VerbCall(self.verb, self.kind, self.args).render()


@dataclass(frozen=True, slots=True)
class Criterion:
    """Success = every ``calls`` pattern matched, ``record`` (if set) matched,
    no ``forbid_calls`` pattern matched, no ``forbid_text`` in the answer."""

    calls: tuple[CallPattern, ...] = ()
    record: dict[str, Any] | None = None
    forbid_calls: tuple[CallPattern, ...] = ()
    forbid_text: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str  # "<skill>/<id>"
    skill: str
    skills: tuple[str, ...]
    prompt: str
    criterion: Criterion
    fake: Transcript


def default_cases_dir() -> Path:
    """The seed corpus shipped as package data."""
    return Path(str(resources.files("precis.data").joinpath("skill-evals")))


def _bad(where: str, msg: str) -> BadInput:
    return BadInput(f"skill eval: {where}: {msg}")


def _load_call(raw: Any, where: str) -> VerbCall:
    if not isinstance(raw, dict) or not isinstance(raw.get("verb"), str):
        raise _bad(where, "a call needs a string 'verb'")
    verb = raw["verb"]
    if verb not in VERBS:
        raise BadInput(
            f"skill eval: {where}: unknown verb {verb!r}", options=sorted(VERBS)
        )
    kind = raw.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise _bad(where, "'kind' must be a string")
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise _bad(where, "'args' must be a mapping")
    return VerbCall(verb=verb, kind=kind, args=dict(args))


def _load_patterns(raw: Any, where: str) -> tuple[CallPattern, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _bad(where, "must be a list of calls")
    out = []
    for i, item in enumerate(raw):
        c = _load_call(item, f"{where}[{i}]")
        out.append(CallPattern(c.verb, c.kind, c.args))
    return tuple(out)


def _load_record(raw: Any, where: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _bad(where, "'record' must be a mapping or null")
    return dict(raw)


def _load_transcript(raw: Any, where: str) -> Transcript:
    if not isinstance(raw, dict):
        raise _bad(where, "'fake' must be a mapping with calls/record/answer")
    calls = raw.get("calls") or []
    if not isinstance(calls, list):
        raise _bad(where, "'calls' must be a list")
    return Transcript(
        calls=tuple(_load_call(c, f"{where}.calls[{i}]") for i, c in enumerate(calls)),
        record=_load_record(raw.get("record"), where),
        answer=str(raw.get("answer") or ""),
    )


def _load_case(
    raw: Any, skill: str, where: str, known_skills: Collection[str] | None
) -> EvalCase:
    if not isinstance(raw, dict):
        raise _bad(where, "case is not a mapping")
    cid = raw.get("id")
    if not isinstance(cid, str) or not _SLUG_RE.match(cid):
        raise _bad(where, "case 'id' must be a lowercase slug")
    where = f"{skill}/{cid}"
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad(where, "'prompt' must be non-empty text")
    skills_raw = raw.get("skills") or [skill]
    if not isinstance(skills_raw, list) or not all(
        isinstance(s, str) for s in skills_raw
    ):
        raise _bad(where, "'skills' must be a list of slugs")
    if known_skills is not None:
        unknown = [s for s in skills_raw if s not in known_skills]
        if unknown:
            raise _bad(where, f"unknown skill(s) {unknown}")
    expect = raw.get("expect") or {}
    forbid = raw.get("forbid") or {}
    if not isinstance(expect, dict) or not isinstance(forbid, dict):
        raise _bad(where, "'expect' and 'forbid' must be mappings")
    forbid_text = forbid.get("text") or []
    if not isinstance(forbid_text, list) or not all(
        isinstance(t, str) and t for t in forbid_text
    ):
        raise _bad(where, "'forbid.text' must be a list of non-empty strings")
    criterion = Criterion(
        calls=_load_patterns(expect.get("calls"), f"{where}.expect.calls"),
        record=_load_record(expect.get("record"), f"{where}.expect"),
        forbid_calls=_load_patterns(forbid.get("calls"), f"{where}.forbid.calls"),
        forbid_text=tuple(forbid_text),
    )
    if not criterion.calls and criterion.record is None:
        raise _bad(where, "'expect' needs at least one of 'calls' or 'record'")
    if "fake" not in raw:
        raise _bad(where, "'fake' transcript is required (the corpus self-check)")
    return EvalCase(
        case_id=where,
        skill=skill,
        skills=tuple(skills_raw),
        prompt=prompt.strip(),
        criterion=criterion,
        fake=_load_transcript(raw["fake"], f"{where}.fake"),
    )


def load_case_file(
    path: Path, *, known_skills: Collection[str] | None = None
) -> list[EvalCase]:
    """Load one ``<skill>.yaml``; the file's ``skill:`` must equal its stem."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BadInput(f"skill eval: cannot read {path}: {exc}") from exc
    where = path.name
    if not isinstance(raw, dict):
        raise _bad(where, "top level must be a mapping with 'skill' and 'cases'")
    skill = raw.get("skill")
    if skill != path.stem:
        raise _bad(where, f"'skill' must equal the file stem {path.stem!r}")
    if known_skills is not None and skill not in known_skills:
        raise _bad(where, f"unknown skill {skill!r}")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise _bad(where, "'cases' must be a non-empty list")
    out = [
        _load_case(c, skill, f"{where} case #{i}", known_skills)
        for i, c in enumerate(cases)
    ]
    ids = [c.case_id for c in out]
    if len(set(ids)) != len(ids):
        raise _bad(where, "duplicate case ids")
    return out


def shipped_skill_slugs() -> frozenset[str]:
    """Every shipped (or plugin-contributed) skill slug — the default
    vocabulary ``skills:`` is validated against."""
    from precis.handlers.skill import skill_corpus_texts

    return frozenset(skill_corpus_texts())


def load_cases(
    directory: str | Path | None = None,
    *,
    known_skills: Collection[str] | None = None,
    skills: Iterable[str] | None = None,
) -> list[EvalCase]:
    """Load every ``*.yaml`` under ``directory`` (default: the shipped seed
    corpus), validated against ``known_skills`` (default: the shipped skill
    corpus). ``skills`` restricts the result to cases *owned by* those
    skills (the file-level ``skill:``)."""
    root = Path(directory) if directory is not None else default_cases_dir()
    if not root.is_dir():
        raise BadInput(f"skill eval: cases dir {root} does not exist")
    if known_skills is None:
        known_skills = shipped_skill_slugs()
    want = set(skills) if skills is not None else None
    out: list[EvalCase] = []
    for path in sorted(root.glob("*.yaml")):
        if want is not None and path.stem not in want:
            continue
        out.extend(load_case_file(path, known_skills=known_skills))
    return out


__all__ = [
    "VERBS",
    "CallPattern",
    "Criterion",
    "EvalCase",
    "Transcript",
    "VerbCall",
    "default_cases_dir",
    "load_case_file",
    "load_cases",
    "shipped_skill_slugs",
    "value_matches",
]
