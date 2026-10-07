"""Independent CLI acceptance of a synthetic orchestrator-shaped memory tree.

Expected metadata, bytes and links are generated independently of the importer.
No real memory directory is read. All minted ids are private test-DB addresses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from precis.cli import _build_parser
from precis.cli.memory import parse_index, run
from precis.utils.text import slugify
from tests.conftest import PG_TEST_DSN, _active_dsn, _pg_available

SOURCE_SHA = "0662bc0cfcae6c8038e24ef7b50373e931049ce8"
REVIEWED_SHA = "2755f8a1adf70ea3ad38ed9318b8fac8f9b6b2e4"
ROOT = Path(__file__).resolve().parents[1]
MIRROR_BLOBS = {
    "src/precis/cli/memory_mirror.py": "57df4173ab98929c2151a63d23608a65be5d38c0",
    "src/precis/cli/memory.py": "6221172b05433fa75a44765db8f0a13e24ac4343",
    "src/precis/utils/text.py": "ee025af69aae61fd32f7060c78abe178a797b995",
}


def _private_test_database(dsn: str, template_dsn: str, *, github_actions: bool) -> str:
    """Refuse anything except a clone of the canonical Docker/CI test DSN."""
    assert not any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE")), (
        "ambient test endpoint overrides are not permitted"
    )
    target = conninfo_to_dict(dsn)
    template = conninfo_to_dict(template_dsn)
    # Match scripts/test and check.yml, not arbitrary PG env/default endpoints.
    assert set(template) <= {"host", "port", "user", "password", "dbname"}, (
        "test endpoint overrides are not permitted"
    )
    assert template.get("dbname") == "precis_test", "canonical test template required"
    assert template.get("user") == "postgres" and template.get("port") == "5432", (
        "canonical test service role/port required"
    )
    host = template.get("host")
    assert host == "precis-test-db" or (github_actions and host == "localhost"), (
        "requires Docker test service or GitHub Actions localhost service"
    )
    database = target.pop("dbname", "")
    assert isinstance(database, str), "test database name must be a string"
    template.pop("dbname")
    assert target == template, "active DSN must retain the canonical test endpoint"
    assert re.fullmatch(r"precis_test_[0-9a-f]{12}", database), (
        "private throwaway clone required"
    )
    return database


@pytest.mark.parametrize(
    ("host", "github_actions"), [("precis-test-db", False), ("localhost", True)]
)
def test_acceptance_guard_allows_canonical_private_clones(
    host: str, github_actions: bool
) -> None:
    template = f"host={host} port=5432 user=postgres dbname=precis_test"
    clone = template.replace("dbname=precis_test", "dbname=precis_test_012345abcdef")
    assert (
        _private_test_database(clone, template, github_actions=github_actions)
        == "precis_test_012345abcdef"
    )


@pytest.mark.parametrize(
    ("template_change", "active_change", "github_actions"),
    [
        ("", "dbname=precis_test", False),  # shared fallback
        ("", "dbname=precis", False),  # production name
        ("", "dbname=precis_test_handmade", False),
        ("host=localhost", "host=localhost", False),  # arbitrary local execution
        ("host=production.example", "host=production.example", True),
        ("port=5433", "port=5433", True),
        ("user=precis", "user=precis", True),
        ("dbname=precis", "", False),  # production template
        ("hostaddr=192.0.2.1", "hostaddr=192.0.2.1", True),
        ("service=production", "service=production", True),
        ("", "host=localhost", True),  # clone endpoint differs from template
        ("", "hostaddr=192.0.2.1", False),
    ],
)
def test_acceptance_guard_refuses_unsafe_targets(
    template_change: str, active_change: str, github_actions: bool
) -> None:
    template = "host=precis-test-db port=5432 user=postgres dbname=precis_test"
    clone = template.replace("dbname=precis_test", "dbname=precis_test_012345abcdef")
    with pytest.raises(AssertionError):
        _private_test_database(
            f"{clone} {active_change}",
            f"{template} {template_change}",
            github_actions=github_actions,
        )


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE"])
def test_acceptance_guard_refuses_ambient_routing(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = "host=precis-test-db port=5432 user=postgres dbname=precis_test"
    clone = template.replace("dbname=precis_test", "dbname=precis_test_012345abcdef")
    monkeypatch.setenv(variable, "untrusted-override")
    with pytest.raises(AssertionError, match="ambient test endpoint overrides"):
        _private_test_database(clone, template, github_actions=False)


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Project Memory", "project-memory"),
        ("  CLI / API__Notes!  ", "cli-api-notes"),
        ("already-kebab-123", "already-kebab-123"),
        ("Café + Δ", "caf"),  # historic ASCII-only keys, no diacritic folding
        ("Å", ""),
        (" -- ! -- ", ""),
        ("", ""),
    ],
)
def test_shared_slugify_preserves_memory_section_keys(
    heading: str, expected: str
) -> None:
    # Fixed outputs from the pre-DRY local helper, including empty fallback.
    assert slugify(heading) == expected
    sections = parse_index(f"## {heading}\n")
    if heading.strip():
        [section] = sections
        assert section.slug == expected
    else:
        assert sections == []  # empty headings are not sections


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fixture(
    root: Path,
) -> tuple[dict[str, bytes], dict[str, Any], set[tuple[str, str]]]:
    root.mkdir()
    files: dict[str, bytes] = {}
    metadata: dict[str, Any] = {}
    edges: set[tuple[str, str]] = set()
    index = "# SYNTHETIC orchestrator memory index — no real content\n\n"
    types = ["user", "feedback", "project", "reference"]
    for i in range(120):
        name = f"topic-{i:03}.md"
        kind = types[i % 4]
        title = f"Synthetic {kind} topic {i:03}"
        description = f"Shape-only acceptance fixture {i:03}; no real user guidance."
        metadata[name] = {
            "name": title,
            "description": description,
            "metadata": {"type": kind, "synthetic": True, "ordinal": i},
            "extra": {"unicode": "π", "values": ["alpha", "beta"]},
        }
        # Deliberate quoting, comment, flow/block styles, whitespace, Unicode.
        header = (
            f'---\n# synthetic fixture, preserve this comment\nname: "{title}"\n'
            f"description: '{description}'\nmetadata:\n  type: {kind}\n"
            f"  synthetic: true\n  ordinal: {i}\n"
            'extra: {unicode: "π", values: [alpha, beta]}\n---\n'
        )
        next_name, prev_name = (
            f"topic-{(i + 1) % 120:03}.md",
            f"topic-{(i - 1) % 120:03}.md",
        )
        body = (
            f"\n# {title}\n\n**Why:** test byte preservation, not personal knowledge.  \n"
            "**How to apply:** use only in the throwaway test database.\n\n"
            f"Walk [[{next_name[:-3]}]] then [previous]({prev_name}).\n"
        )
        raw = (header + body).encode()
        if i % 2:
            raw = raw.replace(b"\n", b"\r\n")
        if i % 3 == 0:
            raw = raw.rstrip(b"\r\n")
        files[name] = raw
        edges.update({(name, next_name), (name, prev_name), ("MEMORY.md", name)})
        index += f"- [{title}]({name}) — {description}\n"
    raw_index = index.encode()
    assert len(raw_index) < 15360
    files["MEMORY.md"] = (
        raw_index + b"\n<!-- " + b"p" * (15360 - len(raw_index) - 11) + b" -->\n"
    )
    assert len(files["MEMORY.md"]) == 15360
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    return files, metadata, edges


def _graph(dsn: str, namespace: str) -> dict[str, Any]:
    # Fresh connection after every CLI invocation; do not trust in-memory reports.
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT ref_id,title,meta FROM refs WHERE kind='memory' "
            "AND meta->'file_mirror'->>'namespace'=%s ORDER BY ref_id",
            (namespace,),
        ).fetchall()
        refs = {row[2]["file_mirror"]["filename"]: row[0] for row in rows}
        assert len(rows) == len(refs), "duplicate filename identity"
        ids = list(refs.values())
        names = {rid: name for name, rid in refs.items()}
        edges = conn.execute(
            "SELECT src_ref_id,dst_ref_id,relation,meta FROM links "
            "WHERE src_ref_id=ANY(%s) ORDER BY link_id",
            (ids,),
        ).fetchall()
        chunks = conn.execute(
            "SELECT chunk_id,ref_id,text FROM chunks WHERE ref_id=ANY(%s) "
            "AND chunk_kind='memory_body' ORDER BY ref_id",
            (ids,),
        ).fetchall()
        events = conn.execute(
            "SELECT event_id,ref_id,event,payload FROM ref_events WHERE ref_id=ANY(%s) "
            "ORDER BY event_id",
            (ids,),
        ).fetchall()
    assert all(
        rel == "related-to"
        and meta == {"source": "file-mirror", "namespace": namespace}
        for _, _, rel, meta in edges
    )
    named_edges = {(names[src], names[dst]) for src, dst, _, _ in edges}
    assert len(edges) == len(named_edges), "duplicate edge"
    return {
        "refs": refs,
        "rows": rows,
        "edges": edges,
        "named_edges": named_edges,
        "chunks": chunks,
        "events": events,
    }


@pytest.mark.db
def test_synthetic_orchestrator_roundtrip_acceptance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _pg_available():
        pytest.skip("canonical test PostgreSQL unavailable")
    dsn = _active_dsn()
    # The canonical session fixture must have cloned its test template. Refuse
    # shared fallback/endpoint overrides before connecting or writing fixtures.
    database = _private_test_database(
        dsn, PG_TEST_DSN, github_actions=os.environ.get("GITHUB_ACTIONS") == "true"
    )
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT current_database(), current_user").fetchone()
    assert row == (database, "postgres"), "connected target must match the test clone"
    for path, expected in MIRROR_BLOBS.items():
        raw = (ROOT / path).read_bytes()
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        assert actual == expected, f"reviewed mirror source differs: {path}"

    source = tmp_path / "synthetic-source"
    expected_files, expected_metadata, expected_edges = _fixture(source)
    calls: list[dict[str, Any]] = []

    def invoke(mode: str, path: Path, namespace: str) -> str:
        argv = [
            "memory",
            "mirror",
            mode,
            str(path),
            "--namespace",
            namespace,
            "--database-url",
            dsn,
        ]
        run(_build_parser().parse_args(argv))
        receipt = capsys.readouterr().out.strip()
        calls.append(
            {"argv": argv[:-1] + ["<verified throwaway test DSN>"], "receipt": receipt}
        )
        return receipt

    first = json.loads(invoke("import", source, "acceptance"))
    assert (first["created"], first["updated"], first["unresolved"]) == (121, 0, [])
    graph = _graph(dsn, "acceptance")
    assert len(graph["refs"]) == 121 and graph["named_edges"] == expected_edges
    bodies = {rid: body for _, rid, body in graph["chunks"]}
    assert len(bodies) == 121
    for rid, title, meta in graph["rows"]:
        name = meta["file_mirror"]["filename"]
        assert (meta["file_mirror"]["header"] + bodies[rid]).encode() == expected_files[
            name
        ]
        if name != "MEMORY.md":
            assert meta["frontmatter"] == expected_metadata[name]
            assert title == expected_metadata[name]["name"]
            assert meta["hook"] == expected_metadata[name]["description"]
            assert "**Why:**" in bodies[rid] and "**How to apply:**" in bodies[rid]

    exported = tmp_path / "synthetic-export"
    assert invoke("export", exported, "acceptance") == "exported 121 files"
    actual_files = {p.name: p.read_bytes() for p in exported.iterdir()}
    assert actual_files == expected_files  # every byte, not normalized prose
    again = json.loads(invoke("import", source, "acceptance"))
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 121)
    assert again["refs"] == first["refs"]
    assert _graph(dsn, "acceptance") == graph

    # The export itself remains valid input; reconstruct links from its literals.
    copy = json.loads(invoke("import", exported, "acceptance-copy"))
    assert copy["created"] == 121
    copied_graph = _graph(dsn, "acceptance-copy")
    assert copied_graph["named_edges"] == expected_edges
    assert set(copy["refs"].values()).isdisjoint(first["refs"].values())
    reexported = tmp_path / "synthetic-reexport"
    invoke("export", reexported, "acceptance-copy")
    assert {p.name: p.read_bytes() for p in reexported.iterdir()} == expected_files

    report = {
        "status": "PASS_SYNTHETIC_TEST_DB_ONLY",
        "source_sha": SOURCE_SHA,
        "reviewed_source_sha": REVIEWED_SHA,
        "verified_git_blobs": MIRROR_BLOBS,
        "test_sha256": _hash(Path(__file__).read_bytes()),
        "source_provenance": "generated shape fixture; actual orchestrator source path not established; no real content read",
        "database": database,
        "address_warning": "ALL ids below are ephemeral TEST-DB addresses, NOT production handles",
        "topic_files": 120,
        "index_bytes": 15360,
        "total_files": 121,
        "metadata_types": dict(
            Counter(v["metadata"]["type"] for v in expected_metadata.values())
        ),
        "nodes_per_namespace": 121,
        "edges_per_namespace": len(expected_edges),
        "final_nodes_two_namespaces": 242,
        "final_edges_two_namespaces": 2 * len(expected_edges),
        "sample_test_refs": {
            name: first["refs"][name]
            for name in ["MEMORY.md", "topic-000.md", "topic-001.md"]
        },
        "sample_test_edges": [
            {"src": first["refs"][src], "dst": first["refs"][dst], "rel": "related-to"}
            for src, dst in sorted(expected_edges)[:3]
        ],
        "expected_actual": "121/121 files byte-identical after export and reexport; 121 identities and 360 edges unchanged on rerun; YAML/body literal checks passed",
        "per_file_hashes": {
            name: {
                "input": _hash(raw),
                "export": _hash(actual_files[name]),
                "reexport": _hash((reexported / name).read_bytes()),
            }
            for name, raw in sorted(expected_files.items())
        },
        "calls": calls,
        "limits": "Synthetic CLI/test-DB acceptance only. No deployed feature, production memory migration, native mirror verb, model or scientific claim. DB clone removed by canonical fixture teardown.",
    }
    report_path = ROOT / ".scratch" / "memory-mirror-acceptance.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"ACCEPTANCE PASS: {report_path}; TEST DB {database}; 121 files / 121 nodes / 360 edges per namespace"
    )
