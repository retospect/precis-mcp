"""``precis repl`` — interactive shell over the seven-verb tool surface.

Builds the runtime (postgres pool + embedder) once and loops on
stdin, dispatching ``verb key=value ...`` lines through the same
``TOOL_REGISTRY`` the MCP server and ``precis tools`` CLI use. Use
this when you want to probe the tool surface without paying the
~50 s bge-m3 cold-start on every invocation.

Grammar (shlex-split, so quote values containing spaces):

    precis> search q="two-photon absorption" kind=paper top_k=5
    precis> get kind=skill id=precis-search-help
    precis> put kind=todo text="buy milk" tags=home,urgent

Line editing comes from ``prompt_toolkit`` — arrow keys, Ctrl-R
reverse search, persistent history across sessions, and Tab
completion over verb names and the current verb's ``key=``
parameters. We use prompt_toolkit instead of ``readline`` because
the slim runtime image (python:3.12-slim-bookworm) doesn't ship
libreadline, so the prior readline-based REPL silently degraded to
no-editing ``input()``.

Meta-commands:

    help                  list verbs
    help <verb>           print verb signature + docstring
    quit / exit / Ctrl-D  leave the shell
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory

from precis.tools import TOOL_REGISTRY, get_tool_info, get_tool_names
from precis.tools.cli_adapter import _convert_value, _is_call_tool_result


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis repl`` subcommand on ``sub``."""
    p = sub.add_parser(
        "repl",
        help="Interactive shell over the seven-verb tool surface.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=run)


class _VerbCompleter(Completer):
    """Tab-complete verb names at position 0, then the verb's ``key=`` params.

    Position 0 → completes against ``get_tool_names()``.
    Position ≥1 → looks up the verb at position 0 in ``TOOL_REGISTRY``
    and offers its parameter names with a trailing ``=``. Already-used
    keys are filtered so completion narrows as the line fills out.
    """

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        text = document.text_before_cursor
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            return  # unbalanced quote — give up rather than complete garbage
        trailing_space = text.endswith((" ", "\t"))
        word = "" if trailing_space else (tokens[-1] if tokens else "")
        position = len(tokens) - (0 if trailing_space else 1)
        if position < 0:
            position = 0

        if position == 0:
            for name in get_tool_names():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word))
            return

        verb = tokens[0]
        if verb not in TOOL_REGISTRY:
            return
        params = TOOL_REGISTRY[verb]["parameters"]
        used = {t.split("=", 1)[0] for t in tokens[1:] if "=" in t}
        if "=" in word:
            return  # mid-value; don't complete

        for pname in params:
            if pname in used:
                continue
            if pname.startswith(word):
                yield Completion(pname + "=", start_position=-len(word))


def run(args: argparse.Namespace) -> None:
    """Build runtime once, warm the embedder, then loop on stdin."""
    del args  # no flags yet

    _silence_tqdm()
    session = _build_session()

    # Force runtime construction (and bge-m3 weight load) up front so
    # the first verb call doesn't eat the 30–50 s lazy-load. The
    # heartbeat dots cover the long silence between sentence-
    # transformers' "Loading model …" log (start of load) and the
    # actual end of weight deserialization. Without it, users assume
    # the REPL has wedged and type blindly into the buffer.
    print(
        "precis repl: building runtime (bge-m3 cold load is ~30-50s) …",
        file=sys.stderr,
    )

    import threading

    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        while not stop_heartbeat.wait(5.0):
            print(".", end="", file=sys.stderr, flush=True)

    hb = threading.Thread(target=_heartbeat, name="precis-repl-heartbeat", daemon=True)
    hb.start()

    try:
        from precis.tools.core import _get_runtime

        runtime = _get_runtime()
        embedder = getattr(runtime, "embedder", None)
        ensure = getattr(embedder, "_ensure_loaded", None) if embedder else None
        if ensure is not None:
            try:
                ensure()
            except Exception as e:
                print(
                    f"\nprecis repl: embedder warmup failed ({e!r}); continuing lazily",
                    file=sys.stderr,
                )
    finally:
        stop_heartbeat.set()
        hb.join(timeout=1.0)
        print(file=sys.stderr)  # terminate the dot line

    print(
        "precis repl: ready. verbs: "
        + ", ".join(get_tool_names())
        + ". `help` for usage, Ctrl-D to exit.",
        file=sys.stderr,
    )

    while True:
        try:
            line = session.prompt("precis> ")
        except EOFError:
            print(file=sys.stderr)
            return
        except KeyboardInterrupt:
            print("^C", file=sys.stderr)
            continue

        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            return

        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"[parse error] {e}", file=sys.stderr)
            continue

        verb = tokens[0]
        rest = tokens[1:]

        if verb in ("help", "?"):
            _print_help(rest)
            continue

        if verb not in TOOL_REGISTRY:
            print(
                f"[unknown verb] {verb!r} — available: {', '.join(get_tool_names())}",
                file=sys.stderr,
            )
            continue

        try:
            payload = _build_payload(verb, rest)
        except ValueError as e:
            print(f"[error] {e}", file=sys.stderr)
            continue

        func = TOOL_REGISTRY[verb]["func"]
        try:
            result = func(**payload)
        except Exception as e:
            print(f"[error:Exception] {type(e).__name__}: {e}", file=sys.stderr)
            continue

        if _is_call_tool_result(result):
            print(result.content[0].text)
        else:
            print(result)


