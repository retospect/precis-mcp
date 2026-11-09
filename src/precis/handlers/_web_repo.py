"""Source-repository snapshots for the ``web`` kind (gr608719).

A ``get(kind='web', id=<github|gitlab repo root>)`` used to fetch the
rendered HTML page — which carries the README and nothing else, so a
claims-vs-code audit had no held source (every file path, class name
and licence contradiction cited nothing precis holds). A repo URL now
takes the archive path instead:

1. Resolve the head commit through the forge's REST API (one call
   when ``?ref=<sha|branch|tag>`` pins the revision, two otherwise —
   project lookup for the default branch, then that branch's head).
2. Stream the ``.tar.gz`` archive through
   :func:`~precis.utils.safe_fetch.safe_stream` — every redirect hop
   revalidated, a compressed-size cap, and an inflation guard so a
   tar bomb cannot balloon a bounded download into an unbounded
   decompress. No archive member is ever executed or written to disk;
   member bodies are read in memory only.
3. Hold the audit-relevant files (licences, READMEs, packaging and
   environment manifests, ``CITATION.cff``) as one chunk each —
   ``get(id='web:<id>~<path>')`` reads one file — plus a single
   ``~tree`` chunk listing every path with its size.

The resolved commit sha lands in ``cache_state.meta['commit_sha']``
and in the ref title, so a held snapshot names the exact revision it
was taken from.

Split out of ``web.py`` for the same reason ``_paper_toc`` exists:
the handler keeps the cache-flow plumbing, this module owns the
forge-specific grammar (URL shapes, API endpoints, member picking).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlsplit

from precis.errors import Upstream
from precis.store.types import ChunkInsert
from precis.utils.http import polite_safe_get
from precis.utils.safe_fetch import safe_stream

if TYPE_CHECKING:
    import httpx

    from precis.embedder import Embedder

log = logging.getLogger(__name__)


#: Compressed archive ceiling. Larger repos stay un-held: a truncated
#: tarball cannot be decompressed, so over-cap is a refusal, not a
#: partial result. 25 MiB covers every audit target seen so far
#: (MDCrow ~2 MB, gollum ~1 MB) with headroom for mid-size repos.
ARCHIVE_MAX_BYTES = 25 * 1024 * 1024

#: Inflated-content ceiling. The compressed cap alone does not bound
#: decompression — a hostile tarball can be 1000:1 — so inflated bytes
#: are counted through a guard and refused past this mark.
INFLATED_MAX_BYTES = 256 * 1024 * 1024

#: Per-file stored-text cap — the "size cap" of a ``~<path>`` read.
FILE_MAX_BYTES = 256 * 1024

#: Listing rows kept in the ``~tree`` chunk.
TREE_MAX_ENTRIES = 5000

#: Held-file count cap — a repo vending hundreds of matching names
#: (a monorepo of vendored manifests) holds the first few only.
HELD_MAX_FILES = 32

#: Marker landing in ``chunks.meta`` on every repo-produced block so
#: ``WebHandler._split_body_blocks`` keeps file chunks whole — a
#: ``~<path>`` read must return the file, not an 800-char slice.
REPO_BLOCK_META = "repo_block"


@dataclass(frozen=True)
class RepoTarget:
    """A forge repo-root URL parsed out of a canonical web key."""

    forge: str  # "github" | "gitlab"
    project: str  # "owner/repo" (github) or "group/sub/repo" (gitlab)
    ref: str | None  # explicit ?ref=<sha|branch|tag>; None = default branch

    @property
    def name(self) -> str:
        return self.project.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ResolvedRepo:
    """A repo pinned to a concrete commit + archive URL."""

    target: RepoTarget
    sha: str
    ref_label: str  # 'ref "v1.2.3"' or 'default branch "main"'
    archive_url: str


def parse_repo_url(key: str) -> RepoTarget | None:
    """``RepoTarget`` when the canonical key is a forge repo root.

    GitHub: exactly ``/<owner>/<repo>`` — deeper paths (``/tree/…``,
    ``/issues``) stay ordinary page fetches. GitLab: two or more
    segments with no ``-`` path marker (the separator GitLab inserts
    before ``/tree``, ``/-/blob`` etc.), so ``group/sub/repo`` counts.
    A trailing ``.git`` is stripped from the project. ``?ref=`` (kept
    by ``canonical_url`` — not a tracking param) pins the revision.
    """
    parts = urlsplit(key)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return None
    ref: str | None = None
    for k, v in parse_qsl(parts.query):
        if k == "ref" and v.strip():
            ref = v.strip()
            break
    if host == "github.com":
        if len(segs) != 2:
            return None
        forge = "github"
    elif host == "gitlab.com":
        # "-" is GitLab's reserved separator segment ("/-/tree/…").
        if len(segs) < 2 or "-" in segs:
            return None
        forge = "gitlab"
    else:
        return None
    project = "/".join(segs)
    if project.endswith(".git"):
        project = project[: -len(".git")]
    return RepoTarget(forge=forge, project=project, ref=ref)


def _api_json(client: httpx.Client, url: str, *, forge: str) -> Any:
    """One forge REST call through the polite SSRF path.

    ``None`` on 404 — the caller decides whether "not found" means
    "not a repo" (fall back to the page fetch) or "ref missing" (a
    typed refusal). Any other >=400 is an Upstream failure; 401/403
    and retries-exhausted surface through ``polite_safe_get``'s own
    ``VendorError``/``VendorUnavailable``, mapped at the call site.
    """
    resp = polite_safe_get(client, url, service=forge)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise Upstream(
            f"{forge} API answered HTTP {resp.status_code} for {url}",
            next="check the repository is public, then retry",
        )
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise Upstream(
            f"{forge} API returned non-JSON for {url}",
            next="retry later — the API may be serving an error page",
        ) from exc


def resolve_commit(client: httpx.Client, target: RepoTarget) -> ResolvedRepo | None:
    """Pin ``target`` to a commit sha + archive URL via the forge API.

    ``None`` when the *project* lookup 404s (not a repo, or private —
    the page path then gets its honest shot at the URL). A ``?ref=``
    that does not resolve, or a repo whose default branch's head
    cannot be read, is an :class:`Upstream` refusal instead: the
    caller asked for a snapshot, and silently serving the rendered
    page would misrepresent "README only" as repo content.
    """
    if target.forge == "github":
        base = f"https://api.github.com/repos/{target.project}"
        commits = f"{base}/commits"
        sha_key = "sha"
    else:
        base = f"https://gitlab.com/api/v4/projects/{quote(target.project, safe='')}"
        commits = f"{base}/repository/commits"
        sha_key = "id"

    if target.ref is not None:
        data = _api_json(
            client, f"{commits}/{quote(target.ref, safe='')}", forge=target.forge
        )
        if data is None:
            raise Upstream(
                f"{target.forge}: ref {target.ref!r} not found in {target.project}",
                next="check the ref (sha, branch or tag) and retry",
            )
        ref_label = f"ref {target.ref!r}"
    else:
        info = _api_json(client, base, forge=target.forge)
        if info is None:
            return None
        branch = info.get("default_branch") or "main"
        data = _api_json(
            client, f"{commits}/{quote(branch, safe='')}", forge=target.forge
        )
        if data is None:
            raise Upstream(
                f"{target.forge}: could not resolve default branch "
                f"{branch!r} of {target.project}",
                next="the repository may be empty; retry once it has commits",
            )
        ref_label = f"default branch {branch!r}"

    sha = data.get(sha_key) if isinstance(data, dict) else None
    if not isinstance(sha, str) or not sha:
        raise Upstream(
            f"{target.forge}: commit lookup for {target.project} returned no sha",
            next="retry later — the API response was unexpected",
        )
    if target.forge == "github":
        archive = f"https://codeload.github.com/{target.project}/tar.gz/{sha}"
    else:
        archive = (
            f"https://gitlab.com/{target.project}/-/archive/"
            f"{sha}/{target.name}-{sha}.tar.gz"
        )
    return ResolvedRepo(
        target=target, sha=sha, ref_label=ref_label, archive_url=archive
    )


def download_archive(client: httpx.Client, url: str) -> bytes:
    """Stream the tarball into memory, bounded by :data:`ARCHIVE_MAX_BYTES`.

    ``safe_stream`` revalidates each redirect hop against the SSRF
    blocklist; the size guard aborts mid-body rather than holding a
    truncated archive no decompressor could read.
    """
    buf = io.BytesIO()
    with safe_stream(client, "GET", url) as resp:
        if resp.status_code >= 400:
            raise Upstream(
                f"archive HTTP {resp.status_code} for {url}",
                next="check the repository is public, then retry",
            )
        for part in resp.iter_bytes(chunk_size=1 << 16):
            if buf.tell() + len(part) > ARCHIVE_MAX_BYTES:
                raise Upstream(
                    f"repository archive over the "
                    f"{ARCHIVE_MAX_BYTES // (1 << 20)} MiB cap for {url}",
                    next=(
                        "fetch single files by their raw URLs instead "
                        "(raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>)"
                    ),
                )
            buf.write(part)
    return buf.getvalue()


class _InflatedPastLimit(Exception):
    """Internal: the guarded reader produced more than the inflation cap."""


class _InflationGuard(io.RawIOBase):
    """Fileobj that counts decompressed bytes and refuses past a limit.

    tarfile's stream mode pulls member bodies through whatever fileobj
    it is given, so wrapping the gzip reader bounds the total inflated
    volume even though nothing is ever written to disk. The inner read
    is clamped to ``left + 1`` so a huge ``read(-1)`` cannot inflate
    the archive before the counter trips.
    """

    def __init__(self, inner: gzip.GzipFile, limit: int) -> None:
        self._inner = inner
        self._left = limit

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        take = self._left + 1 if n is None or n < 0 else min(n, self._left + 1)
        data = self._inner.read(take)
        self._left -= len(data)
        if self._left < 0:
            raise _InflatedPastLimit(
                f"archive inflates past {INFLATED_MAX_BYTES} bytes"
            )
        return data

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


#: Root-level basenames held verbatim — packaging + environment
#: manifests whose contents answer "how does this repo build/run".
_HELD_ROOT_EXACT = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "citation.cff",
        "package.json",
        "cargo.toml",
        "go.mod",
        "tox.ini",
        "noxfile.py",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
        "meta.yaml",
        "manifest.in",
    }
)

#: Root basenames held by prefix — licence and readme families come in
#: freeform suffix shapes (LICENSE-MIT, LICENSE.APACHE, README.rst,
#: COPYING.README, NOTICE.txt, …).
_HELD_ROOT_PREFIXES = ("license", "licence", "copying", "notice", "readme")

#: Manifest stems held only when they carry a plausible text suffix —
#: ``requirements*`` and ``environment*`` would otherwise also match a
#: stray ``environmental-impact.pdf``.
_HELD_MANIFEST_EXTS = ("txt", "in", "yml", "yaml", "toml")


def is_held_path(relpath: str) -> bool:
    """Whether an archive path (root dir already stripped) is held.

    Root level only, plus the ``requirements/`` directory — the files
    an audit actually cites (licence, readme, packaging, environment).
    Deeper vendored copies stay in the tree listing only.
    """
    base = relpath.rsplit("/", 1)[-1].lower()
    if "/" not in relpath:
        if base in _HELD_ROOT_EXACT:
            return True
        if base.startswith(_HELD_ROOT_PREFIXES):
            return True
        stem, _, ext = base.rpartition(".")
        return stem.startswith(("requirements", "environment")) and (
            ext in _HELD_MANIFEST_EXTS
        )
    head, _, tail = relpath.partition("/")
    return (
        "/" not in tail
        and head.lower() == "requirements"
        and base.endswith((".txt", ".in"))
    )


def _strip_root(member_name: str) -> str | None:
    """Drop the forge's top-level ``<repo>-<sha>/`` directory component."""
    _top, sep, rel = member_name.partition("/")
    if not sep or not rel:
        return None
    return rel


