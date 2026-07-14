# anki — cloze cards synced to AnkiWeb, as a model of what you know (design-of-record)

> Design-of-record for the `anki` kind and its headless AnkiWeb sync. The
> **buildable units** are carved into `docs/proposals/`: `anki-cloze-kind.md`
> (slice 1, the kind itself, no Anki dependency), with the sync tick and the
> retention model as fast-follows described here. This file is the full picture
> the slices reference; keep it true. Supersedes the thin, half-wired
> `flashcard` kind (retired — see below).

## Why

Reto lives in Anki. Today the corpus (papers, drafts, the work) and the
retention layer (Anki) are two disconnected apps: precis can *write* knowledge
but has no idea what Reto has actually **internalised**. The goal is to close
that loop in both directions:

1. **Author cloze cards in precis, land them in AnkiWeb** — the reading→retention
   direction. A fact worth remembering becomes an `{{c1::…}}` card without
   leaving the corpus.
2. **Read Anki's decay stats back as a signal of what Reto knows** — the
   retention→reading direction, and the actual prize. Anki already computes a
   per-fact forgetting curve (`interval`, `ease`, `reps`, `lapses`, `due`); if
   precis mirrors that, it can **assemble explanations at the right altitude**
   (don't re-derive what's mature; reinforce what's lapsing; explain from
   scratch what's uncarded) and **find gaps** (key claims with no card, cards
   going stale) and mint cards to close them. That is "help me be better,"
   grounded in Reto's real forgetting curve rather than a guess.

The knowledge-model in (2) is the north star; the kind and the sync in (1) are
the substrate that gets us there.

## Non-negotiable: never corrupt the Anki collection

Reto's collection holds notetypes precis does not understand (image occlusion,
custom structured types) and will grow more. The floor is **precis must never
corrupt any of it**. This is held **structurally, not by discipline** — three
hard rules, each a property of the mechanism:

1. **Own-notes-only writes.** precis only ever inserts / updates / deletes notes
   it owns — the `Precis` deck, the **stock Cloze notetype**, tagged
   `precis::<ref_id>`. It never touches a foreign note, never mutates a notetype/
   model, never runs a schema-changing op after bootstrap. Crucially the Anki
   `guid` is keyed on the **precis `ref_id`, not the card text**: a text edit
   *updates* the existing note in place, preserving its scheduling history — it
   never re-guids (which Anki would read as a new card and reset the forgetting
   curve). These are field-level edits on our own rows; they merge incrementally
   and do **not** escalate to a full sync.
2. **Guarded full-sync — abort on any *lossy* one, allow the bootstrap.** A full
   sync is dangerous only when it would *overwrite* a non-empty side. So the guard
   is: if sync reports *full sync required* and either side has un-pushed content
   that would be lost, precis **aborts and raises a `kind='alert'`** — it never
   auto-picks a direction. The **one** allowed exception is the initial
   **download into a provably-empty fresh mirror** (the bootstrap seed — nothing
   local to lose). Using the stock Cloze notetype + adopting the existing deck
   avoids a schema-bump full-sync on first write; if the very first sync still
   demands one, it can only be a download-only seed. Worst case thereafter is
   "new cards didn't sync until you look," never "review history clobbered."
3. **Single authoritative mirror.** Exactly one host holds the `.anki2` and
   syncs (advisory-locked). Two mirrors syncing one account is how you'd
   *manufacture* the full-sync conflict, so it is structurally forbidden. We never
   call `sync_media` (our cards are text), removing another conflict class.

Under these, the worst a bug in our code can do is mangle our own regenerable
`Precis` cards. Everything foreign is unreachable by any write. The retention
read-back (below) and the foreign-ingest slice are **pure reads** — they cannot
corrupt by construction.

## Delivery: headless, precis-driven AnkiWeb sync

