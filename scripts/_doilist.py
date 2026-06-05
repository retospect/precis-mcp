"""doilist — harvest DOIs from sources/, reconcile against precis-mcp,
queue the missing ones, and (optionally) slowly fetch their PDFs.

Usage (run from your triage workspace, e.g. doilist/):
    doilist scan                      # write dois_to_get.md
    doilist scan --download           # scan, then fetch one/min
    doilist download                  # just fetch from existing queue
    doilist download --interval 90    # custom seconds between fetches
    doilist recheck                   # re-clean + re-validate prior invalids
    doilist convert-doi-to-slugs DIR  # rewrite known DOIs to [slug] in DIR

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
# Same DOI shape, but anchored to a doi.org URL so the URL prefix can be
# consumed alongside the DOI when rewriting (avoids leaving a dangling
# `https://doi.org/[slug]` link).
URL_DOI_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s\"'<>)\]}]+)",
    re.IGNORECASE,
)
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
        print(
            "  ! psycopg not available; falling back to psql subprocess",
            file=sys.stderr,
        )
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
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        print(f"  ! psql failed: {res.stderr}", file=sys.stderr)
        return set()
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


# ---------- DOI validation ----------

# Errors that indicate the connection died mid-flight (TLS handshake
# timeouts, TCP resets, DNS hiccups, truncated JSON). These are worth
# retrying with backoff and MUST NOT poison the .doi_status.json cache —
# a transient network blip should not look like a permanent "invalid"
# verdict.
_TRANSIENT_EXCS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    ConnectionError,
)


def validate_doi(
    doi: str,
    timeout: float = 5.0,
    attempts: int = 2,
) -> bool | None:
    """Hit the doi.org handle API.

    Returns ``True`` if the handle resolves, ``False`` if doi.org
    returns 404, or ``None`` if every attempt died with a transport
    error. The caller MUST NOT cache ``None`` — it just means the
    network was uncooperative; the next scan should try again.
    """
    url = f"https://doi.org/api/handles/{urllib.parse.quote(doi, safe='/')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
                return data.get("responseCode") == 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            print(f"  ? validate {doi}: HTTP {e.code}", file=sys.stderr)
            return False
        except _TRANSIENT_EXCS as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (2**i))  # 1.5s, 3s, ...
    print(
        f"  ? validate {doi} (transient, gave up after {attempts}): {last_err}",
        file=sys.stderr,
    )
    return None


_RETRACT_TITLE_RE = re.compile(
    r"^\s*(?:retract(?:ed|ion)|withdrawn)\b[:\s]",
    re.IGNORECASE,
)


def _interpret_crossref_msg(msg: dict) -> str:
    """Map a Crossref ``message`` dict to ``valid``/``skip:retracted``."""
    if (msg.get("subtype") or "").lower() == "retraction":
        return "skip:retracted"
    for t in msg.get("title") or []:
        if _RETRACT_TITLE_RE.search(t or ""):
            return "skip:retracted"
    for upd in msg.get("update-to") or []:
        if (upd.get("type") or "").lower() == "retraction":
            return "skip:retracted"
    return "valid"


def classify_doi(
    doi: str,
    timeout: float = 8.0,
    attempts: int = 2,
) -> str | None:
    """Classify a DOI via Crossref + doi.org fallback.

    Returns one of ``"valid"``, ``"invalid"``, or ``"skip:retracted"``,
    or ``None`` if every Crossref + doi.org attempt hit a transport
    error (TLS handshake timeout, connection reset, DNS, etc). The
    caller MUST NOT cache ``None``; the next scan should retry.

    Retraction detection: Crossref title prefix ("RETRACTED ARTICLE",
    "Retracted:", "WITHDRAWN") is the primary signal;
    ``subtype=='retraction'`` (the notice itself) and ``update-to``
    retraction relations are also caught.
    """
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
    email = os.environ.get("UNPAYWALL_EMAIL", "doilist@example.invalid")
    req = urllib.request.Request(
        url, headers={"User-Agent": f"doilist/0.2 (mailto:{email})"}
    )

    last_err: Exception | None = None
    fallback = False  # set when Crossref gives up but doi.org might still know
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _interpret_crossref_msg(
                    json.loads(r.read()).get("message") or {}
                )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Crossref polite-pool throttle. Back off harder than the
                # transient retry — and keep the slot in the loop.
                last_err = e
                time.sleep(2.0 + 2.0 * i)
                continue
            if e.code == 404:
                fallback = True
                last_err = e
                break  # Crossref doesn't index this; defer to doi.org.
            print(f"  ? classify {doi}: HTTP {e.code}", file=sys.stderr)
            fallback = True
            last_err = e
            break
        except _TRANSIENT_EXCS as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (2**i))  # 1.5s, 3s, ...

    if fallback:
        fb = validate_doi(doi, timeout=timeout, attempts=attempts)
        if fb is True:
            return "valid"
        if fb is False:
            return "invalid"
        return None  # doi.org also unreachable; retry next run.

    print(
        f"  ? classify {doi} (transient, gave up after {attempts}): {last_err}",
        file=sys.stderr,
    )
    return None


# ---------- queue I/O ----------

QUEUE_HEADER = "# DOIs to fetch\n\nGenerated by `doilist`. One DOI per line.\n\n"


def read_queue() -> list[str]:
    if not QUEUE.exists():
        return []
    out = []
    for line in QUEUE.read_text().splitlines():
        line = line.strip()
        if line.startswith("- https://doi.org/"):
            tok = line[len("- https://doi.org/") :].split(None, 1)[0]
            if tok:
                out.append(tok)
    return out


# Map free-form annotation text -> canonical skip reason. First match wins.
# All matches case-insensitive; checked against the annotation text only.
_ANNOTATION_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bretract", re.I), "retracted"),
    (re.compile(r"\bwithdrawn\b", re.I), "retracted"),
    (re.compile(r"book\s*to\s*buy|purchase|to\s*buy", re.I), "purchase-required"),
    (re.compile(r"\bpaywall|expensive|\$\d+", re.I), "paywall"),
    (re.compile(r"abstract[-\s]?only|poster|meeting\s*abs", re.I), "abstract-only"),
    (
        re.compile(r"does\s*not\s*exist|not\s*real|404|not\s*found|missing", re.I),
        "not-found",
    ),
    (re.compile(r"out\s*of\s*scope|irrelevant|not\s*relevant", re.I), "out-of-scope"),
]


def classify_annotation(text: str) -> str:
    """Map a free-form annotation string to a canonical skip reason tag."""
    for pat, tag in _ANNOTATION_RULES:
        if pat.search(text):
            return tag
    # Fall back: slugify the annotation as the reason. Cap length so the JSON
    # stays readable.
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return slug or "annotated"


def read_queue_annotations() -> dict[str, str]:
    """Parse `dois_to_get.md` and return {doi_lc: skip_reason} for every line
    that has trailing text after the DOI URL. Empty / pure-DOI lines are
    ignored.
    """
    if not QUEUE.exists():
        return {}
    out: dict[str, str] = {}
    for line in QUEUE.read_text().splitlines():
        line = line.strip()
        if not line.startswith("- https://doi.org/"):
            continue
        rest = line[len("- https://doi.org/") :]
        parts = rest.split(None, 1)
        if len(parts) < 2:
            continue
        doi, annotation = parts[0], parts[1].strip(" -\t")
        if not annotation or not DOI_RE.fullmatch(doi):
            continue
        out[doi.lower()] = classify_annotation(annotation)
    return out


def write_queue(dois: list[str]) -> None:
    body = (
        QUEUE_HEADER
        + "\n".join(f"- https://doi.org/{d}" for d in sorted(set(dois)))
        + "\n"
    )
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
        return {
            k.lower(): v
            for k, v in data.get("dois", {}).items()
            if isinstance(v, str) and v
        }

    # Legacy migration
    state: dict[str, str] = {}
    if LEGACY_VALID.exists():
        for line in LEGACY_VALID.read_text().splitlines():
            line = line.strip()
            if line.startswith("- "):
                tok = line[2:].split(" ", 1)[0]
                if tok.startswith("https://doi.org/"):
                    tok = tok[len("https://doi.org/") :]
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
            "skip": sum(1 for v in state.values() if v.startswith("skip")),
        },
        "dois": dict(sorted(state.items())),
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n")


# ---------- download ----------


def unpaywall_pdf_url(doi: str, email: str, timeout: float = 15.0) -> str | None:
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/')}?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT.format(email=email)}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
    ) as e:
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
    req = urllib.request.Request(
        pdf_url, headers={"User-Agent": USER_AGENT.format(email=email)}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  ! fetch {doi}: {e}", file=sys.stderr)
        return None
    if not data.startswith(b"%PDF"):
        print(
            f"  ! fetch {doi}: not a PDF (got {len(data)} bytes, head={data[:8]!r})",
            file=sys.stderr,
        )
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


def _validate_many(
    dois: list[str],
    workers: int,
    state: dict[str, str],
) -> tuple[int, int, int, int]:
    """Validate a list of DOIs, mutating ``state`` in place.

    With ``workers > 1`` Crossref/doi.org tend to drop TLS handshakes,
    so the default is ``1`` (see ``cmd_scan``). Use a thread pool only
    when bumping ``--workers``.

    Transient transport errors (``classify_doi`` returns ``None``) are
    NOT cached — those DOIs stay unclassified so the next scan retries.

    Returns ``(newly_valid, newly_invalid, newly_skipped, transient)``.
    """
    if not dois:
        return 0, 0, 0, 0
    nv = ni = nr = nt = done = 0
    t0 = time.time()

    def _absorb(doi: str, status: str | None) -> None:
        nonlocal nv, ni, nr, nt
        if status is None:
            nt += 1
            return
        state[doi.lower()] = status
        if status == "valid":
            nv += 1
        elif status.startswith("skip"):
            nr += 1
        else:
            ni += 1

    if workers <= 1:
        # Serial path: one TLS connection at a time, friendly to doi.org.
        for doi in dois:
            try:
                status = classify_doi(doi)
            except Exception:
                status = None
            _absorb(doi, status)
            done += 1
            if done % 10 == 0 or done == len(dois):
                rate = done / max(time.time() - t0, 0.001)
                log(
                    f"  classified {done}/{len(dois)} ({rate:.1f}/s)  "
                    f"valid+={nv} invalid+={ni} retracted+={nr} transient={nt}"
                )
            if done % 25 == 0:
                write_state(state)  # checkpoint
        return nv, ni, nr, nt

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(classify_doi, d): d for d in dois}
        for fut in concurrent.futures.as_completed(futures):
            doi = futures[fut]
            done += 1
            try:
                status = fut.result()
            except Exception:
                status = None
            _absorb(doi, status)
            if done % 10 == 0 or done == len(dois):
                rate = done / max(time.time() - t0, 0.001)
                log(
                    f"  classified {done}/{len(dois)} ({rate:.1f}/s)  "
                    f"valid+={nv} invalid+={ni} retracted+={nr} transient={nt}"
                )
            if done % 25 == 0:
                write_state(state)  # checkpoint
    return nv, ni, nr, nt


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
    log(
        f"  precis has: {len(known)} identifier strings (DOIs, arxiv ids, S2, PubMed, ...)"
    )

    state = read_state()
    n_valid = sum(1 for v in state.values() if v == "valid")
    n_invalid = sum(1 for v in state.values() if v == "invalid")
    n_skip = sum(1 for v in state.values() if v.startswith("skip"))
    log(f"  cache: valid={n_valid} invalid={n_invalid} skip={n_skip}")

    if args.revalidate:
        log("  --revalidate: clearing cached 'invalid' entries")
        state = {k: v for k, v in state.items() if v != "invalid"}

    # Validate anything from sources we haven't classified yet (and isn't
    # already in precis — those are implicitly valid).
    candidates = sorted(
        {d for d in raw if d.lower() not in known and d.lower() not in state}
    )
    if args.no_validate:
        log(f"--no-validate: skipping doi.org check for {len(candidates)} DOI(s)")
    else:
        log(f"new candidates to validate: {len(candidates)}")
        if candidates:
            log(f"validating against doi.org ({args.workers} workers) ...")
            nv, ni, nr, nt = _validate_many(candidates, args.workers, state)
            log(f"  done. valid+={nv} invalid+={ni} retracted+={nr} transient={nt}")
            if nt:
                log(f"  ({nt} transient failure(s) NOT cached — re-run scan to retry)")

    # Harvest user-written annotations from the existing queue file before
    # we overwrite it. e.g. a line `- https://doi.org/10.x/y RETRACTED` flips
    # the DOI to `skip:retracted`. See classify_annotation() for tag mapping.
    annots = read_queue_annotations()
    if annots:
        flipped = 0
        for doi_lc, reason in annots.items():
            new_value = f"skip:{reason}"
            if state.get(doi_lc) != new_value:
                state[doi_lc] = new_value
                flipped += 1
        if flipped:
            log(f"  applied {flipped} annotation(s) from queue -> skip:*")

    write_state(state)

    # Queue = sources ∩ valid − precis. Rebuilt from scratch (annotations
    # are already absorbed above; everything else gets thrown away).
    #
    # With --no-validate the filter relaxes: include any DOI not in precis
    # whose state is either "valid" or unknown (i.e. unclassified). Cached
    # "invalid"/"skip:*" entries from prior runs still drop out.
    if args.no_validate:
        queue = sorted(
            {
                d
                for d in raw
                if d.lower() not in known and state.get(d.lower(), "valid") == "valid"
            }
        )
    else:
        queue = sorted(
            {
                d
                for d in raw
                if state.get(d.lower()) == "valid" and d.lower() not in known
            }
        )
    write_queue(queue)

    n_valid_final = sum(1 for v in state.values() if v == "valid")
    n_invalid_final = sum(1 for v in state.values() if v == "invalid")
    n_skip_final = sum(1 for v in state.values() if v.startswith("skip"))
    log(
        f"cache:  valid={n_valid_final} invalid={n_invalid_final} skip={n_skip_final} -> {STATE_FILE.name}"
    )
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
    candidates = sorted(
        {orig for cl, orig in re_cleaned.items() if state.get(cl) != "valid"}
    )
    log(f"  re-validating {len(candidates)} DOI(s) ...")
    nv, ni, nr, nt = _validate_many(candidates, args.workers, state)

    write_state(state)
    log(f"recheck done. valid+={nv} still-invalid={ni} retracted+={nr} transient={nt}")
    if nt:
        log(f"  ({nt} transient failure(s) NOT cached — re-run recheck to retry)")
    log("  (run `doilist scan` to refresh the queue)")


def cmd_skip(args: argparse.Namespace) -> None:
    """Mark DOIs as skip (retracted, paywalled, unobtainable, etc).

    Recommended reason tags (free-form, but be consistent):

      retracted          paper has been retracted (auto-detected by scan)
      purchase-required  book / paywalled article you don't intend to buy
      paywall            paywalled and you've decided to skip
      abstract-only      only an abstract / poster / meeting summary exists
      not-found          OA hunt failed, manual fetch impractical
      out-of-scope       valid DOI but not actually relevant to your work

    Skipped DOIs are removed from the queue on the next scan but kept in
    the state cache so they don't get re-validated. To un-skip, run
    ``./doilist unskip <doi>``.
    """
    state = read_state()
    reason = args.reason or "manual"
    value = f"skip:{reason}" if reason else "skip"
    changed = 0
    for doi in args.dois:
        cleaned = clean_doi(doi)
        if cleaned.startswith("https://doi.org/"):
            cleaned = cleaned[len("https://doi.org/") :]
        if not DOI_RE.fullmatch(cleaned):
            log(f"  ! not a DOI: {doi!r}")
            continue
        state[cleaned.lower()] = value
        log(f"  {cleaned} -> {value}")
        changed += 1
    if changed:
        write_state(state)
        log(f"marked {changed} DOI(s) as skip. Re-run `scan` to refresh queue.")


def cmd_unskip(args: argparse.Namespace) -> None:
    """Remove skip status from DOIs (they go back through normal flow)."""
    state = read_state()
    changed = 0
    for doi in args.dois:
        cleaned = clean_doi(doi)
        if cleaned.startswith("https://doi.org/"):
            cleaned = cleaned[len("https://doi.org/") :]
        key = cleaned.lower()
        if key in state and state[key].startswith("skip"):
            del state[key]
            log(f"  cleared skip on {cleaned}")
            changed += 1
        else:
            log(f"  no skip on {cleaned}")
    if changed:
        write_state(state)
        log(f"cleared {changed} skip(s). Re-run `scan` to revalidate.")


def cmd_download(args: argparse.Namespace) -> None:
    download_loop(args.interval)


# ---------- DOI -> slug rewrite ----------

DEFAULT_CONVERT_EXTS = ("md", "txt")


def precis_doi_to_slug_map() -> dict[str, str]:
    """Map every DOI (and synthesised arXiv DOI form) to its paper slug.

    One JOIN over ``ref_identifiers`` and ``refs`` for ``kind='paper'``,
    restricted to the two schemes that carry DOI-shaped values:

    * ``scheme='doi'``   - stored value is the canonical DOI.
    * ``scheme='arxiv'`` - stored value is the bare arXiv id; we also
      synthesise the ``10.48550/arxiv.<id>`` DOI form so source text
      that cites the arXiv DOI rather than the journal DOI still maps.

    Both keys and values are lowercased on insert in precis, so the
    returned dict is keyed by lowercased DOI and ready for case-
    insensitive lookup.
    """
    db_url = os.environ.get("PRECIS_DATABASE_URL", DEFAULT_DB)
    try:
        import psycopg  # type: ignore
    except ImportError:
        print(
            "  ! psycopg not available; falling back to psql subprocess",
            file=sys.stderr,
        )
        return _psql_doi_to_slug_map(db_url)
    out: dict[str, str] = {}
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pi.scheme, pi.value, r.slug "
            "FROM ref_identifiers pi "
            "JOIN refs r ON r.id = pi.ref_id "
            "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
            "  AND pi.scheme IN ('doi', 'arxiv')"
        )
        for scheme, value, slug in cur.fetchall():
            if not value or not slug:
                continue
            if scheme == "doi":
                out[value] = slug
            elif scheme == "arxiv":
                out[f"10.48550/arxiv.{value}"] = slug
    return out


def _psql_doi_to_slug_map(db_url: str) -> dict[str, str]:
    """``psql`` subprocess fallback when psycopg isn't importable."""
    import subprocess

    sql = (
        "SELECT pi.scheme || E'\t' || pi.value || E'\t' || r.slug "
        "FROM ref_identifiers pi "
        "JOIN refs r ON r.id = pi.ref_id "
        "WHERE r.kind='paper' AND r.deleted_at IS NULL "
        "  AND pi.scheme IN ('doi', 'arxiv')"
    )
    res = subprocess.run(
        ["psql", db_url, "-At", "-c", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        print(f"  ! psql failed: {res.stderr}", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.strip().split("\t", 2)
        if len(parts) != 3:
            continue
        scheme, value, slug = parts
        if scheme == "doi":
            out[value] = slug
        elif scheme == "arxiv":
            out[f"10.48550/arxiv.{value}"] = slug
    return out


def _preserved_tail(tail: str) -> str:
    """Return the prefix of ``tail`` that should be re-emitted after the slug.

    The DOI regex over-captures because its char class allows trailing
    formatting characters (``*``, backtick, ``.``, etc.). :func:`clean_doi`
    strips all of these from the canonical DOI, but a literal substitution
    with ``[slug]`` would lose them — so prose like ``**DOI:** 10.x/y``
    becomes ``**DOI: [slug]`` (unclosed bold) and ``...as in 10.x/y.``
    loses its sentence-final period.

    This helper restores the *typographic* tail (markdown bold/italic,
    backticks, footnote refs, one final punctuation char) while still
    dropping URL-suffix junk (``.full``, ``?utm=...``, ``...``, ellipsis).
    """
    if not tail:
        return ""
    out: list[str] = []
    i, n = 0, len(tail)
    # Markdown formatting closes (``**``, ``*``, backticks) and footnote
    # refs (``[^N]``), in any order, repeated.
    while i < n:
        c = tail[i]
        if c in "*`":
            j = i + 1
            while j < n and tail[j] == c:
                j += 1
            out.append(tail[i:j])
            i = j
            continue
        if c == "[" and i + 1 < n and tail[i + 1] == "^":
            close = tail.find("]", i + 2)
            if close == -1:
                # The DOI regex stops at ``]`` (it's in the exclude class),
                # so for ``...zsag065[^9].`` the regex match ends at ``[^9``
                # and the ``]`` is in the surrounding text. Re-emit the
                # ``[^N`` prefix; the ``]`` follows naturally from the source.
                out.append(tail[i:])
                i = n
                continue
            out.append(tail[i : close + 1])
            i = close + 1
            continue
        break
    # Anything still remaining is either a single sentence-punctuation char
    # to keep, or URL-suffix junk (``.full``, ``?utm=...``, ``..``) to drop.
    rest = tail[i:]
    if len(rest) == 1 and rest in ".,;:!?":
        out.append(rest)
    return "".join(out)


def _rewrite_dois_in_text(text: str, mapping: dict[str, str]) -> tuple[str, int, int]:
    """Replace known DOIs in ``text`` with ``[slug]``.

    URL-form DOIs (``https://doi.org/10.x/y``) are matched first so the
    URL prefix is consumed alongside the DOI when a slug is found.
    Bare DOIs are matched on a second pass. Unknown DOIs are left
    untouched (best-effort). Trailing markdown formatting and a single
    sentence-punctuation char are preserved via :func:`_preserved_tail`.

    Returns ``(new_text, replaced, seen)``.
    """
    counts = {"replaced": 0, "seen": 0}

    def _replace(match: re.Match[str], doi_group: int) -> str:
        counts["seen"] += 1
        raw = match.group(doi_group)
        cleaned = clean_doi(raw)
        slug = mapping.get(cleaned.lower())
        if slug is None:
            return match.group(0)
        counts["replaced"] += 1
        # raw == cleaned + tail since clean_doi only strips suffix chars.
        tail = raw[len(cleaned) :] if raw.startswith(cleaned) else ""
        return f"[{slug}]{_preserved_tail(tail)}"

    text = URL_DOI_RE.sub(lambda m: _replace(m, 1), text)
    text = DOI_RE.sub(lambda m: _replace(m, 0), text)
    return text, counts["replaced"], counts["seen"]


def cmd_convert_doi_to_slugs(args: argparse.Namespace) -> None:
    """Walk a directory and rewrite known DOIs to ``[slug]`` in text files.

    Best-effort: any DOI not in precis is left untouched. The slug
    map is fetched once via a single SQL JOIN and reused across all
    files.
    """
    target = Path(args.directory).resolve()
    if not target.is_dir():
        print(f"! not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    exts = {e.lstrip(".").lower() for e in (args.ext or list(DEFAULT_CONVERT_EXTS))}

    log("loading DOI -> slug map from precis ...")
    mapping = precis_doi_to_slug_map()
    log(f"  {len(mapping)} DOIs known")
    if not mapping:
        log("  (nothing to rewrite; aborting)")
        return

    log(f"scanning {target} (extensions: {sorted(exts)}) ...")
    files_seen = files_changed = total_replaced = total_seen = 0
    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lstrip(".").lower() not in exts:
            continue
        files_seen += 1
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(f"  ! could not read {p}: {e}")
            continue
        new_text, n_replaced, n_seen = _rewrite_dois_in_text(text, mapping)
        total_replaced += n_replaced
        total_seen += n_seen
        if n_replaced == 0:
            continue
        rel = p.relative_to(target)
        if args.dry_run:
            log(f"  {rel}: would replace {n_replaced}/{n_seen} DOI(s)")
        else:
            p.write_text(new_text, encoding="utf-8")
            log(f"  {rel}: replaced {n_replaced}/{n_seen} DOI(s)")
            files_changed += 1

    verb = "would change" if args.dry_run else "changed"
    log(
        f"done. files: {files_seen} scanned, {files_changed} {verb}. "
        f"DOIs: {total_replaced}/{total_seen} replaced."
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="extract DOIs, dedupe against precis, write queue")
    s.add_argument(
        "--download", action="store_true", help="after scanning, slowly fetch PDFs"
    )
    s.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between fetches (default 60)",
    )
    s.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel doi.org validators (default 1; bump only if "
        "you see no handshake timeouts)",
    )
    s.add_argument(
        "--revalidate",
        action="store_true",
        help="re-check DOIs previously logged as invalid",
    )
    s.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the doi.org/Crossref handshake entirely; queue "
        "every well-formed DOI not already in precis. Cached "
        "invalid/skip entries from prior runs still drop out.",
    )
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("download", help="fetch PDFs for queued DOIs, slowly")
    d.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between fetches (default 60)",
    )
    d.set_defaults(func=cmd_download)

    r = sub.add_parser(
        "recheck", help="re-clean and re-validate previously-invalid DOIs"
    )
    r.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel doi.org validators (default 1; bump only if "
        "you see no handshake timeouts)",
    )
    r.set_defaults(func=cmd_recheck)

    sk = sub.add_parser(
        "skip",
        help="mark DOI(s) as skip (retracted/paywalled/unavailable)",
        description="Mark DOIs to be excluded from the queue. Recommended "
        "reason tags: retracted, purchase-required, paywall, "
        "abstract-only, not-found, out-of-scope.",
    )
    sk.add_argument("dois", nargs="+", help="one or more DOIs (bare or as URLs)")
    sk.add_argument(
        "--reason",
        default=None,
        help="short tag, e.g. retracted, purchase-required, paywall",
    )
    sk.set_defaults(func=cmd_skip)

    us = sub.add_parser("unskip", help="clear skip status on DOI(s)")
    us.add_argument("dois", nargs="+")
    us.set_defaults(func=cmd_unskip)

    cv = sub.add_parser(
        "convert-doi-to-slugs",
        help="rewrite known DOIs to [slug] in text files (best-effort)",
        description=(
            "Walk a directory recursively, find DOI strings in text files, "
            "and replace each one that precis already knows with `[slug]`. "
            "Both URL-form (https://doi.org/10.x/y) and bare (10.x/y) "
            "DOIs are handled. arXiv DOI form (10.48550/arXiv.<id>) maps "
            "to the same slug as the bare arXiv id. Unknown DOIs are left "
            "untouched. One bulk SQL query fetches the slug map; files "
            "are rewritten in place unless --dry-run is given."
        ),
    )
    cv.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory to walk recursively (default: cwd)",
    )
    cv.add_argument(
        "--ext",
        action="append",
        default=None,
        help="file extension to include; repeatable. "
        f"Default: {', '.join(DEFAULT_CONVERT_EXTS)}.",
    )
    cv.add_argument(
        "--dry-run", action="store_true", help="report changes without writing"
    )
    cv.set_defaults(func=cmd_convert_doi_to_slugs)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
