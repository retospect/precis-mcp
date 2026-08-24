"""scripts/lib/mutate_driver.py — the budgeted, diff-only mutation pass.

Runs INSIDE the precis-dev container (cwd ``/app``, the worktree bind mount),
driven by ``scripts/mutate-diff``. Mutates ONLY lines a given commit changed
that are also covered by a test (per ``.coverage``'s ``--cov-context=test``
data, written by ``scripts/ship --mutate``), and for each mutant runs ONLY the
tests that covered that line — never the whole suite. Advisory: it always
exits 0 (survivors are a to-do list, not a red gate) except on internal
errors (bad patch, unreadable ``.coverage``), which exit 2.

Kept as small pure functions (``parse_patch`` / ``generate_mutants`` /
``covering_tests_for_file`` / ``classify``) so ``tests/test_mutate_driver.py``
can exercise them directly — the only impure part is the execution loop,
which shells out to pytest.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import coverage

# ── patch parsing ──────────────────────────────────────────────────────────

_FILE_RE = re.compile(r"^\+\+\+ (.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> dict[str, set[int]]:
    """Map new-side path -> set of added/modified line numbers.

    Reads ``git diff -U0`` output. Only ``+++ b/<path>`` targets matching
    ``src/**/*.py`` are kept (``/dev/null`` — a pure delete of the whole
    file — is dropped). A hunk with ``+c`` and no ``,d`` covers exactly one
    line (``d`` defaults to 1); ``,0`` is a pure deletion on the new side —
    it touches no new-side lines, so it contributes nothing to mutate.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _FILE_RE.match(line)
        if m:
            path = m.group(1)
            if path == "/dev/null":
                current = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            if path.startswith("src/") and path.endswith(".py"):
                current = path
                changed.setdefault(current, set())
            else:
                current = None
            continue
        m = _HUNK_RE.match(line)
        if m and current is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            changed[current].update(range(start, start + count))
    return changed


# ── covering-test lookup ───────────────────────────────────────────────────


def strip_context(context: str) -> str:
    """``"tests/test_x.py::test_y|run"`` -> ``"tests/test_x.py::test_y"``.

    Empty-string contexts (recorded for module-import-time coverage outside
    any test) are not test ids — callers filter those separately.
    """
    return context.split("|", 1)[0]


def covering_tests_from_contexts(
    contexts_by_lineno: dict[int, list[str]], lineno: int
) -> list[str]:
    """Distinct, order-preserving test ids that covered ``lineno``."""
    seen: list[str] = []
    for ctx in contexts_by_lineno.get(lineno, []):
        test_id = strip_context(ctx)
        if test_id and test_id not in seen:
            seen.append(test_id)
    return seen


def has_any_test_context(data: coverage.CoverageData) -> bool:
    """False when the ``.coverage`` carries no per-test contexts.

    ``scripts/ship`` without ``--mutate`` still writes ``.coverage`` (for
    diff-cover) but WITHOUT ``--cov-context=test`` — every context is the
    empty string. Distinguish that case so the driver can say so instead of
    silently mutating nothing.
    """
    for measured in data.measured_files():
        for ctxs in data.contexts_by_lineno(measured).values():
            for ctx in ctxs:
                if strip_context(ctx):
                    return True
    return False


def resolve_measured_file(data: coverage.CoverageData, rel_path: str) -> str | None:
    """Match ``rel_path`` (e.g. ``src/precis/x.py``) against a measured file.

    ``relative_files=true`` means measured-file keys are already relative —
    an exact match is the common case. Tolerate a path-separator mismatch
    (measured file recorded with a different prefix) by falling back to a
    suffix match.
    """
    measured = data.measured_files()
    if rel_path in measured:
        return rel_path
    for m in measured:
        if m.replace("\\", "/").endswith(rel_path):
            return m
    return None


def covering_tests_for_file(
    data: coverage.CoverageData, rel_path: str
) -> dict[int, list[str]]:
    """``{lineno: [test_id, ...]}`` for every line ``rel_path`` executed."""
    key = resolve_measured_file(data, rel_path)
    if key is None:
        return {}
    raw = data.contexts_by_lineno(key)
    result: dict[int, list[str]] = {}
    for lineno in raw:
        tests = covering_tests_from_contexts(raw, lineno)
        if tests:
            result[lineno] = tests
    return result


# ── mutant model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mutant:
    path: str
    lineno: int
    col: int
    end_col: int
    original: str
    replacement: str
    description: str


