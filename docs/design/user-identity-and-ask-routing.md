# User identity & ask-user routing — design plan

- **Status**: proposed (2026-06-19).
- **Authors**: Reto + agent
- **Motivation**: "reto" is hard-coded as *the* human in several
  places, while the live DB refers to the same person as
  `user:elmsfeuer` and the code's own generalized path calls them
  `owner`. Pick one canonical identity, de-hard-code "reto", and make
  `ask-user` route to a named user instead of an implicit single one.

## 1. Problem

The system has a **single-user assumption** baked in under three
different names for the same human:

| Name | Where | Kind |
|------|-------|------|
| `reto` | `precis_web/ask.py` `ASKER = "reto"`; `web:reto` source + `waiting:reto` in design docs; a bare `reto` tag (1 ref in prod) | hard-coded literal |
| `elmsfeuer` | `user:elmsfeuer` tag in prod (44 refs); a migration comment in `0011_ref_level_decay.sql` | live data convention |
| `owner` | `precis_web/config.py` `DEFAULT_SOURCE = "web:owner"`; `web:*`→owner classification in `_todo_guards`; `meta.owner` (present in schema, **null on all 35 915 prod refs**) | the already-generalized concept |

Meanwhile the bot is `asa` (`user:asa`, 1308 refs; `ANSWERER = "asa"`;
the `asa-*` worker-source convention). `asa` is consistent — it is
only the *human* identity that is fragmented.

Two distinct problems fall out:

1. **Identity fragmentation.** Nothing ties `reto` ≡ `elmsfeuer` ≡
   `owner` together. A routing query on `user:elmsfeuer` misses
   anything stamped `reto`, and vice-versa.
2. **`ask-user` has no addressee.** `ask-user` / `ask-user:<text>`
   means "a human should answer," but not *which* human. Today that
   is fine (one human) — but the `user:<username>` axis already
   exists in the data, so multi-user routing is one binding away.

This doc proposes a canonical identity, the `ask-user`→`user:<who>`
binding, and cleans up the fragmentation. It also folds in the
already-agreed removal of the deprecated `asking-reto` alias.

## 2. Audit — every "reto" instance and its disposition

Generated 2026-06-19 by `grep -rinE '\breto\b'` over the tree +
`ILIKE '%reto%'` over prod tags/actors.

### 2a. Hard-coded in code — **FIX**

| Site | Current | Proposed |
|------|---------|----------|
| `precis_web/ask.py:41` | `ASKER = "reto"` | resolve from config — `cfg.owner` (default below) |
| `precis_web/ask.py:44` | `ANSWERER = "asa"` | leave (bot identity is stable) or move to config for symmetry |

### 2b. The `asking-reto` legacy alias — **REMOVE** (data-safe)

Already a deprecated alias for `ask-user`; prod carries **zero**
`asking-reto` rows, so removal needs no data migration. Sites
(comments / SQL / enum):

- `handlers/_todo_views.py` (≈8 sites: the `("asking-reto","asking-reto:")`
  prefix tuple, the `render_asking_reto` matcher, 3 `OR value LIKE
  'asking-reto:%'` clauses, doc comments)
- `handlers/todo.py` (`TodoView.ASKING_RETO` enum member + its
  dispatch row + the docstring)
- `workers/nursery.py`, `workers/executors/coordinator.py`,
  `workers/executors/_common.py`, `workers/dispatch.py` (comments + 1
  SQL clause in nursery)
- `precis_web/routes/asks.py`, `routes/tasks.py` (prefix-strip +
  matchers)
- `data/skills/precis-tasks-help.md` (the "legacy alias" notes)

Keep `ask-user` / `ask-user:` exactly as-is. Removing `view='asking-reto'`
is a breaking change for any caller still passing it → CHANGELOG note.

### 2c. Design-doc narrative "Reto" — **LEAVE** (historical intent)

`docs/design/todo-tree-plan.md`, `precis-web-build.md`, `storage-v2.md`
use "Reto" as the human actor in prose ("what Reto asked for",
`level:strategic | Reto`). These are point-in-time design records;
rewriting them rewrites history. **Exception:** literal config values
that drifted from the code — the docs say `source='web:reto'` /
`waiting:reto` while the code already uses `web:owner`. Add a one-line
note to those docs pointing at the generalized `web:owner` rather than
editing the narrative.

### 2d. Author attribution — **LEAVE**

`pyproject.toml` `authors = [{ name = "Reto Stamm", email =
"reto@retostamm.com" }]`, `git` Co-Authored trailers, LICENSE. These
are correct and must not change.

### 2e. Prod data — **DECIDE** (see §5)

| Datum | Count | Disposition |
|-------|------:|-------------|
| `user:elmsfeuer` | 44 | the live human identity — candidate canonical name |
| bare `reto` tag | 1 | normalize to `user:<canonical>` or drop |
| `project:reto-nanocompute`, `project:reto-catalyst-latent` | 1 each | project *names*, not identity — leave unless Reto wants a rename |
| `meta.owner` | null × 35 915 | unused; do not start populating without a reason |

## 3. Canonical identity

