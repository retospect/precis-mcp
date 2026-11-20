"""Mint-time coordinator routing for the dark factory (gr645163).

Ruling (Reto 2026-11-12, "escalate to next model"): work no builder lane
may write — a declared deny-path write, or a fix that is prod-graph only
— skips the builders entirely and becomes one coordinator ask with the
brief attached, instead of burning a build that can't land and stalling
until the coordinator lands it by hand (td643078/gr626879, td638980).
This module is the shared classifier: ``gripe_dispatch`` asks it of each
candidate gripe's report before minting a fix chain, and
``plan_dispatch._step_dispatch`` asks it of each run_slice-bound todo's
body and newest brief before minting a job. Deny paths are judged in
the newest brief first, then independently in the body: labels in one
must not hide the other's requested targets (td651223). Graph-only
classification still considers both texts together. A builder's ordinary
can't-finish (a ``no_commit`` run with no verdict) is NOT here — that
climbs the model ladder in ``_step_dispatch`` and only the top rung's
failure files the coordinator ask.

Two signals:

- ``deny-path named`` — a path-shaped token in the text matches the
  resolved deny set (:func:`precis.plans.lanes.deny_paths_for`,
  ``STRICT_DENY`` by default) AND sits in a declared write set:
  ``File set:``, ``Files:``, ``Seam:``, ``Anchors:`` or ``Targets:``
  lines and their lists, or a ``Want:``, ``Fix:`` or ``Goal:`` paragraph
  carrying an edit verb on that path (gr702295). Quoted lines and fences
  labelled ``log``, ``console`` or ``output`` never declare writes.
  Unstructured prose and a path heading a defect sentence do not route.
  The diff's deny-path gate remains authoritative; this pre-filter only
  saves builds with explicitly declared forbidden writes. A declared
  ``File set:`` takes precedence within its document. Body and newest
  brief retain independent scopes. The route reason quotes the match.
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

#: A routing reason quoted back (an ask body or title, a pasted routing
#: comment): it names the deny path that routed the work, not a target.
#: The optional ``in "…"`` tail is the matched-sentence quote (gr685970)
#: — it must go with the reason or the quoted sentence re-classifies.
_ROUTE_REASON = re.compile(
    r"\bnames deny path `[^`\n]*`(?: \(matched `[^`\n]*`\))?"
    r"(?: in \"[^\"\n]*\")?",
    re.IGNORECASE,
)

#: A paragraph that is a note ABOUT the work's routing, not the work
#: (gr653037): the dispatchers' own stamps (``dispatch: routes to the
#: coordinator — report names deny path `X` …``, ``plan_dispatch: …``,
#: ``dispatch: minted …``) and the coordinator's notes (``Coordinator
#: note — …``, ``coordinator: …``). A routed gripe's routing comment
#: quotes its deny path; judged, it re-routed the gripe once released.
_NOTE_START = re.compile(
    r"^\s*(?:(?:plan_|gripe_)?dispatch:|coordinator(?:\s+note)?\s*[:—–-])",
    re.IGNORECASE,
)

#: A deny path RUN as a command, not written: the token followed by a
#: flag, a test path or a quoted argument (``scripts/test
#: tests/test_x.py -m slow``, ``scripts/test --typecheck``) — every
#: slice brief's acceptance names the harness this way (td647606).
_RUN_INVOCATION = re.compile(
    r"`?\bscripts/[\w.-]+`?(?=\s+(?:--?\w|tests?/|[\"']"
    r"|on\s+[`\"']?(?:tests?/|[\w./-]+\.py)))",
    re.IGNORECASE,
)

#: The verbs that run a command (gr663138): a path inside one — ``never
#: ran `scripts/ship```, ``run the tests through that tree's own
#: scripts/test`` — is an invocation, not a write target.
_RUN_VERBS = frozenset(
    [
        "run",
        "runs",
        "ran",
        "running",
        "re-run",
        "re-runs",
        "re-ran",
        "re-running",
        "rerun",
        "reruns",
        "rerunning",
        "execute",
        "executes",
        "executed",
        "executing",
        "invoke",
        "invokes",
        "invoked",
    ]
)
#: A clause that OPENS with a run verb (after these lead-in words) is a
#: command to run however far the path sits from the verb — unless an
#: edit verb comes between (``run the fix by editing scripts/ship``).
_RUN_LEAD_WORDS = "and then also please just first finally now to can should must will"
_RUN_LEAD = frozenset(_RUN_LEAD_WORDS.split())
_EDIT_VERB_WORDS = """
edit edits editing change changes changing modify modifies modifying
update updates updating patch patches fix fixes fixing rewrite rewrites
replace replaces add adds touch touches amend amends
"""
_EDIT_VERBS = frozenset(_EDIT_VERB_WORDS.split())
#: A parenthetical list of commands after a run verb (``commands to run
#: (`scripts/test`, `scripts/ship`)``): the whole list is invocations.
_RUN_LIST = re.compile(
    r"\b(?:" + "|".join(sorted(_RUN_VERBS)) + r")\s+\([^)\n]*\)", re.IGNORECASE
)

#: A rescue report's evidence (gr663138, td675895): the dead builder's
#: log tail, from the ``log tail:`` label (or a ``--- log tail ---``
#: rule) to the next Markdown H2 or the end, plus the label lines that
#: record what happened rather than what to change. Like a ``log``-
#: labelled fence, nothing in them is a target. The label lines count
#: only when the text is a rescue report (``rescue class:`` /
#: ``signature key:``): a brief's own ``Step: …`` is real intent.
_LOG_TAIL_START = re.compile(
    r"^\s*(?:log tail:|-{3,}\s*log tail\s*-{3,})", re.IGNORECASE
)
_RESCUE_MARK = re.compile(
    r"^\s*(?:rescue(?: class)?|signature key):", re.IGNORECASE | re.MULTILINE
)
_RESCUE_LABEL = re.compile(
    r"^\s*(?:rescue(?: class)?|signature key|first node|job|step|branch|head):",
    re.IGNORECASE,
)

#: A ``.claude/worktrees[/<tree>[/<repo-relative remainder>]]`` path: the
#: slice's checkout location (gr658931). Only the remainder, if any, is a
#: repo path; the location itself is where the work happens, never what
#: it changes.
_CHECKOUT_PATH = re.compile(
    r"(?:^|/)\.claude/worktrees(?:/[^/]+(?:/(?P<rest>.*))?)?$", re.IGNORECASE
)

#: A brief's declared write set (``File set: a.py, b.py``, through the
#: next blank line). When present it is the builder's whole write
#: target, so only it is judged for deny paths (td647606).
_FILE_SET = re.compile(r"^\s*File set:(.*?)(?:\n\s*\n|\Z)", re.MULTILINE | re.DOTALL)

#: Explicit report labels, optionally formatted as Markdown headings/bold.
_INTENT_START = re.compile(
    r"^(?:#{1,6}\s+)?\*{0,2}(?:Goal|Fix|Want)\b"
    r"\*{0,2}[ \t]*(?::\*{0,2}[ \t]*|$)",
    re.IGNORECASE,
)
#: Labels that cite paths as evidence (gr653037). Unlike a ``Symptom``
#: (often the target itself, gr639737), these never name the edit, so
#: their paragraphs are dropped even from a report without intent labels.
_CITATION_LABELS = (
    r"Evidence|Related|See also|Cross-checked|Checked|Cites|Refs?|References"
    # ``Out of scope``/``Non-goal`` paragraphs (gr685970): they name what
    # the fix must NOT touch — a deny path on one is a non-target.
    r"|Out of scope|Non-goals?"
)
_EVIDENCE_START = re.compile(
    rf"^(?:#{{1,6}}\s+)?\*{{0,2}}(?:Symptom|Route|Seen|{_CITATION_LABELS})\b"
    r"(?:[ \t]*\([^)]*\))?\*{0,2}[ \t]*(?::|$)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^(`{3,}|~{3,})")

#: Where a mention's clause starts, looking back from the path token:
#: sentence or list punctuation, a dash, an opening paren, or a
#: contrast word (``not in a.py but in scripts/ship``). ``cf.`` is no
#: sentence end.
_CLAUSE_BREAK = re.compile(
    r"[;,!?\n(—–]|(?<![Cc]f)\.(?=\s|$)|\b(?:but|instead|rather)\b",
    re.IGNORECASE,
)
#: Cue words that make a deny-path mention a non-target (gr653037) when
#: one sits within :data:`_CUE_REACH` words before the path in its clause:
#: negation (``no deploy/.github/scripts paths``, ``never edit X``,
#: ``without touching X``, ``not X``) and citation (``cross-checked
#: against X``, ``matches X's format``, ``cf. X``, ``related: X``).
_NEGATION_CUES = frozenset(
    ["no", "not", "never", "without", "nor", "nothing", "don't", "dont"]
)
_CITATION_CUES = frozenset(
    [
        "cross-checked",
        "checked",
        "matches",
        "matching",
        "mirrors",
        "mirroring",
        "cf",
        "cites",
        "citing",
        "cited",
        "see",
        "related:",
        "evidence:",
        "cross-checked:",
        "checked:",
        "see:",
    ]
)
_DISCOUNT_CUES = _NEGATION_CUES | _CITATION_CUES
_CUE_REACH = 3
_CUE_WRAP = "`'\"*_()[]{}<>.,"

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
    skip = note = False
    para_start = True
    for line in text.split("\n"):
        if line.startswith("## "):
            skip = _BOILER_SECTION.match(line) is not None
        if not line.strip():
            note, para_start = False, True
        elif para_start:
            note, para_start = routing_note(line), False
        if not (skip or note):
            out.append(line)
    stripped = "\n".join(out)
    for pattern in (*_LAND_MECHANISM, _ROUTE_REASON):
        stripped = pattern.sub(" ", stripped)
    return stripped


def strip_evidence(text: str) -> str:
    """The text minus a rescue report's evidence (:data:`_LOG_TAIL_START`
    blocks, and the :data:`_RESCUE_LABEL` lines when the text is a rescue
    report): what the dead run logged, never what to change."""
    rescue = _RESCUE_MARK.search(text) is not None
    out: list[str] = []
    tail = False
    for line in text.split("\n"):
        if line.startswith("## "):
            tail = False
        elif _LOG_TAIL_START.match(line):
            tail = True
        if not (tail or (rescue and _RESCUE_LABEL.match(line))):
            out.append(line)
    return "\n".join(out)


def routing_note(text: str) -> bool:
    """True when ``text`` opens as a dispatcher routing stamp or a
    coordinator note (:data:`_NOTE_START`) — a comment about the work's
    routing that never classifies the work (gr653037)."""
    return _NOTE_START.match(text) is not None


def _discounted(text: str, start: int, end: int) -> bool:
    """True when the path token at ``text[start:end]`` is negated or cited
    rather than asked for (gr653037): a negation or citation cue within
    :data:`_CUE_REACH` words before it in its clause, or ``cross-checked``
    right after it."""
    head = text[max(0, start - 200) : start]
    breaks = list(_CLAUSE_BREAK.finditer(head))
    if breaks:
        head = head[breaks[-1].end() :]
    words = _cue_words(head)
    if any(w in _DISCOUNT_CUES or w in _RUN_VERBS for w in words[-_CUE_REACH:]):
        return True
    if _run_clause(words):
        return True
    tail = _cue_words(text[end : end + 40])
    return bool(tail) and tail[0].startswith("cross-checked")


def _run_clause(words: list[str]) -> bool:
    """True when the clause ``words`` opens with a run verb and no edit
    verb follows it: the path ending the clause is a command's argument."""
    rest = list(words)
    while rest and rest[0] in _RUN_LEAD:
        rest.pop(0)
    return bool(rest) and rest[0] in _RUN_VERBS and not _EDIT_VERBS.intersection(rest)


