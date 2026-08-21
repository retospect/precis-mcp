"""Password hashing + credential verification for the precis-web users.

The HTTP gate itself lives in :mod:`precis_web.auth`; the row CRUD in
:mod:`precis.store._users_ops`. This module is the part both the web
process and the ``precis users`` CLI need: turning a plaintext password
into a stored triple, and checking one back.

**KDF.** ``hashlib.scrypt`` — stdlib, memory-hard, no new dependency
(the repo carries no bcrypt/argon2/passlib and this is not worth one).
Parameters :data:`_N` / :data:`_R` / :data:`_P` are the interactive-login
tier from the scrypt paper; on this hardware one hash costs ~60 ms, which
is why :mod:`precis_web.auth` caches verified credentials rather than
re-deriving per request.

**Pepper — why a second secret at all.** :mod:`precis.secrets` promises
that a *logical* ``pg_dump`` is safe to share: vault values are pgcrypto
ciphertext and the passphrase lives in ``postgresql.auto.conf``, which a
logical dump structurally never emits. A plain salted hash in
``web_users`` would quietly break that promise — the dump would carry
crackable credentials. So the plaintext is HMAC-SHA256'd under a
vault-resident pepper (:data:`PEPPER_SECRET`) *before* scrypt, and a
leaked dump is inert without the vault key.

The pepper is optional and recorded per row (``password_algo``), so
peppered and unpeppered users coexist and a deployment can adopt it
incrementally. What it must never do is fail *quietly*: if a row says it
was peppered and the pepper cannot be resolved,
:func:`verify_password` raises :class:`PepperUnavailable` rather than
returning False — a lost pepper is an outage to fix, not a typo to
retry.

**Feed tokens** (the podcast ``?t=`` credential) are the other half of
this module. They are 32 random bytes, so the row stores a bare SHA-256
and that digest is the only thing that authenticates. The *plaintext* is
additionally kept in the vault (:func:`remember_feed_token`) purely so
``/account`` can show you your own subscribe URL — see there for why a
write-only credential is the wrong shape for this one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _stdlib_secrets
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

#: Vault name holding the pepper. Resolved through
#: :func:`precis.secrets.get_secret`, so an env var of the same name
#: overrides (bootstrap / tests) before the DB vault is consulted.
PEPPER_SECRET = "PRECIS_WEB_PASSWORD_PEPPER"

log = logging.getLogger(__name__)

#: Vault prefix for the plaintext podcast token, keyed by login.
FEED_TOKEN_SECRET_PREFIX = "PRECIS_WEB_FEED_TOKEN:"

#: Algo tags written into ``web_users.password_algo``.
ALGO_PLAIN = "scrypt-v1"
ALGO_PEPPERED = "scrypt-pepper-v1"

# scrypt cost parameters. Bumping these needs a new ALGO_* tag (and the
# verify path branching on it), not an edit here — existing rows encode
# their cost implicitly by referencing the tag.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16


class PepperUnavailable(RuntimeError):
    """A row was hashed with a pepper that can't be resolved right now.

    Distinct from "wrong password" on purpose: the caller must surface an
    operator-facing failure (precis-web answers 503), never a login
    prompt, or a vault outage looks like every user forgetting their
    password at once.
    """


@dataclass(frozen=True, slots=True)
class WebUser:
    """One ``web_users`` row, minus the secret material.

    ``abbrev`` is the short display handle a future per-edit attribution
    UI renders and links; nothing reads it in this cut.
    """

    id: int
    login: str
    abbrev: str
    full_name: str | None
    email: str | None
    disabled_at: datetime | None
    last_login_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    #: Whether ``feed_token_sha256`` is set. The digest itself never
    #: leaves the store — only whether there is a live podcast link, so
    #: ``/account`` knows the difference between "no link yet" and "a
    #: link exists but this deployment can't read it back".
    has_feed_token: bool = False

    @property
    def enabled(self) -> bool:
        return self.disabled_at is None


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    """The three stored columns produced by :func:`hash_password`."""

    password_hash: str
    password_salt: str
    password_algo: str


#: Shortest password any surface will accept. Enforced by
#: :func:`validate_password`, which both ``precis users`` and the
#: ``/account`` self-service form call — a policy that holds on only one
#: of the two entry points is theatre.
MIN_PASSWORD_LENGTH = 8


def validate_password(password: str) -> None:
    """Raise :class:`ValueError` if ``password`` fails policy.

    Length only. Composition rules ("one digit, one symbol") were dropped
    from NIST 800-63B because they reliably produce ``Passw0rd!`` — they
    shrink the search space they claim to widen. Length is the term that
    actually matters, and the scrypt cost does the rest.
    """
    if not password:
        raise ValueError("password must not be empty")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters "
            f"(got {len(password)})"
        )


def normalize_login(value: str) -> str:
    """Lowercase + strip. The table CHECKs ``login = lower(login)``, so
    every write and every lookup goes through here rather than trusting
    the caller to have folded case."""
    return value.strip().lower()


def resolve_pepper(*, store: Store | None = None) -> str | None:
    """Return the pepper, or ``None`` when none is configured.

    Best-effort by design — :func:`precis.secrets.get_secret` already
    degrades (no vault schema, unset ``app.secret_key``, DB down) to the
    file/env layers, and a deployment that never set one is a legitimate
    ``scrypt-v1`` deployment.
    """
    from precis import secrets as vault

    value = vault.get_secret(PEPPER_SECRET, store=store)
    return value or None


def ensure_pepper(*, store: Store) -> str:
    """Return the pepper, minting + vaulting a fresh 32-byte one if absent.

    Called by ``precis users add`` so the peppered path is what a new
    deployment gets by default without the operator having to know the
    concept exists.
    """
    from precis import secrets as vault

    existing = resolve_pepper(store=store)
    if existing:
        return existing
    minted = _stdlib_secrets.token_urlsafe(32)
    vault.set_secret(PEPPER_SECRET, minted, store=store)
    return minted


def _derive(password: str, *, salt: bytes, pepper: str | None) -> bytes:
    """scrypt over the (optionally peppered) plaintext.

    HMAC before scrypt, not after: it collapses an arbitrary-length
    password to a fixed 32 bytes under the secret, so the pepper is
    inside the slow function's input rather than a fast outer wrapper an
    attacker with the hash could strip.
    """
    material = password.encode("utf-8")
    if pepper is not None:
        material = hmac.new(pepper.encode("utf-8"), material, hashlib.sha256).digest()
    return hashlib.scrypt(material, salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def hash_password(
    password: str, *, pepper: str | None = None, salt: bytes | None = None
) -> PasswordRecord:
    """Hash ``password`` into the three stored columns.

    ``salt`` is injectable for tests only; production always takes the
    fresh :func:`secrets.token_bytes` default.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt_bytes = salt if salt is not None else _stdlib_secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt=salt_bytes, pepper=pepper)
    return PasswordRecord(
        password_hash=derived.hex(),
        password_salt=salt_bytes.hex(),
        password_algo=ALGO_PEPPERED if pepper is not None else ALGO_PLAIN,
    )


