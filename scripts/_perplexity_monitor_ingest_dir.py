"""perplexity-monitor-ingest-dir — watch a directory and import *.md as
Perplexity cache entries.

For every ``*.md`` at the top level of the watched directory:

    1. Read the body.
    2. Derive ``id=`` (the original "query") from the filename stem —
       this becomes the cache key and slug source for the
       ``put(kind=<kind>, mode='import')`` call. The handler runs
       ``slug_from_text(id, max_len=60)`` to produce the actual ref
       slug, so filenames that look like titles work fine.
    3. Dispatch ``put(kind=<kind>, id=<query>, text=<body>,
       mode='import')`` against the precis runtime. Idempotent on the
       request hash (same id+model collapses to a single cache row).
    4. On success, move the markdown into ``<watch_dir>/completed/``.
    5. On failure, move it into ``<watch_dir>/errors/`` alongside a
       ``<stem>.error.log`` with the rendered error so it stays
       hand-curatable.

Default kind is ``research`` (matches typical Perplexity Pro Deep
Research exports — pinned cache, never expires). Override via
``--kind {websearch,think,research}``.

Loops forever (default poll = 10s). Pass ``--once`` for a single sweep.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import DEFAULT_PERPLEXITY_INGEST_DIR

_KINDS = ("research", "think", "websearch")


def _list_md(base: Path) -> list[Path]:
    """Top-level markdown only — `completed/` and `errors/` are skipped."""
    return sorted(p for p in base.glob("*.md") if p.is_file())


def _ensure_dirs(base: Path) -> tuple[Path, Path]:
    completed = base / "completed"
    errors = base / "errors"
    completed.mkdir(exist_ok=True)
    errors.mkdir(exist_ok=True)
    return completed, errors


def _move(src: Path, dst_dir: Path) -> Path:
    if not src.exists():
        return src
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    return dst


def _id_for(md: Path) -> str:
    """Use the filename stem as the original Perplexity 'query'.

    The handler's ``_canonical_key`` strips and embeds the model so
    re-imports with the same stem under the same kind collapse to a
    single cache row. Whitespace normalised so on-disk variants like
    ``"Foo  bar.md"`` and ``"Foo bar.md"`` produce the same key.
    """
    return " ".join(md.stem.split())


def _process_one(
    md: Path,
    *,
    runtime,
    kind: str,
    completed: Path,
    errors: Path,
    tags: list[str],
) -> tuple[str, str]:
    """Returns ``(status, detail)`` where status is ``"ok" | "error"``."""
    try:
        text = md.read_text(encoding="utf-8")
    except Exception as exc:
        _move(md, errors)
        (errors / f"{md.stem}.error.log").write_text(traceback.format_exc())
        return "error", f"read failed: {exc}"

    if not text.strip():
        _move(md, errors)
        (errors / f"{md.stem}.error.log").write_text("file body is empty\n")
        return "error", "empty body"

    query = _id_for(md)
    body, is_error = runtime.dispatch_with_status(
        "put",
        {
            "kind": kind,
            "id": query,
            "text": text,
            "mode": "import",
        },
    )
    if is_error:
        _move(md, errors)
        (errors / f"{md.stem}.error.log").write_text(body + "\n")
        # Show only the first line in the console summary.
        first_line = body.splitlines()[0] if body else "import failed"
        return "error", f"import failed: {first_line}"

    # Best-effort tag attach via the dedicated tag verb. We only know
    # the slug from the rendered body — but the import message includes
    # ``ref 'slug'`` so we re-fetch by query to get a stable handle.
    if tags:
        # Round-trip through get(kind, id=query) → ref via store, but
        # simplest: re-run dispatch with `tag` after resolving slug
        # from the import response. The import body looks like
        # "imported research ref 'slug-name' (N blocks). future ..."
        slug = _extract_slug_from_import(body)
        if slug:
            for raw in tags:
                tag_body, tag_err = runtime.dispatch_with_status(
                    "tag",
                    {"kind": kind, "id": slug, "add": [raw]},
                )
                if tag_err:
                    print(
                        f"  warn: tag attach {raw!r} failed: "
                        f"{tag_body.splitlines()[0]}",
                        file=sys.stderr,
                    )
        else:
            print(
                f"  warn: could not parse slug from import response; "
                f"tags not attached: {tags}",
                file=sys.stderr,
            )

    _move(md, completed)
    return "ok", body.splitlines()[0] if body else "imported"


def _extract_slug_from_import(body: str) -> str | None:
    """Pull the slug out of the handler's import success message.

    Format (see ``perplexity.py::put``):
        ``imported <kind> ref '<slug>' (N block(s)). future ...``
    """
    if "ref '" not in body:
        return None
    after = body.split("ref '", 1)[1]
    if "'" not in after:
        return None
    return after.split("'", 1)[0]


_should_stop = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _should_stop
        _should_stop = True
        print(f"\nreceived signal {signum}, draining and exiting...")

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Watch a directory and import *.md Perplexity reports into "
            "precis-mcp as $0 cache entries."
        ),
    )
    p.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_PERPLEXITY_INGEST_DIR,
        help=f"Directory to watch (default: {DEFAULT_PERPLEXITY_INGEST_DIR}).",
    )
    p.add_argument(
        "--kind",
        choices=_KINDS,
        default="research",
        help=(
            "Perplexity tier to import as (default: research — pinned "
            "cache, matches typical Pro Deep Research exports)."
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Process current contents and exit (no polling loop).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Seconds between scans when looping (default: 10).",
    )
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        metavar="TAG",
        help="Repeatable: open tag to attach to each imported ref.",
    )
    args = p.parse_args()

    base: Path = args.dir.expanduser().resolve()
    if not base.is_dir():
        print(
            f"perplexity-monitor-ingest-dir: not a directory: {base}",
            file=sys.stderr,
        )
        sys.exit(2)

    completed, errors = _ensure_dirs(base)
    _install_signal_handlers()

    from precis.runtime import build_runtime

    runtime = build_runtime()
    try:
        mode = "once" if args.once else f"loop (every {args.interval:g}s)"
        print(
            f"watching {base} — kind={args.kind} mode={mode}",
            flush=True,
        )

        first_idle_print = True
        while not _should_stop:
            mds = _list_md(base)
            if mds:
                first_idle_print = True
                for md in mds:
                    if _should_stop:
                        break
                    print(f"\n→ {md.name}", flush=True)
                    status, detail = _process_one(
                        md,
                        runtime=runtime,
                        kind=args.kind,
                        completed=completed,
                        errors=errors,
                        tags=list(args.tags),
                    )
                    marker = "ok" if status == "ok" else "FAIL"
                    print(f"  {marker}: {detail}", flush=True)
            elif args.once:
                print("no markdown files to import; exiting.")
                break
            elif first_idle_print:
                print(
                    f"idle (no *.md at top level of {base}); polling...",
                    flush=True,
                )
                first_idle_print = False

            if args.once:
                break
            slept = 0.0
            while slept < args.interval and not _should_stop:
                time.sleep(min(0.5, args.interval - slept))
                slept += 0.5
    finally:
        if runtime.store is not None:
            runtime.store.close()


if __name__ == "__main__":
    main()
