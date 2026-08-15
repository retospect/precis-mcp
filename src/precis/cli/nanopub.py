"""``precis nanopub`` — mint, sign, anchor and audit claim nanopubs.

The interactive half of the slice-2/3 pipeline
(:mod:`precis.nanopub`). This CLI **is** the interactive sign surface
key custody names: ``sign --attest`` is the one place the attesting key
may be invoked (a person runs this; workers/jobs sign with the
non-attesting bot key only, and a bot signature alone publishes
nothing).

Subcommands:

* ``keygen ROLE``       — generate an RSA keypair (4096 default), store
  the private half straight into the vault, print public key +
  fingerprint (for the out-of-band fingerprint page).
* ``status [FI]``       — publish rows by state / one hub's row.
* ``approve FI``        — freeze-at-review: approve the hub's claim
  string with a grounding payload (JSON via ``--payload``/stdin); every
  Layer-A mint gate runs here.
* ``check FI``          — run the gates advisory, no writes.
* ``sign FI``           — mint + sign (``--attest`` for the human key).
* ``reopen FI``         — flip a pre-anchor row back to candidate.
* ``view FI``           — the TriG rendering (same as
  ``get(kind='finding', view='nanopub')``).
* ``anchor --live``     — one manual OTS sweep (stamp + upgrade);
  ``--live`` is required because it talks to the calendar.
* ``audit``             — the proof-store recompute audit, findings to
  stdout.
* ``preflight FI``      — every publish-time gate, advisory (no writes).
* ``signoff LINK_ID``   — human sign-off of one unverified evidence
  edge (``--note`` required; the literal attestation that makes a
  withheld edge publishable).
* ``allow …``           — the publication-time trust allowlist
  (``list`` / ``add`` / ``end``); keys pinned by fingerprint, the
  ``--attesting`` entry is the human key.
* ``publish FI --live`` — the registry POST, **the point of no
  return**; without ``--live`` a dry run printing what would be POSTed.
"""

from __future__ import annotations

import argparse
import json
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        "nanopub", help="Mint, sign, anchor and audit claim nanopubs."
    )
    parser.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    s = parser.add_subparsers(dest="nanopub_cmd", required=True)

    p_key = s.add_parser(
        "keygen", help="Generate a keypair into the vault; print the public half."
    )
    p_key.add_argument("role", choices=("bot", "attesting"))
    p_key.add_argument("--bits", type=int, default=4096)
    p_key.add_argument(
        "--print-private",
        action="store_true",
        help="Also print the private key instead of storing it in the vault.",
    )

    p_status = s.add_parser("status", help="Publish rows by state.")
    p_status.add_argument("hub", nargs="?", help="fi<id> or bare ref id.")

    p_appr = s.add_parser("approve", help="Freeze-at-review (runs all mint gates).")
    p_appr.add_argument("hub")
    p_appr.add_argument(
        "--payload",
        default=None,
        help="Path to the grounding-payload JSON (default: stdin).",
    )
    p_appr.add_argument(
        "--title", default=None, help="Approve this exact string (default: live title)."
    )

    p_check = s.add_parser("check", help="Run the mint gates advisory (no writes).")
    p_check.add_argument("hub")
    p_check.add_argument("--payload", default=None)

    p_sign = s.add_parser("sign", help="Mint + sign the reviewed publish row.")
    p_sign.add_argument("hub")
    p_sign.add_argument(
        "--attest",
        action="store_true",
        help="Sign with the human attesting key (interactive door).",
    )
    p_sign.add_argument(
        "--llm-model",
        action="append",
        default=[],
        help="LLM model id used in extraction/verification (repeatable; "
        "recorded as software provenance).",
    )

    p_reopen = s.add_parser("reopen", help="Flip a pre-anchor row back to candidate.")
    p_reopen.add_argument("hub")

    p_view = s.add_parser("view", help="TriG rendering (draft or signed bytes).")
    p_view.add_argument("hub")

    p_anchor = s.add_parser("anchor", help="One manual OTS sweep (stamp + upgrade).")
    p_anchor.add_argument(
        "--live",
        action="store_true",
        help="Required: contacts the OTS calendar (a 32-byte digest leaves "
        "the box; discloses nothing).",
    )

    s.add_parser("audit", help="Proof-store recompute audit.")

    p_pf = s.add_parser("preflight", help="Publish-time gates, advisory.")
    p_pf.add_argument("hub")

    p_so = s.add_parser(
        "signoff", help="Human sign-off of one unverified evidence edge."
    )
    p_so.add_argument("link_id", type=int)
    p_so.add_argument("--note", required=True)

    p_allow = s.add_parser("allow", help="Publication-time trust allowlist.")
    allow_sub = p_allow.add_subparsers(dest="allow_cmd", required=True)
    allow_sub.add_parser("list", help="Every entry, open and closed windows.")
    p_aa = allow_sub.add_parser("add", help="Pin one (identity, fingerprint).")
    p_aa.add_argument("identity_uri")
    p_aa.add_argument("fingerprint")
    p_aa.add_argument(
        "--attesting",
        action="store_true",
        help="Mark the human key (its signature = 'a human checked').",
    )
    p_aa.add_argument("--note", default="")
    p_ae = allow_sub.add_parser("end", help="Close an entry's validity window.")
    p_ae.add_argument("entry_id", type=int)

    p_pub = s.add_parser("publish", help="Registry POST — THE point of no return.")
    p_pub.add_argument("hub")
    p_pub.add_argument(
        "--live",
        action="store_true",
        help="Required to POST: the artifact propagates across registry "
        "mirrors forever. Without it: dry run.",
    )
    return parser


