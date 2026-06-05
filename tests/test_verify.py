"""Contract tests for ``precis verify`` + ``precis resolve --strict-verified``.

Two surfaces:

  ``precis verify <pub_id>`` stamps ``human_verified_at`` /
  ``human_verified_by`` / ``human_verified_note`` on a finding's
  ref. ``--clear`` undoes the stamp.

  ``precis resolve --strict-verified`` extends the existing
  ``--strict`` gate: an *established* finding whose
  ``human_verified_at`` is still NULL is rendered as in-flight
  (substitution skipped, exit code 3). The placeholder text keeps
  its in-flight marker so the operator sees what's blocking ship.
"""

from __future__ import annotations

import re
import sys

import pytest

from precis.cli.resolve import _lookup_finding, _resolve_text
from precis.cli.verify import _resolve_finding_ref_id
from precis.errors import NotFound
from precis.handlers.finding import FindingHandler
from precis.hints import HintBus
from precis.store.types import BlockInsert, Tag

# ── plumbing ────────────────────────────────────────────────────────


def _make_handler(store):
    class _StubHub:
        def __init__(self) -> None:
            self.store = store
            self.embedder = None
            self.hints = HintBus()

    return FindingHandler(hub=_StubHub())


def _seed_paper(store, *, cite_key: str = "miller23a") -> int:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"paper {cite_key}", meta={}
    )
    store.insert_blocks(
        ref.id, [BlockInsert(pos=0, text=f"body of {cite_key}", meta={})]
    )
    return ref.id


def _seed_established_finding(store) -> tuple[int, str]:
    """Seed a finding + flip it to ``STATUS:established`` with a
    ``primary_cite_key`` (so substitution would otherwise fire).
    Returns ``(ref_id, pub_id)``."""
    _seed_paper(store)
    h = _make_handler(store)
    resp = h.put(title="t", body="b", scope={}, cited_in="miller23a")
    ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
    pub_id = re.search(r"pub_id=(\w+)", resp.body).group(1)
    store.update_ref(ref_id, meta_patch={"primary_cite_key": "miller23a"})
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", "established"),
        set_by="chase",
        replace_prefix=True,
    )
    return ref_id, pub_id


# ── verify CLI helper: pub_id / numeric / unknown ───────────────────


class TestResolveFindingRefId:
    def test_resolves_by_pub_id(self, store) -> None:
        ref_id, pub_id = _seed_established_finding(store)
        assert _resolve_finding_ref_id(store, pub_id) == ref_id

    def test_resolves_by_numeric_id(self, store) -> None:
        ref_id, _ = _seed_established_finding(store)
        assert _resolve_finding_ref_id(store, str(ref_id)) == ref_id

    def test_rejects_unknown_pub_id(self, store) -> None:
        with pytest.raises(NotFound, match="no finding with pub_id"):
            _resolve_finding_ref_id(store, "deadbeef")

    def test_rejects_non_finding_ref(self, store) -> None:
        """A bare numeric id pointing at a non-finding ref is
        rejected — the verb is finding-scoped."""
        paper_id = _seed_paper(store)
        with pytest.raises(NotFound, match=f"ref_id={paper_id}"):
            _resolve_finding_ref_id(store, str(paper_id))

    def test_rejects_deleted_finding(self, store) -> None:
        ref_id, pub_id = _seed_established_finding(store)
        store.soft_delete_ref(ref_id)
        with pytest.raises(NotFound):
            _resolve_finding_ref_id(store, pub_id)


# ── set / clear via the store API ───────────────────────────────────


