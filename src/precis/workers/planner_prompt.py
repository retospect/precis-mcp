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


def build_planner_prompts(store: Store, *, ref_id: int, model: str) -> PlannerPrompts:
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

        handler = SkillHandler(hub=None)  # type: ignore[arg-type]
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

   **Prose style.** Write plain declarative sentences, one idea each.
   No em-dashes (the `—` character): split the thought into separate
   sentences, or use a colon, comma, or parentheses. Do not use bold or
   italics for emphasis; let sentence structure carry the weight.
   Introduce an abbreviation by writing the short form and relying on a
   glossary entry, not by spelling it out inline as `Full Form (ABBR)`.

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
  `get(kind='perplexity-research', q='<question>')` for a perplexity research
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
the corpus yet, **request it** — don't just flag a gap and move on.
Discovery tools find the source; the corpus is the only thing you
cite. Work cheapest-first:

1. **Re-check the corpus** — `search(kind='paper', q=…)`; we may
   already hold it under another slug.
2. **Find the real DOI.** Mine a held paper's citation graph, or
   search by topic — both hand you a resolvable id, no guessing:

       get(kind='semanticscholar', id='refs:<held-doi>')   # papers it cites
       get(kind='semanticscholar', id='cites:<held-doi>')  # papers citing it
       get(kind='semanticscholar', id='<title or topic>')  # search → DOIs
       get(kind='perplexity-research', q='<question>')      # fallback pointer-finder

   Perplexity/websearch only *name* the work — convert the answer to
   a DOI and ingest it; never cite an aggregator as the source.
3. **Got a resolvable id → stub it + park the citing work:**

       put(kind='paper', doi='10.x/y')        # stub → fetch_oa + ingest + embed
       wait = put(kind='todo',
                  text='[auto] wait for 10.x/y ingested+indexed',
                  meta={'auto_check': {'type': 'paper_ingested',
                                       'doi': '10.x/y',
                                       'timeout_at': '<ISO-8601>'}})
       link(kind='todo', id=<your citing todo>, target=f'todo:{wait.id}',
            rel='blocked-by')

   The `paper_ingested` leaf auto-resolves once the paper lands +
   embeds; your re-tick then writes a real `\\cite{<slug>}`.
4. **Only a fuzzy claim, no id?** → `put(kind='finding',
   text='<claim>', ...)` so `finding_chase` resolves it via
   Unpaywall / arXiv / S2 / EPO, then cite on a re-tick.

Either way, write `[citation pending]` in your prose as the
placeholder — but ALWAYS with a stub or finding actually chasing it
behind the scenes (a placeholder nobody is fetching never becomes a
citation, and a lingering "References needed" note is PROHIBITED —
it trips nursery flags). NEVER write `\\cite{TODO}` or a guessed bib
key — it breaks the compile.

