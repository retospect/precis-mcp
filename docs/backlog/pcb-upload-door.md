# pcb-upload-door — proposal (gr520323)

Status: proposal, Reto 2026-10-31 ("we should make one, propose it").
Blocked-by nothing; the pattern already exists for papers.

## Problem

A 50-part board script's `to_batch()` is ~36 KB of JSON (two
polygon-pad sensor footprints). The only submission path is inline
`put(kind='pcb', args=<36KB json>)` — MCP tool args are a terrible file
format: clients truncate, sessions re-type, and review diffs of giant
single-line payloads. Current workaround is direct `PcbHandler` calls
from host-side scratch scripts (taz-leaf prod pushes), which bypasses
the MCP surface entirely and is not a sanctioned door.

## Proposal: the paper-PDF signed-door pattern, for pcb

Mirror the paper PDF upload door (precedent 3c44a411c — proven in
prod for the same problem class):

1. `precis door pcb <file.json>` (CLI) → uploads the batch file through
   the signed door → returns a `plaintext` ref (or a dedicated
   `pcb_batch` ref kind) plus a sha256.
2. `put(kind='pcb', id='<slug>', args={'batch_ref': '<ref>'})` — the
   handler resolves the ref, validates size/sha/mime (application/json,
   cap e.g. 1 MB), and runs the existing put pipeline unchanged.
3. The door refuses paths above the cap with the same legible envelope
   as the paper door; the put still validates the full rule set
   (courtyards, currents, quadrant) — the door moves bytes, it never
   gates.

Why not the alternatives:

- *Folder watch / put-from-folder*: host-local state a prod handler
  can't see; breaks the "handlers never touch the filesystem" rule.
- *Chunk-assembly through conversation*: what we have now — 36KB of
  session tokens per attempt, unreviewable.

## Exit criteria

- A 50-part taz-leaf-class batch uploads via the door and `put
  (batch_ref=…)` stores a board identical (modulo slug) to the inline
  path — byte-diff the stored rows.
- The taz-leaf prod-push scratch scripts switch to the door and the
  direct-`PcbHandler` workaround is deleted.
- gr520323 closes.
