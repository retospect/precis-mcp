"""``precis taproot lint`` (:mod:`precis.cli.taproot`'s ``lint`` subcommand).

Two layers, per the repo's DB-vs-pure split:

* Pure aggregation/formatting (``_lint_hub``/``_lint_code``/``_lint_cohort``/
  ``_print_lint``) over hand-built ``(ref_id, title, meta, body)`` rows --
  no DB. ``body`` is the hub's ``ord=0`` ``finding_body`` chunk text (feeds
  ``title-body-divergence``/``missing-body-chunk``) -- pass it equal to
  ``title`` in a cohort literal to keep those two codes silent when a test
  isn't specifically exercising them.
* DB-backed cohort selection (``_select_lint_cohort``) and ``--fix``
  writes (``_run_lint_fix``) via the ``store`` fixture, minting real
  ``TAPROOT:claim`` hubs with :func:`~precis.taproot.hub.mint_hub` so the
  ``ref_tags``/``tags``/``expires_at`` join is exercised for real.

``normalize_notation`` (a sibling agent's in-flight addition to
``precis.taproot.notation``) is faked in via ``monkeypatch`` rather than
assumed present -- these tests must pass whether or not it has landed yet.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from precis.cli import taproot as taproot_cli
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from tests.conftest import _active_dsn
from tests.workers._helpers import seed_ref

# ── pure aggregation/formatting (no DB) ──────────────────────────────────


def test_lint_code_splits_at_first_colon() -> None:
    warning = "ascii-ohm: 'kOhm' found -- use 'kΩ'"
    assert taproot_cli._lint_code(warning) == "ascii-ohm"


def test_lint_hub_runs_notation_sentence_and_scope_linters() -> None:
    # 'kOhm' -> notation (ascii-ohm); no evidence verb / no terminal period /
    # no epistemic mode -> sentence_lint; scope carries an unknown key.
    # body == title -- no title-body-divergence noise in this assertion.
    title = "The resistance is 5 kOhm"
    warnings = taproot_cli._lint_hub(title, {"scope": {"bogus": "x"}}, title)
    codes = {taproot_cli._lint_code(w) for w in warnings}
    assert "ascii-ohm" in codes
    assert "scope-unknown-key" in codes
    assert "no-terminal-period" in codes


def test_lint_hub_flags_title_body_divergence() -> None:
    warnings = taproot_cli._lint_hub(
        "DFT shows the gap is 1.2 eV.", {}, "DFT shows a DIFFERENT gap value."
    )
    codes = {taproot_cli._lint_code(w) for w in warnings}
    assert "title-body-divergence" in codes
    assert "missing-body-chunk" not in codes


def test_lint_hub_no_divergence_when_title_and_body_match() -> None:
    # Matching after .strip() -- surrounding whitespace must not count.
    warnings = taproot_cli._lint_hub(
        "DFT shows the gap is 1.2 eV.", {}, "  DFT shows the gap is 1.2 eV.  "
    )
    codes = {taproot_cli._lint_code(w) for w in warnings}
    assert "title-body-divergence" not in codes
    assert "missing-body-chunk" not in codes


def test_lint_hub_flags_missing_body_chunk() -> None:
    warnings = taproot_cli._lint_hub("DFT shows the gap is 1.2 eV.", {}, None)
    codes = {taproot_cli._lint_code(w) for w in warnings}
    assert "missing-body-chunk" in codes
    # Mutually exclusive with divergence -- nothing to diverge against.
    assert "title-body-divergence" not in codes


@pytest.mark.parametrize(
    "cohort,codes,expected_counts,expected_clean",
    [
        # Two hubs share one notation code; a third is clean under filter.
        (
            [
                (1, "The gap is 25 degrees C.", {}, "The gap is 25 degrees C."),
                (
                    2,
                    "The gap is 50 degrees C, measured by DFT.",
                    {},
                    "The gap is 50 degrees C, measured by DFT.",
                ),
                (
                    3,
                    "This claim uses DFT and shows a gap of 1.2 eV.",
                    {},
                    "This claim uses DFT and shows a gap of 1.2 eV.",
                ),
            ],
            None,
            None,  # asserted structurally below
            None,
        ),
    ],
)
def test_lint_cohort_aggregates_counts_by_code(
    cohort: list[tuple[int, str, dict[str, Any], str | None]],
    codes: list[str] | None,
    expected_counts: None,
    expected_clean: None,
) -> None:
    result = taproot_cli._lint_cohort(cohort, codes)
    assert result["cohort_size"] == 3
    assert "ascii-degrees" in result["codes"]
    assert sorted(result["codes"]["ascii-degrees"]) == [1, 2]
    # hub 3 has no ascii-degrees warning but still trips other codes
    # (no-terminal-period is fine -- it ends in '.'; over-long isn't hit) --
    # assert at minimum it isn't counted under ascii-degrees.
    assert 3 not in result["codes"]["ascii-degrees"]


def test_lint_cohort_codes_filter_narrows_to_named_codes() -> None:
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (1, "The gap is 25 degrees C.", {}, "The gap is 25 degrees C."),
        (2, "no warnings hub sentence.", {}, "no warnings hub sentence."),
    ]
    result = taproot_cli._lint_cohort(cohort, ["ascii-degrees"])
    assert set(result["codes"]) == {"ascii-degrees"}
    assert result["codes"]["ascii-degrees"] == [1]
    # Hub 2 trips other (unfiltered-out) codes for real, but since only
    # ascii-degrees is requested and hub 2 never trips it, it must not
    # count toward hubs_with_warnings under the filter.
    assert result["hubs_with_warnings"] == 1
    assert result["hubs_clean"] == 1


def test_lint_cohort_codes_filter_selects_title_body_divergence() -> None:
    """``--codes title-body-divergence`` narrows to just that code, same as
    any string-linter code -- the DB-derived code is a first-class citizen
    of the ``--codes`` filter, not a special case."""
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (1, "DFT shows the gap is 1.2 eV.", {}, "DFT shows a DIFFERENT gap."),
        (2, "no warnings hub sentence.", {}, "no warnings hub sentence."),
    ]
    result = taproot_cli._lint_cohort(cohort, ["title-body-divergence"])
    assert set(result["codes"]) == {"title-body-divergence"}
    assert result["codes"]["title-body-divergence"] == [1]
    assert result["hubs_with_warnings"] == 1
    assert result["hubs_clean"] == 1


def test_lint_cohort_empty_is_all_clean() -> None:
    result = taproot_cli._lint_cohort([], None)
    assert result == {
        "cohort_size": 0,
        "hubs_with_warnings": 0,
        "hubs_clean": 0,
        "codes": {},
    }


def test_print_lint_json_shape(capsys: Any) -> None:
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (1, "The gap is 25 degrees C.", {}, "The gap is 25 degrees C."),
        # Truly clean under all three linters: an evidence verb + epistemic
        # mode token + terminal period + a valid non-prose scope value +
        # a matching body chunk.
        (
            2,
            "DFT shows the gap is 1.2 eV.",
            {"scope": {"material": "Pd/C"}},
            "DFT shows the gap is 1.2 eV.",
        ),
    ]
    result = taproot_cli._lint_cohort(cohort, None)
    taproot_cli._print_lint(result, "json", detail=False)
    payload = json.loads(capsys.readouterr().out)
    assert payload["cohort_size"] == 2
    assert payload["hubs_with_warnings"] == 1
    assert payload["hubs_clean"] == 1
    assert "ascii-degrees" in payload["codes"]
    assert payload["codes"]["ascii-degrees"] == {"count": 1}
    assert "hub_ids" not in payload["codes"]["ascii-degrees"]  # no --detail


def test_print_lint_json_detail_lists_and_caps_hub_ids(capsys: Any) -> None:
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (i, "The gap is 25 degrees C.", {}, "The gap is 25 degrees C.")
        for i in range(1, 30)
    ]
    result = taproot_cli._lint_cohort(cohort, ["ascii-degrees"])
    taproot_cli._print_lint(result, "json", detail=True)
    payload = json.loads(capsys.readouterr().out)
    entry = payload["codes"]["ascii-degrees"]
    assert entry["count"] == 29
    assert len(entry["hub_ids"]) == taproot_cli._LINT_DETAIL_CAP
    assert entry["hub_ids"][0] == "fi1"
    assert entry["more"] == 29 - taproot_cli._LINT_DETAIL_CAP


def test_print_lint_json_detail_shows_title_body_divergence(capsys: Any) -> None:
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (1, "DFT shows the gap is 1.2 eV.", {}, "DFT shows a DIFFERENT gap."),
        (2, "no warnings hub sentence.", {}, None),  # missing-body-chunk instead
    ]
    result = taproot_cli._lint_cohort(cohort, None)
    taproot_cli._print_lint(result, "json", detail=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["codes"]["title-body-divergence"]["hub_ids"] == ["fi1"]
    assert payload["codes"]["missing-body-chunk"]["hub_ids"] == ["fi2"]


def test_print_lint_text_detail_shows_and_n_more(capsys: Any) -> None:
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (i, "The gap is 25 degrees C.", {}, "The gap is 25 degrees C.")
        for i in range(1, 25)
    ]
    result = taproot_cli._lint_cohort(cohort, ["ascii-degrees"])
    taproot_cli._print_lint(result, "text", detail=True)
    out = capsys.readouterr().out
    assert "ascii-degrees: 24" in out
    assert "...and 4 more" in out


def test_print_lint_text_no_warnings(capsys: Any) -> None:
    # Annotated because `list` is invariant: an inferred
    # `dict[str, dict[str, str]]` won't satisfy `dict[str, Any]`.
    cohort: list[tuple[int, str, dict[str, Any], str | None]] = [
        (
            1,
            "DFT shows the gap is 1.2 eV.",
            {"scope": {"material": "Pd/C"}},
            "DFT shows the gap is 1.2 eV.",
        ),
    ]
    result = taproot_cli._lint_cohort(cohort, None)
    taproot_cli._print_lint(result, "text", detail=False)
    out = capsys.readouterr().out
    assert "no lint warnings." in out


# ── DB-backed cohort selection ───────────────────────────────────────────


def _mint(store: Any, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def test_select_lint_cohort_default_returns_live_claim_hubs(store: Any) -> None:
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")
    h2 = _mint(store, "Second DFT claim shows a gap of 1.4 eV.")

    cohort = taproot_cli._select_lint_cohort(store, None)
    ref_ids = {r for r, _, _, _ in cohort}
    assert {h1, h2} <= ref_ids


def test_select_lint_cohort_excludes_non_claim_refs(store: Any) -> None:
    paper = seed_ref(store, title="Not a claim hub", kind="paper")
    cohort = taproot_cli._select_lint_cohort(store, None)
    ref_ids = {r for r, _, _, _ in cohort}
    assert paper not in ref_ids


def test_select_lint_cohort_hub_filter_selects_specific_hubs(store: Any) -> None:
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")
    h2 = _mint(store, "Second DFT claim shows a gap of 1.4 eV.")
    _mint(store, "Third DFT claim shows a gap of 1.6 eV.")  # not selected

    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}", f"fi{h2}"])
    ref_ids = [r for r, _, _, _ in cohort]
    assert set(ref_ids) == {h1, h2}


def test_select_lint_cohort_excludes_expired_tag(store: Any) -> None:
    h1 = _mint(store, "Expiring DFT claim shows a gap of 1.2 eV.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 day' "
            "WHERE ref_id = %s AND tag_id = ("
            "  SELECT tag_id FROM tags WHERE namespace = 'TAPROOT' AND value = 'claim'"
            ")",
            (h1,),
        )
        conn.commit()

    cohort = taproot_cli._select_lint_cohort(store, None)
    ref_ids = {r for r, _, _, _ in cohort}
    assert h1 not in ref_ids


def test_select_lint_cohort_pulls_body_chunk_text(store: Any) -> None:
    """The ``LEFT JOIN`` on the ``ord=0`` ``finding_body`` chunk delivers
    the live chunk text alongside title/meta -- a freshly-minted hub's
    title and body match (:func:`~precis.taproot.hub.mint_hub` writes both
    from the same ``claim.sentence``)."""
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")

    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])
    assert len(cohort) == 1
    ref_id, title, _meta, body = cohort[0]
    assert ref_id == h1
    assert body == title == "First DFT claim shows a gap of 1.2 eV."


def test_select_lint_cohort_body_none_when_chunk_missing(store: Any) -> None:
    """A live claim hub whose ``ord=0`` ``finding_body`` chunk was somehow
    removed still surfaces in the cohort (the ``LEFT JOIN`` never drops the
    row) with ``body_text=None`` -- :func:`_lint_hub` turns that into
    ``missing-body-chunk`` rather than silently excluding the hub."""
    h1 = _mint(store, "Third DFT claim shows a gap of 1.6 eV.")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord = 0", (h1,))
        conn.commit()

    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])
    assert len(cohort) == 1
    ref_id, _title, _meta, body = cohort[0]
    assert ref_id == h1
    assert body is None


# ── --fix / --apply ───────────────────────────────────────────────────────


def _fake_normalize_ohm(sentence: str) -> tuple[str, list[str]]:
    if "kOhm" in sentence:
        return sentence.replace("kOhm", "kΩ"), ["ascii-ohm"]
    return sentence, []


def test_run_lint_fix_reports_unavailable_when_normalize_notation_missing(
    store: Any, monkeypatch: Any
) -> None:
    import precis.taproot.notation as notation_mod

    monkeypatch.delattr(notation_mod, "normalize_notation", raising=False)
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])

    results: list[dict[str, Any]] = []
    available = taproot_cli._run_lint_fix(
        store, cohort, apply=False, set_by="agent", results=results
    )
    assert available is False
    assert results == []


def test_run_lint_fix_dry_run_writes_nothing(store: Any, monkeypatch: Any) -> None:
    import precis.taproot.notation as notation_mod

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])

    results: list[dict[str, Any]] = []
    available = taproot_cli._run_lint_fix(
        store, cohort, apply=False, set_by="agent", results=results
    )
    assert available is True
    assert len(results) == 1
    entry = results[0]
    assert entry["changed"] is True
    assert entry["applied"] is False
    assert "kΩ" in entry["new_title"]

    with store.pool.connection() as conn:
        row = conn.execute("SELECT title FROM refs WHERE ref_id = %s", (h1,)).fetchone()
    assert row is not None
    assert "kOhm" in row[0]  # unchanged -- dry-run wrote nothing


def test_run_lint_fix_apply_updates_title_and_body_chunk(
    store: Any, monkeypatch: Any
) -> None:
    import precis.taproot.notation as notation_mod

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])

    results: list[dict[str, Any]] = []
    available = taproot_cli._run_lint_fix(
        store, cohort, apply=True, set_by="agent", results=results
    )
    assert available is True
    entry = results[0]
    assert entry["applied"] is True

    with store.pool.connection() as conn:
        title_row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (h1,)
        ).fetchone()
        body_row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 AND retired_at IS NULL",
            (h1,),
        ).fetchone()
    assert title_row is not None and "kΩ" in title_row[0] and "kOhm" not in title_row[0]
    assert body_row is not None and "kΩ" in body_row[0]


def test_run_lint_fix_round_trip_mismatch_raises(store: Any, monkeypatch: Any) -> None:
    """``_run_lint_fix`` no longer runs its own round-trip check -- the
    real write door (:func:`~precis.taproot.hub.refine_claim_sentence`)
    owns that guarantee now. Simulate the historical silent-truncation bug
    one layer down (``store.update_ref``) so the *real*
    ``refine_claim_sentence`` runs and its own assert is what raises."""
    import precis.taproot.notation as notation_mod
    from precis.taproot.hub import TitleRoundTripError

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )

    orig_update_ref = store.update_ref

    def _truncating_update_ref(
        ref_id: int, *, title: str | None = None, **kw: Any
    ) -> Any:
        return orig_update_ref(ref_id, title=(title[:5] if title else title), **kw)

    monkeypatch.setattr(store, "update_ref", _truncating_update_ref)

    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}"])

    results: list[dict[str, Any]] = []
    with pytest.raises(TitleRoundTripError, match="round-trip mismatch"):
        taproot_cli._run_lint_fix(
            store, cohort, apply=True, set_by="agent", results=results
        )

    # Nothing was written -- rolled back atomically.
    with store.pool.connection() as conn:
        row = conn.execute("SELECT title FROM refs WHERE ref_id = %s", (h1,)).fetchone()
    assert row is not None and "kOhm" in row[0]


def test_run_lint_fix_reports_hubs_written_before_a_mid_batch_failure(
    store: Any, monkeypatch: Any
) -> None:
    """A raise on hub N must still leave hubs 1..N-1 visible in ``results``.

    Each ``refine_claim_sentence`` commits its own transaction, so those
    earlier writes are live in the DB. Discarding the record would leave an
    operator diffing the whole cohort by hand to find out what changed."""
    import precis.taproot.notation as notation_mod
    from precis.taproot.hub import TitleRoundTripError

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )

    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    h2 = _mint(store, "Impedance is 9 kOhm shown by DFT.")
    cohort = taproot_cli._select_lint_cohort(store, [f"fi{h1}", f"fi{h2}"])
    # _select_lint_cohort's order decides which hub fails; truncate on the
    # second one it hands us, whichever that is.
    second = cohort[1][0]

    orig_update_ref = store.update_ref

    def _truncating_update_ref(
        ref_id: int, *, title: str | None = None, **kw: Any
    ) -> Any:
        if ref_id == second and title:
            return orig_update_ref(ref_id, title=title[:5], **kw)
        return orig_update_ref(ref_id, title=title, **kw)

    monkeypatch.setattr(store, "update_ref", _truncating_update_ref)

    results: list[dict[str, Any]] = []
    with pytest.raises(TitleRoundTripError):
        taproot_cli._run_lint_fix(
            store, cohort, apply=True, set_by="agent", results=results
        )

    # Both hubs are accounted for: the first committed, the second did not.
    assert len(results) == 2
    assert results[0]["applied"] is True
    assert results[1]["applied"] is False
    assert results[1]["hub_ref_id"] == second

    with store.pool.connection() as conn:
        first_title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (cohort[0][0],)
        ).fetchone()
        second_title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (second,)
        ).fetchone()
    assert first_title is not None and "kΩ" in first_title[0]
    assert second_title is not None and "kOhm" in second_title[0]


# ── CLI-level (argparse.Namespace -> run()) ──────────────────────────────


def _cli_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "hub": None,
        "codes": None,
        "detail": False,
        "fix": False,
        "apply": False,
        "format": "text",
        "set_by": "agent",
        "database_url": _active_dsn(),
    }
    base.update(overrides)
    ns = argparse.Namespace(**base)
    ns.taproot_cmd = "lint"
    return ns


def test_cli_lint_text_reports_cohort_and_codes(store: Any, capsys: Any) -> None:
    _mint(store, "Resistance is 5 kOhm shown by DFT.")
    _mint(store, "Clean claim shows a gap of 1.2 eV by DFT.")

    taproot_cli.run(_cli_args())
    out = capsys.readouterr().out
    assert "cohort: 2 hub(s)" in out
    assert "ascii-ohm: 1" in out


def test_cli_lint_hub_filter_scopes_to_selected_hubs(store: Any, capsys: Any) -> None:
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    _mint(store, "A second, unrelated DFT claim shows a gap of 1.2 eV.")

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"]))
    out = capsys.readouterr().out
    assert "cohort: 1 hub(s)" in out
    assert "ascii-ohm: 1" in out


def test_cli_lint_codes_filter(store: Any, capsys: Any) -> None:
    # No trailing period -> trips BOTH ascii-ohm and no-terminal-period;
    # filtering to just the latter must hide the former from the output.
    _mint(store, "Resistance is 5 kOhm shown by DFT")

    taproot_cli.run(_cli_args(codes=["no-terminal-period"]))
    out = capsys.readouterr().out
    assert "no-terminal-period: 1" in out
    assert "ascii-ohm" not in out


def test_cli_lint_json_output_shape(store: Any, capsys: Any) -> None:
    _mint(store, "Resistance is 5 kOhm shown by DFT.")

    taproot_cli.run(_cli_args(format="json"))
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"cohort_size", "hubs_with_warnings", "hubs_clean", "codes"}
    assert payload["cohort_size"] == 1
    assert "ascii-ohm" in payload["codes"]


def test_cli_lint_reports_title_body_divergence(store: Any, capsys: Any) -> None:
    """Simulates the historical incident directly: a hub whose
    ``refs.title`` was written out of band (bypassing the write door, e.g.
    a stale-code caller) so it disagrees with the still-full
    ``finding_body`` chunk -- ``taproot lint`` must surface it."""
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s",
            ("First DFT claim shows a gap of", h1),  # truncated, like the incident
        )
        conn.commit()

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"], detail=True))
    out = capsys.readouterr().out
    assert "title-body-divergence: 1" in out
    assert f"fi{h1}" in out


def test_cli_lint_reports_missing_body_chunk(store: Any, capsys: Any) -> None:
    h1 = _mint(store, "Second DFT claim shows a gap of 1.4 eV.")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord = 0", (h1,))
        conn.commit()

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"]))
    out = capsys.readouterr().out
    assert "missing-body-chunk: 1" in out


def test_cli_lint_codes_filter_selects_title_body_divergence(
    store: Any, capsys: Any
) -> None:
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s",
            ("Diverged title text.", h1),
        )
        conn.commit()

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"], codes=["title-body-divergence"]))
    out = capsys.readouterr().out
    assert "title-body-divergence: 1" in out
    assert "no-terminal-period" not in out  # filtered out


def test_cli_lint_json_includes_title_body_divergence(store: Any, capsys: Any) -> None:
    h1 = _mint(store, "First DFT claim shows a gap of 1.2 eV.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s",
            ("Diverged title text.", h1),
        )
        conn.commit()

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"], format="json"))
    payload = json.loads(capsys.readouterr().out)
    assert "title-body-divergence" in payload["codes"]
    assert payload["codes"]["title-body-divergence"]["count"] == 1


def test_cli_lint_fix_never_proposes_title_body_divergence(
    store: Any, capsys: Any, monkeypatch: Any
) -> None:
    """``--fix`` is a REPORTING-only surface for this code -- it must never
    even consider fixing a title/body divergence (see the comment on
    :data:`taproot_cli._TITLE_BODY_DIVERGENCE_MSG`); ``--fix`` only ever
    proposes mechanically-safe *notation* rewrites via
    ``normalize_notation``, which never touches this code at all."""
    import precis.taproot.notation as notation_mod

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s",
            ("Resistance is 5 kOhm shown by DFT, diverged.", h1),
        )
        conn.commit()

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"], fix=True, apply=False))
    out = capsys.readouterr().out
    assert "title-body-divergence" not in out


def test_cli_lint_fix_without_apply_writes_nothing(
    store: Any, capsys: Any, monkeypatch: Any
) -> None:
    import precis.taproot.notation as notation_mod

    monkeypatch.setattr(
        notation_mod, "normalize_notation", _fake_normalize_ohm, raising=False
    )
    h1 = _mint(store, "Resistance is 5 kOhm shown by DFT.")

    taproot_cli.run(_cli_args(hub=[f"fi{h1}"], fix=True, apply=False))
    out = capsys.readouterr().out
    assert "DRY-RUN" in out

    with store.pool.connection() as conn:
        row = conn.execute("SELECT title FROM refs WHERE ref_id = %s", (h1,)).fetchone()
    assert row is not None and "kOhm" in row[0]
