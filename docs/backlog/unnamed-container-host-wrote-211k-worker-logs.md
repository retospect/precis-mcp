# An unnamed container host wrote 211K worker_logs rows for ten days, invisible to every host check

Observed 2026-09-29 while auditing the fleet. `worker_logs` has **five**
distinct hosts, not four:

| host | rows | first | last |
|---|---|---|---|
| melchior | 1.6M | 2026-08-30 | 2026-09-29 (live) |
| castor | 763K | 2026-08-30 | 2026-09-29 (live) |
| balthazar | 534K | 2026-08-30 | 2026-09-29 (live) |
| pollux | 377K | 2026-08-30 | 2026-09-29 (live) |
| **`725f57f94fb1`** | **211K** | 2026-08-30 | **2026-09-09, then silent** |

`725f57f94fb1` is a 12-hex Docker container ID — the ephemeral-identity
case `heartbeat._resolve_host_ephemeral` describes: a worker booted in a
container with no `--hostname`/`PRECIS_HOST_NAME` advertises the
container's throwaway ID as its fleet identity.

**Why nothing said anything.** `_detect_host_dark` excludes it twice over
(gr306275, gr331348): by the `meta.ephemeral` stamp, and by the identity
*shape* — `AND hh.host !~ '^[0-9a-f]{12}$'`. Both exclusions are correct
and deliberate; a torn-down container must not page critical. The
consequence is that this worker ran for ten days, wrote a fifth of
melchior's log volume, and then stopped, with no surface reporting either
event.

**What is not known, and matters:**

- Whose container was it? A local compose stack (the gr331348 writer was
  exactly that — "a stale local compose stack"), a CI container, or
  something on a fleet host.
- Was it claiming and executing *jobs*? Log volume alone does not say. A
  container silently claiming prod work for ten days is a different
  problem from one that only logged.
- Did it stop on 2026-09-09 because someone tore it down, or because it
  broke?

**Ask.** Not "alert on container hosts" — the exclusion is right. Rather:
a container-identity worker that writes to prod `worker_logs` at this
volume should be *attributable*. Something that records "a non-fleet
identity is writing" once, with whatever provenance is available
(`meta`, boot ids, the first log line), so the question above is
answerable later without forensics. Possibly also a guard on whether an
ephemeral identity may claim jobs at all.

**Owner anchors:** `src/precis/workers/nursery.py::_detect_host_dark`
(the `!~ '^[0-9a-f]{12}$'` belt and the `meta.ephemeral` stamp),
`src/precis/workers/heartbeat.py::_resolve_host_ephemeral`,
`_CONTAINER_ID_RE`. Prior art: gr306275, gr331348.

test: a worker booting with a container identity is journalled once with
its provenance, and (if decided) refused a job claim.
