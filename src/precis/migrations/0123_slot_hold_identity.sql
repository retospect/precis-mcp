-- 0123_slot_hold_identity.sql
--
-- Boot-epoch identity on `resource_slot_holds` (self-healing-spine
-- Layer 1, slice 1): a hold whose worker generation is *provably
-- replaced* can be reclaimed immediately instead of waiting out the
-- full TTL (deploy SIGKILL wedged `llm:*` rows at `free = 0` for up to
-- 1 h — TTL was the only arm).
--
-- All three NULLable: a hold minted outside an advertised worker (CLI,
-- or a worker without PRECIS_PROCESS) stamps NULL and keeps the
-- TTL-only recovery — the same asymmetry job leases document in
-- `workers/executors/_common.py::_this_worker_lease_identity`. The
-- existing free-text `holder` column stays operator-debug-only.
--
-- Forward-only (ADR 0005): additive, no data migration.

ALTER TABLE resource_slot_holds ADD COLUMN IF NOT EXISTS holder_host    TEXT;
ALTER TABLE resource_slot_holds ADD COLUMN IF NOT EXISTS holder_process TEXT;
ALTER TABLE resource_slot_holds ADD COLUMN IF NOT EXISTS holder_boot_id TEXT;

COMMENT ON COLUMN resource_slot_holds.holder_boot_id IS
    'Worker boot epoch of the holder (see host_heartbeat.meta.boot_ids). '
    'NULL = unadvertised holder, TTL-only reclaim; non-NULL lets the '
    'reaper reclaim as soon as the generation is provably replaced.';

-- End of 0123_slot_hold_identity.sql
