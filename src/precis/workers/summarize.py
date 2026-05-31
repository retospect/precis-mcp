"""RAKE keyword-extraction worker handler.

The seeded ``summarizers`` row (``0001_initial.sql``) names this
``rake-lemma`` and configures a scispacy lemmatizer:

.. code-block:: json

    {
      "lemmatizer": "scispacy",
      "model": "en_core_sci_sm",
      "max_keywords": 50,
      "min_phrase_words": 1,
      "max_phrase_words": 4
    }

This handler honours ``max_keywords`` / ``min_phrase_words`` /
``max_phrase_words`` faithfully but treats ``lemmatizer`` as
informational only — we run RAKE on lowercased surface forms with
no morphological reduction. Wiring ``scispacy`` in is a separate
change (it pulls a ~200 MB scientific spaCy model and ``scispacy``
itself which is GPL-adjacent — a non-trivial dep decision deferred
until the worker is exercised end-to-end).

The RAKE algorithm itself lives in :mod:`precis.utils.rake` — this
module is just the worker-side wrapper that persists the joined
keyword list as ``chunk_summaries.text``. ``extract_keywords`` is
re-exported here for back-compat with callers that imported it from
``precis.workers.summarize`` before the consolidation
(2026-05-31 — RAKE moved to a util shared with the agent-facing
search handlers).
"""

from __future__ import annotations

from typing import ClassVar

from psycopg import Connection

from precis.utils.rake import _STOPWORDS, extract_keywords
from precis.workers.base import ChunkRow, WorkerHandler


# ---------------------------------------------------------------------------
# Handler — wraps extract_keywords for the worker runner
# ---------------------------------------------------------------------------


class RakeLemmaHandler(WorkerHandler):
    """``summarize:rake-lemma`` worker handler.

    Persists the joined-by-``"; "`` keyword list as the
    ``chunk_summaries.text`` value.
    """

    output_table: ClassVar[str] = "chunk_summaries"
    model_column: ClassVar[str] = "summarizer"
    # RAKE on a citation list yields pure noise ("Smith Johnson",
    # "Nature Chem", journal abbreviations). Filter references the
    # same way the embedder does so card_keywords stays clean.
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = ("references",)

    def __init__(
        self,
        *,
        max_keywords: int = 50,
        min_phrase_words: int = 1,
        max_phrase_words: int = 4,
        model_name: str = "rake-lemma",
    ) -> None:
        self._max_keywords = max_keywords
        self._min_phrase_words = min_phrase_words
        self._max_phrase_words = max_phrase_words
        self.model_name = model_name
        self.name = f"summarize:{model_name}"

    # ------------------------------------------------------------------
    # process — pure RAKE on chunk text
    # ------------------------------------------------------------------

    def process(self, row: ChunkRow) -> str:
        """Return the summary string (joined keywords).

        Empty chunks return the empty string and the runner persists
        a row with ``text=''``; this is intentional — an empty
        summary is a valid (if uninteresting) fact, distinct from a
        failure marker.
        """
        keywords = extract_keywords(
            row.text,
            max_keywords=self._max_keywords,
            min_phrase_words=self._min_phrase_words,
            max_phrase_words=self._max_phrase_words,
        )
        return "; ".join(keywords)

    # ------------------------------------------------------------------
    # write_ok — INSERT into chunk_summaries
    # ------------------------------------------------------------------

    def write_ok(self, conn: Connection, chunk_id: int, payload: object) -> None:
        if not isinstance(payload, str):  # pragma: no cover — defensive
            raise TypeError(
                f"RakeLemmaHandler.write_ok expected str, got {type(payload).__name__}"
            )
        conn.execute(
            """
            INSERT INTO chunk_summaries
                (chunk_id, summarizer, text, status)
            VALUES (%s, %s, %s, 'ok')
            ON CONFLICT (chunk_id, summarizer) DO UPDATE
               SET text = EXCLUDED.text,
                   status = 'ok',
                   last_error = NULL,
                   attempts = chunk_summaries.attempts + 1
            """,
            (chunk_id, self.model_name, payload),
        )


__all__ = ["RakeLemmaHandler", "_STOPWORDS", "extract_keywords"]
