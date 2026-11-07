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


def test_d4_abandon_detour_quoted_mention_not_a_call() -> None:
    """td580721: ``echo 'example; prod-psql'`` mentions the tool but runs
    nothing — the same command-position rule detour_census applies."""
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
            arg_digest='{"command": "echo \'example; prod-psql\'"}',
        ),
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


def test_d5_detour_census_mention_not_call_excluded() -> None:
    """gr570090(c): a command line that merely *contains* ``prod-psql`` is
    a mention, not a call — ``cat``/``grep``/``git log --`` on the script
    was most of the ``other`` bucket's 792 noise hits. Only command
    position counts."""
    digest = '{"command": "%s"}'
    mentions = [
        "cat scripts/prod-psql",
        "grep -n prod-psql scripts/prod-psql",
        "git log --oneline -- scripts/prod-psql",
        "sed -n '1,40p' scripts/prod-psql",
        "echo try prod-psql next",
    ]
    for cmd in mentions:
        events = [mk(seq=1, tool="Bash", verb=None, arg_digest=digest % cmd)]
        assert detect.detect_detour_census(events) == [], cmd


def test_d5_detour_census_wrappers_still_count() -> None:
    for cmd in (
        "sudo prod-psql --ro 'select 1'",
        "bash scripts/prod-psql --ro 'select 1'",
        "FOO=1 psql -c 'select 1'",
        "true && prod-psql --ro 'select 1'",
    ):
        events = [
            mk(
                seq=1,
                tool="Bash",
                verb=None,
                arg_digest='{{"command": "{}"}}'.format(cmd.replace("'", "\\u0027")),
            )
        ]
        cands = detect.detect_detour_census(events)
        assert len(cands) == 1, cmd


def test_d5_detour_census_sql_file_buckets() -> None:
    """gr570090(c): ``prod-psql < q.sql`` hid its table — bucket by the
    file name, which still says what was queried."""
    digest = '{"command": "%s"}'
    cases = {
        "prod-psql < scratch/q.sql": "sql-file:q.sql",
        "prod-psql --ro -f /tmp/dump.sql": "sql-file:dump.sql",
        "psql -f queries/report.sql": "sql-file:report.sql",
    }
    for cmd, want in cases.items():
        events = [
            mk(
                seq=1,
                tool="Bash",
                verb=None,
                arg_digest=digest % cmd,
            )
        ]
        cands = detect.detect_detour_census(events)
        assert [c.signature for c in cands] == [want], cmd


def test_d5_detour_census_curl_external_not_counted() -> None:
    """gr570090(c): an external-API curl is ordinary web use, not a detour
    around the precis surface."""
    for cmd in (
        "curl -s https://api.github.com/rate_limit",
        "curl https://pypi.org/simple/requests/ | head",
    ):
        events = [
            mk(
                seq=1,
                tool="Bash",
                verb=None,
                arg_digest=f'{{"command": "{cmd}"}}',
            )
        ]
        assert detect.detect_detour_census(events) == [], cmd


def test_d5_detour_census_curl_precis_surface_counted() -> None:
    for cmd, want in (
        ("curl -s http://localhost:8000/mcp -d '{}'", "endpoint:/mcp"),
        ("curl -s https://precis.example.com/mcp", "endpoint:/mcp"),
    ):
        events = [
            mk(
                seq=1,
                tool="Bash",
                verb=None,
                arg_digest=f'{{"command": "{cmd}"}}',
            )
        ]
        cands = detect.detect_detour_census(events)
        assert [c.signature for c in cands] == [want], cmd


def test_d5_detour_census_dedupes_same_call_two_sessions() -> None:
    """gr570090(c): identical calls at identical seqs in two sessions are
    the same observed event extracted under two file paths — count once."""
    digest = '{"command": "psql -c \\"select * from refs\\""}'
    events = [
        mk(
            session="s1",
            seq=4,
            ts="2026-11-05T10:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
        ),
        mk(
            session="s1/dup",
            seq=4,
            ts="2026-11-05T10:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
        ),
        # Different ts: a genuinely repeated call, counted.
        mk(
            session="s2",
            seq=4,
            ts="2026-11-05T11:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
        ),
    ]
    cands = detect.detect_detour_census(events)
    assert len(cands) == 1
    assert cands[0].n == 2


