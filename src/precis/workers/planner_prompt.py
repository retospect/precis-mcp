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
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.handlers._skill_common import parse_frontmatter
from precis.utils.prompt import (
    AssemblyContext,
    ClaudeAgentAdapter,
    Layer,
    Module,
    assemble,
    doc_context_table,
    kinds_table,
    section_review_block,
    tools_table,
)

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


#: The thread **persona** rides first in the cached system prompt verbatim
#: (ADR 0051 §2) — the floor that states how the thread works. Selected per
#: thread type from :data:`~precis.workers.thread_persona.THREAD_PERSONAS`;
#: the default ``write-document`` persona is the operational manual
#: ``precis-tasks-help``, so the cached floor is byte-identical to the
#: pre-A2 pinned skill. Other skills are summary-only in the index (personas
#: excluded, §2) and pulled on demand via MCP ``get``.


#: Hard cap on the skill index. Each entry costs ~40 tokens; at the
#: current corpus (~90 active skills) the full index renders at ~3.7k
#: tokens, so the cap keeps the cached system prompt under ~5k tokens
#: even as the corpus grows. Sized to admit the whole active set today
#: (a purely alphabetical truncation would otherwise drop late-sorted
#: core skills like ``precis-tasks-help``). If a planner needs a skill
#: beyond the cap it calls ``search(kind='skill', q='…')`` — that's the
#: discovery mechanism by design.
_SKILL_INDEX_MAX: int = 120


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

    Pinned skill + skill index + tools + kinds + planner contract — the
    cached layer (ADR 0038 §1). Assembled from :data:`_CACHED_MODULES`
    via the shared assembler so the planner shares one prompt surface
    with the editor/reviewers (migration step 1). No timestamps, no
    per-tick ids, no body text — the prefix stays long-lived so the
    cache hits on every tick. Tolerates ``store=None`` (no cached module
    dereferences it)."""
    ctx = AssemblyContext(store=store, ref_id=0, model="")
    system, _ = ClaudeAgentAdapter.render(assemble(_CACHED_MODULES, ctx))
    return system


def _load_skill_verbatim(skill_id: str, store: Store | None = None) -> str:
    """Return the verbatim body of a skill (the persona floor, ADR 0051 §2).

    ``store`` is accepted for a uniform module-builder signature but
    unused — skills are file-backed (loaded via importlib). A load failure
    degrades to a stub rather than crashing the tick, so a thread never runs
    persona-less."""
    try:
        from precis.handlers.skill import SkillHandler

        handler = SkillHandler(hub=None)  # type: ignore[arg-type]
        resp = handler.get(id=skill_id)
        return resp.body
    except Exception:
        log.exception("planner_prompt: failed to load persona skill %s", skill_id)
        return f"# {skill_id}\n\n(skill load failed — fall back to MCP get)\n"


def _build_skill_index(store: Store | None = None) -> str:
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
        # Personas (ADR 0051 §2) are pinned as a thread's floor, not
        # on-demand reference docs — keep them out of the discovery menu.
        if fm.flavor == "persona":
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

2. **Write the artefact directly.** *If this project owns a draft*
   (you will see a `## Draft` block below in this message), that draft
   is the deliverable: write prose **into the draft** with
   `put(kind='draft', …)` / `edit(id='dc<id>', …)` exactly as that
   block describes — the `.tex` is regenerated from the draft, so the
   file kinds below are gated off for you this tick. Otherwise (a plain
   tex workspace with no draft), write the artefact via the
   workspace-routed
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
   **Precise terms propagate — coin them.** When you pin down the precise
   term for something (not a vague description), *define it in the glossary*
   (`put(kind='draft', chunk_kind='term', text='<long form>',
   meta={'short':'<SHORT>'})`) rather than only using it locally: the
   glossary is surfaced into every section's prompt and auto-linked
   throughout the document, so one definition keeps the whole draft
   consistent and makes the term findable from every other section. A
   precise term is a strong search key, so defining it also improves recall
   everywhere — depositing it in the shared vocabulary is how a discovery in
   one section reaches the others, without hand-carrying it.
   **Units and temperatures** are plain text with the literal Unicode
   sign and no space: write `63°C` (digit, then `°C`), a range as
   `63–65°C`, and a tolerance as `±1°C` (the `±` sign, not `+/-`). Never
   use a superscript, the single-character `℃`, or LaTeX (`^\\circ`,
   `\\degree`, `\\textdegree`) — and don't spell it out as
   "63 degrees Celsius".

3. **Cite by paper-chunk handle.** When a claim rests on a source,
   write the supporting chunk's **bare handle** inline in your prose:
   `[pc234]` (paper chunk 234). For several supporting chunks — in one
   paper or across papers — list them: `[pc232][pc234][pc593]`. Cite
   the **specific chunk** that bears the fact, not the paper as a whole.
   The handle is a value you **copy from `search`/`get` output**, never
   construct: locate the passage (`search(kind='paper', q='…')` or the
   paper's TOC), read it to confirm it supports the claim, then paste
   its `pc<id>`. See `get(kind='skill', id='precis-citation-help')`.

   The export engine turns each `pc<id>` into the right citation and
   **one bibliography entry per paper** at compile time. So **never**
   hand-write LaTeX citation commands or a bibliography key in your
   prose — those are export-only output, not something you type. A
   made-up key matches nothing in the generated bibliography and
   silently breaks it. You write handles; precis writes the citations.

   Patents cite the same way, by their chunk handle (`pk<id>`). A
   **memory or thought is a link, not a citation**: write `[me<id>]` to
   record provenance — it joins the graph but never enters the
   bibliography (citations are to the literature, not to our own notes).
   For a claim whose primary source you still need, register a
   `kind='finding'` and cite its handle `[fi<id>]` until the chase
   resolves it (`get(kind='skill', id='precis-finding-help')`).

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
- **Broken tool? File a gripe.** If a precis tool errors, returns
  wrong results, or actively fights you, `search(kind='gripe',
  q='<the symptom>')` first (avoid dupes), then — if it's new —
  `put(kind='gripe', text='<what broke, one sentence>')`. Then carry
  on with your task; don't silently work around a broken tool.

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
   embeds; your re-tick then cites it by a chunk handle `[pc<id>]`.
4. **Only a fuzzy claim, no id?** → `put(kind='finding',
   text='<claim>', ...)` so `finding_chase` resolves it via
   Unpaywall / arXiv / S2 / EPO, then cite on a re-tick.

Either way, write `[citation pending]` in your prose as the
placeholder — but ALWAYS with a stub or finding actually chasing it
behind the scenes (a placeholder nobody is fetching never becomes a
citation, and a lingering "References needed" note is PROHIBITED —
it trips nursery flags). Once it lands, cite it by its chunk handle
`[pc<id>]`. NEVER hand-write LaTeX citation commands or a bibliography
key — you write handles, the export engine writes the citations.

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
       put(kind='finding',
           text='<claim — include the paper title/DOI so chase can find it>',
           verifier_confidence=0.5)
     (No source_handle: the paper isn't in the corpus yet, so there is
     no pc<id> handle to copy — never guess one.)
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
re-tick cites it by a chunk handle `[pc<id>]`. Reference notes that
linger in memory are PROHIBITED — they trigger nursery flags and the
chase loop never starts.

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
where possible, cite primary sources by their paper-chunk handle
`[pc<id>]` (copied from search/get — never a bare number or a guessed
handle), distinguish along the field's natural axes, and flag
contradictions explicitly. Shallow summaries waste the slice.

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

    Assembled from :data:`_VARIABLE_MODULES` via the shared assembler
    (ADR 0038 migration step 1). Each block keeps its prior text — the
    optional ones self-gate by returning ``""``, which the assembler
    drops — and a new ``doc_context`` table rides after the anchor when
    one is set.
    """
    ctx = AssemblyContext(store=store, ref_id=ref_id, model=model)
    _, user = ClaudeAgentAdapter.render(assemble(_VARIABLE_MODULES, ctx))
    return user


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


