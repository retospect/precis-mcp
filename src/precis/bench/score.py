"""QASPER's own answer metric, for the LLM-agent tier of the ablation bench.

The scripted tier produces no answer text and never calls this. Follows the
QASPER paper's evaluator (SQuAD-style): normalise (lowercase, strip
punctuation, drop the articles a/an/the, collapse whitespace), token-F1
against each acceptable answer and take the max over annotators; yes/no
answers are exact; an unanswerable question is answered correctly by
abstaining. Rows are the ``bench/tasks/qasper-*.jsonl`` shape: ``answer``
(list of acceptable strings, yes/no as ``"yes"``/``"no"``) and
``answer_kind`` (``extractive``, ``abstractive``, ``yes_no`` or
``unanswerable``).
"""

from __future__ import annotations

import re
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

ABSTAIN = "unanswerable"
_PUNCT = frozenset(string.punctuation)
_ARTICLES = re.compile(r"\b(a|an|the)\b")


def normalize(text: str) -> str:
    """QASPER/SQuAD normalisation: lowercase, no punctuation, no articles,
    single spaces."""
    t = "".join(ch for ch in text.lower() if ch not in _PUNCT)
    return " ".join(_ARTICLES.sub(" ", t).split())


def is_abstention(pred: str) -> bool:
    """True for an empty prediction or the literal ``Unanswerable``."""
    return normalize(pred) in ("", ABSTAIN)


def token_f1(pred: str, gold: str) -> float:
    """Token-F1 of two strings after :func:`normalize`. Two empty strings
    score 1.0; one empty scores 0.0."""
    p, g = normalize(pred).split(), normalize(gold).split()
    if not p or not g:
        return float(p == g)
    common = sum((Counter(p) & Counter(g)).values())
    if common == 0:
        return 0.0
    prec, rec = common / len(p), common / len(g)
    return 2 * prec * rec / (prec + rec)


def score_answer(pred: str, answers: Sequence[str], answer_kind: str) -> float:
    """Score one prediction in [0, 1] against a row's acceptable answers."""
    if answer_kind == "unanswerable" or not answers:
        return float(is_abstention(pred))
    if is_abstention(pred):
        return 0.0
    if answer_kind == "yes_no":
        got = normalize(pred)
        return float(any(got == normalize(a) for a in answers))
    return max(token_f1(pred, a) for a in answers)


def score_row(pred: str, row: Mapping[str, Any]) -> float:
    """:func:`score_answer` for a QASPER task row."""
    return score_answer(pred, row.get("answer") or [], str(row.get("answer_kind", "")))
