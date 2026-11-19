"""``precis eval '<call>'`` — execute one verb call in the command syntax.

Second consumer of :mod:`precis.tools.command_parser` (the frontier
MCP profile, ``PRECIS_MCP_PROFILE=command``, is the first) — lets an
operator run ``precis eval "get(kind='skill', id='toc')"`` without
building the eight-flag ``precis tools <verb> --flag value`` command
line. Routes through the same ``TOOL_REGISTRY[verb]['func']`` call
every other surface uses, so validation and error rendering match.

``--json`` makes stdout exactly one JSON object —
``{"ok", "text", "created", "error"}`` — for a script that must read a put's
new handle back: the create receipts differ by kind, so ``created`` is derived
once, here, instead of in every caller (gr735223).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

from precis.cli._common import REFUSAL_EXIT, is_refusal
from precis.tools import TOOL_REGISTRY
from precis.tools.cli_adapter import _is_call_tool_result
from precis.tools.command_parser import CommandParseError, parse_command
from precis.utils import handle_registry

#: The leading line of a create receipt, in the three shapes handlers emit:
#: ``created todo td735113 (STATUS:open)``, ``created gripe id=734976 (…).``
#: and ``created memory me158.`` — a kind word, then either a universal handle
#: or a bare ``id=<N>``. Anything else (``created plan 'slug' …``) is not a
#: handle receipt and yields no ``created``. The ack wording is prose, but no
#: structured id survives the tool layer (the verbs return ``str``), so this is
#: the one place that reads it.
_CREATED_RE = re.compile(
    r"^created (?P<kind>[\w-]+) (?:id=(?P<id>\d+)|(?P<handle>[a-z]{2}\d+))\b"
)

#: ``[error:Class]`` at the start of a rendered refusal.
_ERROR_CLASS_RE = re.compile(r"^\[error:(?P<cls>[A-Za-z]\w*)\]")


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "eval",
        help="Execute one verb call, e.g. eval \"get(kind='skill', id='toc')\".",
        description=__doc__,
    )
    p.add_argument(
        "command",
        help="Call expression with keyword-literal args, "
        "e.g. get(kind='skill', id='toc').",
    )
    p.add_argument(
        "--text",
        default=None,
        help="Large text payload merged in as text= (avoids quote-escaping "
        "it inside command).",
    )
    p.add_argument(
        "--text-file",
        type=Path,
        default=None,
        help="Read the text= payload from this file (UTF-8) instead of --text.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print exactly one JSON object on stdout: "
        '{"ok": bool, "text": <the normal text result>, "created": '
        '<handle of the ref a put created, e.g. "td735113"> | null, '
        '"error": <error class, e.g. "NotFound"> | null}. Exit codes are '
        "unchanged; logging and stray prints go to stderr.",
    )
    p.set_defaults(func=run)


def created_handle(text: str) -> str | None:
    """The canonical handle of the ref a create receipt names, else ``None``.

    Reads only the leading line. ``id=<N>`` maps to its handle through the
    registry for the receipt's kind; a kind with no handle code yields
    ``None`` rather than a guess.
    """
    first = text.lstrip().split("\n", 1)[0]
    m = _CREATED_RE.match(first)
    if m is None:
        return None
    if m["id"] is not None:
        return handle_registry.try_format(m["kind"], int(m["id"]))
    handle = handle_registry.normalize(m["handle"])
    return handle if handle_registry.parse(handle) is not None else None


def _emit_json(text: str, *, ok: bool, error: str | None = None) -> None:
    """Write the single ``--json`` object to stdout."""
    payload = {
        "ok": ok,
        "text": text,
        "created": created_handle(text) if ok else None,
        "error": error,
    }
    print(json.dumps(payload, ensure_ascii=False))


def _error_class(text: str) -> str | None:
    m = _ERROR_CLASS_RE.match(text.lstrip())
    return m["cls"] if m else None


def run(args: argparse.Namespace) -> None:
    as_json = bool(getattr(args, "json", False))
    text = args.text
    if args.text_file is not None:
        if text is not None:
            print(
                "eval: --text and --text-file are mutually exclusive", file=sys.stderr
            )
            sys.exit(2)
        text = args.text_file.read_text(encoding="utf-8")

    try:
        verb, kwargs = parse_command(args.command, text=text)
    except CommandParseError as e:
        if as_json:
            _emit_json(
                f"[error:CommandParseError] {e}", ok=False, error="CommandParseError"
            )
            sys.exit(1)
        print(f"[error:CommandParseError] {e}", file=sys.stderr)
        sys.exit(1)

    tool_func = TOOL_REGISTRY[verb]["func"]
    try:
        if as_json:
            # Stdout carries the one JSON object and nothing else: a handler
            # that prints (or a library that writes to stdout) lands on stderr
            # with the log lines.
            with contextlib.redirect_stdout(sys.stderr):
                result = tool_func(**kwargs)
        else:
            result = tool_func(**kwargs)
    except TypeError as e:
        if as_json:
            _emit_json(f"[error:BadInput] {verb}(...): {e}", ok=False, error="BadInput")
            sys.exit(1)
        print(f"[error:BadInput] {verb}(...): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if not as_json:
            raise
        _emit_json(f"{type(e).__name__}: {e}", ok=False, error=type(e).__name__)
        sys.exit(1)

    # A refusal reaches here two ways: as a structured CallToolResult, or as a
    # rendered `[error:…]` string that fell through to the plain print
    # below at exit 0 — the gr458317 shape. Both go to stderr at REFUSAL_EXIT,
    # matching `precis tools` so a script can treat the two commands alike.
    # Under --json the refusal is the object's text, on stdout, same exit code.
    if _is_call_tool_result(result):
        refusal = result.content[0].text
    elif is_refusal(result):
        refusal = str(result)
    else:
        refusal = None
    if refusal is not None:
        if as_json:
            _emit_json(refusal, ok=False, error=_error_class(refusal) or "Error")
        else:
            print(refusal, file=sys.stderr)
        sys.exit(REFUSAL_EXIT)
    if as_json:
        _emit_json(str(result), ok=True)
        return
    print(result)


__all__ = ["add_parser", "created_handle", "run"]
