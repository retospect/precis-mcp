---
status: plan — hard-cutover, no deprecation window; critic prereqs fixed on main
applies-to: MCP tool surface (server.py) + every handler + every skill
supersedes: nothing; replaces the four-verb surface from precis-overview
last-updated: 2026-05-01
---

# Seven-verb surface migration plan

Rewire the published MCP tool surface from
`get / search / put / move` (4 verbs, `put` polymorphic over 6 modes) to
`get / search / put / edit / delete / tag / link` (7 verbs, each with a
narrow, schema-describable shape).

The guts — handlers, store, parsers, validation gates — do not change.
Only the dispatch layer in `src/precis/server.py` and the advertised
schemas are rewired. Skills are updated to match.

## TL;DR

```
get     read (ref, file, view, stateless tool)     — unchanged
search  find by content                            — unchanged
put     create or fully replace a ref / file       — narrowed
edit    partial content modify                     — NEW verb
delete  remove ref / block / symbol                — NEW verb
tag     label add/remove on existing ref           — NEW verb
link    relate two refs                            — NEW verb
move    (retired)                                  — gone
```

**Hard cutover in one release.** No alias window, no `put(mode=X)`
aliases, no deprecation phase. LLM agents read `tools/list` fresh
every session; there is no cross-session habit to preserve. The
new surface replaces the old one outright. Human-authored clients
get one migration pass when they next update.

## Why this change

From the MCP critique 2026-05-01 (`mcp-critic.md`):

- `put` carries 6 modes with mode-conditional required params — the
  JSON Schema for `put` can't declare required fields honestly.
  Finding MAJOR-C B3.
- `move` is advertised but reserved for unwired kinds — documentation
  fossil. Finding MAJOR-C B1.
- Small models pick between verb names more reliably than between
  mode strings of the same verb. Rule E1.
- Pre-training priors favor the 7-verb shape (REST PUT/PATCH/DELETE,
  SQL INSERT/UPDATE/DELETE, git tag, graph APIs for `link`).

The change is mechanical: rename + dispatch split. No semantics
change at the handler layer.

## Decisions (locked)

These are the open questions from design discussion, answered here so
the implementation steps below have no guess-work.

### D1 — `tag` verb kwarg shape

```python
tag(kind: str, id: str|int, add: list[str] = [], remove: list[str] = [])
```

- `add=` and `remove=` are independent lists. A single call can do
  both (atomic).
- Closed UPPERCASE axes (`PRIO`, `STATUS`, `SRC`, `CACHE`) keep
  replace-within-prefix semantics when in `add=`. Adding
  `STATUS:done` implicitly removes any other `STATUS:*` on the ref.
  No change from today's `put(tags=[...])` semantics.
- `remove=` is value-matched for closed prefixes (same as `untags=`).
- Empty call (`tag(kind, id)` with no `add` / `remove`) is a no-op,
  not an error. Returns current tag set.

Alternatives considered and rejected:

- `tag(..., set=[...])`: destructive replace of the whole tag set.
  Too easy to nuke unintended tags. Use `add=` + `remove=` explicitly.
- `tag(..., tags=[...], untags=[...])`: mirrors today's `put`. The
  whole point of the refactor is to leave that shape behind.

### D2 — `link` verb kwarg shape

```python
link(kind: str, id: str|int,
     target: str,                # canonical 'kind:id[~selector]'
     rel: str = 'related-to',
     mode: str = 'add')          # 'add' | 'remove'
```

- `target` uses the existing canonical form, documented in
  `precis-relations` (e.g. `paper:wang2020~38`, `memory:47`).
- `rel` defaults to `'related-to'`. Required when adding any
  non-default relation.