def _render_patent_authoring(store: Store, ref_id: int) -> str:
    """The patent-authoring loop block (gated by ``is_patent``).

    A patent is written *against the prior art*, so this leads the tick with
    the freedom-to-operate loop: sweep prior art → ingest → sync register →
    claim around what is already claimed → log the scoping decisions. The
    prior-art claims themselves arrive via the tick's working set (the
    ``patent_digest`` claims digest stamped on ``meta.working_set``); this
    block is the *methodology* that reads them. See
    ``docs/design/patent-authoring-loop.md`` and the ``patent-*`` skills."""
    return (
        "## Patent authoring — write against the prior art\n\n"
        "You are drafting a **patent**. Do not write in a vacuum; each tick:\n\n"
        "1. **Sweep prior art.** Derive queries from the current description "
        "and run `search(kind='patent', source='remote', q='…')` — it returns "
        "EPO OPS hits you do not already hold. For each *material* hit, "
        "`get(kind='patent', id='<docdb-id>')` to ingest it (synchronous). "
        "Keep pulls bounded (a few per tick — each `get` persists a real "
        "patent); say what you ingested in your summary.\n"
        "2. **Sync the register.** Revise the description so its terminology "
        "matches the patents you pulled — a patent should read like the "
        "field's patents. Skills: `get(kind='skill', id='patent-description')`, "
        "`patent-prior-art`.\n"
        "3. **Claim against freedom to operate.** Before claiming a scope, "
        "check the prior-art **claims** (your working set below shows them, "
        "independent claims verbatim): *if a scope is already claimed, you "
        "cannot claim it* — design around it. Reference a prior-art patent by "
        "its patent-chunk handle `[pk…]` (**not** `[pc…]`, which is a paper). "
        "Skill: `get(kind='skill', id='patent-claim')`.\n"
        "4. **Keep the scoping ledger.** Maintain a `plan` for this project as "
        "a freedom-to-operate record. **Read it first** "
        "(`get(kind='plan', id='<project-slug>')`) so you do not re-propose "
        "scope already ruled out. When you decline or narrow a claim because a "
        "specific prior-art claim blocks it, log the decision pointing at that "
        "claim: `put(kind='plan', id='<slug>', text='considered X; narrowed "
        "because [pk…~n] already claims it', status='done')`. This ledger is "
        "internal reasoning — it never exports.\n"
    )


def _render_plan_ledger(store: Store, plan_ref_id: int, slug: str) -> str:
    """Inject the project's ``plan`` (ADR 0051 §2b) — its reasoning /
    decision ledger — so a tick respects recorded decisions without having
    to fetch it. For a patent this is the freedom-to-operate scoping ledger:
    what was declined or narrowed, and why (``docs/design/
    patent-authoring-loop.md``). One line per node, capped for the flow."""
    try:
        chunks = store.reading_order(plan_ref_id)
    except Exception:  # pragma: no cover — no plan / store hiccup
        return ""
    entries: list[str] = []
    for c in chunks:
        text = (getattr(c, "text", "") or "").strip().replace("\n", " ")
        if text:
            entries.append(f"- {text[:240]}")
    if not entries:
        return ""
    return "\n".join(
        [
            f"## Project plan — decision ledger ({slug})",
            "",
            "Decisions and reasoning recorded for this project — respect them; "
            "do not re-propose scope already ruled out. Record new decisions "
            "with `put(kind='plan', …)`.",
            "",
            *entries,
        ]
    )