_COMPARE_FLIP = {
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", ">="),
    ast.GtE: (">=", "<"),
    ast.Gt: (">", "<="),
    ast.LtE: ("<=", ">"),
    ast.Is: ("is", "is not"),
    ast.IsNot: ("is not", "is"),
    ast.In: ("in", "not in"),
    ast.NotIn: ("not in", "in"),
}


def _single_line_span(node: ast.AST) -> tuple[int, int, int] | None:
    """``(lineno, col_offset, end_col_offset)`` iff ``node`` fits one line."""
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    if lineno is None or end_lineno is None or col is None or end_col is None:
        return None
    if lineno != end_lineno:
        return None
    return lineno, col, end_col


def _apply(line: str, col: int, end_col: int, replacement: str) -> str:
    return line[:col] + replacement + line[end_col:]


def _is_string_or_list_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
        return True
    return isinstance(node, ast.List)


def _op_token_span(
    left: ast.expr, right: ast.expr, lines: list[str], token: str
) -> tuple[int, int, int] | None:
    """Locate ``token`` in the gap between ``left`` and ``right`` (a binary
    operator's own AST node — ``ast.cmpop``/``ast.operator``/``ast.boolop`` —
    carries NO position info in the stdlib grammar, so the operator token has
    to be found textually between its two operands instead).
    """
    left_span = _single_line_span(left)
    right_span = _single_line_span(right)
    if left_span is None or right_span is None:
        return None
    l_lineno, _, left_end = left_span
    r_lineno, right_start, _ = right_span
    if l_lineno != r_lineno or left_end > right_start:
        return None
    line = lines[l_lineno - 1]
    segment = line[left_end:right_start]
    idx = segment.find(token)
    if idx == -1:
        return None
    col = left_end + idx
    end_col = col + len(token)
    return l_lineno, col, end_col


def _mutants_for_node(node: ast.AST, lines: list[str], path: str) -> list[Mutant]:
    out: list[Mutant] = []

    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
    ):
        flip = _COMPARE_FLIP.get(type(node.ops[0]))
        if flip is not None:
            orig_tok, new_tok = flip
            span = _op_token_span(node.left, node.comparators[0], lines, orig_tok)
            if span is not None:
                lineno, col, end_col = span
                out.append(
                    Mutant(
                        path,
                        lineno,
                        col,
                        end_col,
                        orig_tok,
                        new_tok,
                        f"compare {orig_tok} -> {new_tok}",
                    )
                )

    elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
        span = _single_line_span(node)
        if span is not None:
            lineno, col, end_col = span
            op_name = "and" if isinstance(node.op, ast.And) else "or"
            new_op_name = "or" if op_name == "and" else "and"
            new_node = ast.BoolOp(
                op=(ast.Or() if isinstance(node.op, ast.And) else ast.And()),
                values=node.values,
            )
            replacement = ast.unparse(new_node)
            line = lines[lineno - 1]
            out.append(
                Mutant(
                    path,
                    lineno,
                    col,
                    end_col,
                    line[col:end_col],
                    replacement,
                    f"boolop {op_name} -> {new_op_name}",
                )
            )

    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
        if not (
            _is_string_or_list_literal(node.left)
            or _is_string_or_list_literal(node.right)
        ):
            orig_tok = "+" if isinstance(node.op, ast.Add) else "-"
            new_tok = "-" if orig_tok == "+" else "+"
            span = _op_token_span(node.left, node.right, lines, orig_tok)
            if span is not None:
                lineno, col, end_col = span
                out.append(
                    Mutant(
                        path,
                        lineno,
                        col,
                        end_col,
                        orig_tok,
                        new_tok,
                        f"arith {orig_tok} -> {new_tok}",
                    )
                )

    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        span = _single_line_span(node)
        operand_span = _single_line_span(node.operand)
        if span is not None and operand_span is not None:
            lineno, col, _ = span
            _, op_col, _ = operand_span
            if op_col > col:
                out.append(
                    Mutant(
                        path,
                        lineno,
                        col,
                        op_col,
                        lines[lineno - 1][col:op_col],
                        "",
                        "unary: remove not",
                    )
                )

    elif isinstance(node, ast.Constant):
        span = _single_line_span(node)
        if span is not None:
            lineno, col, end_col = span
            line = lines[lineno - 1]
            if isinstance(node.value, bool):
                new_tok = "False" if node.value else "True"
                out.append(
                    Mutant(
                        path,
                        lineno,
                        col,
                        end_col,
                        str(node.value),
                        new_tok,
                        f"const {node.value} -> {new_tok}",
                    )
                )
            elif isinstance(node.value, int):
                out.append(
                    Mutant(
                        path,
                        lineno,
                        col,
                        end_col,
                        line[col:end_col],
                        str(node.value + 1),
                        f"const {node.value} -> {node.value + 1}",
                    )
                )

    elif isinstance(node, ast.Break | ast.Continue):
        span = _single_line_span(node)
        if span is not None:
            lineno, col, end_col = span
            is_break = isinstance(node, ast.Break)
            new_tok = "continue" if is_break else "break"
            out.append(
                Mutant(
                    path,
                    lineno,
                    col,
                    end_col,
                    "break" if is_break else "continue",
                    new_tok,
                    f"{'break' if is_break else 'continue'} -> {new_tok}",
                )
            )

    return out


