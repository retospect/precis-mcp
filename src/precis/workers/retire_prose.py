"""retire_prose — the retire flag's draft-prose edit modes (gr693791 residual 2).

The reground pass's retire stage (:func:`precis.workers.hub_refine._flag_retire`)
never deletes anything: it leaves the hub standing with a durable
``meta.reground_verdict`` record (``verdict='retire'`` plus a proposed
one-sentence groundable ``reword``), the draft-citer census, and the
``TAPROOT_REGROUND_RETIRE`` worklist tag. This module is the prose pass
that acts on that record — editing the draft sentences whose ``[fi<id>]``
inline cites pin the retired claim, through the draft store's
``edit_text_in_tx`` primitive (sha bump + ``edited`` chunk event +
base_sha concurrency guard), so every rewrite is reviewable in the draft
diff surface.

**Built here — reword-in-place** (:func:`apply_reword_in_place`): the
smallest mode and the only one that never deletes prose. The sentence
asserting the retired claim is replaced by the proposed reword and the
citation marker is re-attached verbatim — a pin or a handle list citing
this hub rides along untouched. Only two cite shapes are rewritten; every
other shape (marker mid-sentence, trailing prose after the marker, no
previous sentence for a marker-only cite sentence) is **skipped with a
named reason**, never guessed at — those are exactly the paragraphs the
frontier-model pass must read whole.

**Not built — replace-with-fact and stitch-delete.** Both delete prose
(the fact replacement drops the claim sentence's assertion; stitch-delete
removes the sentence and re-joins its neighbours), so per the spec they
wait on a draft-citer census showing zero other live citers on the
paragraph and on the reversible end-review surface. Until then they
refuse loudly if asked.

**The double gate.** Every entry point checks both halves of the retire
gate before touching prose: the caller's explicit authorization (the
``reground_claim`` job's ``authorize_retire`` param — never an env flag)
and the per-hub vetting tag (``TAPROOT_REGROUND_OK:1`` or the literal
``TAPROOT:reground-ok``), so the destructive-adjacent stage can never
fire on a hub nobody vetted.

**The proposal itself.** This module applies ``meta.reground_verdict``
as stored; it does not generate the reword. Until the frontier proposer
exists, a human writes the proposal through the ordinary finding-edit
door. With no proposal stored the apply refuses with a next step, and the
hub simply rests flagged on the worklist — the stub's safe resting state.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.errors import BadInput
from precis.store._draft_ops import content_sha
from precis.store._refs_ops import fi_cite_pattern
from precis.store.types import Tag
from precis.taproot.hub import append_reground_log

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Value stamped on ``meta.reground_verdict['prose_pass']`` once the
#: reword has been applied — the idempotency guard: a re-run must never
#: rewrite the already-rewritten sentence.
PROSE_PASS_REWORD_APPLIED = "reword-applied"


@dataclass(frozen=True)
class RewordPlan:
    """One live draft chunk whose marker-carrying sentence will be
    reworded — the plan/apply pair shares this shape, so a dry run lists
    exactly what a real run would write."""

    chunk_id: int
    handle: str
    draft_ref_id: int
    old_text: str
    new_text: str


@dataclass(frozen=True)
class RewordSkip:
    """A citing chunk the deterministic splice refused — named, counted,
    never silently dropped."""

    chunk_id: int
    draft_ref_id: int
    reason: str


@dataclass(frozen=True)
class RetireProseResult:
    """What :func:`apply_reword_in_place` did. ``skipped`` is load-bearing:
    a caller reading only the applied count must still see the paragraphs
    left for the frontier pass."""

    hub_ref_id: int
    reworded: tuple[RewordPlan, ...] = ()
    skipped: tuple[RewordSkip, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "hub": self.hub_ref_id,
            "reworded": [
                {"chunk_id": p.chunk_id, "draft_ref_id": p.draft_ref_id}
                for p in self.reworded
            ],
            "skipped": [
                {"chunk_id": s.chunk_id, "reason": s.reason} for s in self.skipped
            ],
        }


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
#: A candidate boundary right after one of these is an abbreviation's
#: period, not a sentence end ("et al. ", "e.g. ", "Fig. 3", "p. 5", a
#: lone initial) — the splitter must not cut there.
_ABBREV_TAIL = re.compile(
    r"(?:^|[\s(\[])(?:et al|e\.g|i\.e|cf|vs|viz|fig|figs|eq|eqs|ref|refs|"
    r"no|nos|p|pp|vol|ch|sec|approx|ca|dr|prof|mr|mrs|ms|st|[A-Za-z])\.$",
    re.IGNORECASE,
)
#: A post-marker tail so bare it can be dropped when the marker is
#: re-attached at the sentence's end: whitespace + terminal punctuation
#: only. A tail holding a real character (a parenthesis, words) means the
#: marker is NOT sentence-final and the splice would orphan prose.
_CLOSERS_ONLY = re.compile(r"[\s.!?\"']*")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of ``text``'s sentences (whitespace between
    sentences excluded, so a span's ``text[a:b]`` is exactly one
    sentence)."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_BOUNDARY.finditer(text):
        if _ABBREV_TAIL.search(text[start : m.start()]):
            continue
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _reword_sentence(
    text: str, hub_ref_id: int, reword: str
) -> tuple[str | None, str | None]:
    """Compute the reworded chunk text for ONE marker occurrence.

    Returns ``(new_text, None)`` on a shape the deterministic splice
    handles, else ``(None, reason)``. Two shapes, and only two:

    * **Trailing cite** — ``"<assertion> [fi<id>]."``: the post-marker
      tail is closers-only, so the assertion is everything before the
      marker. Replaced by the reword; the marker re-attaches at the end.
    * **Marker-own sentence** — ``"<assertion>. [fi<id>]."``: the marker
      sits in a sentence of its own (typically right after the assertion
      it grounds). The *previous* sentence is replaced by the reword;
      the marker sentence is untouched.

    Repeated hub markers make the target assertion ambiguous. An opening
    bracket in the replaced span may introduce a cite or cross-reference.
    Both shapes need the frontier pass to read the whole paragraph.
    """
    marker = re.compile(fi_cite_pattern(hub_ref_id))
    m = marker.search(text)
    if m is None:
        return None, "no live marker (already rewritten?)"
    if marker.search(text, m.end()) is not None:
        return None, "multiple markers cite this hub in the chunk"
    core = reword.strip()
    if not core:
        return None, "empty reword proposal"
    spans = _sentence_spans(text)
    for i, (a, b) in enumerate(spans):
        if not (a <= m.start() and m.end() <= b):
            continue
        sentence = text[a:b]
        pre = text[a : m.start()]
        post = text[m.end() : b]
        punct = re.search(r"([.!?]+)\s*$", sentence)
        terminal = punct.group(1) if punct else "."
        core_body = core.rstrip(".!?").rstrip()
        core_punct = core if core.endswith((".", "!", "?")) else core + terminal
        if pre.strip():
            # Trailing-cite shape: only when nothing real follows the
            # marker inside this sentence. The reword takes the marker's
            # terminal punctuation; the marker sits sentence-final.
            if not _CLOSERS_ONLY.fullmatch(post):
                return None, "marker not sentence-final (prose follows it)"
            if "[" in pre:
                return None, "sentence being replaced may hold another cite marker"
            lead = core if core.endswith((".", "!", "?")) else core_body
            new_sentence = f"{lead} {m.group(0)}{terminal}"
            return text[:a] + new_sentence + text[b:], None
        # Marker at the sentence's start. Its own sentence must be nothing
        # but the marker (terminal punctuation allowed); then the reword
        # replaces the PREVIOUS sentence, keeping its own punctuation.
        if not _CLOSERS_ONLY.fullmatch(post):
            return None, "marker leads a sentence with prose after it"
        if i == 0:
            return None, "marker-only sentence at the chunk start"
        pa, pb = spans[i - 1]
        if "[" in text[pa:pb]:
            return None, "sentence being replaced may hold another cite marker"
        return text[:pa] + core_punct + text[pb:], None
    return None, "marker outside any sentence span"


def _citing_chunks(
    conn: Connection, hub_ref_id: int
) -> list[tuple[int, str, int, str]]:
    """Every live draft chunk (with its edit ``handle``) carrying this
    hub's ``[fi<id>]`` marker — the citer census the retire stage
    computes, plus the addressing the edit primitive needs. Chunks with
    no handle are unreadable by the edit door and are left for a human,
    exactly the cite-rewrite planner's rule."""
    rows = conn.execute(
        "SELECT c.chunk_id, c.handle, c.ref_id, c.text "
        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE r.kind = 'draft' AND r.retired_at IS NULL AND c.ord >= 0 "
        "AND c.retired_at IS NULL AND c.handle IS NOT NULL "
        "AND c.text ~ %s ORDER BY c.ref_id, c.chunk_id",
        (fi_cite_pattern(hub_ref_id),),
    ).fetchall()
    return [(int(cid), str(h), int(rid), str(t)) for cid, h, rid, t in rows]


