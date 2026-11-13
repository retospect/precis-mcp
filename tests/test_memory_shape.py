"""Mechanical shape predicates and optional Git lookups; no DB or model."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from precis.handlers import _memory_shape as shape
from precis.store import Tag


def _codes(**kwargs) -> set[str]:
    return {f.code for f in shape.audit_shape(**kwargs).findings}


@pytest.mark.parametrize(
    ("title", "body", "tags", "code", "fix"),
    [
        (
            "Independent failures need separate repairs",
            "- Cache keys include the namespace\n- Cache keys include the version\n"
            "- Parser tokens require escaping\n- Upload retries preserve offsets",
            (),
            "grab-bag",
            "split into one claim",
        ),
        (
            "The parser preserves input",
            "Slice 4a needs escaping.",
            (),
            "unanchored-plan",
            "add the owning",
        ),
        (
            "The parser preserves input",
            "Merged the parser change.\nShipped the parser fix.\n"
            "The parser preserves input.\nEscaping retains delimiters.",
            (),
            "history-density",
            "move still-true claims",
        ),
        (
            "The parser preserves input",
            "The parser always preserves input.",
            ("section:reference",),
            "missing-route",
            "add a cites/derived-from",
        ),
        (
            "parser_snapshot",
            "The parser preserves input.",
            (),
            "title-shape",
            "state one claim",
        ),
    ],
)
def test_shape_fixture_names_fix(title, body, tags, code, fix):
    audit = shape.audit_shape(title=title, body=body, tags=tags)
    finding = next(f for f in audit.findings if f.code == code)
    assert f"AUDIT:{code}" in finding.advisory()
    assert fix in finding.advisory()
    assert code in audit.checked_codes
    assert Tag.parse_strict(f"AUDIT:{code}", kind="memory").value == code


def test_old_sha_fixture_names_fix(monkeypatch):
    old = int((datetime.now(UTC) - timedelta(days=31)).timestamp())
    monkeypatch.setattr(
        shape, "_commit_times", lambda shas: (("abcdef1" + "0" * 33, old),)
    )
    audit = shape.audit_shape(
        title="The parser preserves input", body="Fixed at abcdef1."
    )
    finding = next(f for f in audit.findings if f.code == "old-sha")
    assert "replace with a live anchor and keep the date" in finding.advisory()
    assert "abcdef1" in finding.advisory()


def test_grab_bag_clusters_repeated_topics_and_proposes_titles():
    audit = shape.audit_shape(
        title="Independent failures need separate repairs",
        body="- Cache keys include the namespace\n- Cache keys include the version\n"
        "- Parser tokens require escaping\n- Upload retries preserve offsets",
    )
    finding = next(f for f in audit.findings if f.code == "grab-bag")
    assert "~3 claims; 3 memories find better" in finding.advisory()
    assert finding.titles == (
        "Cache keys include the namespace",
        "Parser tokens require escaping",
        "Upload retries preserve offsets",
    )


@pytest.mark.parametrize("incident_tags", [(), ("prod-incident",), ("incident",)])
def test_incident_exempts_only_grab_bag(monkeypatch, incident_tags):
    old = int((datetime.now(UTC) - timedelta(days=31)).timestamp())
    monkeypatch.setattr(
        shape, "_commit_times", lambda shas: (("abcdef1" + "0" * 33, old),)
    )
    audit = shape.audit_shape(
        title="incident_snapshot",
        body="- Cache keys always include namespace; merged at abcdef1.\n"
        "- Parser tokens require escaping; shipped.\n"
        "- Upload retries preserve offsets; deployed.\n"
        "- Slice 4a repairs lease expiration; resolved.",
        tags=iter(("section:reference", *incident_tags)),
    )
    expected = shape.SHAPE_CODES - {"notation"}  # clean prose: no notation spot
    if "incident" in incident_tags:
        expected = expected - {"grab-bag"}
    assert {finding.code for finding in audit.findings} == expected
    # An exempt check is clean, not unavailable: stale grab-bag tags can clear.
    assert audit.checked_codes == shape.SHAPE_CODES


def test_section_index_hub_list_is_not_grab_bag():
    body = (
        "- me563920 — Structure and curator\n"
        "- me563916 — Projects and quests\n"
        "- me563907 — Sessions and panes\n"
        "- me568215 — Specifications index"
    )
    title = "repo-dev memory graph root"
    assert "grab-bag" in _codes(title=title, body=body)
    audit = shape.audit_shape(title=title, body=body, tags=("section:index",))
    assert "grab-bag" not in {f.code for f in audit.findings}
    # An exempt check is clean, not unavailable: stale grab-bag tags can clear.
    assert audit.checked_codes == shape.SHAPE_CODES


def test_one_topic_list_is_not_grab_bag():
    assert "grab-bag" not in _codes(
        title="Cache keys must include every input",
        body="- Cache keys include namespace\n- Cache keys include version\n"
        "- Cache keys include encoding\n- Cache keys include language",
    )


@pytest.mark.parametrize(
    "title",
    [
        "Cache and parser traps",
        "Two parser traps",
        "Work from 2026-09-01 to 2026-10-01",
        "Work 2025–2026",
    ],
)
def test_title_can_signal_grab_bag(title):
    audit = shape.audit_shape(
        title=title,
        body="- Cache keys require versions.\n- Parser tokens need escaping.",
    )
    finding = next(f for f in audit.findings if f.code == "grab-bag")
    assert finding.titles
    assert all(len(t.split()) <= 12 for t in finding.titles)


def test_top_level_headings_count_but_fences_and_nested_bullets_do_not():
    title = "Independent failures need separate repairs"
    assert "grab-bag" in _codes(
        title=title,
        body="# Traps\n## Cache keys\n"
        "## Parser tokens\n## Upload offsets\n## Lease expiry",
    )
    assert "grab-bag" not in _codes(
        title=title,
        body="- Cache keys\n  - Parser tokens\n  - Upload offsets\n  - Lease expiry",
    )
    assert "grab-bag" not in _codes(
        title=title,
        body="```md\n- Cache keys\n"
        "- Parser tokens\n- Upload offsets\n- Lease expiry\n```",
    )


def test_ruling_is_grab_bag_exempt_like_incident():
    """gr631570 (me631991): a ruling's bullets are one decision's
    consequences, not independent claims — the other checks still apply."""
    title = "Tags stay flat axis:value; hierarchy lives only in part-of"
    body = (
        "- Tags keep the one-level axis:value namespace.\n"
        "- Topic hierarchies belong in the taxon kind.\n"
        "- The generated TOC lists roots then walks part-of.\n"
        "- Spec tags land on the spec root only."
    )
    assert "grab-bag" in _codes(title=title, body=body)
    audit = shape.audit_shape(title=title, body=body, tags=("ruling",))
    assert "grab-bag" not in {f.code for f in audit.findings}
    # An exempt check is clean, not unavailable: stale grab-bag tags can clear.
    assert audit.checked_codes == shape.SHAPE_CODES


@pytest.mark.parametrize(
    "label", ["slice 4a", "round 6", "R17", "R196", "leg 3", "wave 3"]
)
def test_plan_labels_need_handle_on_same_line(label):
    title = "The parser preserves input"
    assert "unanchored-plan" in _codes(
        title=title, body=f"{label} fixes this.\nSee td123."
    )
    assert "unanchored-plan" not in _codes(
        title=title, body=f"{label} fixes this: td123."
    )
    assert "unanchored-plan" not in _codes(
        title=title, body=f"{label} fixes this: md:main/docs/spec.md."
    )
    assert "unanchored-plan" in _codes(
        title=title, body=f"{label} fixed this on 2026-10-01."
    )


def test_plan_label_inside_a_hyphenated_doc_name_is_not_a_label():
    """gr631570 (me628980): ``se-atomic-round2`` / ``se-atomic round 2`` are
    document names; the audit needs a real label."""
    title = "The parser preserves input"
    assert "unanchored-plan" not in _codes(
        title=title, body="docs not yet transferred: se-atomic-round2."
    )
    assert "unanchored-plan" not in _codes(
        title=title, body="docs not yet transferred: se-atomic round 2."
    )
    # A bullet/dash before the match is no name part: real labels still fire.
    assert "unanchored-plan" in _codes(
        title=title, body="- round 6 needs a review pass."
    )


def test_history_density_threshold_and_dated_case():
    title = "The parser preserves input"
    assert "history-density" in _codes(
        title=title,
        body="Merged parser fix.\nShipped escaping fix.\n"
        "Input is preserved.\nEscaping retains delimiters.",
    )
    assert "history-density" not in _codes(
        title=title,
        body="Merged parser fix.\nInput is preserved.\nSource: python:main::Parser",
    )
    assert "history-density" not in _codes(
        title=title, body="2026-10-01: input is preserved."
    )
    assert "history-density" in _codes(
        title=title,
        body="2026-10-01: added escaping.\n2026-10-02: fixed delimiters.\n"
        "Input is preserved.\nEscaping retains delimiters.",
    )
    assert "history-density" not in _codes(
        title=title,
        body="Merged parser fix.\nInput is preserved.\n"
        "Escaping retains delimiters.\nSource: python:main::Parser",
    )


@pytest.mark.parametrize("route", ["edge", "source", "tag"])
def test_reference_route_forms(route):
    assert "missing-route" not in _codes(
        title="The parser preserves input",
        body="The parser always preserves input."
        + ("\nSource: python:main::Parser" if route == "source" else ""),
        meta={"type": "reference"},
        tags=("route:parser",) if route == "tag" else (),
        has_evidence=route == "edge",
    )


def test_uncertain_reference_and_workflow_do_not_need_route():
    assert "missing-route" not in _codes(
        title="The parser might preserve input",
        body="Perhaps this always preserves input?",
        tags=("section:reference",),
    )
    assert "missing-route" not in _codes(
        title="The parser preserves input",
        body="The parser always preserves input.",
        tags=("section:workflow",),
    )


@pytest.mark.parametrize(
    "title",
    [
        "parser_snapshot",
        "parser-snapshot",
        "src/parser.py",
        "The parser snapshot as of 2026-10-01",
        "word " * 17,
    ],
)
def test_title_shapes(title):
    assert "title-shape" in _codes(title=title, body="The parser preserves input.")


@pytest.mark.parametrize(
    ("title", "body", "tags"),
    [
        (
            "Dream and stage workers share one queue",
            "Dream workers share the job queue.\nStage workers share the job queue.",
            (),
        ),
        (
            "Two workers share one queue",
            "Both workers share the job queue.\nThe queue tracks pending jobs.",
            (),
        ),
        (
            "scripts/ship lands a branch via CAS squash",
            "The ship command checks the expected branch head before landing.",
            (),
        ),
        (
            "Use round a corner trick",
            "Round the corner before closing the outline.",
            (),
        ),
        ("Resistor R1 sets gain", "R1 sets the amplifier gain.", ()),
        ("r12", "The printed label identifies the resistor.", ()),
        ("cache", "The cache stores derived values.", ()),
        ("Parser escapes delimiters", "The escaping fix shipped.", ()),
        (
            "Parser escapes delimiters",
            "The escaping fix shipped.\nInput delimiters remain intact.",
            (),
        ),
        (
            "Parser escapes delimiters",
            "The escaping fix shipped.\n\nInput delimiters remain intact.\n\n",
            (),
        ),
        (
            "Workers share the job queue",
            "The queue is shared.\nWorkers are independent.\n"
            "Each worker uses a lease to claim a job.",
            ("section:reference",),
        ),
        ("Numeric identifiers remain opaque", "The identifier is 1234567.", ()),
        ("Date stamps remain opaque", "The date stamp is 20261104.", ()),
    ],
)
def test_ordinary_memories_have_no_audit_codes(monkeypatch, title, body, tags):
    lookup = Mock(side_effect=AssertionError("ordinary prose must not query Git"))
    monkeypatch.setattr(shape, "_commit_times", lookup)
    assert _codes(title=title, body=body, tags=tags) == set()
    lookup.assert_not_called()


def test_clean_reference_gotcha_and_numbered_runbook_have_no_codes():
    memories = [
        (
            "Dream and stage workers share one queue",
            "Dream workers and stage workers claim jobs from the shared queue.\n"
            "Each claim carries a lease so another worker can recover abandoned work.",
            "section:reference",
        ),
        (
            "Retry body replacement after a concurrent edit",
            "A concurrent editor can invalidate the replacement snapshot.\n"
            "Read the body again before retrying the anchored edit.\n"
            "Source: src/precis/handlers/memory.py::MemoryHandler.edit",
            "section:gotchas",
        ),
        (
            "Inspect and restart a stalled worker",
            "1. Inspect the oldest pending job in the queue.\n"
            "2. Check the process log for an expired lease.\n"
            "3. Restart the worker with its configured profile.\n"
            "4. Confirm that the pending job advances.",
            "section:runbooks",
        ),
    ]
    for title, body, section in memories:
        assert _codes(title=title, body=body, tags=(section,)) == set()


@pytest.mark.parametrize(
    "body",
    [
        "The retry delay is 5 seconds.",
        "Retries always preserve input.",
        "Retries never discard input.",
        "Only retries change the attempt count.",
    ],
)
def test_confident_reference_claim_needs_route(body):
    assert _codes(
        title="Retry behavior follows the worker contract",
        body=body,
        tags=("section:reference",),
    ) == {"missing-route"}


@pytest.mark.parametrize("token", ["1234567", "20261104", "abcdefa", "ABCDEFAB"])
def test_sha_requires_both_a_letter_and_a_digit(monkeypatch, token):
    lookup = Mock(side_effect=AssertionError("non-SHA tokens must not query Git"))
    monkeypatch.setattr(shape, "_commit_times", lookup)
    audit = shape.audit_shape(title="Identifiers retain their spelling", body=token)
    assert audit.findings == ()
    assert "old-sha" in audit.checked_codes
    lookup.assert_not_called()


def test_clean_claim_clears_all_checks_without_git(monkeypatch):
    lookup = Mock(side_effect=AssertionError("no hashes, no Git"))
    monkeypatch.setattr(shape, "_commit_times", lookup)
    audit = shape.audit_shape(
        title="The parser preserves input",
        body="Input survives escaping.\nSource: python:main::Parser\n2026-10-01 UTC",
    )
    assert audit.findings == ()
    assert audit.checked_codes == shape.SHAPE_CODES
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("git", 0.05),
        subprocess.CalledProcessError(128, "git"),
        ValueError(),
    ],
)
def test_git_failure_does_not_clear_old_sha_or_block_other_advice(monkeypatch, error):
    monkeypatch.setattr(shape, "_commit_times", Mock(side_effect=error))
    audit = shape.audit_shape(
        title="parser_snapshot", body="The parser changed at abcdef1."
    )
    assert "old-sha" not in audit.checked_codes
    assert "title-shape" in {f.code for f in audit.findings}


def test_recent_sha_is_clean(monkeypatch):
    now = int(datetime.now(UTC).timestamp())
    monkeypatch.setattr(
        shape, "_commit_times", lambda shas: (("abcdef1" + "0" * 33, now),)
    )
    audit = shape.audit_shape(
        title="The parser preserves input", body="Verified at abcdef1."
    )
    assert "old-sha" in audit.checked_codes
    assert "old-sha" not in {f.code for f in audit.findings}


def test_git_lookup_is_bounded_local_and_cached(monkeypatch):
    lookup = Mock(
        return_value=subprocess.CompletedProcess([], 0, "abcdef1" + "0" * 33 + ":1\n")
    )
    monkeypatch.setattr(shape.subprocess, "run", lookup)
    shape._commit_times.cache_clear()
    try:
        shape._commit_times(("abcdef1",))
        shape._commit_times(("abcdef1",))
        lookup.assert_called_once()
        kwargs = lookup.call_args.kwargs
        assert 0 < kwargs["timeout"] <= 0.05
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert "--no-walk" in lookup.call_args.args[0]
        assert "--ignore-missing" in lookup.call_args.args[0]
        assert "abcdef1^{commit}" in lookup.call_args.args[0]
        assert "log" in lookup.call_args.args[0]
        assert kwargs["encoding"] == "utf-8"
    finally:
        shape._commit_times.cache_clear()


def test_large_sha_list_skips_git_without_clean_verdict(monkeypatch):
    lookup = Mock(side_effect=AssertionError("oversize input must skip Git"))
    monkeypatch.setattr(shape, "_commit_times", lookup)
    audit = shape.audit_shape(
        title="The parser preserves input",
        body=" ".join(f"abc{i:04x}" for i in range(17)),
    )
    assert "old-sha" not in audit.checked_codes
    lookup.assert_not_called()


def test_unknown_sha_does_not_hide_known_old_commit(monkeypatch):
    lookup = Mock(
        return_value=subprocess.CompletedProcess([], 0, "abcdef1" + "0" * 33 + ":1\n")
    )
    monkeypatch.setattr(shape.subprocess, "run", lookup)
    shape._commit_times.cache_clear()
    try:
        audit = shape.audit_shape(
            title="The parser preserves input",
            body="The fix refers to abcdef1 and deadbee.",
        )
        finding = next(f for f in audit.findings if f.code == "old-sha")
        assert "abcdef1" in finding.message
        assert "deadbee" not in finding.message
    finally:
        shape._commit_times.cache_clear()


def test_locked_context_can_skip_git_without_clearing_its_flag(monkeypatch):
    lookup = Mock(side_effect=AssertionError("no Git while the write lock is held"))
    monkeypatch.setattr(shape, "_commit_times", lookup)
    audit = shape.audit_shape(
        title="The parser preserves input", body="Verified at abcdef1.", check_git=False
    )
    assert "old-sha" not in audit.checked_codes
    assert audit.git_context is None
    lookup.assert_not_called()


def test_notation_finding_carries_canon_wording_and_gripe_route() -> None:
    (finding,) = [
        f
        for f in shape.audit_shape(
            title="Clean title here",
            body="Mobility is 5 x 10^4 and C60 is round.",
            check_git=False,
        ).findings
        if f.code == "notation"
    ]
    assert "precis-notation-canon" in finding.message
    assert "caret-exponent" in finding.message
    assert "formula-ascii-subscript" not in finding.message  # never tags a memory
    assert "link='skill:precis-notation-canon'" in finding.message
    assert "rel='raises-concern-about'" in finding.message


def test_notation_checked_even_when_clean() -> None:
    audit = shape.audit_shape(title="Clean", body="Area is 3 cm².", check_git=False)
    assert "notation" in audit.checked_codes
    assert "notation" not in {f.code for f in audit.findings}