def _render_seeds(store: Store, ref_id: int) -> str:
    """Surface the human's seed reading-list for this project.

    A draft can be created (web new-draft form) with free-text seed
    notes + topic tags — people, ORCIDs, lab capabilities, relevant
    papers, key topics — the author flagged as starting material. Stored
    at ``meta.workspace.extra['seeds']`` on the project root and cascaded
    to descendants via the workspace inheritance, so the planner sees it
    on every tick. Variable layer (per-project), like the project brief.

    Tells the planner to work outside-in: read the linked call (if any)
    first, then the flagged seeds, then the rest of the corpus — and to
    chase a named-but-unheld paper rather than invent it.

    No-op when no seeds are set.
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
    seeds = workspace.extra.get("seeds")
    if not isinstance(seeds, dict):
        return ""
    text = str(seeds.get("text") or "").strip()
    raw_tags = seeds.get("tags") or []
    tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    if not text and not tags:
        return ""

    lines = [
        "## Seed material to read first",
        "",
        "Before you draft, work outside-in: read the linked "
        "call-for-proposal in full first (if any — see the requirements "
        "block above), then pull in the source material the human "
        "flagged below, then search the rest of the corpus for whatever "
        "else the claims need.",
    ]
    if text:
        lines += [
            "",
            "Author's seed notes (people, ORCIDs, lab capabilities, "
            "relevant papers, key topics):",
            "",
            text,
        ]
    if tags:
        lines += ["", "Seed topics/tags: " + ", ".join(tags)]
    lines += [
        "",
        "For each seed: `search(kind='paper', q='…')` (and a plain "
        "`search(q='…')`) the corpus. For a named paper not yet held, "
        "stub it (`put(kind='paper', doi='…')`) or mint a "
        "`kind='finding'` so the fetcher ingests it, then cite the "
        "landed chunk with `[pc<id>]`. Never invent a source.",
    ]
    return "\n".join(lines)


def bound_draft(store: Store, ref_id: int) -> tuple[str, str, str] | None:
    """Resolve the draft bound to this todo's subtree, if any.

    A project's draft is linked by a ``draft-of`` edge from the draft to
    a node in this ref's ancestry (the link sits on the project root, so
    we walk parents up from ``ref_id``). Returns ``(ident, title, fmt)``
    where ``ident`` is the draft's ``cite_key`` slug or its bare ref_id
    (whatever ``put(kind='draft', id=…)`` accepts), ``title`` is the
    draft heading, and ``fmt`` is the workspace format (``'tex'`` /
    ``'md'``, defaulting to ``'tex'``). ``None`` when no live draft is
    bound to the subtree.

    Single source of truth for "is this a draft-editing tick?" — used by
    :func:`_render_draft_identity` (to name the draft in the prompt) and
    by ``plan_tick`` (to gate the colliding prose-file kind off the tool
    surface). The same predicate covers the planner's section-writing
    children *and* the "around here…" anchored change-request ticks,
    since both run under the project subtree.
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
                     LIMIT 1) AS slug,
                   dr.meta->'workspace'->>'format' AS fmt
              FROM links l JOIN refs dr ON dr.ref_id = l.src_ref_id
             WHERE l.relation = 'draft-of'
               AND l.dst_ref_id IN (SELECT ref_id FROM anc)
               AND dr.deleted_at IS NULL
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    if not row:
        return None
    src, title, slug, fmt = row
    return (str(slug or src), title, (fmt or "tex"))


def _render_draft_identity(store: Store, ref_id: int) -> str:
    """Tell the agent *which draft it is editing* and how to write into
    it, when this todo's project owns one (resolved via
    :func:`bound_draft`).

    Without this an editor agent has no told-to-it answer for "what draft
    am I in?", and reaches for awkward workarounds — e.g. searching the
    corpus for one of its own chunk handles, or (the failure this fixes)
    writing the section to a freestanding ``kind='tex'`` file the draft
    never renders. Naming the draft + the body-write verbs up front
    removes that whole class of confusion. The colliding prose-file kind
    is *also* gated off the tool surface for this tick (``plan_tick`` sets
    ``PRECIS_KINDS_DISABLED``), so this block is the positive half: where
    to write, not a prohibition. No-op when no draft is bound.
    """
    resolved = bound_draft(store, ref_id)
    if resolved is None:
        return ""
    ident, title, _fmt = resolved
    return (
        f"## Draft\n\n"
        f"You are editing draft **{title}** (`id={ident}`). **This draft is "
        f"this project's deliverable** — its chunks are the canonical, "
        f"editable source, and both the web reader and the PDF export render "
        f"*the draft's chunks* (the `.tex` is export-only output, regenerated "
        f"from the draft). Read it with `get(kind='draft', id='{ident}')` "
        f"(outline) and `get(id='dc<id>')` (one chunk); search within it via "
        f"`search(kind='draft', q='…', scope='{ident}')`. Address chunks by "
        f"their `dc<id>` handle; cross-reference anything by embedding its "
        f"handle in brackets — `[dc<id>]` (a chunk), `[me<id>]` (a memory) — in "
        f"prose.\n\n"
        f"**Write prose INTO this draft.** Add a paragraph under a heading "
        f"with `put(kind='draft', id='{ident}', chunk_kind='paragraph', "
        f"text='…', at={{'into': 'dc<heading>', 'last': True}})`; add a "
        f"section heading with `put(kind='draft', id='{ident}', "
        f"chunk_kind='heading', text='…', at={{'after': 'dc<id>'}})`; revise "
        f"an existing chunk in place with `edit(id='dc<id>', text='…')`. When "
        f"you decompose into section subtasks, tell each child to write its "
        f"section **into draft `{ident}`** under the relevant `dc<id>` "
        f"heading."
    )


#: Soft cap on call-for-proposal section headings surfaced in the
#: requirements block. A CFP with more required sections than this is
#: unusual; we list the first N and point at the full TOC.
_CFP_HEADINGS_CAP = 40


