"""Mint-time coordinator routing for the dark factory (gr645163).

Ruling (Reto 2026-11-12, "escalate to next model"): work no builder lane
may write — a brief naming a deny path, or a fix that is prod-graph only
— skips the builders entirely and becomes one coordinator ask with the
brief attached, instead of burning a build that can't land and stalling
until the coordinator lands it by hand (td643078/gr626879, td638980).
This module is the shared classifier: ``gripe_dispatch`` asks it of each
candidate gripe's report before minting a fix chain, and
``plan_dispatch._step_dispatch`` asks it of each run_slice-bound todo's
body and newest brief/handoff before minting a job. A builder's ordinary
can't-finish (a ``no_commit`` run with no verdict) is NOT here — that
climbs the model ladder in ``_step_dispatch`` and only the top rung's
failure files the coordinator ask.

Two signals:

- ``deny-path named`` — a path-shaped token in the text matches the
  resolved deny set (:func:`precis.plans.lanes.deny_paths_for`,
  ``STRICT_DENY`` by default). Any path-shaped token counts, backticks
  and code spans included: a plain-prose mention of ``scripts/ship`` is
  the td643078 shape. Case folds like the lane's own write check
  (``human_only_hits``: the slice hosts' filesystems fold case).
- ``graph-only`` — every non-empty token is a well-formed graph handle
  (``td<id>``/``gr<id>``/``me<id>``/…), bare punctuation, graph
  vocabulary or grammar glue, and at least one handle appears. A
  report reduced to handles — or to a graph edit phrased as a sentence
  (``remove the stale edge from me123456``, td645566) — names no repo
  target: the fix lives in the prod graph, which a builder's dev
  worktree cannot reach (gr630346/gr630611's class of no-commit runs).

The instruction boilerplate must not classify: the ``gripe-fix-direct``
head names ``scripts/ship --quick`` as the land's own mechanism, and a
brief pasted whole into a body carries the "Paths you may not write" and
"When done" sections quoting the same harness paths. Both are stripped
before the text is judged.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from precis.plans import templates as _templates
from precis.utils import handle_registry

#: A token that could name a repo path: carries a ``/`` (``scripts/ship``,
#: ``src/precis/plans/lanes.py``), a bare dotted name (``CLAUDE.md``,
#: ``ansible.cfg``), or a dotfile (``.mcp.json``). Glob metachars are kept
#: so a brief quoting the glob itself (``deploy/inventory*/**``) still hits.
_PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_.$~*+-]*(?:/[A-Za-z0-9_.$~*+-]+)+"
    r"|[A-Za-z0-9_-]+\.[A-Za-z]{1,5}"
    r"|\.[A-Za-z0-9][A-Za-z0-9_.-]*"
)

#: Markdown H2 sections of the slice brief whose boilerplate names the
#: harness (the deny-path list itself, the land-lint command block) — a
#: body quoting the rendered brief would classify on its own instructions.
_BOILER_SECTION = re.compile(r"^##\s+(Paths you may not write|When done)\b")

#: Factory-minted bodies name deny paths only as the land's own
#: mechanism — the gripe chain's ``the land (land_slice, `scripts/ship
#: --quick`)``, the release-fixer's ``land with scripts/ship --quick``
#: and the revert task's ``land it as a normal slice (scripts/ship)``.
#: Stripping the whole stock phrase keeps the land mechanics from
#: reading as the work's target (a genuine "fix scripts/ship" report
#: never matches these).
_LAND_MECHANISM = (
    re.compile(r"\bthe land \([^)]*\)", re.IGNORECASE),
    re.compile(r"\bwith `?scripts/ship`?\s+--\w+", re.IGNORECASE),
    re.compile(r"\bas a normal slice \([^)]*\)", re.IGNORECASE),
)

#: Characters a handle may be wrapped in inside prose.
_HANDLE_WRAP = ".,;:()[]{}`'\"*_#<>|"

_LETTER = re.compile(r"[a-zA-Z]")

#: Grammar glue (td645566): articles, pronouns, auxiliaries and
#: connectives that carry no target — a graph edit phrased as a
#: sentence stays graph-only when the glue is all that joins the
#: handles and graph words.
_GLUE_VOCAB = """
a an the and or of to from in on at by for with without into onto
under over between among through per via as is are was were be been
being it its it's this that these those there their they them he she
his her i you we me my your our us who whose whom which what when
where why how not no do does did done doing don't doesn't didn't has
have had having can could will would shall should may might must
need needs all any each every some none one two three both either
neither other others another same own such only just also still now
then than so too very more most less least much many few out off up
down back again once first last next after before while if else but
yet because since until unless though although please let's i'm i've
you're we're they're isn't aren't wasn't weren't can't couldn't
won't wouldn't shouldn't
"""
_GLUE_WORDS = frozenset(_GLUE_VOCAB.split())

#: The prod graph's own vocabulary (td645566): nouns naming graph rows
#: and the verbs and states that mutate them. A letter-carrying token
#: outside this set is a repo target or ambiguous work and the text does
#: not classify — generic verbs like ``fix``/``check`` stay out on
#: purpose: ``fix gr1's stale edge`` can mean the code that wrote the
#: edge, so it still builds. Repo notions (file, code, test, docs,
#: branch, commit, diff, …) are simply absent — their absence, not a
#: deny list, is what keeps a repo brief out.
# the rows
_GRAPH_VOCAB_ROWS = """
graph node nodes edge edges link links rel rels relation
relations handle handles memory memories chunk chunks gripe
gripes todo todos ask asks question questions job jobs tag tags
comment comments thread threads taxon taxa skill skills draft
drafts plan plans quest quests alert alerts message messages
snapshot snapshots handoff handoffs index hub hubs parent
parents child children subtree title titles gloss glosses body
bodies meta claim claims section sections finding findings
citation citations review reviews verdict verdicts ref refs row
rows record records card cards alias aliases checklist checklists
measure measures entry entries orphan orphans duplicate
duplicates mirror mirrors resume resumes
"""
# the rel names
_GRAPH_VOCAB_RELS = """
part-of derived-from related-to blocked-by supersedes qualifies
cites
"""
# the mutations
_GRAPH_VOCAB_MUTATIONS = """
remove removes removed removing delete deletes deleted deleting
retire retires retired retiring unretire restore restores
restored linked linking unlink unlinks unlinked relink relinks
rewire attach attaches attached detach detaches detached
reattach reparent reparents supersede superseded tagged tagging
untag retag mark marks marked unmark rename renames renamed
close closes closed closing reopen reopens reopened edit edits
edited editing update updates updated updating add adds added
adding merge merges merged merging split splits prune prunes
pruned sweep sweeps groom grooms consolidate consolidates
reconsolidate reconsolidates archive archives archived correct
corrects corrected revise revises revised redirect redirects
point points pointed repoint move moves moved moving drop drops
dropped clear clears cleared clean cleans cleaned cleanup
replace replaces replaced rewrite rewrites rewritten rewriting
annotate annotates label labels labeled labelled relabel expire
expires expired cite cited uncite qualify derive derives derived
create creates created mint mints minted set sets setting unset
change changes changed connect connects disconnect disconnects
answer answers answered acknowledge acknowledges acknowledged
pin pins pinned unpin repin insert inserts inserted append
appends appended prepend prepends make makes made retry retries
retried rerun swap swaps swapped toggle toggles
"""
# the states
_GRAPH_VOCAB_STATES = """
stale orphaned dangling duplicated wrong dead live missing
broken stray spurious leftover redundant empty outdated wonky
bad old new open current incorrect inconsistent unused unneeded
obsolete irrelevant related unresolved unanswered blocked
unblocked stuck parked held
"""
_GRAPH_WORDS = frozenset(
    _GRAPH_VOCAB_ROWS.split()
    + _GRAPH_VOCAB_RELS.split()
    + _GRAPH_VOCAB_MUTATIONS.split()
    + _GRAPH_VOCAB_STATES.split()
)


def _handle_token(t: str) -> bool:
    """A well-formed handle, a trailing ``'s`` possessive allowed —
    ``gr630346's`` still names the gripe."""
    base = t[:-2] if t.endswith("'s") else t
    return handle_registry.is_well_formed(base)


