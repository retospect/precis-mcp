"""agent_cli.py — report and update the vendor agent CLIs on THIS host (gr543827).

Run by roles/agent_clis through ansible's ``script`` module (stdlib only, no
install on the host). One door for claude, codex, devin and kimi:

    agent_cli.py report --config-json '<json>'   read-only: version vs pin
    agent_cli.py update --config-json '<json>'   install each pin, smoke, roll back

The config comes from the role (pins in the overlay or role defaults)::

    {"clis": {"claude": {"bin": "claude", "pin": "2.1.143", "min": "",
                         "install": "npm install -g pkg@{version}",
                         "pinnable": true, "smoke": ["-p", "Reply OK"]}, ...},
     "only": ["claude"], "smoke_prompt": true}

``update`` per CLI: installed == pin -> nothing. Otherwise run the vendor
installer for the pin, then smoke it: ``--version`` names the pin and (when
``smoke_prompt``) one headless prompt exits 0 with output. A failed install or
smoke reinstalls the previous version (vendor installers that cannot pin a
version, ``pinnable: false``, are reported with no rollback) and the run
exits 1. Restoring the previous pin and running ``update`` again is the
rollback by pin. No CLI auth file is read or copied.

Output: one human line per CLI, ``ALERT`` lines for a host behind its pin
(or below the vendor minimum) and a final ``AGENT_CLI_JSON {...}`` line the
role parses. ``report`` exits 0 unless ``--strict`` and an alert is raised.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from typing import Any

CLIS = ("claude", "codex", "devin", "kimi")
#: Dirs a non-login ansible shell lacks: Homebrew, user-local, kimi's own.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.kimi-code/bin")
INSTALL_TIMEOUT_S = 600.0
SMOKE_TIMEOUT_S = 120.0

Runner = Callable[[list[str], float], tuple[int, str]]
Finder = Callable[[str], str | None]

_VERSION = re.compile(r"\d+(?:\.\d+)+")
_SAFE_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")


def parse_version(text: str) -> str | None:
    """First dotted version in ``--version`` output, else None."""
    m = _VERSION.search(text)
    return m.group(0) if m else None


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def classify(version: str | None, pin: str, minimum: str = "") -> str:
    """missing | below-min | unpinned | ok | behind | ahead."""
    if version is None:
        return "missing"
    if minimum and _key(version) < _key(minimum):
        return "below-min"
    if not pin:
        return "unpinned"
    if version == pin:
        return "ok"
    return "behind" if _key(version) < _key(pin) else "ahead"


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    """One subprocess: (exit code, stdout+stderr). 127 not found, 124 timeout."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def extend_path() -> None:
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [os.path.expanduser(p) for p in EXTRA_PATH]
    os.environ["PATH"] = os.pathsep.join([*[p for p in extra if p not in have], *have])


def probe(cli: str, cfg: dict[str, Any], run: Runner, find: Finder) -> dict[str, Any]:
    """One CLI's installed state against its pin and vendor minimum."""
    path = find(cfg.get("bin") or cli)
    version = None
    if path:
        rc, out = run([path, "--version"], 30.0)
        version = parse_version(out) if rc == 0 else None
    pin, minimum = str(cfg.get("pin") or ""), str(cfg.get("min") or "")
    return {
        "cli": cli,
        "path": path,
        "version": version,
        "pin": pin,
        "min": minimum,
        "status": classify(version, pin, minimum),
    }


def report(
    config: dict[str, Any], run: Runner = run_command, find: Finder = shutil.which
) -> list[dict[str, Any]]:
    only = config.get("only") or list(CLIS)
    return [probe(c, config["clis"][c], run, find) for c in CLIS if c in only]


def _install(cfg: dict[str, Any], version: str, run: Runner) -> tuple[int, str]:
    if not _SAFE_VERSION.match(version):
        return 2, f"unsafe version string {version!r}"
    return run(shlex.split(cfg["install"].format(version=version)), INSTALL_TIMEOUT_S)


def _smoke(
    cfg: dict[str, Any], pin: str, path: str, prompt: bool, run: Runner
) -> str | None:
    """None when the CLI passes (``--version`` names the pin, one headless
    prompt answers), else the failure."""
    rc, out = run([path, "--version"], 30.0)
    if rc != 0 or parse_version(out) != pin:
        return f"--version gave rc={rc} {out[:120]!r}, wanted {pin}"
    if prompt and cfg.get("smoke"):
        rc, out = run([path, *cfg["smoke"]], SMOKE_TIMEOUT_S)
        if rc != 0 or not out:
            return f"headless prompt rc={rc} {out[:120]!r}"
    return None


