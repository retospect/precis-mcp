"""``precis_diag`` — the shared diagnostics core (shared-core-layers S1a).

One structured failure/check record for every domain that reports to an
agent as prose today: CAD workspace runs (gr522025), pcb ops (gr522027),
hex/structure workspace runs (gr522031). The core holds only domain-free
code — no pad, atom, net or CAD import; stdlib-only. The
``precis-geom`` wheel carries this package, so workspace scripts get it
client-side under ``--network none``.

``Diag`` is a frozen dataclass, a superset of
``hexfold.report.Finding``: every Finding field has a Diag home
(``severity`` → ``status``, ``fix`` → ``next``, ``code``/``where``/
``span``/``data``/``message`` carry verbatim) plus the run-level
machinery Finding lacks — ``anchor`` (the stable frame key or op id a
failing call is cited by), ``refs`` (entity handles) and
``schema_version``. The conversion both ways —
:func:`diag_from_finding` / :func:`hexfold.diag.finding_from_diag` — is
tested in ``tests/test_precis_diag.py``. The forward conversion is
duck-typed; the reverse lives in hexfold's S2 adapter so the core never
imports a domain package, even lazily.

``Anchor`` names the user-facing op and its script call site. Its optional
``chain`` contains ``FrameSite`` values for the inner traced frames, outer
to inner; schema 1 serializes them as a list of ``{frame_key, file, line}``
objects. An absent chain reads as empty so older schema-1 records still
parse. ``Diag.code`` is namespaced ``<domain>.<code>`` with a lowercase
domain, such as ``cad.run-failed``; unqualified or empty codes refuse.

Status mapping (S1 fixes it — this table is the contract):

===========  ====================  ==================================  ===================
status       hexfold ``Severity``  ``ValidationIssue``/``DrcFinding``  meaning
===========  ====================  ==================================  ===================
ok           —                     — (no finding)                      check ran clean
info         INFO                  —                                   observation
warn         WARN                  ``"warn"``                          finding, non-fatal
error        ERROR                 ``"error"``                         finding, fatal
not_checked  —                     ``FitResult`` unchecked count       check could not run
===========  ====================  ==================================  ===================

``not_checked`` is a coverage gap, never silent clean: a declared check
that cannot fire reports ``not_checked`` rather than ``ok``.

``RunResult`` is the run-level record: the Diags one run produced plus a
``run`` pointer naming the run's ``trace.json``. Run identity
(``script_sha``, ``lock_sha``, params hash) stays in
``precis_geom.trace_schema`` — this layer points, it does not re-key.

``schema_version`` follows the ``trace_schema.SCHEMA_VERSIONS`` pattern:
readers accept every older version (the laptop wheel lags the server)
and refuse only newer ones, with a next step — :func:`diag_from_dict` /
:func:`run_result_from_dict` raise ``ValueError`` naming the upgrade;
the importer maps that to ``BadInput``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

#: The Diag wire schema this package writes.
SCHEMA_VERSION = 1

#: Schema versions the readers accept — older versions parse, newer refuse.
SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})


class Status(IntEnum):
    """A Diag's verdict. Ordered by severity: ``OK`` < ``INFO`` <
    ``NOT_CHECKED`` < ``WARN`` < ``ERROR`` — a check that could not run
    ranks above a clean observation and below a real finding."""

    OK = 0
    INFO = 1
    NOT_CHECKED = 2
    WARN = 3
    ERROR = 4


#: ``Status`` ↔ hexfold ``Severity`` / the ``"warn"``/``"error"`` string
#: severities — the one mapping table (package docstring is the contract).
_STATUS_FROM_SEVERITY_NAME = {
    "INFO": Status.INFO,
    "WARN": Status.WARN,
    "ERROR": Status.ERROR,
}


@dataclass(frozen=True)
class FrameSite:
    """One traced frame below an anchor, in outer-to-inner order."""

    frame_key: str = ""
    file: str = ""
    line: int = 0


@dataclass(frozen=True)
class Anchor:
    """Where a Diag attaches. ``frame_key`` is the trace frame's stable
    key (``trace_schema.frame_key`` — schema 2+); ``op_id`` is for
    scriptless ops (unused in S1a); ``file``/``line`` are the user's call
    site. ``chain`` holds inner traced frames, outer to inner, without
    repeating the anchored op. It is optional in schema 1 on the wire."""

    frame_key: str = ""
    op_id: str = ""
    file: str = ""
    line: int = 0
    chain: tuple[FrameSite, ...] = ()


@dataclass(frozen=True)
class Diag:
    """One structured record: a check outcome or a run failure.

    Superset of ``hexfold.report.Finding`` — see the package docstring.
    ``code`` is namespaced ``<domain>.<code>`` with a lowercase domain
    (for example, ``cad.run-failed``); empty/unqualified codes refuse.
    ``data`` keeps Finding's tuple-of-pairs shape (a scalar map —
    ``measured``, ``expected``, ``margin_mm``…) so the dataclass stays
    hashable and the conversion is verbatim."""

    status: Status
    message: str
    code: str
    anchor: Anchor = field(default_factory=Anchor)
    refs: tuple[str, ...] = ()
    next: str = ""
    data: tuple[tuple[str, Any], ...] = ()
    where: str = ""
    span: tuple[int, int] | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*\.[A-Za-z0-9][A-Za-z0-9_.-]*", self.code):
            raise ValueError(
                "diag: code must be '<domain>.<code>' with a lowercase domain"
            )

    def to_dict(self) -> dict[str, Any]:
        return diag_to_dict(self)


@dataclass(frozen=True)
class RunResult:
    """The run-level record: the Diags a run produced plus ``run``, a
    path to the run's ``trace.json`` (empty when unavailable)."""

    diags: tuple[Diag, ...] = ()
    run: str = ""

    @property
    def ok(self) -> bool:
        return not any(d.status >= Status.ERROR for d in self.diags)

    def to_dict(self) -> dict[str, Any]:
        return run_result_to_dict(self)