def _render_requirements(store: Store, ref_id: int) -> str:
    """Inject the call-for-proposal a proposal project must satisfy.

    A proposal-project root carries a ``has-requirement`` link to the
    ingested ``kind='cfp'`` document (ADR: proposal-writing). We walk
    parents up from ``ref_id`` (the link sits on the project root) and,
    when one is found, render a ``## Proposal requirements`` block: the
    CFP's slug (so the planner can read it in full) plus its section
    headings (the required structure the draft must mirror, and where the
    word limits live).

    Belongs in the *variable* (user) layer, not the cached system prompt:
    the requirements are per-project, so two proposals' planners must not
    share a cache prefix carrying one's CFP.

    No-op when no CFP is linked into the subtree.
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
            SELECT cf.ref_id, cf.title,
                   (SELECT id_value FROM ref_identifiers ri
                     WHERE ri.ref_id = cf.ref_id AND ri.id_kind = 'cite_key'
                     LIMIT 1) AS slug
              FROM links l JOIN refs cf ON cf.ref_id = l.dst_ref_id
             WHERE l.relation = 'has-requirement'
               AND l.src_ref_id IN (SELECT ref_id FROM anc)
               AND cf.kind = 'cfp'
               AND cf.deleted_at IS NULL
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
        if not row:
            return ""
        cfp_ref_id, title, slug = row
        ident = str(slug or cfp_ref_id)
        headings = conn.execute(
            """
            SELECT text FROM chunks
             WHERE ref_id = %s AND chunk_kind = 'heading'
               AND COALESCE(text, '') <> ''
             ORDER BY ord
             LIMIT %s
            """,
            (cfp_ref_id, _CFP_HEADINGS_CAP + 1),
        ).fetchall()

    lines = [
        "## Proposal requirements",
        "",
        f"This project answers the call-for-proposal **{title}** "
        f"(`kind='cfp'`, `id={ident}`). It is the **spec** — the "
        f"requirements your proposal draft must satisfy. **Do not cite "
        f"it as evidence**; it defines what to write, not a source to "
        f"quote. Read it in full with `get(kind='cfp', id='{ident}')` "
        f"and `get(kind='cfp', id='{ident}', view='toc')`; search it with "
        f"`search(kind='cfp', q='…', scope='{ident}')`.",
    ]
    if headings:
        shown = headings[:_CFP_HEADINGS_CAP]
        lines += [
            "",
            "Required sections (from the call's headings — create one "
            "draft section per relevant requirement, and stamp each "
            "section's word limit via "
            "`edit(id='dc<heading>', word_target={'min':…,'max':…})`):",
            "",
        ]
        lines += [f"- {str(h[0]).strip()}" for h in shown]
        if len(headings) > _CFP_HEADINGS_CAP:
            lines.append(
                f"- … (+more — read the full TOC via "
                f"`get(kind='cfp', id='{ident}', view='toc')`)"
            )
    return "\n".join(lines)


#: Soft cap on glossary lines in the prompt — beyond this we list the
#: count and the first N (a glossary this large is itself a smell).
_GLOSSARY_PROMPT_CAP = 80


def _render_glossary(store: Store, ref_id: int) -> str:
    """List the draft's active abbreviations + definitions to the editor.

    The abbreviation system is otherwise purely *reactive* — it nags about
    undefined acronyms only *after* a write (``_write_abbrev_hints``).
    Surfacing the whole live glossary up front lets the agent write *with*
    the established vocabulary: use the canonical short, don't redefine an
    existing term, and read what an acronym means in a chunk it's editing.

    Belongs in the *variable* (user) layer, not the cached system prompt:
    the glossary mutates as terms are added, so two drafts (and the same
    draft over time) must not share a cache prefix carrying one's terms.

    No-op when no draft is bound to this subtree, or it has no terms.
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
            SELECT l.src_ref_id FROM links l
             WHERE l.relation = 'draft-of'
               AND l.dst_ref_id IN (SELECT ref_id FROM anc)
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    if not row:
        return ""
    terms = store.draft_terms(int(row[0]))  # {handle: (short, long)}
    rows = sorted(
        ((short, long, h) for h, (short, long) in terms.items() if short),
        key=lambda t: t[0].lower(),
    )
    if not rows:
        return ""
    shown = rows[:_GLOSSARY_PROMPT_CAP]
    lines = [
        "## Glossary (active abbreviations — use these, do not redefine)",
        "",
        "Just write the short form as plain text — write `MOF` (and the plural "
        "`MOFs`) naturally; the exporter auto-links every occurrence to its "
        "glossary entry (first use expands, later uses abbreviate). No handle "
        "needed. **Define a new term** once with `put(kind='draft', "
        "id='<slug>', chunk_kind='term', text='<long form>', "
        "meta={'short':'<SHORT>'})`; it files under the Glossary heading. The "
        "`handle` column below is only for editing/moving a term, not for "
        "citing it in prose.",
        "",
        f"glossary: [{len(shown)}]{{short,long,handle}}",
    ]
    for short, long, h in shown:
        lg = (long or "").replace("\n", " ").strip()
        if len(lg) > 70:
            lg = lg[:70].rstrip() + "…"
        # commas would break the TOON row parser → swap for U+201A
        lines.append(f"{short.replace(',', '‚')},{lg.replace(',', '‚')},{h}")
    if len(rows) > _GLOSSARY_PROMPT_CAP:
        lines.append(f"(… and {len(rows) - _GLOSSARY_PROMPT_CAP} more terms)")
    return "\n".join(lines)


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


def _render_heading_intent(store: Store, anchor_handle: str) -> str:
    """Surface the **heading-intent** context for an anchored draft tick (slice
    8b.2): the *breadcrumb up* (why this section exists, root→here) and the *sibling
    intents across* (the placement boundary — what belongs elsewhere). It is the
    writer's **hierarchical prompt** — a leaf that can see "this section exists to
    support §2's argument" writes to that purpose instead of drifting.

    The notes are model-owned **guidance, not document content**: rendered as keyed
    meta with an explicit do-not-transcribe caveat (the reader-asymmetry boundary),
    and they never export (a separate ``memory`` ref, not a draft chunk).

    No-op when the tick has no anchor or the section carries no intents anywhere in
    its breadcrumb/siblings. Fallback-safe: any resolution failure degrades to ``""``
    so it can never break the planner prompt."""
    from precis.backfill.heading_intent import Rung, section_intents

    try:
        ctx = section_intents(store, anchor_handle)
    except Exception:
        log.debug("heading-intent render failed for %r", anchor_handle, exc_info=True)
        return ""
    if not ctx:
        return ""

    def _line(rung: Rung, *, cursor: bool = False) -> str:
        # Key the line off the heading *title* (the position the hierarchy already
        # carries), not the bare handle; the intent adds the purpose the title
        # doesn't spell out.
        title = " ".join((rung.title or rung.handle).split()) or rung.handle
        text = " ".join((rung.intent.text or "").split())
        if len(text) > 160:
            text = text[:160].rstrip() + "…"
        strength = "hard" if rung.intent.hard else "soft"
        marker = "▸ " if cursor else "  "
        return f"{marker}{title} · {strength} · {text}"

    parts = [
        "## Section intent (guidance — shapes what you write; do NOT transcribe "
        "into the prose)",
    ]
    if ctx.breadcrumb:
        parts.append("")
        parts.append("Why this section exists (root → here):")
        last = len(ctx.breadcrumb) - 1
        parts.extend(
            _line(intent, cursor=(i == last)) for i, intent in enumerate(ctx.breadcrumb)
        )
    if ctx.siblings:
        parts.append("")
        parts.append(
            "Sibling sections (the boundary — a thing belongs *there*, not here, "
            "when it fits their job better):"
        )
        parts.extend(_line(intent) for intent in ctx.siblings)
    parts.append(
        "\nKeep it current: if new information changes what THIS section is *for*, "
        "update its intent. A **soft** intent evolves freely; changing a **hard** "
        "intent is a structural decision — flag it, don't silently rewrite. If a "
        "discovery belongs under a sibling, record it there, not here."
    )
    return "\n".join(parts)