def apply_mutant(source: str, mutant: Mutant) -> str:
    """Return ``source`` with just ``mutant``'s single line rewritten."""
    lines = source.splitlines(keepends=True)
    idx = mutant.lineno - 1
    raw = lines[idx]
    newline = ""
    body = raw
    if body.endswith("\r\n"):
        newline = "\r\n"
        body = body[:-2]
    elif body.endswith("\n"):
        newline = "\n"
        body = body[:-1]
    lines[idx] = _apply(body, mutant.col, mutant.end_col, mutant.replacement) + newline
    return "".join(lines)


def generate_mutants(
    source: str, changed_lines: set[int], path: str = "<source>"
) -> list[Mutant]:
    """All mutants on ``changed_lines`` of ``source`` that still compile."""
    tree = ast.parse(source)
    lines = source.splitlines()
    candidates: list[Mutant] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or lineno not in changed_lines:
            continue
        candidates.extend(_mutants_for_node(node, lines, path))

    out: list[Mutant] = []
    for m in candidates:
        mutated = apply_mutant(source, m)
        try:
            compile(mutated, path, "exec")
        except SyntaxError:
            continue
        out.append(m)
    return out


# ── selection ────────────────────────────────────────────────────────────


def select_mutants(by_file: dict[str, list[Mutant]], max_mutants: int) -> list[Mutant]:
    """Round-robin across files (sorted), each file's mutants sorted by
    (lineno, description), capped at ``max_mutants``. Deterministic — no
    file starves the others, and re-running with the same inputs picks the
    same mutants.
    """
    files = sorted(by_file.keys())
    queues = [
        sorted(by_file[f], key=lambda m: (m.lineno, m.description)) for f in files
    ]

    selected: list[Mutant] = []
    i = 0
    while queues and len(selected) < max_mutants:
        idx = i % len(queues)
        q = queues[idx]
        selected.append(q.pop(0))
        if not q:
            queues.pop(idx)
            continue  # index idx now names the next queue — don't advance i
        i += 1
    return selected


# ── classification ─────────────────────────────────────────────────────────


def classify(rc: int, *, timed_out: bool = False) -> str:
    """Map a pytest return code (or timeout) to KILLED / SURVIVED / SKIPPED.

    0 = every selected test passed against the mutant unnoticed -> SURVIVED.
    1 = a test failed -> the mutant was caught -> KILLED. A timeout is
    treated the same as rc==1 (an infinite loop IS a detected behaviour
    change). Any other rc (2/4 usage errors, 5 no tests collected — e.g. a
    renamed test id) is inconclusive -> SKIPPED.
    """
    if timed_out or rc == 1:
        return "KILLED"
    if rc == 0:
        return "SURVIVED"
    return "SKIPPED"


# ── CLI / execution ─────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--patch", required=True, type=Path)
    p.add_argument("--coverage", default=Path(".coverage"), type=Path)
    p.add_argument("--max-mutants", default=20, type=int)
    p.add_argument("--budget", default=600, type=int)
    p.add_argument("--per-mutant-timeout", default=120, type=int)
    p.add_argument("--max-tests", default=5, type=int)
    p.add_argument("--dry-run", action="store_true")
    return p


