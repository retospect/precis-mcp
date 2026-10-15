"""scripts/dev seed-prod-shaped — the anonymizer strips secret-shaped fields,
the extract only reads, and loading the same slice twice gives the same DB."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest

from precis.store import Store

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_seed_prod_shaped.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_seed_prod_shaped", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve the module by name
    spec.loader.exec_module(mod)
    return mod


seed = _load()

# Secret-shaped fakes, built at runtime so the tree-wide secret gate never
# sees a literal (tests/test_deploy_tree_no_secrets.py).
_GH = "gh" + "p_" + "A1b2C3d4E5f6G7h8" + "I9j0K1l2M3n4O5p6Q7r8"
_ANT = "sk-" + "ant-" + "api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3"
_PEM = (
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----"
)
_URL = "postgresql://agent_rw:" + "S3cr3tPassw0rd" + "@db.internal:6432/precis"
_TAILNET = "100." + "101.7.9"
_LAN = "192." + "168.1.20"
_EMAIL = "someone" + "@" + "example.org"
_DISCORD = "<@" + "123456789012345678" + ">"
_VAULT_BLOB = "$ANSIBLE" + "_VAULT;1.1;AES256"

_ALL_SECRETS = [
    _GH,
    _ANT,
    "MIIEow",
    "S3cr3tPassw0rd",
    _TAILNET,
    _LAN,
    _EMAIL,
    _DISCORD,
    _VAULT_BLOB,
]


def _leaks(blob: str) -> list[str]:
    return [s for s in _ALL_SECRETS if s in blob]


def test_anonymizer_strips_secret_shaped_fields() -> None:
    anon = seed.Anonymizer(names=["Ada Lovelace", "alov"], ref_kinds={7: "paper"})
    row = {
        "ref_id": 1,
        "kind": "memory",
        "title": f"Ada said: token at {_URL}",
        "owner_login": "alov",
        "human_verified_by": "Ada Lovelace",
        "authors": None,
        "meta": {
            "api_key": "plain-but-keyed",
            "note": f"mail {_EMAIL}, host {_TAILNET} and {_LAN}, ping {_DISCORD}",
            "nested": [{"gh": _GH}, {"Authorization": "x"}, f"path /Users/ada/{_ANT}"],
            "handle": "adalovelace.com login",
            "vault": f"blob {_VAULT_BLOB}",
        },
    }
    out = anon.row("refs", row)
    blob = json.dumps(out)
    assert _leaks(blob) == []
    assert out["owner_login"] is None
    assert out["human_verified_by"] == "dev-user"
    assert out["meta"]["api_key"] == seed.REDACTED
    assert out["meta"]["nested"][1]["Authorization"] == seed.REDACTED
    assert "example.invalid" in out["meta"]["note"]
    assert "192.0.2.1" in out["meta"]["note"]
    assert "/Users/dev/" in out["meta"]["nested"][2]
    assert not re.search(r"(?i)ada|lovelace", blob)

    pem = anon.row("chunks", {"ref_id": 1, "text": f"key:\n{_PEM}\nend"})
    assert "MIIEow" not in pem["text"] and "redacted-private-key" in pem["text"]


def test_bibliographic_names_survive_but_credentials_do_not() -> None:
    anon = seed.Anonymizer(names=["Ada Lovelace"], ref_kinds={7: "paper"})
    paper = anon.row(
        "refs",
        {
            "ref_id": 7,
            "kind": "paper",
            "title": "Notes by Ada Lovelace",
            "authors": ["Lovelace, Ada"],
        },
    )
    assert paper["title"] == "Notes by Ada Lovelace"
    assert paper["authors"] == ["Lovelace, Ada"]
    chunk = anon.row("chunks", {"ref_id": 7, "text": f"Ada Lovelace ({_EMAIL})"})
    assert chunk["text"].startswith("Ada Lovelace") and _EMAIL not in chunk["text"]
    author = anon.row("paper_authors", {"ref_id": 7, "full_name": "Ada Lovelace"})
    assert author["full_name"] == "Ada Lovelace"


# ── extract against a fake prod-psql ───────────────────────────────────────

_CATALOG = {
    "columns": [
        {"t": t, "c": c, "gen": c == "tsv"}
        for t, cols in {
            "refs": ["ref_id", "kind", "title", "meta", "owner_login", "authors"],
            "chunks": ["chunk_id", "ref_id", "ord", "chunk_kind", "text", "tsv"],
            "links": ["link_id", "src_ref_id", "dst_ref_id", "relation", "set_by"],
            "tags": ["tag_id", "namespace", "value"],
            "ref_tags": ["ref_id", "tag_id"],
            "web_users": ["id", "login", "password_hash"],
            "chunk_embeddings": ["chunk_id", "embedder"],
        }.items()
        for c in cols
    ],
    "fks": [
        {"t": "chunks", "cols": ["ref_id"], "rt": "refs", "rcols": ["ref_id"]},
        {"t": "links", "cols": ["src_ref_id"], "rt": "refs", "rcols": ["ref_id"]},
        {"t": "links", "cols": ["dst_ref_id"], "rt": "refs", "rcols": ["ref_id"]},
        {"t": "ref_tags", "cols": ["ref_id"], "rt": "refs", "rcols": ["ref_id"]},
        {"t": "ref_tags", "cols": ["tag_id"], "rt": "tags", "rcols": ["tag_id"]},
        {"t": "refs", "cols": ["owner_login"], "rt": "web_users", "rcols": ["login"]},
        {
            "t": "chunk_embeddings",
            "cols": ["chunk_id"],
            "rt": "chunks",
            "rcols": ["chunk_id"],
        },
    ],
    "pks": {
        "refs": ["ref_id"],
        "chunks": ["chunk_id"],
        "links": ["link_id"],
        "tags": ["tag_id"],
    },
    "rows": {"tags": 10},
}

_PROD_ROWS: list[tuple[str, dict[str, Any]]] = [
    ("tags", {"tag_id": 900001, "namespace": "SPACE", "value": "dev-seed"}),
    (
        "refs",
        {
            "ref_id": 900001,
            "kind": "memory",
            "title": "Ada's note",
            "meta": {"token": "abc", "hook": f"see {_URL}"},
            "owner_login": "alov",
            "authors": None,
        },
    ),
    (
        "refs",
        {
            "ref_id": 900002,
            "kind": "paper",
            "title": "Sketch of the Analytical Engine",
            "meta": {},
            "owner_login": None,
            "authors": ["Lovelace, Ada"],
        },
    ),
    (
        "chunks",
        {
            "chunk_id": 900001,
            "ref_id": 900001,
            "ord": 0,
            "chunk_kind": "heading",
            "text": f"{_GH} {_EMAIL}",
        },
    ),
    (
        "chunks",
        {
            "chunk_id": 900002,
            "ref_id": 900002,
            "ord": 0,
            "chunk_kind": "heading",
            "text": "body by Ada Lovelace",
        },
    ),
    (
        "links",
        {
            "link_id": 900001,
            "src_ref_id": 900001,
            "dst_ref_id": 900002,
            "relation": "cites",
            "set_by": "agent",
        },
    ),
    ("ref_tags", {"ref_id": 900001, "tag_id": 900001}),
]


class FakeProdPsql:
    """Answers the four statements extract sends, in ``-At`` shape."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, sql: str) -> str:
        self.sent.append(sql)
        if "pg_constraint" in sql:
            body = json.dumps(_CATALOG)
        elif "per_kind" in sql:
            body = json.dumps([900001, 900002])
        elif "web_users" in sql and "s_refs" not in sql:
            body = json.dumps(["Ada Lovelace", "alov"])
        else:
            body = "\n".join(f"{t}|{json.dumps(r)}" for t, r in _PROD_ROWS)
        return f"BEGIN\n{body}\nCOMMIT\n"


