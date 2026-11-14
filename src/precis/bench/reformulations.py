"""Committed query reformulations for the multi-query arm (td664457).

The multi-query arm (``search`` with ``queries=`` and ``answers=``) and the
``found_by`` sweep need several phrasings of each benchmark question. A
scripted caller cannot write them, and writing them at run time would make
runs irreproducible, so a pinned model writes them once and the file is
committed. The harness reads the file; nobody needs a model to re-run.

Files
-----
``bench/tasks/<name>.queries.jsonl``, next to the task files, one line per
task. ``<name>`` is ``qasper-<set>`` for the QASPER questions and
``planted-<seed>`` for one seed's planted questions (:func:`default_name`).
A row:

``task``             the task id
``queries``          1 to :data:`MAX_QUERIES` rephrasings of the question
``answers``          0 to :data:`MAX_ANSWERS` hypothetical answer sentences
``model``            the model id the generator reported for the call
``prompt_sha256``    sha256 of the prompt template that produced the row
``question_sha256``  sha256 of the question text the row was written for

Use :func:`lookup`, not a bare id match: planted questions are generated
from a seed and the hosts a world's chunker leaves, so the same id can carry
another question in another world. A task with no row, or with a row
written for another question, runs single-query and is counted as such.

No gold leak
------------
The generator sees the question and nothing else: :func:`render_prompt`
takes the question string, never a task row, so evidence, answers and paper
text cannot reach the model. One question per call, so a paper's other
questions do not reach it either. What the model already knows is not
controlled: QASPER papers are public, so a hypothetical answer can repeat a
published fact the model learned in training. The planted questions are
about made-up entities and are the control for that.

Generating
----------
``scripts/bench questions`` writes ``{task, question}`` lines (the planted
ones need the dev DB, for the host paragraphs); ``scripts/bench reformulate``
turns them into the committed file. The transport is the logged-in Claude
CLI through :func:`precis.utils.claude_p.call_claude_p` (subscription auth,
no tools, its own short system prompt, a neutral working directory), with
the model id pinned on the command line; a call answered by another model
is refused. An existing output file is never overwritten without
``--force``. Finished rows are kept in ``<file>.partial`` while a run is in
flight, so an interrupted run resumes instead of paying twice.

The prompt template is ``bench/tasks/reformulation-prompt.txt``; its sha256
is on every row, so a prompt edit is visible in the data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

#: Most rephrasings a row carries.
MAX_QUERIES = 3
#: Most hypothetical answer sentences a row carries.
MAX_ANSWERS = 1
#: Longest rephrasing or answer sentence, in characters.
MAX_CHARS = 300
#: The generator the committed files were written with (Reto 2026-11-13:
#: Opus; a smaller model is acceptable, a larger one is not).
DEFAULT_MODEL = "claude-opus-5-5"
#: Replaces the CLI's own system prompt, so the call carries no agent persona.
SYSTEM_PROMPT = (
    "You rewrite search questions. Reply with one JSON object and nothing else."
)
#: Where the question goes in the prompt template.
PLACEHOLDER = "{question}"
#: Attempts per question before the run reports it as failed.
ATTEMPTS = 3
WORKERS = 4

ROW_KEYS = (
    "task",
    "queries",
    "answers",
    "model",
    "prompt_sha256",
    "question_sha256",
)

#: ``(prompt, model) -> (parsed reply, model id the call reported)``.
Transport = Callable[[str, str], tuple[Mapping[str, Any], str]]


class ReformulationError(ValueError):
    """A reply, a row or a file breaks the reformulation contract."""


# ---------------------------------------------------------------------------
# paths and hashes
# ---------------------------------------------------------------------------


def tasks_dir() -> Path:
    """``bench/tasks`` of the source checkout."""
    return Path(__file__).resolve().parents[3] / "bench" / "tasks"


def prompt_path() -> Path:
    return tasks_dir() / "reformulation-prompt.txt"


def default_name(set_name: str, seed: int | None = None) -> str:
    """File stem for a task set: ``qasper-<set>``, or for a planted seed
    ``planted-<seed>`` (quick, the set every planted run uses) and
    ``planted-<set>-<seed>`` otherwise."""
    if seed is None:
        return f"qasper-{set_name}"
    return f"planted-{seed}" if set_name == "quick" else f"planted-{set_name}-{seed}"


def default_path(name: str) -> Path:
    """``bench/tasks/<name>.queries.jsonl``."""
    return tasks_dir() / f"{name}.queries.jsonl"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_prompt(path: Path | None = None) -> tuple[str, str]:
    """``(template, sha256)`` of the committed prompt."""
    text = (path or prompt_path()).read_text(encoding="utf-8")
    if text.count(PLACEHOLDER) != 1:
        raise ReformulationError(
            f"the prompt template must contain {PLACEHOLDER} exactly once"
        )
    return text, sha256_text(text)


def render_prompt(template: str, question: str) -> str:
    """The prompt for one question. Takes the question string only: nothing
    else of a task row can reach the generator."""
    return template.replace(PLACEHOLDER, question.strip())


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip("?.! ")


def _strings(value: Any, what: str, lo: int, hi: int) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ReformulationError(f"{what} must be a list of strings")
    out = [" ".join(v.split()) for v in value]
    if not lo <= len(out) <= hi:
        raise ReformulationError(f"{what}: {len(out)} entries, want {lo} to {hi}")
    for s in out:
        if not s:
            raise ReformulationError(f"{what}: empty string")
        if len(s) > MAX_CHARS:
            raise ReformulationError(f"{what}: {len(s)} chars, cap {MAX_CHARS}")
    return out


def check_texts(
    data: Mapping[str, Any], question: str | None, *, exact: bool
) -> tuple[list[str], list[str]]:
    """Validated ``(queries, answers)`` of a reply or a row. ``exact`` (a
    fresh reply) wants the full counts; a stored row may carry fewer. With a
    ``question``, no rephrasing may equal it."""
    if not isinstance(data, Mapping):
        raise ReformulationError("not a JSON object")
    queries = _strings(
        data.get("queries"), "queries", MAX_QUERIES if exact else 1, MAX_QUERIES
    )
    answers = _strings(
        data.get("answers"), "answers", MAX_ANSWERS if exact else 0, MAX_ANSWERS
    )
    seen = {_norm(q) for q in queries}
    if len(seen) != len(queries):
        raise ReformulationError("queries: two rephrasings are the same")
    if question is not None and _norm(question) in seen:
        raise ReformulationError("queries: a rephrasing equals the question")
    return queries, answers


def validate_row(row: Any) -> None:
    """Raise :class:`ReformulationError` unless ``row`` is a well-formed line."""
    if not isinstance(row, dict) or set(row) != set(ROW_KEYS):
        raise ReformulationError(f"row keys must be exactly {', '.join(ROW_KEYS)}")
    for key in ("task", "model", "prompt_sha256", "question_sha256"):
        if not isinstance(row[key], str) or not row[key]:
            raise ReformulationError(f"{key} must be a non-empty string")
    for key in ("prompt_sha256", "question_sha256"):
        if len(row[key]) != 64:
            raise ReformulationError(f"{key} is not a sha256 hex digest")
    queries, answers = check_texts(row, None, exact=False)
    if queries != row["queries"] or answers != row["answers"]:
        raise ReformulationError("strings must be single-spaced, one line each")


def load(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read and validate a reformulations file: ``{task id: row}``."""
    rows: dict[str, dict[str, Any]] = {}
    text = Path(path).read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            validate_row(row)
        except (json.JSONDecodeError, ReformulationError) as exc:
            raise ReformulationError(f"{path}:{n}: {exc}") from exc
        if row["task"] in rows:
            raise ReformulationError(f"{path}:{n}: duplicate task {row['task']!r}")
        rows[row["task"]] = row
    return rows


