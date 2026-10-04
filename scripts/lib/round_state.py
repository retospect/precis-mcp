"""Shared, stdlib-only lifecycle reader for round and shell publication guards.

Absence permits legacy main operation. Present but invalid identities/phases
refuse: truthiness must never turn a broken recovery journal into permission.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


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
    if "release" in data:
        rel = data["release"]
        if (
            not isinstance(rel, dict)
            or rel.get("branch") != f"release/r{data['n']}"
            or not _sha(rel.get("base"))
            or not data["open"]
        ):
            raise ValueError("invalid release identity")
        if "deployment" in rel and not _journal(rel["deployment"]):
            raise ValueError("invalid deployment journal")
        if "cut_pending" in rel and type(rel["cut_pending"]) is not bool:
            raise ValueError("invalid cut intent")
    if "deployed" in data:
        receipt = data["deployed"]
        if (
            not _journal(receipt, completed=True)
            or receipt.get("tag") != f"deployed/r{data['n']}"
            or receipt.get("branch") != f"release/r{data['n']}"
            or not _text(receipt, "at")
        ):
            raise ValueError("invalid completed receipt")
    return data


if __name__ == "__main__":
    try:
        state = read_round(Path(sys.argv[1]))
        rel = state.get("release")
        blocked = (
            bool(rel)
            if sys.argv[2] == "publication"
            else bool(rel and ("deployment" in rel or rel.get("cut_pending")))
        )
    except (OSError, ValueError):
        sys.exit(1)
    sys.exit(1 if blocked else 0)
