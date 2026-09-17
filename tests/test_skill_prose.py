"""Skill prose ratchet — unfollowable references and operator affordances.

``src/precis/data/skills/*.md`` are served verbatim to agents via
``get(kind='skill')`` (frontmatter stripped, ``{{include}}`` and
``[[wikilink]]`` expanded, nothing else). Every ADR number, env var, CLI
line or backlog path in a body therefore reaches an agent as text it cannot
act on: it has the seven verbs and nothing else. The prose convention
(``docs/conventions/skill-authoring-style.md``) says cut them; nothing in the
ship gate enforced it, so they crept back. This test does — as a ratchet.

Five checks, each a regex-level scan over one skill body:

- ``adr``: ``ADR 0042`` / ``ADR-0042`` anywhere. No ADR file exists in the
  tree, so the reference resolves to nothing.
- ``backlog``: ``docs/backlog/`` in prose. Reachable via
  ``get(kind='md', …)`` inside a fenced call, but a bare path in prose rots
  the day the item ships (delete-on-ship).
- ``operator``: env-var assignments, ``precis jobs|worker|service|cast``
  CLI lines, raw SQL. Operator affordances, not agent ones.
- ``unfenced_verb``: a verb call (``get(`` … ``link(``) on a line outside a
  triple-backtick fence — four-space indented blocks included. The fence is
  what tells the agent "issue this verbatim"; without it the call gets
  paraphrased.
- ``alias_overrun``: more than four consecutive H2s sharing one body. The
  chunker embeds each alias separately; past 3–4 the retrieval lift is
  nil and the storage cost is real.

Frozen allowlist (``_ALLOWLIST``): the offender counts on the day the gate
landed. A file may only match its recorded count or fewer; when it shrinks,
the entry must be lowered (so it cannot regrow), and when it reaches zero the
entry must be deleted. New files start at zero tolerance.

Gripe ids (``gr\\d{6}``) are valid ``get`` handles and are deliberately not
gated. Bare-noun H2s are an advisory warning only.

Pure file stats — no DB, no embedder, no imports beyond stdlib + pytest.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"

_ADR = re.compile(r"\bADR[ -]?\d{4}\b")
_BACKLOG = re.compile(r"docs/backlog/")
_MD_GET = re.compile(r"""kind=['"]md['"]""")
_OPERATOR = re.compile(
    r"\bPRECIS_[A-Z_]+="
    r"|\bprecis (jobs|worker|service|cast)\b"
    r"|\bINSERT INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
)
_VERB_LINE = re.compile(r"^\s*(get|search|put|edit|delete|tag|link)\(")
_H2 = re.compile(r"^## (.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")

#: Consecutive H2s sharing one body beyond this count is an overrun.
ALIAS_MAX = 4

#: Bare-noun H2 heuristic: this many words or fewer, not a goal statement.
_BARE_H2_MAX_WORDS = 2
_BARE_H2_EXEMPT = {"see also"}