class TestSetHumanVerified:
    def test_stamps_columns_and_surfaces_in_lookup(self, store) -> None:
        ref_id, pub_id = _seed_established_finding(store)
        store.set_human_verified(ref_id, by="alice", note="chain ok")

        finding = _lookup_finding(store, pub_id)
        assert finding is not None
        assert finding["human_verified"] is True

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT human_verified_by, human_verified_note "
                "FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
        assert row == ("alice", "chain ok")

    def test_clear_drops_the_stamp(self, store) -> None:
        ref_id, pub_id = _seed_established_finding(store)
        store.set_human_verified(ref_id, by="alice")
        store.clear_human_verified(ref_id)

        finding = _lookup_finding(store, pub_id)
        assert finding is not None
        assert finding["human_verified"] is False

    def test_unknown_ref_raises_not_found(self, store) -> None:
        with pytest.raises(NotFound):
            store.set_human_verified(99999, by="alice")

    def test_idempotent_re_stamp(self, store) -> None:
        """Re-stamping refreshes the timestamp and overwrites the note."""
        ref_id, _ = _seed_established_finding(store)
        store.set_human_verified(ref_id, by="alice", note="first pass")

        with store.pool.connection() as conn:
            t1 = conn.execute(
                "SELECT human_verified_at FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()[0]

        store.set_human_verified(ref_id, by="bob", note="second pass")

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT human_verified_at, human_verified_by, "
                "       human_verified_note "
                "FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
        t2, by2, note2 = row
        assert t2 >= t1
        assert by2 == "bob"
        assert note2 == "second pass"


# ── resolve --strict-verified gate ──────────────────────────────────


class TestStrictVerified:
    def test_unverified_established_renders_as_inflight(self, store) -> None:
        """An established finding without ``human_verified_at`` is
        treated as in-flight under ``require_verified=True`` —
        substitution is skipped, the in-flight marker appears."""
        _ref_id, pub_id = _seed_established_finding(store)
        text = f"see the kV claim [{pub_id}]."

        out, summary = _resolve_text(
            text,
            store=store,
            format="plain",
            ascii_mode=True,
            keep_id=False,
            require_verified=True,
        )

        # primary_cite_key is "miller23a" — should NOT appear in the
        # output because verification gate blocked substitution.
        assert "miller23a" not in out
        # The placeholder text is preserved.
        assert pub_id in out
        # Summary records the unverified-as-inflight warning.
        assert summary.resolved_count == 0
        assert pub_id in summary.inflight_pub_ids
        # And the warning explains why.
        unverified = [
            w for w in summary.warnings if w[0] == pub_id and w[1] == "unverified"
        ]
        assert unverified, f"missing unverified warning; got {summary.warnings}"

    def test_verified_established_substitutes_normally(self, store) -> None:
        """Once stamped, the finding substitutes like any
        established placeholder."""
        ref_id, pub_id = _seed_established_finding(store)
        store.set_human_verified(ref_id, by="alice", note="reviewed")

        text = f"see the kV claim [{pub_id}]."
        out, summary = _resolve_text(
            text,
            store=store,
            format="plain",
            ascii_mode=True,
            keep_id=False,
            require_verified=True,
        )

        assert "miller23a" in out
        assert pub_id not in out
        assert summary.resolved_count == 1
        assert summary.inflight_pub_ids == []

    def test_lenient_mode_unchanged(self, store) -> None:
        """``require_verified=False`` keeps the original behaviour
        — established findings substitute even without verification."""
        _ref_id, pub_id = _seed_established_finding(store)

        text = f"see the kV claim [{pub_id}]."
        out, summary = _resolve_text(
            text,
            store=store,
            format="plain",
            ascii_mode=True,
            keep_id=False,
            require_verified=False,
        )

        assert "miller23a" in out
        assert summary.resolved_count == 1


# ── CLI integration: precis verify <pub_id> ─────────────────────────


class TestVerifyCli:
    def test_stamps_via_cli(
        self,
        store,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from precis.cli.main import main as cli_main

        ref_id, pub_id = _seed_established_finding(store)
        dsn = store.pool.conninfo

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "precis",
                "verify",
                pub_id,
                "--by",
                "reto",
                "--note",
                "walked the chain",
                "--database-url",
                dsn,
            ],
        )
        cli_main()
        out = capsys.readouterr().out
        assert f"ref_id={ref_id}" in out
        assert "reto" in out

        finding = _lookup_finding(store, pub_id)
        assert finding is not None
        assert finding["human_verified"] is True

    def test_clear_via_cli(
        self,
        store,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from precis.cli.main import main as cli_main

        ref_id, pub_id = _seed_established_finding(store)
        store.set_human_verified(ref_id, by="alice", note="initial")
        dsn = store.pool.conninfo

        monkeypatch.setattr(
            sys,
            "argv",
            ["precis", "verify", pub_id, "--clear", "--database-url", dsn],
        )
        cli_main()
        finding = _lookup_finding(store, pub_id)
        assert finding is not None
        assert finding["human_verified"] is False

    def test_unknown_pub_id_exits_2(
        self,
        store,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from precis.cli.main import main as cli_main

        dsn = store.pool.conninfo
        monkeypatch.setattr(
            sys, "argv", ["precis", "verify", "nosuch", "--database-url", dsn]
        )
        with pytest.raises(SystemExit) as exc:
            cli_main()
        assert exc.value.code == 2