def _build_session() -> PromptSession:
    """Construct the prompt_toolkit session with history + completion.

    History lives at ``~/.cache/precis/repl_history`` (the same path
    the prior readline-based REPL used). The cache directory is
    mounted as a docker volume by ``scripts/precis-shell`` so it
    survives container restarts; on hosts where HOME isn't writable
    we fall back to an in-memory session.
    """
    histdir = os.path.expanduser("~/.cache/precis")
    histfile = os.path.join(histdir, "repl_history")
    history = None
    try:
        os.makedirs(histdir, exist_ok=True)
        history = FileHistory(histfile)
    except OSError:
        pass  # read-only HOME — session keeps history in memory only

    return PromptSession(
        history=history,
        completer=_VerbCompleter(),
        complete_while_typing=False,
    )


def _silence_tqdm() -> None:
    """Force-disable tqdm progress bars in this process.

    sentence-transformers emits a tqdm bar per ``encode`` call, which
    buries REPL output (each ``search`` re-ranks → many encode calls).
    Patch ``tqdm.tqdm.__init__`` to default ``disable=True``; this is
    a no-op when tqdm isn't installed.
    """
    try:
        import tqdm
    except ImportError:
        return
    orig_init = tqdm.tqdm.__init__

    def quiet_init(self, *a, **kw):  # type: ignore[no-untyped-def]
        kw["disable"] = True
        return orig_init(self, *a, **kw)

    tqdm.tqdm.__init__ = quiet_init  # type: ignore[assignment]


def _build_payload(verb: str, tokens: list[str]) -> dict[str, Any]:
    """Turn ``key=value`` tokens into the verb's kwargs dict.

    Types are coerced through the same ``_convert_value`` the
    ``precis tools`` CLI uses, so int/bool/list parameters behave
    consistently between the two surfaces.
    """
    info = get_tool_info(verb)
    params = info["parameters"]
    payload: dict[str, Any] = {}

    for tok in tokens:
        if "=" not in tok:
            raise ValueError(
                f"expected key=value, got {tok!r}; "
                f"try `help {verb}` for the parameter list"
            )
        key, _, raw = tok.partition("=")
        key = key.strip()
        if key not in params:
            allowed = ", ".join(params.keys())
            raise ValueError(f"unknown arg {key!r} for {verb} (allowed: {allowed})")
        payload[key] = _convert_value(raw, params[key])

    return payload


def _print_help(tokens: list[str]) -> None:
    """Print verb list or a single verb's signature + docstring."""
    if not tokens:
        print("verbs:", ", ".join(get_tool_names()), file=sys.stderr)
        print(
            "usage: <verb> key=value [key=value …]   (quote values with spaces)",
            file=sys.stderr,
        )
        return

    verb = tokens[0]
    if verb not in TOOL_REGISTRY:
        print(f"[unknown verb] {verb!r}", file=sys.stderr)
        return

    info = get_tool_info(verb)
    sig_parts = []
    for name, pinfo in info["parameters"].items():
        if name == "args":
            continue
        marker = "" if pinfo["required"] else "?"
        sig_parts.append(f"{name}{marker}")
    print(f"{verb}({', '.join(sig_parts)})", file=sys.stderr)
    doc = info["doc"].strip()
    if doc:
        print(doc, file=sys.stderr)
