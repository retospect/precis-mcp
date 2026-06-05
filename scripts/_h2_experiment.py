"""H2-as-description retrieval experiment.

Four configurations:
  A — short H2 + body (current style)
  B — long descriptive H2 + body (nominal: "Finding a paper…")
  C — long descriptive H2 only (no body)
  D — long active goal-voice H2 + body ("Find a paper by topic when…")

Embeds each "document" with bge-m3, compares against user-vocab queries
labelled with the correct H2 section. Reports P@1, P@3, MRR per config.
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

# (doc, short_h2, long_h2_nominal, long_h2_active, body) per section
SECTIONS = [
    # search-help
    (
        "search",
        "Searching by content",
        "Finding a paper, memory, or web page when you know the topic but not the exact title or slug",
        "Find a paper, memory, or web page by topic when you don't know the exact title or slug",
        "Use search(kind=..., q='...') to look up content by topic. The query embedding is matched against chunk embeddings; results return handles you can drill into. Default top_k is 5, capped at 100. Pass kind= to scope, omit to fan out across every search-supporting kind.",
    ),
    (
        "search",
        "Excluding seen hits",
        "Paginating through search results — skipping the papers or notes you have already looked at",
        "Page through search results, skipping the papers or notes you have already seen",
        "Use exclude=['slug1', 'slug2'] to suppress hits you have already seen. The Next: trailer of every multi-hit result pre-fills this list — copy-paste, no bookkeeping.",
    ),
    (
        "search",
        "Filtering by tag",
        "Narrowing search to a specific tag — only papers marked priority high, only memories pinned",
        "Filter search results to a specific tag — only papers marked priority high, only memories pinned",
        "Pass tags=['flag1', 'flag2'] for AND-semantics filtering. Tag prefixes use UPPERCASE:value for replace-in-prefix, lowercase:value for accumulate, bare flags toggle.",
    ),
    # put-help
    (
        "put",
        "Creating a memory",
        "Saving a quick note or scratch thought to revisit later",
        "Save a quick note or scratch thought to revisit later",
        "Use put(kind='memory', text='...') to create a numeric-ref memory. Add tags at creation with tags=['pinned']. For files use mode='create' instead.",
    ),
    (
        "put",
        "Creating a file",
        "Writing a new markdown or plaintext file under PRECIS_ROOT",
        "Create a new markdown or plaintext file under PRECIS_ROOT",
        "Use put(kind='markdown', mode='create', id='notes/foo.md', text='...') for files. mode='create' is required for file kinds. Region edits live on the edit verb, not put.",
    ),
    (
        "put",
        "Linking at creation",
        "Attaching a cross-reference to another ref when creating something new",
        "Attach a cross-reference to another ref when creating something new",
        "Use link='paper:wang2020state', rel='cites' to attach a typed edge at creation time. For retroactive link changes use the link verb.",
    ),
    # preflight
    (
        "preflight",
        "The 30-second version",
        "Quickstart: running a citation audit on a manuscript before release",
        "Run a citation audit on a manuscript before release",
        "Pull DOIs from your bibtex, run precis jobs check-provenance --refs preflight.txt, read the resulting markdown for retracted, expression-of-concern, and correction findings.",
    ),
    (
        "preflight",
        "Severity buckets",
        "Understanding the severity scale of the citation audit — blocker, review, correction, info, unknown",
        "Understand the severity scale of the citation audit — blocker, review, correction, info, unknown",
        "Blocker = retraction, drop the citation. Review = expression of concern or cites-retracted, requires human judgement. Correction = corrigendum, usually housekeeping. Info = clean. Unknown = Crossref 404.",
    ),
    (
        "preflight",
        "How to interpret cites-retracted-work",
        "Deciding whether a citation to retracted work is load-bearing in your argument or peripheral",
        "Decide whether a citation to retracted work is load-bearing or peripheral",
        "Read the citing paper's use of the contested source. Background context or alternative-source citations are usually fine; foundational claims that depend on the retracted result are blockers.",
    ),
]

# (query, expected_section_index)
QUERIES = [
    ("how do I look up a paper by topic", 0),
    ("I already saw the first 5 results, show me the next ones", 1),
    ("filter memory results by a pinned tag", 2),
    ("save a quick scratch note for later", 3),
    ("create a new markdown notes file", 4),
    ("link a new memory to a paper I am citing", 5),
    ("audit my manuscript citations before submitting", 6),
    ("what does the orange review severity mean in the audit", 7),
    ("is a citation to a retracted paper always a blocker", 8),
    ("find content by keyword across my notes", 0),
    ("I want to make a new ref that cites a paper", 5),
    ("skip to next page of search results", 1),
    ("write down a thought I just had", 3),
    ("create a markdown file in PRECIS_ROOT", 4),
    ("retraction watch check on my bibliography", 6),
]


def build_corpus(mode: str) -> list[str]:
    """Return embedded text per section for the given config."""
    docs = []
    for _, short_h2, long_h2_nominal, long_h2_active, body in SECTIONS:
        if mode == "A":
            docs.append(f"{short_h2}\n\n{body}")
        elif mode == "B":
            docs.append(f"{long_h2_nominal}\n\n{body}")
        elif mode == "C":
            docs.append(long_h2_nominal)
        elif mode == "D":
            docs.append(f"{long_h2_active}\n\n{body}")
        else:
            raise ValueError(mode)
    return docs


def evaluate(model: SentenceTransformer, mode: str) -> dict[str, object]:
    corpus = build_corpus(mode)
    corpus_emb = model.encode(corpus, normalize_embeddings=True)
    query_emb = model.encode([q for q, _ in QUERIES], normalize_embeddings=True)

    sims = query_emb @ corpus_emb.T  # cosine since normalized
    p1 = p3 = mrr = 0.0
    n = len(QUERIES)
    misses: list[str] = []
    for i, (q, expected) in enumerate(QUERIES):
        ranked = np.argsort(-sims[i])
        rank = int(np.where(ranked == expected)[0][0]) + 1
        if rank == 1:
            p1 += 1
        if rank <= 3:
            p3 += 1
        mrr += 1.0 / rank
        if rank > 1:
            top = int(ranked[0])
            misses.append(
                f"  q={q!r}\n    expected #{expected} "
                f"({SECTIONS[expected][1]}), got rank {rank}; "
                f"top was #{top} ({SECTIONS[top][1]})"
            )
    return {
        "P@1": p1 / n,
        "P@3": p3 / n,
        "MRR": mrr / n,
        "misses": misses,
    }


LABELS = {
    "A": "A: short H2 + body (current)",
    "B": "B: long descriptive H2 + body (nominal)",
    "C": "C: long descriptive H2 only (no body)",
    "D": "D: long active goal-voice H2 + body (proposed)",
}


def main() -> None:
    print("loading bge-m3 …")
    model = SentenceTransformer("BAAI/bge-m3")
    print(f"corpus: {len(SECTIONS)} sections, {len(QUERIES)} queries\n")

    for mode in ("A", "B", "C", "D"):
        r = evaluate(model, mode)
        print(LABELS[mode])
        print(f"  P@1 = {r['P@1']:.2f}   P@3 = {r['P@3']:.2f}   MRR = {r['MRR']:.3f}")
        misses = r["misses"]
        assert isinstance(misses, list)
        if misses:
            print("  not-top-1 misses:")
            for m in misses:
                print(m)
        print()


if __name__ == "__main__":
    main()
