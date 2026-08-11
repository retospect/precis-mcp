# NFS export leftovers — orphan uid 503 + retired monolith homes

Residue found during the 2026-08 uid/gid parity work (parity itself is done:
gid==uid fleet-wide, `users_gid_drift_fatal: true` armed, vestigial
prometheus/grafana 803/804 retired — spark's apt-managed monitoring keeps
those names as the distro's own package accounts).

Two things remain in caspar's `/opt/nfs/shared` export:

- **`data/papers` is owned by uid 503, which has no account on caspar at
  all.** Figure out what wrote it (a pre-cluster Mac's first local user is
  the usual suspect for 503) and chown it to whichever principal owns paper
  ingest today.
- **`home/openclaw{,/scratch,/.ssh}` and `home/pgboss`** are homes of
  retired monolith users. They want *deleting*, not relabeling — destructive,
  so eyeball contents first and get a nod.

All five dirs are `drwxr-xr-x`, so their stale gid-20 labels grant nothing
in the meantime.

Owner: caspar `/opt/nfs/shared` (the `nfs_servers` host).
Test: `find /opt/nfs/shared -nouser -o -nogroup` returns nothing, and the
retired homes are gone.
