# MCP session ergonomics

*Token budget + kind enablement + default-tag injection.*

**Status**: planned
**Owner**: `src/precis/server.py`, `src/precis/tools/core.py`,
`src/precis/data/skills/`, `src/precis/handlers/skill.py`,
`src/precis/dispatch.py`, `src/precis/protocol.py`,
note-like handlers (`memory`, `gripe`, `conversation`,
`flashcard`, `quest`, `todo`, file kinds)
**Predecessors**:
- ADR 0003 — shared tool registry (`docs/decisions/0003-shared-tool-registry.md`)
- `docs/mcp-critic-review-2026-05-02.md` — MAJOR-C cold-start
  discoverability finding

## Problem

When a fresh MCP client connects, two payloads dominate the
agent-facing context window before any user request lands:

1. `serverInfo.instructions` (one string).
2. `tools/list` (the seven verbs `get`, `search`, `put`, `edit`,
   `delete`, `tag`, `link`, each with a JSON Schema whose
   `description` field is the Python docstring of the registered
   function).

(2) is the bigger surface and the one that grew organically. The
`search` docstring at `src/precis/tools/core.py:178-213` is ~50 lines
of prose covering cross-kind fan-out semantics, the `patent`-only
`source=` matrix, the `exclude=` pagination story, the tag-axes
matrix, and several worked examples. Multiply by seven verbs and the
cold-start bill is dominated by reference material that 90% of agent
turns never need.

There is no single discovery tool to point agents at — the existing
mechanism is `search(kind='skill', q='...')`, which is genuinely
embedding-backed (see `src/precis/skill_index/index.py`) but is not
loudly advertised in the cold-start banner. Agents don't know the
verb's docstring is the *short* version of what's available via
`get(kind='skill', id='precis-<topic>-help')`.

## Goal

Four things, in this order of importance:

1. **Always-paid cost down.** Trim the per-verb docstrings inlined
   into `tools/list` to a tight signature + critical wire-level
   constraint + pointer to a skill. The reference content moves to
   skill files that the agent retrieves on demand.
2. **Discovery is the obvious first action.** Strengthen
   `serverInfo.instructions` so a cold-start agent's literal first
   move on any non-trivial request is
   `search(kind='skill', q='<their goal>')`.
3. **Per-workflow pre-surfacing is opt-in, not unconditional.** A
   new env var `PRECIS_STARTUP_SKILLS=<id>,<id>,...` resolves named
   skills into MCP `prompts/list` entries at boot. Clients that
   honour the prompts modality can pre-load them; clients that don't
   pay nothing extra. The default empty list keeps cold-start lean
   for the general case.
4. **Kind enablement is principled.** A kind is loaded iff
   *(resources present)* AND *(not prohibited)*. The first gate
   already exists — `KindSpec.requires_env`, `InitError`,
   `_try()`. The second gate is the new env var
   `PRECIS_KINDS_DISABLED=<kind>,<kind>,...`. The cold-start
   banner surfaces the live set so the agent knows what's
   actually callable on this host.
5. **Session context propagates as default tags.** A new env var
   `PRECIS_DEFAULT_TAGS=<tag>,<tag>,...` causes every `put` and
   `edit` on note-like kinds to merge those tags into whatever
   the caller passed. Generalises the existing `workspace`
   auto-tag for file-rooted kinds. Surfaced via the hint bus
   (response-time, only when used) — zero unconditional banner
   cost. Lets an operator set project context once per shell
   and have every memory / gripe / conversation note inherit it.

## Non-goals

- **No new top-level tools.** Adding a `how_can_i` alias for
  `search(kind='skill', ...)` would inflate `tools/list` for no
  semantic gain. The discovery story is `search(kind='skill')` —
  promote it via the banner, don't duplicate it.
- **No change to verb names or wire contracts.** The seven-verb
  surface is stable; this is a documentation-shape change plus
  two env vars (`PRECIS_STARTUP_SKILLS`, `PRECIS_KINDS_DISABLED`).
- **No removal of wire-level schema constraints.** `top_k <= 100`,
  the `edit` `mode`-conditional `required` coupling installed by
  `_install_edit_schema_constraints`, and reserved-arg rejection
  stay inline. Those are not narrative — they are protocol.
- **No reorganisation of the skill index machinery.** The lazy
  embedding index, sha256-keyed disk cache, and two-stream
  semantic+lexical merge in `SkillHandler.search()` are the right
  shape and stay as-is. New skills slot into the existing pipeline.
- **No `*` wildcard for `PRECIS_STARTUP_SKILLS`.** Footgun: a wheel
  bump that adds a skill would silently inflate every operator's
  cold start. Explicit lists only.
- **`PRECIS_KINDS_DISABLED` is a prohibition list, not an enable
  list.** Default empty (= autoload everything that has resources).
  An enable-list semantics would force operators to update their
  env var every time a new kind landed in the wheel — a constant
  source of "why isn't X working any more?" surprises.
- **No new resource-gating mechanism.** Phase 4 piggy-backs on the
  existing `KindSpec.requires_env` + `InitError` infrastructure.
  A separate sub-task (Phase 4 step 5) converges the few handlers
  that today gate inline at the `boot()` site (notably patent)
  onto the declarative `KindSpec` path.
