"""The runner seam: fake (replay + record), live (``claude -p`` dry run)
and executed (``claude -p`` with a precis MCP bound to a scratch DSN).

Both dry-run runners receive the same prompt — :func:`build_prompt`
renders the injected skill(s), the task and the JSON reply contract — so
a test that asserts on the fake's recorded prompt is asserting on what
the live model would have read. The executed runner gets
:func:`build_exec_prompt` instead: same skill injection, a *do it with
the tools* contract, and its :class:`Transcript` is reconstructed from
the run's stream-json ``tool_use`` events, not a declared reply — so a
skill that misleads at execution time (right plan, wrong verb arguments
under a real tool surface, a record that never lands) scores red here
where the live runner stays green (gr608716).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from precis.skill_eval.cases import VERBS, EvalCase, Transcript, VerbCall
from precis.utils._claude_subprocess import ClaudeProcessError, resolve_binary
from precis.utils.claude_oauth import LOGGED_OUT_MARKERS

log = logging.getLogger(__name__)

#: Default caps for the live runner. Env knobs (Tier 2, read once here):
#: ``PRECIS_SKILL_EVAL_CASE_USD``, ``PRECIS_SKILL_EVAL_RUN_USD``,
#: ``PRECIS_SKILL_EVAL_TIMEOUT_S``, ``PRECIS_SKILL_EVAL_MODEL``.
DEFAULT_CASE_USD = 0.25
DEFAULT_RUN_USD = 2.00
DEFAULT_TIMEOUT_S = 180.0

#: Default caps for the executed runner — a multi-turn tool session costs
#: more than a one-shot declaration, so the defaults are larger. The same
#: env knobs apply; ``PRECIS_SKILL_EVAL_MAX_TURNS`` is executed-only.
DEFAULT_EXEC_CASE_USD = 1.00
DEFAULT_EXEC_RUN_USD = 10.00
DEFAULT_EXEC_TIMEOUT_S = 600.0
DEFAULT_EXEC_MAX_TURNS = 30

#: The DSN ``scripts/dev db-up`` provisions — the only DSN this runner
#: binds the eval MCP to by default. Prod coordinates never enter this
#: path: an explicit ``--dsn`` / ``PRECIS_SKILL_EVAL_DSN`` selects a
#: different *test* stack, but nothing here resolves prod.
DEFAULT_DEV_DSN = "postgresql://postgres@precis-test-db:5432/precis_dev"

#: How the spawned ``claude -p`` launches ``precis serve`` — the binary on
#: PATH inside the dev container. ``PRECIS_SKILL_EVAL_SERVE_CMD`` /
#: ``--serve-cmd`` overrides (e.g. ``uv run --no-sync precis serve``).
DEFAULT_SERVE_CMD = "precis serve"

#: Milliseconds the CLI waits for the precis MCP server to connect —
#: a cold ``precis serve`` loads the embedder (~50 s) and the default
#: connect timeout would fail the gate first (same numbers as
#: ``scripts/exercise-mcp``).
_SERVE_CONNECT_TIMEOUT_MS = "180000"

#: What ``{run}`` expands to in a dry-run (fake/live) prompt — the same
#: ``<digits>-<digits>`` shape as an executed ``run_id``, so a corpus
#: ``re:<slug>-\d+-\d+`` expectation matches either surface.
DRY_RUN_TOKEN = "0-0"

#: Prefix the typed MCP profile registers per-verb tools under.
_MCP_TOOL_PREFIX = "mcp__precis__"

#: A put/edit result is an error envelope (``[error:<Class>] …``) or a
#: stream-json ``is_error`` tool_result — either way the write did not
#: land and must not seed a record candidate.
_ERROR_ENVELOPE_RE = re.compile(r"^\s*\[error:")

#: ``created <kind> <handle|id=N>`` in a put/edit ack — the minted ref.
_CREATED_RE = re.compile(r"\bcreated\s+(\w+)\s+(\S+?)[:.,\s)]")

_AUTH_MARKERS: tuple[str, ...] = (
    *LOGGED_OUT_MARKERS,
    "invalid authentication credentials",
    "failed to authenticate",
    "authentication_error",
)


class LiveUnavailable(RuntimeError):
    """``claude`` is missing or unauthenticated: the run is skipped, never
    scored — an unauthenticated container ``claude`` exits 0 with a banner
    and would otherwise read as a failed (or, worse, passed) case."""


class RunBudgetExhausted(RuntimeError):
    """The per-run USD cap would be exceeded by the next case."""


@dataclass(frozen=True, slots=True)
class RecordedRun:
    case_id: str
    prompt: str


class Runner(Protocol):
    name: str

    def run(self, case: EvalCase, prompt: str) -> Transcript: ...


def build_prompt(case: EvalCase, skill_texts: Mapping[str, str]) -> str:
    """The dry-run prompt: skills in full, the task, one-JSON-object reply.

    A ``{run}`` token in the case prompt renders as
    :data:`DRY_RUN_TOKEN` — corpus cases address the records they create
    as ``<slug>-{run}`` so executed runs on a shared eval DB never
    collide, and dry runs get the same ``<digits>-<digits>`` shape.
    """
    skills = "\n\n".join(
        f"<skill id={slug!r}>\n{skill_texts[slug]}\n</skill>" for slug in case.skills
    )
    verbs = ", ".join(sorted(VERBS))
    return (
        "You are an agent working through the precis MCP server (verbs: "
        f"{verbs}). This is a dry run: you cannot execute calls. Read the "
        "skill(s) below, then plan exactly how you would complete the task.\n\n"
        f"{skills}\n\n"
        f"Task: {case.prompt.replace('{run}', DRY_RUN_TOKEN)}\n\n"
        "Reply with exactly one JSON object and nothing else:\n"
        '{"calls": [{"verb": "get", "kind": "paper", "args": {"id": "pa40"}}], '
        '"record": null, "answer": "..."}\n'
        "- calls: the precis verb calls you would make, in order, with the exact "
        "arguments (kind in 'kind', every other parameter in 'args').\n"
        "- record: if the task asks you to create something, an object with "
        "the 'kind' and the fields of the one record you would put; else null.\n"
        "- answer: the one-paragraph reply you would give the user."
    )


class FakeRunner:
    """Replays each case's scripted ``fake:`` transcript (or an override from
    ``scripts``) and records every prompt it was handed."""

    name = "fake"
    #: Every runner may carry a model tag; the fake's default is none.
    model: str | None = None

    def __init__(self, scripts: Mapping[str, Transcript] | None = None) -> None:
        self.scripts: dict[str, Transcript] = dict(scripts or {})
        self.recorded: list[RecordedRun] = []

    def run(self, case: EvalCase, prompt: str) -> Transcript:
        self.recorded.append(RecordedRun(case.case_id, prompt))
        return self.scripts.get(case.case_id, case.fake)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def parse_transcript(data: Mapping[str, Any], *, cost_usd: float | None) -> Transcript:
    """The model's JSON reply → :class:`Transcript`. Lenient on shape: a
    malformed call is dropped with a log, never a crash, so a sloppy reply
    scores as the calls it did declare."""
    calls: list[VerbCall] = []
    for raw in data.get("calls") or []:
        if not isinstance(raw, dict) or raw.get("verb") not in VERBS:
            log.warning("skill eval: dropping malformed call %r", raw)
            continue
        args = raw.get("args")
        kind = raw.get("kind")
        calls.append(
            VerbCall(
                verb=str(raw["verb"]),
                kind=str(kind) if kind is not None else None,
                args=dict(args) if isinstance(args, dict) else {},
            )
        )
    record = data.get("record")
    return Transcript(
        calls=tuple(calls),
        record=dict(record) if isinstance(record, dict) else None,
        answer=str(data.get("answer") or ""),
        cost_usd=cost_usd,
    )


class LiveClaudeRunner:
    """Planned-call dry run through tool-free ``claude -p`` (host only).

    ``call`` defaults to :func:`precis.utils.claude_p.call_claude_p`; tests
    inject a stub. Cost accounting is conservative: a reply without a
    ``total_cost_usd`` is booked at the per-case cap.
    """

    name = "live"

    def __init__(
        self,
        *,
        model: str | None = None,
        case_usd: float | None = None,
        run_usd: float | None = None,
        timeout_s: float | None = None,
        call: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model or os.environ.get("PRECIS_SKILL_EVAL_MODEL") or None
        self.case_usd = (
            case_usd
            if case_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_CASE_USD", DEFAULT_CASE_USD)
        )
        self.run_usd = (
            run_usd
            if run_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_RUN_USD", DEFAULT_RUN_USD)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else _env_float("PRECIS_SKILL_EVAL_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        )
        if call is None:
            from precis.utils.claude_p import call_claude_p

            call = call_claude_p
        self._call = call
        self.spent_usd = 0.0

    def unavailable_reason(self) -> str | None:
        """Pre-flight: a reason string when no run should even start."""
        binary = resolve_binary()
        if shutil.which(binary) is None and not os.path.isfile(binary):
            return f"claude binary not found ({binary!r})"
        return None

    def run(self, case: EvalCase, prompt: str) -> Transcript:
        if self.spent_usd + self.case_usd > self.run_usd + 1e-9:
            raise RunBudgetExhausted(
                f"run budget ${self.run_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.2f}, next case cap ${self.case_usd:.2f})"
            )
        try:
            res = self._call(
                prompt,
                model=self.model,
                max_usd=self.case_usd,
                timeout_s=self.timeout_s,
            )
        except ClaudeProcessError as exc:
            if exc.binary_missing:
                raise LiveUnavailable(str(exc)) from exc
            blob = " ".join(
                t for t in (str(exc), exc.stdout or "", exc.stderr or "") if t
            ).lower()
            if any(m in blob for m in _AUTH_MARKERS):
                raise LiveUnavailable(f"claude is not authenticated: {exc}") from exc
            raise
        cost = res.cost_usd
        self.spent_usd += cost if cost is not None else self.case_usd
        data = res.data if isinstance(res.data, dict) else {}
        if not data:
            try:
                data = json.loads(res.text or "")
            except (TypeError, ValueError):
                data = {}
        return parse_transcript(data, cost_usd=cost)


def build_exec_prompt(
    case: EvalCase,
    skill_texts: Mapping[str, str],
    *,
    run_id: str = DRY_RUN_TOKEN,
) -> str:
    """The executed prompt: same skill injection as :func:`build_prompt`,
    a *call the tools* contract in place of the declared-JSON one.

    ``run_id`` is the runner's per-run token — a ``{run}`` placeholder in
    the task text expands to it so every record the case mints is
    namespaced to this run on the shared eval DB (a second pass, or a
    second model's pass, can't collide with or read this one's rows).
    """
    skills = "\n\n".join(
        f"<skill id={slug!r}>\n{skill_texts[slug]}\n</skill>" for slug in case.skills
    )
    return (
        "You are an agent working through the precis MCP server; its tools "
        "are attached and live on a scratch database. Read the skill(s) "
        "below, then complete the task by actually calling the tools — a "
        "plan without calls is a failure.\n\n"
        f"{skills}\n\n"
        f"Task: {case.prompt.replace('{run}', run_id)}\n\n"
        "Do it now with the precis tools. Finish with a short reply "
        "naming the calls you made and the result."
    )


# ── stream-json → Transcript ────────────────────────────────────────


def _iter_stream_events(stdout: str) -> Iterator[dict[str, Any]]:
    """Parsed ``--output-format stream-json`` events, one JSON object per
    line; non-JSON lines (banners, stub output) are skipped."""
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            ev = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("type"), str):
            yield ev


def _message_content(ev: dict[str, Any]) -> Any:
    msg = ev.get("message")
    if isinstance(msg, dict) and "content" in msg:
        return msg["content"]
    return ev.get("content")


def _precis_call_from_tool(name: str, inp: Any) -> VerbCall | None:
    """One ``mcp__precis__*`` ``tool_use`` block → a :class:`VerbCall`.

    The typed profile registers per-verb tools whose input carries
    ``kind`` at top level; kind-specific fields may ride a nested
    ``args`` dict — flattened into the call's args (the nested dict
    itself stays too) so an ``expect.calls`` pattern matches either
    spelling. Under the ``command`` profile the single
    ``mcp__precis__precis`` tool carries ``command="verb(kind=…)"`` —
    parsed back through :func:`parse_command` so the transcript looks the
    same either way. Non-precis tools return ``None``.
    """
    if not name.startswith(_MCP_TOOL_PREFIX):
        return None
    tool = name[len(_MCP_TOOL_PREFIX) :]
    inp = inp if isinstance(inp, dict) else {}
    if tool == "precis":  # command profile
        from precis.tools.command_parser import parse_command

        try:
            verb, kwargs = parse_command(
                str(inp.get("command") or ""), text=inp.get("text")
            )
        except Exception:
            log.warning("skill eval: unparseable command-profile call %r", inp)
            return None
        kind = kwargs.pop("kind", None)
        return VerbCall(
            verb=verb, kind=str(kind) if kind is not None else None, args=kwargs
        )
    kind = inp.get("kind")
    args = dict(inp.get("args") or {})
    args.update({k: v for k, v in inp.items() if k != "kind"})
    return VerbCall(verb=tool, kind=str(kind) if kind is not None else None, args=args)


def _tool_result_text(block: dict[str, Any]) -> str:
    """The text a ``tool_result`` content block carried."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def stream_tool_calls(
    stdout: str,
) -> tuple[list[tuple[str, VerbCall]], dict[str, tuple[str, bool]]]:
    """``(tool_use_id, VerbCall)`` pairs + ``tool_use_id → (text, is_error)``.

    Every precis verb call surfaces as a ``tool_use`` content block in an
    ``assistant`` event named ``mcp__precis__<verb>``; its result lands in
    the paired ``tool_result`` block of a later ``user`` event. Sidechain
    (subagent) events are internal transcript, not the agent's own calls,
    and are skipped — same rule as ``claude_agent._is_sidechain_event``.
    """
    calls: list[tuple[str, VerbCall]] = []
    results: dict[str, tuple[str, bool]] = {}
    for ev in _iter_stream_events(stdout):
        if ev.get("parent_tool_use_id") or ev.get("isSidechain"):
            continue
        content = _message_content(ev)
        if not isinstance(content, list):
            continue
        if ev["type"] == "assistant":
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                call = _precis_call_from_tool(str(b.get("name") or ""), b.get("input"))
                if call is not None:
                    calls.append((str(b.get("id") or ""), call))
        elif ev["type"] == "user":
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tid = str(b.get("tool_use_id") or "")
                results[tid] = (_tool_result_text(b), bool(b.get("is_error")))
    return calls, results