def test_extract_reads_only_and_writes_an_anonymized_slice(tmp_path: Path) -> None:
    fake = FakeProdPsql()
    out = tmp_path / "slice.jsonl"
    counts = seed.extract(out, per_kind=5, run=fake, anchors=(900001,))
    assert counts["refs"] == 2 and counts["chunks"] == 2

    for sql in fake.sent:  # nothing extract sends could change prod
        assert not re.search(
            r"(?i)\b(insert|update|delete|truncate|drop|alter|create)\b", sql
        )
    data_sql = fake.sent[-1]
    assert "web_users" not in data_sql and "chunk_embeddings" not in data_sql
    assert '"tsv"' not in data_sql  # generated columns come from the dev schema

    blob = out.read_text(encoding="utf-8")
    assert _leaks(blob) == []
    meta, rows = seed.read_slice(out)
    assert meta["tree"] == ["refs", "chunks", "links", "ref_tags"]
    assert meta["lookups"] == ["tags"]
    memo = next(r for r in rows["refs"] if r["kind"] == "memory")
    assert memo["meta"]["token"] == seed.REDACTED
    assert "Ada" not in memo["title"]
    paper = next(r for r in rows["refs"] if r["kind"] == "paper")
    assert paper["authors"] == ["Lovelace, Ada"]
    assert next(c for c in rows["chunks"] if c["ref_id"] == 900002)["text"].endswith(
        "Ada Lovelace"
    )