def _lock_drafts(conn: Connection, draft_ref_ids: list[int]) -> None:
    """The parent draft rows of the rewrite set, locked ``FOR UPDATE`` in
    ``ref_id`` order after the hub row lock — the cite-rewrite lock order
    (``taproot.hub._lock_cite_drafts``), kept locally per the cross-task
    seam precedent."""
    if not draft_ref_ids:
        return
    conn.execute(
        "SELECT 1 FROM refs WHERE ref_id = ANY(%s) ORDER BY ref_id FOR UPDATE",
        (sorted({int(d) for d in draft_ref_ids}),),
    )


def _tagged_ok(conn: Connection, hub_ref_id: int) -> bool:
    """The per-hub half of the retire double gate: the vetting tag a
    human attached (the code-safe side-channel namespace or the literal
    spec form — see ``hub_refine._REGROUND_OK_NS``)."""
    from precis.workers.hub_refine import (
        _REGROUND_OK_NS,
        _REGROUND_OK_SPEC_NS,
        _REGROUND_OK_SPEC_VALUE,
        _REGROUND_OK_VALUE,
    )

    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND ((t.namespace = %s AND t.value = %s)
             OR (t.namespace = %s AND t.value = %s))
         LIMIT 1
        """,
        (
            hub_ref_id,
            _REGROUND_OK_NS,
            _REGROUND_OK_VALUE,
            _REGROUND_OK_SPEC_NS,
            _REGROUND_OK_SPEC_VALUE,
        ),
    ).fetchone()
    return row is not None


def plan_reword_in_place(
    conn: Connection, hub_ref_id: int, reword: str
) -> tuple[list[RewordPlan], list[RewordSkip]]:
    """The read-only plan: every citing chunk plus its reworded text, and
    every chunk the splice refused with its reason. No writes."""
    plans: list[RewordPlan] = []
    skips: list[RewordSkip] = []
    for chunk_id, handle, draft_ref_id, text in _citing_chunks(conn, hub_ref_id):
        new_text, reason = _reword_sentence(text, hub_ref_id, reword)
        if new_text == text:
            continue  # already carries the reword (a partial pass's rerun)
        if new_text is None:
            skips.append(RewordSkip(chunk_id, draft_ref_id, reason or "unhandled"))
        else:
            plans.append(
                RewordPlan(
                    chunk_id=chunk_id,
                    handle=handle,
                    draft_ref_id=draft_ref_id,
                    old_text=text,
                    new_text=new_text,
                )
            )
    return plans, skips


def apply_reword_in_place(
    store: Store,
    hub_ref_id: int,
    *,
    authorized: bool,
    set_by: str = "system",
) -> RetireProseResult:
    """Apply the retire record's proposed reword to every citing draft
    sentence (reword-in-place), behind the retire double gate.

    Raises :class:`~precis.errors.BadInput` — with a next step, never a
    bare refusal — when the gate is closed, the hub carries no retire
    verdict or no proposal, or the reword was already applied
    (idempotency: a second run must never re-splice its own output).
    Skipped chunks (unsafe cite shapes) are returned, not raised, so one
    hard paragraph never blocks the rest of the scope.
    """
    from precis.workers.hub_refine import (
        _META_REGROUND_VERDICT,
        _REGROUND_RETIRE_NS,
        _REGROUND_RETIRE_VALUE,
        VERDICT_RETIRE,
    )

    if not authorized:
        raise BadInput(
            "retire prose edit refused: not authorized",
            next=(
                "pass authorize_retire=true on the reground_claim job — "
                "the run-level half of the retire double gate"
            ),
        )

    with store.pool.connection() as conn:
        # The hub row lock serializes against a concurrent reground pass
        # stamping the same verdict (residual-5 discipline) and makes the
        # verdict read + prose_pass stamp atomic with the draft edits.
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR UPDATE",
            (hub_ref_id,),
        ).fetchone()
        if row is None:
            raise BadInput(
                f"hub fi{hub_ref_id} is gone",
                next="drop it from the job's hub scope",
            )
        meta = dict(row[0] or {})
        verdict = dict(meta.get(_META_REGROUND_VERDICT) or {})
        if verdict.get("prose_pass") == PROSE_PASS_REWORD_APPLIED:
            return RetireProseResult(
                hub_ref_id=hub_ref_id,
                skipped=(RewordSkip(0, 0, "reword already applied"),),
            )
        if verdict.get("verdict") != VERDICT_RETIRE:
            raise BadInput(
                f"hub fi{hub_ref_id} carries no retire verdict "
                f"(reground_verdict={verdict.get('verdict')!r})",
                next=(
                    "run the reground pass with external=true and "
                    "authorize_retire on this hub first, so the retire "
                    "record and its citer census exist"
                ),
            )
        reword = str(verdict.get("reword") or "").strip()
        if not reword:
            raise BadInput(
                f"hub fi{hub_ref_id}'s retire record carries no proposed reword",
                next=(
                    "write one into meta.reground_verdict.reword through the "
                    "finding-edit door (the automated proposer is not built "
                    "yet), then re-run"
                ),
            )
        if not _tagged_ok(conn, hub_ref_id):
            raise BadInput(
                f"hub fi{hub_ref_id} has no retire vetting tag",
                next=(
                    "a human must tag the hub TAPROOT_REGROUND_OK:1 (or "
                    "TAPROOT:reground-ok) after reviewing the proposed "
                    "reword — the per-hub half of the double gate"
                ),
            )

        plans, skips = plan_reword_in_place(conn, hub_ref_id, reword)
        _lock_drafts(conn, [p.draft_ref_id for p in plans])
        applied: list[RewordPlan] = []
        for p in plans:
            live = conn.execute(
                "SELECT retired_at FROM refs WHERE ref_id = %s FOR UPDATE",
                (p.draft_ref_id,),
            ).fetchone()
            if live is None or live[0] is not None:
                # Soft-deleted after the plan read — the cite-rewrite rule:
                # never write into dead prose.
                skips.append(
                    RewordSkip(
                        p.chunk_id, p.draft_ref_id, "draft retired after planning"
                    )
                )
                continue
            try:
                store.drafts.edit_text_in_tx(
                    conn,
                    p.handle,
                    p.new_text,
                    base_sha=content_sha(p.old_text),
                    source={
                        "kind": "reground-retire",
                        "hub_ref_id": hub_ref_id,
                        "set_by": set_by,
                    },
                )
            except Exception:
                log.warning(
                    "retire_prose: reword failed for chunk %s (hub #%d)",
                    p.handle,
                    hub_ref_id,
                    exc_info=True,
                )
                skips.append(RewordSkip(p.chunk_id, p.draft_ref_id, "edit refused"))
                continue
            applied.append(p)

        if applied or not skips:
            complete = not skips
            verdict.update(
                {
                    "prose_pass": (
                        PROSE_PASS_REWORD_APPLIED if complete else "reword-partial"
                    ),
                    "prose_pass_at": datetime.now(UTC).isoformat(),
                    "prose_pass_skipped": [s.chunk_id for s in skips],
                }
            )
            store.update_ref(
                hub_ref_id,
                meta_patch={_META_REGROUND_VERDICT: verdict},
                conn=conn,
            )
            # The worklist flag means "needs the prose pass" — applied
            # means it doesn't. The verdict record keeps the history.
            # Only when nothing was skipped: skipped paragraphs stay on
            # the worklist for the frontier pass.
            if complete:
                store.remove_tag(
                    hub_ref_id,
                    Tag.closed(_REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE),
                    conn=conn,
                )
            append_reground_log(
                store,
                hub_ref_id,
                [
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "edge": f"fi{hub_ref_id}",
                        "src_ref_id": hub_ref_id,
                        "src_chunk_id": None,
                        "relation": "(hub)",
                        "verdict": VERDICT_RETIRE,
                        "reason": (
                            f"reword-in-place applied to {len(applied)} draft "
                            f"chunk(s); {len(skips)} skipped for the frontier pass"
                        ),
                        "action": "reword-applied" if complete else "reword-partial",
                        "sha": verdict.get("sha"),
                    }
                ],
                conn=conn,
            )
        conn.commit()

    return RetireProseResult(
        hub_ref_id=hub_ref_id,
        reworded=tuple(applied),
        skipped=tuple(skips),
    )


__all__ = [
    "PROSE_PASS_REWORD_APPLIED",
    "RetireProseResult",
    "RewordPlan",
    "RewordSkip",
    "apply_reword_in_place",
    "plan_reword_in_place",
]
