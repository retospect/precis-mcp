"""Tests for `scripts/mine-sessions/detect.py`, `cards.py`, and `stats.py`.

``scripts/`` is not a package under ``src/`` (see the CLAUDE.md scripts
convention), so these modules are imported via a ``sys.path`` insert —
mirroring ``tests/test_guide_scripts.py``'s ``import guide_lib`` pattern,
adapted for the hyphenated ``scripts/mine-sessions/`` directory (the
directory itself goes on ``sys.path``; the files inside import as plain
top-level modules).

For every detector D0-D11 there is one test that triggers it and one
near-miss that must NOT — a detector that silently never fires is the worst
failure mode of a mining pass (see ``schema.py``'s own docstring on this),
and these negatives are what prove each rule is actually discriminating
rather than vacuously always-on or always-off.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MINE_DIR = _REPO_ROOT / "scripts" / "mine-sessions"
sys.path.insert(0, str(_MINE_DIR))

import cards
import detect
import stats
from redact import scan
from schema import Event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def mk(
    session: str = "s1",
    seq: int = 0,
    tool: str | None = "mcp__precis__precis",
    verb: str | None = None,
    kind: str | None = None,
    arg_keys: tuple[str, ...] = (),
    arg_digest: str = "",
    is_error: bool = False,
    err_type: str | None = None,
    err_head: str = "",
    result_bytes: int = 0,
    result_head: str = "",
    ts: str | None = None,
    usage: dict | None = None,
    corpus: str = "local",
    latency_ms: int | None = None,
) -> Event:
    return Event(
        corpus=corpus,
        session=session,
        seq=seq,
        ts=ts,
        tool=tool,
        verb=verb,
        kind=kind,
        arg_keys=tuple(arg_keys),
        arg_digest=arg_digest,
        is_error=is_error,
        err_type=err_type,
        err_head=err_head,
        result_bytes=result_bytes,
        result_head=result_head,
        latency_ms=latency_ms,
        usage=usage or {},
    )


# ---------------------------------------------------------------------------
# D0 exec_class
# ---------------------------------------------------------------------------


def test_d0_exec_class_hit_blocked() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            result_head="Claude requested permissions to use get",
        ),
    ]
    cands = detect.detect_exec_class(events)
    assert len(cands) == 1
    assert cands[0].signature == "blocked"
    assert cands[0].n == 1


def test_d0_exec_class_hit_zero() -> None:
    events = [mk(seq=1, verb="search", kind="paper", is_error=False, result_bytes=0)]
    cands = detect.detect_exec_class(events)
    sigs = {c.signature for c in cands}
    assert "zero" in sigs


def test_d0_exec_class_near_miss_ok_emits_nothing() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=False,
            result_bytes=500,
            result_head="a paper",
        ),
    ]
    assert detect.detect_exec_class(events) == []


# ---------------------------------------------------------------------------
# D1 hard_error
# ---------------------------------------------------------------------------


def test_d1_hard_error_hit_collapses_normalized_signature() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper with slug 'aaa'",
        ),
        mk(
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper with slug 'bbb'",
        ),
    ]
    cands = detect.detect_hard_error(events)
    assert len(cands) == 1
    assert cands[0].n == 2


def test_d1_hard_error_near_miss_different_err_type_stays_split() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper with slug 'aaa'",
        ),
        mk(
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="BadArgs",
            err_head="missing kind",
        ),
    ]
    cands = detect.detect_hard_error(events)
    assert len(cands) == 2
    assert all(c.n == 1 for c in cands)


# ---------------------------------------------------------------------------
# D2 retry_to_success
# ---------------------------------------------------------------------------


def test_d2_retry_to_success_hit_within_window() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'x'",
        ),
        mk(seq=2, verb="get", kind="paper", is_error=False, result_bytes=100),
    ]
    cands = detect.detect_retry_to_success(events)
    assert len(cands) == 1
    assert cands[0].n == 1
    assert cands[0].metrics["mean_retries"] == 1.0


def test_d2_retry_to_success_near_miss_outside_window() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'x'",
        ),
        mk(seq=6, verb="get", kind="paper", is_error=False, result_bytes=100),
    ]
    assert detect.detect_retry_to_success(events) == []


# ---------------------------------------------------------------------------
# D3 reformulation
# ---------------------------------------------------------------------------


def test_d3_reformulation_hit_three_differing_shapes() -> None:
    events = [
        mk(seq=1, verb="search", kind="paper", arg_keys=("q",)),
        mk(seq=2, verb="search", kind="paper", arg_keys=("q", "kind")),
        mk(seq=3, verb="search", kind="paper", arg_keys=("kind",)),
    ]
    cands = detect.detect_reformulation(events)
    assert len(cands) == 1
    assert cands[0].n == 3


def test_d3_reformulation_near_miss_only_two_consecutive() -> None:
    events = [
        mk(seq=1, verb="search", kind="paper", arg_keys=("q",)),
        mk(seq=2, verb="search", kind="paper", arg_keys=("q", "kind")),
    ]
    assert detect.detect_reformulation(events) == []


# ---------------------------------------------------------------------------
# D4 abandon_detour
# ---------------------------------------------------------------------------


def test_d4_abandon_detour_hit() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
        mk(
            seq=3,
            tool="Bash",
            verb=None,
            arg_digest="scripts/prod-psql \"select * from refs where kind='paper'\"",
        ),
    ]
    cands = detect.detect_abandon_detour(events)
    assert len(cands) == 1
    assert cands[0].signature == "get|paper"


def test_d4_abandon_detour_near_miss_unrelated_bash() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
        mk(seq=3, tool="Bash", verb=None, arg_digest="ls -la"),
    ]
    assert detect.detect_abandon_detour(events) == []


# ---------------------------------------------------------------------------
# D5 detour_census
# ---------------------------------------------------------------------------


def test_d5_detour_census_hit_buckets_by_table() -> None:
    events = [
        mk(
            seq=1,
            tool="Bash",
            verb=None,
            arg_digest="psql -c \"select * from refs where kind='paper'\"",
            result_bytes=200,
        ),
    ]
    cands = detect.detect_detour_census(events)
    assert len(cands) == 1
    assert cands[0].signature == "table:refs"
    assert cands[0].metrics["result_bytes_total"] == 200


def test_d5_detour_census_near_miss_unmatched_bash() -> None:
    events = [mk(seq=1, tool="Bash", verb=None, arg_digest="ls -la")]
    assert detect.detect_detour_census(events) == []


# ---------------------------------------------------------------------------
# D6 render_obesity
# ---------------------------------------------------------------------------


def test_d6_render_obesity_hit_groups_and_narrow_after_fat() -> None:
    events = [
        # Genuine narrowing: the SAME entity (id=5), re-fetched by the same
        # verb with an extra selector. All three conditions matter — see the
        # negative test below for the pair that used to false-positive.
        mk(
            seq=1,
            verb="get",
            kind="paper",
            arg_digest="kind='paper', id=5, view='full'",
            arg_keys=("id", "kind", "view"),
            result_bytes=8000,
        ),
        mk(
            seq=2,
            verb="get",
            kind="paper",
            arg_digest="kind='paper', id=5, view='full', section='abstract'",
            arg_keys=("id", "kind", "section", "view"),
            result_bytes=200,
        ),
    ]
    cands = detect.detect_render_obesity(events)
    sigs = {c.signature for c in cands}
    assert "get|paper|full" in sigs
    assert "narrow-after-fat|paper" in sigs
    fat = next(c for c in cands if c.signature == "get|paper|full")
    assert fat.metrics["max_bytes"] == 8000


def test_d6_render_obesity_near_miss_no_narrowing() -> None:
    events = [
        mk(seq=1, verb="get", kind="paper", arg_keys=("kind", "view", "id")),
        mk(seq=2, verb="get", kind="paper", arg_keys=("kind",)),
    ]
    cands = detect.detect_render_obesity(events)
    assert not any(c.signature.startswith("narrow-after-fat") for c in cands)


def test_d6_get_then_unrelated_search_is_not_narrowing() -> None:
    """The exact false positive that inflated this detector to n=1,830 on
    the first real pass: `get(id=X)` followed by an unrelated `search(...)`
    for a different topic. `search`'s argument shape simply has more keys
    than `get`'s, which the original same-kind/more-keys rule read as the
    caller narrowing a too-fat render. It is the ordinary look-up-then-search
    workflow and must not fire.
    """
    events = [
        mk(
            seq=1,
            verb="get",
            kind="gripe",
            arg_digest="kind='gripe', id=366639",
            arg_keys=("id", "kind"),
            result_bytes=9000,
        ),
        mk(
            seq=2,
            verb="search",
            kind="gripe",
            arg_digest="kind='gripe', q='449720', status='*'",
            arg_keys=("kind", "q", "status"),
            result_bytes=400,
        ),
    ]
    cands = detect.detect_render_obesity(events)
    assert not any(c.signature.startswith("narrow-after-fat") for c in cands)


def test_by_session_drops_payload_free_corpora() -> None:
    """Adjacency rules are meaningless on the ledger: `extract._day_bucket`
    gives the whole fleet's calls for a calendar day one synthetic session
    key, so dozens of unrelated concurrent jobs look like one agent's
    consecutive turns. On the first real pass that reported `get|quest`
    reformulating 26 times inside a 19-second range.
    """
    events = [
        mk(seq=1, corpus="ledger", session="ledger:2026-09-28", verb="get", kind="q"),
        mk(seq=2, corpus="ledger", session="ledger:2026-09-28", verb="get", kind="q"),
        mk(seq=3, corpus="local", session="s1", verb="get", kind="q"),
    ]
    grouped = detect.by_session(events)
    assert "ledger:2026-09-28" not in grouped
    assert [e.seq for e in grouped["s1"]] == [3]


# ---------------------------------------------------------------------------
# D7 read_then_unused
# ---------------------------------------------------------------------------


def test_d7_read_then_unused_hit() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            result_bytes=5000,
            result_head="SPECIALTOKENABC123XYZ appears here",
        ),
        mk(
            seq=2,
            tool=None,
            verb=None,
            result_head="thinking about something else entirely",
        ),
        mk(seq=3, tool=None, verb=None, result_head="still unrelated text"),
    ]
    cands = detect.detect_read_then_unused(events)
    assert len(cands) == 1
    assert cands[0].signature == "get"
    assert cands[0].caveat


def test_d7_read_then_unused_near_miss_token_reused() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="paper",
            result_bytes=5000,
            result_head="SPECIALTOKENABC123XYZ appears here",
        ),
        mk(
            seq=2,
            tool=None,
            verb=None,
            result_head="quoting SPECIALTOKENABC123XYZ back",
        ),
    ]
    assert detect.detect_read_then_unused(events) == []


# ---------------------------------------------------------------------------
# D8 skill_taught_wrong
# ---------------------------------------------------------------------------


def test_d8_skill_taught_wrong_hit() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest="id='precis-overview'",
            arg_keys=("kind", "id"),
        ),
        mk(
            seq=3,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
    ]
    cands = detect.detect_skill_taught_wrong(events)
    assert len(cands) == 1
    assert cands[0].signature == "skill:precis-overview"


def test_d8_skill_taught_wrong_near_miss_outside_window() -> None:
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest="id='precis-overview'",
            arg_keys=("kind", "id"),
        ),
        mk(
            seq=9,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
    ]
    assert detect.detect_skill_taught_wrong(events) == []


# ---------------------------------------------------------------------------
# D9 vocab_near_miss
# ---------------------------------------------------------------------------


def test_d9_vocab_near_miss_hit_on_kind_typo() -> None:
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(
            seq=2,
            verb="get",
            kind="paperx",
            is_error=True,
            err_type="BadKind",
            err_head="unknown kind",
        ),
    ]
    cands = detect.detect_vocab_near_miss(events)
    assert any(c.signature == "kind:paperx~paper" for c in cands)


def test_d9_vocab_near_miss_near_miss_too_far() -> None:
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(
            seq=2,
            verb="get",
            kind="zzzzzzzzzz",
            is_error=True,
            err_type="BadKind",
            err_head="unknown kind",
        ),
    ]
    assert detect.detect_vocab_near_miss(events) == []


# ---------------------------------------------------------------------------
# D10 correction_roundtrip
# ---------------------------------------------------------------------------


def test_d10_correction_roundtrip_hit() -> None:
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(
            seq=3,
            tool="__user__",
            verb=None,
            result_head="No, that's wrong, I meant the other one",
        ),
    ]
    cands = detect.detect_correction_roundtrip(events)
    assert len(cands) == 1
    assert cands[0].signature == "get|paper"


def test_d10_correction_roundtrip_near_miss_no_lexicon_match() -> None:
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(seq=3, tool="__user__", verb=None, result_head="thanks, that looks great"),
    ]
    assert detect.detect_correction_roundtrip(events) == []


# ---------------------------------------------------------------------------
# D11 spin
# ---------------------------------------------------------------------------


def test_d11_spin_hit_three_distinct_sessions() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'a'",
            ts="2026-09-01T00:00:00Z",
        ),
        mk(
            session="s2",
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'b'",
            ts="2026-09-01T01:00:00Z",
        ),
        mk(
            session="s3",
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'c'",
            ts="2026-09-01T02:00:00Z",
        ),
    ]
    cands = detect.detect_spin(events)
    assert len(cands) == 1
    assert cands[0].n == 3


def test_d11_spin_near_miss_same_session_same_day() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'a'",
            ts="2026-09-01T00:00:00Z",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'b'",
            ts="2026-09-01T01:00:00Z",
        ),
        mk(
            session="s1",
            seq=3,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'c'",
            ts="2026-09-01T02:00:00Z",
        ),
    ]
    assert detect.detect_spin(events) == []


# ---------------------------------------------------------------------------
# cards.py
# ---------------------------------------------------------------------------


def test_cards_scrubs_tailnet_address_and_stays_under_cap(tmp_path: Path) -> None:
    # Assembled from parts, not written out: a literal CGNAT address anywhere
    # in the tree trips tests/test_deploy_tree_no_secrets.py, and its
    # exemption marker is budget-capped on purpose. Parameterising is what
    # that test tells you to do instead.
    tailnet_ip = "100." + "101." + "2.3"
    events = [
        mk(
            session="sess-secret",
            seq=1,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="ConnError",
            err_head=f"could not reach worker at {tailnet_ip}",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(e.to_json() for e in events), encoding="utf-8")

    candidate = {
        "detector": "hard_error",
        "signature": f"get|paper|ConnError|could not reach worker at {tailnet_ip}",
        "n": 1,
        "n_sessions": 1,
        "first_ts": None,
        "last_ts": None,
        "evidence": [{"session": "sess-secret", "seq": 1}],
        "metrics": {},
        "caveat": "",
    }
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([candidate]), encoding="utf-8")

    out_dir = tmp_path / "cards"
    cards.main(
        [
            "--events",
            str(events_path),
            "--candidates",
            str(candidates_path),
            "--out",
            str(out_dir),
            "--top",
            "5",
            "--random",
            "0",
        ]
    )

    card_files = list((out_dir / "hard_error").glob("*.md"))
    assert len(card_files) == 1
    text = card_files[0].read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= cards.CARD_BYTE_CAP
    assert scan(text) == []
    assert (out_dir / "INDEX.md").exists()


def test_cards_random_seed_is_reproducible(tmp_path: Path) -> None:
    events = [
        mk(
            session=f"sess{i}",
            seq=j,
            verb="get",
            kind="paper",
            result_bytes=10,
            result_head=f"row {j}",
        )
        for i in range(4)
        for j in range(5)
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(e.to_json() for e in events), encoding="utf-8")
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text("[]", encoding="utf-8")

    out_a = tmp_path / "cards_a"
    out_b = tmp_path / "cards_b"
    for out_dir in (out_a, out_b):
        cards.main(
            [
                "--events",
                str(events_path),
                "--candidates",
                str(candidates_path),
                "--out",
                str(out_dir),
                "--top",
                "5",
                "--random",
                "3",
                "--seed",
                "42",
            ]
        )

    names_a = sorted(p.name for p in (out_a / "random").glob("*.md"))
    names_b = sorted(p.name for p in (out_b / "random").glob("*.md"))
    assert names_a == names_b
    for name in names_a:
        assert (out_a / "random" / name).read_text(encoding="utf-8") == (
            out_b / "random" / name
        ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# stats.py
# ---------------------------------------------------------------------------


def test_stats_produces_valid_json_with_documented_keys() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            verb="get",
            kind="paper",
            is_error=False,
            result_bytes=100,
            ts="2026-09-01T00:00:00Z",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper 'a'",
            ts="2026-09-01T00:01:00Z",
        ),
        mk(
            session="s1",
            seq=3,
            tool=None,
            verb=None,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 50,
            },
            ts="2026-09-01T00:02:00Z",
        ),
        mk(
            session="s1",
            seq=4,
            tool="Bash",
            verb=None,
            arg_digest="psql -c 'select 1'",
            result_bytes=30,
        ),
    ]
    sb = stats.build_scoreboard(events)
    dumped = json.dumps(sb)
    reparsed = json.loads(dumped)

    assert reparsed["schema_version"] == stats.SCHEMA_VERSION
    for key in (
        "corpus_census",
        "per_verb",
        "per_kind_top20",
        "tokens",
        "detour_census",
        "exec_class_mix",
    ):
        assert key in reparsed
    assert reparsed["corpus_census"]["total_events"] == len(events)
    assert reparsed["tokens"]["input_total"] == 100
    assert reparsed["detour_census"]["count"] == 1
    assert "get" in reparsed["per_verb"]


def test_stats_cli_writes_both_outputs(tmp_path: Path) -> None:
    events = [mk(session="s1", seq=1, verb="get", kind="paper", result_bytes=10)]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(e.to_json() for e in events), encoding="utf-8")
    out_md = tmp_path / "scoreboard.md"
    out_json = tmp_path / "scoreboard.json"

    stats.main(
        [
            "--events",
            str(events_path),
            "--out-md",
            str(out_md),
            "--out-json",
            str(out_json),
        ]
    )

    assert out_md.exists()
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == stats.SCHEMA_VERSION