There is **no Anki desktop** in this deployment, and AnkiWeb exposes **no
per-note API** — the only way onto the account is to sync a whole collection as
a client. So precis *is* the Anki client: it holds a local `.anki2` mirror and
drives Anki's own sync via the official **`anki` Python package** (pylib — the
real GPL backend, not a reverse-engineered protocol). This is the only headless
path to the AnkiWeb account; the design makes it safe rather than avoiding it.

### The sync tick

An **occasional batch tick** (precis triggers it "from time to time"), not a
resident daemon:

```
advisory-lock(anki:mirror)          # single authoritative runner
open .anki2 mirror (seed via bootstrap download if empty/absent)
  own-notes-only: insert-or-update our Precis cloze notes by precis::<ref_id>
                  (guid keyed on ref_id → text edits update in place, keep schedule)
  col.sync_collection()                       # incremental
    └─ full-sync-required AND lossy? → ABORT + raise kind='alert', leave intact
       (empty-mirror bootstrap download is the one allowed full sync)
  read back stats for ALL notes → precis      (interval/ease/reps/lapses/due)
close mirror
```

- **Weight, honestly.** Heavier than a desktop-owned sync would be, because
  AnkiWeb syncs whole collections — the mirror holds *all* notetypes (text only;
  media off). The `anki` wheel is a Rust-backed dependency, gated on **one
  runner** behind an optional `[anki]` extra + `PRECIS_ANKI_ENABLED` (default-off
  → merges dark, per repo convention). "Light on the server" is honoured where it
  matters: the tick is occasional, opens/closes the mirror per run, and is not a
  cluster-wide pass.
- **Version tax.** AnkiWeb periodically raises the minimum sync version; the
  `anki` package must be kept current or syncs start bouncing. Noted as an
  ongoing maintenance cost, not a one-time build.
- **Runner.** The Mac's `precis-infra` local stack is the natural home:
  off-cluster, a single box (single-runner is easy to guarantee), and the
  AnkiWeb credentials stay on Reto's machine as a local secret. A cluster host
  (melchior) with the `.anki2` on the NAS is the alternative; the Mac is
  preferred.

## The `anki` kind

A numeric ref (reusing the `flashcard`/`_numeric_ref` infra), `corpus_role='none'`
(an authored artifact, never cited as evidence — like `figure`/`plan`).

### Storage shape — generic, so "additional stuff later" needs no migration

Authoring is **cloze-only** for now, but the note is stored generically so a
future structured notetype drops in without a schema change:

```jsonc
meta = {
  "notetype": "Cloze",                 // default; the only authored type in v1
  "deck":     "Precis",
  "fields": {                          // key→value for ANY notetype
    "Text":       "The mitochondrion is the {{c1::powerhouse}} of the cell.",
    "Back Extra": "Krebs cycle happens in the matrix."   // optional, see below
  },
  "anki": {                           // sync-state block, written by the tick
    "note_id":     1699999999999,      // AnkiWeb note id once synced
    "guid":        "precis:1234",      // deterministic → re-push updates, no dup
    "last_synced": "2026-07-12T10:00:00Z"
  },
  "anki_stats": {                     // decay signal, read back for ALL cards
    "interval": 34, "ease": 2500, "reps": 6, "lapses": 1,
    "due": "2026-08-15", "last_reviewed": "2026-07-12"
  }
}
```

- **The card body** is the cloze text with `{{cN::…}}` markup. The cloze notetype
  expands it to N cards for free (`{{c1::}} {{c2::}}` → two cards; same index →
  revealed together; `{{c1::answer::hint}}` → a hint).
- **Super-terse meta, from time to time** → the optional **`Back Extra`** field:
  a short annotation shown only on the answer side — a source, a mnemonic, a
  one-line gotcha. Empty by default; used sparingly by convention (the skill
  says "terse or omit"). This is the "some super terse meta from time to time"
  Reto asked for, mapped onto Anki's native affordance rather than a bespoke
  field.
