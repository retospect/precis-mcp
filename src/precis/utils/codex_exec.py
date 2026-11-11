"""Worker-side wrapper around ``codex exec`` — the ChatGPT-subscription lane.

The OpenAI-side sibling of :mod:`precis.utils.claude_p`: a one-shot, tool-less
model call over the first-party ``codex`` binary logged in with a ChatGPT
subscription (gr543830). Reto's 2026-10-31 no-extraction ruling is absolute
for this lane: only the ``codex`` binary is ever invoked — nothing here reads
``~/.codex/auth.json``, calls ``chatgpt.com`` backend APIs, or proxies
through a third party. Each host logs in through Codex's own flow
(``codex login``); no auth file is copied between hosts.

Why ``codex exec`` rather than an SDK:

* **No new top-level dep.** ``codex`` is the vendor's own CLI; the
  subscription's auth and refresh stay inside it.
* **Subprocess isolation.** A bad call (OOM, timeout, parse failure) cannot
  crash the worker.
* **Easy to mock for tests.** ``PRECIS_CODEX_BIN`` names the binary — point
  it at a stub script that emits recorded JSONL; no real codex required.

Invocation (all flags are ``codex exec``'s own, 0.166.x):

* ``--json`` — newline-delimited event stream on stdout. The reply is the
  ``agent_message`` text of the last ``item.completed`` item; token usage
  rides the trailing ``turn.completed`` event's ``usage`` object
  (``input_tokens`` / ``cached_input_tokens`` / ``output_tokens``); a failed
  turn arrives as a ``turn.failed`` / top-level ``error`` event.
* ``--sandbox read-only`` — a worker prompt can carry prod content, so the
  lane never gets a writable workspace. ``--cd <throwaway>`` points the run
  at a fresh temp directory (created per call, removed after), so nothing
  project-shaped is read either.
* ``--ignore-user-config`` — ``$CODEX_HOME/config.toml`` is skipped, so no
  user-configured MCP server can attach. The write-capable precis MCP stays
  out unless a later ruling adds it (me538243). Auth still resolves from
  ``$CODEX_HOME`` — the ruling above only excludes *config*, not login.
* ``--skip-git-repo-check`` + ``--ephemeral`` — the cwd is a temp dir, not a
  repo, and no session rollout files are persisted.

Output contract mirrors :func:`~precis.utils.claude_p.call_claude_p`'s: the
caller embeds a JSON-shape hint in the prompt and this helper parses the
*last* ``{ … }`` block in the reply text
(:func:`~precis.utils.claude_p._parse_last_json_block`). No parseable block
→ :class:`CodexExecUnparseableError`. Stdout that isn't a JSONL stream at
all (a stub binary, an older CLI ignoring ``--json``) degrades to the
bare-text path — same tolerance as ``claude_p``'s envelope unwrap.

Auth env: a ChatGPT login lives in ``$CODEX_HOME/auth.json`` (``CODEX_HOME``
defaulting to ``~/.codex``). When that file exists the child env has
``OPENAI_API_KEY`` scrubbed — the billed-per-token path must not shadow the
subscription, the same trade :func:`claude_oauth.prefer_oauth_over_api_key`
makes on the claude lane. The file itself is never opened here; existence
is the whole probe.

Knobs (all overridable per call, project defaults via env):

* ``PRECIS_CODEX_BIN``       — codex binary path (default ``codex``).
* ``PRECIS_CODEX_MODEL``     — model id for ``-m`` (default: the CLI's own
  configured default — no flag is passed).
* ``PRECIS_CODEX_TIMEOUT_S`` — wall-clock timeout (default ``120``).

Capability probes — :func:`codex_bin_present` and
:func:`codex_login_present` — let the router's rung gate and the heartbeat's
capability surface skip hosts where the lane can't run (no binary, or no
ChatGPT login per ``codex login status``). ``codex_login_present`` is
TTL-cached so a dispatch hot path pays one probe a minute, not a subprocess
per call.

**Ships dark.** The transport binds in the router registry but a
``codex_exec`` chain rung is dropped at dispatch until an operator lights
the lane — ``llm.codex_exec`` in ``app_settings``, or
``PRECIS_CODEX_EXEC`` per process
(:func:`precis.utils.llm.live_config.codex_exec_enabled`). Open question
for Reto before lighting: whether unattended ``codex exec`` use is covered
by a personal ChatGPT plan's terms (the gripe's terms check).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.utils._claude_subprocess import (
    ClaudeProcessError,
    disarm_session_hooks,
    exit_detail,
    terminate_claude,
    to_str,
)
from precis.utils.claude_p import _parse_last_json_block
from precis.utils.job_footprint import MeasuredPopen

log = logging.getLogger(__name__)


#: Default wall-clock timeout. A one-shot exec turn is sized like the
#: ``claude -p`` lane's — fast judge calls finish in seconds; the headroom
#: absorbs a cold CLI + a slow subscription window.
_DEFAULT_TIMEOUT_S = 120

#: Seconds ``run``'s timeout branch drains the child's pipes after
#: :func:`terminate_claude` — same bound as the claude runner's
#: ``_REAP_DRAIN_S``: a survivor the KILL could not reach still holds the
#: write ends, so an unbounded ``communicate()`` would wedge the worker.
_REAP_DRAIN_S: float = 5.0

#: ``codex login status`` verdict, cached this long — a worker dispatch
#: shouldn't spawn a probe subprocess per call.
_LOGIN_PROBE_TTL_S: float = 60.0


class CodexExecError(ClaudeProcessError):
    """Raised when ``codex exec`` fails or its output cannot be parsed.

    Shares :class:`ClaudeProcessError`'s shape — stdout / stderr /
    returncode / ``timed_out`` / ``binary_missing`` — so the router's
    :func:`~precis.utils.llm.router._error_result` maps a missing binary to
    ``cli_unavailable`` and a wall-clock timeout to ``paused`` with no
    codex-specific branches.

    ``reply`` carries the CLI's own message text when one was extracted —
    an ``agent_message`` reply that failed the JSON contract, or the
    ``message`` of a ``turn.failed``/``error`` event — so a caller can
    surface (and quota-classify) the model's words instead of the raw
    JSONL stream. ``None`` when no event payload was parsed.
    """

    def __init__(
        self, message: str, *, reply: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.reply = reply


class CodexExecUnparseableError(CodexExecError):
    """The subprocess ran fine but the reply carried no parseable JSON
    block — the *model* broke the output contract, not the infrastructure.
    Split out (mirroring :class:`~precis.utils.claude_p.ClaudePUnparseableError`)
    so a caller can retry a format flake without also retrying timeouts /
    non-zero exits / a missing binary. Catching plain
    :class:`CodexExecError` keeps working — this is a subclass."""


@dataclass(frozen=True)
class CodexExecResult:
    """Parsed result of a successful :func:`call_codex_exec` invocation.

    ``data`` is the parsed JSON dict from the reply; ``text`` the reply
    itself (the joined ``agent_message`` items). ``raw_stdout`` keeps the
    full JSONL stream for audit. ``cost_usd`` is always ``None`` today —
    ``codex exec`` reports token usage, never dollars (and a subscription
    call has no dollar figure to report).
    """

    data: dict[str, Any]
    raw_stdout: str
    cost_usd: float | None
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None


def resolve_codex_binary() -> str:
    """The codex binary path — ``PRECIS_CODEX_BIN`` or ``codex``."""
    return os.environ.get("PRECIS_CODEX_BIN", "codex")


def codex_home() -> Path:
    """``$CODEX_HOME`` — the CLI's config/login root (default ``~/.codex``)."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_bin_present() -> bool:
    """Whether THIS host can exec ``codex`` — ``PRECIS_CODEX_BIN`` (a name
    on PATH or an absolute path, the escape hatch for a launchd daemon's
    thin PATH) else ``codex`` on PATH. Mirrors
    :func:`precis.workers.capability_probe._probe_claude_bin`."""
    configured = resolve_codex_binary()
    return bool(
        shutil.which(configured)
        or (os.path.isabs(configured) and os.path.exists(configured))
    )