def _cue_words(text: str) -> list[str]:
    return [w for w in (w.strip(_CUE_WRAP).lower() for w in text.split()) if w]


#: A line that DECLARES write targets (gr685970): a spec's
#: ``Seam:``/``Anchors:``/``Files:`` line or a ``File set:``-style
#: listing — a deny path on one is the requested edit.
_WRITE_LINE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?"
    r"(?:seam|anchors?|files?|file\s*(?:set|list)|targets?|write\s*set)\s*:",
    re.IGNORECASE,
)

#: The verbs that make a deny-path mention a requested write (gr685970):
#: the gripe's set (edit, change, add to, write, touch, rename), the
#: :data:`_EDIT_VERBS` paraphrases (with their past forms — ``… test.yml
#: updated``), and the request verbs real reports use (``repair``/
#: ``adjust``/``make``/``rotate``/``refresh``/``audit``…). Removal verbs
#: (``drop``/``remove``/``delete``) stay out: they double as defect
#: descriptions (``the parser drops scripts/ship calls``) and the
#: gripe's rule is precision-first — a coordinator's ``route:builder``
#: releases a denied-target report the wording hides. A verb word right
#: after a determiner is a noun (``a fix node``, ``the change``), not a
#: cue (:data:`_DETERMINERS`); the location assertion those nouns still
#: make — ``the fix is in X`` — is intent via :data:`_FIX_LOCATION`.
_WRITE_VERBS = frozenset(
    _EDIT_VERB_WORDS.split()
    + [
        "edited",
        "changed",
        "modified",
        "updated",
        "patched",
        "fixed",
        "replaced",
        "added",
        "touched",
        "amended",
        "rewritten",
        "rewrote",
        "writes",
        "writing",
        "written",
        "write",
        "rename",
        "renames",
        "renaming",
        "renamed",
        "move",
        "moves",
        "moved",
        "moving",
        "rotate",
        "rotates",
        "rotated",
        "rotating",
        "refresh",
        "refreshes",
        "refreshed",
        "refreshing",
        "adjust",
        "adjusts",
        "adjusted",
        "adjusting",
        "repair",
        "repairs",
        "repaired",
        "repairing",
        "make",
        "makes",
        "made",
        "making",
        "extend",
        "extends",
        "extended",
        "refactor",
        "refactors",
        "refactored",
        "merge",
        "merges",
        "merged",
        "split",
        "splits",
        "split",
        "enumerate",
        "enumerates",
        "audit",
        "audits",
        "inspect",
        "inspects",
        "review",
        "reviews",
        "live",
        "lives",
        "lived",
        "belong",
        "belongs",
        "belonged",
        "go",
        "goes",
        "went",
    ]
)

