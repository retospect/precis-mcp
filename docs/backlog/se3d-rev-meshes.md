---
status: draft
title: "se 3D viewer: ?rev= past revisions draw the live cad design's meshes"
pillar: 3d-design
---

# se 3D viewer: ?rev= realized meshes are revision-blind

Source gripes: gr531335.

## Motivation / why

Found during Do-next 8 (realized-rung meshes, se-3d-viewer thread,
2026-10-28). With the meshes toggle ON, a URL addressing a past se
revision (`?rev=<sha>`) draws the meshes of the LIVE bound cad design —
cad content is not part of the se checkpoint, so the revision's own
components are not recoverable server-side. Envelope geometry is
revision-correct; realized meshes are not. Symptom: revisiting an old
revision of a design whose cad source changed since shows current
geometry labelled as the old revision.

## In scope

One of (which one is the open question):

- refuse realized meshes when `?rev=` is present — envelope-only with a
  note saying why, or
- persist cad snapshots into the se checkpoint so a past revision's own
  meshes are recoverable.

## Explicitly NOT in scope

- The other Do-next 8 residues recorded in the se-3d-viewer thread
  (unapplied fasten-stamped hole cuts on viewer meshes, atom pick, the
  nightly `meshes` probe, the box3 prod copy-down) — separate follow-ups.
- Envelope rendering — already revision-correct.

## Acceptance criteria

TBD — hinges on the ruling:

- (refuse option) `?rev=<sha>` with meshes ON renders envelopes plus a
  visible note, and never draws the live cad design's meshes under a
  past revision's label.
- (persist option) `?rev=<sha>` with meshes ON draws the meshes of the
  cad source snapshotted at that revision.

## Target + blast radius

`precis_web` blocktree 3D route and scene (`scene3d.json?meshes=1`,
`_realized_leaves`); the se checkpoint format if the persist option is
chosen.

## Open questions / decisions log

- **Open — Reto to decide (the gripe's recommendation comment):** refuse
  realized meshes on `?rev=` (envelope-only + note) vs persist cad
  snapshots into se checkpoints. Persist makes every checkpoint pay the
  snapshot cost; refuse loses realized geometry on history views.
