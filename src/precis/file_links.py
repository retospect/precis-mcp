"""Signed, short-lived file links — HMAC tokens the MCP mints and
precis_web verifies (gr517423, ``paper-backup-pdf-transfer.md`` slice 3).

PDF bytes never ride an MCP result: ``get(kind='paper', view='pdf_link')``
mints a token and hands back a URL; ``GET /files/pdf/<sha>?t=<token>`` on
precis_web verifies it and streams the file itself. Two different
processes sign and verify, so the key must be persistent and shared —
a process-local key like :class:`precis_web.auth.SessionTokens`'s cannot
work. It lives in the vault as ``PRECIS_FILE_LINK_KEY``, minted on first
use by :func:`ensure_file_link_key` (the same shape
:func:`precis.users.ensure_pepper` established). The token itself is
``v1.<op>.<sha>.<ref_id>.<exp>.<hexsig>`` — every field before the
signature is authenticated by it, so the bearer can re-fetch until
``exp`` but cannot widen the grant (other sha, other ref, other op)
without the key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _stdlib_secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store.store import Store

#: Vault secret the signing key lives under — the MCP writes it once on
#: first use, precis_web reveals it on every verify. Never logged, never
#: returned inside a token or a view body.
FILE_LINK_SECRET = "PRECIS_FILE_LINK_KEY"

#: Operator-set public origin the minted URLs are joined onto (env →
#: file → vault via ``get_secret`` — Q3 of the spec: the vault, not
#: container env). Unset → the view returns the path alone and says so;
#: no origin literal can live in this repo (it is public).
WEB_BASE_URL_SECRET = "PRECIS_WEB_BASE_URL"

#: Token ops. ``get`` is the slice-3 download; ``put`` is reserved for
#: the slice-6 upload door. The op is inside the signature so a download
#: token can never be replayed as an upload.
OPS = ("get", "put")

#: Reto 2026-10-25: links last 600 s by default, 3600 s max, and a
#: download link is reusable until expiry.
DEFAULT_TTL_S = 600
MAX_TTL_S = 3600

_VERSION = "v1"


class LinkTokenError(ValueError):
    """Base for every token refusal."""


class TokenInvalid(LinkTokenError):
    """Malformed, wrong op/sha, or bad signature — a 403."""


class TokenExpired(LinkTokenError):
    """Well-signed but past ``exp`` — a 410."""


@dataclass(frozen=True)
class Claims:
    """What a parsed token asserts. Produced by :func:`parse` (fields
    only — unverified) and :func:`verify` (signature + expiry checked)."""

    op: str
    sha: str
    ref_id: int
    exp: int


def _resolve_key(*, store: Store | None = None) -> str | None:
    """Read-only key lookup — env → vault → file. Never mints: a
    *verifier* must not create the key it checks against."""
    from precis import secrets as vault

    return vault.get_secret(FILE_LINK_SECRET, store=store) or None


def ensure_file_link_key(*, store: Store | None = None) -> str:
    """Return the signing key, minting + vaulting a fresh 32-byte one if absent.

    A copy of ``users.py::ensure_pepper``: read through
    :func:`precis.secrets.get_secret` (env → vault → file), mint with
    ``secrets.token_urlsafe(32)``, persist via
    :func:`precis.secrets.set_secret_if_absent`. The vault is the shared
    store because signer (MCP) and verifier (precis_web) are different
    processes. The mint is atomic — two racing first-mints converge on
    the stored key, and the returned value is that key (ours when we
    won, the winner's when we didn't) — so nobody signs with a key that
    loses the race and dies under the winner's overwrite (gr517874).
    """
    existing = _resolve_key(store=store)
    if existing:
        return existing
    if store is None:
        raise RuntimeError(
            f"no store to vault {FILE_LINK_SECRET} into — set it as an env "
            "var or secrets-file entry for this process"
        )
    from precis import secrets as vault

    minted = _stdlib_secrets.token_urlsafe(32)
    stored = vault.set_secret_if_absent(FILE_LINK_SECRET, minted, store=store)
    if stored is None:
        raise RuntimeError(
            f"{FILE_LINK_SECRET}: vault unreachable or raced a delete "
            "during first mint; retry"
        )
    return stored


def _sig(key: str, fields: str) -> str:
    return hmac.new(key.encode(), fields.encode(), hashlib.sha256).hexdigest()


def mint(
    op: str,
    sha: str,
    ref_id: int,
    ttl_s: int = DEFAULT_TTL_S,
    *,
    store: Store | None = None,
    now: float | None = None,
) -> str:
    """Mint ``v1.<op>.<sha>.<ref_id>.<exp>.<hexsig>`` — the URL bearer token.

    ``ttl_s`` is clamped to ``[1, MAX_TTL_S]`` (a caller asking for a
    day-long link still gets the 3600 s ceiling). ``now`` exists for
    tests; production callers leave it to the clock.
    """
    if op not in OPS:
        raise ValueError(f"op must be one of {OPS}, got {op!r}")
    ttl = max(1, min(int(ttl_s), MAX_TTL_S))
    exp = int(time.time() if now is None else now) + ttl
    fields = f"{_VERSION}.{op}.{sha}.{int(ref_id)}.{exp}"
    key = ensure_file_link_key(store=store)
    return f"{fields}.{_sig(key, fields)}"


def parse(token: str) -> Claims:
    """Split a token into its claims without checking anything —
    :func:`verify` consumes this; the ``pdf_link`` view reads ``exp``
    back for its expiry line."""
    # ``hmac.compare_digest(str, str)`` raises TypeError on non-ASCII —
    # an auth surface answers 403, not a 500, to ``?t=…%C3%A9``.
    if not token.isascii():
        raise TokenInvalid("malformed token")
    parts = token.split(".")
    if len(parts) != 6 or parts[0] != _VERSION:
        raise TokenInvalid("malformed token")
    try:
        ref_id, exp = int(parts[3]), int(parts[4])
    except ValueError:
        raise TokenInvalid("malformed token") from None
    return Claims(op=parts[1], sha=parts[2], ref_id=ref_id, exp=exp)


def verify(
    token: str,
    *,
    op: str,
    sha: str,
    store: Store | None = None,
    now: float | None = None,
) -> Claims:
    """The verified claims, or a raise — never a silent ``None``.

    Recomputes the signature over the token's own fields (the
    ``SessionTokens`` discipline: a token verifies only if it round-trips
    exactly), then compares with :func:`hmac.compare_digest`. Expiry is
    checked *after* the signature: only a well-signed token earns the
    410 — a forgery is a 403 whatever expiry it claims.

    The key is read-only here — a verifier never mints. An absent key
    is a 403, not a vault write (let alone a last-writer-wins race with
    the MCP's own first mint).
    """
    if not token:
        raise TokenInvalid("missing token")
    claims = parse(token)
    if claims.op != op or claims.sha != sha:
        raise TokenInvalid("token does not name this resource")
    key = _resolve_key(store=store)
    if key is None:
        raise TokenInvalid("no file-link key configured")
    fields = token.rsplit(".", 1)[0]
    if not hmac.compare_digest(f"{fields}.{_sig(key, fields)}", token):
        raise TokenInvalid("bad signature")
    if claims.exp <= int(time.time() if now is None else now):
        raise TokenExpired("link expired")
    return claims


__all__ = [
    "DEFAULT_TTL_S",
    "FILE_LINK_SECRET",
    "MAX_TTL_S",
    "OPS",
    "WEB_BASE_URL_SECRET",
    "Claims",
    "LinkTokenError",
    "TokenExpired",
    "TokenInvalid",
    "ensure_file_link_key",
    "mint",
    "parse",
    "verify",
]
