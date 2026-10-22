---
status: ready
pillar: platform
---
# Drive slash command line

Reto, 2026-10-12 (from gr480070): typing `/` in the /drive search box (and the
global loupe in `base.html.j2`, which submits to /drive) opens a popdown that
narrows as you type, `/d` → drive, drafts, datasheets…; then grows into
expressions like `/draft:nanobuds` → its papers, "papers this draft cites that
are stubs and need downloading", `/pathway:no->nh3/pareto`. Discoverability is
the popdown itself: every stage completes from what exists.

## Design rule: the address grammar, not a new language

An expression is the precis address agents already use (`kind:id~selector`,
`view=`, path ids like `/recent`), so one language serves both:

- `/draft:nanobuds` = `get(kind='draft', id='nanobuds')`
- `/draft:nanobuds/fisheye` = the same with `view='fisheye'`
- `/pathway:no->nh3/pareto` — `no->nh3` is a query, not a slug: try id first,
  fall back to search, and show which happened (`kind:?<query>` forces search).

Completions come from the live registry (kinds, each kind's views, ids via
search), so the palette cannot drift from the surface. The expression is the
URL, so every result is bookmarkable and shareable.

## Stages (pipe-separated)

`/draft:nanobuds | cites paper | is:stub`

| Stage | Examples | Source of truth |
|---|---|---|
| address | `draft:nanobuds`, `paper:wang2020state~40` | address grammar (`precis-addressing-help`) |
| traverse | `cites paper`, `part-of`, `refines` | link relations (`precis-relations`) |
| filter | `is:stub`, `has:body`, `tag:PRIO:high`, `since:7d` | GitHub-style qualifiers |
| view | `/recent`, `/pareto`, `/toc` | the kind's view roster |

`|` rather than space: ids and queries contain spaces. The popdown inserts the
pipe, so it is never typed blind.

`is:stub` / `has:body` replace the asked-for `/papers:withchunks` /
`:withoutchunks` and work on any chunked kind. `is:stub` counts
`body:partial` and `body:needs_ocr` papers as stubs (ingest already treats
them as no usable body).

## Actions stay out of the grammar

