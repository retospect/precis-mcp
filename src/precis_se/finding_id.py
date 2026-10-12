"""Stable ids and a stateless delta for ``se`` check findings.

Same contract as ``pcb`` (:func:`precis.pcb.drc.finding_key`, hash step shared
in :mod:`precis.finding_id`): a finding's id is derived from ``(rule,
canonicalised participants)`` — the ``subject`` split on ``—`` and sorted, so
the two blocks of an interference key the same either way round — never from
detail text, a measured value or list position. A depth change that leaves the
blocks involved alone keeps the id.

``se`` recomputes on every call and stores nothing, so the delta is
caller-supplied: ``args={'previous': [<ids>]}`` (list, or comma/space-separated
string) from the earlier report is diffed into new / still / gone. Several
findings may share an id (one rule, one subject, different detail); the diff
treats ids as a multiset so a second hit on a standing id reads ``new``."""

from __future__ import annotations

import collections
import re
from collections.abc import Sequence
from typing import Any

from precis.errors import BadInput
from precis.finding_id import finding_hash

_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SEP = "—"


def finding_id(rule: str, subject: str) -> str:
    """The id of a finding by ``rule`` and ``subject`` alone."""
    parts = sorted(p.strip() for p in subject.split(_SEP) if p.strip())
    return finding_hash(rule, parts or [subject])


def parse_previous(args: dict[str, Any] | None) -> list[str] | None:
    """``args['previous']`` as a list of ids, or ``None`` when absent.
    Refuses anything that is not a 12-hex id so a typo cannot read as
    "everything is new"."""
    raw = (args or {}).get("previous")
    if raw is None:
        return None
    if isinstance(raw, str):
        items: Sequence[Any] = re.split(r"[,\s]+", raw.strip())
    elif isinstance(raw, Sequence):
        items = raw
    else:
        raise BadInput("args['previous'] must be a list of finding ids")
    ids = [str(i).strip() for i in items if str(i).strip()]
    bad = [i for i in ids if not _ID_RE.match(i)]
    if bad:
        raise BadInput(
            f"args['previous']: not finding ids (12 hex chars): {', '.join(bad[:3])}"
        )
    return ids


def annotate(
    rows: list[dict[str, Any]],
    ids: list[str],
    previous: list[str] | None,
) -> tuple[list[dict[str, Any]], str]:
    """Add an ``id`` column (and ``delta`` when ``previous`` is given) to
    ``rows`` (parallel to ``ids``); return the rows and a text block (empty
    without ``previous``): the new/still/gone tally plus the gone ids."""
    out = [{"id": i, **r} for r, i in zip(rows, ids, strict=True)]
    if previous is None:
        return out, ""
    left = collections.Counter(previous)
    n_new = n_still = 0
    for row in out:
        if left[row["id"]] > 0:
            left[row["id"]] -= 1
            row["delta"] = "still"
            n_still += 1
        else:
            row["delta"] = "new"
            n_new += 1
    gone = sorted(left.elements())
    text = f"vs previous: {n_new} new, {n_still} still, {len(gone)} gone"
    if gone:
        text += "\n## gone since previous\n" + "\n".join(f"- {g}" for g in gone)
    return out, text
