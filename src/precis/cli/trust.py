"""``precis trust`` — operator surface of graph trust state, slice 1
(migration 0214; docs/backlog/graph-node-trust-state.md).

* ``precis trust report`` — the compatibility-window measurement: write
  mode, stamp histogram per table, version-0 rows written since 0214 by
  unconverted writers (by ref kind / link relation and set_by) and the
  pre-0214 legacy count. ``--json`` for the raw shape.
* ``precis trust show ref|link ID`` — one target's stamp, content sha and
  every recorded act.
* ``precis trust consider|demote ref|link ID --sha SHA --reason R`` —
  record the act against the content sha it was decided on.
* ``precis trust correct-origin ref|link ID --origin O --sha SHA --reason R``
  and ``precis trust legacy-import ...`` — the operator-only acts the MCP
  surface refuses (``precis.runtime.trust_verb``).
* ``precis trust strict --lock-timeout 5s`` — the one-way strict switch.
  The coordinator runs it once every in-scope writer is converted; a lock
  timeout aborts and leaves compatibility mode in force.

Acts record ``--by`` (default ``$USER``) with actor type ``operator``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from precis.cli._common import resolve_dsn
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis.store.revision_context import TRUST_ORIGINS, TrustAct


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``trust`` subparser on ``sub``."""
    p = sub.add_parser(
        "trust",
        help="Graph trust state: compat report, acts, strict switch.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tp = p.add_subparsers(dest="trust_cmd", required=True)

    rep = tp.add_parser("report", help="Compatibility-window measurement.")
    rep.add_argument("--json", action="store_true", help="Print the raw report.")
    rep.add_argument("--dsn", default=None, help="Database URL override.")

    show = tp.add_parser("show", help="One target's stamp and acts.")
    show.add_argument("target_kind", choices=["ref", "link"])
    show.add_argument("target_id", type=int)
    show.add_argument("--dsn", default=None, help="Database URL override.")

    for name, helptext in (
        ("consider", "Record a consider act (confidence -> considered)."),
        ("demote", "Record a demotion (confidence -> hunch)."),
        ("correct-origin", "Correct a recorded origin (operator only)."),
        ("legacy-import", "Stamp a version-0 row as an imported legacy row."),
    ):
        sp = tp.add_parser(name, help=helptext)
        sp.add_argument("target_kind", choices=["ref", "link"])
        sp.add_argument("target_id", type=int)
        sp.add_argument("--sha", required=True, help="Content sha the act is on.")
        sp.add_argument("--reason", required=True)
        if name == "correct-origin":
            sp.add_argument("--origin", required=True, choices=list(TRUST_ORIGINS))
        if name == "legacy-import":
            sp.add_argument("--origin", default=None, choices=list(TRUST_ORIGINS))
        sp.add_argument("--by", default=None, help="Actor (default: $USER).")
        sp.add_argument("--dsn", default=None, help="Database URL override.")

    st = tp.add_parser("strict", help="Activate strict mode (one-way).")
    st.add_argument("--lock-timeout", default="5s")
    st.add_argument("--dsn", default=None, help="Database URL override.")
    return p


def _report(args: argparse.Namespace, store: Store) -> None:
    report = store.trust_compat_report()
    if args.json:
        print(json.dumps(report, default=str, indent=2))
        return
    print(
        f"mode {report['mode']}; 0214 applied {report['expanded_at']}; "
        f"strict since {report['strict_at'] or '-'}"
    )
    for target_kind in ("ref", "link"):
        part = report[target_kind]
        print(f"\n{target_kind}s: {part['legacy_rows']} pre-0214 legacy rows (v0)")
        for s in part["stamps"]:
            print(f"  v{s['version']} {s['origin']} · {s['confidence']}: {s['count']}")
        window = part["compat_writes"]
        total = sum(w["count"] for w in window)
        print(f"  unconverted writes since 0214: {total}")
        for w in window[:25]:
            key = w.get("kind") or w.get("relation")
            print(f"    {key} (set_by {w['set_by']}): {w['count']}")


def _show(args: argparse.Namespace, store: Store) -> None:
    state = store.trust_state(args.target_kind, args.target_id)
    if state is None:
        raise NotFound(f"no {args.target_kind} {args.target_id}")
    print(f"{state.target_kind} {state.target_id}: {state.label} v{state.version}")
    print(f"sha {state.sha}")
    for act in store.trust_acts_for(args.target_kind, args.target_id):
        before = act.get("before") or {}
        after = act.get("after") or {}
        print(
            f"  {act.get('at')} {act.get('action')}: "
            f"{before.get('origin', '-')}/{before.get('confidence', '-')} -> "
            f"{after.get('origin')}/{after.get('confidence')} "
            f"by {act.get('actor')} ({act.get('actor_type')}) — {act.get('reason')}"
        )


def _act(args: argparse.Namespace, store: Store) -> None:
    act = TrustAct(
        action=args.trust_cmd,
        target_kind=args.target_kind,
        target_id=args.target_id,
        expected_sha=args.sha,
        reason=args.reason,
        origin=getattr(args, "origin", None),
        confidence="hunch" if args.trust_cmd == "demote" else None,
    )
    receipt = store.record_trust_act(
        act,
        actor=args.by or os.environ.get("USER") or "operator",
        actor_type="operator",
    )
    replay = " (replay)" if receipt.replayed else ""
    print(
        f"{args.trust_cmd} {args.target_kind} {args.target_id}: "
        f"now {receipt.state.label} v{receipt.state.version}, "
        f"op {receipt.act.get('op_id')}{replay}"
    )


def _strict(args: argparse.Namespace, store: Store) -> None:
    at = store.activate_trust_strict(lock_timeout=args.lock_timeout)
    print(f"strict mode active since {at}")


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.dsn))
    handlers = {
        "report": _report,
        "show": _show,
        "consider": _act,
        "demote": _act,
        "correct-origin": _act,
        "legacy-import": _act,
        "strict": _strict,
    }
    try:
        handlers[args.trust_cmd](args, store)
    except (BadInput, NotFound, ValueError) as exc:
        print(f"trust: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        store.close()


__all__ = ["add_parser", "run"]
