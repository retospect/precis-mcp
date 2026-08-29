"""Key custody — vault-resident signing keys, invocation-guarded.

Both keypairs live in the DB secrets vault (0059 pattern,
:func:`precis.secrets.get_secret`) as base64-DER private keys; the
public half is derived on load, never stored separately (one fewer
value to drift). The boundary that matters is **invocation**, not
storage (spec: Key custody):

* **bot** — worker-invocable, allowlisted non-attesting. A bot
  signature alone authorizes nothing; it exists so machine-derived
  provenance artifacts carry provenance too.
* **attesting** — the human key. :func:`load_profile` refuses it unless
  the caller passes ``interactive=True``, which only the interactive
  sign surfaces (the sign CLI a person runs; later the web sign button)
  may do — no worker, job, or scheduled pass. This parameter is the
  grep-able enforcement point for "signed means a human checked";
  the accepted residual risk (anything holding ``agent_rw`` can read
  the vault) is recorded in the spec.

Floor: our keys sign at 2048 minimum, 4096 preferred (generation
default). Loading a weaker or malformed key raises — parse leniently,
validate strictly applies to *inbound* keys someday; our own get no
leniency at all.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

from precis.nanopub.vocab import BOT_AGENT

if TYPE_CHECKING:
    from nanopub import Profile

    from precis.store import Store

#: Vault secret names (base64-DER PKCS#8 private keys).
VAULT_SECRET = {
    "bot": "NANOPUB_BOT_PRIVATE_KEY",
    "attesting": "NANOPUB_ATTESTING_PRIVATE_KEY",
}
#: The attesting identity's ORCID URI lives in the vault too — it is the
#: allowlist key, not a code constant. It names the human the *key* is
#: registered to; :func:`load_profile` takes the signer's own iD (from
#: their ``web_users`` row, via ``/account``) and refuses to sign under
#: this key for anybody else.
ATTESTING_ORCID_SECRET = "NANOPUB_ATTESTING_ORCID"

#: URI form of an ORCID iD — what a nanopub carries as its signer.
ORCID_URI_PREFIX = "https://orcid.org/"

MIN_KEY_BITS = 2048
GENERATE_KEY_BITS = 4096


def generate_keypair(bits: int = GENERATE_KEY_BITS) -> tuple[str, str]:
    """A fresh RSA keypair as ``(private_b64der, public_b64der)``.

    Callers store the private half in the vault
    (``precis secret set <name>``); the public half goes public via the
    introduction nanopub (``precis nanopub intro`` — no self-hosted
    fingerprint page, by decision). Never writes anywhere itself."""
    from Crypto.PublicKey import RSA

    if bits < MIN_KEY_BITS:
        raise ValueError(f"key floor is {MIN_KEY_BITS} bits (got {bits})")
    key = RSA.generate(bits)
    private = base64.b64encode(key.export_key(format="DER", pkcs=8)).decode()
    public = base64.b64encode(key.publickey().export_key(format="DER")).decode()
    return private, public


def public_key_b64(private_b64der: str) -> str:
    """Derive the base64-DER public key from a private key, validating
    the size floor on the way."""
    from Crypto.PublicKey import RSA

    key = RSA.import_key(base64.b64decode(private_b64der))
    if key.size_in_bits() < MIN_KEY_BITS:
        raise ValueError(
            f"refusing a {key.size_in_bits()}-bit key — the floor is "
            f"{MIN_KEY_BITS} (4096 preferred); rotate it"
        )
    return base64.b64encode(key.publickey().export_key(format="DER")).decode()


def fingerprint(public_b64der: str) -> str:
    """sha256 hex over the public key's DER bytes — the allowlist /
    out-of-band-page fingerprint form."""
    return hashlib.sha256(base64.b64decode(public_b64der)).hexdigest()


def orcid_uri(value: str) -> str:
    """An ORCID iD in any accepted form → the ``https://orcid.org/…``
    URI a nanopub is signed under. Raises on a malformed or
    checksum-failing iD (:func:`precis.users.normalize_orcid`)."""
    from precis.users import normalize_orcid

    canonical = normalize_orcid(value)
    if not canonical:
        raise ValueError("no ORCID iD given")
    return f"{ORCID_URI_PREFIX}{canonical}"


def load_profile(
    store: Store | None,
    role: str,
    *,
    interactive: bool = False,
    signer_orcid: str | None = None,
    signer_name: str | None = None,
) -> Profile:
    """The signing :class:`nanopub.Profile` for ``role`` (``'bot'`` or
    ``'attesting'``), keys from the vault.

    ``interactive=True`` is the attesting-key door: pass it ONLY from a
    surface a person is driving right now. Worker/job/scheduled code
    calling with it is a defect by definition — the parameter exists to
    make that defect a one-line grep.

    ``signer_orcid`` is **who is attesting** — the iD off the signed-in
    person's account (``/account``, ``web_users.orcid``). A nanopub is
    attributed to an ORCID and never to a login, so the identity has to
    come from the human at the surface rather than from a
    deployment-wide constant; the web sign button passes theirs. Omitted
    (the CLI, where there is no web session), the vault value stands in.

    **It must match the vault's** :data:`ATTESTING_ORCID_SECRET`. That
    secret names the human this key is *registered to* — the nanopub
    registry allowlists the pair — so signing under someone else's iD
    with it would publish a claim attributed to a person who never held
    the key. A mismatch is refused, which is also what makes the account
    field an authorization check and not just a label: only the human the
    key belongs to can drive the attesting button.
    """
    from nanopub import Profile

    from precis.secrets import get_secret

    if role not in VAULT_SECRET:
        raise ValueError(f"unknown key role {role!r}")
    if role == "attesting" and not interactive:
        raise PermissionError(
            "the attesting key is invocable only from an interactive sign "
            "surface (pass interactive=True from code a person is driving); "
            "workers and jobs sign with the non-attesting bot key"
        )

    # Identity first, key second: everything that can refuse this call
    # runs before the private key is read out of the vault. Nothing
    # downstream depends on the order — it is so a rejected sign never
    # touches the key material at all, which keeps any future
    # vault-access audit honest about what was actually opened.
    if role == "bot":
        identity = str(BOT_AGENT)
        name = "precis (non-attesting bot identity)"
    else:
        stored = get_secret(ATTESTING_ORCID_SECRET, store=store) or ""
        if not stored.startswith(ORCID_URI_PREFIX):
            raise RuntimeError(
                "the attesting identity must be an ORCID URI in vault "
                f"secret {ATTESTING_ORCID_SECRET} (got {stored!r})"
            )
        try:
            # Canonicalize BOTH sides before comparing. The vault value is
            # typed in by an operator; a lowercase checksum 'x' or a
            # missing dash there would otherwise fail every legitimate
            # signer closed, and the person staring at the refusal has no
            # way to see that the two iDs are the same one.
            identity = orcid_uri(stored)
        except ValueError as exc:
            raise RuntimeError(
                f"vault secret {ATTESTING_ORCID_SECRET} is not a usable "
                f"ORCID iD ({exc})"
            ) from exc
        name = "attesting reviewer"
        if signer_orcid:
            try:
                claimed = orcid_uri(signer_orcid)
            except ValueError as exc:
                raise PermissionError(f"signer ORCID iD: {exc}") from exc
            if claimed != identity:
                raise PermissionError(
                    f"this attesting key is registered to {identity} — it "
                    f"cannot sign as {claimed}. Set the right iD on "
                    "/account, or have the person who holds that identity "
                    "sign."
                )
            name = signer_name or name

    private = get_secret(VAULT_SECRET[role], store=store)
    if not private:
        raise RuntimeError(
            f"no {VAULT_SECRET[role]} in the vault — generate one with "
            "`precis nanopub keygen` and store it via `precis secret set`"
        )
    public = public_key_b64(private)

    return Profile(
        orcid_id=identity,
        name=name,
        private_key=private,
        public_key=public,
    )
