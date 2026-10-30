"""Tiny shared helper used by ``finding.py`` and its split-out helper
modules (``_finding_acquire``, ``_finding_edit``, ``_finding_evidence``).

Kept separate (rather than importing from ``finding.py`` itself) so those
helper modules don't have to import back into the module that imports
them — ``finding.py``'s ``FindingHandler`` pulls all of these in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.handlers._gripe_title import cut_at_word_boundary
from precis.store.types import Ref

if TYPE_CHECKING:
    from precis.store import Store

#: ``refs.title`` cap for a finding. ``put`` silently cuts an over-long
#: ``title=`` at a word boundary — Reto's gripe-title ruling ("silently
#: cut, put it inside", gr538410): the full original text goes to the
#: top of the finding body, so nothing is lost and ``edit(title=)``
#: (hub-only anyway) is never the fix door.
FINDING_TITLE_MAX = 80


def cap_finding_title(title: str, body: str) -> tuple[str, str]:
    """Cap a finding title at :data:`FINDING_TITLE_MAX`; returns
    ``(title, body)``, both stripped.

    An over-long title is cut at a word boundary and the full original
    text is kept at the top of ``body`` when the body doesn't already
    lead with it — the gripe-title ruling applied to findings
    (gr538410). A title at or under the cap passes through.
    """
    title = title.strip()
    body = body.strip()
    if len(title) <= FINDING_TITLE_MAX:
        return title, body
    if not body.startswith(title):
        body = f"{title}\n\n{body}" if body else title
    return cut_at_word_boundary(title, FINDING_TITLE_MAX), body


def fetch_ref_any_kind(store: Store, ref_id: int) -> Ref:
    """Look up a ref by id without knowing its kind.

    The store's ``get_ref`` API requires ``kind``; ``parse_link_target``
    returns the resolved kind on the ``LinkTarget`` so callers can
    round-trip. Re-fetching here reads the slug (cite_key) for the
    deterministic pub_id input, and is reused everywhere a finding-family
    caller needs a handle for an arbitrary link endpoint (put's cited_in
    resolution, the candidate-pick / evidence-render paths, the begat
    detail render).
    """
    from precis.store._mappers import _REFS_COLS, _row_to_ref

    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"cited_in target ref_id={ref_id} not found",
            next=(
                "the target was deleted or never existed — find a live one "
                "with search(kind='paper', q='<topic>') or look up by DOI "
                "with get(kind='paper', id='<doi>')"
            ),
        )
    return _row_to_ref(row)


def multi_article_advisory(store: Store, paper_ref_id: int, handle: str) -> str:
    """One-line advisory when the cited paper is tagged ``multi-article``
    (gr463414), else ``""``. The write still succeeds; the agent confirms
    the cited passage belongs to this paper before relying on the edge."""
    if not store.is_multi_article(paper_ref_id):
        return ""
    return (
        f"\nadvisory: {handle}'s stored body bundles several articles "
        "(tag multi-article) - confirm the cited passage belongs to this "
        "paper (its page carries this paper's own title or DOI) before "
        "relying on the edge"
    )
