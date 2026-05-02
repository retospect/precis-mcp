"""RandomHandler — dice / int / choice / neighbor / chunk.

Dice, int, choice are stateless and tested against the default
stateless hub. Neighbor and chunk need a wired store + embedded
blocks, so their tests use the ``hub`` fixture (which carries a
fresh store + MockEmbedder at the right dim).
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.random import RandomHandler


@pytest.fixture
def handler() -> RandomHandler:
    """Stateless handler — dice / int / choice paths."""
    return RandomHandler(hub=Hub())


# ---------------------------------------------------------------------------
# Basic handler contract
# ---------------------------------------------------------------------------


def test_kindspec_declares_only_get() -> None:
    spec = RandomHandler.spec
    assert spec.kind == "random"
    assert spec.supports_get is True
    assert spec.supports_search is False
    assert spec.supports_put is False
    assert spec.supports_edit is False
    assert spec.supports_delete is False
    assert spec.supports_tag is False
    assert spec.supports_link is False


def test_other_verbs_unsupported(handler: RandomHandler) -> None:
    with pytest.raises(Unsupported):
        handler.search(q="x")
    with pytest.raises(Unsupported):
        handler.put(text="x")
    with pytest.raises(Unsupported):
        handler.edit(id=1)
    with pytest.raises(Unsupported):
        handler.delete(id=1)
    with pytest.raises(Unsupported):
        handler.tag(id=1, add=["x"])
    with pytest.raises(Unsupported):
        handler.link(id=1, target="x:y")


def test_missing_expr_raises(handler: RandomHandler) -> None:
    with pytest.raises(BadInput, match="requires an expression"):
        handler.get()


def test_unknown_form_raises_with_full_grammar(handler: RandomHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(id="roll a d20 please")
    # Recovery hint must list every supported form so the caller
    # can pick the right one without reading the source.
    assert exc.value.next is not None
    for form in ("2d6+3", "int(1..100)", "choice(", "neighbor(", "chunk("):
        assert form in exc.value.next


def test_uses_q_when_id_absent(handler: RandomHandler) -> None:
    r = handler.get(q="d6")
    # d6 → [1, 6]
    m = re.search(r"d6 = (\d+)", r.body)
    assert m is not None
    assert 1 <= int(m.group(1)) <= 6


# ---------------------------------------------------------------------------
# Dice
# ---------------------------------------------------------------------------


class TestDice:
    """``NdM[±K]`` rolling."""

    def test_d20_single_die(self, handler: RandomHandler) -> None:
        """Bare ``dM`` defaults to N=1."""
        r = handler.get(id="d20")
        m = re.search(r"d20 = (\d+)", r.body)
        assert m is not None
        value = int(m.group(1))
        assert 1 <= value <= 20

    def test_3d6_bounds(self, handler: RandomHandler) -> None:
        """3d6 must be in [3, 18]."""
        for _ in range(20):
            r = handler.get(id="3d6")
            m = re.search(r"3d6 = (\d+)", r.body)
            assert m is not None
            total = int(m.group(1))
            assert 3 <= total <= 18, f"out of bounds: {total}"

    def test_3d6_plus_modifier(self, handler: RandomHandler) -> None:
        """``3d6+3`` must be in [6, 21] and echo the modifier line."""
        for _ in range(20):
            r = handler.get(id="3d6+3")
            m = re.search(r"3d6\+3 = (\d+)", r.body)
            assert m is not None
            assert 6 <= int(m.group(1)) <= 21
        # Modifier is echoed for transparency.
        assert "modifier: +3" in r.body
        assert "rolls:" in r.body

    def test_dice_negative_modifier(self, handler: RandomHandler) -> None:
        """``2d6-1`` must be in [1, 11] (not clamped to 0)."""
        for _ in range(20):
            r = handler.get(id="2d6-1")
            m = re.search(r"2d6-1 = (-?\d+)", r.body)
            assert m is not None
            assert 1 <= int(m.group(1)) <= 11
        assert "modifier: -1" in r.body

    def test_dice_single_die_no_rolls_list(self, handler: RandomHandler) -> None:
        """``d20`` (N=1, K=0) elides the rolls: trailer — one die
        and the total are the same number, no value in echoing it."""
        r = handler.get(id="d20")
        assert "rolls:" not in r.body
        assert "modifier:" not in r.body

    def test_dice_rolls_count_matches_n(self, handler: RandomHandler) -> None:
        """Every individual face value is echoed for N>1 so a GM
        can reroll specific dice if needed."""
        r = handler.get(id="5d6")
        m = re.search(r"rolls: ([^;)\n]+)", r.body)
        assert m is not None
        rolls = [int(x.strip()) for x in m.group(1).split(",")]
        assert len(rolls) == 5
        assert all(1 <= r <= 6 for r in rolls)

    def test_dice_zero_count_rejected(self, handler: RandomHandler) -> None:
        """``0d6`` is nonsense — rejected with a hint toward d6 /
        3d6."""
        with pytest.raises(BadInput, match="dice count must be >= 1"):
            handler.get(id="0d6")

    def test_dice_one_sided_rejected(self, handler: RandomHandler) -> None:
        """``d1`` is always 1 — useless and likely a typo."""
        with pytest.raises(BadInput, match="at least 2 sides"):
            handler.get(id="d1")

    def test_dice_count_cap_enforced(self, handler: RandomHandler) -> None:
        """DoS protection: ``10000d6`` exceeds the cap."""
        with pytest.raises(BadInput, match="exceeds cap"):
            handler.get(id="10000d6")

    def test_dice_sides_cap_enforced(self, handler: RandomHandler) -> None:
        """``d9999999`` exceeds the sides cap; hint suggests
        ``int(1..N)`` for the single-draw equivalent."""
        with pytest.raises(BadInput, match="exceeds cap") as exc:
            handler.get(id="d9999999")
        assert "int(1.." in (exc.value.next or "")


# ---------------------------------------------------------------------------
# int() range
# ---------------------------------------------------------------------------


class TestInt:
    """Uniform integer in ``[LO, HI]`` inclusive."""

    def test_bounds_inclusive(self, handler: RandomHandler) -> None:
        """``int(1..3)`` must hit all of {1, 2, 3} over enough
        draws. Use a high draw count so a random CI hiccup doesn't
        flake (probability of missing any single value over 200
        draws ≈ (2/3)^200 ≈ 1e-35)."""
        seen = set()
        for _ in range(200):
            r = handler.get(id="int(1..3)")
            m = re.search(r"int\(1\.\.3\) = (-?\d+)", r.body)
            assert m is not None
            seen.add(int(m.group(1)))
        assert seen == {1, 2, 3}

    def test_negative_range(self, handler: RandomHandler) -> None:
        """Negative bounds round-trip correctly."""
        for _ in range(20):
            r = handler.get(id="int(-5..5)")
            m = re.search(r"int\(-5\.\.5\) = (-?\d+)", r.body)
            assert m is not None
            assert -5 <= int(m.group(1)) <= 5

    def test_single_value_range(self, handler: RandomHandler) -> None:
        """``int(7..7)`` is a constant — exactly 7 every time."""
        for _ in range(5):
            r = handler.get(id="int(7..7)")
            assert "int(7..7) = 7" in r.body

    def test_whitespace_tolerated(self, handler: RandomHandler) -> None:
        """``int( 1 .. 10 )`` is legal — whitespace around the
        operators is fine for the bracketed forms."""
        r = handler.get(id="int( 1 .. 10 )")
        m = re.search(r"= (-?\d+)", r.body)
        assert m is not None
        assert 1 <= int(m.group(1)) <= 10

    def test_reversed_range_rejected(self, handler: RandomHandler) -> None:
        """``int(10..1)`` is empty — the hint suggests swapping."""
        with pytest.raises(BadInput, match="range is empty") as exc:
            handler.get(id="int(10..1)")
        assert "int(1..10)" in (exc.value.next or "")


# ---------------------------------------------------------------------------
# choice()
# ---------------------------------------------------------------------------


class TestChoice:
    """``choice(A|B|C)`` uniform pick."""

    def test_picks_one_of_options(self, handler: RandomHandler) -> None:
        """Every draw must return one of the offered options."""
        for _ in range(20):
            r = handler.get(id="choice(heads|tails)")
            m = re.search(r"= (\S+)", r.body)
            assert m is not None
            assert m.group(1) in {"heads", "tails"}

    def test_whitespace_trimmed(self, handler: RandomHandler) -> None:
        """Options have leading/trailing whitespace stripped."""
        seen = set()
        for _ in range(50):
            r = handler.get(id="choice( red | green | blue )")
            m = re.search(r"=\s+(\S+)", r.body)
            assert m is not None
            seen.add(m.group(1))
        assert seen == {"red", "green", "blue"}

    def test_single_option(self, handler: RandomHandler) -> None:
        """One option is a degenerate but legal choice — returns
        it every time. (No separator count hint for N=1.)"""
        r = handler.get(id="choice(only)")
        assert "only" in r.body
        assert "picked from" not in r.body

    def test_empty_options_rejected(self, handler: RandomHandler) -> None:
        """``choice( | )`` (all empty) has no options — BadInput."""
        with pytest.raises(BadInput, match="no options"):
            handler.get(id="choice( | )")

    def test_distribution_roughly_uniform(self, handler: RandomHandler) -> None:
        """Over 300 draws of ``choice(a|b|c)``, each option should
        appear — the CSPRNG isn't biased toward any. Loose test:
        each > 50 draws. P(any < 50 | uniform) ≈ 1e-8."""
        counts: dict[str, int] = {"a": 0, "b": 0, "c": 0}
        for _ in range(300):
            r = handler.get(id="choice(a|b|c)")
            m = re.search(r"= (\S+)", r.body)
            assert m is not None
            counts[m.group(1)] += 1
        for k, v in counts.items():
            assert v > 50, f"{k}: got {v} < 50; distribution suspect"


# ---------------------------------------------------------------------------
# Next: trailer
# ---------------------------------------------------------------------------


def test_dice_response_has_roll_again_hint(handler: RandomHandler) -> None:
    """Every successful dice roll includes a ``Next:`` section
    pointing the agent at "roll again" — reinforces that this is
    a non-deterministic kind."""
    r = handler.get(id="2d6")
    assert "Next:" in r.body
    assert "roll again" in r.body


def test_int_response_has_draw_again_hint(handler: RandomHandler) -> None:
    r = handler.get(id="int(1..6)")
    assert "draw again" in r.body


def test_choice_response_has_pick_again_hint(handler: RandomHandler) -> None:
    r = handler.get(id="choice(a|b)")
    assert "pick again" in r.body


# ---------------------------------------------------------------------------
# Stateless deployments reject neighbor / chunk cleanly
# ---------------------------------------------------------------------------


def test_neighbor_without_store_raises_badinput(handler: RandomHandler) -> None:
    """``neighbor(...)`` in a stateless deployment raises
    BadInput (not NotFound) — the call shape is fine, the
    infrastructure isn't wired."""
    with pytest.raises(BadInput, match="requires a wired store"):
        handler.get(id="neighbor(paper:foo~0)")


