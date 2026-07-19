"""precis-fix — the human→LLM→card feedback loop (slice 2.5).

Tag a card **`precis-fix`** inside Anki and write what's wrong in a note field
(the "comment"). The sync tick then reads the tagged cards, has an LLM rewrite
each from the comment, writes the fix **back to that card**, and swaps the tag
to `precis-fixed`. The tag is the user's explicit per-card consent to edit that
one foreign note — a deliberate, bounded widening of the add-only-own-notes
floor. An *un-tagged* foreign note is still never touched.

Split so the deterministic halves test locally (no LLM, no network):

- `find_fix_requests(col)` — read the tagged cards + pull the instruction.
- `apply_fix(col, ...)` — write the new fields back + swap the tag.
- `propose_fix(req)` — the one LLM call (stub-mockable via PRECIS_CLAUDE_BIN).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Tag the human puts on a card in Anki to request a fix.
FIX_TAG = "precis-fix"
#: Tag precis swaps in once the fix is applied.
FIXED_TAG = "precis-fixed"

#: Fields checked, in order, for the human's fix instruction.
_INSTRUCTION_FIELDS = ("precis-fix", "Back Extra", "Extra", "Back", "Comment")


@dataclass
class FixRequest:
    note_id: int
    guid: str
    notetype: str
    fields: dict[str, str]
    instruction: str
    ref_id: int | None = None


def _extract_instruction(fields: dict[str, str]) -> str:
    """The human's fix comment — first non-empty of the known instruction
    fields. Empty string if none (the LLM then just cleans the card up)."""
    for key in _INSTRUCTION_FIELDS:
        val = fields.get(key)
        if val and val.strip():
            return val.strip()
    return ""


def find_fix_requests(col: Any) -> list[FixRequest]:
    """Every card tagged `precis-fix`, with its instruction extracted. Pure
    read — no card is modified here."""
    from precis.anki.sync import read_all_cards

    out: list[FixRequest] = []
    for c in read_all_cards(col, tag=FIX_TAG):
        out.append(
            FixRequest(
                note_id=c.note_id,
                guid=c.guid,
                notetype=c.notetype,
                fields=c.fields,
                instruction=_extract_instruction(c.fields),
                ref_id=c.ref_id,
            )
        )
    return out


def build_fix_prompt(req: FixRequest) -> str:
    """The one-shot fix prompt. Asks for corrected field values as JSON, keyed by
    the SAME field names, so `apply_fix` can write them straight back."""
    cloze_note = (
        "\nThis is an Anki CLOZE card: keep the {{cN::…}} deletion markup intact "
        "(you may adjust which spans are hidden, but every card must keep at "
        "least one {{cN::…}})."
        if req.notetype == "Cloze"
        else ""
    )
    instruction = req.instruction or (
        "No explicit comment was left — improve clarity/correctness minimally."
    )
    return (
        "You are an expert spaced-repetition card editor. Fix the Anki card "
        "below per the user's comment. Preserve the card's intent; change only "
        "what the comment asks for (plus obvious errors)." + cloze_note + "\n\n"
        f"Notetype: {req.notetype}\n"
        f"Fields (JSON): {json.dumps(req.fields, ensure_ascii=False)}\n"
        f"User comment: {instruction}\n\n"
        "Return ONLY a JSON object mapping the SAME field names to their "
        "corrected values. Include only fields you changed. Example: "
        '{"Text": "corrected text"}'
    )


def propose_fix(req: FixRequest, *, model: str | None = None) -> dict[str, str]:
    """Ask the LLM for corrected field values. Returns only changed fields whose
    names exist on the note (a hallucinated field name is dropped).

    Routes through the ADR 0046 seam (:func:`~precis.utils.llm.router.dispatch`)
    on the ``CLOUD_SMALL`` tier so the ``/factory`` backend switch reaches it
    (ADR 0046 unit 4b). Under the default ``anthropic`` backend this resolves to
    the ``claude_p`` transport — the same one-shot JSON judge the direct call
    used — and ``LlmResult.data`` carries the parsed dict exactly as
    ``ClaudePResult.data`` did; when ``PRECIS_LLM_BACKEND=openai`` the OSS
    ``openai_compat`` lane parses the same trailing JSON into ``.data``. A
    transport failure is re-raised (``dispatch`` folds it into ``result.error``
    rather than raising) so :func:`run_fix_pass` counts it and leaves the card
    tagged for a retry instead of swapping its tag on an empty result.
    """
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    result = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SMALL,
            prompt=build_fix_prompt(req),
            model=model,
            source="anki:fix",
            ref_id=req.ref_id,
        )
    )
    if result.error is not None:
        raise RuntimeError(f"anki propose_fix failed: {result.error}")
    proposed = result.data if isinstance(result.data, dict) else {}
    return {
        k: str(v)
        for k, v in proposed.items()
        if k in req.fields and str(v).strip() and str(v) != req.fields.get(k)
    }


def apply_fix(col: Any, note_id: int, new_fields: dict[str, str]) -> bool:
    """Write the corrected fields back to the note and swap `precis-fix` →
    `precis-fixed`. Returns True if anything changed. This is the one place
    precis edits a foreign card — reached only for a `precis-fix`-tagged note."""
    note = col.get_note(note_id)
    changed = False
    for name, val in new_fields.items():
        if name in note.keys() and note[name] != val:  # noqa: SIM118 (anki Note)
            note[name] = val
            changed = True
    tags = [t for t in note.tags if t != FIX_TAG]
    if FIXED_TAG not in tags:
        tags.append(FIXED_TAG)
    if tags != note.tags:
        note.tags = tags
        changed = True
    if changed:
        col.update_note(note)
    return changed


@dataclass
class FixResult:
    requested: int = 0
    fixed: int = 0
    unchanged: int = 0
    errors: int = 0


def run_fix_pass(col: Any, *, model: str | None = None) -> FixResult:
    """Full precis-fix pass over the mirror: find tagged cards → LLM rewrite →
    write back + tag swap. Call between the download and the push in the sync
    tick so the fixes ride the same sync up to AnkiWeb."""
    res = FixResult()
    for req in find_fix_requests(col):
        res.requested += 1
        try:
            new_fields = propose_fix(req, model=model)
        except Exception:
            res.errors += 1
            continue
        # Swap the tag even if the LLM proposed no field change, so the card
        # doesn't re-enter the fix queue every tick.
        if apply_fix(col, req.note_id, new_fields):
            res.fixed += 1 if new_fields else 0
            res.unchanged += 0 if new_fields else 1
        else:
            res.unchanged += 1
    return res
