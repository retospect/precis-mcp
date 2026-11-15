-- 0329_read_receipts.sql
--
-- gr660588: the exact-read receipts that license ``text={'$ref': handle}``
-- in put/edit lived only in a per-process dict keyed on the live
-- ServerSession object, so every MCP restart silently dropped them (and
-- ``stateless_http`` could never work — a $ref always needs a session a
-- previous request already wrote to). This table makes the receipt
-- durable: keyed on the authenticated owner the token-auth middleware
-- stamps (``oauth:<grant>``, ``personal:<token_id>``, ``install``,
-- ``slice:<grant>``), not the transport session. An owner that read a
-- handle exactly may $ref it from any of its sessions within the TTL —
-- deliberately the owner, not the conversation (spec option a): the
-- receipt is a same-principal proof, and one bearer already holds the
-- whole permission set its sessions share).
--
-- ``identity`` is the serialised receipt identity
-- ``<kind>:<ref_id>:<chunk_id|->`` written by
-- ``precis.runtime.handle_deref``; ``expires_at`` carries the 1 h TTL.
-- Writes upsert on the PK and sweep expired rows on the same round trip
-- (``precis.store._receipt_ops``); the ``expires_at`` index exists so
-- that sweep and any later bulk prune stay an index range delete.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS read_receipts (
    owner_key  text        NOT NULL,
    identity   text        NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (owner_key, identity)
);

CREATE INDEX IF NOT EXISTS read_receipts_expires_idx
    ON read_receipts (expires_at);

COMMENT ON TABLE read_receipts IS
    'gr660588: durable exact-read receipts for text={$ref} substitution '
    '— keyed on the authenticated owner (principal stamp), surviving '
    'MCP restarts and enabling stateless_http. Session-less callers '
    'keep the in-process receipts dict in runtime.handle_deref.';

COMMIT;

-- End of 0329_read_receipts.sql
