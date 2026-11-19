"""Voice descriptions remain short even with incomplete bibliography."""

from precis.spoken_names import spoken_name


def test_paper_name() -> None:
    assert (
        spoken_name(
            "paper",
            "Unused summary",
            {
                "author": "Miller, J.",
                "year": 2023,
                "title": "Ammonia synthesis at scale",
            },
        )
        == "Miller 2023 ammonia"
    )
    assert (
        spoken_name(
            "paper",
            "Ammonia synthesis",
            {"authors": [{"name": "Jane Miller"}], "year": 2023},
        )
        == "Miller 2023 ammonia"
    )


def test_summary_names() -> None:
    for kind in ("gripe", "todo", "memory"):
        assert (
            spoken_name(kind, "Endpoint split for jobs ...", {})
            == "endpoint split jobs"
        )
    assert spoken_name("paper", "The ammonia synthesis", {}) == "ammonia"
    assert spoken_name("memory", "", {}) == "memory"
