"""Re-dispatch the failed tick-zero replicates WITH the GPU slot token.

The original MCP-dispatched batch (jb263259–263823, idem tick0-<st>-r<n>)
lost 49 of 50 replicates to GPU-slot contention (`infra:child-killed`) —
the MCP verb surface drops `requires=`, so nothing serialized. This re-put
goes through the real `JobHandler.put` with `requires={'gpu': 1}` (the same
token `quest/compute.py` sets, gr192371), copying each failed job's params
and idem_key verbatim (terminal jobs don't block a fresh idem attempt).
Reto-authorized prod write (continue-the-program, 2026-08-28).
"""

from __future__ import annotations

from pathlib import Path


def main() -> int:
    dsn = (
        (Path.home() / ".secrets/pw/PRECIS_DATABASE_URL")
        .read_text(encoding="utf-8")
        .strip()
        .replace("host.docker.internal", "127.0.0.1")
    )

    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler
    from precis.store.store import Store

    store = Store.connect(dsn, min_size=1, max_size=2)
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.ref_id, r.meta->>'idem_key', r.meta->'params',
                       r.parent_id
                  FROM refs r
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE r.kind = 'job'
                   AND r.meta->>'idem_key' LIKE 'tick0-%'
                   AND t.namespace = 'STATUS' AND t.value = 'failed'
                 ORDER BY r.ref_id
                """
            ).fetchall()
        print(f"failed tick0 replicates to re-dispatch: {len(rows)}")

        hub = Hub(store=store)
        jobs = JobHandler(hub=hub)
        put_ok = 0
        for old_id, idem_key, params, parent_id in rows:
            if not isinstance(params, dict) or not idem_key or not parent_id:
                print(f"  SKIP jb{old_id}: missing params/idem_key/parent")
                continue
            resp = jobs.put(
                job_type="autocatpath_seed",
                executor="ssh_node",
                idem_key=idem_key,
                requires={"gpu": 1},
                parent_id=int(parent_id),
                params=params,
            )
            put_ok += 1
            txt = str(getattr(resp, "text", resp))[:90].replace("\n", " ")
            print(f"  jb{old_id} {idem_key} -> {txt}")
        print(f"re-dispatched: {put_ok}/{len(rows)}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