def _hub_id(raw: str) -> int:
    return int(raw.removeprefix("fi"))


def _read_payload(path: str | None) -> dict:
    if path:
        with open(path) as f:
            return json.load(f)
    if sys.stdin.isatty():
        return {}
    data = sys.stdin.read().strip()
    return json.loads(data) if data else {}


def run(args: argparse.Namespace) -> None:
    from precis.store import Store

    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        cmd = args.nanopub_cmd
        if cmd == "keygen":
            _keygen(args, store)
        elif cmd == "status":
            _status(args, store)
        elif cmd == "approve":
            _approve(args, store)
        elif cmd == "check":
            _check(args, store)
        elif cmd == "sign":
            _sign(args, store)
        elif cmd == "reopen":
            _reopen(args, store)
        elif cmd == "view":
            _view(args, store)
        elif cmd == "anchor":
            _anchor(args, store)
        elif cmd == "audit":
            _audit(store)
        elif cmd == "preflight":
            _preflight(args, store)
        elif cmd == "signoff":
            _signoff(args, store)
        elif cmd == "allow":
            _allow(args, store)
        elif cmd == "publish":
            _publish(args, store)
    finally:
        store.close()


def _keygen(args: argparse.Namespace, store) -> None:
    from precis import secrets as vault
    from precis.nanopub.keys import VAULT_SECRET, fingerprint, generate_keypair

    private, public = generate_keypair(args.bits)
    name = VAULT_SECRET[args.role]
    if args.print_private:
        print(f"# {name} (store this in the vault yourself)")
        print(private)
    else:
        vault.set_secret(name, private, store=store)
        print(f"stored {name} in the vault ({args.bits}-bit RSA)")
    print(f"public key (base64 DER):\n{public}")
    print(f"fingerprint (sha256): {fingerprint(public)}")
    print(
        "publish the fingerprint out-of-band (the identity URI page) — "
        "signedBy alone proves nothing"
    )


def _status(args: argparse.Namespace, store) -> None:
    if args.hub:
        row = store.nanopub_publish_row(_hub_id(args.hub))
        if row is None:
            print("no live publish row")
            return
        print(json.dumps(_row_dict(row), indent=2, default=str))
        return
    from precis.nanopub.state import STATES

    for state in STATES:
        rows = store.nanopub_rows_in_state(state, limit=500)
        if rows:
            ids = ", ".join(f"fi{r.claim_ref_id}" for r in rows[:20])
            more = f" (+{len(rows) - 20} more)" if len(rows) > 20 else ""
            print(f"{state:>10}: {len(rows):4d}  {ids}{more}")


def _row_dict(row) -> dict:
    return {
        "id": row.id,
        "claim_ref_id": row.claim_ref_id,
        "artifact_type": row.artifact_type,
        "state": row.state,
        "approved_title": row.approved_title,
        "claim_sha": row.claim_sha,
        "aida_uri": row.aida_uri,
        "trusty_uri": row.trusty_uri,
        "artifact_id": row.artifact_id,
        "batch_id": row.batch_id,
        "grounding": row.grounding,
        "dependency_codes": row.dependency_codes,
    }


def _approve(args: argparse.Namespace, store) -> None:
    from precis.nanopub import mint

    payload = _read_payload(args.payload)
    # This CLI subcommand IS the interactive review surface — a person
    # runs it; approval is never batched (mint.approve's guard).
    row = mint.approve(
        store, _hub_id(args.hub), payload=payload, title=args.title, interactive=True
    )
    print(
        f"approved fi{row.claim_ref_id} → reviewed (publish row {row.id})\n"
        f"frozen: {row.approved_title!r}\n"
        f"aida:   {row.aida_uri}"
    )


def _check(args: argparse.Namespace, store) -> None:
    from precis.nanopub import evidence, gates

    hub_id = _hub_id(args.hub)
    payload = _read_payload(args.payload)
    if not payload:
        row = store.nanopub_publish_row(hub_id)
        if row is not None:
            payload = row.grounding
    bundle = evidence.load_bundle(store, hub_id)
    hub_ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    violations = gates.run_mint_gates(
        store, bundle, payload, hub_meta=hub_ref.meta or {}, at_sign=True
    )
    if not violations:
        print("all mint gates pass")
        return
    for v in violations:
        print(f"[{v.gate}] {v.message}")
    sys.exit(1)