def test_d5_detour_census_quoted_separator_is_not_a_command() -> None:
    """td580721: ``;`` inside quotes is literal text, not a command
    boundary — ``echo 'example; prod-psql'`` printed a psql call that
    never ran."""
    digest = '{"command": "%s"}'
    for cmd in (
        "echo 'example; prod-psql'",
        'echo "a; psql -c select"',
        "echo 'see `prod-psql`'",
    ):
        events = [mk(seq=1, tool="Bash", verb=None, arg_digest=digest % cmd)]
        assert detect.detect_detour_census(events) == [], cmd


def test_d5_detour_census_unquoted_separator_still_splits() -> None:
    """A real ``;``/``&&`` outside quotes still starts a new segment."""
    for cmd in (
        "echo hi; prod-psql 'select 1'",
        "true && prod-psql --ro 'select 1'",
    ):
        events = [
            mk(
                seq=1,
                tool="Bash",
                verb=None,
                arg_digest='{{"command": "{}"}}'.format(cmd.replace("'", "\\u0027")),
            )
        ]
        cands = detect.detect_detour_census(events)
        assert len(cands) == 1, cmd


def test_d5_detour_census_bash_c_descends_into_script() -> None:
    """td580721: ``bash -c '<script>'`` runs the quoted text, so a
    separator inside that string DOES delimit commands — the quoted-text
    rule must not hide a real detour."""
    events = [
        mk(
            seq=1,
            tool="Bash",
            verb=None,
            arg_digest=json.dumps(
                {"command": "bash -c 'echo a; psql -c \"select * from refs\"'"}
            ),
        ),
    ]
    cands = detect.detect_detour_census(events)
    assert [c.signature for c in cands] == ["table:refs"]

    events = [
        mk(
            seq=1,
            tool="Bash",
            verb=None,
            arg_digest=json.dumps({"command": "bash -c 'echo \"a;b\"'"}),
        ),
    ]
    assert detect.detect_detour_census(events) == []


def test_d5_detour_census_curl_flag_value_not_destination() -> None:
    """td580721: only URL arguments decide a curl detour — an output
    path or header that merely *says* "precis" is not the destination."""
    digest = '{"command": "%s"}'
    for cmd in (
        "curl https://api.github.com -o /tmp/precis.json",
        "curl -d @precis-payload.json https://api.github.com",
        "curl -H 'x-marker: precis' https://api.github.com",
    ):
        events = [mk(seq=1, tool="Bash", verb=None, arg_digest=digest % cmd)]
        assert detect.detect_detour_census(events) == [], cmd


def test_d5_detour_census_curl_bare_host_counts() -> None:
    """A schemeless curl to a precis-surface host still counts."""
    events = [
        mk(
            seq=1,
            tool="Bash",
            verb=None,
            arg_digest='{"command": "curl localhost:8000/mcp"}',
        ),
    ]
    cands = detect.detect_detour_census(events)
    assert [c.signature for c in cands] == ["endpoint:/mcp"]


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


def test_d8_skill_taught_wrong_recovers_id_from_json_digest() -> None:
    """The local extractor renders ``arg_digest`` as JSON — ``{"id":
    "precis-memory-help", "kind": "skill"}`` — which the old ``id=``-only
    regex could not read, so every real signature collapsed to
    ``skill:?``. ``_arg_value`` now reads both dialects."""
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-memory-help", "kind": "skill"}',
            arg_keys=("id", "kind"),
        ),
        mk(
            seq=2,
            verb="put",
            kind="memory",
            is_error=True,
            err_type="BadInput",
            err_head="kind 'memory' is numeric",
        ),
    ]
    cands = detect.detect_skill_taught_wrong(events)
    assert len(cands) == 1
    assert cands[0].signature == "skill:precis-memory-help"


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


def _patch_skill_kinds(monkeypatch, kinds_map: dict) -> None:
    monkeypatch.setattr(detect, "_load_skill_kinds", lambda *a, **k: kinds_map)