def verify_password(
    password: str, record: PasswordRecord, *, pepper: str | None = None
) -> bool:
    """Constant-time check of ``password`` against a stored record.

    Raises :class:`PepperUnavailable` when the record needs a pepper and
    ``pepper`` is ``None`` — see the module docstring.
    """
    if record.password_algo == ALGO_PEPPERED:
        if pepper is None:
            raise PepperUnavailable(
                f"user hashed with {ALGO_PEPPERED} but {PEPPER_SECRET} "
                "could not be resolved from the vault/env"
            )
        effective: str | None = pepper
    elif record.password_algo == ALGO_PLAIN:
        effective = None
    else:
        raise PepperUnavailable(f"unknown password_algo {record.password_algo!r}")
    try:
        salt = bytes.fromhex(record.password_salt)
        expected = bytes.fromhex(record.password_hash)
    except ValueError:
        return False
    derived = _derive(password, salt=salt, pepper=effective)
    return hmac.compare_digest(derived, expected)


#: Fixed salt for :func:`burn_verify`. Not a secret and never stored — its
#: only job is to make the unknown-user branch cost the same scrypt as the
#: known-user branch.
_DUMMY_SALT = b"\x00" * _SALT_BYTES


def burn_verify() -> None:
    """Spend one scrypt on nothing.

    Called on the unknown-login branch so "no such user" and "wrong
    password" take the same wall-clock time; without it the gate leaks
    which logins exist to anyone with a stopwatch.
    """
    _derive("", salt=_DUMMY_SALT, pepper=None)