- **Searchable in the corpus.** On write, the card emits a `card_combined` chunk
  built from the *stripped* cloze text (markup removed) + `Back Extra`, so cards
  embed and appear in `search(kind='*')` like any other ref.
- **No SM-2 in precis.** Anki owns scheduling; precis only *mirrors* the stats it
  reads back. (This is the dead weight retired from `flashcard`.)

### Verbs

`put` (author a cloze card), `get` (+ `/recent`, `/due` from `anki_stats.due`),
`search`, `tag`, `link` (e.g. `derived-from` a paper/draft), `delete`. `edit` of
the cloze text follows the append-only card discipline (delete + re-insert so the
chunk/embedding cascade re-runs; the sync tick then updates the note by `guid`).

### Retiring `flashcard`

`flashcard` is thin and its only "smarts" is a half-wired SM-2 scheduler that
never writes — exactly what Anki does better. Prod has **0 live flashcard refs**
(5 exist, all already soft-deleted), so there is nothing to migrate. Slice 1
deprecates the `flashcard` KindSpec and points the skill at `anki`.

## The knowledge model (north star)

Once the tick reads back **all** cards' stats (free once we're syncing), each
card is a fact with a retention signal attached. Two capabilities fall out:

1. **Retention-aware retrieval.** When precis assembles an explanation or a
   draft, it co-retrieves the user's cards near the topic and reads maturity:
   mature → *known, build on it*; lapsing/low-ease → *reinforce*; no card →
   *explain from first principles*. The help adapts to actual knowledge instead
   of a flat altitude.
2. **Gap / weakness pass + card minting.** A pass cross-references the corpus
   (papers, drafts, active work) against card coverage + maturity and surfaces
   *key claims with no card* (gaps) and *cards going stale* (decay). It mints
   precis cloze cards for the gaps — which sync back — closing the read→retain
   loop.

**Honest caveat.** Card maturity is a **proxy**, not ground truth: a mature card
doesn't prove mastery, and no-card doesn't prove ignorance. precis *weights* the
signal in how it explains; it does not treat it as fact. It is, however, the only
retention signal grounded in Reto's real forgetting curve.

**Foreign-ingest is where the whole collection enters PG.** The sync mechanism
needs only *our* cards in Postgres. The knowledge model wants *all* of them:
a read-only pass reads every note's fields off the mirror (`notesInfo` shape —
generic key→value for any notetype, including the weird ones) and embeds them as
`anki` refs marked `readonly`/`foreign`, carrying `anki_stats`. Bounded (a
personal collection is thousands of cards, not the paper corpus's millions),
incremental per sync, riding the embed pipeline precis already runs, and — being
pure read — incapable of corruption. This is the slice that makes precis adaptive.

## Slices

1. **`anki` kind + retire `flashcard`** (`docs/proposals/anki-cloze-kind.md`) —
   migration, KindSpec (generic `meta.notetype`/`fields`/`deck` + optional
   `Back Extra`), `card_combined` chunk, put/get/search/tag/link/delete, the
   `precis-anki-help` skill (cloze authoring rules + terse-meta convention +
   exemplars). Deprecate `flashcard`. **No Anki dependency yet** — cards author,
   store, and search in the corpus. Standalone, shippable.
2. **Headless sync tick** — the `anki`-pylib mirror job: single-runner advisory
   lock, add-only writes by `precis::<ref_id>`, incremental `sync_collection`
   with the **hard abort-on-full-sync guard**, stat read-back for **all** cards
   into `anki_stats`. Optional `[anki]` extra + `PRECIS_ANKI_ENABLED`, on the Mac
   local stack. AnkiWeb creds as a local secret. The payoff slice, where the care
   goes.
3. **Retention model** — read-only foreign ingest (embed all notetypes) +
   retention-aware retrieval + the gap/weakness card-minting pass. The north star.

Ship incrementally, but design slice 2's read-back to capture **every** card's
stats from the start — it's free once syncing, and slice 3 needs the data waiting.

## Slice 2 — build status (2026-07-13)

**Core BUILT** on `worktree-anki-sync` (not yet shipped), proven end-to-end
against Reto's real AnkiWeb account first (7168 notes; Cloze=4772, Image
Occlusion Enhanced=1901 — the untouchable foreign types):