def _rank_held(path: str) -> tuple[int, str]:
    """Deterministic held-file order: licence family, README, CITATION,
    then packaging manifests — the order an audit wants to read them."""
    base = path.lower()
    if base.startswith(("license", "licence", "copying", "notice")):
        rank = 0
    elif base.startswith("readme"):
        rank = 1
    elif base == "citation.cff":
        rank = 2
    else:
        rank = 3
    return rank, base


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n / (1024 * 1024):.1f} MB"


def build_repo_blocks(
    tarball: bytes,
    *,
    embedder: Embedder | None,
) -> tuple[list[ChunkInsert], list[tuple[str, int]], list[str]]:
    """Slice a tarball into body chunks.

    Returns ``(blocks, listing, held_paths)``: one unsplit chunk per
    held file (slug = repo path) and a trailing ``~tree`` chunk with
    every regular file's path + size. The caller prepends its own
    summary block — it owns the forge wording and needs ``held_paths``
    to write it.

    Held file texts are embedded in one batch when an embedder is
    wired; on failure they land unembedded — the ``repo_block`` meta
    marker keeps them whole either way so ``~<path>`` reads return the
    file, not a slice. Lexical search works immediately; semantic hits
    arrive once the embed-fill cascade backfills.
    """
    files: list[tuple[str, int]] = []
    held: list[tuple[str, str, bool]] = []
    held_overflow = 0
    try:
        gz = gzip.GzipFile(fileobj=io.BytesIO(tarball))
        with tarfile.open(
            fileobj=_InflationGuard(gz, INFLATED_MAX_BYTES), mode="r|"
        ) as tf:
            for member in tf:
                rel = _strip_root(member.name)
                if rel is None or not member.isfile():
                    continue
                files.append((rel, member.size))
                if not is_held_path(rel):
                    continue
                if len(held) >= HELD_MAX_FILES:
                    held_overflow += 1
                    continue
                fh = tf.extractfile(member)
                data = fh.read(FILE_MAX_BYTES + 1) if fh is not None else b""
                truncated = len(data) > FILE_MAX_BYTES
                text = data[:FILE_MAX_BYTES].decode("utf-8", errors="replace")
                if truncated:
                    text += f"\n\n_(truncated at {FILE_MAX_BYTES} bytes)_"
                held.append((rel, text, truncated))
    except _InflatedPastLimit as exc:
        raise Upstream(
            f"repository archive inflates past "
            f"{INFLATED_MAX_BYTES // (1 << 20)} MiB — refusing it as a "
            "decompression bomb",
            next="fetch single files by their raw URLs instead",
        ) from exc
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise Upstream(
            f"repository archive is not a readable .tar.gz: {exc}",
            next="retry later, or fetch the files by their raw URLs",
        ) from exc

    held.sort(key=lambda h: _rank_held(h[0]))

    blocks: list[ChunkInsert] = []
    for rel, text, truncated in held:
        blocks.append(
            ChunkInsert(
                ord=len(blocks),
                slug=rel,
                text=text,
                meta={
                    REPO_BLOCK_META: True,
                    "repo_path": rel,
                    "repo_truncated": truncated,
                },
            )
        )

    listing = sorted(files)
    total_bytes = sum(size for _, size in listing)
    tree_lines = [
        f"# file listing — {len(listing)} file(s), {_human_size(total_bytes)} total"
    ]
    for path, size in listing[:TREE_MAX_ENTRIES]:
        tree_lines.append(f"{size:>12}  {path}")
    if len(listing) > TREE_MAX_ENTRIES:
        tree_lines.append(
            f"… +{len(listing) - TREE_MAX_ENTRIES} more "
            f"(listing capped at {TREE_MAX_ENTRIES})"
        )
    if held_overflow:
        tree_lines.append(
            f"… and {held_overflow} more holdable file(s) not kept "
            f"(held cap is {HELD_MAX_FILES})"
        )
    blocks.append(
        ChunkInsert(
            ord=len(blocks),
            slug="tree",
            text="\n".join(tree_lines),
            meta={REPO_BLOCK_META: True},
        )
    )

    if embedder is not None:
        try:
            vectors = embedder.embed([b.text for b in blocks])
        except Exception:
            log.warning(
                "repo snapshot stored un-embedded (worker will backfill)",
                exc_info=True,
            )
        else:
            for b, vec in zip(blocks, vectors, strict=True):
                b.embedding = vec

    return blocks, listing, [p for p, _, _ in held]


def content_sha256(tarball: bytes) -> str:
    """The fetch pin: sha256 of the raw archive bytes as served."""
    return hashlib.sha256(tarball).hexdigest()


__all__ = [
    "ARCHIVE_MAX_BYTES",
    "FILE_MAX_BYTES",
    "HELD_MAX_FILES",
    "INFLATED_MAX_BYTES",
    "REPO_BLOCK_META",
    "TREE_MAX_ENTRIES",
    "RepoTarget",
    "ResolvedRepo",
    "build_repo_blocks",
    "content_sha256",
    "download_archive",
    "is_held_path",
    "parse_repo_url",
    "resolve_commit",
]