#: A write-verb word right after one of these is a noun, not a cue
#: (gr685970): ``a fix node``, ``the change``, ``its update``.
_DETERMINERS = frozenset(
    [
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "its",
        "their",
        "our",
        "his",
        "her",
        "my",
        "your",
        "each",
        "every",
        "no",
        "any",
        "one",
    ]
)

#: A fix-location assertion (gr685970): ``the fix is in X``,
#: ``a change to X``, ``it lands in X`` — the path after the phrase is
#: the asserted write target. A negated one (``the fix is not in X``)
#: asserts the opposite: the match is skipped.
_FIX_LOCATION = re.compile(
    r"\b(?:fix|change|patch|edit|update|it(?:'s)?)\s+"
    r"(?:(?:is|was|'s|goes|lands|lives|belongs|sits|isn't)\s+)?"
    r"(?:not\s+)?"
    r"(?:in|to|for|on|of|inside|within)\b",
    re.IGNORECASE,
)

#: Where a write-intent sentence ends (gr685970): like
#: :data:`_CLAUSE_BREAK` minus the comma and the opening paren — a write
#: instruction and the path it edits often share one comma-joined
#: clause, and a parenthesised path still belongs to its sentence.
_SENTENCE_BREAK = re.compile(
    r"[;!?\n—–]|(?<![Cc]f)\.(?=\s|$)|\b(?:but|instead|rather)\b",
    re.IGNORECASE,
)