def codex_login_file_present() -> bool:
    """Whether a ChatGPT/API login file exists under ``$CODEX_HOME``.

    Presence only — the file is NEVER opened (Reto's no-extraction ruling:
    ``auth.json`` is the ``codex`` CLI's credential, not ours). Used to scrub
    ``OPENAI_API_KEY`` from a child's env so the subscription path wins.
    """
    return (codex_home() / "auth.json").is_file()


def _probe_login_status(binary: str, *, timeout_s: float = 15.0) -> bool:
    """One ``codex login status`` subprocess → ``True`` on a ChatGPT login.

    The CLI's own credential check — works whether the login came from the
    interactive flow or ``codex login --with-access-token``. Output names
    the method (``Logged in using ChatGPT`` vs an API-key login); the lane
    is the *subscription* transport, so only the ChatGPT wording counts —
    an API-key-only host answers ``False`` and skips the rung rather than
    silently billing per token. An error/timeout answers ``False`` too: a
    probe that can't tell must not advertise the capability.
    """
    try:
        res = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    return "chatgpt" in (res.stdout or "").lower()


_login_lock = threading.Lock()
_login_cache: tuple[float, bool] | None = None

#: One exec at a time per process — the gripe asks for runs serialised per
#: login once refresh races show up, and a subscription lane's quota is
#: per-login anyway, so a cheap module lock preempts concurrent refreshes
#: racing inside ``$CODEX_HOME/auth.json``. Cross-process races (two
#: workers on one host) are out of scope for the dark lane.
_run_lock = threading.Lock()


