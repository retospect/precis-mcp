"""``precis email`` — manage mailbox accounts for the email kind (slice 1).

Subcommands:

* ``add ACCOUNT``  — create/re-configure a row (provider preset fills host/
  port from the domain; flags override). ``--password-stdin`` also stores the
  password in the vault under the derived (or ``--secret-name``) key.
* ``list``         — configured accounts (never prints the secret).
* ``rm ACCOUNT``   — delete a row (the vault secret is left in place).
* ``test ACCOUNT`` — connect + SELECT each watched folder; report counts and
  UIDVALIDITY. The end-to-end proof that credentials + TLS + login work.
* ``poll [ACCOUNT]`` — run one ``mail_poll`` tick now (fetch new mail past the
  high-water + inline tier-0 injection scan). No arg = every *due* account;
  ``ACCOUNT`` = that one regardless of cadence; ``--all`` = every enabled now.
* ``scan`` — run one ``inject_scan`` tick now (model-score the tier-0-flagged
  backlog + quarantine ladder). Needs the local model proxy; ops-side tool.

The password/token itself lives in the secrets vault (ADR 0055); this table
holds only its vault key. Send (SMTP) is a later slice; this is read config.
"""

from __future__ import annotations

import argparse
import json
import sys

from precis.cli._common import resolve_dsn
from precis.mail.account import Account, AuthMode, default_secret_name
from precis.store import Store


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "email",
        help="Manage mailbox accounts for the email kind (IMAP read; v1).",
        description=(
            "Register IMAP/SMTP accounts the email kind browses. The password "
            "lives in the secrets vault; this stores only its key plus a JSONB "
            "config bag (host/port/tls/folders/poll/auth/scan-policy)."
        ),
    )
    esub = p.add_subparsers(dest="email_cmd", required=True)

    a = esub.add_parser("add", help="Create/re-configure an account.")
    a.add_argument("account", help="Address, e.g. rs@retostamm.com.")
    a.add_argument("--imap-host", default=None, help="Override IMAP host.")
    a.add_argument("--imap-port", type=int, default=None, help="Override IMAP port.")
    a.add_argument(
        "--imap-tls",
        choices=["ssl", "starttls", "none"],
        default=None,
        help="IMAP TLS mode (default ssl).",
    )
    a.add_argument("--imap-user", default=None, help="LOGIN user (default = address).")
    a.add_argument("--smtp-host", default=None, help="Override SMTP host (send later).")
    a.add_argument("--smtp-port", type=int, default=None, help="Override SMTP port.")
    a.add_argument(
        "--folder",
        action="append",
        dest="folders",
        default=None,
        help="Watched folder (repeatable; default INBOX).",
    )
    a.add_argument(
        "--poll-seconds", type=int, default=None, help="Poll cadence (default 900)."
    )
    a.add_argument(
        "--auth",
        choices=[m.value for m in AuthMode],
        default=None,
        help="Auth mode (default password; xoauth2 is a stub).",
    )
    a.add_argument(
        "--scan-policy",
        choices=["quarantine", "flag-only"],
        default=None,
        help="Injection-scan policy (default quarantine).",
    )
    a.add_argument(
        "--secret-name",
        default=None,
        help="Vault key for the password (default email.<account>.password).",
    )
    a.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin and store it in the vault.",
    )
    a.add_argument("--disabled", action="store_true", help="Create the row disabled.")
    a.add_argument(
        "--config-json",
        default=None,
        help="Extra config as a JSON object, merged under the flag-derived keys.",
    )
    a.add_argument("--database-url", default=None, help="Postgres DSN override.")

    ls = esub.add_parser("list", help="List configured accounts (no secrets).")
    ls.add_argument("--database-url", default=None, help="Postgres DSN override.")

    rm = esub.add_parser("rm", help="Delete an account (vault secret is kept).")
    rm.add_argument("account")
    rm.add_argument("--database-url", default=None, help="Postgres DSN override.")

    t = esub.add_parser("test", help="Connect + SELECT folders; report counts.")
    t.add_argument("account")
    t.add_argument("--database-url", default=None, help="Postgres DSN override.")

    po = esub.add_parser(
        "poll", help="Run one mail_poll tick now (fetch new mail + tier-0 scan)."
    )
    po.add_argument(
        "account",
        nargs="?",
        default=None,
        help="Poll just this account (default: every due account).",
    )
    po.add_argument(
        "--all",
        action="store_true",
        help="Ignore the cadence and poll now even if not yet due.",
    )
    po.add_argument("--database-url", default=None, help="Postgres DSN override.")

    sc = esub.add_parser(
        "scan", help="Run one inject_scan tick now (model-score flagged mail)."
    )
    sc.add_argument(
        "--batch", type=int, default=8, help="Max messages to scan this tick."
    )
    sc.add_argument(
        "--model", default=None, help="Model alias (default PRECIS_INJECT_SCAN_MODEL)."
    )
    sc.add_argument("--database-url", default=None, help="Postgres DSN override.")


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        cmd = args.email_cmd
        if cmd == "add":
            _add(args, store)
        elif cmd == "list":
            _list(store)
        elif cmd == "rm":
            _rm(args, store)
        elif cmd == "test":
            _test(args, store)
        elif cmd == "poll":
            _poll(args, store)
        elif cmd == "scan":
            _scan(args, store)
    finally:
        store.close()


