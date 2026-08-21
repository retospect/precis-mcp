"""``precis users`` — manage the precis-web login roster.

Every account here is **fully authorized** on the web UI; there are no
roles to assign. Subcommands:

* ``add LOGIN``     — create an account (``--abbrev`` required).
* ``list``          — the roster: login, abbrev, name, email, state.
* ``passwd LOGIN``  — set a new password. **This is the recovery path** —
  HTTP Basic has no reset flow of its own (see below).
* ``edit LOGIN``    — change abbrev / full name / email.
* ``disable`` / ``enable LOGIN`` — soft toggle; the row (and its abbrev,
  which future edit-attribution renders) survives.
* ``rm LOGIN``      — delete outright.
* ``feed-token LOGIN`` — mint + print the private podcast feed URL.

**Passwords never come from argv.** ``ps`` on a shared host and shell
history both leak it, so the value arrives on an interactive no-echo
prompt (default) or stdin (``--password-stdin``) — the same discipline
``precis secret set`` uses. Length policy
(:func:`precis.users.validate_password`) is checked in
:func:`_read_password`, the one funnel both ``add`` and ``passwd`` use,
so this surface and the web form enforce the same floor.

The *self-service* half of this lives at ``/account``
(:mod:`precis_web.routes.account`): a signed-in user changes their own
password, profile and podcast link there. Roster management —
creating, disabling, deleting — is deliberately only here, because
every account is fully authorized and a web affordance for minting one
would turn a single stolen credential into several.

**On "forgot password".** Basic auth is a single request header: no
session, no server-side state, no reset affordance. Wiring email
recovery would mean adding an unauthenticated public route that mints
credentials — the exact surface the auth gate exists to remove. So
recovery is this CLI, run over SSH by someone who already has the box.
The ``email`` column is for display and future notification, not auth.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import TYPE_CHECKING

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser("users", help="Manage precis-web login accounts.")
    parser.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    s = parser.add_subparsers(dest="users_cmd", required=True)

    p_add = s.add_parser("add", help="Create a fully-authorized web account.")
    p_add.add_argument("login", help="Basic-auth username (lowercased).")
    p_add.add_argument(
        "--abbrev",
        required=True,
        help="Short display handle for edit attribution, e.g. 'rs'.",
    )
    p_add.add_argument("--name", default=None, help="Full name (display).")
    p_add.add_argument("--email", default=None, help="Contact address (not auth).")
    _password_flags(p_add)
    p_add.add_argument(
        "--no-pepper",
        action="store_true",
        help="Hash without the vault pepper (scrypt-v1). Default mints/uses "
        "PRECIS_WEB_PASSWORD_PEPPER so a leaked logical pg_dump stays inert.",
    )

    s.add_parser("list", help="Show the roster.")

    p_pw = s.add_parser("passwd", help="Set a new password (the recovery path).")
    p_pw.add_argument("login")
    _password_flags(p_pw)
    p_pw.add_argument("--no-pepper", action="store_true", help="Hash as scrypt-v1.")

    p_edit = s.add_parser("edit", help="Change abbrev / name / email.")
    p_edit.add_argument("login")
    p_edit.add_argument("--abbrev", default=None)
    p_edit.add_argument("--name", default=None, help="Empty string clears it.")
    p_edit.add_argument("--email", default=None, help="Empty string clears it.")

    p_dis = s.add_parser("disable", help="Block login, keep the row.")
    p_dis.add_argument("login")

    p_en = s.add_parser("enable", help="Undo disable.")
    p_en.add_argument("login")

    p_rm = s.add_parser("rm", help="Delete the account.")
    p_rm.add_argument("login")

    p_feed = s.add_parser(
        "feed-token", help="Mint + print the private podcast feed URL."
    )
    p_feed.add_argument("login")
    p_feed.add_argument(
        "--base-url",
        default=None,
        help="Origin for the printed URL (default $PRECIS_PODCAST_BASE_URL).",
    )
    p_feed.add_argument(
        "--clear",
        action="store_true",
        help="Revoke the existing token instead of minting a new one.",
    )
    return parser


def _password_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin instead of prompting (scripts).",
    )
    p.add_argument(
        "--prompt",
        action="store_true",
        help="Interactive no-echo prompt (the default when stdin is a tty).",
    )


def _read_password(args: argparse.Namespace) -> str:
    """Password from stdin or a twice-confirmed no-echo prompt.

    Policy (:func:`precis.users.validate_password`) is checked here, the
    single point every CLI password flows through, so ``add`` and
    ``passwd`` can't drift apart — and so this surface enforces the same
    floor the ``/account`` form does.
    """
    if getattr(args, "password_stdin", False):
        value = sys.stdin.read().strip()
        if not value:
            raise SystemExit("error: empty password on stdin")
        _check_policy(value)
        return value
    first = getpass.getpass("Password: ")
    if not first:
        raise SystemExit("error: empty password")
    if first != getpass.getpass("Repeat: "):
        raise SystemExit("error: passwords do not match")
    _check_policy(first)
    return first


def _check_policy(password: str) -> None:
    from precis.users import validate_password

    try:
        validate_password(password)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc


def run(args: argparse.Namespace) -> None:
    from precis.store import Store

    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        cmd = args.users_cmd
        if cmd == "add":
            _add(args, store)
        elif cmd == "list":
            _list(store)
        elif cmd == "passwd":
            _passwd(args, store)
        elif cmd == "edit":
            _edit(args, store)
        elif cmd in ("disable", "enable"):
            _toggle(args, store, disabled=(cmd == "disable"))
        elif cmd == "rm":
            _rm(args, store)
        elif cmd == "feed-token":
            _feed_token(args, store)
    finally:
        store.close()


def _pepper_for_write(args: argparse.Namespace, store: Store) -> str | None:
    """The pepper to hash with: minted-on-first-use unless ``--no-pepper``."""
    from precis.users import ensure_pepper

    if getattr(args, "no_pepper", False):
        return None
    return ensure_pepper(store=store)


def _add(args: argparse.Namespace, store: Store) -> None:
    from precis.users import hash_password

    password = _read_password(args)
    pepper = _pepper_for_write(args, store)
    record = hash_password(password, pepper=pepper)
    user = store.create_web_user(
        login=args.login,
        abbrev=args.abbrev,
        password=record,
        full_name=args.name,
        email=args.email,
    )
    print(f"created {user.login} ({user.abbrev}) — algo {record.password_algo}")


def _list(store: Store) -> None:
    users = store.list_web_users()
    if not users:
        print("no accounts — precis-web will answer 503 until one exists")
        return
    width = max(len(u.login) for u in users)
    for u in users:
        state = "disabled" if u.disabled_at else "active"
        seen = u.last_login_at.strftime("%Y-%m-%d") if u.last_login_at else "never"
        print(
            f"{u.login:<{width}}  {u.abbrev:<6} {state:<8} "
            f"last-login {seen}  {u.full_name or ''} {u.email or ''}".rstrip()
        )


def _passwd(args: argparse.Namespace, store: Store) -> None:
    from precis.users import hash_password

    password = _read_password(args)
    pepper = _pepper_for_write(args, store)
    record = hash_password(password, pepper=pepper)
    if not store.set_web_user_password(args.login, record):
        raise SystemExit(f"error: no such user {args.login!r}")
    print(f"password updated for {args.login} — algo {record.password_algo}")


def _edit(args: argparse.Namespace, store: Store) -> None:
    changed = store.update_web_user(
        args.login, abbrev=args.abbrev, full_name=args.name, email=args.email
    )
    if not changed:
        raise SystemExit("error: nothing to change, or no such user")
    print(f"updated {args.login}")


def _toggle(args: argparse.Namespace, store: Store, *, disabled: bool) -> None:
    if not store.set_web_user_disabled(args.login, disabled=disabled):
        raise SystemExit(f"error: no such user {args.login!r}")
    print(f"{args.login} {'disabled' if disabled else 'enabled'}")


def _rm(args: argparse.Namespace, store: Store) -> None:
    if not store.delete_web_user(args.login):
        raise SystemExit(f"error: no such user {args.login!r}")
    print(f"deleted {args.login}")


def _feed_token(args: argparse.Namespace, store: Store) -> None:
    import os

    from precis.users import mint_feed_token

    if args.clear:
        if not store.set_web_user_feed_token(args.login, None):
            raise SystemExit(f"error: no such user {args.login!r}")
        print(f"feed token revoked for {args.login}")
        return
    token, digest = mint_feed_token()
    if not store.set_web_user_feed_token(args.login, digest):
        raise SystemExit(f"error: no such user {args.login!r}")
    base = (args.base_url or os.environ.get("PRECIS_PODCAST_BASE_URL") or "").rstrip(
        "/"
    )
    url = (
        f"{base}/podcast/feed.xml?t={token}" if base else f"/podcast/feed.xml?t={token}"
    )
    print("Subscribe to this URL in the podcast app (any previous one is now dead):")
    print(url)
    if not base:
        print(
            "\nno origin known — prefix it with the tailscale-served host, or set "
            "PRECIS_PODCAST_BASE_URL",
            file=sys.stderr,
        )