def codex_login_present() -> bool:
    """Whether THIS host has the CLI logged in on the ChatGPT subscription.

    Binary probe first (a missing binary is never logged in), then a
    :data:`_LOGIN_PROBE_TTL_S`-cached ``codex login status``. The public
    seam the router's rung gate and
    :func:`precis.workers.capability_probe.codex_login_present` share.
    """
    if not codex_bin_present():
        return False
    now = time.monotonic()
    with _login_lock:
        global _login_cache
        if _login_cache is not None and _login_cache[0] > now:
            return _login_cache[1]
        ok = _probe_login_status(resolve_codex_binary())
        _login_cache = (now + _LOGIN_PROBE_TTL_S, ok)
        return ok


def _bust_login_probe() -> None:
    """Drop the cached login verdict — the next read re-probes. Tests (and
    an operator who just ran ``codex login``) call this."""
    global _login_cache
    with _login_lock:
        _login_cache = None


def _int_or_none(v: Any) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _events_to_reply(
    stdout: str,
) -> tuple[list[str], dict[str, int | None], list[str]]:
    """Split ``--json`` stdout into (agent texts, usage, error messages).

    The stream is one JSON object per line. ``item.completed`` items of
    type ``agent_message`` carry the reply text; ``turn.completed``'s
    ``usage`` maps to the three token fields (``cached_input_tokens`` →
    ``cache_read_tokens``, matching the claude lane's naming); a failed
    turn surfaces as a ``turn.failed`` (``error.message``) or a top-level
    ``error`` event — collected verbatim so the caller can quota-classify
    the CLI's own words (a usage-limit notice arrives exactly this way).
    """
    texts: list[str] = []
    errors: list[str] = []
    usage: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
    }
    for line in stdout.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "item.completed":
            item = ev.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                texts.append(item["text"])
        elif etype == "turn.completed":
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = {
                    "input_tokens": _int_or_none(u.get("input_tokens")),
                    "output_tokens": _int_or_none(u.get("output_tokens")),
                    "cache_read_tokens": _int_or_none(u.get("cached_input_tokens")),
                }
        elif etype == "turn.failed":
            err = ev.get("error")
            msg = err.get("message") if isinstance(err, dict) else None
            errors.append(str(msg) if msg else s)
        elif etype == "error":
            msg = ev.get("message")
            errors.append(str(msg) if msg else s)
    return texts, usage, errors


def _saw_event_stream(stdout: str) -> bool:
    """Whether stdout looks like a ``--json`` event stream at all — any
    parseable ``{"type": …}`` line. A stub binary or an older CLI that
    ignored ``--json`` prints bare text instead; that degrades to the
    bare-text reply path rather than "unparseable events"."""
    for line in stdout.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("type"), str):
            return True
    return False