- `src/precis/anki/notes.py` — pure conventions (guid `precis:<ref_id>`, deck
  `Precis`, `precis::managed` tag, ref→spec, stats aggregation).
- `src/precis/anki/sync.py` — engine (lazy-imports `anki`): `upsert_notes`
  (add-only-own-notes by deterministic guid), `read_precis_stats`,
  `read_all_cards` (pure read of ANY notetype — the accessibility + precis-fix
  substrate), and `sync_tick` (the guard: allow FULL_DOWNLOAD, **refuse
  FULL_UPLOAD**).
- `precis anki-sync` CLI (`cli/anki_sync.py`) — single-runner pg advisory lock,
  reads `anki` refs → upsert → guarded sync → writes `meta.anki_stats` back.
  Gated `PRECIS_ANKI_ENABLED`; `anki` wheel lazy-imported (ansible installs it
  on the one runner; NOT a locked dep). Config: `PRECIS_ANKI_{ENABLED,USER,
  PASSWORD,MIRROR_DIR,DECK}`.
- Tests: `tests/test_anki_sync.py` — 6 pure (always) + 5 local-collection
  (`importorskip('anki')`, no network).

Proven pylib idioms (from live probes): `sync_login(u,p,None)` → hkey;
`sync_collection(auth,False).required` (NO_CHANGES/NORMAL/FULL_SYNC/FULL_DOWNLOAD/
FULL_UPLOAD) + `new_endpoint` + `server_media_usn`; bootstrap =
`close_for_full_sync()` → `full_upload_or_download(upload=False)` →
`reopen(after_full_sync=True)`. `anki` 26.05 arm64 wheel, `pip install anki`
(prebuilt, no Rust toolchain).

## Accessibility of foreign clozes — OPEN FORK

precis-authored clozes are PG-authoritative (born as `anki` refs). The question
is the **foreign** cards (your 4772 hand-made Cloze notes + others):
- **(A) read-only PG projection** — ingest foreign cards as `readonly`/`foreign`
  `anki` refs each sync (embedded + searchable). Unlocks unified semantic search
  + the retention-aware knowledge-model. The mirror/AnkiWeb stays the *source of
  truth*; PG holds a derived, disposable, re-syncable index (DRY in spirit, like
  paper chunks vs the PDF on disk). `read_all_cards` already provides the read.
- **(B) mirror-only** — never copy foreign cards to PG; query the `.anki2` live
  when needed. DRYest + always fresh, but no semantic search / knowledge-model
  over them.
**(A) CONFIRMED + BUILT** (2026-07-13, `src/precis/anki/project.py`). Each sync
(`--project` / `PRECIS_ANKI_PROJECT_ENABLED`) reads the whole mirror and upserts
foreign cards (any notetype) as **read-only** `anki` refs (`meta.source=
anki-foreign`, `readonly`, `anki-foreign` flag), emitting a plain-text (HTML +
cloze stripped) `card_combined` chunk so they embed + search. Idempotent + cheap:
a per-card `content_sha` re-embeds only *changed* cards; vanished guids are
soft-deleted; precis-authored notes (`precis:<id>`) are skipped (already
authoritative). Read-only derived index — can't corrupt the account. Tests:
`tests/test_anki_project.py`.

## precis-fix — the human→LLM→card feedback loop (slice 2.5, requested 2026-07-13)

