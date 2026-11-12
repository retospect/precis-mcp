"""Headless vendor protocols for ``run_slice``.

Only Claude receives write-capable precis MCP credentials. Codex and
Devin can carry a *scoped* grant (``read_mcp`` — reader/triage,
gr574082) when ``PRECIS_SLICE_READ_MCP`` arms it, and otherwise use
their host account login and return their handoff through output; the worker
checks every vendor's branch against the lane's deny paths before land.
Codex's ``workspace-write`` sandbox needs ``--add-dir`` for anything the
session must write outside its tree — ``run_slice`` passes the shared
git dir (worktree index/refs, objects, the gate queue + slot locks) and
the Docker socket's parent so a slice can ``git commit`` and run
``scripts/test`` (gr570533).
Devin's model allowlist is supplied by the selected lane, never inferred
from a vendor-wide default: unattended runs must not buy overage.

Observed with installed ``codex exec --json`` on 2026-11-04 (a trivial
``ADAPTER_OK`` prompt): ``{"type":"item.completed","item":{"id":"item_2",
"type":"agent_message","text":"ADAPTER_OK"}}``, followed by
``turn.completed`` with usage. ``codex exec --json --help`` confirms
``-`` reads stdin and ``-o`` writes the last message.

Kimi's headless mode (``kimi -p``, installed 2.3.3, verified 2026-11-07
against a fake provider) takes the prompt in argv and emits JSONL on
``--output-format stream-json`` — the last ``{"role":"assistant"}``
line is the final message. The CLI authenticates through a synthesized
provider env (``KIMI_MODEL_*``) the worker fills from the vault key at
launch — the only env channel for it — and answers the folder-trust
prompt with ``KIMI_CODE_TRUST_WORKSPACE=1``. It is exec'd directly,
like devin, never through scripts/kimi-tree (its ``-p`` refusal is for
interactive launches). No ``-m`` — it would rebind to the config's
managed provider — and no UA or identity override: the subscription
terms forbid tampering (td573681; autonomous lane sanctioned
2026-11-07).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PERMISSION_MODE = "bypassPermissions"
_DENY_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")

#: Command prefixes a slice session never runs — the coordinator verbs.
#: The same ``deny_exec`` the fleet's tier-R authority carries
#: (``.claude/fleet/generated/claude-settings-R.json``; restated — this
#: module stays standalone — and pinned equal by a test). gr574884: a
#: free-lane builder ran ``scripts/round in <sha>`` itself — the round
#: mark is the coordinator's — so the verbs are denied at the settings
#: layer, not left to the brief alone.
DENY_BASH: tuple[str, ...] = (
    "scripts/ship",
    "scripts/release",
    "scripts/deploy",
    "scripts/round",
    "scripts/host-ops/play",
    "pkill -f",
)


def claude_bin() -> str:
    """``PRECIS_CLAUDE_BIN`` or ``claude`` on PATH."""
    return os.environ.get("PRECIS_CLAUDE_BIN") or "claude"


def codex_bin() -> str:
    """``PRECIS_CODEX_BIN`` or ``codex`` on PATH."""
    return os.environ.get("PRECIS_CODEX_BIN") or "codex"


def devin_bin() -> str:
    """``PRECIS_DEVIN_BIN`` or ``devin`` on PATH — resolved on the worker
    at submit, for argv and the preflight alike (gr570688)."""
    return os.environ.get("PRECIS_DEVIN_BIN") or "devin"


def kimi_bin() -> str:
    """``PRECIS_KIMI_BIN``, else ``kimi`` on ``PATH``, else the user
    install at ``~/.kimi-code/bin/kimi`` — resolved on the worker at
    submit, for argv and the preflight alike (mirrors scripts/kimi-tree)."""
    return (
        os.environ.get("PRECIS_KIMI_BIN")
        or shutil.which("kimi")
        or str(Path.home() / ".kimi-code" / "bin" / "kimi")
    )


_ADAPTER_BINS: dict[str, Callable[[], str]] = {
    "claude_p": claude_bin,
    "codex_exec": codex_bin,
    "devin_tree": devin_bin,
    "kimi_tree": kimi_bin,
}


def adapter_bin(name: str) -> str:
    """The adapter's own binary, resolved exactly as its argv does —
    the name a ``cli-missing`` verdict must see on the process's own
    last line (``KeyError`` on an unknown adapter)."""
    return _ADAPTER_BINS[name]()


def deny_settings(deny_paths: tuple[str, ...], tree: Path) -> str:
    """Claude deny rules in cwd-relative and absolute forms, plus trust
    for the service account checkout's project MCP servers. The
    :data:`DENY_BASH` verbs are denied in plain and ``./`` spellings,
    like the path rules' cwd/absolute pair."""
    rules: list[str] = []
    for path in deny_paths:
        for spec in (f"./{path}", f"/{tree.as_posix()}/{path}"):
            rules += [f"{tool}({spec})" for tool in _DENY_TOOLS]
    for cmd in DENY_BASH:
        forms = (cmd, f"./{cmd}") if cmd.startswith("scripts/") else (cmd,)
        rules += [f"Bash({form}:*)" for form in forms]
    return json.dumps(
        {"permissions": {"deny": rules}, "enableAllProjectMcpServers": True}
    )


