# Patent kind — deferred follow-ons

From the shipped patent-kind spec (git-only); truth in the
`precis.handlers.patent` docstrings.

- **Deploy artefacts**: the ansible launchd timer for
  `run-patent-watches` (balthazar) and the shared NFS mount playbook
  (`/opt/nfs/shared/patents/`) — the watch machinery itself shipped.
- **`view='images'`** — on-demand fetch from EPO `published-data/images`
  to `$PRECIS_PATENT_RAW_ROOT/.../images/`; image bytes never touch
  Postgres (same boundary as audio-on-NFS). TIFF→web conversion is a
  separate downstream concern.
- **Local-vs-remote ranking bias** — small constant bias for `[local]`
  hits; tune empirically from logs.
- Decided constraints (don't revisit without cause): no
  `put(mode='ingest')` (`get(id=)` already ingests); no `view='full'`
  raw ST.36 (the XML lives on disk for forensic re-parse); family
  members stay one-ref-per-DOCDB-id — the `patent_family` relation
  (migration 0115) now covers family linkage, so check whether the old
  "family-aware dedup" open question is fully closed by it.
