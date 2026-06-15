"""Planner-prompt builder for the LLM-tick coroutine.

When the dispatch worker mints a ``plan_tick`` job under an
``LLM:*``-tagged todo, the claude_inproc executor needs a prompt to
hand to ``claude -p``. This module builds that prompt in two
layers so Anthropic's prompt cache works:

CACHED LAYER — stable across every planner tick, system role:

* The pinned ``precis-tasks-help`` skill verbatim (the planner's
  operational manual: levels, doable rotation, halt/ask-user, LLM
  tag convention).
* The skill **index** — one line per active skill carrying its
  ``summary`` field. Tells the planner what territories exist;
  detail is fetched on demand via ``get(kind='skill', id=…)`` or
  ``search(kind='skill', q='…')``.
* The planner contract — short paragraph listing the four output
  shapes (mint children / link blocked-by / yield to user / mark
  done) plus the depth-discipline reminder.

VARIABLE LAYER — per-tick, user role:

* TOON-formatted ancestry chain (id, title, ``from``).
* The todo's body chunks (its goal).
* Every completed child's ``job_summary`` chunk, ordered by
  completion, with the child's id and title as a header.

Order matters: cached content goes first so the cache prefix is
the longest possible across calls. Anything dynamic — clocks,
ids, the body of *this* todo — lives in the variable layer.

This module is pure: it reads the store, returns ``(system, user)``
strings. The executor is responsible for actually invoking
``claude_agent.call_claude_agent`` with them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.handlers._skill_common import parse_frontmatter

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


#: Skill that always rides in the cached system prompt verbatim. The
#: planner's operational manual — without it the contract isn't
#: visible. Other skills are summary-only in the index and pulled
#: on demand via MCP ``get``.
_PINNED_SKILL_ID: str = "precis-tasks-help"


#: Hard cap on the skill index. Each entry costs ~80 tokens; capping
#: keeps the cached system prompt under ~5k tokens even as the skill
#: corpus grows. If a planner needs a skill not in the top N, it
#: calls ``search(kind='skill', q='…')`` — that's the discovery
#: mechanism by design.
_SKILL_INDEX_MAX: int = 80


@dataclass(frozen=True)
class PlannerPrompts:
    """Two-layer prompt for a single planner tick.

    ``system`` is identical across every tick of every parent todo —
    same cache prefix lands hits across the whole fleet.
    ``user`` is per-tick: ancestry + body + accumulated child
    summaries.
    """

    system: str
    user: str


def build_planner_prompts(
    store: Store, *, ref_id: int, model: str
) -> PlannerPrompts:
    """Build the cached + variable layers for a planner tick on ``ref_id``.

    ``model`` is the LLM the executor will hand the prompts to; it's
    embedded in the user layer so the planner is aware which budget
    tier it's working in (cheaper model → fewer children, simpler
    output).
    """
    system = _build_system_prompt(store)
    user = _build_user_prompt(store, ref_id=ref_id, model=model)
    return PlannerPrompts(system=system, user=user)


# ── cached layer ──────────────────────────────────────────────────


def _build_system_prompt(store: Store) -> str:
    """Build the stable, cache-friendly system prompt.

    Pinned skill + skill index + planner contract. No timestamps,
    no per-tick ids, no body text — keep the prefix as long-lived
    as possible so the cache hits on every tick.
    """
    pinned = _load_pinned_skill(store)
    index = _build_skill_index(store)
    contract = _PLANNER_CONTRACT
    return pinned + "\n\n" + index + "\n\n" + contract


def _load_pinned_skill(store: Store) -> str:
    """Return the verbatim text of the pinned skill (precis-tasks-help)."""
    try:
        from precis.handlers.skill import SkillHandler

        handler = SkillHandler(hub=None)
        resp = handler.get(id=_PINNED_SKILL_ID)
        return resp.body
    except Exception:
        log.exception("planner_prompt: failed to load pinned skill")
        return f"# {_PINNED_SKILL_ID}\n\n(skill load failed — fall back to MCP get)\n"


def _build_skill_index(store: Store) -> str:
    """One-line entry per active skill, derived from ``summary:`` front-matter.

    Reads every shipped skill via :func:`SkillHandler._load_skills_map`
    (the importlib.resources path that works from a wheel) and emits
    a sorted list ``- slug — summary``. Skills missing ``summary:``
    are skipped (with a debug log) rather than emitting a noisy
    ``(no summary)`` placeholder.
    """
    from precis.handlers.skill import _load_skills_map

    skills_map = _load_skills_map()
    if not skills_map:
        log.warning("planner_prompt: no skills loaded")
        return "Available skills: (none — no skills loaded)"
    entries: list[tuple[str, str]] = []
    for slug, raw in skills_map.items():
        fm = parse_frontmatter(raw)
        if fm.status not in (None, "active"):
            continue
        summary = (fm.summary or "").strip()
        if not summary:
            log.debug("planner_prompt: skill %s missing summary", slug)
            continue
        entries.append((slug, summary))
    entries.sort(key=lambda e: e[0])
    if len(entries) > _SKILL_INDEX_MAX:
        entries = entries[:_SKILL_INDEX_MAX]
    lines = [
        "Available skills (call get(kind='skill', id=<slug>) for full "
        "content; search(kind='skill', q='...') to discover by topic):"
    ]
    for slug, summary in entries:
        lines.append(f"- {slug} — {summary}")
    return "\n".join(lines)


#: The planner's operational contract. Stable text — lives in the
#: cached layer. Spelled out explicitly so the LLM knows the four
#: output shapes and the depth discipline before reading the
#: per-tick body. References the runtime skills it can fetch when
#: the per-task discipline matters.
_PLANNER_CONTRACT: str = """\
# Planner contract

