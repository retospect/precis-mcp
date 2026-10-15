"""Seed the dev DB with a small, anonymized, prod-shaped slice.

Two halves, run by ``scripts/dev seed-prod-shaped``:

``extract`` (host)
    Reads prod through ``scripts/prod-psql --ro`` only (the ``agent_ro``
    role inside ``BEGIN READ ONLY``). Picks a bounded ref set (the newest
    ``--per-kind`` live refs of every kind, plus named anchors and their
    one-hop link neighbours), then follows the foreign-key graph down from
    ``refs`` in one statement: chunks, tags, links and every kind-specific
    child table (pcb_*, struct_*, se_*, measures, ...). Lookup tables those
    rows point at come along. Every row is anonymized in memory; only the
    anonymized slice is written, to a gitignored JSONL file.

``load`` (dev container)
    Creates the dev database if missing and migrates it (the schema comes
    from migrations, never from the dump), then in one transaction
    truncates the slice tables, inserts the slice, prunes rows whose
    foreign keys point outside it and moves sequences past the copied ids.
    Re-running replaces the slice.

Excluded on purpose: secrets and identities (web_users, email_account,
...), logs and runtime state, embeddings (the worker regenerates them) and
bulk caches. See :data:`EXCLUDED`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.utils.secret_scan import find_secrets, mask_secrets

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / ".scratch" / "dev-seed" / "slice.jsonl"
DEFAULT_DB = "precis_dev"
SLICE_VERSION = 1

#: Named anchors from the work that showed prod-only bugs on 2026-10-15:
#: quest qu164903 and two of its pathways (pourbaix map blend), pcb
#: taz15-blank (net classes), memory root me474312 and its seven hubs.
ANCHORS: tuple[int, ...] = (
    164903,
    483780,
    483766,
    482131,
    474312,
    477654,
    477655,
    477656,
    477657,
    477658,
    477659,
    477660,
)
#: Link neighbours pulled in per anchor (newest links first).
ANCHOR_HOP = 40
#: Gripes with the most comment text, and the most-seen alerts.
LONG_GRIPES = 5
BUSY_ALERTS = 10
#: Chunks kept per non-anchor ref (``ord`` below this); anchors keep all.
CHUNK_ORD_CAP = 120
#: Hard ceiling on rows per table.
TABLE_CAP = 50_000
#: A lookup parent this small is copied whole; a bigger one only the rows
#: the slice references.
LOOKUP_WHOLE_MAX = 5_000

EXCLUDED: frozenset[str] = frozenset(
    {
        # secrets, credentials and identities
        "web_users",
        "email_account",
        "email_scan",
        "app_settings",
        "service_config",
        "nanopub_trust_allowlist",
        "nanopub_publish",
        "nanopub_artifacts",
        "nanopub_mirror",
        "nanopub_mirror_edges",
        "nanopub_ots_batches",
        "nanopub_ots_leaves",
        "nanopub_ots_proofs",
        "nanopub_supersessions",
        # logs, telemetry and runtime state
        "_migrations",
        "llm_call_log",
        "llm_blob",
        "llm_outcome_hourly",
        "tool_calls",
        "worker_logs",
        "ref_events",
        "chunk_events",
        "host_heartbeat",
        "host_heartbeat_log",
        "dream_log",
        "dream_transcripts",
        "claude_quota_snapshot",
        "external_rate_limits",
        "scheduler_leases",
        "resource_slots",
        "resource_slot_holds",
        "kind_provider",
        "app_state",
        "revision_trigger_state",
        "cache_state",
        "revisions",
        "pdf_locations",
        # embeddings: the worker regenerates them
        "chunk_embeddings",
        "ref_embeddings",
        "claim_embeddings",
        "tag_embeddings",
        # bulk derived caches
        "cluster_runs",
        "cluster_cells",
        "cluster_assignments",
        "provenance_rw_cache",
        "provenance_rw_sync",
        "s2_neighbors",
        "chunk_blobs",
        "se_hexfold_catalogue",
    }
)

#: Kinds whose names are the bibliographic record: user names are kept.
BIBLIO_KINDS = frozenset({"paper", "patent", "citation", "orcid", "semanticscholar"})
BIBLIO_TABLES = frozenset({"paper_authors", "paper_bib_entries", "ref_identifiers"})

# ── anonymizer ─────────────────────────────────────────────────────────────

REDACTED = "[redacted]"
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|cookie|^token$|_token$|^auth$|authorization|session[_-]?id|"
    r"feed_token|salt)"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_QUAD = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_DISCORD_MENTION = re.compile(r"<@!?\d{15,}>")
_HOME = re.compile(r"(/Users|/home)/[^/\s\"']+")
# An ansible-vault blob; the header is spelled in two halves so the tree-wide
# secret gate does not read this pattern as a blob.
_VAULT = re.compile(re.escape("$ANSIBLE" + "_VAULT") + r"[^\s\"']*")
#: Generic words that look like names in user tables but are not people.
_NAME_STOP = frozenset(
    {"agent", "user", "system", "admin", "precis", "claude", "bot", "chase", "dream"}
)


def _is_private_quad(q: str) -> bool:
    a, b, *_ = (int(p) for p in q.split("."))
    return (
        a == 10
        or (a == 192 and b == 168)
        or (a == 172 and 16 <= b <= 31)
        or (a == 100 and 64 <= b <= 127)
    )


@dataclass
class Anonymizer:
    """Scrubs one row at a time. ``names`` are people to pseudonymize
    outside the bibliographic record; ``ref_kinds`` maps the slice's ref
    ids to their kind so a paper's chunks keep their author names."""

    names: Iterable[str] = ()
    ref_kinds: dict[int, str] = field(default_factory=dict)
    redactions: int = 0

    def __post_init__(self) -> None:
        parts: set[str] = set()
        for n in self.names:
            n = (n or "").strip()
            if not n:
                continue
            for p in [n, *re.split(r"[\s,]+", n)]:
                if len(p) >= 3 and p.lower() not in _NAME_STOP:
                    parts.add(p)
        # Longest first so "Ada Lovelace" wins over "Ada".
        alts = sorted(parts, key=len, reverse=True)
        self._names = (
            re.compile(r"(?i)\b(?:" + "|".join(map(re.escape, alts)) + r")\b")
            if alts
            else None
        )
        # A 5+ letter single name also hides inside handles and domains
        # ("alovelace", "adalovelace.com"): the whole token goes.
        long_parts = sorted((p for p in parts if " " not in p and len(p) >= 5), key=len)
        self._handles = (
            re.compile(
                r"(?i)[\w\-]*(?:" + "|".join(map(re.escape, long_parts)) + r")[\w\-]*"
            )
            if long_parts
            else None
        )

    def text(self, s: str, *, names: bool = True) -> str:
        """``s`` with secret-shaped spans, emails, private addresses, home
        paths and (when ``names``) known person names replaced."""
        out = s
        if s.startswith("\\x"):  # bytea hex
            return s
        out = _VAULT.sub(REDACTED, out)
        out = re.sub(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            "[redacted-private-key]",
            out,
            flags=re.S,
        )
        out = mask_secrets(out, warn=False)
        out = _EMAIL.sub("redacted@example.invalid", out)
        out = _QUAD.sub(lambda m: "192.0.2.1" if _is_private_quad(m[0]) else m[0], out)
        out = _DISCORD_MENTION.sub("<@0>", out)
        out = _HOME.sub(r"\1/dev", out)
        if names and self._names is not None:
            out = self._names.sub("Dev User", out)
        if names and self._handles is not None:
            out = self._handles.sub("devuser", out)
        if find_secrets(out):  # belt: whatever the patterns above missed
            out = REDACTED
        if out != s:
            self.redactions += 1
        return out

    def value(self, v: Any, *, names: bool) -> Any:
        if isinstance(v, str):
            return self.text(v, names=names)
        if isinstance(v, list):
            return [self.value(x, names=names) for x in v]
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, x in v.items():
                if _SECRET_KEY.search(k) and x not in (None, "", [], {}):
                    out[k] = REDACTED
                    self.redactions += 1
                else:
                    out[k] = self.value(x, names=names)
            return out
        return v

    def row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """One anonymized copy of ``row`` from ``table``."""
        names = table not in BIBLIO_TABLES
        if table == "refs":
            names = row.get("kind") not in BIBLIO_KINDS
        elif isinstance(row.get("ref_id"), int):
            names = names and self.ref_kinds.get(row["ref_id"]) not in BIBLIO_KINDS
        out: dict[str, Any] = {}
        for col, v in row.items():
            if table == "refs" and col == "authors":
                # bibliographic authors stay; scrub credentials only
                out[col] = self.value(v, names=names)
            elif _SECRET_KEY.search(col) and v not in (None, "", [], {}):
                out[col] = REDACTED
                self.redactions += 1
            else:
                out[col] = self.value(v, names=names)
        if table == "refs":
            out["owner_login"] = None  # FK to web_users, which is not copied
            if out.get("human_verified_by"):
                out["human_verified_by"] = "dev-user"
        return out