The language is read-only. Spending/writing operations ("fetch these 12
stubs") are buttons on the result list with a cost line and confirmation, so a
shared or bookmarked URL can never trigger a download or a paid fetcher.

## Shape

One server-side parser → stage list → executed by existing handlers (no new
query engine). The popdown calls a completion endpoint with the text and
cursor position. Reuse the Drive kind/filter picker, chips and URL facets
(`src/precis_web/__init__.py`, `drive-filter-hierarchy.md`) rather than a
second picker. A later MCP form (`search(q='| …')`) can share the parser.

## Slices

1. Palette: `/` + kinds, ids, views; navigation only.
2. Qualifiers: `is:stub`, `has:body`, `tag:`, `since:`.
3. Pipes: traverse stages over link relations.
4. Result-list actions, first: fetch stubs, with cost line.

## Slice 2 — qualifiers (ready 2026-10-21, landed 2026-10-21)

`status: ready` covers this slice only; slices 3 and 4 stay draft.

Already on main (slice 1 is mostly shipped): `GET /drive/complete`
(gr486407), the leading-`/kind` peel `routes/drive.py::_split_kind_tokens`
with `/paper+chunks` (gr498598), the Tab-complete in `base.html.j2`
(gr500642). There is no pipe parser yet, so slice 2 extends the existing
peel and maps each qualifier onto a facet `index()` already accepts. No
new query engine, no migration.

1. **Grammar.** A qualifier is a bare whitespace-separated word (no
   leading `/`) matching `is:stub`, `has:body`, `tag:<value>` or
   `since:<N><d|h|w>`, anywhere in the free text. Recognised qualifiers
   are removed from the free text before search. A word with one of these
   prefixes but an unrecognised value (`is:foo`, `since:soon`) stays in the
   free text and the page shows one notice naming it. `/tag:<id>` (leading
   slash) stays the address of a `tag` ref; only the bare form is a
   qualifier.
2. **Mapping.**
   - `has:body` → `paper_chunks=with`; `is:stub` → `paper_chunks=without`.
     `/paper+chunks` stays as sugar for `/paper has:body`.
   - The `paper_chunks` predicate also honours
     `store/_body_predicate.py::UNUSABLE_BODY_TAGS`: `without` includes
     refs tagged `body:partial`/`body:needs_ocr`, `with` excludes them.
     This changes the existing selector the same way (intended: those
     tags already mean "no usable body").
   - `tag:<value>` → one `tag=` facet; split on the first colon only, so
     `tag:PRIO:high` is the tag `PRIO:high`. Repeatable.
   - `since:<N><unit>` → `since=` set to now (UTC) minus N days/hours/weeks,
     on the same timestamp the `since=` facet already bounds.
3. **`//` escape.** A query starting with `//` skips the peel and searches
   the literal text with one leading slash stripped (`//etc/hosts` searches
   `/etc/hosts`). The parser does it; the client Tab-complete ignores a
   `//` prefix.
4. **Completions.** `/drive/complete` offers the four qualifier prefixes
   after the kind tokens, and `tag:` values from the tag roster.
   `_SLASH_TOKEN_RE` is unchanged (qualifiers carry no leading `/`).

Acceptance (tests in the drive route tests):

- `/paper is:stub` returns only papers with no body chunk or tagged
  `body:partial`/`body:needs_ocr`; `/paper has:body` is the complement;
  the free text passed to search contains neither qualifier.
- `tag:PRIO:high nano` filters on tag `PRIO:high` and searches `nano`.
- `since:7d` sets `since=` to 7 days before now (UTC, frozen clock in the
  test); `since:soon` stays in the free text with the notice.
- `//foo` searches `/foo` and peels no kind; `/tag:x` is not a qualifier.
- `/paper+chunks` still behaves as before.

## Decided

- `//` strips one slash and searches the rest literally (default taken
  2026-10-21, see slice 2 §3; Reto may overrule).
- Traversal (slice 3) uses relation slugs verbatim from the live
  `relations` table, inverses included (`cites`/`cited-by`,
  `contains`/`part-of`); direction is the slug's. No alias table: none
  exists in code and none is needed. Friendlier names, if wanted, are a
  separate item.

### Ready-check findings (2026-10-21, ready agent; slice 2 + `//` / relation-name focus)

- blocker: Open question "leading `//` escapes a literal search — confirm" is still open; also ambiguous whether `//foo` searches `foo` or `/foo`. Today (`routes/drive.py::_split_kind_tokens`) an unknown `/word` already stops the scan and stays literal, so `//foo` already searches the literal `//foo`; spec must state the intended change (strip one slash?) and who owns it (parser vs. client in `base.html.j2`).
- blocker: Open question "raw relation vs friendlier aliases" is unresolved and there is no alias table anywhere in code (`store/types.py::_INVERSE_RELATIONS` and the DB `relations` table carry only canonical slugs + `inverse_slug`; skill `precis-relations` lists no aliases). Aliases would be a new invented registry; spec must pick raw slugs (recommended) or define the table.
- blocker: spec claims "Completions come from the live registry" but names no accessor for the relation list (DB `relations` table via Store; plugin relations exist), and traversal direction is unspecified: `cites paper` = outbound only? `cited-by`/`contains`/`part-of` are separate slugs (inverses auto-mirror, `contains`/`part-of` one tree; `related-to`/`see-also`/`disputes`/`refines` have no inverse). State: slugs verbatim incl. inverses, direction implied by the slug.
- blocker: Spec ignores shipped work: `/drive/complete` (gr486407), `_split_kind_tokens` with `/paper+chunks` (gr498598, already the "papers with chunks" filter this spec says `has:body` replaces), the Tab-complete in `base.html.j2` (gr500642) and the `/fav` redirect route. Slice 1 is largely done; slices must say what happens to `/paper+chunks` (keep as alias of `/paper | has:body`? remove?) and that qualifiers must not break `_SLASH_TOKEN_RE` (token chars exclude `:`/`|`/`~`/`>`).
- blocker: Slice 2 acceptance criteria missing for the whole spec (no per-slice "done"). Needed e.g.: `/paper is:stub` returns only refs with no body chunk (`has_body_sql`) or carrying `body:partial`/`body:needs_ocr` (`store/_body_predicate.py::UNUSABLE_BODY_TAGS`); `has:body` is its complement; `since:7d` unit grammar and which timestamp (created/updated) undefined; `tag:PRIO:high` splitting on first colon only.
- advisory: Command-word collision check: no relation slug equals a kind name (kinds like `part`, `draft`, `paper` vs `part-of`, `draft-of`), but `/fav` is a real route not a kind, and a future kind named like a stage word (`is`, `has`, `tag`, `since`) would collide; `tag` IS a kind (`kind='tag'`), so `tag:PRIO:high` vs address `tag:<id>` is ambiguous in an address-grammar design. Spec should say qualifiers are only recognised after a `|`.
- advisory: Relation claims verified: `cites`, `refines`, `part-of` exist (`part-of` is the inverse slug of `contains`; the skill notes SI use only). `is:stub` definition matches `UNUSABLE_BODY_TAGS`.
- advisory: Bundles four slices (palette, qualifiers, pipes, actions); slice 4 reaches into fetch/spend paths (`/drive/requeue-stubs`, `fetch-next-batch`). Split warranted: slice 1+2 (parser + completions + qualifiers) vs slice 3 (traversal) vs slice 4 (actions, own spec). Slice 3 should block on 2; 4 is independent.
- advisory: Model tier: parser/grammar design reads as Opus-grade; status is draft with no `model:` declared.
- **Resolved 2026-10-21 (readychecks):** the five blockers are resolved in §Slice 2 and §Decided (slice 2 restated as the delta from shipped code; qualifiers are bare words, so no pipe and no `tag` address collision; defaults taken for `//` and `since:`, Reto may overrule). The split advice stands for slices 3–4.