You are working on ONE todo. Its body and the results of its prior
children (if any) appear below in the user message. Your job is to
move the work forward by exactly one of these output shapes:

1. **Mint subtasks** via `put(kind='todo', parent_id=<your id>,
   tags=['LLM:<model>'], text='<specific brief>')`. Each child must
   carry exactly one `LLM:opus|sonnet|haiku` tag picking the
   cheapest model that can produce the depth required. Add
   ordering with `link(rel='blocked-by', src=B, dst=A)` for
   sequential pairs; leave unlinked for parallel siblings. You will
   be re-called once all children resolve. Children automatically
   inherit your workspace.

2. **Write the artefact directly** via the workspace-routed
   `put(kind='tex', name='<slug>', text='\\section{...}\\n...')`.
   The system places the file at the right path under your project's
   workspace; you don't compute paths. For figures use
   `put(kind='pic', name='<slug>.svg', text='<svg>')`; for raw data
   `put(kind='data', name='<slug>.csv', text='...')`. The first
   `put` in a fresh workspace auto-creates the directory layout
   (entrypoint `main.tex`, `tex/`, `pics/`, `data/`, `.gitignore`,
   `git init`). Every successful `put` produces a git commit; on
   rollback, `git reset --hard <sha>` returns the workspace to that
   tick's state.

3. **Mint citations** via `put(kind='citation', text='<claim>',
   source_handle='paper:<slug>', source_quote='<verbatim>')` for
   every quantitative claim. Citations stay global so the same paper
   cite is reusable across projects; the system auto-tags your
   citations with `workspace:<your-workspace>` so `refs.bib`
   generation finds them. Write `\\cite{<paper-slug>}` in your tex
   file body; the system resolves it.

4. **Yield to the human** via `tag(id=<your id>, add=
   ['ask-user:<the question>'])`. Use when the work needs a value
   judgement or hard-ambiguity decision only a human can make.

5. **Halt** via `tag(id=<your id>, add=['halt'])` or
   `tag(add=['halt:<reason>'])`. Stronger yield: "do not call me
   again until a human intervenes." Use when genuinely stuck
   (`halt:planner-stuck`) or broken
   (`halt:impossible-as-specified`).

6. **Finish** via `tag(id=<your id>, add=['STATUS:done'])`. Your
   work is done; the parent will read your files on its next tick.

## Runtime context (set in your env by the runner)

- **You are running on the model named in `PRECIS_CURRENT_MODEL`**
  (opus / sonnet / haiku). Use this for degradation/escalation:
  too hard for haiku? mint a child with `LLM:opus`. Sonnet on a
  topic that needs external data? call
  `get(kind='research', q='<question>')` for a perplexity research
  dive, or mint a child with `executor:fetch` to ingest missing
  papers. Opus on a clear task? do it inline.
- **Your parent todo's id is in `PRECIS_CURRENT_TODO`** and
  `put(kind='todo', ...)` auto-defaults `parent_id=` to it. You
  do NOT need to pass `parent_id` when minting subtasks under
  yourself — the system reads the env.
