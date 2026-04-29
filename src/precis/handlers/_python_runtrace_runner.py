"""Subprocess runner for ``view='runtrace'``.

Invoked by `_python_runtrace.run_trace` as a separate Python process.
Imports an entry point, installs `sys.setprofile`, calls the entry,
and writes captured events as JSON to a file the parent reads.

This module is **NEVER imported** by precis itself at runtime — it
only runs as a script in a subprocess. Keeping it isolated means
the profiler doesn't accidentally trace precis's own code, and the
runtime cost of the import side-effect chain is bounded to the
subprocess.

Invocation:

  python /path/to/_python_runtrace_runner.py \\
      --entry pkg.mod:func \\
      --output /tmp/trace.json \\
      --max-events 10000 \\
      -- arg1 arg2 ...

Output JSON shape::

  {
    "events":    [{"event": "call", "qn": "pkg.mod.func", "t": 0.0}, ...],
    "truncated": false,
    "exception": null,
    "exit_code": 0,
    "elapsed_s": 0.018,
  }

`event` is one of: 'call', 'return', 'c_call', 'c_return'.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="precis runtrace runner")
    parser.add_argument("--entry", required=True, help="pkg.mod:func or pkg.mod.func")
    parser.add_argument("--output", required=True, help="JSON output path")
    parser.add_argument("--max-events", type=int, default=10_000)
    parser.add_argument(
        "--syspath", default="", help="extra sys.path entries (os.pathsep separated)"
    )
    parser.add_argument(
        "argv", nargs=argparse.REMAINDER, help="-- argv to forward to the entry"
    )
    args = parser.parse_args(argv)

    # Strip the '--' marker if present.
    forwarded_argv = args.argv
    if forwarded_argv and forwarded_argv[0] == "--":
        forwarded_argv = forwarded_argv[1:]

    # Apply --syspath BEFORE importing the entry.
    if args.syspath:
        import os

        for p in args.syspath.split(os.pathsep):
            if p and p not in sys.path:
                sys.path.insert(0, p)

    # Resolve entry → callable.
    try:
        module_name, attr_name = _split_entry(args.entry)
    except ValueError as e:
        return _write_failure(args.output, f"invalid entry {args.entry!r}: {e}")

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        return _write_failure(args.output, f"could not import {module_name!r}: {e}")

    try:
        func = getattr(module, attr_name)
    except AttributeError:
        return _write_failure(
            args.output, f"module {module_name!r} has no attribute {attr_name!r}"
        )

    if not callable(func):
        return _write_failure(
            args.output, f"{args.entry!r} is not callable (got {type(func).__name__})"
        )

    # Set sys.argv to entry + forwarded argv. Convention: argv[0] is
    # the module name (mirrors how setuptools console scripts work).
    sys.argv = [module_name] + list(forwarded_argv)

    # ------------------------------------------------------------------
    # Profiler setup
    # ------------------------------------------------------------------

    events: list[dict[str, Any]] = []
    max_events = args.max_events
    truncated = [False]
    start = time.perf_counter()

    def profiler(frame, event, arg):
        if truncated[0]:
            return
        if len(events) >= max_events:
            truncated[0] = True
            return

        ts = time.perf_counter() - start
        if event == "call":
            qn = _frame_qualname(frame)
            if qn is not None:
                events.append({"event": "call", "qn": qn, "t": ts})
        elif event == "return":
            qn = _frame_qualname(frame)
            if qn is not None:
                events.append({"event": "return", "qn": qn, "t": ts})
        elif event == "c_call":
            qn = _c_qualname(arg)
            if qn is not None:
                events.append({"event": "c_call", "qn": qn, "t": ts})
        elif event == "c_return":
            qn = _c_qualname(arg)
            if qn is not None:
                events.append({"event": "c_return", "qn": qn, "t": ts})

    sys.setprofile(profiler)
    exc_str: str | None = None
    exit_code = 0
    try:
        try:
            func()
        except SystemExit as e:
            # Treat sys.exit(N) as completion. argparse uses this for --help.
            try:
                exit_code = int(e.code) if isinstance(e.code, int) else 0
            except (TypeError, ValueError):
                exit_code = 0
        except BaseException as e:
            exc_str = f"{type(e).__name__}: {e}"
            exit_code = 1
    finally:
        sys.setprofile(None)

    elapsed = time.perf_counter() - start

    payload = {
        "events": events,
        "truncated": truncated[0],
        "exception": exc_str,
        "exit_code": exit_code,
        "elapsed_s": elapsed,
    }
    Path(args.output).write_text(json.dumps(payload), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def _split_entry(entry: str) -> tuple[str, str]:
    """Return ``(module_name, attr_name)`` for an entry spec.

    Accepts ``pkg.mod:func`` (setuptools console-script form) or the
    dotted form ``pkg.mod.func`` (last component treated as attr).
    """
    if not entry:
        raise ValueError("empty entry")
    if ":" in entry:
        m, a = entry.rsplit(":", 1)
        if not m or not a:
            raise ValueError("entry needs both module and attr")
        return m, a
    if "." not in entry:
        raise ValueError(
            "dotted entry needs at least one '.' separating module and attr"
        )
    m, a = entry.rsplit(".", 1)
    return m, a


# ---------------------------------------------------------------------------
# Qualname extraction
# ---------------------------------------------------------------------------


def _frame_qualname(frame) -> str | None:
    """Compute a dotted qualname for a Python stack frame.

    Uses ``f_code.co_qualname`` (Python 3.11+, always present here)
    combined with the module name from ``f_globals['__name__']``.
    Returns None if the module name is unavailable (e.g. exec'd code).
    """
    code = frame.f_code
    qn = getattr(code, "co_qualname", code.co_name)
    mod = frame.f_globals.get("__name__")
    if not mod or mod == "__main__":
        # __main__ frames are usually the runner itself; treat as
        # outside the trace. Also drops any user-side `if __name__ ==
        # "__main__":` wrapper.
        return None
    return f"{mod}.{qn}"


def _c_qualname(arg: Any) -> str | None:
    """Compute a dotted qualname for a C function or builtin method.

    `arg` is the C-level callable that fired the c_call/c_return.
    Builds ``module.qualname`` (or just ``qualname`` for builtins).
    Returns None if the callable doesn't expose enough metadata.
    """
    name = getattr(arg, "__qualname__", None) or getattr(arg, "__name__", None)
    if not name:
        return None
    mod = getattr(arg, "__module__", None)
    if mod and mod != "builtins":
        return f"{mod}.{name}"
    return f"builtins.{name}" if name else None


# ---------------------------------------------------------------------------
# Failure helpers
# ---------------------------------------------------------------------------


def _write_failure(output: str, message: str) -> int:
    """Write a structured failure JSON and return exit code 2."""
    payload = {
        "events": [],
        "truncated": False,
        "exception": message,
        "exit_code": 2,
        "elapsed_s": 0.0,
    }
    Path(output).write_text(json.dumps(payload), encoding="utf-8")
    return 2


if __name__ == "__main__":
    sys.exit(main())