def _write_candidates(
    calls: list[tuple[str, VerbCall]],
    results: dict[str, tuple[str, bool]],
) -> list[tuple[str, str]]:
    """``(kind, ident)`` of every ref a *successful* put/edit produced or
    addressed — the candidate set the landed-record check fetches. A
    failed call yields none: the write never happened."""
    out: list[tuple[str, str]] = []
    for tid, call in calls:
        if call.verb not in ("put", "edit"):
            continue
        text, is_error = results.get(tid, ("", False))
        if is_error or _ERROR_ENVELOPE_RE.match(text):
            continue
        created = _CREATED_RE.search(text)
        if created:
            out.append((created.group(1), created.group(2).rstrip(".)")))
            continue
        ident = call.args.get("id")
        if call.kind and ident is not None:
            out.append((call.kind, str(ident)))
    return out


def _scored_calls(
    pairs: list[tuple[str, VerbCall]],
    results: dict[str, tuple[str, bool]],
) -> tuple[VerbCall, ...]:
    """The transcript's call list with each call's tool-result error
    attached: a ``tool_result`` that came back ``is_error`` or an
    ``[error:…]`` envelope means the call's work never happened, so it
    must not quietly satisfy an ``expect.calls`` pattern — a call-only
    case whose every call failed is a fail, not a pass."""
    out: list[VerbCall] = []
    for tid, call in pairs:
        text, is_error = results.get(tid, ("", False))
        if is_error or _ERROR_ENVELOPE_RE.match(text):
            err = text.strip().splitlines()[0][:200] if text.strip() else "tool error"
            call = VerbCall(call.verb, call.kind, call.args, error=err)
        out.append(call)
    return tuple(out)