- `mode='remove'` without `rel=` removes every link to `target`
  (matches today's `unlink=` semantics).
- `mode='remove'` with `rel=` removes only the (target, relation)
  pair.

Alternatives considered and rejected:

- Separate `unlink` verb. Doubles the surface for an operation with
  ~5% of link-call traffic. Not worth the eighth tool schema.
- `link(target=..., add=True|False)`: booleans read worse than
  `mode='add'|'remove'` on inspection.

### D3 — Create-with-initial-labels shortcut stays on `put`

```python
put(kind='todo', text='Review section 3.', tags=['PRIO:high'])
```

- `tags=` accepted on `put` only during creation.
- `link=` similarly accepted on `put` at creation.
- `untags=` / `unlink=` / `rel=` **not** accepted on `put` —
  metadata removal always goes through `tag` / `link`.
- After creation, any tag or link change goes through the dedicated
  verb. Attempting `put(id=<existing>, tags=[...])` with an existing
  id is a `BadInput` that names the `tag` verb.

Rationale: initial-state labels are the single most common mixed-shape
call and splitting them forces an extra round-trip. Everything else
splits cleanly.

### D4 — Hard cutover, zero backward compatibility

One release. The old surface is deleted outright:

- `put(mode='edit'|'insert'|'append'|'delete')` — removed from the
  handler dispatch. The `put` verb no longer accepts these modes at
  all.
- `put(tags=[...])` / `put(link=...)` on an existing id — the
  `tags=` / `link=` kwargs on `put` are accepted **only** when
  the call is a create. On an existing id they raise `BadInput`
  pointing at `tag` / `link`.
- `move(...)` — verb entry removed from `server.py`. Clients that
  still call it get the transport-layer 'method not found' error.
- `put(mode='replace')` with a selector in the id — `BadInput`
  pointing at `edit(mode='replace', ...)`. Whole-file replace
  (no selector) stays on `put`.

Rationale: LLM agents have no cross-session memory. On the next
restart, they read `tools/list`, see the new surface, and use it.
Aliases would only preserve the confused shape for human-authored
clients, at the cost of bloating every response and every
schema with legacy paths.

### D5 — Reorder (structured-file rearrangement) is `edit(mode='reorder')`

- No dedicated `move` or `reorder` verb.
- Signature deferred to when the first structured-file kind ships
  (`docx` or `tex`). Probable shape:
  `edit(kind='tex', id=..., mode='reorder', target='~<anchor>', where='before'|'after')`.
- `move` verb is retired entirely — spec footprint goes to zero,
  not reserved-for-later.

Rationale: reorder is a content-shape change; it belongs in the same
verb family as `insert` and `replace`. Separating it would be two
verbs for one layer.

### D6 — Per-kind capability matrix

Read row-by-row: which kinds accept which verbs after migration.
"—" = not exposed for this kind. "initial" = accepted only at creation.

| Kind     | get | search | put | edit | delete | tag | link |
|----------|-----|--------|-----|------|--------|-----|------|
| calc     | ✓   | —      | —   | —    | —      | —   | —    |
| conv     | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| fc       | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| gripe    | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| math     | ✓   | —      | —   | —    | —      | —   | —    |
| memory   | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| oracle   | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| paper    | ✓   | ✓      | ingest | —  | —      | ✓   | ✓    |
| python   | ✓   | ✓      | ✓   | ✓    | ✓      | —   | —    |
| quest    | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| research | ✓   | —      | import | —  | —      | —   | —    |
| skill    | ✓   | ✓      | —   | —    | —      | —   | —    |
| think    | ✓   | —      | import | —  | —      | —   | —    |
| todo     | ✓   | ✓      | ✓   | —    | soft   | ✓   | ✓    |
| web      | ✓   | —      | —   | —    | —      | —   | —    |
| websearch| ✓   | —      | import | —  | —      | —   | —    |
| youtube  | ✓   | —      | —   | —    | —      | —   | —    |

When `markdown` / `plaintext` are (re-)wired, row shape matches
`python`.

Unsupported verb/kind combinations raise the existing
`Unsupported` error with a pointer to the right verb. No silent
no-ops.

### D7 — Registration: constructor registers, one phase

Each handler registers itself in its `__init__`, as the last step
after all validation. No separate `try_init` classmethod, no
`register()` follow-up, no `post_register(registry)` hook, no
decorator-scan. Construction **is** registration.

```python
class Python:
    def __init__(self, r: Registry, env: Env, fh: FileReader):
        # 1. validate deps, load config, build indexes. May raise InitError.
        self.fh = fh
        self.roots = _load_roots(env)
        if not self.roots:
            raise InitError("python: no roots configured")
        self.index = PythonIndex(fh, self.roots)

        # 2. register LAST, after everything is ready
        r.register_ability("python", "get",    None,      self.get)
        r.register_ability("python", "put",    "create",  self.put_create)
        r.register_ability("python", "put",    "replace", self.put_replace)
        r.register_ability("python", "edit",   None,      self.edit)
        r.register_ability("python", "delete", None,      self.delete)
        r.register_skill("precis-python-help", _PY_HELP_MD)
        r.register_overview("python", "Python source files with AST + qualname graph.")
```

Composition root in `src/precis/registry.py`:

```python
def boot(env: Env) -> Registry:
    r = Registry()

    fh = _try(FileReader, r, env)
    py = _try(Python,   r, env, fh) if fh else None
    md = _try(Markdown, r, env, fh) if fh else None
    em = _try(Embedder, r, env)
    ss = _try(SearchService, r, env, em) if em else None
    # ... one line per handler, ordered by dependency

    return r


def _try(cls, *args, **kw):
    try:
        return cls(*args, **kw)
    except InitError as exc:
        log.warning("%s init failed: %s", cls.__name__, exc)
        return None
```

**Invariant: register as the last statement of `__init__`.** If a
constructor registers and then fails, the registry ends up with
pointers into a half-constructed object. Register-last makes this
free — a raise before the registration block leaves the registry
untouched and `_try` swallows the exception.

**Failure mode is uniform.** Any handler whose `__init__` raises
`InitError` (or whose required dep is `None`) is silently absent
from the registry. The kind never appears in `tools/list`, in
`precis-help`, or in dispatch. WARN logs are operator-facing only.

**Dependencies are constructor args.** No hidden lookups, no
"fetch filereader from registry during init". If the shape reads
`Python(r, env, fh)`, python needs a registry, env, and
filereader — and can't be constructed otherwise. Circular deps are
avoided by construction; if one ever appears, pass a lazy
`lambda: r.kinds["other"]` — but precis' graph is shallow enough
that hand-ordering the boot loop is the whole plan.

Alternatives considered and rejected:

- **`try_init(config) -> Handler | InitError` classmethod + separate
  `register(r)` method.** Two phases where one suffices. A
  constructed-but-unregistered handler is dead weight — there's no
  legitimate state where that's useful.
- **Decorator-scan (`@verb('get')`, `@mode('create')`) populating
  the dispatch table.** Magic, hard to grep, classic "why isn't my
  verb registered?" bug class.
- **`post_register(registry)` cross-handler hook.** No real
  requirement. Cross-cutting views (overview, skill index,
  `precis-help` kind list) are assembled by the registry iterating
  its own state after boot — that's a registry method, not a
  per-handler hook.
- **Topological sort of handler classes.** Overkill for precis'
  shallow dep graph. Hand-order the composition root; add topo
  sort later only if a real DAG emerges.

#### Handler-author contract (paste into the docstring of the handler base module)

> Handler `__init__` must do all validation before its first
> `register_ability` / `register_skill` / `register_overview` call.
> If any validation fails, raise `InitError(reason)` with a short
> actionable message. The boot loop catches `InitError`, logs WARN,
> and leaves the kind absent from the dispatch surface. Any other
> exception propagates — it's a bug, not a missing dep, and should
> crash boot so it gets noticed. Register as the last block of
> `__init__`; once any `register_*` call has run, the instance is
> committed to the registry.

**Failure-mode semantics.** A handler's `__init__` has exactly two
exit paths:

- *Returns normally.* Instance exists, fully wired, every ability
  in the dispatch table points at a working bound method. Kind is
  live on the LLM surface.
- *Raises `InitError`.* No instance created (Python never binds a
  name on a raising `__init__`). Registry state untouched because
  registration is the last block. Kind invisible: absent from
  `tools/list` dispatch, from `precis-help`, from search
  suggestions. Operator sees one WARN line naming the reason.

Rejected alternative: "return self with `status='failed'`". Python
`__init__` returns `None`, so the "returns self" framing is a
category error — you'd need a factory classmethod. Even with that,
a broken-but-instantiated handler forces every caller to check
status, defeats the binary contract, and invites partial
registration bugs where a future refactor adds validation *after*
the first `register_*` call. Raise + catch is both simpler and
more robust.

### D8 — Dispatch: flat table, O(1) lookup

Registry owns `abilities: dict[(kind, verb, mode | None), Callable]`.
Every MCP tool function in `server.py` does one lookup and one call:

```python
@mcp.tool
def edit(kind, id, mode='find-replace', **kw) -> Response:
    fn = r.abilities.get((kind, 'edit', mode))
    if fn is None:
        return _dispatch_error(r, kind, 'edit', mode)
    return fn(id=id, **kw)
```

That's the whole `server.py` pattern. Seven copies of it, one per
verb. No per-kind switch statements, no reflection on handler
method names, no decorator introspection.

Miss handling is typed and uniform:

- **Unknown kind** → `Unsupported: kind '<K>' is not registered`.
  Hint: `get(kind='skill', id='precis-help')` to see active kinds.
- **Kind registered, verb unsupported** →
  `Unsupported: kind '<K>' does not support <verb>`. Hint lists the
  verbs the kind *does* support, read from `r.abilities` live.
- **Verb supported, mode invalid** →
  `BadInput: <verb> mode '<M>' is invalid for kind '<K>'`. Hint
  lists the valid modes for that (kind, verb) pair.

All three hints are built by the registry reading its own live
state — they can never drift from the actual dispatch table.

Alternatives considered and rejected:

- **Per-kind sub-methods on the server (`_edit_python`,
  `_edit_markdown`).** Grows linearly with kinds; breaks the flat
  surface that was the point of the migration.
- **Omnibus `dispatch(request: dict)` with one tool.** Loses
  per-verb JSON Schema honesty; the LLM can't see verb-specific
  required fields.
- **Two-level lookup (`kinds[K].abilities[(verb, mode)]`).** Same
  asymptotic cost, more indirection, and the flat table makes
  cross-kind queries ("who supports `tag`?") a single pass.

## Target signatures

Final published schemas. Use these as the authoritative reference for
schema generation in `server.py`.

### `get`

```python
get(kind: str,
    id: str | int | None = None,
    view: str | None = None,
    q: str | None = None,
    args: dict[str, Any] | None = None) -> Response
```

Unchanged from today.

### `search`

```python
search(q: str,
       kind: str | None = None,
       scope: str | None = None,
       tags: list[str] | None = None,
       top_k: int = 20) -> Response
```

Unchanged from today.

### `put`

```python
put(kind: str,
    id: str | int | None = None,    # None for numeric-ref create
    text: str | None = None,        # required for most paths
    tags: list[str] | None = None,  # initial labels only
    link: str | None = None,        # initial graph edge only
    rel: str = 'related-to',        # paired with link= on create
    mode: str = 'create') -> Response
```

Modes accepted:

- `create` — default; new ref / new file.
- `replace` — whole-file replace (file kinds, no selector in `id`).

Any selector in `id=` on `put` → `BadInput: selector on put means
region-replace; use edit(kind=K, id=<id-with-selector>, mode='replace', ...)`.
That disambiguates whole-file replace from region replace cleanly.

### `edit`

```python
edit(kind: str,
     id: str | int,                 # required — target must exist
     mode: str = 'find-replace',    # default: anchored find/replace
     text: str | None = None,
     find: str | None = None,       # required for find-replace
     before: str | None = None,
     after: str | None = None,
     where: str | None = None,      # 'before' | 'after' for insert
     match: str = 'unique',         # 'unique' | 'first' | 'all' | 'nth'
     nth: int | None = None,
     allow_rename: bool = False,
     dry_run: bool | str = False) -> Response
```

Modes accepted:

- `find-replace` (default) — anchored find-and-replace within `id`'s
  resolved region. `find=` required. Today's `put(mode='edit')`.
- `append` — append `text=` to the end of the addressed region /
  file. Today's `put(mode='append')`.
- `replace` — replace the whole region named by `id`'s selector with
  `text=`. Today's `put(mode='replace')` with selector.
- `insert` — insert `text=` adjacent to the anchor `find=`. `where=`
  required. Today's `put(mode='insert')`.
- `reorder` — deferred; shape locked when first structured-file
  kind ships (see D5).

`mode='find-replace'` is the default because it's the highest-value
new primitive and the one agents most need to discover. Other modes
must name themselves.

### `delete`

```python
delete(kind: str, id: str | int) -> Response
```

Behavior depends on kind + selector, same as today's
`put(mode='delete')`:

- Numeric-ref kinds (memory, todo, gripe, fc, quest, oracle, conv):
  soft-delete the ref.
- File kinds with a selector (`python`, `markdown`, `plaintext`):
  delete the addressed block / symbol / line range.
- File kinds without a selector: `BadInput: whole-file delete is out
  of scope; use OS tools`.
- Non-deletable kinds (skill, calc, math, web, youtube, research,
  think, websearch, paper): `Unsupported: kind '<K>' is not
  deletable` — one generic error message, no per-kind nuance.

### `tag`

```python
tag(kind: str, id: str | int,
    add: list[str] | None = None,
    remove: list[str] | None = None) -> Response
```

See D1 for semantics. Response body is minimal:

```
tagged memory:47

Next:
  get(kind='memory', id=47)     — see current tags
```

No dump of the full tag set; no diff; no affirmation of what
changed. Callers who want to inspect state fetch it.

### `link`

```python
link(kind: str, id: str | int,
     target: str,
     rel: str = 'related-to',
     mode: str = 'add') -> Response
```

See D2 for semantics. Response body is minimal, matching `tag`:

```
linked memory:47 --[cites]--> paper:wang2020~38

Next:
  get(kind='memory', id=47)     — see all links
```

One-line confirmation + one follow-up.

## Implementation phases

### Phase 0 — critic prereqs on main (status: ✅ landed)

Three bugs from `grimoire/agents/mcp-critic.md` blocked the new
surface. Fixed on main in the commit that preceded the migration
branch:

- **CRITICAL-C B2** — `_reject_doubled_root_prefix` helper added to
  `handlers/python.py`. Rejects ids whose relative path re-enters
  the root's tail segments (e.g. `precis/src/precis/foo.py` when
  root is `.../src/precis`). Regression test:
  `test_python_handler_writes.py::test_put_create_rejects_doubled_root_prefix`.
