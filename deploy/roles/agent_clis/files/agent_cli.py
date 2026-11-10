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

For pinnable npm CLIs, ``latest`` resolves via ``npm view <pkg> version``.
Resolve once before comparing or installing, so smoke and rollback use the
same concrete target even if the registry changes during the run. A failed
lookup reports an error without touching the installed version.
Only the last non-empty stdout line supplies the pin; npm warnings on stderr
are diagnostics. Reject prereleases because smoke parses numeric versions,
and refuse ``latest`` for installers that cannot pin a version.

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
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

CLIS = ("claude", "codex", "devin", "kimi")
#: Dirs a non-login ansible shell lacks: Homebrew, user-local, kimi's own.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.kimi-code/bin")
INSTALL_TIMEOUT_S = 600.0
SMOKE_TIMEOUT_S = 120.0
RESOLVE_TIMEOUT_S = 30.0
LAUNCHER_FILES = (
    "scripts/agent-start",
    "scripts/claude-tree",
    "scripts/codex-tree",
    "scripts/kimi-tree",
    "scripts/devin-tree",
    "scripts/lib/agent-tree.sh",
)


class Runner(Protocol):
    def __call__(
        self, argv: list[str], timeout: float, *, stdout_only: bool = False
    ) -> tuple[int, str]: ...


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


def run_command(
    argv: list[str], timeout: float, *, stdout_only: bool = False
) -> tuple[int, str]:
    """Return rc and output; stdout_only omits stderr on success, keeps errors."""
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
    out = proc.stdout
    if not stdout_only or proc.returncode != 0:
        out += proc.stderr
    return proc.returncode, out.strip()


def extend_path() -> None:
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [os.path.expanduser(p) for p in EXTRA_PATH]
    os.environ["PATH"] = os.pathsep.join([*[p for p in extra if p not in have], *have])


def _resolve_latest(cfg: dict[str, Any], run: Runner) -> tuple[int, str]:
    """Resolve the npm package named by the installer to a concrete pin."""
    argv = shlex.split(cfg["install"])
    packages = [
        arg.removesuffix("@{version}") for arg in argv if arg.endswith("@{version}")
    ]
    if argv[:2] != ["npm", "install"] or len(packages) != 1:
        return 2, "latest requires an npm installer with one package@{version}"
    rc, out = run(
        ["npm", "view", packages[0], "version"], RESOLVE_TIMEOUT_S, stdout_only=True
    )
    if rc != 0:
        return rc, f"npm view failed (rc={rc}): {out[:160]}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return 2, "npm view returned an empty version"
    out = lines[-1]
    if not _SAFE_VERSION.fullmatch(out) or parse_version(out) != out:
        return 2, f"npm view returned an invalid version: {out[:160]!r}"
    return 0, out


def probe(cli: str, cfg: dict[str, Any], run: Runner, find: Finder) -> dict[str, Any]:
    """One CLI's installed state against its pin and vendor minimum."""
    path = find(cfg.get("bin") or cli)
    version = None
    if path:
        rc, out = run([path, "--version"], 30.0)
        version = parse_version(out) if rc == 0 else None
    pin, minimum = str(cfg.get("pin") or ""), str(cfg.get("min") or "")
    row: dict[str, Any] = {
        "cli": cli,
        "path": path,
        "version": version,
        "pin": pin,
        "min": minimum,
        "status": classify(version, pin, minimum),
    }
    if pin == "latest":
        row["pin_source"] = "latest"
        rc, out = (
            _resolve_latest(cfg, run)
            if cfg.get("pinnable", True)
            else (2, "latest needs a pinnable CLI")
        )
        if rc != 0:
            row.update(
                status="resolve-failed",
                action="failed",
                ok=False,
                detail=f"resolve latest failed: {out}",
            )
        else:
            row.update(pin=out, status=classify(version, out, minimum))
    return row


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
    if before.get("ok") is False:
        return {**before, "previous": before["version"]}
    pin = before["pin"]
    cfg = {**cfg, "pin": pin}
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
        if r["status"] != "resolve-failed":
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
        if r.get("pin_source") == "latest":
            line += " (from latest)"
        if "action" in r:
            line += f" · {r['action']}" + (f" ({r['detail']})" if r["detail"] else "")
        lines.append(line)
    return lines