# ── load into the test DB, twice ───────────────────────────────────────────


def _snapshot(dsn: str) -> dict[str, list[tuple[Any, ...]]]:
    with psycopg.connect(dsn) as conn:
        return {
            "refs": conn.execute(
                "SELECT ref_id, kind, title FROM refs ORDER BY ref_id"
            ).fetchall(),
            "chunks": conn.execute(
                "SELECT chunk_id, ref_id, text FROM chunks ORDER BY chunk_id"
            ).fetchall(),
            "links": conn.execute(
                "SELECT src_ref_id, dst_ref_id, relation FROM links"
            ).fetchall(),
            "ref_tags": conn.execute("SELECT ref_id, tag_id FROM ref_tags").fetchall(),
        }


def test_load_is_idempotent_and_replaces_the_slice(
    store: Store, tmp_path: Path
) -> None:
    assert store.dsn is not None
    slice_path = tmp_path / "slice.jsonl"
    seed.extract(slice_path, per_kind=5, run=FakeProdPsql(), anchors=(900001,))
    with psycopg.connect(store.dsn) as conn:
        conn.execute(
            "INSERT INTO refs (ref_id, kind, title) VALUES (5, 'memory', 'old dev row')"
        )

    first = seed.load_slice(store.dsn, slice_path, migrate=False)
    snap1 = _snapshot(store.dsn)
    second = seed.load_slice(store.dsn, slice_path, migrate=False)
    snap2 = _snapshot(store.dsn)

    assert first == second
    assert snap1 == snap2
    assert [r[0] for r in snap2["refs"]] == [900001, 900002]  # old dev row replaced
    assert len(snap2["chunks"]) == 2 and len(snap2["links"]) == 1
    assert snap2["ref_tags"] == [(900001, 900001)]
    assert _leaks(json.dumps(snap2, default=str)) == []

    with psycopg.connect(store.dsn) as conn:  # new dev writes land past the slice
        new_id = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('memory', 'fresh') RETURNING ref_id"
        ).fetchone()
    assert new_id is not None and new_id[0] > 900002


def test_dangling_fks_are_pruned(store: Store, tmp_path: Path) -> None:
    """A link to a ref outside the slice is dropped, not loaded dangling."""
    assert store.dsn is not None
    slice_path = tmp_path / "slice.jsonl"
    meta = {"version": seed.SLICE_VERSION, "tree": ["refs", "links"], "lookups": []}
    lines = [
        {"_meta": meta},
        {"t": "refs", "r": {"ref_id": 910001, "kind": "memory", "title": "a"}},
        {
            "t": "links",
            "r": {
                "link_id": 910001,
                "src_ref_id": 910001,
                "dst_ref_id": 999999,
                "relation": "cites",
                "set_by": "agent",
            },
        },
    ]
    slice_path.write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )
    counts = seed.load_slice(store.dsn, slice_path, migrate=False)
    assert counts["refs"] == 1 and counts["links"] == 0 and counts["_pruned"] == 1


@pytest.mark.parametrize("bad", [{"version": 99}, {}])
def test_read_slice_refuses_an_unknown_version(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"_meta": bad}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        seed.read_slice(p)