- **MAJOR-C F1** — `_module_qualname_for` in `handlers/python.py`
  now delegates to the indexer's `_qualname_for_file`. The old
  in-file reimplementation stopped walking at `repo_root` and
  produced qualnames shorter than the indexer's whenever the repo
  root was itself a package. Regression test:
  `test_python_handler_writes.py::test_replace_identical_body_under_package_root_is_noop`.
- **MAJOR-C D6** — resolved as a side-effect of B2. The outline
  hint's code was always correct; its rendered qualnames were wrong
  because files ended up at bogus locations where the indexer
  truncated qualnames. With B2 blocking the bogus creates, the
  hint form and the `put` resolver agree again.

Tag these fixes as `6.0.1` and ship before opening the surface-
migration branch.

### Phase 1 — registry refactor + new surface (release 6.1)

No alias window. The registry refactor, the new verbs, and the
deletion of the old shape all land in the same commit.

1. **Registry + boot (D7/D8 infrastructure).** Net-new code in
   `src/precis/registry.py`:
   - `Registry` class with `abilities: dict[(kind, verb, mode),
     Callable]`, `skills: dict[str, str]`, `overview: dict[str,
     str]`, `kinds: set[str]`. Methods: `register_ability`,
     `register_skill`, `register_overview`. Duplicate registration
     raises — wiring bugs fail loudly at boot, not at first call.
   - `boot(env: Env) -> Registry` composition root. Constructs
     handlers in hand-ordered dependency order, wrapped in `_try`
     so `InitError` silently drops the kind from the surface.
   - `_try(cls, *args, **kw)` helper that catches `InitError`,
     logs at WARN, returns `None`.