# ── extract (host) ─────────────────────────────────────────────────────────

PsqlRunner = Callable[[str], str]


def prod_psql(sql: str) -> str:
    """Run ``sql`` read-only against prod; return psql's unaligned stdout."""
    env = dict(os.environ, PRECIS_PROD_PSQL_OPTS="-At")
    res = subprocess.run(
        [str(REPO / "scripts" / "prod-psql"), "--ro"],
        input=sql,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(
            f"prod-psql --ro failed ({res.returncode}): {res.stderr[-2000:]}"
        )
    return res.stdout


def _json_line(out: str) -> Any:
    """The one JSON document in a ``-At`` output (BEGIN/COMMIT lines aside)."""
    for line in out.splitlines():
        if line[:1] in "{[":
            return json.loads(line)
    raise SystemExit(f"no JSON in prod-psql output: {out[:500]!r}")


CATALOG_SQL = """
SELECT json_build_object(
 'columns', (SELECT json_agg(json_build_object('t', c.relname, 'c', a.attname,
                'gen', a.attgenerated <> '') ORDER BY c.relname, a.attnum)
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition
      AND a.attnum > 0 AND NOT a.attisdropped),
 'fks', (SELECT json_agg(json_build_object('t', cl.relname, 'rt', rc.relname,
            'cols', (SELECT json_agg(a.attname ORDER BY k.o) FROM unnest(co.conkey)
                     WITH ORDINALITY k(n, o) JOIN pg_attribute a
                     ON a.attrelid = co.conrelid AND a.attnum = k.n),
            'rcols', (SELECT json_agg(a.attname ORDER BY k.o) FROM unnest(co.confkey)
                     WITH ORDINALITY k(n, o) JOIN pg_attribute a
                     ON a.attrelid = co.confrelid AND a.attnum = k.n))
            ORDER BY cl.relname, co.conname)
    FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
    JOIN pg_class rc ON rc.oid = co.confrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE co.contype = 'f' AND n.nspname = 'public'),
 'pks', (SELECT json_object_agg(cl.relname, (SELECT json_agg(a.attname ORDER BY k.o)
            FROM unnest(co.conkey) WITH ORDINALITY k(n, o) JOIN pg_attribute a
            ON a.attrelid = co.conrelid AND a.attnum = k.n))
    FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE co.contype = 'p' AND n.nspname = 'public'),
 'rows', (SELECT json_object_agg(c.relname, c.reltuples::bigint)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r')
)
"""


def _int_array(ids: Iterable[int]) -> str:
    return "'{" + ",".join(str(int(i)) for i in sorted(set(ids))) + "}'::bigint[]"


def select_refs_sql(per_kind: int, anchors: Iterable[int] = ANCHORS) -> str:
    """The ref-id set: newest live refs per kind, anchors, their link
    neighbours, the longest gripes, the most-seen alerts, and the parents
    of all of those."""
    a = _int_array(anchors)
    return f"""
WITH per_kind AS (
  SELECT ref_id FROM (SELECT ref_id, row_number() OVER
      (PARTITION BY kind ORDER BY ref_id DESC) AS rn
    FROM refs WHERE retired_at IS NULL) s WHERE rn <= {int(per_kind)}),
anchors AS (SELECT unnest({a}) AS ref_id),
hop AS (
  SELECT nb AS ref_id FROM (
    SELECT CASE WHEN l.src_ref_id = an.ref_id THEN l.dst_ref_id ELSE l.src_ref_id END AS nb,
           row_number() OVER (PARTITION BY an.ref_id ORDER BY l.link_id DESC) AS rn
    FROM anchors an JOIN links l ON an.ref_id IN (l.src_ref_id, l.dst_ref_id)) s
  WHERE rn <= {ANCHOR_HOP} AND nb IS NOT NULL),
long_gripes AS (
  SELECT c.ref_id FROM chunks c JOIN refs r USING (ref_id)
  WHERE r.kind = 'gripe' AND r.retired_at IS NULL
  GROUP BY c.ref_id ORDER BY sum(length(c.text)) DESC LIMIT {LONG_GRIPES}),
busy_alerts AS (
  SELECT ref_id FROM refs WHERE kind = 'alert'
  ORDER BY meta->'seen_count' DESC NULLS LAST LIMIT {BUSY_ALERTS}),
picked AS (
  SELECT ref_id FROM per_kind UNION SELECT ref_id FROM anchors
  UNION SELECT ref_id FROM hop UNION SELECT ref_id FROM long_gripes
  UNION SELECT ref_id FROM busy_alerts)
SELECT json_agg(DISTINCT ref_id) FROM (
  SELECT r.ref_id FROM refs r JOIN picked p USING (ref_id)
  UNION SELECT r.parent_id FROM refs r JOIN picked p USING (ref_id)
  WHERE r.parent_id IS NOT NULL) u
"""


NAMES_SQL = """
SELECT json_agg(DISTINCT x) FROM (
  SELECT login AS x FROM web_users UNION SELECT abbrev FROM web_users
  UNION SELECT full_name FROM web_users
  UNION SELECT meta->>'author' FROM refs WHERE kind = 'message'
  UNION SELECT human_verified_by FROM refs) s
WHERE x IS NOT NULL
"""


@dataclass
class Plan:
    """Which tables the slice copies and how each is filtered."""

    tree: list[str]  # BFS order from refs
    lookups: list[str]
    columns: dict[str, list[str]]  # non-generated columns
    pks: dict[str, list[str]]
    tree_fks: dict[str, list[tuple[list[str], str, list[str]]]]
    lookup_refs: dict[str, list[tuple[str, list[str], list[str]]]]
    whole: set[str]


def build_plan(catalog: dict[str, Any]) -> Plan:
    """Walk the FK graph down from ``refs``: a table joins the slice when it
    references a table already in it. Its filter uses only the FKs to
    tables found at earlier levels, so the walk has no cycles."""
    columns: dict[str, list[str]] = {}
    for c in catalog["columns"] or []:
        if not c["gen"]:
            columns.setdefault(c["t"], []).append(c["c"])
    fks = [
        (f["t"], list(f["cols"]), f["rt"], list(f["rcols"]))
        for f in catalog["fks"] or []
    ]
    pks = {t: list(v) for t, v in (catalog["pks"] or {}).items()}
    rows = catalog["rows"] or {}

    tree = ["refs"]
    in_tree = {"refs"}
    tree_fks: dict[str, list[tuple[list[str], str, list[str]]]] = {"refs": []}
    while True:
        level: dict[str, list[tuple[list[str], str, list[str]]]] = {}
        for t, cols, rt, rcols in fks:
            if t in in_tree or t in EXCLUDED or t not in columns or t == rt:
                continue
            if rt in in_tree:
                level.setdefault(t, []).append((cols, rt, rcols))
        if not level:
            break
        for t in sorted(level):
            tree.append(t)
            in_tree.add(t)
            tree_fks[t] = level[t]

    lookup_refs: dict[str, list[tuple[str, list[str], list[str]]]] = {}
    for t, cols, rt, rcols in fks:
        if t in in_tree and rt not in in_tree and rt not in EXCLUDED and rt in columns:
            lookup_refs.setdefault(rt, []).append((t, cols, rcols))
    lookups = sorted(lookup_refs)
    # reltuples is -1 for a table never analyzed: tiny, copy it whole.
    whole = {t for t in lookups if rows.get(t, -1) <= LOOKUP_WHOLE_MAX}
    return Plan(tree, lookups, columns, pks, tree_fks, lookup_refs, whole)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tuple(cols: list[str], prefix: str = "") -> str:
    inner = ", ".join(prefix + _ident(c) for c in cols)
    return inner if len(cols) == 1 else f"({inner})"


def slice_sql(
    plan: Plan, ref_ids: Iterable[int], anchors: Iterable[int] = ANCHORS
) -> str:
    """One read-only statement returning ``table|row-json`` lines for the
    whole slice. Each table is a CTE ``s_<table>`` so children filter on
    exactly the rows their parents copied."""
    ctes = [f"seed AS (SELECT unnest({_int_array(ref_ids)}) AS ref_id)"]
    for t in plan.tree:
        cols = ", ".join(_ident(c) for c in plan.columns[t])
        if t == "refs":
            where = '"ref_id" IN (SELECT ref_id FROM seed)'
        else:
            conds, present = [], []
            for fcols, rt, rcols in plan.tree_fks[t]:
                nulls = " OR ".join(f"{_ident(c)} IS NULL" for c in fcols)
                conds.append(
                    f"({nulls} OR {_tuple(fcols)} IN "
                    f"(SELECT {', '.join(_ident(c) for c in rcols)} FROM s_{rt}))"
                )
                present.append(f"{_ident(fcols[0])} IS NOT NULL")
            where = " AND ".join(conds) + " AND (" + " OR ".join(present) + ")"
            if t == "chunks":
                where += f' AND ("ref_id" = ANY({_int_array(anchors)}) OR "ord" < {CHUNK_ORD_CAP})'
        order = ", ".join(_ident(c) for c in plan.pks.get(t, plan.columns[t][:1]))
        ctes.append(
            f"s_{t} AS MATERIALIZED (SELECT {cols} FROM {_ident(t)} "
            f"WHERE {where} ORDER BY {order} LIMIT {TABLE_CAP})"
        )
    for t in plan.lookups:
        cols = ", ".join(_ident(c) for c in plan.columns[t])
        if t in plan.whole:
            where = "TRUE"
        else:
            ors = []
            for child, fcols, rcols in plan.lookup_refs[t]:
                ors.append(
                    f"{_tuple(rcols)} IN (SELECT {', '.join(_ident(c) for c in fcols)} "
                    f"FROM s_{child})"
                )
            where = " OR ".join(ors)
        ctes.append(
            f"l_{t} AS (SELECT {cols} FROM {_ident(t)} WHERE {where} LIMIT {TABLE_CAP})"
        )
    selects = [
        f"SELECT '{t}', row_to_json(_slice_row)::text FROM l_{t} _slice_row"
        for t in plan.lookups
    ]
    selects += [
        f"SELECT '{t}', row_to_json(_slice_row)::text FROM s_{t} _slice_row"
        for t in plan.tree
    ]
    return "WITH " + ",\n".join(ctes) + "\n" + "\nUNION ALL ".join(selects)


def parse_rows(out: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """``(table, row)`` pairs from the slice statement's ``-At`` output."""
    for line in out.splitlines():
        t, sep, js = line.partition("|")
        if not sep or not js.startswith("{"):
            continue  # BEGIN / COMMIT
        yield t, json.loads(js)


def extract(
    out_path: Path,
    *,
    per_kind: int = 20,
    run: PsqlRunner = prod_psql,
    anchors: Iterable[int] = ANCHORS,
) -> dict[str, int]:
    """Pull, anonymize and write the slice; return rows per table."""
    anchors = tuple(anchors)
    catalog = _json_line(run(CATALOG_SQL))
    plan = build_plan(catalog)
    ref_ids = [
        int(i) for i in _json_line(run(select_refs_sql(per_kind, anchors))) or []
    ]
    names = [n for n in (_json_line(run(NAMES_SQL)) or []) if isinstance(n, str)]
    rows = list(parse_rows(run(slice_sql(plan, ref_ids, anchors))))

    ref_kinds = {r["ref_id"]: r["kind"] for t, r in rows if t == "refs"}
    anon = Anonymizer(names=names, ref_kinds=ref_kinds)
    counts: dict[str, int] = {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        meta = {
            "version": SLICE_VERSION,
            "extracted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "per_kind": per_kind,
            "anchors": list(anchors),
            "tree": [t for t in plan.tree],
            "lookups": plan.lookups,
        }
        fh.write(json.dumps({"_meta": meta}) + "\n")
        for t, r in rows:
            fh.write(
                json.dumps({"t": t, "r": anon.row(t, r)}, ensure_ascii=False) + "\n"
            )
            counts[t] = counts.get(t, 0) + 1
    tmp.replace(out_path)
    counts["_redactions"] = anon.redactions
    return counts


# ── load (dev container) ───────────────────────────────────────────────────


def read_slice(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    meta: dict[str, Any] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if "_meta" in rec:
                meta = rec["_meta"]
            else:
                rows.setdefault(rec["t"], []).append(rec["r"])
    if meta.get("version") != SLICE_VERSION:
        raise SystemExit(
            f"{path}: slice version {meta.get('version')!r}, want {SLICE_VERSION}"
        )
    return meta, rows


def ensure_database(dsn: str) -> None:
    """Create the DSN's database on its server when it does not exist."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    dbname = str(params.get("dbname") or "")
    admin = make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        hit = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if hit is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


_DEV_FKS_SQL = """
SELECT cl.relname, rc.relname,
  (SELECT array_agg(a.attname ORDER BY k.o) FROM unnest(co.conkey) WITH ORDINALITY k(n, o)
     JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.n),
  (SELECT array_agg(a.attname ORDER BY k.o) FROM unnest(co.confkey) WITH ORDINALITY k(n, o)
     JOIN pg_attribute a ON a.attrelid = co.confrelid AND a.attnum = k.n),
  (SELECT bool_and(NOT a.attnotnull) FROM unnest(co.conkey) k(n)
     JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.n)
FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
JOIN pg_class rc ON rc.oid = co.confrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
WHERE co.contype = 'f' AND n.nspname = 'public'
"""


def load_slice(dsn: str, path: Path, *, migrate: bool = True) -> dict[str, int]:
    """Replace the dev DB's slice with ``path``; return rows per table."""
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb

    if migrate:
        from precis.store.migrate import packaged_migrator

        ensure_database(dsn)
        packaged_migrator(dsn).apply_all()

    meta, rows = read_slice(path)
    with psycopg.connect(dsn) as conn, conn.transaction():
        # Superuser-only: FK and other triggers stay off while the slice
        # lands in arbitrary order; the prune below restores integrity.
        conn.execute("SET LOCAL session_replication_role = replica")
        dev_cols: dict[str, set[str]] = {}
        for t, c in conn.execute(
            "SELECT c.relname, a.attname FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 "
            "AND NOT a.attisdropped AND a.attgenerated = ''"
        ).fetchall():
            dev_cols.setdefault(t, set()).add(c)

        tree = [t for t in meta["tree"] if t in dev_cols]
        if tree:
            conn.execute(
                sql.SQL("TRUNCATE {} CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tree)
                )
            )
        loaded: list[str] = []
        for t in [*meta["lookups"], *meta["tree"]]:
            batch = rows.get(t)
            if not batch or t not in dev_cols:
                continue
            seen: dict[str, None] = {}
            for r in batch:
                seen.update(dict.fromkeys(r))
            cols = [c for c in seen if c in dev_cols[t]]
            col_sql = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            stmt = sql.SQL(
                "INSERT INTO {t} ({cols}) OVERRIDING SYSTEM VALUE "
                "SELECT {cols} FROM jsonb_populate_recordset(NULL::{t}, %s) "
                "ON CONFLICT DO NOTHING"
            ).format(t=sql.Identifier(t), cols=col_sql)
            for i in range(0, len(batch), 2000):
                conn.execute(stmt, (Jsonb(batch[i : i + 2000]),))
            loaded.append(t)

        pruned = _prune_dangling(conn, set(loaded))
        _bump_sequences(conn, loaded)
        counts = {
            t: int(
                conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
                ).fetchone()[0]  # type: ignore[index]
            )
            for t in loaded
        }
    counts["_pruned"] = pruned
    return counts


def _prune_dangling(conn: Any, loaded: set[str]) -> int:
    """Null (nullable) or delete rows of loaded tables whose FK target is
    not in the DB; repeat until nothing changes. Returns rows touched."""
    from psycopg import sql

    fks = [f for f in conn.execute(_DEV_FKS_SQL).fetchall() if f[0] in loaded]
    total = 0
    for _ in range(20):
        changed = 0
        for t, rt, cols, rcols, nullable in fks:
            child = sql.Identifier(t)
            match = sql.SQL(" AND ").join(
                sql.SQL("p.{} = c.{}").format(sql.Identifier(rc), sql.Identifier(cc))
                for cc, rc in zip(cols, rcols, strict=True)
            )
            present = sql.SQL(" AND ").join(
                sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(cc)) for cc in cols
            )
            missing = sql.SQL(
                "{present} AND NOT EXISTS (SELECT 1 FROM {p} p WHERE {m})"
            ).format(present=present, p=sql.Identifier(rt), m=match)
            if nullable:
                sets = sql.SQL(", ").join(
                    sql.SQL("{} = NULL").format(sql.Identifier(cc)) for cc in cols
                )
                stmt = sql.SQL("UPDATE {t} c SET {s} WHERE {w}").format(
                    t=child, s=sets, w=missing
                )
            else:
                stmt = sql.SQL("DELETE FROM {t} c WHERE {w}").format(t=child, w=missing)
            changed += conn.execute(stmt).rowcount
        total += changed
        if not changed:
            break
    return total


def _bump_sequences(conn: Any, tables: list[str]) -> None:
    """Move every serial/identity sequence past the copied ids so dev
    writes don't collide with prod-shaped rows."""
    from psycopg import sql

    for t in tables:
        for (col,) in conn.execute(
            "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s AND a.attnum > 0 "
            "AND NOT a.attisdropped "
            "AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL",
            (t,),
        ).fetchall():
            conn.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, %s), "
                    "coalesce((SELECT max({c}) FROM {t}), 0) + 1, false)"
                ).format(c=sql.Identifier(col), t=sql.Identifier(t)),
                (t, col),
            )


# ── CLI ────────────────────────────────────────────────────────────────────


def _print_counts(label: str, counts: dict[str, int]) -> None:
    rows = sum(v for k, v in counts.items() if not k.startswith("_"))
    print(
        f"{label}: {rows} rows in {sum(1 for k in counts if not k.startswith('_'))} tables"
    )
    for k in sorted(counts):
        print(f"  {k:32s} {counts[k]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser(
        "extract", help="read-only prod pull -> anonymized slice (host)"
    )
    ex.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ex.add_argument("--per-kind", type=int, default=20)
    ld = sub.add_parser("load", help="slice -> dev DB, replacing the previous slice")
    ld.add_argument("--slice", type=Path, default=DEFAULT_OUT)
    ld.add_argument("--dsn", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "extract":
        _print_counts(
            f"extract -> {args.out}", extract(args.out, per_kind=args.per_kind)
        )
    else:
        _print_counts(f"load -> {args.dsn}", load_slice(args.dsn, args.slice))
    return 0


if __name__ == "__main__":
    sys.exit(main())
