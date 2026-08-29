"""Introduction nanopub — the public key→ORCID declaration.

The registry half of registering the attesting key with its ORCID: a
nanopub, signed with the attesting key itself, whose assertion declares
``keyDeclaration npx:declaredBy <orcid>`` and embeds the public key
(:class:`nanopub.NanopubIntroduction`). Anyone verifying one of our
signed claims can dereference the signer and find this declaration.

The introduction alone proves nothing — anyone can publish a nanopub
claiming any ORCID. The binding becomes authoritative only after the
**out-of-band half**: the person adds the introduction's trusty URI to
their ORCID record (Websites & social links), a place only the iD
holder, authenticated to orcid.org, can edit. This module prints that
instruction; it cannot do the step.

Same custody rules as every attesting-key surface (spec: Key custody):
``interactive=True`` required (a person runs ``precis nanopub intro``),
``live=True`` required for the registry POST, and a live POST is the
point of no return — introductions can be superseded, never deleted.

On a live publish the trusty URI is recorded in the vault
(:data:`INTRO_URI_SECRET`) so later surfaces (provenance renderers)
can find the current introduction without re-deriving it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.nanopub.keys import ATTESTING_ORCID_SECRET, fingerprint, load_profile
from precis.nanopub.registry import DEFAULT_REGISTRY_URL, _default_post

if TYPE_CHECKING:
    from precis.store import Store

#: Where the live introduction's trusty URI is recorded (vault, 0059
#: pattern — the vault doubles as cluster-wide config resolution).
INTRO_URI_SECRET = "NANOPUB_ATTESTING_INTRO_URI"


@dataclass(frozen=True, slots=True)
class IntroResult:
    """What happened (``live=True``) or would happen (dry run)."""

    trusty_uri: str
    orcid: str
    key_fingerprint: str
    byte_count: int
    registry_url: str
    live: bool
    trig: str


def introduce(
    store: Store | None,
    *,
    name: str,
    key_location: str | None = None,
    live: bool = False,
    interactive: bool = False,
    registry_url: str = DEFAULT_REGISTRY_URL,
    post: Callable[[str, bytes], None] | None = None,
) -> IntroResult:
    """Build, sign, and (``live=True``) publish the attesting key's
    introduction nanopub. Dry run signs locally and returns the exact
    bytes that would be POSTed — signing is local and reversible; only
    the POST is not.

    ``name`` is the person's public name (``foaf:name`` in the
    assertion) — an introduction names a human, so there is no default.
    """
    if not interactive:
        raise PermissionError(
            "introduce() invokes the attesting key — invocable only from an "
            "interactive surface a person is driving (pass interactive=True); "
            "no worker, job, or scheduled pass may introduce a key"
        )
    if not name.strip():
        raise ValueError("an introduction names a person publicly — pass their name")

    from nanopub import NanopubConf, NanopubIntroduction

    from precis.secrets import get_secret

    # Round-trip the vault identity through the signer check so ``name``
    # lands on the profile the same way the web sign button's does.
    stored = get_secret(ATTESTING_ORCID_SECRET, store=store) or ""
    profile = load_profile(
        store,
        "attesting",
        interactive=True,
        signer_orcid=stored,
        signer_name=name.strip(),
    )

    np = NanopubIntroduction(conf=NanopubConf(profile=profile), host=key_location)
    np.sign()
    if not (np.has_valid_trusty and np.has_valid_signature):
        raise RuntimeError("nanopub library produced an invalid introduction")

    trig = np.rdf.serialize(format="trig")
    trig_bytes = trig.encode("utf-8")
    result = IntroResult(
        trusty_uri=str(np.source_uri),
        orcid=profile.orcid_id,
        key_fingerprint=fingerprint(profile.public_key),
        byte_count=len(trig_bytes),
        registry_url=registry_url,
        live=live,
        trig=trig,
    )
    if not live:
        return result

    (post or _default_post)(registry_url, trig_bytes)
    if store is not None:
        from precis import secrets as vault

        vault.set_secret(INTRO_URI_SECRET, result.trusty_uri, store=store)
    return result
