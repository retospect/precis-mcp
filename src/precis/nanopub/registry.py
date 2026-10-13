"""Registry POST — slice 5, **the one true point of no return**.

Everything upstream (mint, sign, anchor) is local and reversible; a
successful POST here propagates the artifact across registry mirrors
forever. Accordingly this module is triple-gated:

* ``interactive=True`` required — publication is a human act (a person
  runs ``precis nanopub publish``); a worker or scheduled pass calling
  it is a defect by definition.
* ``live=True`` required for any network traffic; without it the call
  is a dry run that returns exactly what *would* be POSTed.
* :func:`precis.nanopub.preflight.publish_preflight` must return zero
  blocking issues — withheld edges, trust allowlist (attesting key
  only), state machine, drift, dependency order.

What is POSTed is the **exact stored artifact bytes** (the proof-store
authority), never a re-serialization — the trusty URI covers those
bytes.

**No transaction spans the POST.** A live publish is: fresh preflight →
short transaction 1 (CAS ``anchored`` → ``publishing``, committed, connection
released) → POST with no connection held → short transaction 2
(``publishing`` → ``published``). The durable ``publishing`` row is the lock:
supersede, reopen, restage and a second publish all refuse it, so a public
predecessor cannot be classified as unpublished during an in-flight POST. A
definite failure (the registry answered 4xx) reverts to
``anchored``; an ambiguous one (timeout, dropped connection, crash) leaves the
row ``publishing`` — the trusty URI is known before the POST, so
:func:`reconcile_publishing` resolves it by asking the registry for that URI.
Target URL is a registry constant (mirrors replicate), not agent-supplied, so
this is not a `safe_fetch` surface.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.nanopub.preflight import PreflightIssue, publish_preflight

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: npx:hasPublicKey / npx:hasSignature lines of the signed trig (public values).
_SIG_VALUE = re.compile(r"(?:npx:|/nanopub/x/)has(?:PublicKey|Signature)>?\s+\"")

#: First entry of the `nanopub` reference library's registry list
#: (`nanopub.definitions.NANOPUB_REGISTRY_URLS`) — kept as our own
#: constant so a library upgrade can't silently retarget the point of no
#: return. POST of `application/trig` bytes; mirrors replicate from there.
DEFAULT_REGISTRY_URL = "https://registry.petapico.org/np/"


class PublishBlocked(BadInput):
    """Publication refused: blocking preflight issues. ``issues`` carries
    the machine-routable list (the review surface renders it)."""

    def __init__(self, issues: list[PreflightIssue]) -> None:
        self.issues = issues
        lines = "; ".join(f"[{i.check}] {i.message}" for i in issues)
        super().__init__(f"publish preflight failed: {lines}")


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What happened (``live=True``) or would happen (dry run)."""

    hub_ref_id: int
    trusty_uri: str
    registry_url: str
    byte_count: int
    live: bool
    #: Non-blocking preflight notes that rode along (e.g. ots-pending).
    notes: list[PreflightIssue]


def _default_post(url: str, trig_bytes: bytes) -> None:
    import httpx

    resp = httpx.post(
        url,
        content=trig_bytes,
        headers={"Content-Type": "application/trig"},
        timeout=30.0,
    )
    resp.raise_for_status()


