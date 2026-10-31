"""Ratchet: the two bash 3.2 (stock macOS) traps — gr515242, gr515243.

``scripts/*`` must run on the bash 3.2 macOS ships (``scripts/test``
header). Two expansions that are safe on bash >=4.4 but misbehave on 3.2:

1. ``"${arr[@]}"`` / ``"${arr[*]}"`` on an EMPTY array under ``set -u``
   dies with ``unbound variable`` — the loop or exec never runs. Safe
   forms: ``${arr[@]+"${arr[@]}"}`` (the repo idiom), ``${arr[@]:-x}``,
   ``${arr[@]:0:n}`` slices, ``${#arr[@]}``, and any expansion reached
   only under a ``${#arr[@]}`` non-empty check. This test flags a bare
   ``${name[@]}``/``${name[*]}`` expansion in a file that gives ``name``
   no non-empty literal ``name=(...)`` initialiser — i.e. an array that
   can be empty at expansion — unless the site is allowlisted below as
   audited-safe (count check, early exit, or a producer that guarantees
   at least one element).

2. ``IFS=$'\\t' read -r a b ...`` with two or more vars collapses a run
   of tabs into one delimiter (tab is IFS whitespace), so a producer's
   empty middle column — for-each-ref's ``%(worktreepath)``, an unset awk
   field — shifts every later field left silently. Fix by emitting a
   non-whitespace delimiter (``\\x1f`` in bash, ``\\037`` octal in awk;
   ``scripts/inflight`` and ``scripts/memory-lint`` do this) or by
   splitting with parameter expansion. Flagged unless allowlisted as
   audited (producer can never emit an empty middle field, or fills
   empties with ``-``/``?``).

Either failure means a NEW unsafe site landed: fix it, or add a
``(file, count)`` allowlist entry after auditing every field/expansion
for the empty case. Convention: docs/conventions/container-ops.md
"bash 3.2 gotchas".

gr513892 adds the third ratchet: bash >=4 constructs that fail LOUDLY on
3.2 (``declare -A``, ``mapfile``, ``${x,,}``, ``&>``, ``|&``, ``coproc``,
``globstar``, negative array indexes, namerefs) — the two above fail
silently, these just die with ``command not found``/``bad substitution``.
A ``bash -n`` parse over every bash-shebang script comes along: a syntax
error ships red anyway, but running the check on the STOCK bash matters
when the file is fine on 5.x. Python files are skipped — a ``.py`` under
scripts/ never sees a bash parser.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: parses and runs bash scripts"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"

#: ``name=(`` — literal init (safe unless it's the empty ``name=()``).
_LITERAL_INIT = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)=\(")
_EMPTY_INIT = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)=\(\)")
#: A bare ``${name[@]}``/``${name[*]}`` — not sliced, defaulted or guarded.
_BARE_EXPAND = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\[[@*]\]\}")
#: ``IFS=$'\t' read [-flags] var...``
_TAB_READ = re.compile(r"IFS=\$?'\\t'\s+read(?:\s+-[a-zA-Z]+)*\s+(.+)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

#: Audited-safe bare ``${name[@]}``/``${name[*]}`` sites, as
#: ``(file, array name) -> expected count``. Every entry is reachable only
#: with a non-empty array: inside a ``${#name[@]}`` guard, past an
#: early-exit on empty, or fed by a producer that always yields >=1
#: element. A new bare expansion of a maybe-empty array fails here.
_BARE_ALLOW: dict[tuple[str, str], int] = {
    ("scripts/cluster-ping", "WANT"): 2,  # both under ${#WANT[@]} > 0
    ("scripts/deploy", "SKIPPED_HOSTS"): 1,  # under ${SKIPPED_HOSTS[@]+} guard
    ("scripts/deploy", "_skip_hosts"): 1,  # read -a on a non-empty value
    ("scripts/fleet", "named"): 1,  # under ${#named[@]} > 0
    ("scripts/inflight", "REMOVABLE"): 1,  # under ${#REMOVABLE[@]} > 0
    ("scripts/lib/ship-subject.sh", "shas"): 1,  # case ${#shas[@]}: 0/1 handled
    ("scripts/lib/ship-subject.sh", "subjects"): 1,  # built one per sha, >=2
    ("scripts/memory-lint", "findings"): 1,  # under ${#findings[@]}
    ("scripts/memory-lint", "reasons"): 1,  # under ${#reasons[@]}
    ("scripts/memory-lint", "revs"): 1,  # read -a on a non-empty field
    ("scripts/memory-lint", "sib_findings"): 2,  # under ${#sib_findings[@]}
    ("scripts/memory-lint", "suspects"): 1,  # under ${#suspects[@]}
    ("scripts/memory-lint", "topic_files"): 1,  # under ${#topic_files[@]}
    ("scripts/mine-sessions/run.sh", "SOURCES"): 1,  # defaulted non-empty
    ("scripts/nightly", "suspects"): 2,  # under ${#suspects[@]}
    ("scripts/reap-test-dbs", "REAPED"): 2,  # under ${#REAPED[@]}
    ("scripts/reap-test-dbs", "STOPPED"): 2,  # under ${#STOPPED[@]}
    ("scripts/reap-worktrees", "REAPED"): 2,  # under ${#REAPED[@]}
    ("scripts/reap-worktrees", "deleted"): 2,  # under ${#deleted[@]}
    ("scripts/render-uml", "container_paths"): 1,  # one per puml file
    ("scripts/render-uml", "puml_files"): 2,  # past the -eq 0 early exit
    ("scripts/ship", "fix"): 1,  # under ${#fix[@]}
    ("scripts/ship", "other"): 1,  # under ${#other[@]}
}

#: Audited multi-var ``IFS=$'\t' read`` sites, ``file -> expected count``.
#: Each producer either can never emit an empty middle field or fills
#: empties (awk ``-``/``?``) before bash sees them.
_TAB_READ_ALLOW: dict[str, int] = {
    "scripts/deploy": 2,  # _role_parents/role_map emit non-empty fields
    "scripts/fleet": 3,  # threads()/st_line() printf guaranteed fields
    "scripts/hooks/session-end-reap.sh": 1,  # path filtered non-empty; branch last
    "scripts/inflight": 1,  # task markers: cwd required, pid last
    "scripts/memory-lint": 6,  # awk fills empty columns with -/?
    "scripts/reap-test-dbs": 1,  # task markers: cwd required, pid last
    "scripts/reap-worktrees": 6,  # JSON rows: middle fields non-empty or last
}


def _strip_comment(line: str) -> str:
    """Cut a ``#`` comment — a hash at col 0 or preceded by whitespace.

    ``${x#y}`` pattern-removal survives (its ``#`` follows ``{``/``]``),
    as does a ``#`` inside a word.
    """
    m = re.search(r"(^|\s)#", line)
    return line[: m.start()] if m else line


def _script_files() -> list[Path]:
    return [p for p in sorted(_SCRIPTS.rglob("*")) if p.is_file()]


def _code(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""
    return "\n".join(_strip_comment(line) for line in text.splitlines())


def test_no_new_bare_empty_array_expansion() -> None:
    flagged: Counter[tuple[str, str]] = Counter()
    for path in _script_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        text = _code(path)
        literal = {m.group(1) for m in _LITERAL_INIT.finditer(text)}
        literal -= {m.group(1) for m in _EMPTY_INIT.finditer(text)}
        for m in _BARE_EXPAND.finditer(text):
            name = m.group(1)
            if name in literal:
                continue
            # skip the inner expansion of the ${arr[@]+"${arr[@]}"} idiom
            if text[max(0, m.start() - 2) : m.start()].endswith('+"'):
                continue
            flagged[(rel, name)] += 1
    new = {k: n for k, n in flagged.items() if n != _BARE_ALLOW.get(k)}
    assert not new, (
        "bare ${arr[@]}/${arr[*]} expansion(s) of arrays with no non-empty "
        "literal init — under bash 3.2 + set -u an empty array dies with "
        "'unbound variable'. Guard with ${arr[@]+\"${arr[@]}\"} or audit "
        "and allowlist in tests/test_bash32_gotchas.py: " + str(new)
    )


#: bash >=4 constructs that must not appear anywhere under scripts/ —
#: pattern -> the 3.2-safe shape to write instead (gr513892).
_BASH4_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(?:declare|typeset|local|export|readonly)\s+-[A-Za-z]*A\b"),
        "declare -A (assoc array) — use parallel plain arrays or a delimited string",
    ),
    (
        re.compile(r"\b(?:declare|typeset|local|readonly)\s+-n\b"),
        "nameref (declare/local -n) — use indirect expansion ${!var}",
    ),
    (
        re.compile(r"\b(?:mapfile|readarray)\b"),
        'mapfile/readarray — while IFS= read -r x; do arr+=("$x"); done < <(…)',
    ),
    (
        re.compile(r"\$\{[^}]*,,|\$\{[^}]*\^\^|\$\{[A-Za-z_][A-Za-z0-9_]*[,^]\}"),
        "${x,,}/${x^^}/${x,}/${x^} case ops — use tr or printf",
    ),
    (
        re.compile(r"\$\{[^}\[]*\[\s*-[0-9]"),
        "negative array index ${a[-1]} — use ${a[${#a[@]}-1]}",
    ),
    (
        re.compile(r"(?<![\w|&])&>{1,2}(?![=&])"),
        "&> combined redirect — use >file 2>&1",
    ),
    (
        re.compile(r"(?<![\w|&])\|&(?![=&|;])"),
        "|& stderr pipe — use 2>&1 |",
    ),
    (re.compile(r"\bwait\s+-n\b"), "wait -n — poll 'jobs -p' instead"),
    (re.compile(r"\bcoproc\b"), "coproc — use a fifo + background process"),
    (re.compile(r"\bglobstar\b"), "shopt -s globstar (**) — use find"),
]

#: Audited bash-4 sites, ``file -> expected total count`` — same allowlist
#: convention as the two ratchets above. Empty today: keep it that way.
_BASH4_ALLOW: dict[str, int] = {}


def test_no_bash4_constructs() -> None:
    """``scripts/test`` and every other shell entry point must run on the
    bash 3.2 macOS ships (gr513892) — flag bash >=4 syntax anywhere under
    scripts/ except .py files, which never meet a bash parser."""
    flagged: Counter[str] = Counter()
    for path in _script_files():
        if path.suffix == ".py":
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        for line in text.splitlines():
            code = _strip_comment(line)
            for rx, _hint in _BASH4_PATTERNS:
                if rx.search(code):
                    flagged[rel] += 1
    new = {k: n for k, n in flagged.items() if n != _BASH4_ALLOW.get(k)}
    assert not new, (
        "bash >=4 construct(s) under scripts/ — macOS /bin/bash is 3.2 "
        "and these fail loudly there; rewrite 3.2-safe (the "
        "_BASH4_PATTERNS table in tests/test_bash32_gotchas.py names the "
        "safe shape per construct) or audit and allowlist there: " + str(new)
    )


def test_bash_scripts_parse_under_bash_n() -> None:
    """Every bash-shebang script under scripts/ passes ``bash -n`` — the
    floor is only meaningful if the files actually parse."""
    bad: list[str] = []
    for path in _script_files():
        with path.open("rb") as f:
            head = f.read(200)
        if not (head.startswith(b"#!") and b"bash" in head.split(b"\n", 1)[0]):
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if (
            subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, check=False
            ).returncode
            != 0
        ):
            bad.append(rel)
    assert not bad, f"bash -n fails under scripts/: {bad}"


def test_no_new_multi_var_tab_read() -> None:
    flagged: Counter[str] = Counter()
    for path in _script_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        text = _code(path)
        for m in _TAB_READ.finditer(text):
            nvars = sum(1 for v in m.group(1).split() if _NAME.fullmatch(v))
            if nvars >= 2:
                flagged[rel] += 1
    new = {k: n for k, n in flagged.items() if n != _TAB_READ_ALLOW.get(k)}
    assert not new, (
        "multi-var IFS=$'\\t' read site(s) — tab is IFS whitespace, so an "
        "empty middle column shifts later fields left. Emit \\x1f/\\037 or "
        "split with parameter expansion, then allowlist in "
        "tests/test_bash32_gotchas.py: " + str(new)
    )