def diag_from_finding(f: Any) -> Diag:
    """``hexfold.report.Finding`` → ``Diag`` — duck-typed on Finding's
    fields so this module never imports the domain package."""
    return Diag(
        status=_STATUS_FROM_SEVERITY_NAME[f.severity.name],
        message=f.message,
        code=f.code,
        where=f.where,
        span=f.span,
        data=f.data,
        next=f.fix,
    )


def _sort_key(d: Diag) -> tuple:
    site = d.anchor.file or d.where
    return (-int(d.status), site, d.anchor.line, d.code, d.message)


def _site(d: Diag) -> str:
    """The anchor's rendered form: ``main.py:30 (frame ab12cd34)``,
    ``op <op_id>``, or the Finding-compat ``where``."""
    parts: list[str] = []
    if d.anchor.file:
        parts.append(
            f"{d.anchor.file}:{d.anchor.line}" if d.anchor.line else d.anchor.file
        )
    if d.anchor.frame_key:
        parts.append(f"frame {d.anchor.frame_key}")
    if d.anchor.op_id:
        parts.append(f"op {d.anchor.op_id}")
    if parts:
        return "(" + ", ".join(parts) + ") "
    if d.where:
        return f"{d.where} "
    return ""


def render(diags: Iterable[Diag]) -> str:
    """The one text renderer for Diag lists — the MCP error path's
    shape: ``<status> <code> (<site>) <message>`` plus indented
    ``next:``/``refs:`` continuations. ``"ok"`` on an empty list."""
    lines: list[str] = []
    for d in sorted(diags, key=_sort_key):
        code = f"{d.code} "
        lines.append(
            f"{d.status.name.lower():>11} {code}{_site(d)}{d.message}".rstrip()
        )
        if d.next:
            lines.append(f"{'':>11} next: {d.next}")
        if d.refs:
            lines.append(f"{'':>11} refs: {', '.join(d.refs)}")
    return "\n".join(lines) if lines else "ok"