def _is_definite_failure(exc: BaseException) -> bool:
    """True when the registry demonstrably answered with an error, i.e. the
    artifact was not accepted: an HTTP response with a 4xx status. Every 5xx
    and anything without a response (timeout, reset, DNS) is ambiguous — the
    registry may have stored the bytes, and a wrong revert would leave an
    anchored-unpublished row that supersede accepts. A wrong "ambiguous" only
    costs a reconcile (republishing the same trusty URI is idempotent).
    408/425/429 are 4xx the server may send after reading the body, so they
    stay ambiguous."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return (
        isinstance(status, int) and 400 <= status < 500 and status not in _AMBIGUOUS_4XX
    )


_AMBIGUOUS_4XX = frozenset({408, 425, 429})


def publish(
    store: Store,
    hub_ref_id: int,
    *,
    live: bool = False,
    interactive: bool = False,
    registry_url: str = DEFAULT_REGISTRY_URL,
    post: Callable[[str, bytes], None] | None = None,
) -> PublishResult:
    """Publish one hub's anchored artifact to the public registry —
    ``anchored`` → ``publishing`` → ``published``. Dry run unless ``live=True``."""
    if not interactive:
        raise PermissionError(
            "publish() is the point of no return — invocable only from an "
            "interactive surface a person is driving (pass interactive=True); "
            "no worker, job, or scheduled pass may publish"
        )
    # Each store call below opens, commits and releases its own pooled
    # connection; none is held across another call or across the POST.
    row = store.nanopub_publish_row(hub_ref_id)
    issues = publish_preflight(store, hub_ref_id, row=row)
    blocking = [i for i in issues if i.blocking]
    if blocking:
        raise PublishBlocked(blocking)
    notes = [i for i in issues if not i.blocking]

    assert row is not None and row.artifact_id is not None  # preflight guaranteed
    artifact = store.nanopub_artifact(row.artifact_id)
    assert artifact is not None

    result = PublishResult(
        hub_ref_id=hub_ref_id,
        trusty_uri=artifact.trusty_uri,
        registry_url=registry_url,
        byte_count=len(artifact.trig_bytes),
        live=live,
        notes=notes,
    )
    if not live:
        return result

    from precis.utils.secret_scan import refuse_secrets

    # Last look before the point of no return (mint already gated the inputs).
    # The signature graph's public key and signature values are public,
    # high-entropy by design: blank them (keeping line numbers) before the scan.
    trig = artifact.trig_bytes.decode("utf-8", errors="replace")
    refuse_secrets(
        [
            (
                "published trig",
                "\n".join(
                    "" if _SIG_VALUE.search(ln) else ln for ln in trig.splitlines()
                ),
            )
        ]
    )

    # Transaction 1: the durable claim. Losing the CAS means a concurrent
    # publish/supersede/reopen moved the row since the preflight read.
    if not store.nanopub_claim_publishing(
        row.id,
        artifact_id=row.artifact_id,
        trusty_uri=artifact.trusty_uri,
        registry_url=registry_url,
    ):
        cur = store.nanopub_publish_row_by_id(row.id)
        raise BadInput(
            f"fi{hub_ref_id}: publish row {row.id} is "
            f"{cur.state if cur else 'gone'!r}, not 'anchored' — another publish "
            "or state change won; nothing was POSTed"
        )

    # Post-claim re-check: anything that changed between the first preflight
    # and the claim (signoff, dependency, drift) is caught here, before the
    # point of no return. The row now reads 'publishing', which preflight
    # would block on, so judge it as the anchored row it was.
    claimed = store.nanopub_publish_row_by_id(row.id)
    assert claimed is not None
    recheck = [
        i
        for i in publish_preflight(
            store, hub_ref_id, row=dataclasses.replace(claimed, state="anchored")
        )
        if i.blocking
    ]
    if recheck:
        store.nanopub_release_publishing(row.id)
        raise PublishBlocked(recheck)

    try:
        (post or _default_post)(registry_url, artifact.trig_bytes)
    except Exception as exc:
        if _is_definite_failure(exc):
            reverted = store.nanopub_release_publishing(row.id)
            log.warning(
                "nanopub: fi%s POST to %s refused (%s); publish row %s %s",
                hub_ref_id,
                registry_url,
                exc,
                row.id,
                "reverted to 'anchored'" if reverted else "was not 'publishing'",
            )
            raise
        log.error(
            "nanopub: fi%s POST to %s has an UNKNOWN outcome (%s); publish row "
            "%s stays 'publishing' — run `precis nanopub reconcile`",
            hub_ref_id,
            registry_url,
            exc,
            row.id,
        )
        raise BadInput(
            f"registry POST for fi{hub_ref_id} has an unknown outcome ({exc}); "
            "the row stays 'publishing' — run `precis nanopub reconcile` to "
            "resolve it against the registry"
        ) from exc

    # Transaction 2.
    if not store.nanopub_record_published(row.id, registry_url=registry_url):
        # The POST succeeded but the row left 'publishing': the registry
        # now holds the artifact while our state machine disagrees.
        # Nothing here can undo a POST — reconcile by hand, loudly.
        log.critical(
            "nanopub: fi%s POSTed to %s but publish row %s left 'publishing' "
            "mid-publish — reconcile state by hand",
            hub_ref_id,
            registry_url,
            row.id,
        )
        raise BadInput(
            f"registry POST for fi{hub_ref_id} succeeded but the publish row "
            "moved mid-publish — state needs manual reconciliation"
        )
    log.info(
        "nanopub: PUBLISHED fi%s as %s to %s (%d bytes)",
        hub_ref_id,
        artifact.trusty_uri,
        registry_url,
        len(artifact.trig_bytes),
    )
    return result


@dataclass(frozen=True, slots=True)
class Reconciled:
    """Outcome of one stuck ``publishing`` row. ``outcome`` is ``published``
    (registry holds the URI), ``reverted`` (registry answered 404; back to
    ``anchored``) or ``unknown`` (registry unreachable/odd; row untouched)."""

    row_id: int
    hub_ref_id: int
    trusty_uri: str
    outcome: str
    detail: str = ""


def _default_lookup(url: str) -> bytes:
    from precis.nanopub.mirror import _default_fetch

    return _default_fetch(url)


def reconcile_publishing(
    store: Store,
    *,
    older_than: timedelta = timedelta(minutes=30),
    fetch: Callable[[str], bytes] | None = None,
) -> list[Reconciled]:
    """Resolve ``publishing`` rows claimed at least ``older_than`` ago by
    asking the registry whether it holds the (pre-known) trusty URI.

    Present → ``published``; a clean 404 → back to ``anchored``; any other
    failure → left ``publishing`` (``unknown``). ``fetch(url) -> bytes`` is
    injectable; it must raise on a non-2xx (an exception carrying
    ``response.status_code == 404`` means "absent"). The default
    ``older_than`` leaves room for registry propagation after an ambiguous POST.
    """
    out: list[Reconciled] = []
    for row in store.nanopub_publishing_rows(older_than=older_than):
        art = store.nanopub_artifact(row.artifact_id) if row.artifact_id else None
        if art is None or not row.registry_url:
            out.append(
                Reconciled(
                    row.id,
                    row.claim_ref_id,
                    row.trusty_uri or "",
                    "unknown",
                    "row lacks its artifact or registry url — resolve by hand",
                )
            )
            continue
        code = art.trusty_uri.rsplit("/", 1)[-1]
        url = f"{row.registry_url.rstrip('/')}/{code}.trig"
        try:
            body = (fetch or _default_lookup)(url)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                moved = store.nanopub_release_publishing(row.id)
                out.append(
                    Reconciled(
                        row.id,
                        row.claim_ref_id,
                        art.trusty_uri,
                        "reverted" if moved else "unknown",
                        "registry has no such URI; back to anchored"
                        if moved
                        else "row moved concurrently",
                    )
                )
            else:
                out.append(
                    Reconciled(
                        row.id, row.claim_ref_id, art.trusty_uri, "unknown", str(exc)
                    )
                )
            continue
        if not body or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            out.append(
                Reconciled(
                    row.id,
                    row.claim_ref_id,
                    art.trusty_uri,
                    "unknown",
                    "registry returned an empty or HTML body",
                )
            )
            continue
        moved = store.nanopub_record_published(row.id, registry_url=row.registry_url)
        out.append(
            Reconciled(
                row.id,
                row.claim_ref_id,
                art.trusty_uri,
                "published" if moved else "unknown",
                "registry holds the URI" if moved else "row moved concurrently",
            )
        )
    return out
