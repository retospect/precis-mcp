"""``precis.pcb.diag`` — the pcb domain's Diag adapter
(gr522027, shared-core-layers S1b).

Two failure surfaces, one record:

* **Board scripts are traced** — ``precis_geom.pcb`` ops are
  ``@traced``, so a failed ``out.run`` anchors on the op's stable
  ``frame_key`` (plus the script's ``file:line`` call site). The
  builder stamps ``diag_refs``/``diag_code`` on its raises;
  :func:`precis_geom.out._error_entry` serializes them onto
  ``manifest.error`` and
  :func:`precis_geom.run_diag.failed_run_result` folds them into the
  run-failed Diag — ``failed_run_result`` below is the pcb domain's
  named door to that shared adapter.
* **Scriptless ops have no trace** — ``pcb.local``'s
  place/route/topo_place/drc/run raise off-trace, so their Diags anchor
  on ``op_id`` (the op's name) with ``file:line`` of the caller's own
  site. The :func:`op` decorator does the stamping: a raise leaves with
  ``exc.diag`` set, innermost stamp wins (a ``run`` whose ``place``
  step failed records ``place``, the failing step).

Findings convert both ways: :func:`drc_to_diag` maps a
:class:`~precis.pcb.drc.DrcFinding` onto ``pcb.<rule>`` codes with the
finding's participants as ``refs`` (``part:<refdes>``,
``pad:<refdes>/<pin>``, ``fixed:<id>``… — the
:func:`~precis.pcb.drc.finding_identity` vocabulary), ``objects`` and
``margin_mm`` riding ``data`` so :func:`drc_from_diag` restores the
finding verbatim.

This module ships in the ``precis-geom`` wheel (it is not in
``packaging/geom/pyproject.toml``'s exclude list), so module-level
imports stay stdlib + ``precis_diag``; the domain imports
(``precis.pcb.drc``, ``precis_geom``) are lazy — ``drc.py`` needs the
``pcb-kernel`` extra's shapely.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from precis_diag import Anchor, Diag, RunResult, Status

#: ``DrcFinding.severity`` → ``Status`` — the only severities a finding
#: carries (the mapping table in ``precis_diag``'s docstring is the
#: contract).
_STATUS_FROM_SEVERITY = {"error": Status.ERROR, "warn": Status.WARN}
_SEVERITY_FROM_STATUS = {Status.ERROR: "error", Status.WARN: "warn"}

#: Frame ``__name__`` prefixes :func:`script_call_site` skips — every
#: first-party library frame, so the site lands on the caller's own
#: code no matter how much plumbing sits between.
_SKIP_PREFIXES = ("precis", "hexfold")

#: The ``<domain>.<code>`` shape ``Diag.__post_init__`` enforces — an
#: exception's ``code`` attr is kept only when it already IS a Diag
#: code (a vendor ``code=404`` or a bare word is not one).
_CODE_RE = re.compile(r"[a-z][a-z0-9_-]*\.[A-Za-z0-9][A-Za-z0-9_.-]*")


def drc_to_diag(f: Any) -> Diag:
    """One :class:`~precis.pcb.drc.DrcFinding` → its ``pcb.``-namespaced
    Diag: ``status`` from ``severity``, ``message`` the ``detail``,
    ``refs`` the finding's sorted participant identities, ``data``
    carrying ``objects`` (and ``margin_mm`` when set) so
    :func:`drc_from_diag` round-trips verbatim."""
    try:
        status = _STATUS_FROM_SEVERITY[f.severity]
    except KeyError:
        raise ValueError(
            f"drc_to_diag: severity {f.severity!r} — "
            f"options: {', '.join(sorted(_STATUS_FROM_SEVERITY))}"
        ) from None
    from precis.pcb import drc as _drc

    _rule, ids, _layer = _drc.finding_identity(f)
    data: list[tuple[str, Any]] = [
        ("objects", json.dumps([dict(o) for o in f.objects]))
    ]
    if f.margin_mm is not None:
        data.append(("margin_mm", f.margin_mm))
    return Diag(
        status=status,
        code="pcb." + f.rule.replace(":", "."),
        message=f.detail,
        where=f.where,
        refs=ids,
        data=tuple(data),
    )


def drc_from_diag(d: Diag) -> Any:
    """A ``pcb.`` Diag back to a :class:`~precis.pcb.drc.DrcFinding` —
    refuses non-``pcb.`` codes and statuses with no severity
    (``ok``/``info``/``not_checked`` are not findings), mirroring
    :func:`hexfold.diag.finding_from_diag`."""
    if not d.code.startswith("pcb."):
        raise ValueError(f"drc_from_diag: code {d.code!r} is not a pcb finding")
    if d.status not in _SEVERITY_FROM_STATUS:
        raise ValueError(
            f"drc_from_diag: status {d.status.name.lower()!r} has no DRC "
            "severity — ok/info/not_checked are not findings"
        )
    from precis.pcb.drc import DrcFinding

    data = dict(d.data)
    raw = data.get("objects") or ()
    objects = tuple(dict(o) for o in (json.loads(raw) if isinstance(raw, str) else raw))
    return DrcFinding(
        rule=d.code.removeprefix("pcb.").replace(".", ":", 1),
        severity=_SEVERITY_FROM_STATUS[d.status],
        where=d.where,
        detail=d.message,
        objects=objects,
        margin_mm=data.get("margin_mm"),
    )


def error_diag(
    exc: BaseException,
    *,
    op: str = "",
    code: str = "",
    refs: Iterable[str] = (),
    next: str = "",
    file: str = "",
    line: int = 0,
) -> Diag:
    """A failed pcb op as one ERROR Diag — ``message`` is the
    exception's own text, the anchor carries ``op_id`` (the scriptless
    op's name) and the caller's ``file``/``line`` site."""
    return Diag(
        status=Status.ERROR,
        code=code or (f"pcb.{op}-failed" if op else "pcb.op-failed"),
        message=str(exc),
        anchor=Anchor(op_id=op, file=file, line=line),
        refs=tuple(str(r) for r in refs),
        next=next,
    )


def script_call_site() -> tuple[str, int]:
    """``(file, line)`` of the first frame above this call that is not
    inside a first-party library — the board script's (or test's, or
    handler's) own call site, rendered relative to the script dir or
    cwd by :func:`precis_geom.pcb._frame_site`. ``("", 0)`` when the
    stack is all library."""
    f = inspect.currentframe()
    try:
        while f is not None:
            f = f.f_back
            if f is None:
                return "", 0
            mod = f.f_globals.get("__name__") or ""
            if mod.startswith(_SKIP_PREFIXES):
                continue
            from precis_geom.pcb import _frame_site

            file, _, line = _frame_site(f).rpartition(":")
            return file, int(line) if line.isdigit() else 0
        return "", 0
    finally:
        del f


def failed_run_result(
    manifest: dict[str, Any],
    trace_doc: dict[str, Any] | None,
    *,
    run: str = "",
) -> RunResult:
    """A failed board-script ``out.run`` → its RunResult — a thin
    delegate to :func:`precis_geom.run_diag.failed_run_result` (which
    reads ``manifest.error``'s ``code``/``refs`` — what the builder
    stamps as ``diag_code``/``diag_refs`` — plus the traced anchor).
    Exists so the pcb domain has a named adapter for the conformance
    registry."""
    from precis_geom.run_diag import failed_run_result as _failed_run_result

    return _failed_run_result(manifest, trace_doc, run=run)


def _stamp(exc: Any, d: Diag) -> None:
    """The stamped side of the :func:`op` contract — ``exc.diag`` for the
    scriptless path, ``diag_refs``/``diag_code`` for the traced manifest
    path — set as attributes on whatever exception the op raised."""
    exc.diag = d
    exc.diag_refs = d.refs
    exc.diag_code = d.code


def _valid_code(code: Any, op: str) -> str:
    """An exception's ``code``/``diag_code`` kept only when it is a str
    already matching the Diag ``<domain>.<code>`` pattern — a vendor
    ``code=404`` or a bare word falls back to ``pcb.<op>-failed``."""
    if isinstance(code, str) and _CODE_RE.fullmatch(code):
        return code
    return f"pcb.{op}-failed"


def _exc_code(exc: BaseException, op: str) -> str:
    """The op's Diag code: the exception's own ``code``/``diag_code``
    when valid, else a wrapped cause's ``diag_code`` (``local.py``'s
    ``raise LocalError(...) from exc`` keeps the inner
    ``PcbBuilderError``'s ``pcb.builder-error``), else the fallback."""
    raw = getattr(exc, "code", None) or getattr(exc, "diag_code", None)
    if not raw and exc.__cause__ is not None:
        raw = getattr(exc.__cause__, "diag_code", None)
    return _valid_code(raw, op)