2. **Handler migration.** Every handler class (17 today) moves to
   the constructor-registers shape:
   - `__init__(self, r: Registry, env: Env, *deps)` signature.
   - Validation, index build, config load — all before the
     registration block.
   - Registration is the last statement of `__init__`. Each ability
     points at a bound method; no trampolines, no decorators.
   - Any class-level `KindSpec` / `supports` / per-handler
     registration plumbing is deleted.
   - Skill text and overview blurb move from separate files into
     `register_skill(...)` / `register_overview(...)` calls on the
     handler (skill markdown stays on disk; the handler just
     references and registers it).

3. **Server.py dispatch migration.** Every `@mcp.tool` function
   becomes three lines:

   ```python
   @mcp.tool
   def edit(kind, id, mode='find-replace', **kw) -> Response:
       fn = r.abilities.get((kind, 'edit', mode))
       if fn is None:
           return _dispatch_error(r, kind, 'edit', mode)
       return fn(id=id, **kw)
   ```

   - Register four new `@mcp.tool` functions: `edit`, `delete`,
     `tag`, `link`. Each does one `r.abilities.get(...)` lookup.
   - Narrow `put`'s schema and dispatch to only `create` and whole-
     file `replace` modes. Any other `mode=` → `BadInput` naming
     the verb the caller should have used.
   - Narrow `put`'s kwargs: `tags=` / `link=` accepted only on
     create (no existing id). On existing ids → `BadInput`
     pointing at `tag` / `link`.
   - Remove `move` verb registration entirely.
   - `_dispatch_error(r, kind, verb, mode)` reads the live
     registry to build the three miss-message shapes from D8.

