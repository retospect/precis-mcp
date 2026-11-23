"""Run the operator audit against the same schema it reads in production."""

from pathlib import Path

from precis.store import Store

_AUDIT = Path(__file__).resolve().parents[1] / "scripts/audit-paper-cite-keys.sql"


def test_audit_counts_each_live_paper_once(store: Store) -> None:
    both = store.insert_ref(kind="paper", slug="design19", title="Cell redesign")
    multiple = store.insert_ref(kind="paper", slug="kim21", title="A Catalyst")
    mismatch = store.insert_ref(kind="paper", slug="xu2025research", title="Membranes")
    store.insert_ref(kind="paper", slug="han2023cell", title="Fuel CELL-based design")
    retired = store.insert_ref(kind="paper", slug="lee2020wrong", title="Other")
    store.retire_ref(retired.id)
    store.insert_ref(kind="draft", slug="patel2020wrong", title="Other")
    store.insert_ref_identifiers(
        both.id, [("cite_key", "patel2019design", "tex-import")]
    )
    store.insert_ref_identifiers(
        multiple.id,
        [
            ("cite_key", "kim2021catalyst", "tex-import"),
            ("cite_key_alias", "kim2021wrong", "test"),
        ],
    )

    with store.pool.connection() as conn:
        rows = conn.execute(_AUDIT.read_text(encoding="utf-8")).fetchall()

    assert rows[0][:6] == ("summary", None, None, 3, 2, 2)
    assert {row[1]: (row[6], row[8]) for row in rows[1:]} == {
        both.id: (2, ["patel2019design"]),
        multiple.id: (2, []),
        mismatch.id: (1, ["xu2025research"]),
    }