- **`PRECIS_DEFAULT_TAGS` applies only to user-authored, note-like
  kinds.** Not to ingested kinds (`paper`, `patent`), fetched
  caches (`web`, `wolfram`, `youtube`), or generators (`oracle`,
  `random`, `skill`). Auto-tagging an ingested paper with
  `fbproj` is semantically wrong; this distinction is encoded as
  `KindSpec.note_like` (new boolean field).
- **`PRECIS_DEFAULT_TAGS` does not surface in
  `serverInfo.instructions`.** Active tags are advertised through
  the hint bus (per-call response trailer), not the cold-start
  banner. Zero unconditional bytes; visibility scales with use.

## Current state

### Tool registration

All seven verbs are registered from a shared registry
(`src/precis/tools/__init__.py`, ADR 0003). FastMCP inlines each
function's `__doc__` into the JSON Schema's top-level
`description`. The registry has no separate "short doc" /
"long doc" split — what's in the docstring is what the wire ships.

### Banner

`_INSTRUCTIONS` at `src/precis/server.py:53-61` is already short
(~280 chars) and already mentions `get(kind='skill', id='toc')`
and `search(kind='skill', q='...')`. `_build_instructions()` at
`src/precis/server.py:113-166` prepends a per-`PRECIS_ROOT`
sandbox preamble. Neither calls out skill-search as the *primary*
action.

### Skill discovery

`SkillHandler` at `src/precis/handlers/skill.py:243-315` runs:

1. **Semantic stream** — bge-m3 cosine over H2-chunked skill bodies
   (`FileCorpusIndex`). Cached on disk by sha256 (`embedder_model`,
   `chunker_version`) so wheel bumps re-embed automatically.
2. **Lexical stream** — substring + count, scores clamped to
   `[0, 0.49)` so any genuine semantic hit outranks any lexical hit.
3. **Per-slug merge**, ranked by score.

Existing skills under `src/precis/data/skills/`:

```
precis-overview.md            (the kind-topology mental model)
precis-edit-protocol.md       (verb-mechanics: edit modes / find / where)
precis-paper-help.md          (per-kind: paper)
precis-patent-help.md         (per-kind: patent — incl. some search nuance)
precis-patent-power.md        (advanced patent workflows)
precis-files-help.md          (per-kind: markdown / plaintext / tex)
precis-markdown-help.md       (per-kind: markdown)
precis-plaintext-help.md      (per-kind: plaintext)
precis-python-help.md         (per-kind: python)
precis-paper-tag-axes.md      (verb×kind: tag × paper)
precis-tags.md                (cross-kind tag conventions)
precis-cache.md               (cache kinds: web / wolfram / youtube)
precis-gripe-help.md          (per-kind: gripe)
precis-oracle-help.md         (per-kind: oracle)
precis-random-help.md         (per-kind: random)
```

