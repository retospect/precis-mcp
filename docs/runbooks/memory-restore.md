# Restoring repo-dev memory nodes

**When.** A memory node was deleted or overwritten by mistake, or the graph is
unreachable and you need the text. Three sources, cheapest first: undelete in
place, the SessionStart node cache, the nightly dump.

## 1. Undelete a soft-deleted node (prod, in place)

`delete(kind='memory', id=...)` only sets `refs.retired_at = now()`
(`Store.retire_ref`, `src/precis/store/_refs_ops.py`). Nothing else is touched:
body chunks, tags and `links` rows stay, so clearing the flag brings the whole
node back with its handle, links and tags (`Store.restore_ref` documents the
same). A restore like this is not destructive, so it needs no extra ask.

Find it (read-only):

    scripts/prod-psql --ro "SELECT ref_id, title, retired_at FROM refs
      WHERE kind='memory' AND retired_at IS NOT NULL
      ORDER BY retired_at DESC LIMIT 20"

`ref_id` is the number in the handle (`me<ref_id>`). Restore, inside a
transaction with a verify:

    scripts/prod-psql "BEGIN;
      UPDATE refs SET retired_at = NULL, updated_at = now()
        WHERE ref_id = <id> AND kind = 'memory' AND retired_at IS NOT NULL;
      SELECT ref_id, title, retired_at FROM refs WHERE ref_id = <id>;
      COMMIT;"

Then `get(kind='memory', id='me<id>', view='fisheye+1hop')` and check the links
are present. Caveat: a node that was *superseded* (merged into a survivor) had
its links migrated to the survivor at that time; undeleting the original
restores only what is still on it, and the survivor keeps the migrated links.
`updated_at` moves, so the node reads as freshly edited (live-thread lists
sort by it).

The precis web app has the same operation: `POST /refs/<kind>/<id>/undelete`
(the Undelete button on the tombstone page, `src/precis_web/routes/refs.py`).

## 2. Restore from a node-cache export

The SessionStart hook (`scripts/hooks/session-start-memory.sh`) writes every
topic node to `~/.cache/precis/memory-nodes/<handle>.md` (`# title`, blank
line, body) plus `_sections.tsv`. Before each replacement it keeps the previous
set as `memory-nodes.<UTC YYYYMMDDTHHMMSSZ>`, newest 7, at most one per 6 hours
(`PRECIS_MEMORY_ROTATE_SECS`). A failed or empty export never rotates. Older
`memory-nodes.old.<pid>` / `.tmp.<pid>` dirs are CLI debris, not rotation; they
are safe to delete.

**There is no import path from this format.** `precis memory import` reads a
harness directory (`MEMORY.md` plus topic files) and refuses a graph-pointer
`MEMORY.md`; `precis memory mirror import` needs YAML frontmatter (`name`,
`description`, `metadata.type`) per file and a source `MEMORY.md`
(`src/precis/cli/memory_mirror.py::_parse`). The cache files have neither. So:

- If the node still exists (live or retired), prefer section 1; to put back
  old text use `edit(kind='memory', id='me<id>', ...)` with the cached body.
- If the row is gone, recreate with `put(kind='memory', ...)` from the cached
  title and body. It gets a new handle; inbound links and the old handle are
  not recovered (rebuild them with `link(...)`; `_sections.tsv` has the old
  section and parent columns).
- Do not run `precis memory import` / `--sync` against the pre-cutover
  `MEMORY.md.pre-cutover` snapshot: it would revive retired threads.

`precis memory mirror export <new dir> --namespace <ns> --no-stamp` writes a
full YAML/Markdown snapshot of native nodes without touching the graph; that
format *is* importable by `mirror import`. Taking one before a risky bulk edit
is the supported way to get a restorable file backup (no schedule runs it).

## 3. Selective restore from the nightly dump (last option)

Restore the dump into a throwaway container per
[`backup-restore-drill`](./backup-restore-drill.md) (about 2 hours; the dump is
taken 03:30 UTC, so it is up to a day old), then copy only the rows you need
into prod: the `refs` row, its `chunks` (append-only: INSERT, never UPDATE),
`ref_tags`, and `links` for those ids. Keep the original `ref_id` only if it is
free in prod. Never restore the whole dump over prod for a node.