def lookup(
    rows: Mapping[str, Mapping[str, Any]], task_id: str, question: str
) -> Mapping[str, Any] | None:
    """The row for ``task_id`` when it was written for ``question``; else
    ``None`` (the task then runs single-query)."""
    row = rows.get(task_id)
    if row is None or row["question_sha256"] != sha256_text(question.strip()):
        return None
    return row


def dump(rows: Sequence[Mapping[str, Any]]) -> str:
    """Stable JSONL rendering (one row per line, non-ASCII kept)."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def _model_matches(reported: str, requested: str) -> bool:
    return reported == requested or reported.startswith(requested + "-")


def _one(
    task: str,
    question: str,
    *,
    template: str,
    prompt_sha: str,
    model: str,
    transport: Transport,
) -> dict[str, Any]:
    prompt = render_prompt(template, question)
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            data, reported = transport(prompt, model)
            if not _model_matches(reported, model):
                raise ReformulationError(
                    f"asked for model {model!r}, the call reported {reported!r}"
                )
            queries, answers = check_texts(data, question, exact=True)
        except (ReformulationError, RuntimeError) as exc:
            last = exc
            continue
        return {
            "task": task,
            "queries": queries,
            "answers": answers,
            "model": reported,
            "prompt_sha256": prompt_sha,
            "question_sha256": sha256_text(question.strip()),
        }
    raise ReformulationError(f"{task}: {last}")


def generate(
    questions: Sequence[tuple[str, str]],
    *,
    template: str,
    model: str,
    transport: Transport,
    done: Mapping[str, Mapping[str, Any]] | None = None,
    on_row: Callable[[Mapping[str, Any]], None] | None = None,
    workers: int = WORKERS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per ``(task id, question)``, in input order, and the failures.

    A row of ``done`` is reused when it was written for the same question,
    prompt and model (the resume path); every newly generated row is handed
    to ``on_row``. A question that fails :data:`ATTEMPTS` times is reported
    in the second return value and has no row."""
    prompt_sha = sha256_text(template)
    out: dict[str, dict[str, Any]] = {}
    todo: list[tuple[str, str]] = []
    for task, question in questions:
        prior = (done or {}).get(task)
        if (
            prior is not None
            and prior["question_sha256"] == sha256_text(question.strip())
            and prior["prompt_sha256"] == prompt_sha
            and _model_matches(str(prior["model"]), model)
        ):
            out[task] = dict(prior)
        else:
            todo.append((task, question))
    failures: list[str] = []

    def work(item: tuple[str, str]) -> dict[str, Any] | str:
        try:
            return _one(
                item[0],
                item[1],
                template=template,
                prompt_sha=prompt_sha,
                model=model,
                transport=transport,
            )
        except ReformulationError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for res in pool.map(work, todo):
            if isinstance(res, str):
                failures.append(res)
                continue
            out[res["task"]] = res
            if on_row is not None:
                on_row(res)
    return [out[t] for t, _ in questions if t in out], failures


