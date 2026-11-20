"""Failure classification for untrusted barrier stamps (gr593138).

Every barrier that harvests ``barrier_trusted: False`` carries a failure
class — ``mechanical`` or ``science`` — so the automatic lanes recover the
machine problems on their own and only the science problems reach the quest
agent or a human.

Mechanical (auto-requeue on the current engine under a retry cap, a GPU-hour
budget and a cooldown — and EXEMPT from the ``fidelity_restale_cap=0`` hold,
Reto ruling 2026-11-12): stale engine (``engine:stale`` / ``engine:unknown``
— the restale re-measure), seed shortfalls (``seeds:<n>-independent`` /
``seeds:unknown`` / ``endpoint:union-not-run`` — same science, more seeds;
the harvest logbook already prescribes "rerun with fresh seed numbers"),
``anchor:none``, and the infra/timeout markers (the candidate's infra-retry
counters and the seed-timeout note stamp — job-level ``failure_class``
``'infra'``/``'timeout'`` recovery lives in harvest's existing retry arms).

Science (the fix changes the science — to the quest agent with the blocker
named, never an automatic re-queue): endpoint split
(``endpoint:union-disagree`` / a failing ``endpoint_agreement`` record),
detachment (``barrier_desorbed`` / ``detachment`` records), wrong binder
(``barrier_wrong_site`` / ``wrong_binder`` records), absurd magnitude
(``barrier_absurd`` / ``absurd_magnitude`` records), twin disagreement
(``barrier_twin_disagreement`` — irreproducibility between same-crystal
twins), and ``no_route`` (an invalid upstream job — refused, not re-queued;
fix the input instead).

Both classes present → science wins: a rerun would spend compute without
addressing the science reason. Unrecognized → science: an unclassified
untrusted stamp goes to a human, never an automatic re-queue (fail toward
the human). A candidate whose meta carries no untrusted barrier verdict
classifies as ``None`` — the class is a property of the stamp, not of the
candidate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MECHANICAL = "mechanical"
SCIENCE = "science"

#: Blocker-id prefixes that name a machine problem — re-running the same
#: science on the current engine can clear them. Record ids (not synthetic
#: blockers) never match these prefixes, so a real check's id can't be
#: mistaken for a synthetic one.
_MECHANICAL_BLOCKER_PREFIXES: tuple[str, ...] = (
    "engine:stale",
    "engine:unknown",
    "seeds:",
    "endpoint:union-not-run",
    "anchor:none",
)

#: Blocker-id tokens that name a science problem.
_SCIENCE_BLOCKER_TOKENS: tuple[str, ...] = (
    "endpoint:union-disagree",
    "no_route",
)

#: Substrings of a ``blocked_by`` record id naming the failing check — the
#: id format is ``<step>#s<seed>#<check>`` (e.g. ``R->M#s0#detachment``), so
#: the check name rides the id verbatim.
_SCIENCE_RECORD_TOKENS: tuple[str, ...] = (
    "detachment",
    "wrong_binder",
    "endpoint_agreement",
    "absurd_magnitude",
)

#: Candidate-meta flags whose presence is a science reason in its own right.
_SCIENCE_FLAG_KEYS: tuple[str, ...] = (
    "barrier_absurd",
    "barrier_desorbed",
    "barrier_wrong_site",
    "barrier_twin_disagreement",
)

#: Candidate-meta counters/stamps marking an infra retry in flight (or one
#: already spent) and a seed killed by the wall clock — machine problems.
_INFRA_COUNTER_KEYS: tuple[str, ...] = (
    "quest_infra_retries",
    "quest_autocatpath_infra_retries",
    "quest_seed_infra_retries",
)

#: Display labels for the reasons :func:`untrusted_reasons` can name.
_LABELS: dict[str, str] = {
    "engine:stale": "stale engine",
    "engine:unknown": "unstamped engine",
    "seeds:": "insufficient seeds",
    "endpoint:union-not-run": "pooled seeds without cross-seed evidence",
    "anchor:none": "no credit anchor",
    "endpoint:union-disagree": "endpoint split",
    "no_route": "invalid upstream job (no route)",
    "detachment": "detachment",
    "wrong_binder": "wrong binder",
    "endpoint_agreement": "endpoint split",
    "absurd_magnitude": "absurd magnitude",
    "barrier_absurd": "absurd magnitude",
    "barrier_desorbed": "detachment",
    "barrier_wrong_site": "wrong binder",
    "barrier_twin_disagreement": "twin disagreement",
    "infra": "infra retry",
    "timeout": "timeout",
}


def _blocker_reasons(meta: Mapping[str, Any]) -> list[str]:
    """Reason tokens out of the blocker-id lists (``barrier_blocked_by`` and
    ``barrier_seed_blocker``): synthetic ids by prefix, record ids by their
    embedded check name."""
    out: list[str] = []
    blocked_by = meta.get("barrier_blocked_by")
    ids = (
        [str(b) for b in blocked_by if isinstance(b, (str, int))]
        if isinstance(blocked_by, list)
        else []
    )
    seed_blocker = meta.get("barrier_seed_blocker")
    if isinstance(seed_blocker, str) and seed_blocker:
        ids.append(seed_blocker)
    for bid in ids:
        if any(bid.startswith(p) for p in _MECHANICAL_BLOCKER_PREFIXES):
            out.append(
                next(p for p in _MECHANICAL_BLOCKER_PREFIXES if bid.startswith(p))
            )
            continue
        if any(t in bid for t in _SCIENCE_BLOCKER_TOKENS) or any(
            t in bid for t in _SCIENCE_RECORD_TOKENS
        ):
            out.extend(
                t
                for t in (*_SCIENCE_BLOCKER_TOKENS, *_SCIENCE_RECORD_TOKENS)
                if t in bid
            )
    return out


def untrusted_reasons(meta: Mapping[str, Any]) -> list[str]:
    """The human-readable reason labels of an untrusted barrier stamp, in a
    stable order, deduplicated — the names a logbook entry or an agent
    prompt can cite. Empty for a candidate with no untrusted verdict."""
    if meta.get("barrier_trusted") is not False:
        return []
    tokens: list[str] = []
    tokens.extend(_blocker_reasons(meta))
    for key in _SCIENCE_FLAG_KEYS:
        v = meta.get(key)
        if v or (isinstance(v, int) and not isinstance(v, bool) and v > 0):
            tokens.append(key)
    if any(
        isinstance(meta.get(k), int)
        and not isinstance(meta.get(k), bool)
        and int(meta[k]) > 0
        for k in _INFRA_COUNTER_KEYS
    ):
        tokens.append("infra")
    if meta.get("quest_seed_timeout_noted") is not None:
        tokens.append("timeout")
    labels: list[str] = []
    for tok in tokens:
        label = _LABELS.get(tok, tok)
        if label not in labels:
            labels.append(label)
    return labels


def classify_untrusted(meta: Mapping[str, Any]) -> str | None:
    """The failure class of an untrusted barrier stamp: ``MECHANICAL`` (the
    automatic lanes may requeue on the current engine under retry/cost caps)
    or ``SCIENCE`` (the fix changes the science — to the quest agent or a
    human with the blocker named). ``None`` when the meta carries no
    untrusted barrier verdict.

    Science wins on a mixed stamp, and an unrecognized untrusted stamp
    classifies science — fail toward the human, never toward an automatic
    re-queue."""
    if meta.get("barrier_trusted") is not False:
        return None
    # A flag key (truthy, or a positive int count) is a science reason in
    # its own right; a blocker id is science or mechanical by its prefix /
    # embedded check name; the infra counters and the timeout stamp are
    # machine problems.
    if any(meta.get(k) for k in _SCIENCE_FLAG_KEYS):
        return SCIENCE
    blocker_reasons = _blocker_reasons(meta)
    if any(
        t in _SCIENCE_BLOCKER_TOKENS or t in _SCIENCE_RECORD_TOKENS
        for t in blocker_reasons
    ):
        return SCIENCE
    if any(t in _MECHANICAL_BLOCKER_PREFIXES for t in blocker_reasons) or any(
        meta.get(k) for k in _INFRA_COUNTER_KEYS
    ):
        return MECHANICAL
    if meta.get("quest_seed_timeout_noted") is not None:
        return MECHANICAL
    return SCIENCE