def _sign(args: argparse.Namespace, store) -> None:
    from precis.nanopub import mint

    row = mint.sign(
        store,
        _hub_id(args.hub),
        role="attesting" if args.attest else "bot",
        # This CLI subcommand IS the interactive surface — a person runs it.
        interactive=args.attest,
        llm_models=args.llm_model,
    )
    print(f"signed fi{row.claim_ref_id}: {row.trusty_uri} (artifact {row.artifact_id})")


def _reopen(args: argparse.Namespace, store) -> None:
    hub_id = _hub_id(args.hub)
    row = store.nanopub_publish_row(hub_id)
    if row is None:
        print("no live publish row")
        sys.exit(1)
    if store.nanopub_reopen(row.id):
        print(f"reopened publish row {row.id} → candidate")
    else:
        print(f"row {row.id} is {row.state!r} — only reviewed/signed reopen")
        sys.exit(1)


def _view(args: argparse.Namespace, store) -> None:
    from precis.handlers._finding_nanopub import render_nanopub_view

    hub_id = _hub_id(args.hub)
    ref = store.fetch_refs_by_ids([hub_id]).get(hub_id)
    if ref is None:
        print(f"no live ref {hub_id}", file=sys.stderr)
        sys.exit(1)
    print(render_nanopub_view(store, ref).body)


def _anchor(args: argparse.Namespace, store) -> None:
    from precis.nanopub import ots

    if not args.live:
        print("anchor talks to the OTS calendar; re-run with --live")
        sys.exit(2)
    batch_id = ots.stamp_batch(store)
    print(f"stamped batch: {batch_id if batch_id is not None else 'nothing waiting'}")
    upgraded = ots.upgrade_sweep(store)
    print(f"upgraded batches: {upgraded or 'none'}")


def _audit(store) -> None:
    from precis.nanopub import ots

    findings = ots.audit(store)
    if not findings:
        print("proof store clean (bytes, extracts, roots, proofs all verify)")
        return
    for f in findings:
        print(f"[{f.kind}] {f.subject}: {f.message}")
    sys.exit(1)


def _preflight(args: argparse.Namespace, store) -> None:
    from precis.nanopub.preflight import publish_preflight

    issues = publish_preflight(store, _hub_id(args.hub))
    if not issues:
        print("clear to publish (every publish-time gate passes)")
        return
    blocking = False
    for i in issues:
        marker = "✖" if i.blocking else "·"
        blocking = blocking or i.blocking
        print(f"{marker} [{i.check}] {i.message}")
    if blocking:
        sys.exit(1)


def _signoff(args: argparse.Namespace, store) -> None:
    import getpass

    from precis.nanopub.preflight import signoff_edge

    # This CLI subcommand IS the interactive surface — a person runs it.
    ok = signoff_edge(
        store, args.link_id, by=getpass.getuser(), note=args.note, interactive=True
    )
    if ok:
        print(f"signed off evidence edge {args.link_id}")
    else:
        print(f"no evidence edge with link id {args.link_id}")
        sys.exit(1)


def _allow(args: argparse.Namespace, store) -> None:
    if args.allow_cmd == "list":
        entries = store.nanopub_allowlist()
        if not entries:
            print("allowlist empty — nothing is publishable")
            return
        for e in entries:
            window = (
                "open" if e.valid_until is None else f"closed {e.valid_until:%Y-%m-%d}"
            )
            role = "ATTESTING" if e.attesting else "bot"
            print(
                f"{e.id:4d}  {role:>9}  {e.identity_uri}  "
                f"{e.key_fingerprint[:16]}…  {window}  {e.note}"
            )
    elif args.allow_cmd == "add":
        entry_id = store.nanopub_allowlist_add(
            identity_uri=args.identity_uri,
            key_fingerprint=args.fingerprint,
            attesting=args.attesting,
            note=args.note,
        )
        print(f"pinned entry {entry_id} ({'attesting' if args.attesting else 'bot'})")
    elif args.allow_cmd == "end":
        if store.nanopub_allowlist_end(args.entry_id):
            print(f"closed validity window of entry {args.entry_id}")
        else:
            print(f"entry {args.entry_id} not found or already closed")
            sys.exit(1)


def _publish(args: argparse.Namespace, store) -> None:
    from precis.nanopub import registry

    # This CLI subcommand IS the interactive surface — a person runs it,
    # and --live is the explicit point-of-no-return acknowledgement.
    result = registry.publish(
        store, _hub_id(args.hub), live=args.live, interactive=True
    )
    for note in result.notes:
        print(f"· [{note.check}] {note.message}")
    if result.live:
        print(
            f"PUBLISHED fi{result.hub_ref_id}: {result.trusty_uri}\n"
            f"→ {result.registry_url} ({result.byte_count} bytes) — "
            "propagating across mirrors; from here, change = supersede"
        )
    else:
        print(
            f"dry run — would POST {result.byte_count} bytes of "
            f"{result.trusty_uri}\nto {result.registry_url}; re-run with "
            "--live to publish (irreversible)"
        )