4. **Edit-protocol support.** `edit` verb exposes `find`, `before`,
   `after`, `where`, `match`, `nth`, `allow_rename`, `dry_run` as
   top-level JSON Schema properties. Closes finding B3 from the
   critic report.

5. **Per-kind unsupported errors.** Handled generically by the
   dispatch layer reading `r.abilities`, per D8 — no per-kind
   switch statements. The capability matrix (D6) is an assertion
   target for tests, not runtime code.

6. **Tests added in `tests/test_seven_verb_surface/`:**
   - One file per new verb. Assert dispatch correctness and
     unsupported-kind error shape.
   - `tests/test_registry.py` — boot ordering, `InitError`
     swallowing, duplicate-register fails loud, missing-dep drops
     kind silently.
   - `tests/test_schema.py::test_each_verb_has_flat_schema` —
     asserts every verb's inputSchema is flat (no `if/then`
     conditional required fields).
   - `tests/test_dispatch.py::test_unknown_kind_suggests_precis_help`
     — the three miss-message shapes from D8.

7. **Exit criterion**: full suite passes; `tools/list` shows 7
   tools; `move` gone; `put(mode='edit')` raises `BadInput`
   pointing at `edit`; `r.abilities` populated for every row of the
   D6 capability matrix that reads "✓".

8. Skills ship in the same commit (see Phase 2). A release that
   removes the old surface but still documents it in skills is
   worse than no release.

### Phase 2 — skill migration (same release as phase 1)

Every skill reflects the new surface. Ships with phase 1 — a release
that removes the old surface but still documents it would be worse
than no release.

No deprecation notices. No "previously this was" callouts. The new
surface is the only surface. LLM agents restarting after the release
read clean docs of the shiny new world with no grime from the past.

Skill update checklist — each row is one skill file, one or two
specific edits needed. Full text in section "Skill update details"
below.

