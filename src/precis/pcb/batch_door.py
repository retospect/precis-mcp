"""The pcb batch upload door (gr520323, ``docs/backlog/pcb-upload-door.md``).

A board script's ``Board.to_batch()`` for a 50-part board is ~36 KB of
JSON, and tool args are the wrong pipe for it: clients truncate,
sessions re-type, one-line payloads are unreviewable. The door is the
paper-PDF signed upload (3c44a411c) applied to that batch:

1. ``put(kind='pcb', id=<slug>, args={'op': 'batch_upload_link'})`` mints
   a single-use signed URL bound to the design's ref
   (:func:`upload_link_body`);
2. ``precis door pcb <file.json> --url <url>`` (or curl) posts the file to
   precis_web's ``/files/upload_batch/{ref_id}``, which checks type, size
   and the declared sha256 and stages ``<ref_id>-<sha>.json``;
3. ``put(kind='pcb', id=<slug>, args={'batch_ref': '<sha>'})`` reads the
   staging back (:func:`load_staged_batch`) and runs the batch put on it.

Why this shape:

* The door moves bytes; it never judges a board. Size, sha and the JSON
  gate run at the door AND again when the put reads the staging (it can
  be swept, swapped or truncated in between). The key set and every
  design rule stay with the put, so a door batch and the same batch
  inline meet one validation path and store identical rows.
* The staging binds to the design's ref (the precedent's foreign-ref
  rule), so a link needs an existing design and a new board is created
  empty first (``put(kind='pcb', id=<slug>, title=...)``). The link op
  creating it would hide a write inside a mint; a slug-bound token would
  be a second binding scheme beside the ref-bound one.
* A batch file carries only :data:`BATCH_KEYS` — what ``to_batch()``
  emits plus ``trace``/``run_job``. ``op`` keys, the deprecated
  ``autoplace`` alias and ``planes`` (not a put key; inline refuses it
  too) refuse naming the fix; ``title``/``tags`` ride the put call.
* No table, no ref kind: the staged file is the record and the daily
  sweep drops it after 24 h. The source-bundle programme
  (``docs/backlog/source-bundles.md``) adds immutable snapshots and a
  ``source`` stamp on the batch put beside this, under the same staging
  dir with its own token sentinel.

This module imports nothing heavy at load: ``precis door`` runs it on a
host that may have no database or optional extras.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from precis.errors import BadInput, Upstream

#: The batch door's size cap — the run door's per-file cap
#: (``precis.pcb.workspace.MAX_RUN_FILE_BYTES``): a run manifest's
#: ``board`` is this same batch, so a batch one door takes, the other
#: takes too. A 50-part board is ~36 KB.
MAX_BATCH_BYTES = 2_000_000

#: The keys a batch file may carry. Every one is a declared ``put``
#: args key (pinned by test), so the expanded batch reads through the
#: same ``ArgsView`` an inline batch does.
BATCH_KEYS: frozenset[str] = frozenset(
    {
        "components",
        "nets",
        "connections",
        "measures",
        "features",
        "footprints",
        "generators",
        "meta",
        "net_classes",
        "roles",
        "profile",
        "trace",
        "run_job",
    }
)

#: The mint op on ``put(kind='pcb', args={'op': ...})``.
LINK_OP = "batch_upload_link"


def link_call(slug: str) -> str:
    """The mint call — the ``next=`` of every refusal a re-upload fixes."""
    return f"put(kind='pcb', id='{slug}', args={{'op': '{LINK_OP}'}})"


def put_call(slug: str, sha: str = "<sha256>") -> str:
    """The batch put that reads a staged batch."""
    return f"put(kind='pcb', id='{slug}', args={{'batch_ref': '{sha}'}})"


def create_call(slug: str) -> str:
    """The empty-design put a new board starts with (the link needs a ref)."""
    return f"put(kind='pcb', id='{slug}', title='<title>')"


def is_sha256_hex(text: str) -> bool:
    """Whether ``text`` is a lowercase sha256 hexdigest."""
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def _no_constant(name: str) -> Any:
    raise ValueError(f"not strict JSON: {name} is not a JSON number")


def parse_batch_json(data: bytes) -> dict[str, Any]:
    """``data`` as one strict JSON object, or ``ValueError`` saying why.

    The one format gate the upload route (415), the ``batch_ref`` put
    (BadInput) and ``precis door pcb`` (before upload) share, so all three
    refuse the same bytes. Strict means UTF-8, RFC 8259 numbers (a
    ``NaN`` pose would otherwise store) and an object at the top; nesting
    too deep for the parser refuses rather than crashing.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not UTF-8 text (bad byte at offset {exc.start})") from None
    try:
        doc = json.loads(text, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"not JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from None
    except RecursionError:
        raise ValueError("not JSON this door reads: nested too deeply") from None
    if not isinstance(doc, dict):
        raise ValueError(f"a JSON {type(doc).__name__}, not an object")
    return doc


def key_refusal(keys: Iterable[str], *, slug: str = "<slug>") -> tuple[str, str] | None:
    """``None`` when every key is a batch key, else ``(cause, next)`` —
    shared by the put (BadInput) and ``precis door pcb`` (refused before
    the upload spends the link)."""
    unknown = sorted(set(keys) - BATCH_KEYS)
    if not unknown:
        return None
    cause = f"the batch carries keys a batch put does not take: {unknown}"
    if "planes" in unknown:
        return cause, (
            "drop 'planes' — plane assignments are not a put key (inline "
            "puts and the run import refuse them too); after the put, "
            f"put(kind='pcb', id='{slug}', args={{'op': 'plane_net', "
            "'layer': 'In1.Cu', 'net': 'GND'}) per plane"
        )
    return cause, (
        f"keep only {sorted(BATCH_KEYS)} in the file — ops and "
        f"title=/tags= ride their own call, e.g. {put_call(slug)}"
    )


def load_staged_batch(raw: Any, *, ref_id: int, slug: str) -> dict[str, Any]:
    """The batch staged as ``<ref_id>-<raw>.json``, re-checked, or BadInput.

    ``raw`` is ``args['batch_ref']``: the sha256 the upload answered. The
    staging must exist under THIS design's ref (a missing, swept or
    other-design sha all refuse alike — existence is the record), stay
    within :data:`MAX_BATCH_BYTES`, still hash to ``raw``, parse as one
    strict JSON object and carry only :data:`BATCH_KEYS`. Every refusal's
    ``next=`` is a call that works.
    """
    from precis.ingest import pdf_upload

    sha = raw.strip().lower() if isinstance(raw, str) else ""
    if not is_sha256_hex(sha):
        raise BadInput(
            f"args['batch_ref'] must be the sha256 the batch upload "
            f"answered, got {raw!r}",
            next=f"{link_call(slug)} for an upload link",
        )
    pdf_upload.require_uploads_dir()
    path = pdf_upload.staged_batch_path(sha, ref_id=ref_id)
    if path is None:
        raise BadInput(
            f"no staged batch {sha[:16]}… bound to {slug!r} — the sha comes "
            "from the upload's answer; a staging binds to the design its "
            "link named and is swept after 24 h",
            next=f"{link_call(slug)} for a fresh upload link",
        )
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BATCH_BYTES + 1)
    except OSError as exc:
        raise Upstream(
            f"staged batch {sha[:16]}… could not be read from upload staging",
            next=f"retry {put_call(slug, sha)}; if it persists, upload "
            f"again via {link_call(slug)}",
        ) from exc
    if len(data) > MAX_BATCH_BYTES:
        raise BadInput(
            f"staged batch {sha[:16]}… is over the {MAX_BATCH_BYTES}-byte cap",
            next="shrink the batch (compact JSON: separators=(',', ':')) and "
            f"upload it again via {link_call(slug)}",
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha:
        raise BadInput(
            f"staged batch {sha[:16]}… now hashes to {actual[:16]}… — the "
            "staging changed after upload",
            next=f"upload the batch again via {link_call(slug)}",
        )
    try:
        batch = parse_batch_json(data)
    except ValueError as exc:
        raise BadInput(
            f"staged batch {sha[:16]}… is {exc}",
            next=f"fix the file and upload it again via {link_call(slug)}",
        ) from None
    refusal = key_refusal(batch, slug=slug)
    if refusal is not None:
        cause, hint = refusal
        raise BadInput(cause, options=sorted(BATCH_KEYS), next=hint)
    return batch


def upload_link_body(store: Any, *, slug: str, ref_id: int) -> str:
    """Mint the single-use batch upload URL for ``ref_id`` and render the
    reply: the URL, its expiry, the host command, the curl fallback and
    the put that reads the staging."""
    from precis import file_links
    from precis import secrets as vault
    from precis.ingest import pdf_upload

    pdf_upload.require_uploads_dir()
    token = file_links.mint("put", file_links.UPLOAD_BATCH_SHA, ref_id, store=store)
    exp = file_links.parse(token).exp
    path = f"/files/upload_batch/{ref_id}?t={token}"
    base = (vault.get_secret(file_links.WEB_BASE_URL_SECRET, store=store) or "").rstrip(
        "/"
    )
    url = f"{base}{path}" if base else path
    expires = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    if not base:
        lines.append(
            "PRECIS_WEB_BASE_URL is not set — the link below is path-only; "
            "prepend the precis_web origin to POST to it (the operator sets "
            "it with `precis secret set PRECIS_WEB_BASE_URL` or on /secrets)."
        )
    lines += [
        f"# {slug} — batch upload link",
        f"url: {url}",
        f"expires: {expires} (single use — one upload per link)",
        f"max bytes: {MAX_BATCH_BYTES} · one JSON object, the batch put's "
        f"args (Board.to_batch()): {', '.join(sorted(BATCH_KEYS))}",
        "",
        f"uv run precis door pcb board.json --url '{url}'",
        "(or: curl -f --data-binary @board.json -H 'Content-Type: "
        f"application/json' '{url}')",
        "",
        "then put the sha256 it answers — the same judged put as the batch "
        "inline (pass title= to keep the design's title):",
        put_call(slug),
    ]
    return "\n".join(lines)


__all__ = [
    "BATCH_KEYS",
    "LINK_OP",
    "MAX_BATCH_BYTES",
    "create_call",
    "is_sha256_hex",
    "key_refusal",
    "link_call",
    "load_staged_batch",
    "parse_batch_json",
    "put_call",
    "upload_link_body",
]
