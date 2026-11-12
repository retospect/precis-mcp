"""Bash launch sites must go through admission (gr647996).

The bash half of ``tests/test_launch_admission_ratchet.py``: the dev/slice
host's gates start the heaviest load (``docker compose run`` pytest/mypy
containers, warm gate stacks, ssh fan-out, backgrounded jobs). A bash
launch goes through ``precis.workers.admission`` by calling
``admit_acquire`` first.

Scanned: every bash/sh file under ``scripts/`` (``scripts/lib/``,
``scripts/hooks/``, … included) — a ``bash``/``sh`` shebang or a ``.sh``
suffix. Per line, outside whole-line comments, heredoc bodies, quoted
strings (a ``$(…)`` inside quotes is code again) and ``echo``/``printf``
messages, a *launch site* is the following, also inside the script of a
``sh -c '…'``/``bash -c '…'``:

* ``docker``/``podman`` ``run|create|start|exec|build``;
* ``docker compose … up|run|exec|create|start|build`` (also
  ``docker-compose``), and the same verbs on a local ``compose`` wrapper
  function (``compose() { … docker compose … "$@"; }``);
* ``ssh <host> …`` (not ``ssh-agent``/``ssh-keygen``/``ssh-add``);
* a backgrounded job: a command line ending in a single ``&``, or one
  started with ``nohup``/``setsid``.

Teardown and inspection verbs (``down``, ``rm``, ``ps``, ``inspect``,
``logs``, ``stats``, …) never match. A hit is admitted, and not a site,
when ``admit_acquire`` ran in command position (line start, or after
``;``/``&&``/``||``/``then``/``do``/``if``/…) earlier in the same function
(or top-level flow) — ``echo admit_acquire`` or ``command -v admit_acquire``
does not count — or the line (or the one above) carries
``# admission: exempt — <reason>``.

Today's sites are frozen in ``tests/data/bash_launch_admission_baseline.txt``
as ``path::function count`` (``<top>`` for top-level flow): function-level
keys, so edits elsewhere do not churn it. The test fails when a key appears
or grows (route the launch through admission or justify the entry), and when
one shrinks or vanishes (lower the baseline: it only shrinks).

Regenerate on purpose: ``uv run python tests/test_bash_launch_admission.py
--regen`` (refuses to grow unless ``--allow-growth``); ``--list`` prints each
site with its line.
"""

from __future__ import annotations

import functools
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASELINE = (
    Path(__file__).resolve().parent / "data" / "bash_launch_admission_baseline.txt"
)
_TREES = ("scripts",)
_SKIP = {".venv", "node_modules", "__pycache__"}