def test_chunk_without_store_raises_badinput(handler: RandomHandler) -> None:
    with pytest.raises(BadInput, match="requires a wired store"):
        handler.get(id="chunk(paper:foo)")


# ---------------------------------------------------------------------------
# neighbor / chunk — require wired store + embedded blocks
# ---------------------------------------------------------------------------
#
# These tests ingest a tiny oracle with three blocks so we can
# verify the store-backed paths without pulling the paper ingest
# pipeline in. MockEmbedder is deterministic per text so the
# nearest-neighbour order is reproducible in CI.


@pytest.fixture
def seeded_handler(hub: Hub) -> tuple[RandomHandler, int]:
    """Ingest a 3-block oracle and return the handler + the ref_id.

    Uses the ``oracle`` kind because its ingest path is the
    simplest slug-kind-with-blocks we have; any block-carrying
    kind would work.
    """
    store = hub.store
    embedder = hub.embedder
    assert store is not None
    assert embedder is not None

    # Create a corpus + ref inline — skipping the full ingest
    # pipeline keeps the fixture tight.
    from precis.store.types import BlockInsert

    cid = store.ensure_corpus("default")
    ref = store.insert_ref(
        corpus_id=cid,
        kind="oracle",
        slug="test-tradition",
        title="Test Tradition",
    )
    texts = [
        "the mountain teaches stillness",
        "the river teaches flow",
        "the forest teaches patience",
    ]
    embeddings = embedder.embed(texts)
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(
                pos=i,
                slug=None,
                text=text,
                token_count=len(text.split()),
                embedding=embedding,
                density="sparse",
                meta={},
            )
            for i, (text, embedding) in enumerate(zip(texts, embeddings))
        ],
    )
    return RandomHandler(hub=hub), ref.id


