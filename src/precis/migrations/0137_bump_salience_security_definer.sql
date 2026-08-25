-- 0137_bump_salience_security_definer.sql
--
-- Make ``bump_salience(bigint[])`` (0007) SECURITY DEFINER so read-only
-- connections can still heat salience.
--
-- Every read verb's access accounting funnels through this one function:
-- search result pages (``_paper_search`` / ``_cache_base`` / structure /
-- cad / pcb / numeric-ref handlers), chunk gets, and the paper reader's
-- ref-scoped bump (``store/_blocks_ops.py::bump_salience``,
-- ``bump_salience_for_ref``). As a plain LANGUAGE sql function it runs
-- with the CALLER's privileges, so under the read-only ``agent_ro`` role
-- (the ``precis-ro`` session MCP server; ``envelope.py::db_role`` tier-2
-- boxes) the UPDATE on ``chunks`` raises InsufficientPrivilege and the
-- whole read hard-fails — semantic search errors out instead of serving
-- hits (found 2026-08-25 while standing up the read-only subagent
-- surface).
--
-- A salience bump from a read-only reader is a *genuine external
-- access* — exactly the signal ``last_seen``/``accesses`` exist to
-- record, and already the one metadata-only write sanctioned on the
-- search path (never touches ``chunks.text``). So the fix is the 0079
-- pattern (``file_gripe_readonly``): a narrow, named SECURITY DEFINER
-- verb granted to PUBLIC rather than to a role — ``agent_ro``/
-- ``agent_rw`` are provisioned out-of-tree in ansible, so a fresh/test
-- DB has no such role to grant to. Background-loop self-heat suppression
-- is unaffected: it happens caller-side (``store/_salience.py``), before
-- the function is ever called.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE; body identical
-- to 0007's — only SECURITY DEFINER + the search_path pin are new).

BEGIN;

CREATE OR REPLACE FUNCTION public.bump_salience(ids bigint[]) RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE chunks SET last_seen = now(), accesses = accesses + 1
    WHERE chunk_id = ANY(ids);
$$;

COMMENT ON FUNCTION public.bump_salience(bigint[]) IS
    'Advance last_seen=now() and accesses+1 on a page of chunk ids — the '
    'metadata-only access-accounting write on the read path. SECURITY '
    'DEFINER so a read-only connection (agent_ro / precis-ro server / '
    'write:none envelope) can still heat what it reads; see '
    'store/_blocks_ops.py::bump_salience and migration 0079 for the '
    'pattern.';

REVOKE ALL ON FUNCTION public.bump_salience(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bump_salience(bigint[]) TO PUBLIC;

COMMIT;
