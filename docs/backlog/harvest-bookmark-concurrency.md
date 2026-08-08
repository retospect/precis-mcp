# autocatpath harvest bookmark — multi-job concurrency edge

3e746728 fixed the single-in-flight case, but the loop still advances
`cp_seen` to the newest measure-yielding job even when an *older* job in the
same batch is unresolved — with 2+ concurrent autocatpath jobs per candidate,
the older one's barrier is permanently skipped once it completes. Unconfirmed
as a live scenario (dispatch appears to mint one job per candidate). Design
call: bookmark = min over any still-pending job's predecessor, or per-job
harvested state instead of a single high-water mark. Owner
`src/precis/quest/compute.py::harvest_measures`.