#: Leading path segments of a test-fixture tree (gr685970): under one a
#: deny-looking suffix (``tests/fixtures/deploy/inventory-x/hosts.yml``)
#: is fixture data, not the denied file — the ``*/glob`` fallback does
#: not apply to it.
_FIXTURE_DIRS = frozenset({"test", "tests", "fixture", "fixtures"})


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """The ``(start, end)`` span of the sentence containing
    ``text[start:end]`` (:data:`_SENTENCE_BREAK` bounded) — the unit the
    write-intent checks and the quoted reason share."""
    ss = 0
    for m in _SENTENCE_BREAK.finditer(text):
        if m.end() > start:
            break
        ss = m.end()
    nxt = _SENTENCE_BREAK.search(text, end)
    return ss, nxt.start() if nxt is not None else len(text)


#: The previous sentence's last words merge into a wrapped instruction's
#: verb scan: it ends in a comma, a colon (``update these files:`` + a
#: fence of paths) or a connector — ``adjust the gate in\nscripts/ship``
#: is one instruction across the wrap.
_CONTINUATION_WORDS = frozenset(
    [
        "in",
        "into",
        "to",
        "of",
        "from",
        "for",
        "with",
        "on",
        "at",
        "and",
        "or",
        "a",
        "an",
        "the",
        "its",
        "their",
        "our",
    ]
)