def _plan(
    args: argparse.Namespace,
) -> tuple[dict[str, list[Mutant]], dict[tuple[str, int], list[str]]] | int:
    """Build ``(mutants-by-file, line -> covering tests)``, or an int exit
    code on an internal error (bad patch file / unreadable coverage db).
    """
    try:
        patch_text = args.patch.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"mutate_driver: cannot read patch {args.patch}: {exc}", file=sys.stderr)
        return 2

    changed = parse_patch(patch_text)
    if not changed:
        return {}, {}

    if not args.coverage.exists():
        print(
            f"mutate_driver: coverage file not found: {args.coverage}", file=sys.stderr
        )
        return 2

    data = coverage.CoverageData(basename=str(args.coverage))
    try:
        data.read()
    except Exception as exc:  # coverage's own reader exceptions
        print(
            f"mutate_driver: cannot read coverage db {args.coverage}: {exc}",
            file=sys.stderr,
        )
        return 2

    if not has_any_test_context(data):
        print(
            "note: .coverage has no per-test contexts (needs scripts/ship --mutate) "
            "— nothing to mutate against, skipping."
        )
        return {}, {}

    by_file: dict[str, list[Mutant]] = {}
    line_tests: dict[tuple[str, int], list[str]] = {}
    for rel_path, lines in sorted(changed.items()):
        src_path = Path(rel_path)
        if not src_path.exists():
            continue
        covering = covering_tests_for_file(data, rel_path)
        covered_lines = {ln for ln in lines if covering.get(ln)}
        if not covered_lines:
            continue
        source = src_path.read_text(encoding="utf-8")
        try:
            mutants = generate_mutants(source, covered_lines, path=rel_path)
        except SyntaxError:
            continue
        if not mutants:
            continue
        by_file[rel_path] = mutants
        for m in mutants:
            line_tests[(rel_path, m.lineno)] = covering[m.lineno]

    return by_file, line_tests


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    planned = _plan(args)
    if isinstance(planned, int):
        return planned
    by_file, line_tests = planned

    total_generated = sum(len(v) for v in by_file.values())
    selected = select_mutants(by_file, args.max_mutants)

    if args.dry_run:
        for m in selected:
            tests = line_tests.get((m.path, m.lineno), [])[: args.max_tests]
            print(f"PLANNED {m.path}:{m.lineno}  {m.description}  tests={tests}")
        print(
            f"mutation-summary: total={total_generated} run=0 killed=0 survived=0 skipped=0 elapsed=0s"
        )
        return 0

    start = time.monotonic()
    run = killed = survived = skipped = 0

    for idx, m in enumerate(selected):
        if time.monotonic() - start > args.budget:
            remaining = len(selected) - idx
            skipped += remaining
            print(f"SKIPPED  budget exhausted — {remaining} mutant(s) not run")
            break

        tests = line_tests.get((m.path, m.lineno), [])[: args.max_tests]
        if not tests:
            skipped += 1
            print(f"SKIPPED  {m.path}:{m.lineno}  {m.description}  (no covering tests)")
            continue

        src_path = Path(m.path)
        original_bytes = src_path.read_bytes()
        run += 1
        try:
            mutated = apply_mutant(original_bytes.decode("utf-8"), m)
            src_path.write_text(mutated, encoding="utf-8")

            timed_out = False
            rc = 0
            tail = ""
            try:
                proc = subprocess.run(
                    [
                        "uv",
                        "run",
                        "--no-sync",
                        "pytest",
                        "-q",
                        "-x",
                        "-p",
                        "no:warnings",
                        "-n0",
                        *tests,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=args.per_mutant_timeout,
                )
                rc = proc.returncode
                tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                out = exc.stdout if isinstance(exc.stdout, str) else ""
                err = exc.stderr if isinstance(exc.stderr, str) else ""
                tail = "\n".join((out + err).splitlines()[-5:])

            verdict = classify(rc, timed_out=timed_out)
            if verdict == "KILLED":
                killed += 1
                print(f"KILLED  {m.path}:{m.lineno}  {m.description}")
            elif verdict == "SURVIVED":
                survived += 1
                print(f"SURVIVED  {m.path}:{m.lineno}  {m.description}")
                print(f"    covering tests: {', '.join(tests)}")
            else:
                skipped += 1
                print(
                    f"SKIPPED  {m.path}:{m.lineno}  {m.description}  (pytest-rc-{rc})"
                )
                if tail:
                    print(f"    {tail}")
        finally:
            src_path.write_bytes(original_bytes)

    elapsed_s = int(time.monotonic() - start)
    print(
        f"mutation-summary: total={total_generated} run={run} killed={killed} "
        f"survived={survived} skipped={skipped} elapsed={elapsed_s}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
