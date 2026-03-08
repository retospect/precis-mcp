"""RAKE keyword extraction — zero external dependencies.

Rapid Automatic Keyword Extraction (Rose et al., 2010).
Vendored stopword list + algorithm in ~60 lines.
"""

from __future__ import annotations

import re

# fmt: off
STOPWORDS: frozenset[str] = frozenset({
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for",
    "from", "further", "get", "got", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "isn't",
    "it", "its", "itself", "just", "let", "let's", "like", "may", "me",
    "might", "more", "most", "mustn't", "my", "myself", "no", "nor", "not",
    "of", "off", "on", "once", "only", "or", "other", "ought", "our",
    "ours", "ourselves", "out", "over", "own", "per", "quite", "same",
    "shan't", "she", "should", "shouldn't", "so", "some", "still", "such",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then",
    "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "upon", "us", "use", "used", "using", "very",
    "was", "wasn't", "we", "were", "weren't", "what", "when", "where",
    "which", "while", "who", "whom", "why", "will", "with", "won't",
    "would", "wouldn't", "yet", "you", "your", "yours", "yourself",
    "yourselves", "also", "already", "although", "always", "among",
    "another", "around", "based", "became", "become", "becomes",
    "beginning", "behind", "besides", "bring", "brought", "came", "come",
    "comes", "despite", "either", "else", "enough", "especially", "etc",
    "even", "every", "example", "find", "first", "found", "given", "goes",
    "going", "gone", "hence", "however", "including", "indeed", "instead",
    "keep", "kept", "known", "last", "later", "least", "less", "made",
    "make", "makes", "making", "many", "much", "must", "near", "need",
    "never", "new", "next", "none", "nothing", "now", "often", "one",
    "others", "part", "perhaps", "please", "put", "rather", "really",
    "regarding", "said", "say", "says", "second", "see", "seem", "seemed",
    "seems", "seen", "several", "shall", "show", "showed", "shown",
    "shows", "since", "something", "sometimes", "still", "take", "taken",
    "tell", "therefore", "thing", "things", "think", "third", "thus",
    "together", "took", "toward", "towards", "try", "two", "unless",
    "various", "well", "went", "whether", "without", "work", "works",
    "yet",
})
# fmt: on

# Split text into candidate phrases at stopwords and punctuation
_SPLIT_RE = re.compile(r"[.!?,;:()\[\]{}\"\n\t]|\s+")
_WORD_RE = re.compile(r"[a-zA-Z0-9\-]+")


def _split_to_phrases(text: str) -> list[list[str]]:
    """Split text into candidate keyword phrases (lists of words)."""
    phrases: list[list[str]] = []
    current: list[str] = []

    for token in _SPLIT_RE.split(text):
        token = token.strip()
        if not token:
            if current:
                phrases.append(current)
                current = []
            continue
        low = token.lower()
        if low in STOPWORDS or not _WORD_RE.match(token):
            if current:
                phrases.append(current)
                current = []
        else:
            current.append(token)

    if current:
        phrases.append(current)

    return phrases


def _score_phrases(
    phrases: list[list[str]],
) -> list[tuple[float, str]]:
    """Score phrases using RAKE word co-occurrence metric.

    For each word: degree = number of words it co-occurs with in phrases.
    Phrase score = sum(degree(w) / freq(w)) for each word w in phrase.
    """
    freq: dict[str, int] = {}
    degree: dict[str, int] = {}

    for phrase in phrases:
        words = [w.lower() for w in phrase]
        d = len(words) - 1
        for w in words:
            freq[w] = freq.get(w, 0) + 1
            degree[w] = degree.get(w, 0) + d

    # degree(w) in RAKE = degree + freq (self-co-occurrence)
    for w in freq:
        degree[w] = degree[w] + freq[w]

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()

    for phrase in phrases:
        joined = " ".join(phrase)
        key = joined.lower()
        if key in seen:
            continue
        seen.add(key)
        score = sum(degree.get(w.lower(), 0) / freq.get(w.lower(), 1) for w in phrase)
        scored.append((score, joined))

    scored.sort(reverse=True)
    return scored


def telegram_precis(
    text: str,
    min_n: int = 4,
    max_n: int = 10,
    rel_thresh: float = 0.5,
    abs_min: float = 3.0,
) -> str:
    """Extract RAKE keyphrases and join with '; '.

    Returns 4–10 phrases: score-filtered with backfill from top-ranked.
    """
    phrases = _split_to_phrases(text)
    if not phrases:
        return text.strip()[:80]

    scored = _score_phrases(phrases)
    if not scored:
        return text.strip()[:80]

    max_score = scored[0][0]

    # Score-based filter
    candidates = [p for s, p in scored if s >= abs_min and s >= rel_thresh * max_score]

    # Ensure at least min_n, but no more than max_n
    all_phrases = [p for _, p in scored]
    if len(candidates) < min_n:
        candidates = all_phrases[:min_n]
    if len(candidates) > max_n:
        candidates = candidates[:max_n]

    return "; ".join(candidates)