def _continuation_head(text: str, ss: int) -> list[str]:
    """The preceding sentence's words when it dangles into this one
    (:data:`_CONTINUATION_WORDS`) — else ``[]``."""
    before = text[:ss].rstrip()
    if not before:
        return []
    if before.endswith((",", ":")):
        return _cue_words(before)
    words = _cue_words(before)
    return words if words and words[-1] in _CONTINUATION_WORDS else []


def _in_write_list(text: str, ls: int) -> bool:
    """True when the line starting at ``ls`` closes a list a
    :data:`_WRITE_LINE` label introduces (``Files:\\n- scripts/ship``,
    or a comma-wrapped ``Files: a.py,\\n    scripts/ship``): bullet
    items and comma-continuations climb to the intro line."""
    pos = ls
    for _ in range(40):
        nl = pos - 1  # the ``\n`` ending the previous line
        if nl <= 0:
            return False
        prev_start = text.rfind("\n", 0, nl) + 1
        prev = text[prev_start:nl]
        if not prev.strip():
            return False
        if _WRITE_LINE.match(prev):
            return True
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s", prev) or prev.rstrip().endswith(","):
            pos = prev_start
            continue
        return False
    return False


def _write_intent(text: str, start: int, end: int, span: tuple[int, int]) -> bool:
    """True when the path token at ``text[start:end]`` is a requested
    write target (gr685970): on a spec write-target line or a list one
    introduces (:data:`_WRITE_LINE`, :func:`_in_write_list`), in its
    sentence with an edit verb (:data:`_WRITE_VERBS`, wrap continuations
    merged by :func:`_continuation_head`)."""
    ss, se = span
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    line = text[ls : le if le >= 0 else len(text)]
    if _WRITE_LINE.match(line):
        return True
    # An indented or bulleted line can sit inside a list a write-target
    # label introduces; a flush-left prose line cannot.
    if re.match(r"^\s|^\s*(?:[-*+]|\d+[.)])\s", line) and _in_write_list(text, ls):
        return True
    words = _continuation_head(text, ss) + _cue_words(text[ss:se])
    # A leading ``Label:`` (``Fix:``, ``Want:``…) is the report's field
    # name, not a verb in the sentence — stripping it keeps a bare
    # mention under the label from inheriting its "fix".
    if words and re.fullmatch(r"[a-z][a-z -]*:", words[0]):
        words = words[1:]
    verbs = [
        w
        for i, w in enumerate(words)
        if w in _WRITE_VERBS and not (i and words[i - 1] in _DETERMINERS)
    ]
    if verbs:
        return True
    for m in _FIX_LOCATION.finditer(text[ss:se]):
        if "not" not in m.group(0).lower().split() and m.end() <= start - ss:
            return True
    return False


def deny_path_hit(
    text: str, deny_paths: Iterable[str], *, write_intent: bool = True
) -> tuple[str, str, str] | None:
    """``(token, glob, sentence)`` of the first path-shaped token
    matching a deny glob, ``None`` when none does — case-folded like the
    lane's write check. Tokens are judged as whole paths, so
    ``gr645163`` or ``plan_dispatch`` prose never reads as a
    ``scripts/*`` hit. A negated or cited mention
    (:func:`_discounted`) is no hit, a path through
    ``.claude/worktrees/<tree>/`` is judged by the repo-relative
    remainder alone (:data:`_CHECKOUT_PATH`), and — unless
    ``write_intent`` is off (a declared ``File set:`` write set, whose
    every entry is a target by declaration) — only a token that counts
    as write intent (:func:`_write_intent`) routes (gr685970)."""
    globs = tuple(dict.fromkeys(g.lower() for g in deny_paths))
    for m in _PATH_TOKEN.finditer(text):
        if _discounted(text, m.start(), m.end()):
            continue
        # Sentence punctuation rides the token (``edit scripts/ship.``).
        shown = m.group(0).rstrip(".")
        if checkout := _CHECKOUT_PATH.search(shown):
            shown = checkout.group("rest") or ""
            if not shown:
                continue
        token = shown.lower()
        if token.startswith("./"):
            token = token[2:]
        fixture = token.split("/", 1)[0] in _FIXTURE_DIRS
        hit = next(
            (
                g
                for g in globs
                if fnmatch.fnmatchcase(token, g)
                or (
                    "/" in token
                    and not fixture
                    and fnmatch.fnmatchcase(token, "*/" + g)
                )
            ),
            None,
        )
        if hit is not None:
            # The token may carry the sentence's closing ``.`` — the
            # end search starts at ``shown``'s end, not the match's, so
            # the sentence does not bleed into the next one.
            tend = m.start() + len(m.group(0).rstrip("."))
            span = _sentence_span(text, m.start(), tend)
            if write_intent and not _write_intent(text, m.start(), tend, span):
                continue
            return shown, hit, " ".join(text[span[0] : span[1]].split())
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