def reported_model(raw_stdout: str) -> str | None:
    """The model that wrote most of a ``claude -p`` reply, from the JSON
    envelope's ``modelUsage``; ``None`` when the envelope names none."""
    try:
        usage = json.loads(raw_stdout).get("modelUsage")
    except (ValueError, AttributeError):
        return None
    if not isinstance(usage, dict) or not usage:
        return None

    def written(name: str) -> int:
        block = usage[name]
        return int(block.get("outputTokens") or 0) if isinstance(block, dict) else 0

    return str(max(usage, key=written))


def claude_cli(prompt: str, model: str) -> tuple[Mapping[str, Any], str]:
    """The default :data:`Transport`: one ``claude -p`` call on the
    subscription login, no tools, no thinking, no project MCP servers."""
    from precis.utils.claude_p import call_claude_p

    res = call_claude_p(
        prompt,
        model=model,
        max_usd=0.50,
        timeout_s=180,
        thinking=False,
        extra_args=("--strict-mcp-config", "--system-prompt", SYSTEM_PROMPT),
    )
    return res.data, reported_model(res.raw_stdout) or ""


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def qasper_questions(set_name: str) -> list[tuple[str, str]]:
    """``(task id, question)`` of the committed QASPER task file."""
    from precis.bench import gold, qasper_tasks

    rows = gold.load_tasks(qasper_tasks.default_tasks_path(set_name))
    return [(r["id"], r["question"]) for r in rows]