Tag a card **`precis-fix`** *inside Anki* (phone/desktop) and write what's wrong
in a note field (comment). The sync tick then: `read_all_cards(col, tag=
'precis-fix')` → an LLM reads the card fields + the comment + notetype and
rewrites it → precis writes the fix **back to that foreign card's text fields** →
swaps `precis-fix`→`precis-fixed` (and appends a one-line "fixed: <what>" note) →
syncs up. This is a **deliberate, per-card widening** of own-notes-only: the tag
IS the user's explicit consent to edit that one foreign card, so the corruption
floor holds (precis still never touches an *un-tagged* foreign note). Targets
text-bearing notetypes (cloze/basic); can't fix an occlusion image, only its
text. **BUILT** (`src/precis/anki/fix.py`: `find_fix_requests` / `propose_fix`
(claude_p) / `apply_fix` / `run_fix_pass`; integrated into `sync_tick(fix=True)`,
run after download & before push so fixes ride the sync up; `precis anki-sync
--fix` / `PRECIS_ANKI_FIX_ENABLED`). Tests: `tests/test_anki_fix.py`.

## Slice 4 — authoring craft, decks, retention finder (2026-07-14)

Driven by "asa seems confused" + Reto's card-authoring scheme:
- **Per-card decks** — a `deck-<topic>` create-tag files an authored card under
  the `Precis::<topic>` sub-deck (Anki auto-creates it); no tag → `Precis`. The
  `_initial_meta` hook now takes the tag set; `AnkiCardSpec.deck` + `upsert_notes`
  use it per note. Keeps authored cards namespaced, away from hand-made decks.
- **Stats on foreign cards** — `read_all_cards` now aggregates each note's
  per-card stats; the projection stores `meta.anki_stats` and **refreshes it even
  when content is unchanged** (a cheap meta-only patch, no re-embed) so recall is
  current for free.
- **Leech-finder** — `get(kind='anki', id='/leeches')` lists bad-recall cards
  (lapses ≥ 4 or ease ≤ 2.0) worst-first, across authored + projected. The
  retention loop's entry point: fix the cloze (tag `precis-fix`) or study more.
- **Skills** — new `precis-cloze` (the craft: dedup-first, one-cluster-per-card,
  educational-priority cN ordering, hint types, terse-for-comprehension, deck
  naming, a language-learning worked example). `precis-anki-help` gains
  discoverable search/leech headings + deck tag; `precis-toolpath-help` gains an
  "author spaced-rep cards" sequence (search-first → put). Fixes asa's gap: no
  toolpath authoring path + thin craft guidance.

## Incident 2026-07-14 — sync duplicated the account (fixed)

The enabled sync tick pushed **85,932 junk notes** to Reto's live AnkiWeb (~220k
cards) and created 93,102 dup PG refs. Two interlocking bugs:

1. **The push list included read-only projections.** `spec_from_ref` built the
   "cards to push" from *all* `anki` refs, including `source='anki-foreign'`
   projections of the user's own cards → pushed them back as new notes.
2. **The stats write-back clobbered `meta.anki`.** The CLI patched
   `meta_patch={"anki": {"last_synced": now}}`; `meta ||` is a **shallow** merge,
   so it *replaced* the whole `meta.anki` object, wiping the `guid`/`content_sha`
   the projection dedups on → next tick couldn't match the card → inserted a dup
   ref → which (bug 1) got pushed → ~13× compounding.

Fixes: (1) `spec_from_ref` returns None for `anki-foreign`/`readonly` refs —
**only authored cards ever go up**; (2) the write-back uses a **flat**
`anki_synced_at` key, never nested under `anki`. Regression tests:
`test_spec_from_ref_skips_foreign_projection`,
`test_stats_writeback_does_not_clobber_guid_or_dedup`. Cleanup: account restored
from backup; melchior mirror + daemon removed; all 93,102 junk refs soft-deleted.
Re-enable only after a manual verify shows 0 cards pushed. NB: `last_synced` in
the storage-shape block above is now the flat `meta.anki_synced_at`.

## Decisions log

- **New `anki` kind, retire `flashcard`** (not "add an exporter to flashcard").
  flashcard's SM-2 is redundant with Anki and never wrote; 0 live refs to migrate.
