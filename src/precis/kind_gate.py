"""Kind enablement gate: prohibition + resource present.

The boot composition root (:func:`precis.dispatch.boot`) consults
:func:`gate` before each ``_try(cls, ...)`` call to decide whether a
handler should be constructed at all. Outcomes feed
:attr:`precis.dispatch.Hub.loadabilities` so
:func:`precis.server._build_instructions` can render an honest
``Kinds unavailable:`` summary with short reasons.

Predicate:

    loaded(kind) = NOT prohibited(kind) AND resources_present(kind)

- ``prohibited(kind)`` — ``kind`` appears in ``PRECIS_KINDS_DISABLED``
  (parsed via :func:`parse_disabled`).
- ``resources_present(kind)`` — every env var in
  :attr:`precis.protocol.KindSpec.requires_env` is set non-empty.
  Store / embedder / file root checks happen inside the handler's
  ``__init__`` and surface as :class:`precis.dispatch.InitError`; we
  translate the caught exception into a :class:`Loadability` via
  :func:`loadability_from_exception`.

The gate runs **before** construction, so a prohibited or
resource-missing kind never imports its handler module, never opens
sockets, and never raises a confusing late-binding error. The
``Kinds unavailable:`` banner line tells the operator (and the
connected agent) precisely why each absent kind is absent.

See ``docs/conventions/kind-enablement.md`` for the handler-author
contract and ``docs/design/mcp-cold-start-token-budget.md`` Phase 4
for the design context.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from precis.protocol import KindSpec


@dataclass(frozen=True, slots=True)
class Loadability:
    """Verdict for a kind at boot time.

    ``loaded=True`` means the handler was constructed and registered;
    ``loaded=False`` means it was skipped, and :attr:`reason` carries
    a short tag suitable for the cold-start banner
    (``prohibited``, ``missing <ENV>``, ``store required``,
    ``optional dep not installed``, ...).
    """

    kind: str
    loaded: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.loaded and self.reason is not None:
            raise ValueError(
                f"loaded=True must not carry a reason; got reason={self.reason!r}"
            )
        if not self.loaded and not self.reason:
            raise ValueError("loaded=False must carry a non-empty reason")


def parse_disabled(value: str | None) -> frozenset[str]:
    """Parse ``PRECIS_KINDS_DISABLED`` into a deduped frozen set.

    Whitespace around commas is tolerated. Empty entries (``a,,b``)
    are dropped. Unknown kind names are accepted — they're a no-op
    against the live registry; treating typos as a hard error would
    create a deployment-time footgun every time a kind is renamed
    or removed.
    """
    if not value:
        return frozenset()
    kinds: set[str] = set()
    for raw in value.split(","):
        kind = raw.strip()
        if kind:
            kinds.add(kind)
    return frozenset(kinds)


def gate(spec: KindSpec, *, disabled: frozenset[str]) -> Loadability:
    """Compute the pre-construction loadability verdict.

    Two checks, in order:

    1. ``spec.kind`` in ``disabled`` → ``Loadability(loaded=False,
       reason='prohibited')``. Honours operator intent over resource
       availability — a prohibited kind that *could* load is still
       skipped.
    2. Every env var in ``spec.requires_env`` is set non-empty →
       proceed. Missing envs → ``Loadability(loaded=False,
       reason='missing <ENV1>, <ENV2>')``.

    Returns ``Loadability(loaded=True)`` when the handler should be
    constructed. Further checks (store presence, file-root validity,
    optional-dep imports) happen inside the handler's ``__init__``
    and surface as :class:`precis.dispatch.InitError` /
    :class:`ImportError` / :class:`ValueError`; the caller
    translates those via :func:`loadability_from_exception`.
    """
    if spec.kind in disabled:
        return Loadability(kind=spec.kind, loaded=False, reason="prohibited")
    missing = [env for env in spec.requires_env if not os.environ.get(env)]
    if missing:
        return Loadability(
            kind=spec.kind,
            loaded=False,
            reason="missing " + ", ".join(missing),
        )
    return Loadability(kind=spec.kind, loaded=True)


def loadability_from_exception(spec: KindSpec, exc: BaseException) -> Loadability:
    """Translate a caught construction-time exception into a verdict.

    Strips the leading ``"<kind>: "`` prefix the existing
    :class:`InitError` convention uses (``"paper: store required"``
    → ``"store required"``) so the banner line reads as
    ``Kinds unavailable: paper (store required)`` rather than
    ``paper (paper: store required)``.

    Long reasons are truncated to keep the banner line readable; the
    full exception is already logged by :func:`precis.dispatch._try`
    via ``log.warning``, so the operator can grep stderr for the
    untruncated stack trace.
    """
    msg = str(exc).strip()
    prefix = f"{spec.kind}:"
    if msg.startswith(prefix):
        msg = msg[len(prefix) :].strip()
    if not msg:
        msg = type(exc).__name__
    if len(msg) > 60:
        msg = msg[:57] + "..."
    return Loadability(kind=spec.kind, loaded=False, reason=msg)


def format_unavailable(verdicts: dict[str, Loadability]) -> str:
    """Render the ``Kinds unavailable:`` banner line.

    Returns the empty string when every recorded verdict is
    ``loaded=True`` (i.e. nothing went wrong). When at least one
    kind was skipped, renders a sorted ``kind (reason)`` list:

    ::

        Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited).
    """
    absent = sorted(
        (v for v in verdicts.values() if not v.loaded),
        key=lambda v: v.kind,
    )
    if not absent:
        return ""
    entries = [f"{v.kind} ({v.reason})" for v in absent]
    return "Kinds unavailable: " + ", ".join(entries) + "."
