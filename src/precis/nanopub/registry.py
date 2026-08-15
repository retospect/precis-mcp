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
bytes. Target URL is a registry constant (mirrors replicate), not
agent-supplied, so this is not a `safe_fetch` surface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.nanopub.preflight import PreflightIssue, publish_preflight

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

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
    ``anchored`` → ``published``. Dry run unless ``live=True``."""
    if not interactive:
        raise PermissionError(
            "publish() is the point of no return — invocable only from an "
            "interactive surface a person is driving (pass interactive=True); "
            "no worker, job, or scheduled pass may publish"
        )
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

    (post or _default_post)(registry_url, artifact.trig_bytes)
    if not store.nanopub_record_published(row.id, registry_url=registry_url):
        # The POST succeeded but the row moved mid-publish: the registry
        # now holds the artifact while our state machine disagrees.
        # Nothing here can undo a POST — reconcile by hand, loudly.
        log.critical(
            "nanopub: fi%s POSTed to %s but publish row %s left 'anchored' "
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