| Skill file                      | Update kind                          |
|---------------------------------|--------------------------------------|
| `precis-overview.md`            | rewrite verbs table (4→7 rows)       |
| `precis-help.md`                | auto-regenerates from registry       |
| `precis-files-help.md`          | rewrite "Write" section              |
| `precis-edit-protocol.md`       | retitle, move from put to edit verb  |
| `precis-python-help.md`         | rewrite editing section              |
| `precis-markdown-help.md`       | rewrite editing section              |
| `precis-plaintext-help.md`      | rewrite editing section              |
| `precis-tags.md`                | rewrite put-with-tags → tag verb     |
| `precis-relations.md`           | rewrite put-with-link → link verb    |
| `precis-memory-help.md`         | update examples                      |
| `precis-todo-help.md`           | update examples                      |
| `precis-paper-help.md`          | update tag examples                  |
| `precis-navigation.md`          | update cross-kind recipes            |
| `precis-perplexity-help.md`     | `put(mode='import')` stays — no change |
| `precis-patent-help.md`         | update tag examples                  |
| `precis-patent-power.md`        | no change                            |
| `precis-cache.md`               | no change                            |
| `precis-density.md`             | no change                            |
| `precis-math-help.md`           | no change                            |
| `precis-web-help.md`            | no change                            |
| `precis-youtube-help.md`        | no change                            |

10 skills need substantive changes, 11 are zero-touch.

Regression test: `tests/test_skills.py::test_every_skill_uses_new_surface`
greps every skill for `put(mode='edit')` / `put(mode='delete')` /
`put(mode='append')` / `put(mode='insert')` / `put(..., untags=)` /
`put(..., unlink=)` / `move(` and fails on any hit. No allowlist
for 'historical notes' — there are no historical notes. If the text
appears in any skill, it's a bug.

### Phase 3 — reveal-on-touch hints (same release)

