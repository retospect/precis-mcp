"""The probe protocol (shared-core-layers S1c): ask what a selector
resolves to, without running anything.

``probe(selector, label=None)`` is the one entry point every domain
dispatches through. The domain-free work — finding a resolver, validating
the digest, rendering the sample, minting the refusal Diag — lives here;
the per-domain knowledge (what a selector even IS, what the entities are)
lives in a :class:`ProbeResolver`:

* :meth:`ProbeResolver.can_resolve` — does this resolver own ``selector``?
* :meth:`ProbeResolver.resolve` — ``selector`` → entities (any domain
  shape; the resolver's own :meth:`~ProbeResolver.digest` is the only
  consumer);
* :meth:`ProbeResolver.digest` — entities → ``{"count", "kind",
  "ids" | "bbox"}``: how many, what they are called collectively, and
  either their identities (a list of strings) or their extent — a 2D
  ``[x0, y0, x1, y1]`` or 3D ``[x0, y0, z0, x1, y1, z1]`` bbox.

The result is the plain mapping ``{"count", "digest", "sample"}`` —
``count`` mirrors ``digest["count"]``; ``sample`` is a short rendered
taste of the ids or the bbox, never the whole list. An unresolvable
selector, a resolver raise or a malformed digest raises
:class:`ProbeError`, which carries the :class:`~precis_diag.Diag` the MCP
error path ships: ``code core.probe-unresolved`` (or
``core.probe-digest``), ``next`` naming the fix.

Resolvers register with :func:`register_probe` (same domain replaces) and
the conformance suite in ``tests/test_diag_conformance.py`` walks the
registered adapters — every one must return count + digest + sample.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from precis_diag import Diag, Status

#: How many ids a digest may carry before the resolver should prefer a
#: bbox — past this the ids list is a log, not an identity.
IDS_SAMPLE_CAP = 64

_SAMPLE_IDS = 3


class ProbeError(ValueError):
    """An unresolvable selector or malformed digest.

    Carries ``diag`` — the structured record — and mirrors the Diag's
    code/refs onto ``diag_code``/``diag_refs`` so a traced ``out.run``
    failure picks them up the way a domain exception's stamped fields do
    (gr522027's protocol).
    """

    def __init__(self, diag: Diag) -> None:
        super().__init__(diag.message)
        self.diag = diag
        self.diag_code = diag.code
        self.diag_refs = diag.refs


@runtime_checkable
class ProbeResolver(Protocol):
    """The per-domain probe adapter. Implementations live in the domain
    (``precis_geom.probe`` for CAD, ``precis.pcb.probe`` for boards); the
    core only ever speaks this protocol."""

    domain: str

    def can_resolve(self, selector: Any) -> bool: ...

    def resolve(self, selector: Any) -> Any: ...

    def digest(self, entities: Any) -> dict[str, Any]: ...


_REGISTRY: list[ProbeResolver] = []


def register_probe(resolver: ProbeResolver) -> None:
    """Register (or, same ``domain``, replace) a probe resolver."""
    domain = str(getattr(resolver, "domain", "") or "")
    if not domain:
        raise ValueError("register_probe: resolver needs a 'domain' name")
    _REGISTRY[:] = [r for r in _REGISTRY if r.domain != domain]
    _REGISTRY.append(resolver)


def probe_resolvers() -> tuple[ProbeResolver, ...]:
    """The registered resolvers, registration order — the conformance
    suite's walk list."""
    return tuple(_REGISTRY)


def _find_resolver(selector: Any) -> ProbeResolver | None:
    for r in _REGISTRY:
        try:
            if r.can_resolve(selector):
                return r
        except Exception:
            continue  # a can_resolve raise reads as "not mine"
    return None


def _sample(digest: dict[str, Any]) -> str:
    """A short human taste of the digest: up to three ids, or the bbox
    (2D or 3D)."""
    ids = digest.get("ids")
    if isinstance(ids, list) and ids:
        head = ", ".join(str(i) for i in ids[:_SAMPLE_IDS])
        if len(ids) > _SAMPLE_IDS:
            head += f" (+{len(ids) - _SAMPLE_IDS} more)"
        return head
    bbox = digest.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) in (4, 6):
        return "[" + ", ".join(f"{float(v):.4g}" for v in bbox) + "]"
    return ""


def _check_digest(digest: Any, *, domain: str, selector: Any) -> dict[str, Any]:
    """The digest contract: a dict with an int ``count``, a non-empty
    str ``kind`` and exactly one of ``ids`` (list of str) / ``bbox``
    (4 or 6 numbers). Extra keys ride through."""
    where = f"{domain} probe of {selector!r}"
    if not isinstance(digest, dict):
        raise ProbeError(
            Diag(
                status=Status.ERROR,
                code="core.probe-digest",
                message=f"{where}: digest is not a mapping",
                next="fix the domain resolver's digest() to return a mapping",
            )
        )
    count = digest.get("count")
    kind = digest.get("kind")
    ids = digest.get("ids")
    bbox = digest.get("bbox")
    ok = (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        and isinstance(kind, str)
        and bool(kind)
        and (ids is None) != (bbox is None)
        and (
            ids is None
            or (isinstance(ids, list) and all(isinstance(i, str) for i in ids))
        )
        and (
            bbox is None
            or (
                isinstance(bbox, (list, tuple))
                and len(bbox) in (4, 6)
                and all(
                    isinstance(v, int | float) and not isinstance(v, bool) for v in bbox
                )
            )
        )
    )
    if not ok:
        raise ProbeError(
            Diag(
                status=Status.ERROR,
                code="core.probe-digest",
                message=(
                    f"{where}: digest must carry an int count, a kind and "
                    "exactly one of ids (list of str) / bbox (4 or 6 numbers)"
                ),
                next="fix the domain resolver's digest() output",
            )
        )
    return digest


def probe(
    selector: Any, label: str | None = None, *, resolver: ProbeResolver | None = None
) -> dict[str, Any]:
    """Resolve ``selector`` to ``{"count", "digest", "sample"}``.

    ``resolver`` overrides dispatch (board-bound resolvers, tests); else
    the first registered resolver whose ``can_resolve`` accepts the
    selector wins. No resolver, a raise from ``resolve`` or a malformed
    digest raises :class:`ProbeError` carrying the Diag.
    """
    del label  # the label is the caller's rendering concern, not the core's
    r = resolver if resolver is not None else _find_resolver(selector)
    if r is None:
        domains = sorted(x.domain for x in _REGISTRY)
        raise ProbeError(
            Diag(
                status=Status.ERROR,
                code="core.probe-unresolved",
                message=(
                    f"probe: no resolver accepts {selector!r} "
                    f"(registered domains: {domains or '(none)'})"
                ),
                next="pass a selector the domain understands, or import the "
                "domain's probe adapter to register its resolver",
            )
        )
    domain = str(getattr(r, "domain", "") or "domain")
    try:
        entities = r.resolve(selector)
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError(
            Diag(
                status=Status.ERROR,
                code="core.probe-unresolved",
                message=f"{domain} probe of {selector!r}: {exc}",
                next="fix the selector and probe again — nothing ran",
            )
        ) from exc
    digest = _check_digest(r.digest(entities), domain=domain, selector=selector)
    return {"count": digest["count"], "digest": digest, "sample": _sample(digest)}


__all__ = [
    "IDS_SAMPLE_CAP",
    "ProbeError",
    "ProbeResolver",
    "probe",
    "probe_resolvers",
    "register_probe",
]