Notably absent: per-verb skills for `search`, `get`, `put`, `tag`,
`link`, `delete` (the protocol-level mechanics that today live
inlined in the verbs' docstrings).

### Kind registration

`boot()` at `src/precis/dispatch.py:444+` is the composition root.
For each candidate handler it calls `_try(cls, hub=hub, **kw)`,
which:

1. Constructs the handler.
2. Catches `InitError` / `ImportError` / `ValueError`, logs a
   warning, and returns `None` — the kind silently drops off
   the LLM surface.
3. On success, calls `inst._register_with(hub)` to publish the
   `(kind, verb, mode)` rows into the dispatch table.

Resource gating today is **partly declarative, partly inline**:

- **Declarative**: `KindSpec.requires_env` (e.g. `math` requires
  `WOLFRAM_APP_ID`). `KindSpec.is_available()` checks the env.
  Some handlers (math) also re-check inside their `_fetch()` for
  defense-in-depth.
- **Inline at boot site**: `patent` is gated by a `if epo_key and
  epo_secret and epo_raw_root:` block at
  `src/precis/dispatch.py:604-607`. The handler isn't even
  constructed unless the env trio is present.
- **Store / root gating**: store-backed handlers are inside
  `if store is not None:`; file kinds are inside `if precis_root:`.

There is **no operator-side knob to disable a kind whose
resources are present**. An operator who has `WOLFRAM_APP_ID` set
for an unrelated reason but doesn't want the `math` kind active
has no clean way to suppress it.

The skill-availability filter at
`src/precis/handlers/skill.py:833-879` already handles the
downstream story: skills for unwired kinds get the `[unwired]`
marker so an agent doesn't quote a recipe that immediately fails.

### Tagging

Every kind with `KindSpec.supports_tag=True` accepts open-vocab
open-list tags via `tag(add=[...], remove=[...])` and through
`tags=[...]` on `put` / `edit` (where supported by the handler).

Closed-vocab tag axes (e.g. `cpc:B01J27/24` for patents,
`topic-xxx` for paper topics) are validated per-kind via the
existing `precis-paper-tag-axes` machinery. Open-vocab tags
(`workspace`, `fbproj`, `2026-q2`) flow through with no validation
beyond charset rules.

**Auto-tagging precedent**: file-rooted kinds (`markdown`,
`plaintext`, `tex`) automatically receive a `workspace` tag on
creation, surfaced in the cold-start banner at
`src/precis/server.py:163-164`. There's no operator knob — the
tag is hard-coded. The proposed `PRECIS_DEFAULT_TAGS` generalises
this pattern: same idea, configurable, applies to a broader set
of note-like kinds.

The **hint bus** at `src/precis/dispatch.py:264-271` is the
response-time channel for ambient information. `hub.emit_hint()`
appends a hint to the current request's collector; the runtime
renders collected hints after the verb result. The default-tag
injection uses this channel for visibility (one hint per
auto-tagged put/edit) so token cost scales with use, not with
uptime.

## Proposed change

### Skill axes — sparse population, verb+kind naming

Three logical axes, populate only cells with genuinely unique
content. Skill ids follow a consistent shape so the absence of a
skill at a given cell is itself a discovery signal ("there is no
`precis-patent-put-help` because patents aren't really `put`").

| Axis | Naming shape | Examples |
|---|---|---|
| **Per-verb** (kind-agnostic) | `precis-<verb>-help` | `precis-search-help`, `precis-get-help`, `precis-edit-help`, `precis-put-help`, `precis-tag-help`, `precis-link-help`, `precis-delete-help` |
| **Per-kind** (verb-agnostic) | `precis-<kind>-help` | (status quo) `precis-paper-help`, `precis-patent-help`, `precis-files-help`, … |
| **Verb × kind** (only when cell-unique) | `precis-<kind>-<verb>-help` | `precis-patent-search-help` (the `source=` matrix). Existing under a slightly older name: `precis-paper-tag-axes`. |

Disambiguation rule: when `<x>` could be either a verb or a kind
(unlikely but possible), the parser checks the live registry and
prefers the kind interpretation. Both the verb and kind
name-spaces are short and disjoint today.

`PRECIS_STARTUP_SKILLS` takes literal skill ids regardless of
axis, so an operator can compose any subset.

### Tool names stay seven-verb — do not mirror skill names

The skill-name convention `precis-<kind>-<verb>-help` is *not* a
proposal to flatten the tool surface to `patent_search()`,
`paper_get()`, etc. The seven verbs (`get`, `search`, `put`,
`edit`, `delete`, `tag`, `link`) plus the `kind=` discriminator
remain the wire shape, for three reasons:

1. **Token-budget arithmetic.** A flat verb×kind tool surface
   would have ~30-40 tools at the interesting cells, each with a
   `tools/list` description — multiplying the very cost this
   design exists to reduce.
2. **Identity.** `AGENTS.md` opens with "seven-verb agent tool
   surface". The verb count is a deliberate constraint that
   shapes every handler's API.
3. **Schema constraint machinery.** The `_install_edit_schema_constraints`
   work at `src/precis/server.py:337-404` lives in one place; a
   flat surface would scatter equivalent constraints across
   every per-kind variant.

Skill names are *content* addressing: "the doc describing how to
search patents". Tool names are *protocol* addressing: "the
verb you call to perform a search". The two address spaces are
allowed (and helpful) to use different naming conventions.

### Discoverability invariant

Verb docstrings are exposed only via the `tools/list` JSON Schema
`description` field. They are **not** part of any embedded
corpus. The skill index at
`src/precis/handlers/skill.py:243-315` searches *only* skill
bodies under `src/precis/data/skills/`.

Implication: **every paragraph removed from a docstring must land
in a skill body** or it becomes undiscoverable. Phase 1 enforces
this by treating each verb-docstring trim and the matching
skill-content insertion as one atomic edit.

### Verb description shape

Target ~5 lines per verb. Concrete shape, illustrated for `search`:

```python
def search(q, kind=None, scope=None, top_k=10, tags=None,
           source=None, exclude=None) -> str:
    """Hybrid lexical + semantic search across kinds.

    top_k must be a positive int <= 100. Omit kind (or pass '*') for
    cross-kind fan-out. exclude= takes ref slugs to drop from results
    (pass back the slugs of prior hits to paginate).

    For per-kind search nuances and tag axes:
      search(kind='skill', q='search <kind>')
    """
```

Rules of thumb for the trim:

1. Keep the function signature in the body (small models read it
   first).
2. Keep one sentence per non-trivial wire-level constraint.
3. Replace example-heavy paragraphs with a one-line discovery
   pointer to a specific skill query.
4. Keep `Args:` blocks only where the CLI argparse adapter at
   `src/precis/tools/cli_adapter.py:84-91` needs them to extract
   per-arg help. (See OQ-7.)

### Banner CTA

Replace the closing two lines of `_INSTRUCTIONS` with:

```
First action on any non-trivial request:
  search(kind='skill', q='<your goal in 2-5 words>')
This returns ranked help skills (verb mechanics, kind specifics,
tag axes, edit protocol, ...). For the full index:
  get(kind='skill', id='toc').

Verbs: get, search, put, edit, delete, tag, link.  Discriminator: kind=.
```

The verb-list line stays so the import-time assertion at
`src/precis/server.py:67-70` keeps pinning every verb name.

### Kind enablement: resources + prohibition

Uniform predicate for whether a kind loads:

```
  loaded(kind) = resources_present(kind) AND NOT prohibited(kind)
```

Where:

- `resources_present(kind)` is the existing machinery: env vars
  declared in `KindSpec.requires_env`, optional-dep import
  success, `store is not None` for store-backed kinds,
  `precis_root` for file kinds. Conjunction across all required
  resource categories.
- `prohibited(kind)` is new: `kind in parse(PRECIS_KINDS_DISABLED)`.

Default `PRECIS_KINDS_DISABLED` is empty; default behaviour is
unchanged from today ("autoload everything for which resources
are present").

Resolution at boot:

1. Parse `PRECIS_KINDS_DISABLED` (strip whitespace, dedupe).
2. Wrap `_try()` (or extend `boot()`) so each candidate handler
   first checks the prohibition gate. Skipped kinds log:

   ```
   precis dispatch boot: skipped kind=patent (prohibited via PRECIS_KINDS_DISABLED)
   ```

3. Resource gating runs as today (handler `__init__` raises
   `InitError`, `_try()` catches and logs).
4. After all gates: `_build_instructions()` (Phase 2) appends a
   line to `serverInfo.instructions` summarising the live set:

   ```
   Kinds loaded: paper, memory, markdown, plaintext, calc, oracle,
     skill, gripe, conversation, flashcard, random, web, youtube
   Kinds unavailable: patent (missing EPO_OPS_CLIENT_KEY),
     wolfram (prohibited).
   ```

   The `unavailable` line is suppressed when empty (zero
   unconditional bytes). Reasons are short tags (`missing X`,
   `prohibited`, `no store`, `import failed`). The agent can
   advise the operator on missing-resource kinds and skip
   prohibited ones gracefully.
5. Cross-check with `PRECIS_STARTUP_SKILLS`: if a pinned skill's
   subject kind isn't loaded, route through the same
   ⚠-notice path as unknown skill ids (Phase 3 step 3) so the
   operator sees:

   ```
   ⚠ PRECIS_STARTUP_SKILLS skipped skills for unloaded kinds:
     precis-patent-search-help (kind=patent unavailable).
   ```

### Convergence on declarative resource gating

To make the predicate clean, all handlers should declare their
resources via `KindSpec` rather than inline-gating at the `boot()`
site. Today's outliers:

- **Patent**: gated inline at `dispatch.py:604-607` with a
  three-env-var check before the handler is even imported. Move
  the env check into `PatentHandler.__init__`, raise `InitError`
  with a clear message. Update `KindSpec.requires_env` to list
  the three vars.
- **Store-backed kinds**: declare `requires_store: bool = True` on
  `KindSpec` (new field) and let `boot()` skip them uniformly
  rather than via the `if store is not None:` block. Same for
  `requires_embedder` / `requires_root`.

This convergence is a separate concern from the cold-start budget
— it's a code-organisation cleanup that this design surfaces but
does not require for shipping. Phase 4 step 5 covers it.

### `PRECIS_DEFAULT_TAGS` env var

Format: comma-list of open-vocab tag strings.

```
PRECIS_DEFAULT_TAGS=fbproj,foobar,2026-q2
```

Note-like kinds (`KindSpec.note_like=True` after Phase 5 audit):

- `memory`, `gripe`, `conversation`, `flashcard`, `quest`, `todo`
  — personal-record kinds.
- `markdown`, `plaintext`, `tex` — user-authored files. The
  hard-coded `workspace` tag continues to apply alongside the
  configurable defaults; both merge into the resulting tag set.

Not note-like (auto-tagging would be semantically wrong): `paper`,
`patent`, `web`, `wolfram`, `youtube`, `python`, `calc`,
`oracle`, `random`, `skill`.

Resolution at request time:

1. Parse `PRECIS_DEFAULT_TAGS` once at runtime build (cache the
   resolved list on the runtime; re-resolution per call is
   wasted CPU).
2. **`put` / `edit` on a note-like kind**: merge default tags
   into the explicit `tags=[...]` argument, de-duplicate. Apply
   the merged set as the kind's tag list.
3. Emit a hint via `hub.emit_hint()`:

   ```
   💭 Tagged with default: fbproj, foobar, 2026-q2
   ```

   Suppressed when default list is empty (zero unconditional
   bytes). Suppressed when explicit tags already include every
   default (no surprise; nothing to advertise).
4. **`tag` on a note-like kind**: do **not** auto-apply. Emit a
   hint *if* the kind doesn't already carry every default tag:

   ```
   💡 Active default tags not on this ref: fbproj, 2026-q2.
     tag(kind='memory', id=..., add=['fbproj', '2026-q2']) to apply.
   ```

   Operator-aware reminder; never overrides explicit user intent.
5. **Validation**: default tags pass through the same
   per-kind tag-axis validator as explicit tags. A default tag
   that collides with a closed-vocab axis name is operator
   error; the validator surfaces it on the first put/edit call
   that uses it (same path as `tag(add=...)` validation).

### `PRECIS_STARTUP_SKILLS` env var

Format: comma-list of literal skill ids.

```
PRECIS_STARTUP_SKILLS=precis-search-help,precis-paper-help,precis-patent-search-help
```

Resolution at boot, in `_init_runtime()`:

1. Parse the env var (strip whitespace, drop empties, dedupe).
2. For each id, look up the body via the existing skill loader
   (`precis.data.skills`) and validate the slug exists.
3. Unknown id → log a `WARNING` to stderr **and** append a
   one-line notice to `serverInfo.instructions` so the connected
   agent can advise the operator:

   ```
   ⚠ PRECIS_STARTUP_SKILLS skipped unknown skill ids: foo, bar.
   ```

   Notice is suppressed for valid configs (zero bytes paid
   unconditionally; small bytes paid only when broken).
4. Total resolved bytes capped at a configurable budget
   (default 50 KB) — over budget: log `WARNING`, truncate the
   tail, continue. Cap configurable via `PRECIS_STARTUP_SKILLS_CAP_KB`.
5. Each resolved skill is registered as an MCP `prompts/list`
   entry (alongside the existing skill→prompts wiring in
   `src/precis/mcp_modalities.py`).
6. **Belt-and-suspenders advertising.** Append a one-line notice
   to `serverInfo.instructions` listing the pinned skill ids:

   ```
   Pinned skills (load via prompts/get): precis-paper-help,
   precis-patent-search-help.
   ```

   This guarantees the agent knows about the pre-pinned prompts
   even if the client doesn't auto-render `prompts/list` at
   session start (server-side default-on flag availability is
   uncertain across MCP 2025-06-18 / FastMCP 1.x — see OQ-11).
   Cost is a few dozen bytes per pinned skill in the banner,
   far less than inlining the bodies.

The opt-in posture means: in the absence of the env var, cold
start is the new minimal baseline. Setting the env var costs only
clients that honour the prompts modality.

### Discovery rendering

`get(kind='skill', id='toc')` already lists every skill (see
`SkillHandler._render_toc`). After Phase 1 lands new per-verb
skills, the TOC grows by ~3-7 entries — still well within a
sensible page.

## Implementation

Phased rollout. Each phase is independently shippable.

### Phase 1 — verb docstrings + new skills

1. Author new skill files (per-verb tier):
   - `precis-search-help.md`
   - `precis-get-help.md`
   - `precis-put-help.md` (stub if minimal content)
   - `precis-tag-help.md` (stub if minimal content)
   - `precis-link-help.md` (stub if minimal content)
   - `precis-delete-help.md` (stub if minimal content)
   - **Rename** `precis-edit-protocol.md` → `precis-edit-help.md`.
     No redirect file: MCP clients reconnect and see the new
     surface; backward compat across protocol restarts is
     unnecessary friction.
2. Author new skill files (verb×kind tier):
   - `precis-patent-search-help.md` (the `source=` matrix +
     prior-art sweep mode).
3. Trim docstrings on `get`, `search`, `put`, `edit`, `delete`,
   `tag`, `link` in `src/precis/tools/core.py`. Each ends with a
   `search(kind='skill', q='<verb> <hint>')` pointer. **Atomic**
   with content insertion in steps 1-2: every paragraph removed
   must already exist in a skill (Discoverability invariant).
4. Add per-arg `help=` strings to the CLI argparse adapter at
   `src/precis/tools/cli_adapter.py:84-91` so the CLI surface
   stays informative even though docstrings shrank. (Resolves
   OQ-7.) Note: CLI users always retain `precis search --kind
   skill --q "<query>"` as a richer fallback when the per-arg
   `--help` is too terse — the same skill index that powers MCP
   discovery powers the CLI.
5. Re-run the import-time assertion in `src/precis/server.py:67-70`;
   keep the verb list intact.

### Phase 2 — banner CTA + kinds-loaded summary

1. Edit `_INSTRUCTIONS` in `src/precis/server.py` to lead with
   the discovery CTA.
2. Extend `_build_instructions()` to append a `Kinds loaded:` /
   `Kinds unavailable:` summary, sourced from the live hub plus
   the prohibition set (Phase 4 wires the latter; this phase can
   ship the live-set part with no prohibitions visible yet).
3. Update the docstring of `_build_instructions()` to reflect the
   new shape.
4. Update tests pinning the static core
   (`test_instructions_advertises_every_verb` and friends).

### Phase 3 — `PRECIS_STARTUP_SKILLS` env var

1. New module `src/precis/startup_skills.py` (or extend
   `src/precis/mcp_modalities.py`): parse + resolve + cap +
   register prompts.
2. Wire from `_init_runtime()` after `_wire_modalities()`.
3. Add a config dataclass field on `PrecisConfig` for the cap (so
   tests can set a small cap without monkey-patching).
4. New skill: `precis-startup-skills-help.md` documenting the env
   var so it's discoverable via the same channel it serves.

### Phase 4 — `PRECIS_KINDS_DISABLED` + resource-gating convergence

1. New module `src/precis/kind_gate.py`:
   - `parse_disabled(env: Mapping[str, str]) -> frozenset[str]`
     — read `PRECIS_KINDS_DISABLED`, normalise, dedupe.
   - `Loadability(kind: str, loaded: bool, reason: str | None)`
     — verdict per kind.
   - `gate(spec: KindSpec, *, disabled: frozenset[str], store,
     embedder, root) -> Loadability` — the uniform predicate.
2. Wire the gate into `boot()`:
   - Before each `_try(cls, ...)` call, look up the spec's kind
     and check the prohibition set; on hit, log and skip
     construction entirely.
   - Collect `Loadability` verdicts per kind in a list passed
     back on the `Hub` so `_build_instructions()` can render the
     `Kinds unavailable:` summary with reasons.
3. **Convergence**: move patent's inline env gate from
   `dispatch.py:604-607` into `PatentHandler.__init__`. Add
   `EPO_OPS_CLIENT_KEY`, `EPO_OPS_CLIENT_SECRET`,
   `PRECIS_PATENT_RAW_ROOT` to its `KindSpec.requires_env`. The
   handler raises `InitError` with a clear missing-var message.
4. Cross-check `PRECIS_STARTUP_SKILLS` against the loaded set
   (Phase 3 already has the unknown-id notice path; extend it
   for kind-unavailable cases).
5. **Document** the gate posture in
   `docs/conventions/kind-enablement.md` so handler authors know
   to put resource declarations in `KindSpec` rather than at the
   boot site.

### Phase 5 — `PRECIS_DEFAULT_TAGS`

1. New field on `KindSpec`: `note_like: bool = False`.
   Default False so existing handlers don't accidentally opt
   in.
2. Audit and flip the flag on the note-like handlers:
   `MemoryHandler`, `GripeHandler`, `ConversationHandler`,
   `FlashcardHandler`, `QuestHandler`, `TodoHandler`,
   `MarkdownHandler`, `PlaintextHandler`, `TexHandler`. (Audit
   tracked as Phase 5 step 2; each handler gets a single-line
   spec edit.)
3. New module `src/precis/default_tags.py`:
   - `parse(env: Mapping[str, str]) -> tuple[str, ...]` — read
     `PRECIS_DEFAULT_TAGS`, normalise, dedupe, freeze.
   - `merge(explicit: Sequence[str] | None,
     defaults: Sequence[str]) -> list[str]` — set-merge
     preserving explicit-first ordering.
   - `apply_to_payload(payload: dict[str, Any],
     defaults: Sequence[str], *, kind_is_note_like: bool) ->
     dict[str, Any]` — mutate in-place if applicable.
4. Wire into the dispatch boundary, **not** per-handler:
   - In `precis.tools.core.put` and `precis.tools.core.edit`,
     after payload assembly, look up the kind's spec via the
     hub; if `note_like`, call `apply_to_payload`.
   - On successful dispatch where defaults were applied, emit
     the hint via `hub.emit_hint()`.
   - For `tag`: same lookup, but emit the *suggestion* hint
     instead of mutating.
5. New skill `precis-session-context-help.md` documenting the
   three env vars (`PRECIS_STARTUP_SKILLS`,
   `PRECIS_KINDS_DISABLED`, `PRECIS_DEFAULT_TAGS`) and their
   interactions.
6. Resolve `PRECIS_DEFAULT_TAGS` once at runtime build; cache
   on `PrecisRuntime` so per-call dispatch is `O(1)` (frozenset
   membership for de-dup).

### Phase 6 — regression guard

1. New test in `tests/test_server.py`:
   - Build the runtime in a fresh fixture.
   - Render `tools/list` JSON via the FastMCP test harness.
   - Assert total byte count under a target ceiling (start at
     8 KB; tighten in a follow-up once we measure the real
     post-trim baseline).
   - Same for `serverInfo.instructions` (start at 2 KB).
2. New test for `PRECIS_STARTUP_SKILLS` resolution paths: empty,
   valid, invalid id, over-cap, kind-unavailable.
3. New test for `PRECIS_KINDS_DISABLED` resolution paths: empty,
   single kind, multiple kinds, unknown kind name (log + skip),
   interaction with resource-missing kinds (both reasons logged
   distinctly).
4. New test for `PRECIS_DEFAULT_TAGS` resolution paths: empty,
   set + note-like put (merge + hint), set + non-note-like put
   (no merge, no hint), set + put with explicit tags (union, no
   duplicates, hint shows only the additions), set + tag verb on
   note-like (suggestion hint only, no mutation).

## Open questions

### Open

1. **OQ-11 (verification, not blocking).** Does FastMCP let us
   flag a `prompts/list` entry as "render at session start"
   server-side, or is that a client-side convention only?
   *Needs verification* against MCP 2025-06-18 + FastMCP 1.x
   source. **Mitigation already in design**: belt-and-suspenders
   advertising via a `Pinned skills:` line in
   `serverInfo.instructions` (Phase 3 step 6). The verification
   tells us whether the banner notice is a fallback or the only
   mechanism — either way the design ships.
2. **OQ-13.** Naming: `PRECIS_KINDS_DISABLED` (prohibition
   semantics, the chosen design) vs. `PRECIS_KINDS_DENY` vs.
   something else? *Tentative*: `PRECIS_KINDS_DISABLED` for
   parallel reading with `PRECIS_STARTUP_SKILLS_CAP_KB` and a
   neutral verb. Confirm before sealing.
3. **OQ-14.** Should `PRECIS_KINDS_DISABLED` accept a kind name
   for a kind that doesn't exist (typo, removed kind)? Same
   pattern as OQ-5: log + skip + add notice to instructions so
   the agent can flag the typo. *Tentative*: yes, mirror the
   skill-id treatment.
4. **OQ-15.** When `PRECIS_KINDS_DISABLED=patent` is set, should
   `precis-patent-help` and `precis-patent-search-help` be
   filtered out of the skill TOC entirely, or marked
   `[unwired]` (existing semantics for resource-missing kinds)?
   *Tentative*: keep the same `[unwired]` marker — a deliberate
   prohibition is functionally identical to a missing resource
   from the agent's perspective; both mean "don't recipe this".
5. **OQ-16.** Convergence scope: do we move *all* inline boot-
   site gates to `KindSpec.requires_env` in Phase 4 step 3, or
   only the patent case that's most awkward? *Tentative*: only
   patent in this phase; file other handlers under a follow-up
   `OPEN-ITEMS.md` ticket so each conversion can be reviewed
   in isolation.
6. **OQ-17.** `PRECIS_DEFAULT_TAGS` interaction with `workspace`
   auto-tag on file kinds: do they layer (workspace stays,
   defaults add on top) or does one supersede? *Tentative*: layer
   — workspace identifies file-rooted-ness; defaults identify
   project context; both true simultaneously is the right
   semantics.
7. **OQ-18.** Where does the dispatch hook for default-tag
   injection live: in `precis.tools.core` (the verb wrappers)
   or deeper in the runtime? *Tentative*: in
   `precis.tools.core.put` / `.edit` so the boundary is clear
   and per-handler audit isn't required. Runtime stays
   handler-agnostic.
8. **OQ-19.** Should `PRECIS_DEFAULT_TAGS` be a *list* (current
   design) or a structured form (`PRECIS_PROJECT=fbproj` plus
   reserved tag prefix `project:fbproj`)? *Tentative*: list.
   Structured forms accrete fastest (one env var per axis); a
   plain list keeps the operator's mental model simple. If
   project-tag prefix conventions emerge, they live in skill
   docs, not in env-var schema.

### Resolved

- **OQ-1** — Rename `precis-edit-protocol` → `precis-edit-help`
  with no redirect. MCP clients reconnect and see the new
  surface; protocol-level backward compat is unnecessary
  friction. ✅
- **OQ-2** — Patent `source=` matrix lives in
  `precis-patent-search-help` (verb×kind). Cleaner retrieval
  target for `search`-shaped queries; `precis-patent-help`
  keeps paper-vs-OPS / kind-level concerns. ✅
- **OQ-3** — Stub the trivial-verb skills (`tag`, `link`,
  `delete`) on day one. A `search(kind='skill', q='tag')` that
  lands nowhere is a worse cold-start experience than a
  one-paragraph skill. ✅
- **OQ-4** — Cap on `PRECIS_STARTUP_SKILLS` total bytes is
  configurable (env var `PRECIS_STARTUP_SKILLS_CAP_KB`),
  default 50 KB, behaviour: log warning + truncate. ✅
- **OQ-5** — Unknown skill ids in `PRECIS_STARTUP_SKILLS`: log
  to stderr + append a one-line notice to
  `serverInfo.instructions` so the agent can advise the
  operator. Suppressed for valid configs (zero unconditional
  bytes). ✅
- **OQ-6** — `PRECIS_STARTUP_SKILLS` does **not** accept
  wildcards. Explicit lists only. A wheel bump that added a
  skill would silently inflate every operator's cold start
  under `*`. ✅
- **OQ-7** — Add explicit per-arg `help=` strings to argparse
  in Phase 1 step 4. CLI users always retain
  `precis search --kind skill --q "<query>"` as a richer
  fallback when terse `--help` isn't enough. ✅
- **OQ-8** — Hard cut on the long `search` docstring; the
  docstring is not a public API surface and clients consume
  the JSON Schema description, which can shrink atomically. ✅
- **OQ-9** — Token-budget guard test serialises `tools/list`
  exactly as FastMCP would, asserts
  `len(json.dumps(tools_list)) < 8192` initially; tighten to
  `baseline + 20%` once Phase 1 lands and a real measurement
  exists. ✅
- **OQ-10** — Write `docs/decisions/0013-mcp-cold-start-budget.md`
  capturing the project-wide stance: reference material lives
  in skills, retrieved on demand; tool descriptions carry only
  signature + wire-level constraints + discovery pointer. ✅
- **OQ-11 (mitigation)** — Belt-and-suspenders: register
  pinned skills as `prompts/list` entries **and** advertise
  their ids in the cold-start banner. ✅
- **OQ-12** — Folded into this design as Phase 4. The
  *functional* axis (`PRECIS_KINDS_DISABLED`) and the
  *presentational* axis (`PRECIS_STARTUP_SKILLS`) ship together
  but stay logically distinct. The kind-enablement predicate
  is `(resources present) AND (not prohibited)`. ✅
- **default-tag scope** — `PRECIS_DEFAULT_TAGS` (broader than
  `PRECIS_MEMORY_TAGS`), applies to note-like kinds only,
  put + edit (not tag), merge with explicit, hint-bus surfacing
  (not banner). Folded as Phase 5. ✅

## Risks

1. **Small models lose inline guidance.** Some 7-8B local models
   currently scrape the long `search` docstring for examples. After
   the trim, those models must follow the discovery pointer.
   Mitigation: keep the example sentence in `precis-search-help`
   short and self-contained; verify with the existing critic-review
   battery (`docs/mcp-critic-review-2026-05-02.md`) on a 7B model
   before sealing.
2. **Discovery latency on first call.** First
   `search(kind='skill')` triggers chunking + embedding (or cache
   read). On a cold container with no skill cache, that's
   ~0.5-1 s. Mitigation: pre-warm the index in `_init_runtime()`
   so the first agent call doesn't pay it. (This is a separate
   ticket; flag in `OPEN-ITEMS.md`.)
3. **Skill content drift between releases.** A wheel bump that
   re-words a skill silently changes every connected client's
   discovery answer. Mitigation: skills go through normal review
   like any other public surface; consider a skill-version axis on
   the cache key (already there via `chunker_version`, but content
   lives in the file sha256).
4. **`PRECIS_STARTUP_SKILLS` becoming load-bearing.** Operators
   start relying on `precis-paper-help` being pre-surfaced and
   skip teaching their agents to discover. Mitigation:
   `precis-startup-skills-help.md` explicitly documents the
   feature as a perf/UX optimisation, not a contract.
5. **`PRECIS_KINDS_DISABLED` mistakenly disables a needed kind.**
   Operator sets `=patent` for testing, forgets, ships to prod.
   Mitigation: `Kinds unavailable:` line in the banner shows
   reason (`prohibited` vs. `missing X`) so the operator sees
   their own footprint at a glance.
6. **Convergence creates a behaviour change for patent.** Today
   patent's env gate runs *before* the handler is imported. After
   convergence, the import happens and `__init__` raises
   `InitError` instead. Net behaviour identical (kind not
   registered), but the import-time cost shifts. Negligible in
   practice (epo_ops is small) but worth flagging in the test
   diff.
7. **`PRECIS_DEFAULT_TAGS` left set in a forgotten shell.**
   Operator's notes accrete `fbproj` for weeks after the project
   ended. Mitigation: hint bus surfaces the active set on every
   put/edit, so the operator sees their footprint immediately.
   No silent accretion.
8. **Default-tag collision with closed-vocab axes.** A default
   tag named `cpc:foo` on a kind with the patent CPC axis would
   confuse the validator. Mitigation: the existing per-kind
   axis validator runs unchanged; bad defaults surface as
   validation errors on the first relevant call (same path as
   `tag(add=['cpc:foo'])` would take). Operator fixes the env
   var, no permanent damage.

## Test strategy

1. **Token-budget guard** — new test, see Phase 5 step 1.
2. **Pinned banner** — existing
   `test_instructions_advertises_every_verb` updated to the new
   string but keeping the verb-list assertion.
3. **Skill resolution paths** — new tests for `PRECIS_STARTUP_SKILLS`
   parser: empty, valid, mixed-valid+invalid, all-invalid, over-cap,
   kind-unavailable.
4. **Kind gate paths** — new tests for `PRECIS_KINDS_DISABLED`:
   empty, single, multiple, unknown kind name (log + skip),
   interaction with resource-missing kinds (both reasons logged
   distinctly), `Kinds unavailable:` banner rendering.
5. **Skill index integration** — existing
   `tests/test_skill_index.py` and `tests/test_skill_handler.py`
   continue to pass; they exercise the cosine path the new skills
   land in.
6. **CLI argparse** — existing CLI integration tests for each
   verb continue to pass after the per-arg `help=` strings are
   added explicitly.
7. **`prompts/list` integration** — verify the new entries
   surface; verify the default-on flag (or document the absence,
   per OQ-11).
8. **Patent convergence** — existing patent integration tests
   (where present) continue to pass after the env gate moves
   from `boot()` to `PatentHandler.__init__`. Verify the kind
   doesn't register when env vars are absent and registers
   normally when they're present.
9. **Default-tag merge** — unit-test the `merge()` primitive
   for the union/dedup invariant.
10. **Default-tag hint emission** — integration test that
    `put(kind='memory', tags=['x'])` with
    `PRECIS_DEFAULT_TAGS='fbproj,x'` emits the hint with
    `fbproj` only (since `x` was already explicit).
11. **Note-like flag audit** — a registry-walk test asserting
    every handler we expect to be note-like carries
    `note_like=True`, and every other handler carries
    `note_like=False`. Catches a spec edit drift.

## References

- ADR 0003 — shared tool registry
  (`docs/decisions/0003-shared-tool-registry.md`)
- ADR 0007 — derived queue / no blocking jobs
  (`docs/decisions/0007-derived-queue-no-block-jobs.md`)
  (background: skill index is a derived artefact, lazy-built)
- `docs/mcp-critic-review-2026-05-02.md` — MAJOR-C cold-start
  discoverability finding
- `src/precis/server.py:53-61` — `_INSTRUCTIONS`
- `src/precis/server.py:113-166` — `_build_instructions`
- `src/precis/tools/core.py:178-213` — current `search` docstring
  (the worst offender)
- `src/precis/handlers/skill.py:243-315` — skill search merge logic
- `src/precis/skill_index/index.py:111-151` — semantic search core
- `src/precis/tools/cli_adapter.py:84-91` — argparse help
  extraction
- `src/precis/protocol.py:25-73` — `KindSpec` (incl.
  `requires_env`, `is_available()`)
- `src/precis/dispatch.py:73-78` — `InitError`
- `src/precis/dispatch.py:300-337` — `_try()` (boot-time
  registration helper)
- `src/precis/dispatch.py:444+` — `boot()` composition root
- `src/precis/dispatch.py:604-607` — patent's inline env gate
  (target of Phase 4 convergence)
- `src/precis/handlers/skill.py:833-879` — `_availability_gap`
  (skill filtering for unwired kinds)
- `src/precis/dispatch.py:264-271` — `Hub.emit_hint` (response-
  time hint channel used by Phase 5)
- `src/precis/server.py:163-164` — `workspace` auto-tag for
  file-rooted kinds (precedent for default-tag injection)
