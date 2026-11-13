"""The pipeline-writer seam for the gripe summary fold (gr595551).

The fold itself — old summary + new ledger entry → new summary — lives
with its domain owner, :mod:`precis.handlers._gripe_summary`, but the
pipeline's stage writers (``precis.workers.gripe_triage``,
``precis.fixer.writeback``) sit on layers the import contracts forbid
from reaching ``handlers`` directly. :func:`fold` is the shared
fail-soft helper both call after their ledger write commits: it
resolves the gripe, runs the rebuild, and swallows every fault — the
comment already landed, so a rebuild failure leaves the card's summary
marked ``stale`` (``edit(mode='summary')`` retries) instead of failing
the stage's write.

:func:`mint_anchors` is the write half of the same seam: a stage whose
entry names code locations (the fixer's ``touched files:`` /
``changed symbols:`` lines) turns ``path``/``path::qual`` strings into
``an<N>`` handles before the entry lands — the ledger's contract is
anchors only, never raw paths or symbols. :func:`anchorize` is the
free-text variant: it rewrites the locations inside a prose string
(the triage tick's suspected cause / recommend / project-why / dup
why, the fixer write-back's outcome head) to their handles — or to
the unminted redaction on a mint fault, never raw.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("precis.utils.gripe_summary")


def fold(store: Any, ref_id: int) -> None:
    """Fail-soft rebuild of gripe ``ref_id``'s mutable summary block.

    A gripe with nothing newer than ``through`` costs no model call —
    :func:`precis.handlers._gripe_summary.rebuild_summary` returns
    'already current' first. Never raises.
    """
    try:
        from precis.handlers import _gripe_summary as gs

        ref = store.get_ref(kind="gripe", id=ref_id)
        if ref is not None:
            gs.rebuild_summary(store, ref)
    except Exception:
        log.warning("summary fold failed for gripe:%d", ref_id, exc_info=True)


def mint_anchors(
    store: Any, locations: Iterable[str], *, gripe: int | None = None
) -> dict[str, str]:
    """Fail-soft find-or-mint of ``an<N>`` handles for code locations.

    Delegates to :func:`precis.handlers._gripe_summary.mint_anchors`;
    returns ``{location: handle}``, or ``{}`` on any fault — the caller
    then writes its entry without location lines (raw paths or symbols
    may NEVER substitute: the ledger's contract is anchors only,
    gr595551).
    """
    try:
        from precis.handlers import _gripe_summary as gs

        return gs.mint_anchors(store, locations, gripe=gripe)
    except Exception:
        log.warning("anchor mint failed", exc_info=True)
        return {}


def anchorize(store: Any, text: str, *, gripe: int | None = None) -> str:
    """Fail-soft rewrite of the raw code locations in ``text`` to
    ``an<N>`` handles.

    Delegates to :func:`precis.handlers._gripe_summary.anchorize_text`.
    On a mint fault the locations redact to the unminted marker —
    the ledger's contract is anchors only and a raw path may NEVER
    substitute (review of td655156).
    """
    try:
        from precis.handlers import _gripe_summary as gs

        try:
            return gs.anchorize_text(store, text, gripe=gripe)
        except Exception:
            log.warning("anchorize mint failed — redacting", exc_info=True)
            return gs.redact_locations(text)
    except Exception:
        log.warning("anchorize failed", exc_info=True)
        return text