def test_d8_skill_taught_wrong_excludes_kind_outside_skill_kinds(
    monkeypatch,
) -> None:
    """gr570090(e): a ``kinds: [agentlog]`` skill cannot have taught a
    ``kind='paper'`` error — blaming it was three of the first five cards'
    worth of false positives."""
    _patch_skill_kinds(monkeypatch, {"precis-agentlog-help": frozenset({"agentlog"})})
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-agentlog-help", "kind": "skill"}',
            arg_keys=("id", "kind"),
        ),
        mk(
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
    ]
    assert detect.detect_skill_taught_wrong(events) == []


def test_d8_skill_taught_wrong_keeps_kind_inside_skill_kinds(monkeypatch) -> None:
    _patch_skill_kinds(monkeypatch, {"precis-agentlog-help": frozenset({"agentlog"})})
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-agentlog-help", "kind": "skill"}',
            arg_keys=("id", "kind"),
        ),
        mk(
            seq=2,
            verb="get",
            kind="agentlog",
            is_error=True,
            err_type="BadInput",
            err_head="bad view",
        ),
    ]
    cands = detect.detect_skill_taught_wrong(events)
    assert len(cands) == 1
    assert cands[0].signature == "skill:precis-agentlog-help"


def test_d8_skill_taught_wrong_unknown_skill_keeps_old_rule(monkeypatch) -> None:
    """A skill absent from the map proves nothing — the old any-error
    window still applies."""
    _patch_skill_kinds(monkeypatch, {})
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "no-such-skill", "kind": "skill"}',
            arg_keys=("id", "kind"),
        ),
        mk(
            seq=2,
            verb="get",
            kind="paper",
            is_error=True,
            err_type="NotFound",
            err_head="no paper",
        ),
    ]
    assert len(detect.detect_skill_taught_wrong(events)) == 1


def test_d8_skill_taught_wrong_excludes_overcap_skill_read(
    monkeypatch,
) -> None:
    """gr570090(e): an over-cap failure on a skill READ is a budget
    problem, not the previous skill's teaching."""
    _patch_skill_kinds(monkeypatch, {"precis-x": frozenset({"skill"})})
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-x", "kind": "skill"}',
            arg_keys=("id", "kind"),
        ),
        mk(
            seq=2,
            verb="get",
            kind="skill",
            is_error=True,
            err_type="ResultBudgetExceeded",
            err_head="result exceeds cap",
        ),
    ]
    assert detect.detect_skill_taught_wrong(events) == []


def test_d8_skill_taught_wrong_errored_read_seeds_nothing(monkeypatch) -> None:
    """An errored ``get|skill`` delivered no teaching — it cannot be the
    cause of a following failure either."""
    _patch_skill_kinds(monkeypatch, {})
    events = [
        mk(
            seq=1,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-x", "kind": "skill"}',
            arg_keys=("id", "kind"),
            is_error=True,
            err_type="NotFound",
            err_head="no skill",
        ),
        mk(
            seq=2,
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
    """td580721: the detector hands the session's WHOLE human-turn stream
    to the pass's model read — no keyword filter decides which turns
    count, because the retired lexicon saw n=2 where a hand read found
    ~65. A turn that matches no correction pattern is still material."""
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(
            seq=3,
            tool="__user__",
            verb=None,
            result_head="No, that's wrong, I meant the other one",
        ),
        # Far from any tool call, no lexicon-shaped text — still in the
        # read stream because a model judges it, not a regex.
        mk(
            seq=40,
            tool="__user__",
            verb=None,
            result_head="could you also check the sibling doc",
        ),
    ]
    cands = detect.detect_correction_roundtrip(events)
    assert len(cands) == 1
    c = cands[0]
    assert c.signature == "session:s1"
    assert c.n == 2  # all human turns, not just roundtrips
    assert c.metrics["roundtrip_turns"] == 1
    turns = c.metrics["human_turns"]
    assert "3: No, that's wrong, I meant the other one" in turns
    assert "40: could you also check the sibling doc" in turns
    # The roundtrip turn leads evidence so the card shows it beside the
    # tool calls it may correct.
    assert c.evidence[0].seq == 3
    assert "MODEL READ" in c.caveat


def test_d10_correction_roundtrip_near_miss_no_tool_call() -> None:
    """A human turn that follows nothing the agent did is not a
    roundtrip — the session earns no candidate."""
    events = [
        mk(seq=10, tool="__user__", verb=None, result_head="thanks, that looks great"),
        mk(seq=11, tool=None, verb=None, result_head="assistant text"),
    ]
    assert detect.detect_correction_roundtrip(events) == []


def test_d10_correction_roundtrip_turn_outside_window_not_evidence() -> None:
    """A session qualifies on one roundtrip; a user turn >3 seq from any
    tool call is still read material but not evidence."""
    events = [
        mk(seq=1, verb="get", kind="paper", is_error=False, result_bytes=10),
        mk(seq=2, tool="__user__", verb=None, result_head="wrong doc"),
        mk(seq=30, tool="__user__", verb=None, result_head="new topic"),
    ]
    cands = detect.detect_correction_roundtrip(events)
    assert len(cands) == 1
    assert cands[0].n == 2
    assert {e.seq for e in cands[0].evidence} == {2}


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
# D12 call_chain
# ---------------------------------------------------------------------------


def test_d12_call_chain_hit_search_then_get() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="skill",
            arg_digest='{"kind": "skill", "q": "mining"}',
            arg_keys=("kind", "q"),
            result_bytes=500,
            result_head="precis-mining-help — how to mine sessions",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-mining-help", "kind": "skill"}',
            arg_keys=("id", "kind"),
            result_bytes=800,
        ),
    ]
    cands = detect.detect_call_chain(events)
    assert len(cands) == 1
    assert cands[0].signature == "search|skill -> get|skill"
    assert cands[0].n == 2
    assert cands[0].metrics["runs"] == 1
    assert cands[0].metrics["turns_saved"] == 1