def _anchor_from_dict(raw: Any) -> Anchor:
    if not isinstance(raw, dict):
        raise ValueError("diag: 'anchor' must be an object")
    chain = raw.get("chain", [])
    if not isinstance(chain, list) or any(not isinstance(f, dict) for f in chain):
        raise ValueError("diag: 'anchor.chain' must be a list of objects")
    return Anchor(
        frame_key=str(raw.get("frame_key") or ""),
        op_id=str(raw.get("op_id") or ""),
        file=str(raw.get("file") or ""),
        line=int(raw.get("line") or 0),
        chain=tuple(
            FrameSite(
                frame_key=str(f.get("frame_key") or ""),
                file=str(f.get("file") or ""),
                line=int(f.get("line") or 0),
            )
            for f in chain
        ),
    )


def _check_version(raw: dict[str, Any], what: str) -> None:
    v = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(v, int) or v > SCHEMA_VERSION:
        raise ValueError(
            f"{what}: schema_version {v!r} is newer than this reader's "
            f"{SCHEMA_VERSION} — next: upgrade precis-geom"
        )
    if v not in SCHEMA_VERSIONS:
        raise ValueError(
            f"{what}: schema_version {v!r} not in {sorted(SCHEMA_VERSIONS)}"
        )


def diag_to_dict(d: Diag) -> dict[str, Any]:
    """Canonical dict form — ``schema_version`` always written."""
    out: dict[str, Any] = {
        "schema_version": d.schema_version,
        "status": d.status.name.lower(),
        "message": d.message,
        "code": d.code,
    }
    if d.anchor != Anchor():
        out["anchor"] = {
            k: v
            for k, v in (
                ("frame_key", d.anchor.frame_key),
                ("op_id", d.anchor.op_id),
                ("file", d.anchor.file),
                ("line", d.anchor.line),
                (
                    "chain",
                    [
                        {"frame_key": f.frame_key, "file": f.file, "line": f.line}
                        for f in d.anchor.chain
                    ],
                ),
            )
            if v
        }
    if d.refs:
        out["refs"] = list(d.refs)
    if d.next:
        out["next"] = d.next
    if d.data:
        out["data"] = {k: v for k, v in d.data}
    if d.where:
        out["where"] = d.where
    if d.span is not None:
        out["span"] = list(d.span)
    return dict(sorted(out.items()))


def diag_from_dict(raw: Any) -> Diag:
    """Read a Diag dict back. Accepts every ``schema_version`` in
    ``SCHEMA_VERSIONS`` (older wheels), refuses newer with a next step.
    Preserves the record's original schema_version; reading is not an upgrade.
    """
    if not isinstance(raw, dict):
        raise ValueError("diag: must be an object")
    _check_version(raw, "diag")
    try:
        status = Status[str(raw["status"]).upper()]
    except KeyError:
        raise ValueError(
            f"diag: unknown status {raw.get('status')!r} — "
            f"options: {', '.join(s.name.lower() for s in Status)}"
        ) from None
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("diag: 'data' must be an object")
    span = raw.get("span")
    return Diag(
        status=status,
        message=str(raw.get("message") or ""),
        code=str(raw.get("code") or ""),
        anchor=_anchor_from_dict(raw.get("anchor") or {}),
        refs=tuple(str(r) for r in raw.get("refs") or ()),
        next=str(raw.get("next") or ""),
        data=tuple((str(k), v) for k, v in data.items()),
        where=str(raw.get("where") or ""),
        span=(int(span[0]), int(span[1]))
        if isinstance(span, (list, tuple)) and len(span) == 2
        else None,
        schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
    )


def run_result_to_dict(r: RunResult) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run": r.run,
        "diags": [diag_to_dict(d) for d in r.diags],
    }


def run_result_from_dict(raw: Any) -> RunResult:
    """Read a ``RunResult`` dict — same version rule as
    :func:`diag_from_dict`."""
    if not isinstance(raw, dict):
        raise ValueError("run_result: must be an object")
    _check_version(raw, "run_result")
    diags_raw = raw.get("diags")
    if not isinstance(diags_raw, list):
        raise ValueError("run_result: 'diags' must be a list")
    return RunResult(
        diags=tuple(diag_from_dict(d) for d in diags_raw),
        run=str(raw.get("run") or ""),
    )


__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "Anchor",
    "Diag",
    "FrameSite",
    "RunResult",
    "Status",
    "diag_from_dict",
    "diag_from_finding",
    "diag_to_dict",
    "render",
    "run_result_from_dict",
    "run_result_to_dict",
]