def _body_lines(text: str) -> list[tuple[int, str]]:
    """(lineno, line) pairs for the served body — frontmatter skipped, since
    the loader strips it before an agent ever sees the file."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    return [(i + 1, line) for i, line in enumerate(lines) if i >= start]


def _walk(text: str) -> list[tuple[int, str, bool]]:
    """(lineno, line, in_fence) — fence delimiter lines count as in-fence."""
    out: list[tuple[int, str, bool]] = []
    in_fence = False
    for n, line in _body_lines(text):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append((n, line, True))
            continue
        out.append((n, line, in_fence))
    return out


def _check_adr(text: str) -> list[int]:
    return [n for n, line in _body_lines(text) if _ADR.search(line)]


def _check_backlog(text: str) -> list[int]:
    return [
        n
        for n, line, fenced in _walk(text)
        if _BACKLOG.search(line) and not (fenced and _MD_GET.search(line))
    ]


def _check_operator(text: str) -> list[int]:
    return [n for n, line in _body_lines(text) if _OPERATOR.search(line)]


def _check_unfenced_verb(text: str) -> list[int]:
    return [
        n for n, line, fenced in _walk(text) if not fenced and _VERB_LINE.match(line)
    ]


def _check_alias_overrun(text: str) -> list[int]:
    """Start line of every run of more than ``ALIAS_MAX`` body-less H2s."""
    hits: list[int] = []
    run = 0
    run_start = 0
    for n, line, fenced in _walk(text):
        if not fenced and _H2.match(line):
            if run == 0:
                run_start = n
            run += 1
        elif fenced or line.strip():  # a fence is a body too
            if run > ALIAS_MAX:
                hits.append(run_start)
            run = 0
    if run > ALIAS_MAX:
        hits.append(run_start)
    return hits


CHECKS: dict[str, Callable[[str], list[int]]] = {
    "adr": _check_adr,
    "backlog": _check_backlog,
    "operator": _check_operator,
    "unfenced_verb": _check_unfenced_verb,
    "alias_overrun": _check_alias_overrun,
}

_FIX_HINT: dict[str, str] = {
    "adr": "delete the ADR number; keep the consequence, not the citation",
    "backlog": (
        "drop the path from prose, or make it a fenced "
        "get(kind='md', id='docs/backlog/…') call"
    ),
    "operator": (
        "cut the env var / CLI / SQL line, or replace it with 'ask a human "
        "operator to …' and move the recipe to docs/runbooks/"
    ),
    "unfenced_verb": "wrap the call in a ```python fence",
    "alias_overrun": f"keep at most {ALIAS_MAX} consecutive H2 aliases per body",
}

#: check -> slug -> offender count on the day the ratchet landed
#: (2026-09-17, batch C of docs/backlog/skills-prose-cleanup.md). Batches
#: D–F drain these; a lowered count is the only edit this table accepts.
_ALLOWLIST: dict[str, dict[str, int]] = {
    "adr": {
        "components": 1,
        "patent-claim": 1,
        "patent-description": 1,
        "patent-image-part": 2,
        "patent-prior-art": 1,
        "precis-addressing-help": 2,
        "precis-argument-help": 2,
        "precis-auto-todo-help": 1,
        "precis-automations": 1,
        "precis-cad-help": 1,
        "precis-component-help": 1,
        "precis-figure-help": 2,
        "precis-fisheye-help": 2,
        "precis-folder-help": 2,
        "precis-job-help": 1,
        "precis-lab-help": 2,
        "precis-mermaid-help": 1,
        "precis-paper-tag-axes": 2,
        "precis-pcb-help": 2,
        "precis-plan-help": 3,
        "precis-proposal-help": 1,
        "precis-protein-help": 5,
        "precis-recurring-help": 1,
        "precis-structure-help": 3,
        "precis-taproot-backfill-help": 1,
        "precis-taproot-help": 2,
    },
    "backlog": {
        "personas/precis-claim-adversary": 1,
        "precis-addressing-help": 1,
        "precis-link-help": 1,
        "precis-md-help": 1,
        "precis-nanopub-help": 3,
        "precis-overview": 1,
        "precis-paper-help": 1,
        "precis-relations": 1,
        "precis-rxn-help": 1,
        "precis-se-help": 1,
        "precis-taproot-help": 1,
        "precis-taproot-mint-help": 1,
        "precis-toolpath-help": 1,
    },
    "operator": {
        "personas/precis-citation-reviewer": 1,
        "precis-anki-help": 2,
        "precis-auto-todo-help": 2,
        "precis-doi-extract-help": 1,
        "precis-files-help": 5,
        "precis-finding-help": 1,
        "precis-inner-life-help": 2,
        "precis-job-help": 2,
        "precis-md-help": 1,
        "precis-nursery-help": 2,
        "precis-paper-help": 2,
        "precis-patent-power": 9,
        "precis-perplexity-help": 3,
        "precis-preflight": 3,
        "precis-provenance-help": 4,
        "precis-python-help": 2,
        "precis-recurring-help": 1,
        "precis-session-context-help": 3,
        "precis-startup-skills-help": 2,
        "precis-wikipedia-help": 1,
    },
    "unfenced_verb": {
        "personas/precis-draft-reviewer": 1,
        "personas/precis-review-authoring": 4,
        "precis-figure-help": 10,
    },
    "alias_overrun": {
        "precis-anki-help": 1,
        "precis-status-help": 7,
    },
}


def _skill_files() -> list[Path]:
    """Top-level skills plus ``personas/`` — both are served bodies."""
    return sorted(_SKILLS_DIR.rglob("*.md"))


def _key(path: Path) -> str:
    """Allowlist key: path relative to the skills dir, no suffix
    (``precis-get-help``, ``personas/precis-draft-reviewer``)."""
    return path.relative_to(_SKILLS_DIR).with_suffix("").as_posix()


@pytest.mark.parametrize("path", _skill_files(), ids=_key)
def test_skill_prose_within_ratchet(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    failures: list[str] = []
    for check, fn in CHECKS.items():
        hits = fn(text)
        allowed = _ALLOWLIST[check].get(_key(path), 0)
        if len(hits) > allowed:
            failures.append(
                f"{check}: {len(hits)} hit(s) at line(s) {hits} (allowed {allowed}) "
                f"— {_FIX_HINT[check]}"
            )
    assert not failures, (
        f"{_key(path)}: skill prose ratchet tripped —\n  "
        + "\n  ".join(failures)
        + "\n  Skill bodies reach agents verbatim; an agent has the seven verbs "
        "and nothing else (docs/conventions/skill-authoring-style.md). The "
        "allowlist in tests/test_skill_prose.py only shrinks — do not add to it."
    )


def test_skill_prose_allowlist_only_shrinks() -> None:
    """Every allowlist entry must still match exactly: a file that shrank
    needs its count lowered (so it cannot regrow), a file at zero needs the
    entry deleted, a deleted file needs the entry deleted."""
    stale: list[str] = []
    for check, entries in _ALLOWLIST.items():
        for slug, allowed in entries.items():
            path = _SKILLS_DIR / f"{slug}.md"
            if not path.exists():
                stale.append(f"{check}/{slug}: file gone — delete the entry")
                continue
            hits = len(CHECKS[check](path.read_text(encoding="utf-8")))
            if hits < allowed:
                stale.append(
                    f"{check}/{slug}: now {hits} (entry says {allowed}) — "
                    + ("delete the entry" if hits == 0 else f"lower it to {hits}")
                )
    assert not stale, "skill prose allowlist is stale:\n  " + "\n  ".join(stale)


def test_skill_prose_allowlist_counts_positive() -> None:
    for check, entries in _ALLOWLIST.items():
        for slug, allowed in entries.items():
            assert allowed > 0, f"{check}/{slug}: zero-count entry — delete it"


def test_bare_noun_h2s_advisory() -> None:
    """Advisory only: H2s should read as goals ("Drill into a section of a
    paper"), not nominalisations ("## Search"). Counted, never failed."""
    bare: list[str] = []
    for path in _skill_files():
        for _n, line, fenced in _walk(path.read_text(encoding="utf-8")):
            if fenced:
                continue
            m = _H2.match(line)
            if not m:
                continue
            title = m.group(1).strip()
            if title.lower() in _BARE_H2_EXEMPT:
                continue
            if len(title.split()) <= _BARE_H2_MAX_WORDS:
                bare.append(f"{_key(path)}: {title}")
    if bare:
        warnings.warn(
            f"{len(bare)} bare-noun H2(s) across skills (advisory; goal-voice "
            f"per docs/conventions/skill-authoring-style.md): "
            + "; ".join(bare[:12])
            + ("; …" if len(bare) > 12 else ""),
            stacklevel=1,
        )


# ── the checks themselves, on synthetic text ─────────────────────────────

_SYNTH = """---
id: x
summary: ADR 0001 lives in frontmatter and is stripped at serve time
---
# x

See ADR 0042 and ADR-0043.
Read docs/backlog/foo.md for the plan.

```python
get(kind='md', id='docs/backlog/foo.md')
get(kind='paper', id='<slug>')
```

    put(kind='figure', id='m')   # indented block, no fence

Set PRECIS_FOO=1 then run `precis worker --only x`; UPDATE chunks SET x=1.

## a
## b
## c
## d
## e
body

## f
## g
## h
## i
## j

```python
get(kind='x')
```

## k
## l
"""


def test_adr_check_skips_frontmatter() -> None:
    assert _check_adr(_SYNTH) == [7]


def test_backlog_check_allows_fenced_md_get_only() -> None:
    assert _check_backlog(_SYNTH) == [8]


def test_operator_check_matches_env_cli_and_sql_once_per_line() -> None:
    assert _check_operator(_SYNTH) == [17]


def test_unfenced_verb_check_ignores_fenced_and_flags_indented() -> None:
    assert _check_unfenced_verb(_SYNTH) == [15]


def test_alias_overrun_counts_runs_and_treats_fences_as_body() -> None:
    # a..e = 5 (overrun), f..j = 5 ended by a fence (overrun), k..l = 2 (fine)
    assert _check_alias_overrun(_SYNTH) == [19, 26]


def test_alias_overrun_exactly_max_is_fine() -> None:
    text = "# t\n\n" + "".join(f"## h{i}\n" for i in range(ALIAS_MAX)) + "body\n"
    assert _check_alias_overrun(text) == []