def manage_launcher(
    settings: dict[str, Any], mode: str, *, home: Path | None = None
) -> dict[str, Any]:
    """Converge the user command onto committed code in a stable checkout.

    Remote execution uses a temporary uploaded script, so neither cwd nor
    __file__ identifies the source. Existing wrapper symlinks are discovery
    hints; an explicit inventory checkout resolves missing/ambiguous hints.
    Reports never create directories, execute agents or change links.
    """
    home = home or Path.home()
    destination = home / ".local/bin/agent-start"
    row: dict[str, Any] = {
        "cli": "agent-start",
        "path": str(destination),
        "version": None,
        "pin": "",
        "min": "",
        "status": "missing",
        "action": "failed",
        "ok": False,
        "detail": "",
    }
    try:
        if destination.exists() and not destination.is_symlink():
            raise ValueError(f"refusing to replace user file/directory: {destination}")
        explicit = str(settings.get("checkout") or "")
        if explicit:
            checkout = Path(explicit).expanduser().resolve(strict=True)
        else:
            roots = set()
            for name, script in (
                ("kimi-wt", "kimi-tree"),
                ("devin-wt", "devin-tree"),
                ("agent-start", "agent-start"),
            ):
                wrapper = destination.parent / name
                if not wrapper.is_symlink():
                    continue
                target = wrapper.resolve()
                root = target.parent.parent
                if (
                    target.name == script
                    and target.parent.name == "scripts"
                    and (root / ".git").is_dir()
                ):
                    roots.add(root)
            if len(roots) != 1:
                raise ValueError(
                    "set agent_cli_launcher_checkout: "
                    + (
                        "wrapper checkouts disagree"
                        if roots
                        else "no stable wrapper checkout found"
                    )
                )
            checkout = roots.pop()
        if not (checkout / ".git").is_dir():
            raise ValueError(
                "launcher source must be a primary checkout, not a task worktree"
            )
        for relative in LAUNCHER_FILES:
            source = checkout / relative
            if (
                source.is_symlink()
                or not source.is_file()
                or (
                    relative != "scripts/lib/agent-tree.sh"
                    and not os.access(source, os.X_OK)
                )
            ):
                raise ValueError(
                    f"shipped launcher dependency missing/not executable: {source}"
                )
            rc, _ = run_command(
                [
                    "git",
                    "--no-optional-locks",
                    "-C",
                    str(checkout),
                    "cat-file",
                    "-e",
                    f"HEAD:{relative}",
                ],
                30.0,
            )
            if rc:
                raise ValueError(f"launcher dependency is not committed: {relative}")
        rc, _ = run_command(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(checkout),
                "diff",
                "--quiet",
                "HEAD",
                "--",
                *LAUNCHER_FILES,
            ],
            30.0,
        )
        if rc:
            raise ValueError(
                "launcher source differs from committed code; ship it before installing"
            )
        target = checkout / "scripts/agent-start"
        row["detail"] = f"target={target}"
        if destination.is_symlink() and destination.resolve() == target:
            return {**row, "status": "ok", "action": "current", "ok": True}
        row["status"] = "drift" if destination.is_symlink() else "missing"
        if mode == "report":
            return {**row, "action": "needs-update"}
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_symlink():
            # symlink_to fails on a concurrently created user file.
            destination.symlink_to(target)
        else:
            with tempfile.TemporaryDirectory(
                prefix=".agent-start-", dir=destination.parent
            ) as scratch:
                replacement = Path(scratch) / "agent-start"
                replacement.symlink_to(target)
                if not destination.is_symlink():
                    raise ValueError(
                        f"destination changed during installation: {destination}"
                    )
                replacement.replace(destination)
        return {**row, "status": "ok", "action": "updated", "ok": True}
    except (OSError, ValueError) as exc:
        return {**row, "detail": str(exc)}


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
    launcher = (
        manage_launcher(config["launcher"], args.mode) if "launcher" in config else None
    )
    # Refuse a bad source before running vendor installers. Reporting vendor
    # state remains useful even when the managed command cannot converge.
    operation = (
        update
        if args.mode == "update" and (launcher is None or launcher["ok"])
        else report
    )
    rows = operation(config, run, find)
    if launcher is not None:
        rows.append(launcher)
    lines = [*render(rows), *alerts(rows)]
    print("\n".join(lines))
    print(
        "AGENT_CLI_JSON "
        + json.dumps({"host": socket.gethostname(), "mode": args.mode, "rows": rows})
    )
    if args.mode == "update":
        return 0 if all(r.get("ok", True) for r in rows) else 1
    return 1 if args.strict and any(a.startswith("ALERT") for a in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