def _roll_back(
    cli: str,
    cfg: dict[str, Any],
    row: dict[str, Any],
    run: Runner,
    find: Finder,
    why: str,
) -> dict[str, Any]:
    prev = row["previous"]
    row.update(ok=False, detail=why)
    if not prev or not cfg.get("pinnable", True):
        row["action"] = "failed"
        row["detail"] += (
            " — no rollback (previous version unknown or installer not version-pinnable)"
        )
        return row
    rc, out = _install(cfg, prev, run)
    now = probe(cli, cfg, run, find)["version"]
    row["version"] = now
    if rc == 0 and now == prev:
        row["action"] = "rolled-back"
    else:
        row["action"] = "rollback-failed"
        row["detail"] += f" — rollback to {prev} also failed ({out[:120]!r})"
    return row


def update_one(
    cli: str,
    cfg: dict[str, Any],
    *,
    explicit: bool,
    smoke_prompt: bool,
    run: Runner,
    find: Finder,
) -> dict[str, Any]:
    """Install one CLI's pin, smoke it, roll back on failure."""
    before = probe(cli, cfg, run, find)
    pin = before["pin"]
    row: dict[str, Any] = {
        **before,
        "previous": before["version"],
        "action": "current",
        "ok": True,
        "detail": "",
    }
    if not pin:
        row["action"] = "refused" if explicit else "skipped"
        row["ok"] = not explicit
        row["detail"] = (
            f"no pin: set agent_cli_pin_{cli} in the overlay or role defaults"
        )
        return row
    if before["version"] == pin:
        return row
    rc, out = _install(cfg, pin, run)
    if rc != 0:
        return _roll_back(
            cli, cfg, row, run, find, f"install of {pin} failed: {out[:160]!r}"
        )
    after = probe(cli, cfg, run, find)
    row.update(path=after["path"], version=after["version"])
    if after["path"] is None:
        return _roll_back(cli, cfg, row, run, find, "binary missing after install")
    fault = _smoke(cfg, pin, after["path"], smoke_prompt, run)
    if fault:
        return _roll_back(cli, cfg, row, run, find, f"smoke failed: {fault}")
    row.update(action="updated", status=classify(after["version"], pin, row["min"]))
    return row


def update(
    config: dict[str, Any], run: Runner = run_command, find: Finder = shutil.which
) -> list[dict[str, Any]]:
    only = config.get("only") or []
    rows = [
        update_one(
            c,
            config["clis"][c],
            explicit=bool(only),
            smoke_prompt=bool(config.get("smoke_prompt", True)),
            run=run,
            find=find,
        )
        for c in CLIS
        if not only or c in only
    ]
    for r in rows:
        r["status"] = classify(r["version"], r["pin"], r["min"])
    return rows


def alerts(rows: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in rows:
        if r.get("ok") is False:
            out.append(f"ALERT {r['cli']}: {r['action']} — {r['detail']}")
        elif r["status"] in ("behind", "below-min") or (
            r["status"] == "missing" and r["pin"]
        ):
            out.append(
                f"ALERT {r['cli']}: {r['status']} "
                f"(installed {r['version'] or 'none'}, pin {r['pin'] or 'none'}"
                + (f", vendor minimum {r['min']}" if r["min"] else "")
                + ")"
            )
    return out


def render(rows: list[dict[str, Any]]) -> list[str]:
    lines = []
    for r in rows:
        line = (
            f"{r['cli']:<7} installed={r['version'] or '-':<12} "
            f"pin={r['pin'] or '-':<12} {r['status']}"
        )
        if "action" in r:
            line += f" · {r['action']}" + (f" ({r['detail']})" if r["detail"] else "")
        lines.append(line)
    return lines


def main(
    argv: list[str] | None = None,
    run: Runner = run_command,
    find: Finder = shutil.which,
) -> int:
    ap = argparse.ArgumentParser(prog="agent_cli.py", description=__doc__)
    ap.add_argument("mode", choices=("report", "update"))
    ap.add_argument("--config-json", required=True)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    config = json.loads(args.config_json)
    extend_path()
    rows = (update if args.mode == "update" else report)(config, run, find)
    lines = [*render(rows), *alerts(rows)]
    print("\n".join(lines))
    print(
        "AGENT_CLI_JSON "
        + json.dumps({"host": socket.gethostname(), "mode": args.mode, "rows": rows})
    )
    if args.mode == "update":
        return 0 if all(r["ok"] for r in rows) else 1
    return 1 if args.strict and any(a.startswith("ALERT") for a in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