def _render_section_style(store: Store, ref_id: int) -> str:
    """Inject the section-style skill for the chunk this tick is anchored to
    (ADR 0037/0038).

    When the change-request's ``meta.anchor`` points at a draft chunk, find
    the nearest enclosing styled heading (``store.section_style_for``) and
    surface that style's skill body, so the editor authors the section in
    the right register (e.g. the ``patent-claim`` rules) without hunting for
    it. No-op when there's no anchor or no enclosing styled section.
    Degrades to a pointer when the style names a skill not yet installed
    (the catalogue styles are not all shipped as skill files)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'anchor' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    handle = ((row[0] if row else None) or "").lstrip("¶").strip()
    if not handle:
        return ""
    style = store.section_style_for(handle)
    if not style:
        return ""
    try:
        from precis.handlers.skill import SkillHandler

        body = SkillHandler(hub=None).get(id=style).body  # type: ignore[arg-type]
    except Exception:
        return (
            f"## Section style — {style}\n\n"
            f"This section uses the **{style}** style; load it with "
            f"`get(kind='skill', id='{style}')` and follow it."
        )
    return (
        f"## Section style — {style}\n\n"
        f"You are working inside a **{style}**-styled section. Follow this "
        f"style:\n\n{body}"
    )


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
#: ``halt:`` value redirected onto the ref when it was too long /
#: whitespaced to store as a tag (see
#: ``handlers._tag_redirect.redirect_long_tag_values``).
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


# ── module library (ADR 0038) ─────────────────────────────────────
#
# The planner's blocks expressed as assembler modules. Each builder is a
# thin ``(ctx) -> str`` wrapper over the ``_render_*`` / ``_build_*``
# functions above, so the emitted text is unchanged — the assembler only
# orders the blocks and the adapter splits them by cache layer. New to
# the planner (vs the old hand-rolled concatenation): the cached
# ``tools`` + ``kinds`` legend tables and the variable ``doc_context``
# table (gated on an anchored change-request).


def _m_persona(ctx: AssemblyContext) -> str:
    """The thread's persona floor (ADR 0051 §2) — first cached block.

    Selects the persona skill from the registry by the tick's
    ``thread_type`` (``extras['thread_type']``); absent — as in the current
    thread-type-invariant cached layer — it falls back to the default
    ``write-document`` persona (``precis-tasks-help``), reproducing the
    pre-A2 pinned-skill bytes exactly."""
    from precis.workers.thread_persona import persona_for

    spec = persona_for(ctx.extras.get("thread_type"))
    return _load_skill_verbatim(spec.persona_skill_id, ctx.store)


def _m_skill_index(ctx: AssemblyContext) -> str:
    return _build_skill_index(ctx.store)


def _m_tools(ctx: AssemblyContext) -> str:
    return tools_table()


def _m_kinds(ctx: AssemblyContext) -> str:
    return kinds_table()


def _m_contract(ctx: AssemblyContext) -> str:
    return _PLANNER_CONTRACT


def _m_identity(ctx: AssemblyContext) -> str:
    return f"You are working on todo #{ctx.ref_id}. Model: {ctx.model}."


def _m_ancestry(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    from precis.handlers._todo_views import _ancestor_chain

    chain = _ancestor_chain(ctx.store, ctx.ref_id)
    return _render_ancestry_toon(chain, leaf_id=ctx.ref_id)


def _m_project(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_project_brief(ctx.store, ctx.ref_id)


def _m_patent(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_patent_authoring(ctx.store, ctx.ref_id)


def _m_plan(ctx: AssemblyContext) -> str:
    # The ``has_plan`` predicate memoised the resolved (plan_ref_id, slug).
    pp = ctx.extras.get("project_plan")
    if not pp:
        return ""
    assert ctx.store is not None
    return _render_plan_ledger(ctx.store, pp[0], pp[1])


def _m_draft(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_draft_identity(ctx.store, ctx.ref_id)


def _m_requirements(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_requirements(ctx.store, ctx.ref_id)


def _m_seeds(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_seeds(ctx.store, ctx.ref_id)


def _m_glossary(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_glossary(ctx.store, ctx.ref_id)


def _m_body(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return "## Body\n" + (_load_ref_body(ctx.store, ctx.ref_id) or "(empty)")


def _m_anchor(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_anchor_context(ctx.store, ctx.ref_id)


def _m_doc_context(ctx: AssemblyContext) -> str:
    """The new ``doc_context`` TOON table (ADR 0038 §6). Gated on
    ``has_anchor``, which memoises the resolved handle in ``extras``."""
    assert ctx.store is not None
    anchor = ctx.extras.get("anchor")
    if not anchor:
        return ""
    return doc_context_table(ctx.store, anchor)


def _m_heading_intent(ctx: AssemblyContext) -> str:
    """The heading-intent hierarchical prompt for an anchored draft tick (slice
    8b.2). Gated on ``has_anchor``, which memoises the resolved handle in
    ``extras``; self-gates to ``""`` when the section carries no intents."""
    assert ctx.store is not None
    anchor = ctx.extras.get("anchor")
    if not anchor:
        return ""
    return _render_heading_intent(ctx.store, str(anchor))


def _planner_fisheye_enabled() -> bool:
    """The planner fisheye is the first live ADR-0051 integration (Level 1) —
    default-ON, one env var (`PRECIS_PLANNER_FISHEYE=0`) turns it off cluster-wide
    without a redeploy if it ever misbehaves on prod."""
    return os.environ.get("PRECIS_PLANNER_FISHEYE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _render_reader_working_set(store: Store, ref_id: int) -> str:
    """Render a hand-curated working set (ADR 0051 §6) the draft reader attached
    to this change-request as ``meta.working_set`` — the author's **eyes** (pens
    + context + promoted ring targets) composed into one deduplicated context,
    plus the **edit-these-at-a-minimum** pen hint. Empty when the tick carries no
    working set (the classic single-anchor path then applies)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'working_set' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    ws_meta = row[0] if row and isinstance(row[0], dict) else None
    if not ws_meta:
        return ""
    eyes = ws_meta.get("eyes") or []
    if not eyes:
        return ""
    from precis.utils.working_set_render import render_working_set
    from precis.workers.working_set import WorkingSet

    ws = WorkingSet()
    for e in eyes:
        try:
            ws.focus(str(e["handle"]), e.get("extent", "fisheye+1hop"))
        except Exception:
            continue  # skip a malformed eye, keep the rest
    if not ws.eyes:
        return ""
    try:
        rendered = render_working_set(store, ws)
    except Exception:
        log.debug("planner working-set render failed for ref %s", ref_id, exc_info=True)
        return ""
    parts: list[str] = []
    hint = [str(h) for h in (ws_meta.get("edit_hint") or [])]
    if hint:
        parts.append(
            "## Edit these, at a minimum (probably)\n\n"
            + ", ".join(hint)
            + "\n\nThese are the author's marked edit targets — start here; the "
            "working set below is your read-only grounding."
        )
    parts.append(f"## Working set — the author's eyes in context (§6)\n\n{rendered}")
    return "\n\n".join(parts)


