"""``web_users`` CRUD — the precis-web Basic-auth identity table.

Mixin on :class:`precis.store.Store`. Migration ``0131_web_users.sql``
defines the table (``0134`` adds ``orcid``); :mod:`precis.users` owns the password KDF and the
feed-token digest, so nothing in this module ever sees a plaintext.

Two read shapes, deliberately separate:

- :meth:`get_web_user_credentials` returns the row *with* its
  :class:`~precis.users.PasswordRecord` — the auth gate's hot path.
- :meth:`list_web_users` / :meth:`get_web_user` return
  :class:`~precis.users.WebUser` only, so the CLI and any future UI can
  render the roster without secret material passing through them.

Reads do **not** filter out disabled rows; the caller decides. The gate
rejects ``disabled_at IS NOT NULL`` explicitly so a disabled account is
distinguishable from a deleted one in the logs.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation

from precis.users import PasswordRecord, WebUser, normalize_login, normalize_orcid

_USER_COLS = (
    "id, login, abbrev, full_name, email, disabled_at, "
    "last_login_at, created_at, updated_at, "
    # Presence, never the digest: callers decide whether a podcast link
    # exists, and nothing outside this module needs the hash itself.
    "(feed_token_sha256 IS NOT NULL) AS has_feed_token, orcid"
)


#: Which unique index a collision came from → what to tell the human.
#: Names are Postgres' own defaults for the ``UNIQUE`` columns in
#: ``0131_web_users.sql`` plus the partial index in ``0134``.
_TAKEN = {
    "web_users_orcid_key": "that ORCID iD is already on another account",
    "web_users_abbrev_key": "that abbrev is already taken",
    "web_users_login_key": "that login is already taken",
}


def _taken(exc: UniqueViolation) -> str:
    """A duplicate-key error as a sentence the person who typed it can act
    on. Falls back to the driver's own message for an index this doesn't
    know — a vague sentence beats swallowing which constraint fired."""
    name = getattr(exc.diag, "constraint_name", None) or ""
    return _TAKEN.get(name, str(exc).strip() or "that value is already taken")


def _row_to_user(row: tuple[Any, ...]) -> WebUser:
    return WebUser(
        id=int(row[0]),
        login=str(row[1]),
        abbrev=str(row[2]),
        full_name=row[3],
        email=row[4],
        disabled_at=row[5],
        last_login_at=row[6],
        created_at=row[7],
        updated_at=row[8],
        has_feed_token=bool(row[9]),
        orcid=row[10],
    )


class WebUsersMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    # ── reads ────────────────────────────────────────────────────────

    def count_web_users(self, *, enabled_only: bool = True) -> int:
        """How many accounts exist.

        The auth gate calls this to tell "nobody has run ``precis users
        add`` yet" (503, fail closed) apart from "your password is
        wrong" (401). ``enabled_only`` because a roster of nothing but
        disabled accounts is, for the operator staring at the 503, the
        same situation.
        """
        sql = "SELECT count(*) FROM web_users"
        if enabled_only:
            sql += " WHERE disabled_at IS NULL"
        with self.pool.connection() as conn:
            row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    def list_web_users(self) -> list[WebUser]:
        """Every account, enabled first then by login."""
        sql = (
            f"SELECT {_USER_COLS} FROM web_users "
            "ORDER BY (disabled_at IS NOT NULL), login"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_user(r) for r in rows]

    def get_web_user(self, login: str) -> WebUser | None:
        sql = f"SELECT {_USER_COLS} FROM web_users WHERE login = %s"
        with self.pool.connection() as conn:
            row = conn.execute(sql, (normalize_login(login),)).fetchone()
        return _row_to_user(row) if row else None

    def get_web_user_credentials(
        self, login: str
    ) -> tuple[WebUser, PasswordRecord] | None:
        """The auth gate's read: identity + stored password triple.

        ``None`` for an unknown login — the gate then burns an
        equivalent scrypt (:func:`precis.users.burn_verify`) so the
        miss isn't detectable by timing.
        """
        sql = (
            f"SELECT {_USER_COLS}, password_hash, password_salt, password_algo "
            "FROM web_users WHERE login = %s"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, (normalize_login(login),)).fetchone()
        if not row:
            return None
        record = PasswordRecord(
            password_hash=str(row[11]),
            password_salt=str(row[12]),
            password_algo=str(row[13]),
        )
        return _row_to_user(row), record

    def get_web_user_by_feed_token(self, digest: str) -> WebUser | None:
        """Resolve a ``/podcast?t=`` credential by its SHA-256 digest.

        Enabled accounts only: disabling a user must kill their feed with
        the same keystroke it kills their login.
        """
        sql = (
            f"SELECT {_USER_COLS} FROM web_users "
            "WHERE feed_token_sha256 = %s AND disabled_at IS NULL"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, (digest,)).fetchone()
        return _row_to_user(row) if row else None

    # ── writes ───────────────────────────────────────────────────────

    def create_web_user(
        self,
        *,
        login: str,
        abbrev: str,
        password: PasswordRecord,
        full_name: str | None = None,
        email: str | None = None,
        conn: Connection | None = None,
    ) -> WebUser:
        """INSERT one account. Raises on a duplicate login/abbrev (the
        table's UNIQUE constraints) rather than silently upserting — an
        accidental ``users add`` for an existing login must not reset
        that user's password."""
        sql = (
            "INSERT INTO web_users "
            "(login, abbrev, full_name, email, "
            " password_hash, password_salt, password_algo) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_USER_COLS}"
        )
        params = (
            normalize_login(login),
            normalize_login(abbrev),
            full_name,
            normalize_login(email) if email else None,
            password.password_hash,
            password.password_salt,
            password.password_algo,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = c.execute(sql, params).fetchone()
        assert row is not None  # RETURNING on a successful INSERT
        return _row_to_user(row)

    def set_web_user_password(self, login: str, password: PasswordRecord) -> bool:
        """Replace one account's stored password triple. False if unknown."""
        sql = (
            "UPDATE web_users SET password_hash = %s, password_salt = %s, "
            "password_algo = %s, updated_at = now() WHERE login = %s"
        )
        params = (
            password.password_hash,
            password.password_salt,
            password.password_algo,
            normalize_login(login),
        )
        with self.pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(sql, params)
        return bool(cur.rowcount)

    def update_web_user(
        self,
        login: str,
        *,
        abbrev: str | None = None,
        full_name: str | None = None,
        email: str | None = None,
        orcid: str | None = None,
    ) -> bool:
        """Patch the display fields. ``None`` means "leave alone" — use
        the empty string to clear ``full_name`` / ``email`` / ``orcid``.

        ``orcid`` is normalized (and checksum-validated) here rather than
        trusted from the caller: the column's CHECK is shape-only, so this
        is where a mistyped iD is refused no matter which door it came
        through. Raises :class:`ValueError` on a malformed one — before
        any of the other fields are written, so a bad iD never lands a
        half-applied patch.

        A collision with another account's ``abbrev`` / ``login`` /
        ``orcid`` is a :class:`ValueError` too. The uniqueness lives in
        the table (it has to — two sessions can race), but a bare
        ``UniqueViolation`` reaching the callers means a 500 on the web
        form and a traceback in the CLI for what is, from where the human
        stands, a correctable typo. Translating it here fixes both doors
        at once."""
        sets: list[str] = []
        params: list[Any] = []
        if abbrev is not None:
            sets.append("abbrev = %s")
            params.append(normalize_login(abbrev))
        if full_name is not None:
            sets.append("full_name = %s")
            params.append(full_name or None)
        if email is not None:
            sets.append("email = %s")
            params.append(normalize_login(email) or None)
        if orcid is not None:
            sets.append("orcid = %s")
            params.append(normalize_orcid(orcid) or None)
        if not sets:
            return False
        sets.append("updated_at = now()")
        params.append(normalize_login(login))
        sql = f"UPDATE web_users SET {', '.join(sets)} WHERE login = %s"
        try:
            with self.pool.connection() as conn:
                with conn.transaction():
                    cur = conn.execute(sql, params)
        except UniqueViolation as exc:
            raise ValueError(_taken(exc)) from exc
        return bool(cur.rowcount)

    def set_web_user_disabled(self, login: str, *, disabled: bool) -> bool:
        """Soft-disable / re-enable. Soft because ``abbrev`` is meant to
        stay resolvable for attribution after the person stops logging
        in."""
        sql = (
            "UPDATE web_users "
            "SET disabled_at = CASE WHEN %s THEN now() ELSE NULL END, "
            "    updated_at = now() "
            "WHERE login = %s"
        )
        with self.pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(sql, (disabled, normalize_login(login)))
        return bool(cur.rowcount)

    def set_web_user_feed_token(self, login: str, digest: str | None) -> bool:
        """Store (or clear) the podcast token digest. Rotating overwrites,
        so the previous feed URL stops working immediately."""
        sql = (
            "UPDATE web_users SET feed_token_sha256 = %s, updated_at = now() "
            "WHERE login = %s"
        )
        with self.pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(sql, (digest, normalize_login(login)))
        return bool(cur.rowcount)

    def delete_web_user(self, login: str) -> bool:
        with self.pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    "DELETE FROM web_users WHERE login = %s",
                    (normalize_login(login),),
                )
        return bool(cur.rowcount)

    def touch_web_user_login(self, login: str) -> None:
        """Stamp ``last_login_at``. Best-effort and deliberately outside
        the auth decision — a write failure here must never lock anyone
        out, and the gate's credential cache means it fires roughly once
        per TTL rather than per request."""
        try:
            with self.pool.connection() as conn:
                with conn.transaction():
                    conn.execute(
                        "UPDATE web_users SET last_login_at = now() WHERE login = %s",
                        (normalize_login(login),),
                    )
        except Exception:  # pragma: no cover - liveness bookkeeping only
            return