def parse_claude_result(log_text: str) -> tuple[str | None, bool]:
    """The last Claude JSON object carrying ``result``, unchanged."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            return str(obj.get("result") or ""), bool(obj.get("is_error"))
    return None, True


def parse_kimi_result(
    log_text: str, *, exit_code: int | None = 0
) -> tuple[str | None, bool]:
    """The last ``{"role":"assistant"}`` stream-json line's content —
    the session's final message. ``content`` is a string or a list of
    text parts. No usable assistant line, or a non-zero exit, is an
    error."""
    text: str | None = None
    for line in log_text.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("role") != "assistant":
            continue
        content = obj.get("content")
        if isinstance(content, str):
            candidate = content
        elif isinstance(content, list):
            candidate = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        if candidate.strip():
            text = candidate
    if text is None:
        return None, True
    return text, exit_code != 0


def _num(v: Any) -> float | None:
    """``float(v)`` for a finite number, else ``None`` — a bool or a
    non-number never counts toward usage."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _dim_value(dim: Any) -> tuple[str, float] | None:
    """``(label, value)`` of a Devin response dimension — the
    ``/session-stats`` shape a step's ``extra.response_dimensions``
    carries: ``{uid, group_title, kind: {CumulativeMetric|Metric:
    {label, value}}}``. Values are cumulative, so a caller takes the
    max, never a sum."""
    if not isinstance(dim, dict):
        return None
    kind = dim.get("kind")
    body = (
        kind.get("CumulativeMetric") or kind.get("Metric")
        if isinstance(kind, dict)
        else None
    )
    if not isinstance(body, dict):
        return None
    v = _num(body.get("value"))
    if v is None:
        return None
    return str(dim.get("uid") or body.get("label") or "").lower(), v


#: ``devin models list --format json``'s ``cost_summary`` — the only
#: machine-readable price the local CLI publishes (3000.12.43):
#: ``"$8 / 1M Input · $0.2 / 1M Cached input · $40 / 1M Output"``.
_COST_SUMMARY_RE = re.compile(
    r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+Input"
    r".*?\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+Cached\s+input"
    r".*?\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+Output",
    re.DOTALL,
)