def _m_fisheye(ctx: AssemblyContext) -> str:
    """A `fisheye+1hop` over the anchored section (ADR 0051 §6) — its
    neighborhood **plus the reference ring** (cited papers/patents + linked
    notes) the planner otherwise never sees. Complements the whole-draft body
    (that is every section; this is the anchor's locale + what it points at).

    When the change-request carries a hand-curated ``meta.working_set`` (the
    reader's pens + eyes), that **multi-eye** set is rendered instead — the
    author drove the curation, so honour it over the single-anchor default.

    **Additive and fallback-safe:** gated `has_anchor`, flag default-ON, and any
    render failure degrades to empty — it can never break the planner prompt."""
    if not _planner_fisheye_enabled():
        return ""
    assert ctx.store is not None
    curated = _render_reader_working_set(ctx.store, ctx.ref_id)
    if curated:
        return curated
    anchor = ctx.extras.get("anchor")
    if not anchor:
        return ""
    try:
        from precis.utils.fisheye import render_fisheye

        ring = render_fisheye(
            ctx.store, kind="draft", handle=str(anchor), extent="fisheye+1hop"
        )
    except Exception:
        log.debug("planner fisheye render failed for %r", anchor, exc_info=True)
        return ""
    return f"## Fisheye — {anchor} in context (§6)\n\n{ring}" if ring.strip() else ""