**Literature hunt**: if you identify primary sources that you need
but the corpus doesn't have, **DO NOT** write them as a memory
note ("References needed: ..."). Mint a literature-hunt subtask
with an auto-close evaluator so it closes itself when the chase
finishes — no follow-up tick from you needed:

  put(kind='todo',
      tags=['LLM:sonnet'],
      meta={'auto_check': {'type': 'all_child_findings_resolved'}},
      text='''
  Literature hunt — find and ingest these N papers. For each:
  1. search(kind='paper', q='<title or DOI>') to check the corpus.
  2. If not in corpus, mint
       put(kind='finding', text='<claim>',
           source_handle='<paper:slug-guess>',
           verifier_confidence=0.5)
     The finding_chase worker auto-resolves via Unpaywall / arXiv /
     S2 / EPO OPS. No need to tag STATUS:done — your
     `all_child_findings_resolved` auto_check fires when every
     finding reaches a terminal state (established, dead_chain, or
     multi_candidate).

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

## End every tick with a conclusion block

After all your tool calls and before claude -p hands back control,
print a structured conclusion block. The runner extracts it and
embeds the verdict + one-paragraph summary at the top of the
job_result audit chunk, so the parent's next tick reads your synth
*before* it sees the counts. Without this block the parent sees only
"3 subtasks minted, 1 citation minted" and has to read your stdout
to figure out the gist — expensive and lossy.

Format (copy verbatim, fill in the values):

    === TICK CONCLUSION ===
    verdict: done | continue | yield | halt
    summary: One paragraph synthesising what this tick produced —
             what was written, what was cited, what's left for
             the parent to do.
    files: tex/intro.tex, tex/methods.tex
    === END ===

Verdict semantics — informational; the actual state transition is
still your `tag(id=N, add=['STATUS:done'])` / `ask-user:` / `halt:`
call earlier in the tick:

* `done` — you tagged STATUS:done and the parent can read your files
* `continue` — you minted subtasks; the parent will be re-summoned
  once they resolve
* `yield` — you tagged `ask-user:<question>`; awaiting human input
* `halt` — you tagged `halt:<reason>`; needs human intervention

`files:` lists the workspace-relative paths you wrote this tick (or
omit if you wrote none). `summary:` is your gist — what a reader
who skips your stdout still needs to know.

## Writing happens at any level — but the *shape* differs

Writing is not exclusive to leaves. Where in the tree you sit shapes
what you write:

* **Leaves** (no children) write *substrate*: section bodies,
  citations, raw findings. One file per leaf is the typical pattern.
* **Mid-level synthesis nodes** (you minted children, they finished,
  you're re-ticking with their summaries) write *connective tissue*:
  transitions between sibling sections, intro/outro of a multi-part
  topic, updated `\\input{}` ordering in `main.tex`.
* **Strategic root** writes the *frame*: executive summary, outlook,
  the highest-level transitions. Often writing `main.tex`'s prose
  scaffolding around the leaves' `\\input{}` calls.

If you're a mid-level node and your children's summaries already
cover the substrate, your job is the stitching — not minting another
leaf to write a transition. If you're a leaf, your job is the
substrate — not a meta-commentary on the corpus.

Information flow:

* **Downward**: briefs propagate from parent to child via the body
  you write at mint time. Be specific — name the deliverable, depth
  target, the considerations to weigh. A vague brief produces a
  vague leaf.
* **Upward**: leaves produce artefacts (files on disk) + structured
  conclusion summaries (the block above) + minted refs (citations,
  findings). Parents re-tick and see workspace status + children
  status without re-reading the raw stdouts.

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


def _build_user_prompt(store: Store, *, ref_id: int, model: str) -> str:
    """Build the per-tick user message.

    Structure (deliverable-centric, slim):

    1. Identity (id + model)
    2. Ancestry chain (TOON)
    3. Body (the brief — what you're being asked to do)
    4. Workspace status — current file inventory + per-file author/size
       (so you see what's *already written* without re-reading every file)
    5. Children status — id + status + 1-line Result hint per child
       (NOT raw stdout dumps — that's gigantic and unstructured)

    The big change from prior versions: child stdouts are NOT dumped
    into the parent's prompt. We assume each child wrote either a
    file (visible in the workspace status block) or a citation
    (visible via search). The parent's job is to read those if it
    needs them, not to re-process every word the children emitted.
    Saves thousands of tokens per re-tick.
    """
    from precis.handlers._todo_views import _ancestor_chain

    ancestry = _ancestor_chain(store, ref_id)
    ancestry_block = _render_ancestry_toon(ancestry, leaf_id=ref_id)
    project_block = _render_project_brief(store, ref_id)
    body = _load_ref_body(store, ref_id)
    anchor_block = _render_anchor_context(store, ref_id)
    workspace_block = _render_workspace_status(store, ref_id)
    children_block = _render_children_status(store, ref_id)
    parts: list[str] = [
        f"You are working on todo #{ref_id}. Model: {model}.",
        "",
        ancestry_block,
    ]
    if project_block:
        parts.append("")
        parts.append(project_block)
    draft_block = _render_draft_identity(store, ref_id)
    if draft_block:
        parts.append("")
        parts.append(draft_block)
    parts.append("")
    parts.append("## Body")
    parts.append(body or "(empty)")
    if anchor_block:
        parts.append("")
        parts.append(anchor_block)
    if workspace_block:
        parts.append("")
        parts.append(workspace_block)
    if children_block:
        parts.append("")
        parts.append(children_block)
    return "\n".join(parts)


def _render_project_brief(store: Store, ref_id: int) -> str:
    """Surface the project-level brief that rides with the workspace.

    The project's durable guidance ("project thoughts" — voice, scope,
    standing constraints, what *not* to do) lives at
    ``meta.workspace.brief`` on the project root and cascades to every
    descendant via the workspace-inheritance at ``put`` time
    (``Workspace.to_meta`` carries it down). Surfacing it here means a
    deep leaf reads the project frame on every tick without the owner
    having to repeat it in each child's body.

    Belongs in the *variable* (user) layer, not the cached system
    prompt: the brief is per-project, so two projects' planners must
    not share a cache prefix carrying one's brief.

    No-op when there's no workspace, no brief, or the brief is blank.
    """
    from precis.utils.workspace import Workspace

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    if not row:
        return ""
    workspace = Workspace.from_meta(row[0])
    if workspace is None:
        return ""
    brief = workspace.brief.strip()
    if not brief:
        return ""
    slug = workspace.project_tag or "project"
    return f"## Project context ({slug})\n\n{brief}"


def _render_draft_identity(store: Store, ref_id: int) -> str:
    """Tell the agent *which draft it is editing*, when this todo's
    project owns one (a ``draft-of`` link from the draft to any node in
    this todo's ancestry — the link sits on the project root, so we walk
    parents up from the current ref).

    Without this an editor agent has no told-to-it answer for "what draft
    am I in?", and reaches for awkward workarounds — e.g. searching the
    corpus for one of its own chunk handles. Naming the draft + its id up
    front removes that whole class of confusion and points at the read /
    search calls. No-op when no draft is bound to this subtree.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE anc AS (
                SELECT ref_id, parent_id FROM refs WHERE ref_id = %s
                UNION ALL
                SELECT r.ref_id, r.parent_id
                  FROM refs r JOIN anc a ON r.ref_id = a.parent_id
            )
            SELECT l.src_ref_id, dr.title,
                   (SELECT id_value FROM ref_identifiers ri
                     WHERE ri.ref_id = l.src_ref_id AND ri.id_kind = 'cite_key'
                     LIMIT 1) AS slug
              FROM links l JOIN refs dr ON dr.ref_id = l.src_ref_id
             WHERE l.relation = 'draft-of'
               AND l.dst_ref_id IN (SELECT ref_id FROM anc)
               AND dr.deleted_at IS NULL
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    if not row:
        return ""
    _src, title, slug = row
    ident = slug or _src
    return (
        f"## Draft\n\n"
        f"You are editing draft **{title}** (`id={ident}`). Read it with "
        f"`get(kind='draft', id='{ident}')` (outline) and `get(id='dc<id>')` "
        f"(one chunk); search within it via "
        f"`search(kind='draft', q='…', scope='{ident}')`. Address chunks by "
        f"their `dc<id>` handle; cross-reference by embedding `[[dc<id>]]` in prose."
    )


def _render_anchor_context(store: Store, ref_id: int) -> str:
    """Surface the draft chunk a change request is anchored to.

    The web "around here…" box and the per-heading "review ▾" menu file a
    todo carrying ``meta.anchor='dc<id>'`` — *where* the request is
    about. Without surfacing it, the agent only sees the body ("remove
    this paragraph") with no pointer to which chunk, so it (correctly,
    per the ambiguity guidance) yields ``ask-user:`` — the "see chunk 0"
    loop. This block tells the agent exactly which chunk, shows its
    current text, and says to act on it directly.

    No-op when the todo has no ``meta.anchor``. When the anchor points at
    a chunk that no longer exists, say so (so the agent asks a *grounded*
    question by ``dc<id>`` handle rather than guessing)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'anchor' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    handle = ((row[0] if row else None) or "").lstrip("¶").strip()
    if not handle:
        return ""
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return (
            f"## Anchor — requested at {handle}\n\n"
            f"This request is anchored to draft chunk {handle}, but that "
            "chunk no longer exists (retired or never created). Don't guess "
            "at another chunk — yield `ask-user:` with a question that names "
            f"{handle} and asks what to target instead."
        )
    draft = store.get_ref(kind="draft", id=int(chunk.ref_id))
    dident = draft.slug if draft and draft.slug else chunk.ref_id
    text = (chunk.text or "").strip()
    if len(text) > 1500:
        text = text[:1500].rstrip() + "…"
    quoted = "\n".join("> " + ln for ln in text.splitlines()) or "> (empty)"
    parts = [
        f"## Anchor — requested at {handle}\n",
        f"This change request is anchored to chunk **{handle}** of "
        f"`draft:{dident}` (a {chunk.chunk_kind}). Act on THIS chunk "
        "directly — edit / delete / cite it by its handle; don't ask which "
        "one it is. Its current text:\n",
        quoted,
    ]
    # What's already linked to this chunk — provenance, related thoughts,
    # and dream-memories — so the agent works *with* the existing context
    # (cite the linked source, build on the dream) instead of blind.
    conns = store.chunk_connections(int(chunk.ref_id), [handle]).get(handle, [])
    if conns:
        parts.append("\nLinked to this chunk (use as context / sources):")
        for c in conns[:12]:
            desc = f" — {c['title']}" if c.get("title") else ""
            parts.append(f"- {c['kind']}:{c['ident']} ({c['relation']}){desc}")
    parts.append(
        "\nIf you still must ask the user something, reference chunks by "
        'their `dc<id>` handle (never "chunk 0").'
    )
    return "\n".join(parts)


def _render_workspace_status(store: Store, ref_id: int) -> str:
    """List files already written under this todo's workspace.

    Reads the workspace path from this ref's ``meta.workspace`` and
    walks the on-disk dir. Returns a slim listing — one line per
    file with size + age — so the parent re-tick sees the state of
    the deliverable without reading every file. If the file is
    relevant, the LLM can ``get(kind='tex', id='tex--<slug>')`` it
    explicitly.

    No-op when there's no workspace on the ref. No-op when the dir
    doesn't exist yet (init hasn't fired).
    """
    import os
    from pathlib import Path

    from precis.utils.workspace import Workspace

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    if not row:
        return ""
    workspace = Workspace.from_meta(row[0])
    if workspace is None:
        return ""
    precis_root_str = os.environ.get("PRECIS_ROOT", "")
    if not precis_root_str:
        return ""
    ws_root = workspace.absolute_root(Path(precis_root_str))
    if not ws_root.exists():
        return ""
    lines: list[str] = [
        "## Workspace status",
        "",
        f"Workspace: {workspace.path} (format={workspace.format}, "
        f"entrypoint={workspace.entrypoint})",
        "",
        "Files present:",
    ]
    found = False
    for sub in ("", "tex", "sections", "pics", "data"):
        sub_root = ws_root / sub if sub else ws_root
        if not sub_root.is_dir():
            continue
        for entry in sorted(sub_root.iterdir()):
            if entry.is_dir() or entry.name.startswith("."):
                continue
            rel = entry.relative_to(ws_root)
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            kind_hint = ""
            if entry.suffix == ".tex":
                kind_hint = "  (get via id='tex--" + entry.stem + "')"
            elif entry.suffix == ".md":
                kind_hint = "  (get via id='markdown--" + entry.stem + "')"
            lines.append(f"  - {rel} ({size} bytes){kind_hint}")
            found = True
    if not found:
        lines.append("  (none yet — workspace empty)")
    return "\n".join(lines)


def _render_children_status(store: Store, ref_id: int) -> str:
    """Per-child digest: id, kind, STATUS, last result hint.

    Slim replacement for the prior child-stdout dump. Reads each
    child's most recent ``chunk_kind='job_result'`` chunk if
    present (~200 token structured summary). Falls back to a single
    line stating "no result chunk yet" for never-ticked children.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.ref_id, c.kind, c.title,
                   (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1
                   ) AS status,
                   (SELECT ch.text FROM chunks ch
                     WHERE ch.ref_id = c.ref_id
                       AND ch.meta->>'chunk_kind' = 'job_result'
                     ORDER BY ch.ord DESC LIMIT 1
                   ) AS last_result
              FROM refs c
             WHERE c.parent_id = %s
               AND c.deleted_at IS NULL
               AND c.kind IN ('todo', 'job')
             ORDER BY c.ref_id
            """,
            (ref_id,),
        ).fetchall()
    if not rows:
        return ""
    lines: list[str] = ["## Children", ""]
    for child_ref_id, kind, title, status, last_result in rows:
        title_one_line = (title or "").splitlines()[0][:80]
        lines.append(
            f"#{int(child_ref_id)} ({kind}, STATUS:{status or 'open'}) — "
            f"{title_one_line}"
        )
        if last_result:
            for ln in str(last_result).splitlines()[:8]:
                lines.append(f"    {ln}")
        else:
            lines.append("    (no job_result yet — child hasn't ticked or is mid-tick)")
        lines.append("")
    lines.append(
        "To read a child's full output: get(kind='todo', id=N) for the body, "
        "or get(kind='job', id=M) for the job's stdout chunk."
    )
    return "\n".join(lines)


def _render_ancestry_toon(chain: list[dict[str, object]], *, leaf_id: int) -> str:
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


#: Chunk kinds that are NOT part of a ref's brief. Forensic job logs,
#: per-tick conclusions, the card-search mirror, and ``tag_overflow``
#: spillover — the last is the planner's OWN long ``ask-user:`` /
#: ``halt:`` value redirected onto the ref when it was too long to
#: store as a tag (``TodoHandler`` ``_TAG_VALUE_REDIRECT_THRESHOLD``).
#: Excluded so a re-tick never reads its own prior output back as the
#: brief.
_NON_BODY_CHUNK_KINDS: tuple[str, ...] = (
    "job_event",
    "job_result",
    "job_summary",
    "tag_overflow",
    "card_combined",
)


def _load_ref_body(store: Store, ref_id: int) -> str:
    """Return the ref's brief: ``refs.title`` plus any genuine body chunks.

    A todo stores its entire brief in ``refs.title``
    (``insert_ref(title=text)``) and emits no ``card_combined`` chunk,
    so for a todo the brief lives ONLY in the title. The previous
    implementation read body text exclusively from ``chunks`` and so
    handed the planner an empty ``## Body`` for every todo — it planned
    off nothing but the 70-char title fragment in the ancestry block.
    We now lead with the title (the canonical todo body) and append any
    real ingested body chunks, skipping derived/forensic kinds
    (``_NON_BODY_CHUNK_KINDS``).
    """
    with store.pool.connection() as conn:
        title_row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        chunk_rows = conn.execute(
            """
            SELECT text
              FROM chunks
             WHERE ref_id = %s
               AND chunk_kind != ALL(%s)
             ORDER BY ord
            """,
            (ref_id, list(_NON_BODY_CHUNK_KINDS)),
        ).fetchall()
    title = str(title_row[0]).strip() if title_row and title_row[0] else ""
    chunk_text = "\n".join(str(r[0]) for r in chunk_rows if r[0]).strip()
    return "\n\n".join(p for p in (title, chunk_text) if p)


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
