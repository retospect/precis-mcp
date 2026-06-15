# precis-web: paper verification links, multi-root corpus, conv transcript

- **Status**: implemented
- **Builds on**: ADR 0026 (precis-web surface),
  `docs/design/precis-web-refs-and-filters.md` (per-kind browsers +
  PDF diagnostics), ADR 0029 (multi-root corpus PDF resolution)

## Problem

Three operator-reported gaps in the read surfaces of precis-web:

1. **Papers list gave no way to verify a paper at a glance.** The
   hover card showed authors + abstract but no DOI / arXiv link, so
   confirming "is this the paper I think it is?" meant leaving the UI.
   The abstract backfill also picked the first ≥200-char chunk, which
   is often a long author/affiliation block rather than the abstract.
2. **The PDF viewer 404'd for held papers** whose file lived under a
   corpus root the web wasn't configured for. On the cluster the same
   NFS share is mounted at different paths per host
   (`/opt/shared/corpus` vs `/opt/nas/botshome/papers/corpus`), so a
   single `PRECIS_CORPUS_DIR` was wrong for whichever host didn't
   match. (See ADR 0029.)
3. **Clicking a conversation dumped the agent card.** The conv detail
   view rendered the handler's `get` overview — the LLM-facing card
   with `Next: {if you want to execute this call}` affordances — into
   a `<pre>`. A person reading a thread wants the turns.

## Decisions

### Papers hover card — DOI / arXiv links + sharper abstract

- New batched `Store.identifiers_for_refs(ref_ids) -> {ref_id:
  {scheme: value}}` (one query over `ref_identifiers` for the whole
  page; first value per scheme wins). The papers route builds
  `doi_url` (`https://doi.org/<doi>`) and `arxiv_url`
  (`https://arxiv.org/abs/<id>`) per row.
- The hover card footer and the detail header render the links.
  Inside the card (which is `pointer-events-none` so it doesn't block
  the row click) the links are wrapped in a `pointer-events-auto`
  block so they stay clickable.
- `Store.abstract_previews` now prefers an **explicit abstract
  chunk** — matched by `section_path` containing `"abstract"` or a
  leading `Abstract` label, which is stripped — before falling back
  to the first substantial paragraph, then the longest leading chunk.
  Selection is a pure helper (`_pick_abstract_text`) so it unit-tests
  without a DB. `section_path` is a `TEXT[]`; it is flattened to a
  string before the marker check.

### Multi-root corpus PDF resolution

`PRECIS_CORPUS_DIR` accepts an `os.pathsep`-separated list of roots.
`WebConfig` keeps `corpus_dir` (primary, back-compat) plus
`extra_corpus_dirs`, exposing `corpus_dirs` (primary first). The
papers route's `_resolve_pdf` tries each `<root>/<letter>/<cite_key>.pdf`
in order and serves the first that exists; the "file not found"
diagnostics list **every** path tried and the Status tab lists all
roots. Single-path configs are unchanged. Rationale and alternatives
in ADR 0029.

### Conversation transcript view

The refs detail route special-cases `kind == "conv"`: instead of
dispatching `get` (which yields the agent card), it reads the turn
chunks via `store.list_blocks_for_ref(ref_id)` and renders a
chat-style transcript through a dedicated `refs/conv_detail.html.j2` —
per-turn author (with a deterministic colour dot keyed on the author
name), timestamp (`_fmt_turn_ts` tolerates ISO strings or datetimes),
a `~N` anchor, and the body. A turn count sits in the header. Other
ref kinds keep rendering the handler's `get` output read-only.

This stays inside the precis-web layer: the MCP `get(kind='conv', …)`
surface (overview / `/transcript` / `~N`) is untouched for agents —
the web just chooses a human rendering for the same underlying
chunks.

## Hardening

The paper-detail "held but missing" diagnostics block now guards its
list/`join` filters with `| default([], true)` so a route↔template
version skew during a not-yet-restarted deploy degrades gracefully
(empty diagnostics) instead of raising Jinja `UndefinedError` (500).
`Jinja2Templates` hot-reloads templates per request but the Python
route module is loaded once at process start, so renaming context keys
(`corpus_dir`→`corpus_dirs`) can briefly skew until the web process is
restarted.

## Tests

- `tests/test_abstract_preview.py` — the pure abstract picker
  (explicit-abstract preference, label stripping, fallbacks).
- `tests/precis_web/test_routes.py` — DOI/arXiv links in the hover
  card and detail; multi-root `WebConfig` env parsing; PDF resolving
  from a second root; detail listing all searched roots; conv detail
  rendering turns (not the agent card) with a turn count.

## Deployment note

The cluster `precis_web` role sets `PRECIS_CORPUS_DIR` to
`papers_corpus_path` (the canonical NAS path the watcher writes to)
with the legacy `/opt/shared/corpus` as an `os.pathsep` fallback —
keeping web and watcher single-sourced. The role installs precis-mcp
from `git+…@main`, so this code must be on `main` before the play
runs; the play upgrades the package and restarts the daemon in one
run.
