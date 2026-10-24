"""Shared, stdlib-only lifecycle reader for round and shell publication guards.

round.json holds ``releases``: a list of release records, each
``{branch: release/r<N>, n: N, base: <sha>, cut_at, state, cut_pending?,
deployment?}``. ``state`` is one of cut|gating|fixing|green (the *open*
release), deploying|deployed (a live rollout journal) or
confirmed|superseded|abandoned (terminal). A pre-list file's single
``release`` dict is normalized into a one-element ``releases`` on read — the
file upgrades to the list shape on its next write.

Pipeline invariants, enforced here so a broken journal can never read as
permission: at most one open release and at most one deploying/deployed
record; a deployment journal only on deploying/deployed/superseded;
``deployed`` requires rollout_at, ``confirmed`` the runtime proof, and a
live release only inside an open round.

Absence permits legacy main operation. Present but invalid identities/phases
refuse: truthiness must never turn a broken recovery journal into permission.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

OPEN_STATES = frozenset({"cut", "gating", "fixing", "green"})
DEPLOYING_STATES = frozenset({"deploying", "deployed"})
TERMINAL_STATES = frozenset({"confirmed", "superseded", "abandoned"})
RELEASE_STATES = OPEN_STATES | DEPLOYING_STATES | TERMINAL_STATES


def _text(data: dict[str, Any], key: str) -> bool:
    return isinstance(data.get(key), str) and bool(data[key].strip())


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None
    )


def _runtime_proof(proof: Any) -> bool:
    if not isinstance(proof, dict) or not all(
        _text(proof, k)
        for k in ("runtime_confirmed_at", "runtime_evidence", "confirmed_by")
    ):
        return False
    receipt = proof.get("runtime_receipt")
    return (
        isinstance(receipt, dict)
        and _text(receipt, "content")
        and all(
            type(receipt.get(k)) is int and receipt[k] >= 0
            for k in ("mtime_ns", "inode")
        )
    )


def _journal(journal: Any, *, completed: bool = False) -> bool:
    if (
        not isinstance(journal, dict)
        or not _sha(journal.get("sha"))
        or not _text(journal, "started_at")
    ):
        return False
    ci = journal.get("ci")
    if not isinstance(ci, dict) or ci.get("sha") != journal["sha"]:
        return False
    if "started_epoch" in journal and (
        type(journal["started_epoch"]) is not int or journal["started_epoch"] < 0
    ):
        return False
    if "rollout_at" in journal or completed:
        if not _text(journal, "rollout_at") or "started_epoch" not in journal:
            return False
    if "runtime_confirmed_at" in journal or completed:
        if not _text(journal, "rollout_at") or not _runtime_proof(journal):
            return False
    if "superseded_runtime" in journal and (
        not isinstance(journal["superseded_runtime"], list)
        or not all(_runtime_proof(proof) for proof in journal["superseded_runtime"])
    ):
        return False
    return True


def _derive_state(rel: dict[str, Any]) -> str:
    """State for a record that predates the field (or omits it): its journal."""
    dep = rel.get("deployment")
    if dep is None:
        return "cut"
    return "deployed" if dep.get("rollout_at") else "deploying"


def _norm_record(rel: Any) -> dict[str, Any]:
    if not isinstance(rel, dict):
        raise ValueError("invalid release record")
    branch = rel.get("branch")
    m = re.fullmatch(r"release/r(\d+)", branch) if isinstance(branch, str) else None
    if m is None or not _sha(rel.get("base")):
        raise ValueError("invalid release identity")
    n = int(m.group(1))
    if "n" in rel and rel["n"] != n:
        raise ValueError("invalid release identity")
    state = rel.get("state")
    if state is None:
        state = _derive_state(rel)
    if state not in RELEASE_STATES:
        raise ValueError("invalid release state")
    dep = rel.get("deployment")
    if dep is not None and not _journal(dep):
        raise ValueError("invalid deployment journal")
    if dep is not None and state not in DEPLOYING_STATES | {
        "confirmed",
        "superseded",
    }:
        raise ValueError("deployment journal on a non-deploying release")
    if dep is None and state in DEPLOYING_STATES | {"confirmed"}:
        raise ValueError("invalid release state")
    if state == "deployed" and not (dep or {}).get("rollout_at"):
        raise ValueError("invalid release state")
    if state == "confirmed" and not (dep or {}).get("runtime_confirmed_at"):
        raise ValueError("invalid release state")
    if "cut_pending" in rel and (
        type(rel["cut_pending"]) is not bool or state != "cut"
    ):
        raise ValueError("invalid cut intent")
    rel["n"] = n
    rel["state"] = state
    return rel


def read_round(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("dangling state link") from None
        return {}
    if (
        not isinstance(data, dict)
        or type(data.get("n")) is not int
        or data["n"] < 1
        or type(data.get("open")) is not bool
    ):
        raise ValueError("invalid lifecycle record")
    rels: list[dict[str, Any]] = []
    if "releases" in data:
        if not isinstance(data["releases"], list):
            raise ValueError("invalid release list")
        rels = [_norm_record(r) for r in data["releases"]]
    if "release" in data:
        if "releases" in data:
            raise ValueError("release recorded twice (old and new shape)")
        rel = data["release"]
        if (
            not isinstance(rel, dict)
            or rel.get("branch") != f"release/r{data['n']}"
            or not data["open"]
        ):
            raise ValueError("invalid release identity")
        rels = [_norm_record(rel)]
        del data["release"]
    open_n = sum(r["state"] in OPEN_STATES for r in rels)
    dep_n = sum(r["state"] in DEPLOYING_STATES for r in rels)
    if open_n > 1 or dep_n > 1:
        raise ValueError("release list violates the pipeline invariants")
    if (open_n or dep_n) and not data["open"]:
        raise ValueError("live release inside a closed round")
    data["releases"] = rels
    if "deployed" in data:
        receipt = data["deployed"]
        rn = receipt.get("n", data["n"]) if isinstance(receipt, dict) else None
        if (
            type(rn) is not int
            or rn < 1
            or not _journal(receipt, completed=True)
            or receipt.get("tag") != f"deployed/r{rn}"
            or receipt.get("branch") != f"release/r{rn}"
            or not _text(receipt, "at")
        ):
            raise ValueError("invalid completed receipt")
    return data


def _open_record(rels: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((r for r in rels if r["state"] in OPEN_STATES), None)


def _next_n(state: dict[str, Any], rels: list[dict[str, Any]]) -> int:
    """The release number the next cut would mint (best-effort, for messages)."""
    if rels:
        return max(r["n"] for r in rels) + 1
    n = state.get("n") or 0
    return n + 1 if state.get("deployed") else n


if __name__ == "__main__":
    path, mode = Path(sys.argv[1]), sys.argv[2]
    try:
        state = read_round(path)
    except (OSError, ValueError):
        sys.exit(2 if mode == "open-release" else 1)
    rels = state.get("releases", [])
    if mode == "open-release":
        rec = _open_record(rels)
        if rec is not None:
            print(rec["branch"])
            sys.exit(0)
        nxt = _next_n(state, rels)
        print(f"next r{nxt}" if nxt else "next")
        sys.exit(1)
    if mode == "publication":
        blocked = any(r["state"] not in TERMINAL_STATES for r in rels)
    else:
        # journal <branch>: the named record's pending cut or live deploy
        # journal owns its head; a different release's journal does not block
        # a fix on this one. No branch named: any live journal blocks.
        branch = sys.argv[3] if len(sys.argv) > 3 else ""
        blocked = bool(
            next(
                (
                    r
                    for r in rels
                    if r["state"] not in TERMINAL_STATES
                    and (not branch or r.get("branch") == branch)
                    and ("deployment" in r or r.get("cut_pending"))
                ),
                None,
            )
        )
    sys.exit(1 if blocked else 0)
