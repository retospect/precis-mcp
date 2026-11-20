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
rollback by pin. A recognized missing login/model keeps the verified binary,
reports installed, needs login and exits 2 (failures take precedence).
No CLI auth file is read or copied.

For pinnable npm CLIs, ``latest`` resolves via ``npm view <pkg> version``.
Resolve once before comparing or installing, so smoke and rollback use the
same concrete target even if the registry changes during the run. A failed
lookup reports an error without touching the installed version.
Only the last non-empty stdout line supplies the pin; npm warnings on stderr
are diagnostics. Reject prereleases because smoke parses numeric versions,
and refuse ``latest`` for installers that cannot pin a version.

Where a CLI is looked for, and who installs it. The role runs this script
as ``agent_cli_user`` (ansible ``become_user``), the account the slice lanes
run as, so HOME, ``~/.local/bin`` and ``~/.kimi-code/bin`` are that user's.
The lanes resolve ``devin`` as ``~/.local/bin/devin`` (PRECIS_DEVIN_BIN) or on
PATH and ``kimi`` on PATH or at ``~/.kimi-code/bin/kimi`` (``_slice_adapters``);
detection mirrors that with each CLI's ``fallback_bins``, so a CLI the lanes
can run is never reported missing because PATH lacks it. Installs, version
and smoke runs all happen as that user. When the global npm prefix is not
writable by it (a least-privilege user), npm installs go to ``~/.local``.
Installer choice (``pick_install``): devin absent -> ``fresh_install`` (the
vendor script; ``devin update`` cannot install from nothing), present ->
``install`` (``devin update``). kimi: ``install`` (npm) while node >= 22.19 is
present and the kimi found is not under ``script_dir`` (``~/.kimi-code``),
otherwise ``script_install`` (the vendor install.sh, version-pinnable).

Output: one human line per CLI, ``ALERT`` lines for a host behind its pin
(or below the vendor minimum) and a final ``AGENT_CLI_JSON {...}`` line the
role parses. ``report`` exits 0 unless ``--strict`` and an alert is raised.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
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
NODE_MIN_FOR_NPM = (22, 19)  # @moonshot-ai/kimi-code needs node >= 22.19
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


def user_home() -> str:
    """The home of the account this process runs as: $HOME when this user
    owns it, else the passwd entry (ansible's become may leave the invoking
    user's HOME in place)."""
    env = os.environ.get("HOME")
    try:
        if env and os.stat(env).st_uid == os.geteuid():
            return env
    except OSError:
        pass
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


def _under_home(path: str, home: str) -> str:
    return home + path[1:] if path == "~" or path.startswith("~/") else path


def home_config(config: dict[str, Any], home: str) -> dict[str, Any]:
    """Resolve each CLI's ``~`` paths against ``home``."""
    clis = {}
    for name, cfg in config["clis"].items():
        cfg = dict(cfg)
        cfg["fallback_bins"] = [
            _under_home(p, home) for p in cfg.get("fallback_bins") or []
        ]
        if cfg.get("script_dir"):
            cfg["script_dir"] = _under_home(cfg["script_dir"], home)
        cfg["home"] = home
        clis[name] = cfg
    return {**config, "clis": clis}


def make_finder(config: dict[str, Any], which: Finder = shutil.which) -> Finder:
    """PATH first, then each CLI's ``fallback_bins`` — the order the lanes use
    (``kimi`` on PATH, else ~/.kimi-code/bin/kimi; devin from ~/.local/bin)."""
    fallbacks = {
        (cfg.get("bin") or name): cfg.get("fallback_bins") or []
        for name, cfg in config["clis"].items()
    }

    def find(name: str) -> str | None:
        found = which(name)
        if found:
            return found
        for candidate in fallbacks.get(name, []):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    return find


def extend_path() -> None:
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [os.path.expanduser(p) for p in EXTRA_PATH]
    os.environ["PATH"] = os.pathsep.join([*[p for p in extra if p not in have], *have])


def _node_ok(run: Runner) -> bool:
    """node >= 22.19 is on PATH (what the npm kimi-code package needs)."""
    rc, out = run(["node", "--version"], 15.0, stdout_only=True)
    version = parse_version(out) if rc == 0 else None
    return version is not None and _key(version)[:2] >= NODE_MIN_FOR_NPM


def _under(path: str, directory: str) -> bool:
    """``path`` (or what it links to) lives under ``directory``."""
    root = os.path.abspath(directory)
    return any(
        os.path.commonpath([p, root]) == root
        for p in (os.path.abspath(path), os.path.realpath(path))
    )


def pick_install(cfg: dict[str, Any], path: str | None, run: Runner) -> tuple[str, str]:
    """The installer command for this host and its kind: ``npm``, ``update``
    (the CLI updating itself) or ``script`` (a vendor install script)."""
    fresh = cfg.get("fresh_install")
    if fresh and not path:
        return fresh, "script"
    script = cfg.get("script_install")
    if script:
        vendor = cfg.get("script_dir") or ""
        if (path and vendor and _under(path, vendor)) or not _node_ok(run):
            return script, "script"
    install = cfg["install"]
    return install, "npm" if install.startswith("npm ") else "update"


