---
status: in-progress
title: memory file mirror — explicit import and faithful export
pillar: memory-graph
prio: high
---

# Memory file mirror

## Boundary

Reto R17: files and graph coexist. Explicit export supersedes the old
no-export decision for this slice. Real harness import, migration and
cutover remain held; development uses synthetic files and the test DB.
No schema, classifier, reconsolidation, service or scientific changes.

Premise checked on deployed R15: `cli.memory.import_memory_dir` strips
frontmatter; normal reruns skip edits, `--sync` can retire nodes. The
index export is a lint cache named by handles, not a faithful file mirror.
Keep these interfaces unchanged; add `memory mirror import|export`.

## Contract

- `import DIR --namespace NAME`: read flat Markdown topic files and optional
  `MEMORY.md`; preserve exact UTF-8 bytes, including newlines, YAML header,
  literal **Why:**/**How to apply:** and index formatting. Topics require
  YAML `name`, `description`, `metadata.type` in user/feedback/project/reference.
  Preserve additional JSON-compatible YAML metadata. No symlinks, nested paths,
  duplicate YAML keys, case-folded filename collisions or ambiguous slugs.
- Namespace plus filename is identity in `refs.meta.file_mirror`; same slug
  updates the same memory. Namespace advisory lock serializes mirror runs;
  owning refs lock `FOR NO KEY UPDATE` in id order. Body, metadata, links and
  import baselines commit together. No new tables or constraints.
- Two-pass link resolution supports forward `[[slug]]` and sibling Markdown
  links only; other prose remains literal. The public memory autolinker is not
  reused here: its pooled reads cannot participate in the mirror transaction.
  Persist unresolved targets in mirror metadata/report; resolve on a
  later import. Remove only mirror-owned links no longer present. Preserve
  manual/native links; refuse a provenance collision rather than taking ownership.
  Refuse changed bodies with inbound/outbound chunk-anchored links, because
  replacing a body chunk would cascade-delete those links. Lock chunks before
  the fresh link check; this is not implicit retargeting of someone else's anchor.
- Compare current graph title, body, file-authored metadata and full owned-link
  state (endpoints, provenance metadata and set_by) with the last
  imported baseline under the lock. Refuse graph divergence rather than
  overwriting it, even if files are unchanged. Never adopt legacy imported
  nodes by title or silently reactivate retired refs.
- Read a file-set snapshot and recheck before commit; changed files abort the
  import. Noncooperating filesystem writes after that check belong to the
  next snapshot; no claim of a distributed filesystem/DB transaction.
- Missing files/nodes are reported and retained, never retired. No implicit
  sync, watcher, source-root registration or automatic source-directory write.
- `export --namespace NAME DEST`: export a coherent graph snapshot into a
  new, exclusively created directory using original filenames. Existing
  destinations are refused, so concurrent/local files are never overwritten.
  Preserve original header bytes when metadata is unchanged; render graph body
  edits verbatim. Graph title/hook changes cannot be faithfully represented
  without a metadata policy: refuse them with an explicit conflict.
  The same refusal covers all file-authored YAML changes. Export does not reset import baselines. Reimporting graph edits into the
  same namespace stays a conflict until a later explicit reconciliation design.

## Acceptance and validation

Synthetic test DB: unchanged rerun preserves ids/chunks/events; changed body
and links update the same ref; forward and initially unresolved links resolve;
owned link removal preserves other edges; namespace/case/YAML collisions refuse;
missing files stay live; graph edits and in-flight file changes refuse atomically;
import/export is byte-for-byte for 120 topics plus a 15 KB index, including
CRLF, no final newline, Unicode and extra YAML fields; modified owned links
refuse import; public graph
body edits export faithfully. Unsafe paths and existing destinations refuse.
Host Ruff now; canonical focused tests and scoped types only after coordinator
releases the R16 queue. Reconcile latest main before independent review.
Coordinator owns version/full release gate; no production migration acceptance.

## Live precursor evidence

Native readback verified `td470292`: isolated `me470290`/`me470291` on R15
e77f51e0b92d18424b4fb7731192f02266e44609, 8.35.14. Anchored edit, unchanged
public validation refusal and sequential reciprocal mentions passed.
Transaction rollback and concurrency were not demonstrated by these live calls.