def fetch_record(dsn: str, kind: str | None, ident: str) -> dict[str, Any] | None:
    """One landed ref as a subset-matchable dict, or ``None``.

    The record an ``expect.record`` is judged against for executed runs —
    the row that actually *landed* on the eval DSN, so a ``put`` that
    errored or wrote a different shape fails here where a declared record
    passed the live runner. ``ident`` resolves as ``id=N``, a decimal
    handle, or a cite_key slug. The returned ``id`` is the ref's cite_key
    when it has one — corpus criteria address records by the slug the
    case minted — else its decimal handle; ``handle`` always carries the
    decimal handle. Lazy imports: psycopg/handle_registry stay off the
    fake/live runners' import path.
    """
    import psycopg

    from precis.utils import handle_registry

    ref_id: int | None = None
    m = re.fullmatch(r"id=(\d+)", ident.strip())
    if m:
        ref_id = int(m.group(1))
    elif ident.strip().isdigit():
        ref_id = int(ident.strip())
    else:
        parsed = handle_registry.parse(ident.strip())
        if parsed is not None and not parsed[1]:
            ref_id = parsed[2]
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            if ref_id is None:
                row = conn.execute(
                    "SELECT ref_id FROM ref_identifiers "
                    "WHERE id_kind='cite_key' AND id_value=%s LIMIT 1",
                    (ident.strip(),),
                ).fetchone()
                if row is None:
                    return None
                ref_id = int(row[0])
            row = conn.execute(
                "SELECT kind, title, meta,"
                " (SELECT id_value FROM ref_identifiers ri"
                "  WHERE ri.ref_id = refs.ref_id AND ri.id_kind = 'cite_key'"
                "  ORDER BY id_value LIMIT 1)"
                " FROM refs WHERE ref_id=%s AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                return None
            kind_db, title, meta, cite_key = row
            tags = [
                r[0]
                for r in conn.execute(
                    "SELECT CASE WHEN t.namespace IN ('FLAG','OPEN') "
                    "THEN t.value ELSE t.namespace || ':' || t.value END "
                    "FROM ref_tags rt JOIN tags t USING (tag_id) "
                    "WHERE rt.ref_id=%s ORDER BY 1",
                    (ref_id,),
                )
            ]
            body = "\n\n".join(
                r[0]
                for r in conn.execute(
                    "SELECT text FROM chunks WHERE ref_id=%s AND ord>=0 "
                    "ORDER BY ord LIMIT 50",
                    (ref_id,),
                )
            )
    except Exception as exc:
        log.warning("skill eval: fetch_record(%s, %r) failed: %s", kind, ident, exc)
        return None
    handle = handle_registry.try_format(str(kind_db), int(ref_id)) or str(ref_id)
    return {
        "kind": str(kind_db),
        "id": str(cite_key) if cite_key else handle,
        "ref_id": int(ref_id),
        "handle": handle,
        "title": title,
        "body": body,
        "tags": tags,
        "meta": dict(meta) if isinstance(meta, dict) else {},
    }


#: Bound once at module level so :class:`ExecutedClaudeRunner` can default
#: to it where its own ``fetch_record`` parameter shadows the name.
_default_fetch_record = fetch_record


def serve_mcp_config(
    *, dsn: str, serve_cmd: str | None = None, run_id: str = "run"
) -> dict[str, Any]:
    """The ``--mcp-config`` document for one executed run: one ``precis``
    stdio server bound to the eval DSN, typed profile, and a per-run
    default tag so every record the run mints is namespaced (the dev DB
    is shared; an eval write must not masquerade as real data)."""
    cmd = shlex.split(serve_cmd or DEFAULT_SERVE_CMD)
    return {
        "mcpServers": {
            "precis": {
                "command": cmd[0],
                "args": cmd[1:],
                "env": {
                    "PRECIS_DATABASE_URL": dsn,
                    "PRECIS_MCP_PROFILE": "typed",
                    "PRECIS_DEFAULT_TAGS": f"eval-run-{run_id}",
                    # protein is a plugin kind, dark without this — the
                    # test stack exists to exercise these verbs.
                    "PRECIS_BIO_ENABLED": "1",
                },
            }
        }
    }


class ExecutedClaudeRunner:
    """Executed run: ``claude -p`` with a precis MCP bound to a scratch DSN.

    The transcript is reconstructed from stream-json ``tool_use`` events —
    calls the model *made*, not calls it *declared*. When the case asks
    for a record, the record judged is the row that landed on the eval
    DSN (fetched through ``fetch_record``), never the model's claim. Same
    caps/skip contract as :class:`LiveClaudeRunner`: a missing or
    unauthenticated ``claude`` (or an MCP that never connects — the test
    stack is down) skips the run, exit 0.

    A precis call whose ``tool_result`` is ``is_error`` or an
    ``[error:…]`` envelope keeps its place in the transcript but carries
    the error on :attr:`VerbCall.error` — scoring counts it as a failed
    call, not work done.

    ``call`` defaults to :func:`precis.utils.claude_agent.call_claude_agent`;
    tests inject a stub returning an ``AgentResult``-shaped object with a
    canned stream-json ``raw_stdout``. ``fetch_record`` defaults to
    :func:`fetch_record` (psycopg on the eval DSN); tests inject a fake.
    """

    name = "executed"

    def __init__(
        self,
        *,
        dsn: str | None = None,
        model: str | None = None,
        case_usd: float | None = None,
        run_usd: float | None = None,
        timeout_s: float | None = None,
        max_turns: int | None = None,
        serve_cmd: str | None = None,
        run_id: str | None = None,
        call: Callable[..., Any] | None = None,
        fetch_record: Callable[[str, str | None, str], dict[str, Any] | None]
        | None = None,
    ) -> None:
        self.dsn = dsn or os.environ.get("PRECIS_SKILL_EVAL_DSN") or DEFAULT_DEV_DSN
        self.model = model or os.environ.get("PRECIS_SKILL_EVAL_MODEL") or None
        self.case_usd = (
            case_usd
            if case_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_CASE_USD", DEFAULT_EXEC_CASE_USD)
        )
        self.run_usd = (
            run_usd
            if run_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_RUN_USD", DEFAULT_EXEC_RUN_USD)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else _env_float("PRECIS_SKILL_EVAL_TIMEOUT_S", DEFAULT_EXEC_TIMEOUT_S)
        )
        self.max_turns = (
            max_turns
            if max_turns is not None
            else int(_env_float("PRECIS_SKILL_EVAL_MAX_TURNS", DEFAULT_EXEC_MAX_TURNS))
        )
        self.serve_cmd = (
            serve_cmd
            or os.environ.get("PRECIS_SKILL_EVAL_SERVE_CMD")
            or DEFAULT_SERVE_CMD
        )
        self.run_id = run_id or f"{os.getpid()}-{int(time.time())}"
        if call is None:
            from precis.utils.claude_agent import call_claude_agent

            call = call_claude_agent
        self._call = call
        self._fetch_record = (
            fetch_record if fetch_record is not None else _default_fetch_record
        )
        self.spent_usd = 0.0

    def build_prompt(self, case: EvalCase, skill_texts: Mapping[str, str]) -> str:
        return build_exec_prompt(case, skill_texts, run_id=self.run_id)

    def unavailable_reason(self) -> str | None:
        binary = resolve_binary()
        if shutil.which(binary) is None and not os.path.isfile(binary):
            return f"claude binary not found ({binary!r})"
        if not self.dsn:
            return "no eval DSN — pass --dsn or set PRECIS_SKILL_EVAL_DSN"
        return None

    def _landed_record(
        self,
        case: EvalCase,
        calls: list[tuple[str, VerbCall]],
        results: dict[str, tuple[str, bool]],
    ) -> dict[str, Any] | None:
        """The DB row the record criterion is judged against: the last
        landed write of the expected kind, else the last landed write."""
        if case.criterion.record is None:
            return None
        landed = [
            rec
            for kind, ident in _write_candidates(calls, results)
            if (rec := self._fetch_record(self.dsn, kind, ident)) is not None
        ]
        want = case.criterion.record.get("kind")
        pool = [r for r in landed if want and r["kind"] == want] or landed
        return pool[-1] if pool else None

    def run(self, case: EvalCase, prompt: str) -> Transcript:
        if self.spent_usd + self.case_usd > self.run_usd + 1e-9:
            raise RunBudgetExhausted(
                f"run budget ${self.run_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.2f}, next case cap ${self.case_usd:.2f})"
            )
        config = serve_mcp_config(
            dsn=self.dsn, serve_cmd=self.serve_cmd, run_id=self.run_id
        )
        fd, cfg_path = tempfile.mkstemp(prefix="skill-eval-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(config, fh)
            res = self._call(
                prompt,
                model=self.model,
                mcp_config=cfg_path,
                max_turns=self.max_turns,
                timeout_s=self.timeout_s,
                max_usd=self.case_usd,
                output_format="stream-json",
                require_mcp=("precis",),
                env_overlay={"MCP_CONNECT_TIMEOUT_MS": _SERVE_CONNECT_TIMEOUT_MS},
            )
        except ClaudeProcessError as exc:
            if exc.binary_missing:
                raise LiveUnavailable(str(exc)) from exc
            if exc.mcp_not_ready:
                raise LiveUnavailable(
                    f"precis MCP on the eval DSN never connected: {exc}"
                ) from exc
            blob = " ".join(
                t for t in (str(exc), exc.stdout or "", exc.stderr or "") if t
            ).lower()
            if any(m in blob for m in _AUTH_MARKERS):
                raise LiveUnavailable(f"claude is not authenticated: {exc}") from exc
            raise
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
        cost = res.cost_usd
        self.spent_usd += cost if cost is not None else self.case_usd
        stdout = res.raw_stdout or res.final_text or ""
        pairs, results = stream_tool_calls(stdout)
        record = self._landed_record(case, pairs, results)
        from precis.utils.claude_agent import stream_final_text

        return Transcript(
            calls=_scored_calls(pairs, results),
            record=record,
            answer=stream_final_text(stdout),
            cost_usd=cost,
        )


__all__ = [
    "DEFAULT_CASE_USD",
    "DEFAULT_DEV_DSN",
    "DEFAULT_EXEC_CASE_USD",
    "DEFAULT_EXEC_MAX_TURNS",
    "DEFAULT_EXEC_RUN_USD",
    "DEFAULT_EXEC_TIMEOUT_S",
    "DEFAULT_RUN_USD",
    "DEFAULT_SERVE_CMD",
    "DEFAULT_TIMEOUT_S",
    "DRY_RUN_TOKEN",
    "ExecutedClaudeRunner",
    "FakeRunner",
    "LiveClaudeRunner",
    "LiveUnavailable",
    "RecordedRun",
    "RunBudgetExhausted",
    "Runner",
    "build_exec_prompt",
    "build_prompt",
    "fetch_record",
    "parse_transcript",
    "serve_mcp_config",
    "stream_tool_calls",
]