Agents discover the surface through the shape of their own calls.
All state-free, keyed on request shape, not wall-clock. Lives in
`src/precis/handlers/_hints.py` (new shared middleware, called
from each handler's response builder).

Fire shape-conditional hints on:

1. `put(kind=K, mode='create', ...)` without `tags=` — footer shows
   an example `tag(kind=K, id=<new id>, add=['PRIO:high'])` call.
   Teaches the tag verb at the moment the caller created a label-
   capable ref. Suppressed when the create already used `tags=`
   (caller has found the shortcut).
2. `put(kind=K, id=<existing>, tags=[...])` — `BadInput` that
   points at `tag(kind=K, id=<id>, add=[...])`. Not a hint with a
   success — a hard error with the replacement verb. The shape is
   misuse, not partial use.
3. `put(kind=K, id=<existing>, link=...)` — same pattern, pointing
   at `link`.
4. `put(kind=file, id=<file-with-selector>, mode='replace', ...)`
   → `BadInput: selector on put → use edit(mode='replace', id=...)`.
5. `search()` returning 0 hits — footer: `loosen with top_k= or
   drop scope=`. Suppressed on non-empty result sets.
6. `edit(..., match='unique')` with ≥2 candidates → `BadInput`
   with the anchor-disambiguation hint (`before=` / `after=` /
   `match='nth'`).

Tests: `tests/test_reveal_on_touch.py` — one test per hint trigger
shape, asserting the footer / error is present and points at the
right verb.

### Phase 4 — (eliminated)

There is no phase 4. The hard cutover in phase 1 eliminates the
deprecation window that would have lived here.

If rollback is needed post-release, revert the whole release, not
partial surfaces. The four-verb shape is fossil; no one should be
coding against it after this release.

## Test matrix

Add these files under `tests/`:

| File                                | Covers                               |
|-------------------------------------|--------------------------------------|
| `test_seven_verb_surface/test_put.py`     | narrowed put: create, whole-replace, initial tags/link, reject selector |
| `test_seven_verb_surface/test_edit.py`    | all modes: find-replace (default), append, replace-region, insert |
| `test_seven_verb_surface/test_delete.py`  | numeric refs, blocks, symbols, non-deletable kinds |
| `test_seven_verb_surface/test_tag.py`     | add, remove, mixed, closed-axis replace, bare-flag collision |
| `test_seven_verb_surface/test_link.py`    | add with default rel, add with rel, remove with/without rel |
| `test_seven_verb_surface/test_old_surface_gone.py` | every removed shape raises BadInput with the right verb pointer |
| `test_schema.py`                          | every verb's inputSchema is flat (no if/then/conditional required) |
| `test_skills.py`                          | every skill references only new-surface calls |
| `test_reveal_on_touch.py`                 | shape-conditional hints present on the right paths |

Parameterise across kinds wherever the matrix (D6) says "✓". Every
row of the capability matrix gets at least one test per supported
verb.

Regression tests for critic findings already landed in phase 0:

- `tests/test_python_handler_writes.py::test_put_create_rejects_doubled_root_prefix`
  (critic CRITICAL-C B2)
- `tests/test_python_handler_writes.py::test_put_create_reject_includes_suggested_unprefixed_path`
  (critic CRITICAL-C B2, error shape)
- `tests/test_python_handler_writes.py::test_replace_identical_body_under_package_root_is_noop`
  (critic MAJOR-C F1)
- `tests/test_python_handler_writes.py::test_replace_identical_body_under_package_root_via_line_range`
  (critic MAJOR-C F1, line-range form)

Still-open critic findings to fix during the surface migration:

- **MAJOR-C D2** (post-delete trailer points at deleted symbol) —
  fix in phase 1 when moving `_put_delete` dispatch to the `delete`
  verb's response builder. New test:
  `test_delete.py::test_delete_trailer_points_at_file_not_symbol`.
- **MINOR-C B4** (`(allow_rename=True)` rendered without the flag) —
  fix in `_python_render.py` same commit as phase 1. New test:
  `test_python_render.py::test_create_response_has_no_allow_rename_annotation`.

## Skill update details

For each skill flagged above. Actual text changes — commit one skill
at a time.

### `precis-overview.md`

Replace the verbs table:

```diff
-| Verb     | Use when                                            |
-|----------|-----------------------------------------------------|
-| `get`    | You know the **name** (slug, id, file path) — ...   |
-| `search` | You're looking for **content** by topic or phrase.  |
-| `put`    | You want to **write** (content, note, link, tag).   |
-| `move`   | _Reserved for structured file kinds..._             |
+| Verb     | Use when                                            |
+|----------|-----------------------------------------------------|
+| `get`    | You know the name (slug, id, path, qualname).       |
+| `search` | You're looking for content by topic or phrase.      |
+| `put`    | Create a new ref, or replace a whole file.          |
+| `edit`   | Modify existing content — append, replace, find/replace, insert. |
+| `delete` | Remove a ref, block, or symbol.                     |
+| `tag`    | Add or remove labels on an existing ref.            |
+| `link`   | Connect two refs with a typed relation.             |
```

Update the "Examples" section: replace `put(kind='todo', id=122,
tags=['STATUS:done'])` with `tag(kind='todo', id=122,
add=['STATUS:done'])`.

### `precis-files-help.md`

Replace "Write" section. Today it documents `create` / `append` /
`replace` / `delete` / `edit` / `insert` all under `put`. Split:

- `put(kind=K, id=<path>, text=..., mode='create')` stays.
- `put(kind=K, id=<path>, text=..., mode='replace')` stays (whole file).
- `edit(kind=K, id=..., mode='append', text=...)` — was `put(mode='append')`.
- `edit(kind=K, id=..., mode='replace', text=...)` with selector — was `put(mode='replace')` with selector.
- `edit(kind=K, id=..., find=..., text=...)` — was `put(mode='edit')`.
- `edit(kind=K, id=..., mode='insert', find=..., where=..., text=...)` — was `put(mode='insert')`.
- `delete(kind=K, id=<selector>)` — was `put(mode='delete')`.

Update every code block.

### `precis-edit-protocol.md`

Retitle from "anchored edits across every file kind" to "the `edit`
verb — anchored modifications across every file kind". Change every
`put(kind='...', mode='edit', ...)` to `edit(kind='...', ...)`. The
default mode `find-replace` means agents usually omit `mode=`.

### `precis-python-help.md`

The "Editing code" section — largest rewrite. Every example changes:

- `put(kind='python', id='...', mode='replace', text=...)` →
  `edit(kind='python', id='...', mode='replace', text=...)` (when
  selector present). If the id is a bare file path, stays on `put`.
- `put(kind='python', id='...', mode='delete')` → `delete(kind='python', id='...')`.
- `put(kind='python', id='...', mode='edit', find=..., text=...)` →
  `edit(kind='python', id='...', find=..., text=...)`.
- `put(kind='python', id='...', mode='insert', ...)` →
  `edit(kind='python', id='...', mode='insert', ...)`.
- `put(kind='python', id='...', mode='append', text=...)` →
  `edit(kind='python', id='...', mode='append', text=...)`.

While here, fix the finding from the critic:
`precis/src/precis/FILE.py` → `precis/FILE.py` (drop the
`src/precis/` prefix from every example path). Closes critic
CRITICAL-C B2.

Also split the skill: the pure-navigation half (views, search, TOC,
graph) stays at ~1.2 KT. The editing half moves to a new
`precis-python-edit.md` at ~1.5 KT. Closes critic MINOR-$ L8.

### `precis-markdown-help.md`, `precis-plaintext-help.md`

Same kind of rewrite as `precis-python-help.md`, on a smaller scale.
Markdown-specific: fix the "Heads up: not wired" banner to reflect
current wiring state in whichever release this ships with.

### `precis-tags.md`

Replace every `put(..., tags=[...])` on an existing id with `tag(...,
add=[...])`. Keep one example of create-with-initial-tags on `put`
as the explicit exception. The closed-vocabulary tables stay as-is.

### `precis-relations.md`

Replace every `put(..., link='...')` on an existing id with
`link(..., target='...')`. Keep one create-with-initial-link example
on `put` as the exception. The relation vocabulary stays as-is.

### `precis-memory-help.md`, `precis-todo-help.md`, `precis-paper-help.md`

Find and replace:

- `put(kind=X, id=N, tags=[...])` → `tag(kind=X, id=N, add=[...])`
- `put(kind=X, id=N, mode='delete')` → `delete(kind=X, id=N)`
- `put(kind=X, text='...', tags=[...])` (create) → keep as-is.

### `precis-navigation.md`

Update every cross-kind recipe touching write ops. Any recipe that
tag-bumps a ref or creates a relation uses the new verbs.

### `precis-perplexity-help.md`

`put(mode='import')` stays on `put` — it's a create from a
user-supplied answer, not a modification. No change needed.

### `precis-patent-help.md`

Only tag-ops are affected. Replace as in `precis-tags.md`.

## Rollout verification

### After phase 0 (prereqs on main) — ✅ done

- `test_python_handler_writes.py` passes (63 tests, including the
  four new regression tests).
- Full suite passes (1435 passed / 1 skipped as of 2026-05-01).
- Ruff clean on touched files.
- Tag as `6.0.1`, ship patch release.

### After the 6.1 release (new surface live)

- `tools/list` returns 7 tools. `move` absent.
- `precis --version` = `6.1.0`.
- `get(kind='skill', id='precis-help')` auto-renders the new verbs
  per kind (reads the registry directly).
- `rg 'put\(.*mode=.?.?edit' src/precis/data/skills/` — zero hits.
- `rg 'put\(.*mode=.?.?insert' src/precis/data/skills/` — zero hits.
- `rg 'put\(.*mode=.?.?append' src/precis/data/skills/` — zero hits.
- `rg 'move\(' src/precis/data/skills/` — zero hits.
- `rg 'untags=' src/precis/data/skills/` — zero hits.
- `rg 'unlink=' src/precis/data/skills/` — zero hits.
- Critic re-probe: run the same probes as `grimoire/agents/mcp-critic.md`
  against the new surface. CRITICAL-C and MAJOR-C findings should be
  absent.
- 7B-model smoke probe: start a fresh session, ask the model to
  "change STATUS of todo 42 to done". First call should be
  `tag(kind='todo', id=42, add=['STATUS:done'])`.

## Considered and rejected

- **Keep the 4-verb shape, split modes into sub-tools per kind.**
  Would multiply tool count to 17+ (one per kind) — classic
  `L9` surface-budget violation.
- **Go further: 8 verbs with `untag` and `unlink` as separate.**
  `tag(add, remove)` atomically does both; separating adds two
  schemas for a symmetric op. Not worth it.
- **Rename `edit` to `patch` (HTTP PATCH alignment).** Rejected —
  HTTP PATCH expects diff documents (RFC 6902 / RFC 7396).
  Precis's anchored edits are find-and-replace with anchors, not
  patch documents. `edit` matches IDE priors better.
- **Expose `mode='find-replace'` as `mode='anchor'`.** Rejected —
  "anchor" doesn't say what the op does; new agents would guess
  `mode='patch'` or `mode='replace'`. `find-replace` is a
  five-syllable label but unambiguous.
- **Two-release deprecation window** (6.1 overlap, 6.2 cutover).
  Rejected — LLM agents have no cross-session memory. Aliases
  exist to preserve human-coder habits, which aren't the prime
  customer here. Hard cutover is cleaner and saves one release.
- **Keep `move` as a reserved verb for future structured-file
  reorder.** Rejected — (a) docs fossil violation; (b) reorder is
  a content-shape change, `edit` is its home.
- **Rich response bodies on `tag` / `link` showing the post-write
  state.** Rejected — adds 50–200 tokens per call for a state the
  caller can `get` if they actually need it. Minimal confirmation
  + one follow-up hint is enough.
- **Separate error messages per kind when `delete` is unsupported.**
  Rejected — "kind '<K>' is not deletable" is enough; per-kind
  nuance belongs in the skill, not the error path.

## Follow-ups (out of scope for this migration)

- **Markdown + plaintext wiring** — their capability-matrix rows
  are declared but the handlers aren't registered in the live
  build. That's a separate piece of work; this migration just
  includes them in the matrix so they slot in without a schema
  change later.
- **Resources surface** — dual-publishing file kinds as MCP
  resources (`python://repo/path`) so resource-aware clients can
  pick them. Tracked separately.
- **Completion on tag values** — `completions/complete` for
  closed-axis enums. Nice-to-have once the tag verb ships.
- **Metrics on verb dispatch** — count verb calls per kind; used
  to validate the D6 matrix in production.
