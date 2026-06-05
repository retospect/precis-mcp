"""Abbreviation-aware sentence splitter.

Wraps `pysbd <https://github.com/nipunsadvilkar/pySBD>`_ in a tiny
adapter that returns sentences with their character offsets in the
source text. The offsets are load-bearing for the persistent
discovery layer — :class:`ref_segment_sentences` stores
``char_offset`` per sentence so the verifier sub-call can grep the
verbatim chunk text for the exact span.

Why pysbd over alternatives:

* **Scientific-prose aware** — extensive abbreviation list ("et al.",
  "Fig.", "i.e.", "vs.", "cf.", "approx.") and rule-based handling
  of citation parentheticals "(Smith et al., 2020)" and equation-
  bearing sentences. Tuned for academic text.
* **No model download** — pure-Python rules engine, ~50 KB on disk.
  Plays well with the bake-models-into-image / cold-start budget.
* **Deterministic and fast** — ~1-2 ms per kilobyte of input. The
  worker pass for a 50-segment paper takes <1 s of splitter time.

The single public function is :func:`split_sentences`; callers should
record :data:`SENTENCE_SPLITTER_VERSION` on every row that depends on
the resulting offsets so a future splitter change can be detected via
the lazy-invalidation discipline.
"""

from __future__ import annotations

from dataclasses import dataclass

import pysbd

# Bump this whenever a pysbd upgrade (or splitter swap) materially
# changes the offsets we emit. Read-time consumers compare this against
# the row's stored ``sentence_splitter_version`` and recompute on
# mismatch. Format: ``<engine>-<engine-version>-<adapter-version>``.
SENTENCE_SPLITTER_VERSION = "pysbd-0.3-1"


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence located in its source text.

    ``char_offset`` is the absolute byte offset into the *input text*
    passed to :func:`split_sentences`. Callers that need an offset
    into a larger document (e.g. into the chunk that contained the
    segment) are responsible for adding the chunk's own offset.
    """

    text: str
    char_offset: int


# Module-level singleton — pysbd's Segmenter is internally stateful
# (compiled regexes) and trivially safe to share across threads.
_SEGMENTER = pysbd.Segmenter(language="en", clean=False, char_span=True)


def split_sentences(text: str) -> list[Sentence]:
    """Split ``text`` into sentences with char offsets.

    Returns ``[]`` for empty / whitespace-only input. Otherwise
    returns one :class:`Sentence` per detected sentence boundary,
    preserving original whitespace inside each sentence (we pass
    ``clean=False`` to pysbd). The text of the returned sentences,
    concatenated back together, is *not* guaranteed to equal the
    input — pysbd trims leading/trailing whitespace and drops
    inter-sentence blanks — but each sentence's offset points at its
    starting character in the input, so callers can grep the
    original text.
    """
    if not text or not text.strip():
        return []
    out: list[Sentence] = []
    for span in _SEGMENTER.segment(text):
        # pysbd's ``char_span=True`` mode returns objects with
        # ``.sent``, ``.start``, ``.end`` attributes. Some pysbd
        # builds return a plain string when the input degenerates to
        # a single token — guard for that.
        sent = getattr(span, "sent", span)
        start = getattr(span, "start", 0)
        if isinstance(sent, str) and sent.strip():
            out.append(Sentence(text=sent, char_offset=int(start)))
    return out


__all__ = ["SENTENCE_SPLITTER_VERSION", "Sentence", "split_sentences"]