def _run_codex(
    argv: list[str],
    *,
    binary: str,
    timeout_s: float,
    env: dict[str, str],
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``argv`` and return the completed process on success.

    The codex-side twin of :func:`precis.utils._claude_subprocess.run_claude`
    minus its claude auth bootstrap — same TERM-then-KILL timeout handling
    (the child stays in the worker's process group so a launchd restart
    reaps an in-flight run) and the same error mapping: ``FileNotFoundError``
    → ``binary_missing``, ``TimeoutExpired`` → ``timed_out``, non-zero exit
    → error carrying the exit detail. Raises :class:`CodexExecError`.
    """
    try:
        # MeasuredPopen: the reap captures this child's own rusage and
        # attributes it to the job bound on this thread (gr595536).
        proc = MeasuredPopen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise CodexExecError(
            f"codex binary not found ({binary!r}); "
            "set PRECIS_CODEX_BIN or install the Codex CLI",
            binary_missing=True,
        ) from exc
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        terminate_claude(proc)
        try:
            out, err = proc.communicate(timeout=_REAP_DRAIN_S)
        except subprocess.TimeoutExpired as drain:
            out, err = to_str(drain.output), to_str(drain.stderr)
        raise CodexExecError(
            f"codex exec timed out after {timeout_s}s",
            stdout=to_str(out),
            stderr=to_str(err),
            timed_out=True,
        ) from None
    except BaseException:
        terminate_claude(proc)
        raise
    res: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        argv, proc.returncode, to_str(out), to_str(err)
    )
    if res.returncode != 0:
        raise CodexExecError(
            f"codex exec exited {res.returncode}: "
            f"{exit_detail(res.stdout, res.stderr)}",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=res.returncode,
            reply=_reply_from_stream(res.stdout),
        )
    return res


def _reply_from_stream(stdout: str) -> str | None:
    """The reply/error text a JSONL stream carried, for the error path —
    the joined ``agent_message`` texts, else the first error-event message.
    ``None`` when stdout held no event stream."""
    texts, _usage, errors = _events_to_reply(stdout)
    if texts:
        return "\n".join(texts)
    if errors:
        return errors[0]
    return None


def call_codex_exec(
    prompt: str,
    *,
    model: str | None = None,
    timeout_s: float | None = None,
    extra_args: tuple[str, ...] = (),
) -> CodexExecResult:
    """Run ``codex exec <prompt>`` and parse the reply's last JSON block.

    Args:
        prompt: The full prompt text. The caller is responsible for
            including a JSON-shape hint so the model emits parseable output.
        model: ``-m`` override (``PRECIS_CODEX_MODEL`` else the CLI's own
            default — unset passes no flag). A ``claude*`` id can never be
            a codex model: the router resolves a tier default like
            ``claude-sonnet-5-5`` into a model-less codex rung, and passing it
            would hard-fail every call — dropped here, at the wire boundary.
        timeout_s: Wall-clock cap (``PRECIS_CODEX_TIMEOUT_S`` or ``120``).
        extra_args: Additional ``codex exec`` flags, verbatim. Most callers
            should rely on the defaults.

    Returns:
        :class:`CodexExecResult` with the parsed dict.

    Raises:
        CodexExecError: subprocess non-zero exit, timeout, missing binary,
            or a ``turn.failed``/``error`` event.
        CodexExecUnparseableError: the run completed but the reply carried
            no parseable JSON block.
    """
    binary = resolve_codex_binary()
    model = model or os.environ.get("PRECIS_CODEX_MODEL") or None
    if model and model.startswith("claude"):
        log.debug("codex_exec: dropping claude model id %r (not a codex model)", model)
        model = None
    if timeout_s is None:
        timeout_env = os.environ.get("PRECIS_CODEX_TIMEOUT_S")
        timeout_s = float(timeout_env) if timeout_env else _DEFAULT_TIMEOUT_S

    # A worker prompt can carry prod content: read-only sandbox, a throwaway
    # cwd, no user config (no MCP servers attach — precis stays off the
    # wire), no persisted rollout files.
    workdir = Path(tempfile.mkdtemp(prefix="precis-codex-exec-"))
    argv = [
        binary,
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--cd",
        str(workdir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        *(("--model", model) if model else ()),
        *extra_args,
        # ``--`` end-of-options sentinel, prompt last and positional: a
        # prompt beginning with ``-`` must never be parsed as a flag — the
        # same hardening as claude_p's argv tail.
        "--",
        prompt,
    ]

    env = dict(os.environ)
    disarm_session_hooks(env)
    # Subscription-first auth (mirrors prefer_oauth_over_api_key): a ChatGPT
    # login exists under CODEX_HOME, so an ambient OPENAI_API_KEY — the
    # per-token billed path — is scrubbed from the child.
    if codex_login_file_present():
        env.pop("OPENAI_API_KEY", None)

    log.debug(
        "codex_exec: invoking model=%s timeout_s=%s", model or "(default)", timeout_s
    )
    try:
        with _run_lock:
            res = _run_codex(
                argv, binary=binary, timeout_s=timeout_s, env=env, cwd=str(workdir)
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    texts, usage, errors = _events_to_reply(res.stdout or "")
    if not _saw_event_stream(res.stdout or ""):
        # Bare-text fallback: a stub binary or an older CLI that ignored
        # ``--json`` printed the reply directly. Same contract as the
        # envelope path — the last JSON block is the payload.
        text = (res.stdout or "").strip()
        data = _parse_last_json_block(text)
        if data is None:
            raise CodexExecUnparseableError(
                "codex exec returned no parseable JSON block",
                stdout=res.stdout,
                stderr=res.stderr,
                reply=text,
            )
        return CodexExecResult(
            data=data, raw_stdout=res.stdout, cost_usd=None, text=text
        )

    if errors:
        raise CodexExecError(
            "codex exec reported an error: " + "; ".join(errors),
            stdout=res.stdout,
            stderr=res.stderr,
            reply=errors[0],
        )

    text = "\n".join(texts)
    data = _parse_last_json_block(text)
    if data is None:
        raise CodexExecUnparseableError(
            "codex exec returned no parseable JSON block",
            stdout=res.stdout,
            stderr=res.stderr,
            reply=text or _reply_from_stream(res.stdout or ""),
        )
    return CodexExecResult(
        data=data,
        raw_stdout=res.stdout,
        cost_usd=None,
        text=text,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
    )


__all__ = [
    "CodexExecError",
    "CodexExecResult",
    "CodexExecUnparseableError",
    "call_codex_exec",
    "codex_bin_present",
    "codex_home",
    "codex_login_file_present",
    "codex_login_present",
    "resolve_codex_binary",
]