def _graph_word(t: str) -> bool:
    """A letter-carrying token that names no repo target: graph
    vocabulary or grammar glue (a trailing ``'s`` folds first)."""
    w = t.lower()
    if w.endswith("'s"):
        w = w[:-2]
    return w in _GRAPH_WORDS or w in _GLUE_WORDS


def strip_direct_head(text: str) -> str:
    """The text minus the ``gripe-fix-direct`` instruction head (everything
    through :data:`templates.DIRECT_HEAD_END`), minus the slice brief's
    deny-path / land-lint boilerplate sections, and minus the factory's
    stock land-mechanism phrases — only the report and any real
    instructions remain for classification."""
    idx = text.find(_templates.DIRECT_HEAD_END)
    if idx >= 0:
        text = text[idx + len(_templates.DIRECT_HEAD_END) :]
    out: list[str] = []
    skip = False
    for line in text.split("\n"):
        if line.startswith("## "):
            skip = _BOILER_SECTION.match(line) is not None
        if not skip:
            out.append(line)
    stripped = "\n".join(out)
    for pattern in _LAND_MECHANISM:
        stripped = pattern.sub(" ", stripped)
    return stripped


def deny_path_hit(text: str, deny_paths: Iterable[str]) -> tuple[str, str] | None:
    """``(token, glob)`` of the first path-shaped token matching a deny
    glob, ``None`` when none does — case-folded like the lane's write
    check. Tokens are judged as whole paths, so ``gr645163`` or
    ``plan_dispatch`` prose never reads as a ``scripts/*`` hit."""
    globs = tuple(dict.fromkeys(g.lower() for g in deny_paths))
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0).lower()
        if token.startswith("./"):
            token = token[2:]
        hit = next(
            (
                g
                for g in globs
                if fnmatch.fnmatchcase(token, g)
                or ("/" in token and fnmatch.fnmatchcase(token, "*/" + g))
            ),
            None,
        )
        if hit is not None:
            return m.group(0), hit
    return None


