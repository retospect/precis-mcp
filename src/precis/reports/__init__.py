"""Deterministic report tables, separate from human-authored report prose.

Collectors use creation windows for activity and current live terminal tags
for closure clocks, matching the flow view. Open-age tables are a current
snapshot; they cannot reconstruct removed historical status tags.

The scheduler is opt-in via ``PRECIS_WEEKLY_REPORT_ENABLED=1``. The paired
``PRECIS_WEEKLY_REPORT_DRAFT`` slug and ``PRECIS_WEEKLY_REPORT_SECTION``
heading handle select its destination; absent both, tables stay on the job.
``PRECIS_WEEKLY_REPORT_CHECKOUT`` selects the optional Git checkout (main ref).
On-demand jobs accept the same destination as ``params.draft``/``section``.
"""