- **Your workspace is in `PRECIS_WORKSPACE`**. Everything you mint
  (todos, citations, files) gets auto-tagged
  `project:<workspace-slug>` so `search(tags=['project:<slug>'])`
  surfaces the full project surface. You don't think about this.

## Files (workspace-routed)

The MCP server handles project infrastructure (layout, gitignore,
git init, main.tex skeleton, refs.bib generation) lazily on first
need. You never think about physical paths.

- `put(kind='tex', name='intro', text='\\section{Introduction}...')`
- `put(kind='tex', name='main', text='...')` — entrypoint (special)
- `put(kind='pic', name='timeline.svg', text='<svg>')`
- `put(kind='data', name='qy-by-node.csv', text='...')`
- `get(kind='tex', id='tex--intro')` — read your section back
- `edit(kind='tex', id='tex--intro~scope', mode='replace',
       text='...')` — block-level edit by slug

The `put` returns one of two verdicts:

- **`ok`**: file landed (possibly with a small mechanical-fix note:
  unicode escapes, missing `\\usepackage{}`, etc — silently fixed).
- **`hint`**: file NOT written; the system has a proposed correction
  for an error it won't auto-resolve without your ack. Read the
  hint, decide if it preserves your intent, resubmit if it does.

**Paper not in corpus**: if you want to cite a paper that isn't in
the corpus yet, mint `put(kind='finding', text='<claim>', ...)` to
flag the gap, AND write `[citation pending]` in your prose. NEVER
write `\\cite{TODO}` or a guessed bib key — it breaks the compile.

**Literature hunt**: if you identify primary sources that you need
but the corpus doesn't have, **DO NOT** write them as a memory
note ("References needed: ..."). Mint a literature-hunt subtask:

  put(kind='todo', tags=['LLM:sonnet'], text='''
  Literature hunt — find and ingest these N papers. For each:
  1. search(kind='paper', q='<title or DOI>') to check the corpus.
  2. If not in corpus, mint
       put(kind='finding', text='<claim>',
           source_handle='<paper:slug-guess>',
           verifier_confidence=0.5)
     The finding_chase worker auto-resolves via Unpaywall / arXiv /
     S2 / EPO OPS.
  3. STATUS:done when all are minted.

  Papers needed:
    1. <citation-style identifier> — <topic>
    2. ...
  ''')

When the chase resolves a paper into the corpus, your parent's
re-tick can `\\cite{<slug>}` it. Reference notes that linger in
memory are PROHIBITED — they trigger nursery flags and the chase
loop never starts.

## STATUS:done has a guardrail

You may **not** tag yourself `STATUS:done` unless one of these is
true: (a) a successful child job exists under this todo,
(b) all your live child todos are STATUS:done / won't-do,
(c) you minted at least one citation tagged with your project,
or (d) you wrote at least one file under your workspace in this
tick (or the last 24h). The guardrail rejects worker-source
`STATUS:done` calls that fail all four; the LLM doesn't get to
declare victory without evidence. If you're genuinely blocked,
use `ask-user:<question>` or `halt:<reason>` instead.

## Depth discipline (the value proposition)

The bar to beat is Perplexity. Every output should be quantified
where possible, cite primary sources via `kind='citation'` refs,
distinguish along the field's natural axes, and flag contradictions
explicitly. Shallow summaries waste the slice.

When you write a child's body, be specific: name the deliverable,
list the considerations, set the depth target. A child body that
could fit one Perplexity query is under-specified.

Skills relevant to *how* to produce depth:

- `precis-decomposition-help` — when to split, when to do it yourself,
  how to size siblings, leaf-deep / root-summary inversion.
- `precis-research-help` — corpus searches, primary-source rule,
  quantification, contradiction flagging.
- `precis-write-paper-help` — claim-level citation density, evidence
  threading, voice.
- `precis-review-paper-help` — adversarial review discipline.

The full skill index above tells you what other territories exist;
call `get(kind='skill', id=<slug>)` for any whose summary matches your
current task shape, or `search(kind='skill', q='<specific question>')`
for novel territory.

## Constraints

- Children inherit a depth budget (`meta.tick_count`,
  `meta.cost_usd`); pathological recursion hits the cap and auto-
  halts. Plan to converge within ~5 ticks per branch.
- Owner-only tags (`level:strategic`, `level:tactical`,
  `level:recurring`) reject from worker source; mint children at
  `level:subtask` (the default) or omit `level:*` entirely.
- Never produce a "high-level summary" at intermediate ticks. Your
  output is the parent's input next tick — preserve detail,
  citations, distinctions. Summaries only at root, and only if
  the original goal asked for one.