**Proposal: one configurable owner identity, surfaced as
`user:<owner>`.**

- New config field `PrecisConfig.owner` (env `PRECIS_OWNER`), the
  canonical username for the human running this instance. It feeds:
  - `ask.py` `ASKER` (replaces `"reto"`),
  - the `user:<owner>` tag the web/planner stamps when a todo asks
    "the owner" specifically,
  - any "this is the human" default.
- **Default value.** Recommend `owner` (matches the existing
  `web:owner` source convention and reads correctly on a fresh
  install). Reto's instance sets `PRECIS_OWNER=elmsfeuer` (or whatever
  canonical handle he wants — see open questions) so it lines up with
  the 44 existing `user:elmsfeuer` rows.
- This keeps the single-user case zero-config while making the
  identity *named and overridable* — the prerequisite for multi-user.

Rejected alternatives: hard-coding `elmsfeuer` (same bug, different
string); populating `meta.owner` per-ref (heavy, and routing wants a
tag axis, not a scalar column).

## 4. `ask-user` ↔ `user:<who>` binding

Today `ask-user` is an open pause tag whose **value carries the full
question prose** (verified in prod — 200-word values like "two
blockers on this todo …"). Two changes:

1. **Address the ask.** When something parks a todo on a human, it
   stamps both `ask-user` (the pause marker, unchanged semantics) and
   `user:<who>` (the addressee, defaulting to `cfg.owner`). Routing —
   the Discord/chatter preamble, `view='attention'`, the web Asks page
   — filters by the viewer's `user:<me>`, so each human sees only
   their own asks. With one user this is a no-op; with several it Just
   Works.
2. **Get the prose out of the tag value.** A tag is an index key, not
   a document. The question text should live in a chunk/comment on the
   todo (searchable, embeddable) with the tag reduced to a marker +
   optional short ref. `precis_web/routes/asks.py` already strips the
   `ask-user:` prefix to recover the text — that logic moves to
   reading the chunk instead.

### Registry question

Should `user:<username>` validate against a registry?

- **Today:** open tag, typo-prone (`user:asa`, `user:elmsfeuer`
  coexist with no schema).
- **Option A (recommended, light):** a closed-vocab axis seeded from
  config (`PRECIS_OWNER` + a known bots list incl. `asa`). Rejects
  unknown `user:` values at `tag()` time.
- **Option B (heavier):** extend the `actors` table (today: `agent`,
  `chase`, `system`, `user`) with per-human rows and FK the tag. More
  infra than current needs justify; revisit if real multi-human
  routing lands.

## 5. Migration plan

- **No migration for `asking-reto`** (zero prod rows).
- **`user:elmsfeuer`:** ✅ canonical handle is `elmsfeuer`, so the 44
  rows already match — **nothing to migrate**.
- **Bare `reto` tag (1 row):** a tiny forward migration renames it to
  `user:elmsfeuer` (or just drops it). This is the only DB write the
  whole change needs.
- **`config.owner`** is a new env var; absent → default `owner`. No
  schema change.

## 6. Implementation steps

1. Land this doc; resolve the open questions below.
2. `PrecisConfig.owner` + `PRECIS_OWNER` (config.py + README env table).
3. `ask.py`: `ASKER = cfg.owner`; stamp `user:<owner>` on the parked
   todo.
4. Remove the `asking-reto` alias (all §2b sites) + CHANGELOG note.
5. Route `view='attention'` / Asks page / chatter preamble by
   `user:<me>`.
6. Move question prose from the `ask-user:` value into a chunk;
   update `routes/asks.py` reader.
7. (Optional) closed-vocab `user:` axis (§4 Option A).
8. Data migration for `user:` reconciliation if the canonical handle
   ≠ `elmsfeuer`.
9. Docs: note `web:owner` (not `web:reto`) in the two stale design
   docs; refresh `precis-tasks-help`.
10. Tests: ask-routing filter, alias-removal regression, config default.

## 7. Definition of done

Standard per AGENTS.md (ruff / mypy / pytest, CHANGELOG, version bump).
Plus: a fresh install with no `PRECIS_OWNER` works (`owner` default);
no `\breto\b` left in `src/` except author attribution; `view='asking-reto'`
gone with a CHANGELOG deprecation line.

## 8. Open questions for Reto

1. **Canonical handle.** ✅ **DECIDED (2026-06-19): `elmsfeuer`** —
   matches the 44 live `user:elmsfeuer` rows, so **no data migration**.
   `PRECIS_OWNER` defaults to `owner` on a fresh install; Reto's
   instance sets `PRECIS_OWNER=elmsfeuer`. The bare `reto` tag (1 row)
   normalizes to `user:elmsfeuer`.
2. **`ANSWERER`/bot identity** — leave `asa` hard-coded (it is stable
   and consistent), or move to config for symmetry?
3. **Question prose relocation** — do it in this change (cleaner, more
   work) or defer and keep the prose-in-tag-value smell for now?
4. **Registry** — open `user:` tag (status quo) vs the closed-vocab
   axis (§4 Option A) — now or later?
5. **`project:reto-*` slugs** — leave as project names, or rename for
   consistency?