class TestNeighbor:
    """``neighbor(kind:id~pos)`` vector-nearest blocks."""

    def test_neighbor_excludes_source_block(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """The source block itself has distance 0 — it must not
        appear in the numbered results or the response is
        useless. (The query expression echoing ``~0`` in the
        header and the Next: trailer is fine — we only care
        about the numbered result list.)"""
        handler, _ = seeded_handler
        r = handler.get(id="neighbor(oracle:test-tradition~0)")
        # Slice to just the numbered results section: between the
        # "nearest block(s)" header line and the "Next:" trailer.
        after_header = r.body.split("nearest block", 1)[1]
        results_only = after_header.split("Next:", 1)[0]
        # Source block (pos=0) must not be in the numbered list.
        assert "oracle:test-tradition~0" not in results_only
        # Other blocks must be listed.
        assert "oracle:test-tradition~1" in results_only
        assert "oracle:test-tradition~2" in results_only

    def test_neighbor_returns_distance_labels(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """Each neighbour row carries a distance value so the
        agent can see similarity, not just rank."""
        handler, _ = seeded_handler
        r = handler.get(id="neighbor(oracle:test-tradition~1)")
        assert re.search(r"distance \d+\.\d{3}", r.body)

    def test_neighbor_honours_top_k(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """``top_k=1`` caps the result list at one neighbour."""
        handler, _ = seeded_handler
        r = handler.get(id="neighbor(oracle:test-tradition~0)", top_k=1)
        # The header reports "1 nearest" and the body has exactly
        # one numbered entry.
        assert "1 nearest" in r.body
        assert re.search(r"^1\. ", r.body, re.MULTILINE)
        assert not re.search(r"^2\. ", r.body, re.MULTILINE)

    def test_neighbor_rejects_ref_level_target(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """``neighbor(paper:slug)`` (no selector) is BadInput —
        refs have no embedding, only their blocks do."""
        handler, _ = seeded_handler
        with pytest.raises(BadInput, match="requires a block selector"):
            handler.get(id="neighbor(oracle:test-tradition)")

    def test_neighbor_rejects_invalid_top_k(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """Out-of-range ``top_k`` is BadInput."""
        handler, _ = seeded_handler
        with pytest.raises(BadInput, match="top_k must be in"):
            handler.get(id="neighbor(oracle:test-tradition~0)", top_k=0)
        with pytest.raises(BadInput, match="top_k must be in"):
            handler.get(id="neighbor(oracle:test-tradition~0)", top_k=999)

    def test_neighbor_unknown_ref_raises_notfound(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """The link-target parser raises NotFound for missing
        refs; neighbor() bubbles that up unchanged."""
        handler, _ = seeded_handler
        with pytest.raises(NotFound, match="no live oracle ref"):
            handler.get(id="neighbor(oracle:does-not-exist~0)")


class TestChunk:
    """``chunk(kind:id)`` random block pick."""

    def test_chunk_returns_one_of_the_blocks(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """Every chunk draw returns the text of some ref block."""
        handler, _ = seeded_handler
        valid_texts = {
            "the mountain teaches stillness",
            "the river teaches flow",
            "the forest teaches patience",
        }
        for _ in range(10):
            r = handler.get(id="chunk(oracle:test-tradition)")
            # One of the three texts must appear in the body.
            assert any(t in r.body for t in valid_texts)

    def test_chunk_body_reports_ref_handle(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """The handle ``oracle:test-tradition~N`` must appear so
        the agent can fetch THAT specific block deterministically."""
        handler, _ = seeded_handler
        r = handler.get(id="chunk(oracle:test-tradition)")
        assert re.search(r"oracle:test-tradition~[012]", r.body)

    def test_chunk_block_level_target_rejected(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """``chunk(oracle:slug~0)`` is nonsense — chunk picks from
        a ref, not a single block."""
        handler, _ = seeded_handler
        with pytest.raises(BadInput, match="points at a single block"):
            handler.get(id="chunk(oracle:test-tradition~0)")

    def test_chunk_distribution_covers_all_blocks(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        """Over many draws, every block must be picked at least
        once — confirms we're not hardcoded to any single pos."""
        handler, _ = seeded_handler
        seen_positions: set[str] = set()
        for _ in range(60):
            r = handler.get(id="chunk(oracle:test-tradition)")
            m = re.search(r"oracle:test-tradition~(\d+)", r.body)
            if m:
                seen_positions.add(m.group(1))
        assert seen_positions == {"0", "1", "2"}, (
            f"missing positions; saw {seen_positions}"
        )

    def test_chunk_unknown_ref_raises_notfound(
        self, seeded_handler: tuple[RandomHandler, int]
    ) -> None:
        handler, _ = seeded_handler
        with pytest.raises(NotFound, match="no live oracle ref"):
            handler.get(id="chunk(oracle:does-not-exist)")