def _resolve_latest(
    cfg: dict[str, Any], run: Runner, path: str | None = None
) -> tuple[int, str]:
    """Resolve the version the chosen installer would install to a concrete pin."""
    command, kind = pick_install(cfg, path, run)
    if kind == "script":
        if not cfg.get("script_latest"):
            return 2, "latest needs an installer with a version lookup"
        rc, out = run(_shell(cfg["script_latest"]), RESOLVE_TIMEOUT_S, stdout_only=True)
        what = "latest lookup"
    else:
        argv = shlex.split(command)
        packages = [
            arg.removesuffix("@{version}") for arg in argv if arg.endswith("@{version}")
        ]
        if argv[:2] != ["npm", "install"] or len(packages) != 1:
            return 2, "latest requires an npm installer with one package@{version}"
        rc, out = run(
            ["npm", "view", packages[0], "version"],
            RESOLVE_TIMEOUT_S,
            stdout_only=True,
        )
        what = "npm view"
    if rc != 0:
        return rc, f"{what} failed (rc={rc}): {out[:160]}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return 2, f"{what} returned an empty version"
    out = lines[-1]
    if not _SAFE_VERSION.fullmatch(out) or parse_version(out) != out:
        return 2, f"{what} returned an invalid version: {out[:160]!r}"
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
            _resolve_latest(cfg, run, path)
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


def _npm_prefix_writable(run: Runner) -> bool:
    """The global npm prefix is writable (or unknown: keep npm's own choice)."""
    rc, out = run(["npm", "prefix", "-g"], 15.0, stdout_only=True)
    lines = out.splitlines()
    return rc != 0 or not lines or os.access(lines[-1].strip(), os.W_OK)


def _shell(command: str) -> list[str]:
    return ["bash", "-o", "pipefail", "-c", command]


def _install(
    cfg: dict[str, Any], version: str, run: Runner, path: str | None = None
) -> tuple[int, str]:
    """Run the installer ``pick_install`` chooses; ``path`` is the CLI as found
    now (None = not installed). The version string is validated first, so it
    cannot carry shell syntax into a script installer."""
    if not _SAFE_VERSION.match(version):
        return 2, f"unsafe version string {version!r}"
    command, kind = pick_install(cfg, path, run)
    command = command.replace("{version}", version)
    argv = _shell(command) if "|" in command else shlex.split(command)
    if kind == "update" and path and argv[0] == cfg.get("bin"):
        argv[0] = path  # the user's copy may be off PATH
    if kind == "npm" and cfg.get("home") and not _npm_prefix_writable(run):
        # A least-privilege user cannot write the global prefix: keep the
        # install in its own ~/.local (bin dir is on the detection path).
        argv[argv.index("-g") + 1 : argv.index("-g") + 1] = [
            "--prefix",
            f"{cfg['home']}/.local",
        ]
    return run(argv, INSTALL_TIMEOUT_S)


def _smoke(
    cfg: dict[str, Any], pin: str, path: str, prompt: bool, run: Runner
) -> tuple[str | None, bool]:
    """Failure and whether only login/configuration is missing.

    None when the CLI passes (``--version`` names the pin, one headless
    prompt answers), else the failure."""
    rc, out = run([path, "--version"], 30.0)
    if rc != 0 or parse_version(out) != pin:
        return f"--version gave rc={rc} {out[:120]!r}, wanted {pin}", False
    if prompt and cfg.get("smoke"):
        rc, out = run([path, *cfg["smoke"]], SMOKE_TIMEOUT_S)
        needs_login = any(
            text in out.casefold()
            for text in (
                "not logged in",
                "no model configured",
                "please run /login",
                "run `codex login`",
            )
        )
        if needs_login:
            return f"headless prompt rc={rc} {out[:120]!r}", True
        if rc != 0 or not out:
            return f"headless prompt rc={rc} {out[:120]!r}", False
    return None, False


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
    rc, out = _install(cfg, prev, run, row.get("path"))
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
    rc, out = _install(cfg, pin, run, before["path"])
    if rc != 0:
        return _roll_back(
            cli, cfg, row, run, find, f"install of {pin} failed: {out[:160]!r}"
        )
    after = probe(cli, cfg, run, find)
    row.update(path=after["path"], version=after["version"])
    if after["path"] is None:
        return _roll_back(cli, cfg, row, run, find, "binary missing after install")
    fault, needs_login = _smoke(cfg, pin, after["path"], smoke_prompt, run)
    if needs_login:
        binary = cfg.get("bin") or cli
        login = cfg.get("login") or {
            "claude": "claude /login",
            "codex": "codex login",
            "kimi": "kimi (then /login)",
        }.get(cli, f"{binary} (then /login)")
        row.update(
            action="updated-needs-login",
            detail=f"installed, needs login: run {login} as user {pwd.getpwuid(os.geteuid()).pw_name}; {fault}",
        )
        return row
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
    home = user_home()
    os.environ["HOME"] = home
    config = home_config(config, home)
    if find is shutil.which:
        find = make_finder(config)
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
        if not all(r.get("ok", True) for r in rows):
            return 1
        return 2 if any(r.get("action") == "updated-needs-login" for r in rows) else 0
    return 1 if args.strict and any(a.startswith("ALERT") for a in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
