"""Parked-gripe wake conditions (gr543711) — ``refs.meta->'until'``.

A ``STATUS:parked`` gripe waits on one condition, stored on the gripe's
meta under ``until`` as ``{"type", "value", "note"}`` (``note``
optional). Types:

- ``ref_done`` — ``value`` a gripe/todo handle (``gr<N>``/``td<N>``);
  met when that ref reaches a terminal STATUS.
- ``sha_in_prod`` — ``value`` a hex sha; met when it is in the recorded
  prod head (``release.prod_head``, read by the wake pass via
  :func:`precis.plans.gates` — the server has no git).
- ``spec_landed`` — ``value`` a ``docs/backlog/<slug>.md`` path; met when
  the spec is ready or gone (shipped). The DB-only wake pass cannot see
  the repo, so it reports the condition "not evaluable here" — a human
  unparks, or a ``text`` condition covers it.
- ``date`` — ``value`` an ISO-8601 date/datetime; met when UTC now
  reaches it.
- ``text`` — ``value`` free text; the wake pass asks the cheapest LLM
  tier whether the condition (plus the gripe's newest comments) is met;
  ``unsure`` counts as not met.

``until=`` also takes a shorthand string on the parking calls:
``gr<N>``/``td<N>`` → ``ref_done``, a 9-40-hex sha → ``sha_in_prod``, an
ISO date → ``date``, anything else → ``text``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.plans.gates import MIN_SHA_LEN

if TYPE_CHECKING:
    from precis.store import Store

UNTIL_TYPES: frozenset[str] = frozenset(
    {"ref_done", "sha_in_prod", "spec_landed", "date", "text"}
)

_REF_RE = re.compile(r"^(gr|td)(\d+)$", re.IGNORECASE)
_SHA_RE = re.compile(rf"^[0-9a-f]{{{MIN_SHA_LEN},40}}$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([Tt ].*)?$")
_SPEC_RE = re.compile(r"^docs/backlog/[\w.-]+\.md$")


def parse_until_date(raw: str) -> datetime:
    """An ISO date/datetime as an aware UTC instant (naive ⇒ UTC)."""
    text = raw.strip()
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise BadInput(f"until value {raw!r} is not ISO-8601") from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _ref_kind(handle: str) -> tuple[str, int]:
    """``gr<N>``/``td<N>`` → ``(kind, ref_id)``; anything else raises."""
    m = _REF_RE.match(handle.strip())
    if m is None:
        raise BadInput(
            f"until ref_done value {handle!r} is not a gripe/todo handle",
            next="name it as 'gr<N>' or 'td<N>'",
        )
    return ("gripe" if m.group(1).lower() == "gr" else "todo", int(m.group(2)))


def until_shorthand(text: str) -> dict[str, Any]:
    """Map a bare ``until=`` string onto a condition type."""
    s = text.strip()
    if _REF_RE.match(s):
        kind, _pk = _ref_kind(s)
        handle = ("gr" if kind == "gripe" else "td") + s[2:]
        return {"type": "ref_done", "value": handle}
    if _SHA_RE.match(s) and len(s) >= 9:
        return {"type": "sha_in_prod", "value": s.lower()}
    if _DATE_RE.match(s):
        parse_until_date(s)  # validates
        return {"type": "date", "value": s}
    return {"type": "text", "value": s}


def normalize_until(raw: Any, *, store: Store | None = None) -> dict[str, Any]:
    """Validate ``raw`` into the stored ``{type, value, note}`` shape.

    ``raw`` is the caller's ``until`` value: a dict already in the
    condition shape, or a shorthand string (:func:`until_shorthand`).
    ``store`` lets ``ref_done`` verify the named ref exists. Raises
    ``BadInput`` on anything malformed.
    """
    if isinstance(raw, str):
        raw = until_shorthand(raw)
    if not isinstance(raw, dict):
        raise BadInput(
            f"until must be a dict {{type, value, note}} or a shorthand string, "
            f"got {type(raw).__name__}",
            next="until={'type': 'ref_done', 'value': 'gr123'}",
        )
    typ = raw.get("type")
    if typ not in UNTIL_TYPES:
        raise BadInput(
            f"until type {typ!r} is not one of {sorted(UNTIL_TYPES)}",
            options=sorted(UNTIL_TYPES),
            next="until={'type': 'ref_done', 'value': 'gr123'}",
        )
    value = raw.get("value")
    out: dict[str, Any] = {"type": typ}
    if not isinstance(value, str) or not value.strip():
        raise BadInput(
            f"until type {typ!r} needs a non-empty string value",
            next="until={'type': 'ref_done', 'value': 'gr123'}",
        )
    value = value.strip()
    if typ == "ref_done":
        kind, ref_id = _ref_kind(value)
        if store is not None and store.get_ref(kind=kind, id=ref_id) is None:
            raise BadInput(
                f"until ref_done: {value} does not exist — park on a live ref",
                next="list candidates with get(kind='gripe', id='/open')",
            )
        out["value"] = ("gr" if kind == "gripe" else "td") + str(ref_id)
    elif typ == "sha_in_prod":
        if _SHA_RE.match(value) is None or len(value) < 9:
            raise BadInput(
                f"until sha_in_prod value {value!r} is not a hex sha of 9-40 chars",
                next="until=<sha> — the commit to wait for",
            )
        out["value"] = value.lower()
    elif typ == "spec_landed":
        if _SPEC_RE.match(value) is None:
            raise BadInput(
                f"until spec_landed value {value!r} is not a docs/backlog *.md path",
                next="until={'type': 'spec_landed', 'value': 'docs/backlog/<slug>.md'}",
            )
        out["value"] = value
    elif typ == "date":
        out["value"] = parse_until_date(value).isoformat()
    else:  # text
        out["value"] = value
    if isinstance(raw.get("note"), str) and raw["note"].strip():
        out["note"] = raw["note"].strip()
    return out


def describe_until(until: Any) -> str:
    """``ref_done gr123`` / ``sha_in_prod abc1234`` — one line, for lists
    and the unpark comment."""
    if not isinstance(until, dict):
        return "until (none recorded)"
    typ = until.get("type", "?")
    value = str(until.get("value") or "")
    note = str(until.get("note") or "").strip()
    short = value if typ != "sha_in_prod" else value[:9]
    desc = f"{typ} {short}".rstrip()
    return f"{desc} ({note})" if note else desc