def test_d12_call_chain_hit_three_call_fanout() -> None:
    """search -> get -> get where BOTH gets take ids from the one search
    result — the second get does not feed from the first, yet the whole
    thing is still one chain a composite would collapse to a single call."""
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="gripe",
            arg_digest='{"kind": "gripe", "q": "mining"}',
            arg_keys=("kind", "q"),
            result_bytes=600,
            result_head="gripe 484070 auto-capture ... gripe 366639 ledger",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="gripe",
            arg_digest='{"id": 484070, "kind": "gripe"}',
            arg_keys=("id", "kind"),
            result_bytes=900,
        ),
        mk(
            session="s1",
            seq=3,
            verb="get",
            kind="gripe",
            arg_digest='{"id": 366639, "kind": "gripe"}',
            arg_keys=("id", "kind"),
            result_bytes=800,
        ),
    ]
    cands = detect.detect_call_chain(events)
    assert len(cands) == 1
    assert cands[0].signature == "search|gripe -> get|gripe -> get|gripe"
    assert cands[0].metrics["turns_saved"] == 2


def test_d12_call_chain_hit_id_eq_dialect() -> None:
    """The ``id=5`` arg_digest dialect (jobs transcripts, hand-built cards)
    links the same way the JSON dialect does."""
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="paper",
            arg_digest="kind='paper', q='graphene'",
            arg_keys=("kind", "q"),
            result_bytes=400,
            result_head="paper 'graph-101' on graphene",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="paper",
            arg_digest="kind='paper', id='graph-101'",
            arg_keys=("id", "kind"),
            result_bytes=700,
        ),
    ]
    cands = detect.detect_call_chain(events)
    assert len(cands) == 1
    assert cands[0].signature == "search|paper -> get|paper"


def test_d12_call_chain_near_miss_id_not_in_result() -> None:
    """get(id=X) after a search whose result never showed X is an ordinary
    look-up-then-fetch-the-unrelated workflow, not a chain."""
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="gripe",
            arg_digest='{"kind": "gripe", "q": "mining"}',
            arg_keys=("kind", "q"),
            result_bytes=600,
            result_head="gripe 484070 auto-capture",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="gripe",
            arg_digest='{"id": 999999, "kind": "gripe"}',
            arg_keys=("id", "kind"),
            result_bytes=800,
        ),
    ]
    assert detect.detect_call_chain(events) == []


