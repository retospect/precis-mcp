"""``precis secret`` — manage the DB secrets vault (ADR 0055).

Subcommands:

* ``set NAME``   — store/replace a secret. Value read from stdin (default) or
  an interactive no-echo prompt (``--prompt``); never from argv, so it can't
  leak via ``ps`` / shell history.
* ``list``       — masked inventory (name, hint, updated_at). No plaintext.
* ``get NAME``   — reveal one plaintext (admin/debug; writes an audit row).
* ``rm NAME``    — delete a secret.
* ``import``     — bulk-load from ``~/.secrets/pw/*`` files (migration helper).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser("secret", help="Manage the DB secrets vault.")
    parser.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    s = parser.add_subparsers(dest="secret_cmd", required=True)

    p_set = s.add_parser("set", help="Store/replace a secret (value via stdin).")
    p_set.add_argument("name", help="Secret name, e.g. PERPLEXITY_API_KEY.")
    p_set.add_argument(
        "--prompt",
        action="store_true",
        help="Read the value from an interactive no-echo prompt instead of stdin.",
    )

    s.add_parser("list", help="Masked inventory (no plaintext).")

    p_get = s.add_parser("get", help="Reveal one plaintext (audited).")
    p_get.add_argument("name")

    p_rm = s.add_parser("rm", help="Delete a secret.")
    p_rm.add_argument("name")

    p_imp = s.add_parser("import", help="Bulk-load from ~/.secrets/pw/* files.")
    p_imp.add_argument(
        "--dir",
        default=None,
        help="Source dir (default $PRECIS_SECRETS_FILE_DIR or ~/.secrets/pw).",
    )
    p_imp.add_argument(
        "--commit",
        action="store_true",
        help="Actually write (default: dry-run, list what would be imported).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    from precis import secrets as vault
    from precis.store import Store

    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        cmd = args.secret_cmd
        if cmd == "set":
            _set(args, store, vault)
        elif cmd == "list":
            _list(store, vault)
        elif cmd == "get":
            _get(args, store, vault)
        elif cmd == "rm":
            vault.delete_secret(args.name, store=store)
            print(f"secret: deleted {args.name}")
        elif cmd == "import":
            _import(args, store, vault)
    finally:
        store.close()


def _set(args: argparse.Namespace, store: object, vault: object) -> None:
    if args.prompt:
        import getpass

        value = getpass.getpass(f"value for {args.name}: ")
    else:
        value = sys.stdin.read().rstrip("\n")
    if not value:
        print("secret: empty value; nothing stored", file=sys.stderr)
        sys.exit(2)
    vault.set_secret(args.name, value, store=store)  # type: ignore[attr-defined]
    hint = vault.list_secrets(store=store)  # type: ignore[attr-defined]
    shown = next((r["hint"] for r in hint if r["name"] == args.name), "?")
    print(f"secret: stored {args.name} ({shown})")


def _list(store: object, vault: object) -> None:
    rows = vault.list_secrets(store=store)  # type: ignore[attr-defined]
    if not rows:
        print("secret: vault is empty")
        return
    width = max(len(str(r["name"])) for r in rows)
    for r in rows:
        print(f"{r['name']!s:<{width}}  {r['hint']:<16}  {r['updated_at']}")


def _get(args: argparse.Namespace, store: object, vault: object) -> None:
    val = vault.get_secret(args.name, store=store)  # type: ignore[attr-defined]
    if val is None:
        print(f"secret: {args.name} not found", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(val)
    if sys.stdout.isatty():
        sys.stdout.write("\n")


def _import(args: argparse.Namespace, store: object, vault: object) -> None:
    import os

    src = Path(
        args.dir
        or os.environ.get("PRECIS_SECRETS_FILE_DIR")
        or (Path.home() / ".secrets" / "pw")
    )
    if not src.is_dir():
        print(f"secret: no such dir {src}", file=sys.stderr)
        sys.exit(2)
    files = sorted(p for p in src.iterdir() if p.is_file())
    if not files:
        print(f"secret: no files under {src}")
        return
    for p in files:
        value = p.read_text().strip()
        if not value:
            print(f"  skip {p.name} (empty)")
            continue
        if args.commit:
            vault.set_secret(p.name, value, store=store)  # type: ignore[attr-defined]
            print(f"  imported {p.name}")
        else:
            print(f"  would import {p.name} ({len(value)} chars)")
    if not args.commit:
        print("secret: dry-run — re-run with --commit to write")


__all__ = ["add_parser", "run"]