def parse_devin_model_prices(
    text: str,
) -> dict[str, tuple[float, float, float]] | None:
    """``{model: (input, cache_read, output) USD/token}`` out of ``devin
    models list --format json``, keyed by ``model_uid`` and by the
    lowercased ``label`` — a step's ATIF ``model_name`` is the uid, the
    file-level ``agent.model_name`` is the label. A variant with no
    ``cost_summary`` prices at zero (``cost_tier: "Free"``); one whose
    summary escapes the known shape is left out — unpriceable is not
    free. ``None`` when nothing parses."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    out: dict[str, tuple[float, float, float]] = {}
    families = data.get("families") if isinstance(data, dict) else None
    for fam in families if isinstance(families, list) else ():
        variants = fam.get("variants") if isinstance(fam, dict) else None
        for v in variants if isinstance(variants, list) else ():
            if not isinstance(v, dict):
                continue
            uid = str(v.get("model_uid") or "").strip()
            if not uid:
                continue
            m = _COST_SUMMARY_RE.search(str(v.get("cost_summary") or ""))
            if m is not None:
                rate = (
                    float(m.group(1)) / 1e6,
                    float(m.group(2)) / 1e6,
                    float(m.group(3)) / 1e6,
                )
            elif str(v.get("cost_tier") or "").strip().lower() == "free":
                rate = (0.0, 0.0, 0.0)
            else:
                continue
            out[uid] = rate
            label = str(v.get("label") or "").strip().lower()
            if label:
                out.setdefault(label, rate)
    return out or None


def devin_model_rate(
    prices: dict[str, tuple[float, float, float]], name: Any
) -> tuple[float, float, float] | None:
    """The ``(input, cache_read, output)`` USD/token rate ``name``
    resolves to in a :func:`parse_devin_model_prices` table — exact
    ``model_uid`` first, then the lowercased label — else ``None``."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip()
    return prices.get(n) or prices.get(n.lower())


def _devin_est_cost_usd(
    data: dict[str, Any],
    prices: dict[str, tuple[float, float, float]],
    *,
    model: str | None,
    label: Any,
) -> float | None:
    """USD the ATIF's tokens price at the account's list rates — each
    billed step at ITS ``model_name`` (the uid it records; the router
    can move a conversation), the file-level totals at the launch
    model's rate when no step prices. ``prompt_tokens`` includes
    ``cached_tokens`` (observed 3000.12.43: a step's 21829 prompt is
    its 10309 input + 11520 cache_read), so the uncached share is the
    difference. A step whose ``model_name`` the table does not name —
    the router billed a model we cannot price — pays the table's
    per-token dearest bound, never the launch rate: an over-estimate
    kills early, an under-estimate overspends (td629583 review).
    ``None`` when there are no tokens to price."""
    launch = devin_model_rate(prices, model) or devin_model_rate(prices, label)
    #: The per-token-kind dearest rate in the table — the bound an
    #: unknown model's tokens price at.
    bound: tuple[float, float, float] = (
        max(r[0] for r in prices.values()),
        max(r[1] for r in prices.values()),
        max(r[2] for r in prices.values()),
    )
    est = 0.0
    saw = False
    steps = data.get("steps")
    for step in steps if isinstance(steps, list) else ():
        if not isinstance(step, dict):
            continue
        mm = step.get("metrics")
        if not isinstance(mm, dict):
            continue
        prompt = _num(mm.get("prompt_tokens")) or 0.0
        cached = _num(mm.get("cached_tokens")) or 0.0
        out = _num(mm.get("completion_tokens")) or 0.0
        if prompt <= 0 and out <= 0:
            continue
        saw = True
        rate = devin_model_rate(prices, step.get("model_name")) or bound
        est += max(prompt - cached, 0.0) * rate[0] + cached * rate[1] + out * rate[2]
    if saw:
        return est
    fm = data.get("final_metrics")
    fm = fm if isinstance(fm, dict) else {}
    prompt = _num(fm.get("total_prompt_tokens")) or 0.0
    cached = _num(fm.get("total_cached_tokens")) or 0.0
    out = _num(fm.get("total_completion_tokens")) or 0.0
    if prompt <= 0 and out <= 0:
        return None
    rate = launch or bound
    return max(prompt - cached, 0.0) * rate[0] + cached * rate[1] + out * rate[2]