def test_d12_call_chain_near_miss_short_value() -> None:
    """``id=5``-length values are dropped — '5' substring-matches inside
    any render by accident."""
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="paper",
            result_bytes=400,
            result_head="5 hits returned",
        ),
        mk(
            session="s1",
            seq=2,
            verb="get",
            kind="paper",
            arg_digest='{"id": 5, "kind": "paper"}',
            arg_keys=("id", "kind"),
            result_bytes=700,
        ),
    ]
    assert detect.detect_call_chain(events) == []


def test_d12_call_chain_near_miss_bash_breaks_run() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            verb="search",
            kind="gripe",
            result_bytes=600,
            result_head="gripe 484070 auto-capture",
        ),
        mk(session="s1", seq=2, tool="Bash", verb=None, arg_digest="ls"),
        mk(
            session="s1",
            seq=3,
            verb="get",
            kind="gripe",
            arg_digest='{"id": 484070, "kind": "gripe"}',
            arg_keys=("id", "kind"),
            result_bytes=800,
        ),
    ]
    assert detect.detect_call_chain(events) == []


# ---------------------------------------------------------------------------
# D13 session_digest
# ---------------------------------------------------------------------------


def test_d13_session_digest_hit_bounded_timeline() -> None:
    events = [
        mk(
            session="s1",
            seq=1,
            tool="__user__",
            verb=None,
            result_head="find the mining skill",
        ),
        mk(
            session="s1",
            seq=2,
            verb="search",
            kind="skill",
            arg_digest='{"kind": "skill", "q": "mining"}',
            arg_keys=("kind", "q"),
            result_bytes=300,
        ),
        mk(
            session="s1",
            seq=3,
            verb="get",
            kind="skill",
            arg_digest='{"id": "precis-mining-help", "kind": "skill"}',
            arg_keys=("id", "kind"),
            is_error=True,
            err_type="NotFound",
            err_head="no skill 'precis-mining-help'",
        ),
    ]
    cands = detect.detect_session_digest(events)
    assert len(cands) == 1
    c = cands[0]
    assert c.signature == "session:s1"
    assert c.n == 2  # precis calls, not all events
    tl = c.metrics["timeline"]
    assert "user: find the mining skill" in tl
    assert "search|skill q=mining -> ok" in tl
    assert "get|skill id=precis-mining-help -> ERR:NotFound" in tl
    # Errored call leads the evidence so the card jumps to the failure.
    assert c.evidence[0].seq == 3


def test_d13_session_digest_near_miss_no_precis_calls() -> None:
    events = [
        mk(session="s1", seq=1, tool="Bash", verb=None, arg_digest="ls"),
        mk(session="s1", seq=2, tool="__user__", verb=None, result_head="hi"),
    ]
    assert detect.detect_session_digest(events) == []


def test_d13_session_digest_elides_long_threads() -> None:
    events = [
        mk(
            session="s1",
            seq=i,
            verb="get",
            kind="paper",
            arg_digest=f'{{"id": "paper-{i:04}", "kind": "paper"}}',
            arg_keys=("id", "kind"),
            result_bytes=100,
        )
        for i in range(1, detect._DIGEST_MAX_EVENTS + 20)
    ]
    cands = detect.detect_session_digest(events)
    assert len(cands) == 1
    assert "elided" in cands[0].metrics["timeline"]
    assert cands[0].caveat
    assert len(cands[0].metrics["timeline"]) <= detect._DIGEST_MAX_CHARS


def test_d13_session_digest_pins_user_turns_in_elision() -> None:
    """td580721: elision must never drop ``__user__`` turns — the middle
    of a long thread is where corrections hide, and dropping them made
    the model read blind to exactly the turns it exists for."""
    events = [
        mk(
            session="s1",
            seq=i,
            verb="get",
            kind="paper",
            arg_digest=f'{{"id": "paper-{i:04}", "kind": "paper"}}',
            arg_keys=("id", "kind"),
            result_bytes=100,
        )
        for i in range(1, detect._DIGEST_MAX_EVENTS + 20)
    ]
    # Two human turns deep in the elided middle.
    events[30] = mk(
        session="s1",
        seq=31,
        tool="__user__",
        verb=None,
        result_head="no, I meant the other paper",
    )
    events[50] = mk(
        session="s1",
        seq=51,
        tool="__user__",
        verb=None,
        result_head="still wrong, try again",
    )
    cands = detect.detect_session_digest(events)
    assert len(cands) == 1
    tl = cands[0].metrics["timeline"]
    assert "31 user: no, I meant the other paper" in tl
    assert "51 user: still wrong, try again" in tl
    assert "elided" in tl
    assert "pinned" in cands[0].caveat