#: Verbs that start load; anything else (``down``, ``rm``, ``ps``,
#: ``inspect``, ``logs``, ``stats``, ``pull``, …) is teardown/inspection.
_LOAD_VERBS = frozenset({"run", "create", "start", "exec", "build", "up"})
#: Global flags that take a separate value, per CLI layer.
_DOCKER_VALUE_FLAGS = frozenset(
    {"-c", "--context", "-H", "--host", "-l", "--log-level", "--config"}
)
_COMPOSE_VALUE_FLAGS = frozenset(
    {
        "-f",
        "--file",
        "-p",
        "--project-name",
        "--profile",
        "--env-file",
        "--project-directory",
        "--ansi",
        "--progress",
        "--parallel",
    }
)
#: A container CLI word in command position (not a path, var or suffix).
_CLI_WORD = re.compile(r"(?<![\w./$-])(docker-compose|docker|podman)(?![\w.-])")
_SSH = re.compile(r"(?<![\w./$-])ssh(?![\w.-])\s+\S")
_NOHUP = re.compile(r"(?<![\w./$-])(?:nohup|setsid)\s+\S")
#: A single ``&`` (not ``&&``, ``>&``, ``|&``, ``&>``) ending the command
#: or followed by ``pid=$!``.
_BACKGROUND = re.compile(r"(?<![&>|])&(?![&>])\s*(?:$|\w+=\$!)")
#: ``admit_acquire`` as the command itself, never as an argument.
_ADMIT = re.compile(
    r"(?:^|;|&&|\|\||[({!]|\b(?:then|do|else|if|while|until|time))\s*"
    r"admit_acquire(?![\w-])"
)
#: ``sh -c '<script>'`` / ``bash -lc "<script>"``: the script is code.
_SH_C = re.compile(
    r"(?<![\w./$-])(?:ba)?sh\s+(?:-\w+\s+)*-\w*c\s+(['\"])(.*?)(?<!\\)\1"
)
_EXEMPT = re.compile(r"#\s*admission:\s*exempt\s*[—:-]+\s*\S")
_MESSAGE = re.compile(r"^\s*(?:echo|printf|say|warn|die|note|info|log)\b")
_FUNC = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][\w:.-]*)\s*\(\)\s*\{?(.*)$")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
#: A wrapper function body: ``docker [compose …] … "$@"``.
_WRAPPER = re.compile(r"(?<![\w./$-])(docker-compose|docker|podman)\b(.*)\"\$@\"")


@dataclass(frozen=True)
class Site:
    key: str
    line: int
    kind: str


def _verb(tokens: list[str], value_flags: frozenset[str]) -> int | None:
    """Index of the first non-flag token, skipping ``value_flags``' values."""
    skip = False
    for i, t in enumerate(tokens):
        if skip:
            skip = False
        elif t in value_flags:
            skip = True
        elif not t.startswith("-"):
            return i
    return None


def _container_kind(cli: str, rest: str) -> str | None:
    """``container``/``compose`` when ``cli rest…`` starts load, else None.
    ``cli`` is ``docker``/``podman``/``docker-compose``, or ``compose`` for
    a resolved compose wrapper."""
    tokens = [t.rstrip(";)|&") for t in rest.split()]
    if cli in ("docker", "podman"):
        i = _verb(tokens, _DOCKER_VALUE_FLAGS)
        if i is None:
            return None
        if tokens[i] != "compose":
            return "container" if tokens[i] in _LOAD_VERBS - {"up"} else None
        tokens = tokens[i + 1 :]
    i = _verb(tokens, _COMPOSE_VALUE_FLAGS)
    return "compose" if i is not None and tokens[i] in _LOAD_VERBS else None


def _quoted(code: str) -> list[bool]:
    """Per character: inside a quoted string, and not in a ``$(…)`` or
    backtick substitution nested in it (that is code again)."""
    mask = [False] * len(code)
    stack: list[str] = []
    i = 0
    while i < len(code):
        top = stack[-1] if stack else ""
        c = code[i]
        mask[i] = top in ("'", '"')
        if top == "'":
            if c == "'":
                stack.pop()
        elif c == "\\":
            if i + 1 < len(code):
                mask[i + 1] = mask[i]
            i += 1
        elif code.startswith("$(", i):
            stack.append("(")
            i += 1
        elif c == "`" and top == "`":
            stack.pop()
        elif c == "`":
            stack.append("`")
        elif top == '"':
            if c == '"':
                stack.pop()
        elif c in "'\"(":
            stack.append(c)
        elif c == ")" and top == "(":
            stack.pop()
        i += 1
    return mask


def _kinds(code: str, wrappers: dict[str, str]) -> list[str]:
    q = _quoted(code)

    def hits(rx: re.Pattern[str] | str) -> Iterator[re.Match[str]]:
        return (m for m in re.finditer(rx, code) if not q[m.start()])

    out: list[str] = []
    for m in hits(_CLI_WORD):
        kind = _container_kind(m.group(1), code[m.end() :])
        if kind:
            out.append(kind)
            break
    else:
        for name, cli in wrappers.items():
            for m in hits(rf"(?<![\w./$-]){re.escape(name)}(?![\w.-])"):
                kind = _container_kind(cli, code[m.end() :])
                if kind:
                    out.append(kind)
                    break
            if out:
                break
    if any(hits(_SSH)):
        out.append("ssh")
    if any(hits(_NOHUP)) or any(hits(_BACKGROUND)):
        out.append("background")
    for m in hits(_SH_C):
        out += [k for k in _kinds(m.group(2), wrappers) if k not in out]
    return out


def _lex(line: str, quote: str) -> tuple[str, str]:
    """``(line without its trailing comment, open quote at end of line)``.
    ``quote`` is the quote open at the start (a multi-line string);
    ``$#``, ``${#x}`` and ``a#b`` are not comments."""
    out: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and quote != "'":
            out.append(line[i : i + 2])
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t;"):
            break
        out.append(c)
        i += 1
    return "".join(out), quote


def _code_lines(lines: list[str]) -> Iterator[tuple[int, str, str, str]]:
    """``(line no, raw text, scope, code)`` per logical line of code.

    Whole-line comments, blank lines, heredoc bodies and the inside of
    multi-line quoted strings (``python -c '…'``) are dropped; ``\\``
    continuations join into one logical line reported at its first line;
    a ``name() { … }`` header yields only its body; ``scope`` is the
    enclosing function (``<top>`` outside one). A function closes at a
    ``}`` with the header's indent — the repo's style."""
    func: tuple[str, str] | None = None  # (name, indent)
    heredoc: str | None = None
    quote = ""
    pending: tuple[int, str, str] | None = None  # (first no, raw, code)
    for no, raw in enumerate(lines, 1):
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if quote:
            _, quote = _lex(raw, quote)
            continue
        if pending is None and raw.lstrip().startswith("#"):
            continue
        body, one_liner = raw, False
        m = _FUNC.match(raw) if pending is None else None
        if m:
            func, body = (m.group(2), m.group(1)), m.group(3)
            if body.rstrip().endswith("}"):
                body, one_liner = body.rstrip()[:-1], True
        elif pending is None and func is not None and raw.startswith(func[1] + "}"):
            func = None
            continue
        code, quote = _lex(body, "")
        if pending is not None:
            start, raw, code = pending[0], pending[1] + "\n" + raw, pending[2] + code
            pending = None
        else:
            start = no
        if code.rstrip().endswith("\\") and not quote:
            pending = (start, raw, code.rstrip()[:-1] + " ")
            continue
        hd = _HEREDOC.search(code)
        if hd and "<<<" not in code and "((" not in code:
            heredoc = hd.group(2)
        if code.strip():
            yield start, raw, func[0] if func else "<top>", code
        if one_liner:
            func = None


def _wrappers(lines: list[str]) -> dict[str, str]:
    """Local functions that forward ``"$@"`` to a container CLI:
    ``name -> 'compose'`` (``docker compose … "$@"``) or ``'docker'``."""
    out: dict[str, str] = {}
    for _, _, scope, code in _code_lines(lines):
        w = _WRAPPER.search(code)
        if w and scope != "<top>":
            compose = w.group(1) == "docker-compose" or re.search(
                r"(?<![\w-])compose(?![\w.-])", w.group(2)
            )
            out[scope] = "compose" if compose else w.group(1)
    return out


def scan_source(rel: str, text: str) -> list[Site]:
    sites: list[Site] = []
    lines = text.splitlines()
    wrappers = _wrappers(lines)
    admitted: set[str] = set()
    for no, raw, scope, code in _code_lines(lines):
        if "admit_acquire" in code:
            q = _quoted(code)
            if any(not q[m.end() - 13] for m in _ADMIT.finditer(code)):
                admitted.add(scope)
        above = lines[no - 2] if no > 1 else ""
        if (
            _MESSAGE.match(code)
            or scope in admitted
            or _EXEMPT.search(raw)
            or (above.lstrip().startswith("#") and _EXEMPT.search(above))
        ):
            continue
        sites += [Site(f"{rel}::{scope}", no, k) for k in _kinds(code, wrappers)]
    return sites


def _is_shell(p: Path) -> bool:
    if p.suffix == ".sh":
        return True
    if p.suffix:
        return False
    try:
        with p.open(encoding="utf-8") as fh:
            first = fh.readline()
    except (OSError, UnicodeDecodeError):
        return False
    return first.startswith("#!") and bool(re.search(r"\b(?:ba)?sh\b", first))


def _files(root: Path, trees: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for tree in trees:
        base = root / tree
        if base.is_dir():
            out += [
                p
                for p in base.rglob("*")
                if p.is_file()
                and not _SKIP & set(p.relative_to(root).parts)
                and _is_shell(p)
            ]
    return sorted(out)


def scan_sites(root: Path = ROOT, trees: Iterable[str] = _TREES) -> list[Site]:
    sites: list[Site] = []
    for p in _files(root, trees):
        rel = p.relative_to(root).as_posix()
        sites += scan_source(rel, p.read_text(encoding="utf-8", errors="replace"))
    return sites


def scan(root: Path = ROOT, trees: Iterable[str] = _TREES) -> dict[str, int]:
    return dict(sorted(Counter(s.key for s in scan_sites(root, trees)).items()))


@functools.cache
def _scan_repo() -> tuple[tuple[str, int], ...]:
    return tuple(scan().items())


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, n = line.rpartition(" ")
            out[key] = int(n)
    return out


def ratchet_problems(
    now: dict[str, int], base: dict[str, int]
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """``(grown, stale)`` as ``key -> (baseline, now)``."""
    grown = {k: (base.get(k, 0), n) for k, n in now.items() if n > base.get(k, 0)}
    stale = {k: (n, now.get(k, 0)) for k, n in base.items() if now.get(k, 0) < n}
    return grown, stale


def assert_ratchet(now: dict[str, int], base: dict[str, int]) -> None:
    grown, stale = ratchet_problems(now, base)
    assert not grown, (
        "new bash launch site(s) outside admission (baseline -> now): "
        f"{grown}. Route the launch through precis.workers.admission: call "
        "admit_acquire first, or mark the line "
        "'# admission: exempt — <reason>'; or justify adding a baseline entry "
        "in review: uv run python tests/test_bash_launch_admission.py "
        "--regen --allow-growth."
    )
    assert not stale, (
        "baseline is higher than the tree (baseline -> now): "
        f"{stale}. Lower it: uv run python "
        "tests/test_bash_launch_admission.py --regen"
    )


def render_baseline(now: dict[str, int]) -> str:
    head = (
        "# Bash launch sites outside precis.workers.admission (gr647996).\n"
        "# Shrink-only ratchet: tests/test_bash_launch_admission.py.\n"
        "# Format: <path>::<function|<top>> <count>. Regenerate:\n"
        "#   uv run python tests/test_bash_launch_admission.py --regen\n"
    )
    return head + "".join(f"{k} {n}\n" for k, n in sorted(now.items()))


# -- the scanner bites ------------------------------------------------------


def _kinds_of(src: str) -> list[tuple[str, str]]:
    return [(s.key.split("::", 1)[1], s.kind) for s in scan_source("x", src)]


def test_scanner_flags_each_launch_form() -> None:
    src = (
        "#!/usr/bin/env bash\n"
        'compose() { docker compose -f "$F" -p "$P" --profile dev "$@"; }\n'
        "run_gate() {\n"
        '  compose run --rm precis-dev pytest "$@"\n'
        '  docker compose -f "$F" -p "$P" up -d --wait precis-test-db\n'
        "  docker run --rm -v x:/y img true\n"
        '  ssh "$host" uptime\n'
        "  long_job >log 2>&1 &\n"
        "  nohup other_job\n"
        "}\n"
        "podman --log-level warn build -t x .\n"
        "check() {\n"
        "  compose exec -T g uv run python -c '\n"
        "def f(): pass\n"
        "docker run inside a python string\n"
        "'\n"
        "  compose \\\n"
        "    run --rm x\n"
        "}\n"
    )
    assert _kinds_of(src) == [
        ("run_gate", "compose"),
        ("run_gate", "compose"),
        ("run_gate", "container"),
        ("run_gate", "ssh"),
        ("run_gate", "background"),
        ("run_gate", "background"),
        ("<top>", "container"),
        ("check", "compose"),
        ("check", "compose"),
    ]


def test_teardown_messages_and_admitted_lines_pass() -> None:
    src = (
        "#!/usr/bin/env bash\n"
        "# docker run in a comment\n"
        "reap() {\n"
        '  docker compose -p "$P" down -v\n'
        "  docker ps -a --filter name=x\n"
        "  docker rm -f x\n"
        '  echo "try: docker run --rm img"\n'
        "  cmd && other\n"
        "  exec 2>&1\n"
        "  ssh-agent -k\n"
        "}\n"
        "gate() {\n"
        "  admit_acquire gate 4g\n"
        "  compose run --rm precis-dev pytest\n"
        "}\n"
        "docker run img  # admission: exempt — sub-second version probe\n"
        "# admission: exempt — interactive dev shell\n"
        "ssh host\n"
        "cat <<EOF\n"
        "docker run inside a heredoc\n"
        "EOF\n"
    )
    assert _kinds_of(src) == []


def test_admit_acquire_counts_only_in_command_position() -> None:
    src = (
        "#!/usr/bin/env bash\n"
        "f() {\n"
        "  echo admit_acquire\n"
        "  command -v admit_acquire >/dev/null\n"
        "  declare -f admit_acquire >/dev/null\n"
        '  x="then admit_acquire"\n'
        "  docker run img\n"
        "}\n"
        "g() {\n"
        "  prep && admit_acquire gate 4g\n"
        "  docker run img\n"
        "}\n"
        "h() {\n"
        "  if admit_acquire gate 4g; then docker run img; fi\n"
        "}\n"
    )
    assert _kinds_of(src) == [("f", "container")]


def test_quoted_strings_are_not_sites_but_substitutions_and_sh_c_are() -> None:
    src = (
        "#!/usr/bin/env bash\n"
        "f() {\n"
        '  why="${why:+$why; }docker run timed out after ${per}s"\n'
        "  msg='ssh host failed &'\n"
        '  out="$(docker run --rm img df -Pk /)"\n'
        "  sh -c 'docker run --rm img'\n"
        '  bash -lc "ssh host uptime"\n'
        "  exec docker run img\n"
        "}\n"
    )
    assert _kinds_of(src) == [
        ("f", "container"),
        ("f", "container"),
        ("f", "ssh"),
        ("f", "container"),
    ]


def test_ratchet_bites_on_a_new_launch_script(tmp_path: Path) -> None:
    """Counter-test: a new script with a fresh ``docker run`` fails against
    the real baseline; dropping a baselined site fails as stale. The tree
    is the baseline plus the temp script (the ratchet test pins tree ==
    baseline)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "brand-new-gate").write_text(
        "#!/usr/bin/env bash\nheavy() {\n  docker run --rm precis-dev pytest\n}\n",
        encoding="utf-8",
    )
    base = load_baseline()
    with pytest.raises(AssertionError) as exc:
        assert_ratchet(base | scan(tmp_path), base)
    assert "scripts/brand-new-gate::heavy" in str(exc.value)
    assert "precis.workers.admission" in str(exc.value)

    shrunk = dict(base)
    gone = next(iter(shrunk))
    del shrunk[gone]
    assert gone in ratchet_problems(shrunk, base)[1]
    with pytest.raises(AssertionError, match="Lower it"):
        assert_ratchet(shrunk, base)


def test_baseline_names_no_hosts() -> None:
    text = BASELINE.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


# -- the ratchet ------------------------------------------------------------


def test_no_new_bash_launch_sites_outside_admission() -> None:
    assert_ratchet(dict(_scan_repo()), load_baseline())


def _main(argv: list[str]) -> None:
    if "--list" in argv:
        for s in sorted(scan_sites(), key=lambda s: (s.key, s.line)):
            print(f"{s.key}:{s.line} {s.kind}")
        return
    now = scan()
    if "--regen" not in argv:
        print(f"{sum(now.values())} sites in {len(now)} keys")
        return
    if BASELINE.exists() and "--allow-growth" not in argv:
        grown, _ = ratchet_problems(now, load_baseline())
        if grown:
            raise SystemExit(
                f"refusing to grow the baseline: {grown}; pass --allow-growth "
                "only for a reviewed, justified entry"
            )
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(render_baseline(now), encoding="utf-8")
    print(f"pinned {sum(now.values())} sites in {len(now)} keys")


if __name__ == "__main__":
    _main(sys.argv[1:])