def planted_questions(
    dsn: str, set_name: str, seed: int, n: int
) -> list[tuple[str, str]]:
    """``(task id, question)`` of one seed's planted tasks, built the way
    ``runner.run_cli`` builds them: hosts from the ingested set, then
    :func:`precis.bench.planted.build`. Reads the database; plants nothing."""
    import psycopg

    from precis.bench import corpus, gold, planted, planted_ingest, qasper_tasks, runner

    manifest = corpus.load_manifest(None)
    qasper_rows = gold.load_tasks(qasper_tasks.default_tasks_path(set_name))
    ids = [r["qasper_id"] for r in manifest[set_name]]
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        hosts = planted_ingest.pick_hosts(
            conn,
            runner._quick_ref_ids(conn, ids),
            protected=planted_ingest.evidence_units(qasper_rows),
            seed=seed,
        )
    built, side = planted.build(hosts, seed, n, at=runner.PLANTED_AT)
    rows = [gold.from_planted(r, side["docs"]) for r in built]
    return [(r["id"], r["question"]) for r in rows]


def read_questions(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != {"task", "question"}:
            raise ReformulationError(
                f"{path}:{n}: a questions line carries task and question only"
            )
        out.append((str(row["task"]), str(row["question"])))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scripts/bench", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    qs = sub.add_parser(
        "questions", help="write {task, question} lines for one task set"
    )
    qs.add_argument("--set", dest="set_name", default="quick")
    qs.add_argument(
        "--seed", type=int, default=None, help="planted seed (needs the dev DB)"
    )
    qs.add_argument("--planted", type=int, default=None, help="planted task count")
    qs.add_argument("--out", type=Path, default=None)
    gen = sub.add_parser(
        "reformulate", help="generate the committed reformulations file"
    )
    gen.add_argument("--questions", type=Path, required=True)
    gen.add_argument("--name", required=True, help="qasper-<set> or planted-<seed>")
    gen.add_argument("--out", type=Path, default=None)
    gen.add_argument("--model", default=DEFAULT_MODEL)
    gen.add_argument("--workers", type=int, default=WORKERS)
    gen.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    return ap


def _questions(args: argparse.Namespace) -> int:
    if args.seed is None:
        pairs = qasper_questions(args.set_name)
    else:
        from precis.bench import corpus, runner
        from precis.config import load_config

        dsn = load_config().database_url or ""
        if not dsn:
            raise ReformulationError(
                "PRECIS_DATABASE_URL is unset; run via scripts/bench"
            )
        corpus.assert_throwaway_dsn(dsn)
        pairs = planted_questions(
            dsn, args.set_name, args.seed, args.planted or runner.PLANTED_N
        )
    text = dump([{"task": t, "question": q} for t, q in pairs])
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(pairs)} questions)")
    return 0


def _reformulate(args: argparse.Namespace) -> int:
    out = (args.out or default_path(args.name)).resolve()
    if out.exists() and not args.force:
        raise ReformulationError(
            f"{out} exists; pass --force to regenerate it (the committed file "
            "is what every run reads)"
        )
    questions = read_questions(args.questions.resolve())
    template, _ = load_prompt()
    partial = out.with_name(out.name + ".partial")
    done = load(partial) if partial.is_file() else {}
    # A neutral working directory: the CLI then finds no project CLAUDE.md,
    # settings or MCP servers to put in front of the model.
    os.chdir(tempfile.mkdtemp(prefix="precis-reformulate-"))
    with partial.open("a", encoding="utf-8") as fh:

        def keep(row: Mapping[str, Any]) -> None:
            fh.write(dump([row]))
            fh.flush()

        rows, failures = generate(
            questions,
            template=template,
            model=args.model,
            transport=claude_cli,
            done=done,
            on_row=keep,
            workers=args.workers,
        )
    if failures:
        for line in failures:
            print(f"failed: {line}", file=sys.stderr)
        raise ReformulationError(
            f"{len(failures)} of {len(questions)} questions failed; finished "
            f"rows are in {partial}, run again to retry the rest"
        )
    out.write_text(dump(rows), encoding="utf-8")
    partial.unlink()
    print(f"wrote {out} ({len(rows)} rows, model {args.model})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _questions(args) if args.cmd == "questions" else _reformulate(args)
    except ReformulationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