def test_d13_session_digest_user_turn_not_truncated() -> None:
    """td580721: human text renders at the full extraction head, not an
    80-char cut that could drop the correction itself."""
    long_turn = "please fix " + "the argument shape " * 20 + "before continuing"
    events = [
        mk(session="s1", seq=1, verb="get", kind="paper", result_bytes=10),
        mk(session="s1", seq=2, tool="__user__", verb=None, result_head=long_turn),
    ]
    tl = detect.detect_session_digest(events)[0].metrics["timeline"]
    assert long_turn in tl


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


def test_cards_ledger_row_renders_itself_not_not_found(tmp_path: Path) -> None:
    """gr570090(d): a ledger day-bucket session is excluded from
    ``by_session``, so every ledger-sourced card printed "(event not
    found in this event set)". The card must render the row itself —
    arg_sample and error_head are exactly the evidence the card is for."""
    events = [
        mk(
            session="ledger:2026-11-04",
            seq=17,
            corpus="ledger",
            verb="search",
            kind="gripe",
            arg_keys=("kind", "q"),
            arg_digest='{"kind": "gripe", "q": "gr570090"}',
            is_error=True,
            err_type="BadInput",
            err_head="q must be >= 3 chars",
            ts="2026-11-04T09:00:00Z",
        ),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(e.to_json() for e in events), encoding="utf-8")
    candidate = {
        "detector": "hard_error",
        "signature": "search|gripe|BadInput|q must be >= # chars",
        "n": 1,
        "n_sessions": 1,
        "first_ts": None,
        "last_ts": None,
        "evidence": [{"session": "ledger:2026-11-04", "seq": 17}],
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
            "--random",
            "0",
        ]
    )
    text = next((out_dir / "hard_error").glob("*.md")).read_text(encoding="utf-8")
    assert "event not found" not in text
    assert "q must be >= 3 chars" in text
    assert "gr570090" in text


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


def test_stats_precis_share_excludes_llmlog_bytes() -> None:
    """gr570090(b): llmlog ``result_bytes`` are LLM response chars, not
    tool results — including them printed 5.6% where the payload-corpus
    share was 15.3%."""
    events = [
        mk(seq=1, verb="get", kind="paper", result_bytes=25),
        mk(seq=2, tool="Bash", verb=None, arg_digest="ls", result_bytes=75),
        mk(
            seq=3,
            tool="__llm__",
            verb=None,
            corpus="llmlog",
            result_bytes=10_000,
        ),
    ]
    sb = stats.build_scoreboard(events)
    tk = sb["tokens"]
    assert tk["total_tool_result_bytes"] == 100
    assert tk["precis_result_bytes_total"] == 25
    assert tk["precis_result_bytes_share"] == 0.25


def test_stats_detour_census_shares_detector_dedup() -> None:
    """td580721: the scoreboard consumes ``detect.detour_hits``, so the
    same call extracted under two session paths counts once in BOTH
    places — before, the census counted it twice and the scoreboard
    disagreed with the detector's cards."""
    digest = '{"command": "psql -c \\"select * from refs\\""}'
    events = [
        mk(
            session="s1",
            seq=4,
            ts="2026-11-05T10:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
            result_bytes=10,
        ),
        mk(
            session="s1/dup",
            seq=4,
            ts="2026-11-05T10:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
            result_bytes=10,
        ),
        mk(
            session="s2",
            seq=4,
            ts="2026-11-05T11:00:00Z",
            tool="Bash",
            verb=None,
            arg_digest=digest,
            result_bytes=10,
        ),
    ]
    sb = stats.build_scoreboard(events)
    det = detect.detect_detour_census(events)
    assert sb["detour_census"]["count"] == 2
    assert det[0].n == 2
    assert sb["detour_census"]["bytes_total"] == 20


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
