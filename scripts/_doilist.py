"""doilist — harvest DOIs from sources/, reconcile against precis-mcp,
queue the missing ones, and (optionally) slowly fetch their PDFs.

Usage (run from your triage workspace, e.g. doilist/):
    doilist scan                      # write dois_to_get.md
    doilist scan --download           # scan, then fetch one/min
    doilist download                  # just fetch from existing queue
    doilist download --interval 90    # custom seconds between fetches
    doilist recheck                   # re-clean + re-validate prior invalids

Env:
    PRECIS_DATABASE_URL   default postgresql://acatome:acatome@127.0.0.1:5432/precis
    UNPAYWALL_EMAIL       required for --download (Unpaywall ToS)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# All paths resolve relative to cwd — run this from your triage workspace.
ROOT = Path.cwd()
SOURCES = ROOT / "sources"
QUEUE = ROOT / "dois_to_get.md"
DOWNLOADS = ROOT / "downloads"
STATE_FILE = ROOT / ".doi_status.json"  # {doi_lc: "valid"|"invalid"} — cache

# legacy files (migrated into STATE_FILE on first run)
LEGACY_INVALID = ROOT / "invalid_dois.md"
LEGACY_VALID = ROOT / ".valid_dois.md"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]}]+", re.IGNORECASE)
TRAILING_PUNCT = ".,;:)\u201d\u2019]"
# extra junk that commonly trails a DOI extracted from prose
TRAILING_JUNK_RE = re.compile(
    r"(?:`+|\*+|\[\^[^\]]*\]?|\.full(?:-text)?|\?[^\s]*|…|\.{2,})$"
)

DEFAULT_DB = "postgresql://acatome:acatome@127.0.0.1:5432/precis"
USER_AGENT = "doilist/0.1 (mailto:{email})"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- extraction ----------

def clean_doi(raw: str) -> str:
    """Strip trailing markdown / URL / formatting junk from an extracted DOI.

    Handles: code-spans (`), bold (**), markdown footnotes ([^…]), Frontiers
    .full suffixes, URL query strings (?…), ellipses, repeated periods, plus
    ordinary punctuation. Loops because a DOI often has multiple layers,
    e.g. `10.x/y**` -> `10.x/y` after one strip-` then strip-**.
    """
    s = raw.strip()
    prev = None
    while s and s != prev:
        prev = s
        s = TRAILING_JUNK_RE.sub("", s)
        while s and s[-1] in TRAILING_PUNCT:
            s = s[:-1]
    return s


def extract_from_text(text: str) -> set[str]:
    return {clean_doi(m.group(0)) for m in DOI_RE.finditer(text)}


def scan_dir(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log(f"  ! could not read {p}: {e}")
            continue
        hits = extract_from_text(text)
        if hits:
            log(f"  {p.relative_to(ROOT)}: {len(hits)} DOI(s)")
        found.update(hits)
    return found


# ---------- precis lookup ----------

def precis_known_identifiers() -> set[str]:
    """Every identifier string precis has indexed, across all schemes.

    Dumps raw ``value`` from ``ref_identifiers`` for every live paper
    ref, *regardless of scheme* — DOI, arXiv, S2 paperId, PubMed, MAG,
    DBLP, CorpusId, OpenAlex, PubMedCentral, pdf_hash. Values are
    already lowercased on insert.

    String equality is a reliable match across schemes because the
    forms don't collide: DOIs are ``10.x/y``, arXiv ids are ``N.N``
    dotted digits or ``category/NNNNNNN`` old-format, S2 paperIds are
    40-char hex, PubMed / MAG / CorpusId are pure digits, pdf_hash
    is 64-char hex. A source-text mention of any of these forms lands
    in the right bucket via simple membership test.

    Also synthesises the arXiv DOI form (``10.48550/arxiv.<id>``) per
    arxiv row. Post-enrichment most papers already carry the arXiv
    DOI as ``scheme='doi'``, but the synthesis is belt-and-braces for
    preprint-only papers whose S2 record returned only the arXiv
    externalIds entry.

    Replaces the legacy ``precis_known_dois()`` + ``meta->>'doi'``
    scan after migration ``0009_ref_identifiers``.
    """
    db_url = os.environ.get("PRECIS_DATABASE_URL", DEFAULT_DB)
    try:
        import psycopg  # type: ignore
    except ImportError:
        print("  ! psycopg not available; falling back to psql subprocess", file=sys.stderr)
        return _psql_known_identifiers(db_url)
    out: set[str] = set()
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        # Everything under kind='paper', no scheme filter. One scan
        # of the indexed table; synthesis happens in Python.
        cur.execute(
            "SELECT pi.scheme, pi.value FROM ref_identifiers pi "
            "JOIN refs r ON r.id = pi.ref_id "
            "WHERE r.kind = 'paper' AND r.deleted_at IS NULL"
        )
        for scheme, value in cur.fetchall():
            if not value:
                continue
            out.add(value)
            if scheme == "arxiv":
                out.add(f"10.48550/arxiv.{value}")
    return out


def _psql_known_identifiers(db_url: str) -> set[str]:
    """``psql`` subprocess fallback when psycopg isn't importable.

    Mirrors :func:`precis_known_identifiers`: every raw value under
    ``kind='paper'`` plus the synthesised arXiv DOI form.
    """
    import subprocess
    sql = (
        "SELECT pi.value FROM ref_identifiers pi "
        "JOIN refs r ON r.id = pi.ref_id "
        "WHERE r.kind='paper' AND r.deleted_at IS NULL "
        "UNION "
        "SELECT '10.48550/arxiv.' || pi.value FROM ref_identifiers pi "
        "JOIN refs r ON r.id = pi.ref_id "
        "WHERE r.kind='paper' AND r.deleted_at IS NULL "
        "AND pi.scheme = 'arxiv'"
    )
    res = subprocess.run(
        ["psql", db_url, "-At", "-c", sql],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(f"  ! psql failed: {res.stderr}", file=sys.stderr)
        return set()
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


# ---------- DOI validation ----------

def validate_doi(doi: str, timeout: float = 5.0) -> bool:
    """Hit the doi.org handle API. True iff the handle resolves."""
    url = f"https://doi.org/api/handles/{urllib.parse.quote(doi, safe='/')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            return data.get("responseCode") == 1
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        print(f"  ? validate {doi}: HTTP {e.code}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ? validate {doi}: {e}", file=sys.stderr)
        return False


# ---------- queue I/O ----------

QUEUE_HEADER = "# DOIs to fetch\n\nGenerated by `doilist`. One DOI per line.\n\n"


def read_queue() -> list[str]:
    if not QUEUE.exists():
        return []
    out = []
    for line in QUEUE.read_text().splitlines():
        line = line.strip()
        if line.startswith("- https://doi.org/"):
            out.append(line[len("- https://doi.org/"):])
    return out


def write_queue(dois: list[str]) -> None:
    body = QUEUE_HEADER + "\n".join(f"- https://doi.org/{d}" for d in sorted(set(dois))) + "\n"
    QUEUE.write_text(body)


STATE_HEADER = (
    "# Machine-readable DOI status. Keys are lowercased DOIs, values are\n"
    "# 'valid' or 'invalid' as last seen by doi.org. Regenerated by every\n"
    "# scan/recheck. Safe to delete — next scan rebuilds from scratch.\n"
)


def read_state() -> dict[str, str]:
    """Load the machine-readable state. Migrate legacy files on first run."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError as e:
            log(f"  ! state file corrupt ({e}); starting fresh")
            return {}
        return {k.lower(): v for k, v in data.get("dois", {}).items()
                if v in ("valid", "invalid")}

    # Legacy migration
    state: dict[str, str] = {}
    if LEGACY_VALID.exists():
        for line in LEGACY_VALID.read_text().splitlines():
            line = line.strip()
            if line.startswith("- "):
                tok = line[2:].split(" ", 1)[0]
                if tok.startswith("https://doi.org/"):
                    tok = tok[len("https://doi.org/"):]
                if DOI_RE.fullmatch(tok):
                    state[tok.lower()] = "valid"
    if LEGACY_INVALID.exists():
        for line in LEGACY_INVALID.read_text().splitlines():
            m = re.match(r"^- `([^`]+)`", line)
            if m:
                state[m.group(1).lower()] = "invalid"
    if state:
        log(f"  migrated {len(state)} DOIs from legacy ledger/invalid files")
    return state


def write_state(state: dict[str, str]) -> None:
    payload = {
        "_comment": STATE_HEADER.strip(),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "counts": {
            "valid": sum(1 for v in state.values() if v == "valid"),
            "invalid": sum(1 for v in state.values() if v == "invalid"),
        },
        "dois": dict(sorted(state.items())),
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n")


# ---------- download ----------

def unpaywall_pdf_url(doi: str, email: str, timeout: float = 15.0) -> str | None:
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/')}?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT.format(email=email)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ? unpaywall {doi}: {e}", file=sys.stderr)
        return None
    loc = data.get("best_oa_location") or {}
    return loc.get("url_for_pdf") or loc.get("url")


def slugify_doi(doi: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", doi.lower()).strip("_")


def fetch_pdf(doi: str, email: str) -> Path | None:
    DOWNLOADS.mkdir(exist_ok=True)
    target = DOWNLOADS / f"{slugify_doi(doi)}.pdf"
    if target.exists() and target.stat().st_size > 0:
        return target
    pdf_url = unpaywall_pdf_url(doi, email)
    if not pdf_url:
        return None
    req = urllib.request.Request(pdf_url, headers={"User-Agent": USER_AGENT.format(email=email)})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  ! fetch {doi}: {e}", file=sys.stderr)
        return None
    if not data.startswith(b"%PDF"):
        print(f"  ! fetch {doi}: not a PDF (got {len(data)} bytes, head={data[:8]!r})", file=sys.stderr)
        return None
    target.write_bytes(data)
    return target


def download_loop(interval: float) -> None:
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        print("UNPAYWALL_EMAIL not set; refusing to hit Unpaywall.", file=sys.stderr)
        sys.exit(2)
    queue = read_queue()
    if not queue:
        log("queue empty.")
        return
    log(f"downloading {len(queue)} DOI(s), one every {interval:.0f}s. Ctrl-C to stop.")
    fetched = 0
    skipped = 0
    missed = 0
    for i, doi in enumerate(queue, 1):
        target = DOWNLOADS / f"{slugify_doi(doi)}.pdf"
        if target.exists():
            log(f"[{i}/{len(queue)}] {doi} — already on disk")
            skipped += 1
            continue
        # only sleep before *actual* network calls, not skips
        if fetched + missed > 0:
            time.sleep(interval)
        log(f"[{i}/{len(queue)}] {doi} ...")
        path = fetch_pdf(doi, email)
        if path:
            log(f"  ok ({path.stat().st_size // 1024} KB) -> {path.relative_to(ROOT)}")
            fetched += 1
        else:
            log("  no OA copy")
            missed += 1
    log(f"done. fetched={fetched} skipped={skipped} missed={missed}")


# ---------- top-level ----------

def _validate_many(dois: list[str], workers: int, state: dict[str, str]) -> tuple[int, int]:
    """Validate a list of DOIs in parallel, mutating `state` in place.

    Returns (newly_valid, newly_invalid).
    """
    if not dois:
        return 0, 0
    nv = ni = done = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(validate_doi, d): d for d in dois}
        for fut in concurrent.futures.as_completed(futures):
            doi = futures[fut]
            done += 1
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if ok:
                state[doi.lower()] = "valid"
                nv += 1
            else:
                state[doi.lower()] = "invalid"
                ni += 1
            if done % 10 == 0 or done == len(dois):
                rate = done / max(time.time() - t0, 0.001)
                log(f"  validated {done}/{len(dois)} ({rate:.1f}/s)  "
                    f"valid+={nv} invalid+={ni}")
            if done % 25 == 0:
                write_state(state)  # checkpoint
    return nv, ni


def cmd_scan(args: argparse.Namespace) -> None:
    """For every DOI in sources/: validate at doi.org (if unknown), then
    write queue = {valid DOIs} − {DOIs already in precis}.

    The queue is regenerated from scratch every run. Hand-edits to
    `dois_to_get.md` do NOT persist — precis is the only source of truth
    for "handled". `.doi_status.json` is just a validation cache.
    """
    log("scanning sources/ ...")
    raw = scan_dir(SOURCES)
    log(f"  unique DOIs in sources/: {len(raw)}")

    log("loading known identifiers from precis (all schemes) ...")
    known = precis_known_identifiers()
    log(f"  precis has: {len(known)} identifier strings (DOIs, arxiv ids, S2, PubMed, ...)")

    state = read_state()
    n_valid = sum(1 for v in state.values() if v == "valid")
    n_invalid = sum(1 for v in state.values() if v == "invalid")
    log(f"  cache: valid={n_valid} invalid={n_invalid}")

    if args.revalidate:
        log("  --revalidate: clearing cached 'invalid' entries")
        state = {k: v for k, v in state.items() if v != "invalid"}

    # Validate anything from sources we haven't classified yet (and isn't
    # already in precis — those are implicitly valid).
    candidates = sorted({d for d in raw
                         if d.lower() not in known and d.lower() not in state})
    log(f"new candidates to validate: {len(candidates)}")
    if candidates:
        log(f"validating against doi.org ({args.workers} workers) ...")
        nv, ni = _validate_many(candidates, args.workers, state)
        log(f"  done. valid+={nv} invalid+={ni}")

    write_state(state)

    # Queue = sources ∩ valid − precis. Rebuilt from scratch.
    queue = sorted({d for d in raw
                    if state.get(d.lower()) == "valid"
                    and d.lower() not in known})
    write_queue(queue)

    n_valid_final = sum(1 for v in state.values() if v == "valid")
    n_invalid_final = sum(1 for v in state.values() if v == "invalid")
    log(f"cache:  valid={n_valid_final} invalid={n_invalid_final} -> {STATE_FILE.name}")
    log(f"queue:  {len(queue)} -> {QUEUE.name}")

    if args.download:
        download_loop(args.interval)


def cmd_recheck(args: argparse.Namespace) -> None:
    """Re-clean and re-validate every entry currently marked 'invalid'."""
    state = read_state()
    invalids = [d for d, v in state.items() if v == "invalid"]
    log(f"loaded {len(invalids)} invalid DOI(s) from state")
    if not invalids:
        return

    # Re-run the cleaner — it may have improved since these were stored.
    re_cleaned: dict[str, str] = {}  # cleaned_lc -> original_key
    dropped = 0
    for d in invalids:
        c = clean_doi(d)
        if not c or not DOI_RE.fullmatch(c):
            dropped += 1
            del state[d]
            continue
        cl = c.lower()
        if cl == d:
            re_cleaned[cl] = d
        else:
            # changed under cleanup — drop old key, retry clean form
            del state[d]
            re_cleaned[cl] = c
    log(f"  cleaned: {len(re_cleaned)} candidates (dropped {dropped} junk)")

    # Don't re-check anything already 'valid' under the cleaned form.
    candidates = sorted({orig for cl, orig in re_cleaned.items() if state.get(cl) != "valid"})
    log(f"  re-validating {len(candidates)} DOI(s) ...")
    nv, ni = _validate_many(candidates, args.workers, state)

    write_state(state)
    log(f"recheck done. valid+={nv} still-invalid={ni}")
    log("  (run `doilist scan` to refresh the queue)")


def cmd_download(args: argparse.Namespace) -> None:
    download_loop(args.interval)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="extract DOIs, dedupe against precis, write queue")
    s.add_argument("--download", action="store_true", help="after scanning, slowly fetch PDFs")
    s.add_argument("--interval", type=float, default=60.0, help="seconds between fetches (default 60)")
    s.add_argument("--workers", type=int, default=16, help="parallel doi.org validators (default 16)")
    s.add_argument("--revalidate", action="store_true",
                   help="re-check DOIs previously logged as invalid")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("download", help="fetch PDFs for queued DOIs, slowly")
    d.add_argument("--interval", type=float, default=60.0, help="seconds between fetches (default 60)")
    d.set_defaults(func=cmd_download)

    r = sub.add_parser("recheck", help="re-clean and re-validate previously-invalid DOIs")
    r.add_argument("--workers", type=int, default=16, help="parallel doi.org validators (default 16)")
    r.set_defaults(func=cmd_recheck)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
