"""Per-todo permission envelope (slice 8).

The envelope is the *permission* side of a work spec: three axes (egress /
write / return) resolved from ``meta.envelope`` into enforcement decisions
across three tiers. These tests pin the pure resolvers + the dark-default
behavior; the wiring into ``call_claude_agent`` lives in
``test_claude_agent.py``.
"""

from __future__ import annotations

import pytest

from precis.workers.envelope import (
    DEFAULT,
    ENVELOPES,
    Envelope,
    UnknownEnvelopeError,
    active_envelope,
    db_role,
    disallowed_tools,
    drops_side_effects,
    envelope_for_job,
    envelope_scope,
    named_envelope,
    network_mode,
    parse_envelope,
)

# ── parse: dark defaults ──────────────────────────────────────────


def test_absent_meta_is_default() -> None:
    assert parse_envelope(None) is DEFAULT
    assert parse_envelope({}) is DEFAULT


def test_non_dict_envelope_is_default() -> None:
    assert parse_envelope({"envelope": "nope"}) is DEFAULT
    assert parse_envelope({"envelope": ["a"]}) is DEFAULT


def test_partial_envelope_fills_permissive_defaults() -> None:
    env = parse_envelope({"envelope": {"write": "none"}})
    assert env == Envelope(egress="open", write="none", return_="full")


def test_full_envelope_parsed() -> None:
    env = parse_envelope(
        {"envelope": {"egress": "none", "write": "none", "return": "output-only"}}
    )
    assert env == Envelope(egress="none", write="none", return_="output-only")


def test_out_of_vocabulary_axis_falls_back_to_default() -> None:
    # A typo'd value must not silently loosen *or* tighten — it defaults.
    env = parse_envelope({"envelope": {"write": "readonly", "egress": "none"}})
    assert env.write == "full"  # bad value → permissive default
    assert env.egress == "none"  # the good axis still applies


# ── tier 1: disallowed_tools ──────────────────────────────────────


def test_default_denies_nothing() -> None:
    assert disallowed_tools(DEFAULT) == ()


def test_write_none_denies_mutate_verbs_and_fs_writes() -> None:
    deny = disallowed_tools(Envelope(write="none"))
    # ``put`` stays callable so the box can file gripes (dispatch carve-out).
    assert "mcp__precis__put" not in deny
    assert "mcp__precis__edit" in deny
    assert "mcp__precis__delete" in deny
    assert "mcp__precis__link" in deny
    assert "Write" in deny
    assert "Edit" in deny


def test_write_scoped_denies_only_delete() -> None:
    deny = disallowed_tools(Envelope(write="scoped"))
    assert deny == ("mcp__precis__delete",)


def test_egress_none_denies_fetch_tools() -> None:
    deny = disallowed_tools(Envelope(egress="none"))
    assert "WebFetch" in deny
    assert "WebSearch" in deny
    # write is still full, so no mutate verbs denied
    assert "mcp__precis__put" not in deny


def test_write_none_and_egress_none_combine() -> None:
    deny = disallowed_tools(Envelope(write="none", egress="none"))
    assert "mcp__precis__edit" in deny
    assert "WebFetch" in deny


# ── tier 2: db_role ───────────────────────────────────────────────


def test_write_none_is_read_only_role() -> None:
    assert db_role(Envelope(write="none")) == "agent_ro"


def test_write_scoped_and_full_are_rw() -> None:
    assert db_role(Envelope(write="scoped")) == "agent_rw"
    assert db_role(Envelope(write="full")) == "agent_rw"
    assert db_role(DEFAULT) == "agent_rw"


# ── tier 3: network_mode ──────────────────────────────────────────


def test_egress_none_is_no_network() -> None:
    assert network_mode(Envelope(egress="none")) == "none"


def test_egress_api_only_maps_through() -> None:
    assert network_mode(Envelope(egress="api-only")) == "api-only"


def test_egress_open_is_default_networking() -> None:
    assert network_mode(DEFAULT) is None


# ── return axis ───────────────────────────────────────────────────


def test_output_only_drops_side_effects() -> None:
    assert drops_side_effects(Envelope(return_="output-only")) is True
    assert drops_side_effects(DEFAULT) is False


# ── executor-scoped active envelope ───────────────────────────────


def test_active_envelope_is_none_outside_scope() -> None:
    assert active_envelope() is None


def test_scope_binds_and_restores() -> None:
    env = Envelope(write="none")
    assert active_envelope() is None
    with envelope_scope(env):
        assert active_envelope() is env
    assert active_envelope() is None


def test_scope_none_is_noop() -> None:
    with envelope_scope(None):
        assert active_envelope() is None


def test_nested_scopes_restore_outer() -> None:
    outer = Envelope(write="scoped")
    inner = Envelope(write="none")
    with envelope_scope(outer):
        assert active_envelope() is outer
        with envelope_scope(inner):
            assert active_envelope() is inner
        assert active_envelope() is outer
    assert active_envelope() is None


# ── named envelopes (params.envelope, td573740) ───────────────────


def test_named_envelope_scout_shape() -> None:
    env = named_envelope("scout")
    assert env is ENVELOPES["scout"]
    # WebSearch alone — WebFetch is deliberately out (it would fetch from
    # the worker host with no SSRF guard); the write axis stays at the
    # default because with no MCP server at all it is moot.
    assert env.builtin_tools == ("WebSearch",)


@pytest.mark.parametrize("bad", ["bogus", "", None, 42, ["scout"], {"x": 1}])
def test_named_envelope_fails_closed(bad: object) -> None:
    """An unknown/empty/non-string name raises — never defaults open."""
    with pytest.raises(UnknownEnvelopeError):
        named_envelope(bad)


def test_scout_disallowed_tools_cover_the_unlisted_builtins() -> None:
    """``builtin_tools`` allowlists via ``--tools``; the deny list closes
    the same gap belt-and-suspenders (an ignored flag must not open the
    box) — every built-in not in the allowlist."""
    deny = disallowed_tools(ENVELOPES["scout"])
    assert set(deny) == {
        "Bash",
        "Write",
        "Edit",
        "NotebookEdit",
        "Read",
        "Glob",
        "Grep",
        "Agent",
        "WebFetch",
    }


def test_default_envelope_carries_no_named_facets() -> None:
    """The open default must stay byte-identical: no allowlist, no fence."""
    assert DEFAULT.builtin_tools is None
    assert disallowed_tools(DEFAULT) == ()


def test_envelope_for_job_dark_paths() -> None:
    """No ``params.envelope`` key → the legacy meta.envelope resolver."""
    assert envelope_for_job(None) is DEFAULT
    assert envelope_for_job({}) is DEFAULT
    assert envelope_for_job({"params": {}}) is DEFAULT
    assert envelope_for_job({"params": "not-a-dict"}) is DEFAULT
    # The meta.envelope dict path is untouched by a params dict without
    # an 'envelope' key.
    env = envelope_for_job(
        {"params": {"model": "haiku"}, "envelope": {"write": "none"}}
    )
    assert env == Envelope(write="none")


def test_envelope_for_job_named_wins() -> None:
    env = envelope_for_job({"params": {"envelope": "scout"}})
    assert env is ENVELOPES["scout"]


@pytest.mark.parametrize("bad", ["bogus", "", None, 7])
def test_envelope_for_job_declared_but_bad_raises(bad: object) -> None:
    """A present ``params.envelope`` key is a *declaration* — a bad value
    fails the job closed rather than falling back to the dark path."""
    with pytest.raises(UnknownEnvelopeError):
        envelope_for_job({"params": {"envelope": bad}})