def _exc_refs(exc: BaseException) -> tuple[str, ...]:
    """The op's Diag refs: the exception's ``refs``/``diag_refs``, else a
    wrapped cause's ``diag_refs``. A bare str is one ref; a non-iterable
    (an int, a number) is none."""
    raw = getattr(exc, "refs", None) or getattr(exc, "diag_refs", None)
    if not raw and exc.__cause__ is not None:
        raw = getattr(exc.__cause__, "diag_refs", None)
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Iterable):
        return tuple(str(r) for r in raw)
    return ()


def _exc_next(exc: BaseException) -> str:
    """The op's Diag ``next``: ``PrecisError.next`` is
    ``str | list[str] | None`` — a list joins on ``"; "``, ``None`` is
    empty."""
    raw = getattr(exc, "next", "")
    if isinstance(raw, (list, tuple)):
        return "; ".join(str(n) for n in raw)
    return str(raw or "")


def op(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a scriptless pcb op (``pcb.local.*``) so a raise leaves
    with ``exc.diag`` — an ERROR Diag anchored on ``op_id=name`` at the
    caller's site — plus the ``diag_refs``/``diag_code`` attributes the
    traced manifest path reads. The innermost stamp wins: ``run``
    calling ``place`` records ``place``, the step that failed."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if getattr(exc, "diag", None) is None:
                    try:
                        file, line = script_call_site()
                        _stamp(
                            exc,
                            error_diag(
                                exc,
                                op=name,
                                code=_exc_code(exc, name),
                                refs=_exc_refs(exc),
                                next=_exc_next(exc),
                                file=file,
                                line=line,
                            ),
                        )
                    except Exception:
                        pass  # a stamp failure never replaces the raise
                raise

        return wrapper

    return deco


__all__ = [
    "drc_from_diag",
    "drc_to_diag",
    "error_diag",
    "failed_run_result",
    "op",
    "script_call_site",
]