def _build_config(args: argparse.Namespace) -> dict:
    """Assemble the JSONB config from flags, merged over --config-json."""
    cfg: dict = {}
    if args.config_json:
        parsed = json.loads(args.config_json)
        if not isinstance(parsed, dict):
            raise SystemExit("email add: --config-json must be a JSON object")
        cfg.update(parsed)

    imap = dict(cfg.get("imap", {}))
    if args.imap_host is not None:
        imap["host"] = args.imap_host
    if args.imap_port is not None:
        imap["port"] = args.imap_port
    if args.imap_tls is not None:
        imap["tls"] = args.imap_tls
    if args.imap_user is not None:
        imap["user"] = args.imap_user
    if imap:
        cfg["imap"] = imap

    smtp = dict(cfg.get("smtp", {}))
    if args.smtp_host is not None:
        smtp["host"] = args.smtp_host
    if args.smtp_port is not None:
        smtp["port"] = args.smtp_port
    if smtp:
        cfg["smtp"] = smtp

    if args.folders is not None:
        cfg["folders"] = args.folders
    if args.poll_seconds is not None:
        cfg["poll_seconds"] = args.poll_seconds
    if args.auth is not None:
        cfg["auth"] = args.auth
    if args.scan_policy is not None:
        cfg["scan_policy"] = args.scan_policy
    return cfg


def _add(args: argparse.Namespace, store: Store) -> None:
    from precis import secrets as vault

    secret_name = args.secret_name or default_secret_name(args.account)
    config = _build_config(args)

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("email add: --password-stdin got an empty password")
        vault.set_secret(secret_name, password, store=store)
        print(f"email: stored secret {secret_name}")

    store.upsert_email_account(
        args.account,
        secret_name=secret_name,
        config=config,
        enabled=not args.disabled,
    )
    state = "disabled" if args.disabled else "enabled"
    print(f"email: configured {args.account} ({state}); secret key = {secret_name}")

    # Surface how the config resolves (catches a missing host preset early).
    row = store.get_email_account(args.account)
    if row is not None:
        try:
            acct = Account.from_row(row)
            print(
                f"       imap = {acct.imap.host}:{acct.imap.port}/{acct.imap.tls.value}"
                f"  folders = {', '.join(acct.folders)}"
            )
        except ValueError as exc:
            print(f"       ⚠ {exc}", file=sys.stderr)


def _list(store: Store) -> None:
    rows = store.list_email_accounts()
    if not rows:
        print("email: no accounts configured")
        return
    for row in rows:
        flag = "on " if row.enabled else "off"
        host = (row.config.get("imap") or {}).get("host", "?")
        print(
            f"[{flag}] {row.account:32s} secret={row.secret_name} "
            f"imap={host} last_uid={row.last_uid}"
        )


def _rm(args: argparse.Namespace, store: Store) -> None:
    if store.delete_email_account(args.account):
        print(f"email: deleted {args.account} (vault secret left in place)")
    else:
        print(f"email: no such account {args.account}", file=sys.stderr)
        sys.exit(1)


def _test(args: argparse.Namespace, store: Store) -> None:
    from precis.mail import imap as mail_imap

    row = store.get_email_account(args.account)
    if row is None:
        print(f"email: no such account {args.account}", file=sys.stderr)
        sys.exit(1)

    acct = Account.from_row(row)
    print(
        f"email: connecting to {acct.imap.host}:{acct.imap.port} as {acct.imap.user} …"
    )
    try:
        result = mail_imap.probe(acct, store=store)
    except mail_imap.ImapAuthError as exc:
        print(f"email: AUTH FAILED — {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"email: connection failed — {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"email: OK — {result.host}")
    for f in result.folders:
        print(
            f"   {f.folder:20s} {f.exists:6d} msgs  "
            f"uidvalidity={f.uidvalidity}  uidnext={f.uidnext}"
        )


def _poll(args: argparse.Namespace, store: Store) -> None:
    from precis.workers.mail_poll import run_mail_poll

    if args.account is not None:
        print(f"email: polling {args.account} …")
        r = run_mail_poll(store, only_account=args.account)
    elif args.all:
        print("email: polling all enabled accounts …")
        r = run_mail_poll(store, force=True)
    else:
        print("email: polling due accounts …")
        r = run_mail_poll(store)
    print(
        f"email: polled {r['claimed']} account(s) — "
        f"{r['ok']} message(s) scanned, {r['failed']} failed"
    )


def _scan(args: argparse.Namespace, store: Store) -> None:
    import os

    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.inject_scan import run_inject_scan_pass

    model = args.model or os.environ.get("PRECIS_INJECT_SCAN_MODEL") or "summarizer"
    client = DispatchClient(tier=Tier.LOCAL_SMALL, model=model, source="inject_scan")
    escalate_model = os.environ.get("PRECIS_INJECT_SCAN_ESCALATE_MODEL")
    escalate = (
        DispatchClient(
            tier=Tier.LOCAL_SMALL, model=escalate_model, source="inject_scan"
        )
        if escalate_model
        else None
    )
    print(f"email: inject_scan tick (model={model}) …")
    r = run_inject_scan_pass(
        store, client=client, escalate_client=escalate, batch_size=args.batch
    )
    print(
        f"email: scanned {r['claimed']} flagged message(s) — "
        f"{r['ok']} deepened, {r['failed']} left pending"
    )
