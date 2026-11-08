"""envelope boundary probe (td574934 A4, gr596928).

Builds the agent argv every *named* envelope
(:data:`precis.workers.envelope.ENVELOPES`) produces on the in-process path
(:func:`precis.utils.claude_agent._resolve_agent_args`) and on the
container path (:func:`precis.workers.executors.agent_container.containerize_claude_argv`),
and reports a breach when the fence is open:

* ``agent-mcp.json`` — the container-baked precis MCP config — in a
  produced argv (a ``builtin_tools`` box is a NO-MCP box; the name only
  ever reaches it through ``_rebase_mcp_config`` re-opening it);
* a DSN or vault variable — a ``postgres://`` literal, ``PG*``/
  ``*_DATABASE_URL``/``*_DSN`` connect names, or a credential-shaped
  ``NAME=value`` (and, in a fenced argv, even a by-key ``--env NAME``
  entry: the key alone already grants the secret);
* a missing ``--strict-mcp-config`` guard.

For a ``builtin_tools`` (fenced) envelope the container path itself must
be refused: ``containerize_claude_argv`` is the pure seam a caller could
reach for, so the probe requires BOTH refusals that exist today — the
``require_container`` chokepoint in :func:`call_claude_agent` raising,
and the exec-boundary check :func:`_assert_fenced_argv` rejecting the
containerized argv the seam emits.

Contract (shared by every ``scripts/probes/*.py``): exit 0 pass, exit 1
boundary breach with the finding on stdout, anything else (here exit 2)
= cannot-run. ``argv[1]`` names the tree root whose ``src/`` is probed
(default: cwd — the job mounts the branch worktree as the probe's cwd).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: A credential-shaped env name in an argv token — vault vars, vendor
#: keys/tokens and the libpq connect family (a DSN's parts).
_SECRETY = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "DSN",
    "DATABASE_URL",
    "CRED",
    "VAULT",
)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: An inline DSN literal — secret material wherever it appears.
_DSN_VALUE_RE = re.compile(r"postgres(?:ql)?://")


def _secret_name(name: str) -> bool:
    """``name`` is a credential-shaped env var name."""
    return bool(_ENV_NAME_RE.fullmatch(name)) and (
        name.startswith("PG") or any(s in name for s in _SECRETY)
    )


def _scan(name: str, where: str, argv: list[str], *, fenced: bool) -> list[str]:
    """The three fail conditions over one produced argv."""
    findings: list[str] = []
    toks = [str(t) for t in argv]
    if any("agent-mcp.json" in t for t in toks):
        findings.append(
            f"envelope {name} {where}: argv names agent-mcp.json — the "
            "container-baked precis MCP config would re-open the box"
        )
    if "--strict-mcp-config" not in toks:
        findings.append(
            f"envelope {name} {where}: argv carries no --strict-mcp-config guard"
        )
    for tok in toks:
        var, eq, _val = tok.partition("=")
        if eq and (_secret_name(var) or _DSN_VALUE_RE.search(tok)):
            findings.append(
                f"envelope {name} {where}: argv carries secret material: {var}=…"
            )
        elif not eq and _DSN_VALUE_RE.search(tok):
            findings.append(
                f"envelope {name} {where}: argv carries a DSN literal: {tok[:40]}…"
            )
        elif fenced and not eq and _secret_name(tok):
            findings.append(
                f"envelope {name} {where}: fenced argv requests secret {tok} "
                "(a by-key --env still smuggles the value into the box)"
            )
    return findings


def check(root: Path) -> list[str]:
    """Every finding; empty = pass."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from precis.utils import claude_agent
    from precis.workers import envelope as env_mod
    from precis.workers.executors import agent_container

    findings: list[str] = []
    for name in sorted(env_mod.ENVELOPES):
        env = env_mod.ENVELOPES[name]
        fenced = env.builtin_tools is not None
        _bin, args, _model, _timeout, _usd, _active = claude_agent._resolve_agent_args(
            "boundary probe",
            model="boundary-probe",
            system_prompt=None,
            mcp_config=None,
            max_turns=1,
            timeout_s=1.0,
            max_usd=0.01,
            permission_mode="bypassPermissions",
            output_format="text",
            bare=False,
            disallowed_tools=(),
            envelope=env,
            extra_args=(),
            settings_extra=None,
            gated=False,
        )
        findings += _scan(name, "in-proc", args, fenced=fenced)
        if fenced:
            try:
                claude_agent._assert_fenced_argv(args)
            except Exception as exc:
                findings.append(
                    f"envelope {name} in-proc: _assert_fenced_argv rejects the "
                    f"argv _resolve_agent_args built: {exc}"
                )
        try:
            cargv = agent_container.containerize_claude_argv(
                args, env, name="precis-boundary-probe", model="boundary-probe"
            )
        except Exception:
            cargv = None  # the seam itself refuses — an acceptable fence shape
        if fenced:
            if cargv is not None:
                try:
                    claude_agent._assert_fenced_argv(cargv)
                except Exception:
                    pass  # the exec-boundary check caught it — the fence held
                else:
                    findings.append(
                        f"envelope {name} container: the containerized argv "
                        "passes _assert_fenced_argv — a fenced run would exec "
                        "unboxed"
                    )
                    findings += _scan(name, "container", cargv, fenced=True)
            # The live chokepoint must refuse a fenced run on the container
            # path — any raise is a refusal; a returned AgentResult would be
            # an exec'd, unboxed agent (unreachable while require_container
            # raises before spawn on every path).
            try:
                claude_agent.call_claude_agent(
                    "boundary probe",
                    model="boundary-probe",
                    max_turns=1,
                    max_usd=0.01,
                    timeout_s=1.0,
                    envelope=env,
                    require_container=True,
                    bare=True,
                    env_base={"PATH": os.environ.get("PATH", "")},
                )
            except Exception:
                pass  # refused — correct
            else:
                findings.append(
                    f"envelope {name}: a fenced run reached exec on the "
                    "container path — require_container did not refuse"
                )
        elif cargv is not None:
            findings += _scan(name, "container", cargv, fenced=False)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Probe entry: ``[tree_root]``; 0 pass, 1 breach, 2 cannot-run."""
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        findings = check(root)
    except Exception as exc:
        print(f"cannot-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