def _m_section_style(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_section_style(ctx.store, ctx.ref_id)


def _m_backfill(ctx: AssemblyContext) -> str:
    """Inject the source-backfill workspace + phase-specific instructions (gated
    on ``has_backfill``). The target handles were resolved + memoised by the
    predicate into ``extras['backfill_targets']``; the run's phase (find/review,
    slice 7) is read from its ``BACKFILL_PHASE`` tag."""
    assert ctx.store is not None
    targets = ctx.extras.get("backfill_targets") or []
    phase = _backfill_phase(ctx.store, ctx.ref_id)
    return _render_backfill_workspace(
        ctx.store, list(targets), run_ref_id=ctx.ref_id, phase=phase
    )


def _m_workspace(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_workspace_status(ctx.store, ctx.ref_id)


def _m_children(ctx: AssemblyContext) -> str:
    assert ctx.store is not None
    return _render_children_status(ctx.store, ctx.ref_id)


#: Persona loaded when a tick is a draft-section review (ADR 0038 step 3).
_REVIEW_PERSONA_SKILL: str = "precis-draft-reviewer"


def _load_review_persona() -> str:
    """Verbatim body of the draft-reviewer persona (``{{include}}``-expanded
    by ``SkillHandler.get``). Degrades to a terse inline stance if the skill
    can't load, so a review tick never runs persona-less."""
    try:
        from precis.handlers.skill import SkillHandler

        return SkillHandler(hub=None).get(id=_REVIEW_PERSONA_SKILL).body  # type: ignore[arg-type]
    except Exception:
        log.exception("planner_prompt: failed to load draft-reviewer persona")
        return (
            "You are reviewing a draft section. File each finding as an "
            "anchored change-request todo — `put(kind='todo', "
            "meta={'anchor':'dc<id>'}, text='<fix>')` — and do not edit the "
            "chunks directly."
        )


def _m_reviewer_persona(ctx: AssemblyContext) -> str:
    """Inject the reviewer stance for a review tick (gated on ``has_review``).

    Specialises the persona in the variable layer (ADR 0038 §5) so the
    cached planner contract stays genre-agnostic: a review-todo overrides
    the default plan-this-todo stance with review-this-section."""
    lens = ctx.extras.get("review") or "structural"
    return (
        f"## Reviewer mode — {lens}\n\n"
        f"This tick is a REVIEW (meta.review={lens}), not an edit. Adopt the "
        f"persona below for this tick; apply the specific lens your task body "
        f"names. Your output is anchored change requests, not prose edits.\n\n"
        f"{_load_review_persona()}"
    )


def _m_review_section(ctx: AssemblyContext) -> str:
    """The section subtree the reviewer must read (gated on ``has_review``).

    Resolves ``meta.anchor`` directly (a review-todo always carries it),
    independent of the doc_context module's predicate side-effects."""
    assert ctx.store is not None
    anchor = ctx.extras.get("anchor")
    if not anchor:
        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'anchor' FROM refs WHERE ref_id = %s", (ctx.ref_id,)
            ).fetchone()
        anchor = ((row[0] if row else None) or "").lstrip("¶").strip() or None
    if not anchor:
        return ""
    return section_review_block(ctx.store, anchor)


#: Closed-tag prefix for the backfill run's phase (slice 7). One
#: ``BACKFILL_PHASE:<phase>`` tag on the run todo; absence = ``find``. Upper-case
#: per the ``tags_namespace_check`` constraint (namespace = upper(namespace)).
_BACKFILL_PHASE_NS = "BACKFILL_PHASE"
PHASE_FIND = "find"
PHASE_REVIEW = "review"

#: Values (case-insensitive) that count as the *review* phase. A model advancing
#: the phase pastes a hand-authored tag value; accepting the near-misses ("review"
#: / "reviewing" / "rev") keeps a trivial typo from silently dropping the run back
#: to *find* and re-doing the whole weave.
_REVIEW_PHASE_ALIASES: frozenset[str] = frozenset(
    {"review", "reviewing", "reviewed", "rev", "r"}
)


def _backfill_phase(store: Store, ref_id: int) -> str:
    """The backfill run's phase, read from a ``BACKFILL_PHASE:<phase>`` tag on the
    run todo (default :data:`PHASE_FIND`). Progression is **monotonic**: find →
    review → done (``STATUS:done``); a review that turns up a genuinely new gap may
    tag back to ``find`` for one more pass. The value is matched case-insensitively
    against :data:`_REVIEW_PHASE_ALIASES` so a near-miss ("Review"/"reviewing")
    still enters review; any other value degrades to ``find`` (the safe,
    work-producing phase). Fallback-safe: a read failure → ``find``."""
    try:
        for tag in store.tags_for(ref_id):
            if getattr(tag, "prefix", None) == _BACKFILL_PHASE_NS:
                val = (tag.value or "").strip().lower()
                return PHASE_REVIEW if val in _REVIEW_PHASE_ALIASES else PHASE_FIND
    except Exception:
        log.debug("backfill phase read failed for %s", ref_id, exc_info=True)
    return PHASE_FIND


def _backfill_find_instructions(kind: str, draft_ident: str, run_ref_id: int) -> str:
    """The **find** phase (slice 4 + slice 7 transition): weave / dismiss / request
    each ``○`` candidate, then advance to *review* rather than finishing — so the
    next tick re-reads the woven prose with its sources still open."""
    from precis.backfill.provenance import TIERS

    tier_admonition = "\n".join(f"   - `[{t.tag}]` — {t.admonition}" for t in TIERS)
    return (
        "## Source backfill — weave the sources you missed\n\n"
        "This tick is the **find** phase of a source-backfill pass over the "
        "section(s) below: find corpus sources this text *should* cite but "
        "doesn't, and integrate them. It is the **recall** mirror of the citation "
        "verifier (which checks that what you cited is true) — here you ask *did I "
        "miss anything?* The workspace that follows shows each target section, the "
        "papers it already cites (marked `★ cited`), the ✓/⚠ grounding line (where "
        "the section is well-sourced vs under-sourced), and the uncited-but-"
        "relevant sources recall surfaced (marked `○ candidate`, listed under "
        "*candidate sources*). The `○` candidates are the product — work each "
        "one:\n\n"
        "1. **Weave it** — if the candidate genuinely supports a claim the "
        "section makes (open it with `get(id='pc<id>')` and confirm it before you "
        "trust it), integrate it: edit the draft prose to state/support the claim "
        "and cite the specific chunk by its handle `[pc<id>]` (see the Draft + "
        "citation guidance above). Write for the human reader — a real sentence "
        "that earns the citation, never a bare handle bolted onto an unchanged "
        "line.\n"
        "2. **Dismiss it** — if it is *not* actually relevant (off-topic, "
        "redundant with what you cite, or it doesn't support the claim), record "
        "the rejection so recall never resurfaces it:\n\n"
        f"       tag(kind='{kind}', id='{draft_ident}', "
        "add=['DISMISSED_SOURCE:<candidate>'])\n\n"
        "   where `<candidate>` is the candidate's `pa<id>` handle pasted verbatim "
        "(e.g. `DISMISSED_SOURCE:pa889` — the bare number `889` or the `pc<id>` "
        "chunk handle also work; the ledger resolves any of them). Dismiss "
        "**every** candidate you reject — an un-dismissed reject comes back on the "
        "next run and the pass never converges.\n"
        "3. **Request it** — if the source you actually need is *not* in the "
        "corpus, request it via the `paper_ingested` wait-leaf flow described "
        "above; never invent or guess a citation.\n\n"
        "**Advance to review — don't stop at weaving.** When every `○` candidate "
        "has been woven in or dismissed, do **not** tag `STATUS:done` yet — enter "
        "the *review* phase so the next tick re-reads what you wrote with these "
        "sources still open and checks the weave landed:\n\n"
        f"       tag(kind='todo', id='{run_ref_id}', add=['BACKFILL_PHASE:review'])\n\n"
        "Do not re-open the same candidates tick after tick — a paper you keep "
        "neither citing nor dismissing is a paper to dismiss.\n\n"
        "**Provenance tiers.** Every candidate is tagged with the kind of source "
        "it is — these are *not* interchangeable evidence, so respect the tag "
        "when you decide whether (and how) to cite it:\n\n"
        f"{tier_admonition}\n\n"
        "Recall already down-ranks lower tiers; weave only what the claim "
        "honestly needs, and never let a `[prior-art]` or `[own-note]` hit stand "
        "in for the peer-reviewed source a scientific claim actually requires.\n"
    )


def _backfill_review_instructions(run_ref_id: int) -> str:
    """The **review** phase (slice 7): judge whether the weave *landed*, with the
    sources still open in the same window — a review pass distinct from the
    citation verifier (accurate / well-sourced / reads standalone, not "is a quote
    byte-true"). Converges on a clean read; may reopen ``find`` for a real gap."""
    return (
        "## Source backfill — REVIEW what you wove\n\n"
        "This tick is the **review** phase of source-backfill on the section(s) "
        "below. In the find phase you wove in the sources recall surfaced; now "
        "judge whether the weave *landed*. This is a **review**, not the citation "
        "verifier — you are not checking a quote is byte-true, you are checking "
        "the integration reads and holds. Reviewing your new prose *with its "
        "sources still open above* is the whole point: check each claim against "
        "its source in the same window.\n\n"
        "1. **Claim ↔ source.** For each citation you added, re-read the claim "
        "against the source (its chunk is open above; re-open with "
        "`get(id='pc<id>')` if needed) — does the source actually support what the "
        "sentence asserts, or did the weave overreach? Tighten the prose to what "
        "the source bears.\n"
        "2. **Cold-read test.** Read each new sentence as a context-poor human who "
        "cannot resolve `[pc<id>]`: does it still carry its point? Where a citation "
        "is load-bearing, name the finding in the sentence (integral — 'Kumar et "
        "al. report…') so the reader learns *whose* result it is without chasing "
        "the handle.\n"
        "3. **Coverage.** Re-check the ✓/⚠ grounding line and fix any ⚠ you now "
        "can.\n\n"
        "Revise the prose directly with `edit(id='dc<id>')`. **Converge:** when "
        "the section reads clean and every claim is faithfully sourced, tag "
        "`STATUS:done`. If review turns up a *genuinely new* missing source, "
        "reopen recall for one more pass (never both, only for a real gap — the "
        "phase is monotonic and ping-ponging spins the loop):\n\n"
        f"       tag(kind='todo', id='{run_ref_id}', add=['BACKFILL_PHASE:find'])\n"
    )


def _render_backfill_workspace(
    store: Store, targets: list[str], *, run_ref_id: int, phase: str = PHASE_FIND
) -> str:
    """The source-backfill tick block: the recall workspace for the target
    section(s) — what they already cite (``★``), the uncited-but-relevant corpus
    sources recall found (``○`` candidates), and the ✓/⚠ grounding line — prefixed
    by **phase-specific** instructions (slice 7). ``find`` weaves/dismisses and
    advances to review; ``review`` judges the weave in-context and converges.

    This is the *recall* mirror of the citation verifier: the verifier asks "is
    what I cited true?" (precision); backfill asks "did I miss anything?"
    (recall). A candidate is only worth weaving if it genuinely supports a real
    claim — the pass must not force-fit a citation.

    Additive + fallback-safe: any resolution/render failure degrades to ``""`` so
    a malformed marker can never break the planner prompt. No-op on no targets."""
    if not targets:
        return ""
    from precis.backfill.workspace import recall_embedder, render_backfill
    from precis.utils import handle_registry

    parsed = handle_registry.parse(targets[0])
    kind = parsed[0] if parsed else "draft"
    try:
        # The semantic recall leg lights up when a remote embedder is configured
        # (PRECIS_EMBEDDER_URL); otherwise recall degrades to its lexical +
        # citation-graph legs (still real candidates) — never pulling torch into
        # the agent worker.
        workspace = render_backfill(store, recall_embedder(store), targets, kind=kind)
    except Exception:
        log.debug("planner backfill render failed for %r", targets, exc_info=True)
        return ""
    if not workspace.strip():
        return ""

    if phase == PHASE_REVIEW:
        instructions = _backfill_review_instructions(run_ref_id)
    else:
        # Resolve the draft the dismissal ledger lives on, so the instruction can
        # name the exact tag command (the ledger is a ref-level tag on the draft).
        draft_ident = "<draft>"
        try:
            first = store.get_draft_chunk(targets[0], kind=kind)
            if first is not None:
                draft = store.get_ref(kind=kind, id=int(first.ref_id))
                draft_ident = (
                    str(draft.slug) if draft and draft.slug else str(first.ref_id)
                )
        except Exception:
            pass
        instructions = _backfill_find_instructions(kind, draft_ident, run_ref_id)
    return f"{instructions}\n{workspace}"


#: Cached layer — one stable cache prefix across every planner tick.
_CACHED_MODULES: list[Module] = [
    Module(id="persona", layer=Layer.CACHED, build=_m_persona),
    Module(id="skill-menu", layer=Layer.CACHED, build=_m_skill_index),
    Module(id="tools", layer=Layer.CACHED, build=_m_tools),
    Module(id="kinds", layer=Layer.CACHED, build=_m_kinds),
    Module(id="contract", layer=Layer.CACHED, build=_m_contract),
]

#: Variable layer — per-tick. Optional blocks self-gate (return ``""``)
#: or carry an ``applies_when`` predicate; the assembler drops the rest.
_VARIABLE_MODULES: list[Module] = [
    Module(id="identity", layer=Layer.VARIABLE, build=_m_identity),
    Module(id="ancestry", layer=Layer.VARIABLE, build=_m_ancestry),
    Module(id="project", layer=Layer.VARIABLE, build=_m_project),
    Module(
        id="patent",
        layer=Layer.VARIABLE,
        build=_m_patent,
        applies_when="is_patent",
    ),
    Module(
        id="plan",
        layer=Layer.VARIABLE,
        build=_m_plan,
        applies_when="has_plan",
    ),
    Module(id="requirements", layer=Layer.VARIABLE, build=_m_requirements),
    Module(id="seeds", layer=Layer.VARIABLE, build=_m_seeds),
    Module(id="draft", layer=Layer.VARIABLE, build=_m_draft),
    Module(
        id="reviewer-persona",
        layer=Layer.VARIABLE,
        build=_m_reviewer_persona,
        applies_when="has_review",
    ),
    Module(id="glossary", layer=Layer.VARIABLE, build=_m_glossary),
    Module(id="body", layer=Layer.VARIABLE, build=_m_body),
    Module(id="anchor", layer=Layer.VARIABLE, build=_m_anchor),
    Module(
        id="heading-intent",
        layer=Layer.VARIABLE,
        build=_m_heading_intent,
        applies_when="has_anchor",
    ),
    Module(
        id="doc_context",
        layer=Layer.VARIABLE,
        build=_m_doc_context,
        applies_when="has_anchor",
    ),
    Module(
        id="fisheye",
        layer=Layer.VARIABLE,
        build=_m_fisheye,
        applies_when="has_anchor",
    ),
    Module(
        id="review-section",
        layer=Layer.VARIABLE,
        build=_m_review_section,
        applies_when="has_review",
    ),
    Module(
        id="backfill",
        layer=Layer.VARIABLE,
        build=_m_backfill,
        applies_when="has_backfill",
    ),
    Module(id="section-style", layer=Layer.VARIABLE, build=_m_section_style),
    Module(id="workspace", layer=Layer.VARIABLE, build=_m_workspace),
    Module(id="children", layer=Layer.VARIABLE, build=_m_children),
]


__all__ = [
    "PlannerPrompts",
    "build_planner_prompts",
]
