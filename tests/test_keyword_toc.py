"""Pure keyword facets preserve coverage, relevance order and shrinking drills."""

from precis.utils.keyword_toc import clean_keyword, cluster_pool


def test_clusters_cover_and_shrink() -> None:
    rows = [(i, i, ["catalysis", "solar" if i < 4 else "thermal"]) for i in range(8)]
    clusters, none = cluster_pool(rows, ["catalysis"])
    assert {r for c in clusters for r in c.papers} == set(range(8))
    assert all(2 <= len(c.papers) < 8 for c in clusters)
    assert [c.label for c in clusters] == ["solar", "thermal"]
    assert not none


def test_query_words_dropped_from_labels() -> None:
    clusters, _ = cluster_pool(
        [
            (1, 0, ["catalysis", "$photocatalysis$"]),
            (2, 1, ["catalysis", "$photocatalysis$"]),
            (3, 2, []),
        ],
        ["catalysis"],
    )
    assert [c.label for c in clusters] == ["photocatalysis"]
    assert clean_keyword(r"\rm g, e_") == ""


def test_none_bucket_counts_unkeyworded_papers() -> None:
    _, none = cluster_pool([(1, 0, []), (1, 1, ["solar"]), (2, 2, []), (3, 3, [])], [])
    assert none == (2, 3)
