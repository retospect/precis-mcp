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
#: allowlist key, not a code constant.
ATTESTING_ORCID_SECRET = "NANOPUB_ATTESTING_ORCID"

MIN_KEY_BITS = 2048
GENERATE_KEY_BITS = 4096


def generate_keypair(bits: int = GENERATE_KEY_BITS) -> tuple[str, str]:
    """A fresh RSA keypair as ``(private_b64der, public_b64der)``.

    Callers store the private half in the vault
    (``precis secret set <name>``); the public half is republished at the
    out-of-band fingerprint page. Never writes anywhere itself."""
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


def load_profile(
    store: Store | None,
    role: str,
    *,
    interactive: bool = False,
) -> Profile:
    """The signing :class:`nanopub.Profile` for ``role`` (``'bot'`` or
    ``'attesting'``), keys from the vault.

    ``interactive=True`` is the attesting-key door: pass it ONLY from a
    surface a person is driving right now. Worker/job/scheduled code
    calling with it is a defect by definition — the parameter exists to
    make that defect a one-line grep."""
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

    private = get_secret(VAULT_SECRET[role], store=store)
    if not private:
        raise RuntimeError(
            f"no {VAULT_SECRET[role]} in the vault — generate one with "
            "`precis nanopub keygen` and store it via `precis secret set`"
        )
    public = public_key_b64(private)

    if role == "bot":
        identity = str(BOT_AGENT)
        name = "precis (non-attesting bot identity)"
    else:
        identity = get_secret(ATTESTING_ORCID_SECRET, store=store) or ""
        if not identity.startswith("https://orcid.org/"):
            raise RuntimeError(
                "the attesting identity must be an ORCID URI in vault "
                f"secret {ATTESTING_ORCID_SECRET} (got {identity!r})"
            )
        name = "attesting reviewer"

    return Profile(
        orcid_id=identity,
        name=name,
        private_key=private,
        public_key=public,
    )