def graph_only(text: str) -> bool:
    """True when every non-empty token is a well-formed graph handle,
    carries no letter, or is graph vocabulary or grammar glue, and at
    least one handle appears — the report names graph rows to mutate,
    never a repo path. Bare handles classify (``gr630346 me123456``) and
    so does a graph edit phrased as a sentence (``remove the stale edge
    from me123456``, td645566); a token outside the vocabulary — a file,
    a symbol, a generic verb like ``fix`` — keeps the text a build."""
    real = [t.strip(_HANDLE_WRAP) for line in text.splitlines() for t in line.split()]
    real = [t for t in real if t]
    if not real:
        return False
    return any(_handle_token(t) for t in real) and all(
        _handle_token(t) or not _LETTER.search(t) or _graph_word(t) for t in real
    )


def coordinator_route(text: str | None, deny_paths: Iterable[str]) -> str | None:
    """The short reason the work routes straight to the coordinator, or
    ``None`` when a builder lane may take it. The reason is written into
    the ask body and context, so it names the signal, not advice."""
    if not text or not text.strip():
        return None
    body = strip_direct_head(text)
    hit = deny_path_hit(body, deny_paths)
    if hit is not None:
        token, glob = hit
        return f"names deny path `{token}` (matched `{glob}`)"
    if graph_only(body):
        return "prod-graph only — no repo diff exists for a builder to write"
    return None