"""


# ── variable layer ────────────────────────────────────────────────


def _build_user_prompt(
    store: Store, *, ref_id: int, model: str
) -> str:
    """Build the per-tick user message: ancestry + body + child summaries."""
    from precis.handlers._todo_views import _ancestor_chain

    ancestry = _ancestor_chain(store, ref_id)
    ancestry_block = _render_ancestry_toon(ancestry, leaf_id=ref_id)
    body = _load_ref_body(store, ref_id)
    child_summaries = _load_child_summaries(store, ref_id)
    parts: list[str] = [
        f"You are working on todo #{ref_id}. Model: {model}.",
        "",
        ancestry_block,
        "",
        "## Body",
        body or "(empty)",
    ]
    if child_summaries:
        parts.append("")
        parts.append("## Prior child results")
        parts.append(child_summaries)
    return "\n".join(parts)


def _render_ancestry_toon(
    chain: list[dict[str, object]], *, leaf_id: int
) -> str:
    """Render the ancestor chain as a TOON list with (id, title, from).

    ``chain`` is ``[root, …, leaf]``; the leaf is your own ref. The
    ``from`` column reflects how each ancestor came into the tree —
    today: ``owner`` for refs explicitly tagged ``level:strategic`` /
    ``level:tactical`` / ``level:recurring`` (owner-only tiers), and
    ``planner`` for everything else (subtasks minted by an LLM).
    The runtime doesn't yet record the actual ``set_by`` per
    parent_id assignment, so this is a heuristic; tighten when the
    audit trail exists.
    """
    if not chain:
        return "## Ancestry\n\n(this is a root)"
    lines: list[str] = ["## Ancestry"]
    lines.append("")
    lines.append(f"ancestry: [{len(chain)}]{{id,title,from}}")
    owner_levels = {"level:strategic", "level:tactical", "level:recurring"}
    for entry in chain:
        title = str(entry.get("title", "")).replace("\n", " ").strip()
        if len(title) > 70:
            title = title[:70].rstrip() + "…"
        title = title.replace(",", "‚")  # comma in TOON would break the parser
        level = entry.get("level")
        origin = "owner" if level in owner_levels else "planner"
        lines.append(f"#{entry['id']},{title},{origin}")
    return "\n".join(lines)


def _load_ref_body(store: Store, ref_id: int) -> str:
    """Concatenate all chunk text on ``ref_id`` (excluding job_event).

    For a todo, the "body" is whatever text chunks were attached to
    the ref via the standard ingest path. ``chunk_kind='job_event'``
    rows are forensic logs from prior runs and don't belong in the
    body.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT text
              FROM chunks
             WHERE ref_id = %s
               AND COALESCE(meta->>'chunk_kind', '') != 'job_event'
             ORDER BY ord
            """,
            (ref_id,),
        ).fetchall()
    return "\n".join(str(r[0]) for r in rows if r[0])


def _load_child_summaries(store: Store, ref_id: int) -> str:
    """Concatenate every completed child's ``job_summary`` chunk text.

    Walks the kind='todo' AND kind='job' children of ``ref_id``.
    Todo children: their final state is their last `job_summary`
    chunk (the planner's own output on its last tick). Job
    children: their `job_summary` is the executor's terminal log.

    Result is markdown-headed per child, ordered by ref_id ASC so
    sibling order is stable across ticks (cache-friendly within
    a single tick chain when nothing new lands).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.ref_id, c.kind, c.title,
                   string_agg(ch.text, E'\n' ORDER BY ch.ord) AS summary_text
              FROM refs c
              LEFT JOIN chunks ch ON ch.ref_id = c.ref_id
                                  AND ch.meta->>'chunk_kind' = 'job_summary'
             WHERE c.parent_id = %s
               AND c.deleted_at IS NULL
               AND c.kind IN ('todo', 'job')
             GROUP BY c.ref_id, c.kind, c.title
             ORDER BY c.ref_id
            """,
            (ref_id,),
        ).fetchall()
    parts: list[str] = []
    for child_ref_id, kind, title, summary_text in rows:
        if not summary_text:
            continue
        header = f"### child #{int(child_ref_id)} ({kind}) — {(title or '').splitlines()[0] if title else ''}"
        parts.append(header)
        parts.append(str(summary_text))
        parts.append("")
    return "\n".join(parts).strip()


__all__ = [
    "PlannerPrompts",
    "build_planner_prompts",
]