def devin_atif_usage(
    path: Path | None,
    *,
    prices: dict[str, tuple[float, float, float]] | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """``meta.usage`` for a ``devin --export`` ATIF file (gr599486
    follow-up): the ``final_metrics`` token totals and — where the plan
    reports cost — ``total_cost_usd``, per-step ``metrics.cost_usd``,
    ``committed_acu_cost``/``committed_credit_cost``, and
    ``extra.response_dimensions`` acu/credit/usd entries. ``None`` when
    no ATIF document reads. The paid plan's export carries token totals
    only, so when ``prices`` (the ``devin models list`` table,
    :func:`parse_devin_model_prices`) resolves a rate the tokens are
    priced at the account's list price into ``cost_usd`` — the estimate
    the spend bound rides (gr628944; a vendor-reported spend figure
    always wins). ``model`` is the launch model the file-level fallback
    prices at."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not str(
        data.get("schema_version") or ""
    ).startswith("ATIF-"):
        return None
    raw_fm = data.get("final_metrics")
    fm = raw_fm if isinstance(raw_fm, dict) else {}
    usage: dict[str, Any] = {}
    for src, dst in (
        ("total_prompt_tokens", "input_tokens"),
        ("total_completion_tokens", "output_tokens"),
        ("total_cached_tokens", "cached_tokens"),
        ("total_steps", "steps"),
    ):
        v = _num(fm.get(src))
        if v is not None:
            usage[dst] = v
    usd_total = _num(fm.get("total_cost_usd"))
    acus_total = _num(fm.get("acus_consumed"))
    if acus_total is None:
        acus_total = _num(fm.get("committed_acu_cost"))
    credits_total = _num(fm.get("committed_credit_cost"))
    acus_committed = 0.0
    credit_committed = 0.0
    steps = data.get("steps")
    for step in steps if isinstance(steps, list) else ():
        if not isinstance(step, dict):
            continue
        metrics = step.get("metrics")
        if isinstance(metrics, dict):
            v = _num(metrics.get("cost_usd"))
            if v is not None:
                usage["cost_usd"] = round(usage.get("cost_usd", 0.0) + v, 6)
        extra = step.get("extra")
        if not isinstance(extra, dict):
            continue
        v = _num(extra.get("committed_acu_cost"))
        if v is not None:
            acus_committed += v
        v = _num(extra.get("committed_credit_cost"))
        if v is not None:
            credit_committed += v
        dims = extra.get("response_dimensions")
        for dim in dims if isinstance(dims, list) else ():
            dv = _dim_value(dim)
            if dv is None:
                continue
            label, value = dv
            if "acu" in label:
                usage["acus_consumed"] = max(usage.get("acus_consumed", 0.0), value)
            elif "credit" in label:
                usage["credit_cost"] = max(usage.get("credit_cost", 0.0), value)
            elif "usd" in label or "cost" in label:
                usage["cost_usd"] = max(usage.get("cost_usd", 0.0), value)
    # Per-message committed sums and cumulative dims overlap — keep the
    # larger of either reading, never both.
    if acus_committed > 0:
        usage["acus_consumed"] = max(usage.get("acus_consumed", 0.0), acus_committed)
    if credit_committed > 0:
        usage["credit_cost"] = max(usage.get("credit_cost", 0.0), credit_committed)
    if usd_total is not None:
        usage["cost_usd"] = max(usage.get("cost_usd", 0.0), usd_total)
    if acus_total is not None:
        usage["acus_consumed"] = max(usage.get("acus_consumed", 0.0), acus_total)
    if credits_total is not None:
        usage["credit_cost"] = max(usage.get("credit_cost", 0.0), credits_total)
    if not usage:
        return None
    session = data.get("session_id")
    if session:
        usage["session_id"] = str(session)
    agent = data.get("agent")
    if isinstance(agent, dict) and agent.get("model_name"):
        usage["model"] = str(agent["model_name"])
    if prices and not any(
        usage.get(k) for k in ("cost_usd", "acus_consumed", "credit_cost")
    ):
        # The paid plan reports tokens only (td601426): price them at the
        # account's list rates so the watch's bound has a number to ride
        # (gr628944). Anything the vendor did report stands.
        est = _devin_est_cost_usd(data, prices, model=model, label=usage.get("model"))
        if est is not None:
            usage["cost_usd"] = round(est, 6)
            usage["cost_basis"] = "devin models list"
    return usage


def _claude_usage(log_text: str) -> dict[str, Any] | None:
    """``meta.usage`` from the claude ``--output-format json`` result
    envelope — ``total_cost_usd`` plus the ``usage`` token counts and
    ``num_turns``/``duration_ms`` it reports."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or "result" not in obj:
            continue
        usage: dict[str, Any] = {}
        v = _num(obj.get("total_cost_usd"))
        if v is not None:
            usage["cost_usd"] = v
        inner = obj.get("usage")
        if isinstance(inner, dict):
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                v = _num(inner.get(key))
                if v is not None:
                    usage[key] = v
        for key in ("num_turns", "duration_ms"):
            v = _num(obj.get(key))
            if v is not None:
                usage[key] = v
        return usage or None
    return None


def _codex_usage(log_text: str) -> dict[str, Any] | None:
    """``meta.usage`` from codex's ``turn.completed`` usage line —
    token counts only; the subscription reports no USD."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "turn.completed":
            continue
        inner = ev.get("usage")
        if not isinstance(inner, dict):
            return None
        usage = {
            k: v
            for k in ("input_tokens", "cached_input_tokens", "output_tokens")
            if (v := _num(inner.get(k))) is not None
        }
        return usage or None
    return None


@dataclass(frozen=True)
class Adapter:
    """One vendor's launch/output protocol; ``models`` in ``argv`` is the
    selected lane's allowlist (required for any explicit Devin model).
    ``mcp`` means write-capable precis MCP (it gates generators and the
    reviewer wording); ``read_mcp`` means the lane can take a scoped
    grant (reader/triage, gr574082) — reads plus the triage write set —
    when ``PRECIS_SLICE_READ_MCP`` names a scope. ``budget_capable``
    means the vendor enforces ``params.budget_usd`` itself — claude's
    ``--max-budget-usd``. ``budget_watch`` means run_slice's own watch
    enforces the bound instead: the adapter's usage channel yields or
    derives spend — devin's paid ATIF carries tokens only, priced at
    the account's ``devin models list`` rates (gr628944). An adapter
    with neither refuses a capped launch rather than run one unbounded
    (codex/kimi report no spend at all — td601426)."""

    name: str
    mcp: bool
    read_mcp: bool = False
    budget_capable: bool = False
    budget_watch: bool = False

    def argv(
        self,
        *,
        model: str | None,
        effort: str,
        tree: Path,
        brief_path: Path,
        deny_paths: tuple[str, ...],
        max_turns: int,
        models: tuple[str, ...] = (),
        writable_roots: tuple[Path, ...] = (),
        read_mcp_url: str | None = None,
        export_path: Path | None = None,
        budget_usd: float | None = None,
    ) -> list[str]:
        """Build argv; effort remains the worker's exported metadata.
        ``writable_roots`` are the sandbox's ``--add-dir`` grants (codex
        only — the other vendors run unsandboxed). ``read_mcp_url`` is
        the checkout's precis MCP URL for a scoped-grant lane: codex
        takes it as ``-c`` dotted overrides (``codex exec -c
        mcp_servers.<name>.url=…``); devin's server entry lives in the
        per-tree config file ``run_slice._devin_mcp_config`` writes, not
        argv. ``export_path`` is the ATIF file ``devin --export`` keeps
        refreshed each turn — the poll reads ``meta.usage`` from it.
        ``budget_usd`` is the node's remaining allowance; only
        :attr:`budget_capable` adapters put it in argv (claude's
        ``--max-budget-usd``) — a :attr:`budget_watch` lane's bound rides
        its usage channel, and run_slice refuses the rest."""
        if self.name == "claude_p":
            argv = [
                claude_bin(),
                "-p",
                "--output-format",
                "json",
                "--max-turns",
                str(max_turns),
            ]
            if model is not None:
                argv += ["--model", model]
            if budget_usd is not None:
                argv += ["--max-budget-usd", str(budget_usd)]
            return argv + [
                "--permission-mode",
                PERMISSION_MODE,
                "--settings",
                deny_settings(deny_paths, tree),
            ]
        if self.name == "codex_exec":
            argv = [
                codex_bin(),
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(tree),
                "-o",
                str(tree / "scratch" / "last-message.md"),
            ]
            for root in writable_roots:
                argv += ["--add-dir", str(root)]
            if read_mcp_url:
                argv += [
                    "-c",
                    f"mcp_servers.precis.url={read_mcp_url}",
                    "-c",
                    "mcp_servers.precis.bearer_token_env_var=PRECIS_MCP_TOKEN",
                ]
            if model is not None:
                argv += ["-m", model]
            return argv + ["--sandbox", "workspace-write", "-"]
        if self.name == "devin_tree":
            if model is not None and model not in models:
                raise ValueError(f"Devin model {model!r} is not in the lane's models")
            argv = [
                devin_bin(),
                "-p",
                "--prompt-file",
                str(brief_path),
                "--permission-mode",
                "dangerous",
                "--respect-workspace-trust",
                "false",
                "--verbosity",
                "1",
            ]
            if model is not None:
                argv += ["--model", model]
            if export_path is not None:
                # ATIF-v1.7, refreshed after each turn — the usage the
                # poll stamps on meta.usage (gr599486 follow-up).
                argv += ["--export", str(export_path)]
            return argv
        if self.name == "kimi_tree":
            # The prompt rides argv, not stdin (``kimi -p``). The model
            # never becomes ``-m`` — it would rebind to the config's
            # managed/OAuth provider; ``KIMI_MODEL_NAME`` carries it.
            return [
                kimi_bin(),
                "-p",
                brief_path.read_text(encoding="utf-8"),
                "--output-format",
                "stream-json",
            ]
        raise KeyError(self.name)

    def stdin(self, brief_path: Path) -> Path | None:
        """Devin and kimi carry the prompt in argv; the other CLIs
        consume stdin."""
        return None if self.name in ("devin_tree", "kimi_tree") else brief_path

    def parse_result(
        self, log_text: str, tree: Path, *, exit_code: int | None = 0
    ) -> tuple[str | None, bool]:
        """Return the final message and vendor error bit; poll also checks
        the process exit code independently for every vendor."""
        if self.name == "claude_p":
            return parse_claude_result(log_text)
        if self.name == "devin_tree":
            return log_text.strip(), exit_code != 0
        if self.name == "kimi_tree":
            return parse_kimi_result(log_text, exit_code=exit_code)
        if self.name == "codex_exec":
            try:
                message = (
                    (tree / "scratch" / "last-message.md")
                    .read_text(encoding="utf-8", errors="replace")
                    .strip()
                )
            except OSError:
                message = ""
            if message:
                return message, False
            # A dead turn reports no agent_message; its ``turn.failed`` /
            # ``error`` event text is the vendor's own failure wording —
            # surfaced as the result so ``run_slice._lane_outage`` can
            # class a usage-limit death as the lane's window, not the
            # task's attempt (td572037).
            errors: list[str] = []
            for line in reversed(log_text.strip().splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "item.completed":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            return text, False
                elif etype == "turn.failed":
                    err = event.get("error")
                    msg = err.get("message") if isinstance(err, dict) else None
                    errors.append(str(msg) if msg else line.strip())
                elif etype == "error":
                    msg = event.get("message")
                    errors.append(str(msg) if msg else line.strip())
            if errors:
                return "\n".join(reversed(errors)), True
            return None, True
        raise KeyError(self.name)

    def usage(
        self,
        log_text: str,
        tree: Path,
        *,
        export_path: Path | None = None,
        prices: dict[str, tuple[float, float, float]] | None = None,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """The vendor's reported usage for ``meta.usage`` (gr599486
        follow-up): devin's ATIF ``--export`` file — live, refreshed
        after each turn — claude's result-envelope ``total_cost_usd`` +
        ``usage``, codex's ``turn.completed`` token counts. ``None``
        where the vendor reports nothing — the spend cap reads
        ``cost_usd``/``acus_consumed``/``on_demand_usd`` only. ``prices``
        is the devin account's per-token list-price table (gr628944);
        ``model`` the launch model its file-level fallback prices at."""
        del tree
        if self.name == "devin_tree":
            return devin_atif_usage(export_path, prices=prices, model=model)
        if self.name == "claude_p":
            return _claude_usage(log_text)
        if self.name == "codex_exec":
            return _codex_usage(log_text)
        return None


ADAPTERS: dict[str, Adapter] = {
    "claude_p": Adapter("claude_p", mcp=True, budget_capable=True),
    "codex_exec": Adapter("codex_exec", mcp=False, read_mcp=True),
    "devin_tree": Adapter("devin_tree", mcp=False, read_mcp=True, budget_watch=True),
    "kimi_tree": Adapter("kimi_tree", mcp=False),
}


def get_adapter(name: str) -> Adapter:
    """Resolve a lane adapter; unknown names raise ``KeyError``."""
    return ADAPTERS[name]