def mint_feed_token() -> tuple[str, str]:
    """Return ``(token, sha256_hex)`` for the podcast ``?t=`` credential.

    A bare SHA-256, no KDF: the token is 32 random bytes from
    :mod:`secrets`, so there is no low-entropy guess to slow down. Only
    the digest is stored — minting is the one moment the plaintext
    exists, which is why ``precis users feed-token`` prints the whole
    feed URL and rotating is the way to "recover" it.
    """
    token = _stdlib_secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def feed_token_digest(token: str) -> str:
    """SHA-256 hex of a presented feed token, for the lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def feed_token_secret_name(login: str) -> str:
    """Vault name holding ``login``'s *plaintext* podcast token.

    The colon is deliberate: :func:`precis.secrets.get_secret` lets an
    environment variable of the same name win, and no shell exports a
    name with a colon in it — so a per-user credential can't be shadowed
    by a stray export.
    """
    return f"{FEED_TOKEN_SECRET_PREFIX}{normalize_login(login)}"


def remember_feed_token(login: str, token: str, *, store: Store) -> bool:
    """Keep the plaintext token so ``/account`` can show the URL again.

    ``feed_token_sha256`` stays the *verification* path — nothing checks
    this copy — so a stolen row still yields no working feed URL. This
    exists only because the alternative is worse: with the digest alone,
    the only way to see your own subscription URL is to mint a new one,
    which silently unsubscribes the phone that was already working. A
    credential you cannot read is a credential you rotate by accident.

    The vault, not a column, because that is exactly the line
    :mod:`precis.secrets` draws: vault values are pgcrypto ciphertext
    under a passphrase a logical ``pg_dump`` structurally never emits, so
    the "a dump is safe to share" promise the pepper rests on survives
    unchanged. Returns False when the vault is unavailable — the token
    itself is already stored and working, so this degrades to the old
    show-once behaviour rather than failing the mint.
    """
    from precis import secrets as vault

    try:
        vault.set_secret(feed_token_secret_name(login), token, store=store)
    except Exception:  # pragma: no cover - vault outage / not provisioned
        log.warning("feed token for %s minted but not vaulted", login, exc_info=True)
        return False
    return True


def recall_feed_token(login: str, *, store: Store) -> str | None:
    """The stored plaintext token, or None if there isn't one to show."""
    from precis import secrets as vault

    try:
        return vault.get_secret(feed_token_secret_name(login), store=store) or None
    except Exception:  # pragma: no cover - vault outage
        log.debug("feed token recall failed for %s", login, exc_info=True)
        return None


def forget_feed_token(login: str, *, store: Store) -> bool:
    """Drop the stored plaintext — revoke, rotate-over, and delete-user.

    Deleting a name that was never stored is a no-op in the vault, not an
    error, so anything raising here is a real outage — and the caller has
    just told someone their link is revoked. False means the plaintext is
    still sitting there; say so rather than let it become a credential
    nobody knows about.
    """
    from precis import secrets as vault

    try:
        vault.delete_secret(feed_token_secret_name(login), store=store)
    except Exception:  # pragma: no cover - vault outage
        log.warning(
            "feed token for %s revoked but still in the vault", login, exc_info=True
        )
        return False
    return True


__all__ = [
    "ALGO_PEPPERED",
    "ALGO_PLAIN",
    "FEED_TOKEN_SECRET_PREFIX",
    "MIN_PASSWORD_LENGTH",
    "PEPPER_SECRET",
    "PasswordRecord",
    "PepperUnavailable",
    "WebUser",
    "burn_verify",
    "ensure_pepper",
    "feed_token_digest",
    "feed_token_secret_name",
    "forget_feed_token",
    "hash_password",
    "mint_feed_token",
    "normalize_login",
    "recall_feed_token",
    "remember_feed_token",
    "resolve_pepper",
    "validate_password",
    "verify_password",
]