- **Cloze-only authoring, generic storage.** `meta.notetype`/`meta.fields` carries
  any notetype so future structured types need no migration; only Cloze is
  authored in v1. Image occlusion is explicitly out (Reto hand-authors those).
- **Headless precis-driven sync via the official `anki` pylib**, not `.apkg`
  export (one-way, no stat read-back), not AnkiConnect (no desktop here), not a
  hand-rolled protocol (brittle). AnkiWeb has no per-note API; whole-collection
  sync is unavoidable.
- **Non-corruption is structural**: add-only own-notes writes + abort-on-full-sync
  + single authoritative mirror + media-off. The abort guard is the accepted price
  of "never corrupt" — cards may sit un-synced until a human resolves, by design.
- **Terse meta → the native `Back Extra` field**, used sparingly, not a bespoke
  schema field.
- **The knowledge model is the goal, not a nice-to-have.** Slices 1–2 are the
  substrate; slice 3 (retention-aware help + gap-closing) is why we're doing this.
  Maturity is weighted as a proxy signal, never asserted as fact.

## Open questions & risks (unresolved)

- **First-sync schema behaviour (slice-2 blocker to verify).** Does adding a note
  with the *stock* Cloze notetype into a new `Precis` deck trigger a schema-bump
  full-sync? If yes, the bootstrap must be structured as download-first (seed the
  mirror from AnkiWeb, *then* add our notes, *then* incremental-sync up). This is
  the sharpest risk — verify against the real `anki` pylib + a throwaway AnkiWeb
  account before building the guard.
- **`anki` pylib packaging.** Confirm an importable, arm64-compatible wheel for
  the Mac `precis-infra` container (we want `anki` the backend, **not** `aqt`/Qt),
  and pin a version matching AnkiWeb's current min sync version.
- **Auth flow.** `sync_login(email, password) → hkey`, hkey caching, endpoint
  discovery, re-login on 403 — unspecified. Creds live as a Mac-local secret.
- **Deletion propagation.** When a precis `anki` ref is (soft-)deleted, does the
  tick delete the Anki note too, or leave it? Default lean: leave it (safer;
  precis stops owning it) — but decide explicitly.
- **Tick trigger + prod coupling.** What fires the tick (cron on the Mac stack /
  after an authoring batch / a worker pass)? The Mac stack reads `anki` refs from
  **prod** and holds the mirror **locally** — that split is the single-runner, and
  must be the *only* runner. If the Mac is off, cards simply wait.
- **Two-way edit conflict.** If Reto edits a precis-authored card *inside Anki*
  and precis also edits it, sync is field-level last-write-wins. Acceptable
  (precis authors these), but precis may overwrite an in-Anki tweak — note it.
- **Cloze→search-text rule.** Exact stripping of `{{c1::ans::hint}}` for the
  `card_combined` chunk (keep answer, drop/keep hint?). Low risk; pick and test.
- **Slice-3 mechanism is genuinely fuzzy (by design).** *Where* the retention
  signal injects into retrieval (planner_prompt / web ask-follow-up / draft
  author) and *how* the gap pass defines a "key claim" and matches card coverage
  are research-shaped, not yet specified. Fine for a north star; do not treat as
  buildable until slice 2 lands and the data exists to design against.
- **Honest scope note.** Slice 1 alone authors cards that go nowhere until slice 2
  — the real MVP is **1 + 2**. Slice 1 is shippable, but low-value standalone.

## Target + blast radius

New: `handlers/anki.py`, `migrations/0060_anki_kind.sql`, a Mac-local sync-tick
job + optional `[anki]` extra, `data/skills/precis-anki-help.md`,
`precis_web/routes` browse entry. Retires: `flashcard` KindSpec +
`precis-flashcard-help`. Reuses: `_numeric_ref`, `card_combined`, the embed +
`chunk_keywords` pipeline. External: the `anki` PyPI package (Rust wheel, one
runner), AnkiWeb sync-version coupling, an AnkiWeb credential secret.