def _change_intent(text: str) -> str:
    """Select explicit intent paragraphs, excluding citation and log evidence.

    Only deny-path matching uses this selection; graph-only classification
    must still account for every word in the report.
    """
    out: list[str] = []
    active = awaiting_content = False
    fence = ""
    log_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if fence:
            if line.startswith(fence) and not line[len(fence) :].strip(fence[0]):
                fence = ""
            elif active and not log_fence:
                out.append(line)
                awaiting_content = False
            continue
        if line.startswith(">"):
            continue
        if match := _FENCE.match(line):
            fence = match.group(1)
            log_fence = line[match.end() :].strip().lower() in {
                "log",
                "console",
                "output",
            }
            continue
        if match := _INTENT_START.match(line):
            active = True
            content = line[match.end() :]
            awaiting_content = not content.strip()
            out.append(content)
        elif _EVIDENCE_START.match(line) or line.startswith("#"):
            active = awaiting_content = False
        elif not line:
            if not awaiting_content:
                active = False
        elif active:
            out.append(line)
            awaiting_content = False
    return "\n".join(out)


def _write_set_lines(text: str) -> str:
    """Select declared targets and lists, excluding quoted and fenced evidence."""
    out: list[str] = []
    fence = ""
    log_fence = False
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if fence:
            if line.startswith(fence) and not line[len(fence) :].strip(fence[0]):
                fence = ""
            elif not log_fence:
                if _WRITE_LINE.match(line):
                    active = True
                if active:
                    out.append(raw)
            continue
        if match := _FENCE.match(line):
            fence = match.group(1)
            log_fence = line[match.end() :].strip().lower() in {
                "log",
                "console",
                "output",
            }
            continue
        if line.startswith(">"):
            continue
        if _WRITE_LINE.match(line):
            active = True
            out.append(raw)
        elif active and (
            re.match(r"^\s*(?:[-*+]|\d+[.)])\s", raw) or raw[:1].isspace()
        ):
            out.append(raw)
        else:
            active = False
            out.append("")
    return "\n".join(out)


def coordinator_route(
    text: str | None, deny_paths: Iterable[str], *, brief: str | None = None
) -> str | None:
    """The short reason the work routes straight to the coordinator, or
    ``None`` when a builder lane may take it. The reason is written into
    the ask body and context, so it names the signal, not advice.
    An optional newest brief has its own intent and declared write set;
    neither it nor the body can mask the other's deny paths."""
    parts = [
        strip_evidence(strip_direct_head(part))
        for part in (brief, text)
        if part and part.strip()
    ]
    deny = tuple(deny_paths)
    for body in parts:
        declared = _write_set_lines(body)
        file_set = _FILE_SET.search(declared)
        judged = file_set.group(1) if file_set else declared
        hit = deny_path_hit(judged, deny, write_intent=False)
        if hit is None and not file_set:
            intent = _change_intent(_RUN_LIST.sub(" ", _RUN_INVOCATION.sub(" ", body)))
            hit = deny_path_hit(intent, deny)
        if hit is not None:
            token, glob, sentence = hit
            quote = sentence.replace('"', "'")[:160]
            return f'names deny path `{token}` (matched `{glob}`) in "{quote}"'
    if graph_only("\n\n".join(parts)):
        return "prod-graph only — no repo diff exists for a builder to write"
    return None
