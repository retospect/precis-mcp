"""``precis door`` — push a local file through a signed precis_web upload door.

``precis door pcb <file.json> --url <url>`` is the host half of the pcb
batch door (gr520323, :mod:`precis.batch_door`):

1. ``put(kind='pcb', id=<slug>, args={'op': 'batch_upload_link'})`` mints
   a single-use URL bound to the design;
2. this command checks the file the way the door and the put will (one
   strict JSON object, batch keys only, within the cap) so a bad file
   never spends the link, posts it with its sha256 in ``X-Content-SHA256``
   (the door refuses a body that hashes otherwise), checks the answered
   sha against its own and prints the put that applies the staging;
3. ``put(kind='pcb', id=<slug>, args={'batch_ref': '<sha256>'})``.

No database and no MCP connection: the link is the whole credential, so
this runs on any host that reaches precis_web. One POST, no retry — the
link is single-use, so a retry after a lost answer reads as a replay
(409); mint a fresh link instead.

Exit codes: 2 — the file or ``--url`` failed a local check (fix it; the
link is unspent); 3 — the door refused or could not be reached.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    import httpx

#: The file or ``--url`` failed a local check — nothing was sent.
EXIT_LOCAL = 2
#: The door refused, answered unexpectedly, or could not be reached.
EXIT_DOOR = 3

_FRESH_LINK = (
    "mint a fresh link: put(kind='pcb', id='<slug>', args={'op': 'batch_upload_link'})"
)

#: What fixes each door refusal (``precis_web.routes.files.upload_batch``).
_NEXT_BY_STATUS: dict[int, str] = {
    400: "re-run this command — the body did not hash to what was sent "
    "(the file changed while uploading?); a refused upload leaves the "
    "link unspent",
    403: f"the link is not valid for this door — {_FRESH_LINK}",
    404: "precis_web serves no batch door at this URL — check --url, or "
    "the deployed precis_web predates the door",
    409: f"the link was already used — {_FRESH_LINK}",
    410: f"the link expired — {_FRESH_LINK}",
    413: "write the batch compact — json.dumps(batch, separators=(',', ':')) "
    "— or split it across two puts",
    415: "the door did not read the body as one JSON object — check the "
    "file; a refused upload leaves the link unspent",
    503: "upload staging is not configured on precis_web — the operator "
    "sets PRECIS_UPLOAD_STAGING",
}


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``door`` and its per-kind subcommands (``pcb``)."""
    p = sub.add_parser(
        "door",
        help="Upload a file through a signed precis_web door (pcb batch JSON).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dsub = p.add_subparsers(dest="door_kind", required=True)
    pcb = dsub.add_parser(
        "pcb",
        help="Stage a pcb batch JSON for put(kind='pcb', args={'batch_ref': ...}).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pcb.add_argument(
        "path", help="The batch JSON file: one object, the put's batch args."
    )
    pcb.add_argument(
        "--url",
        required=True,
        help="The signed upload URL put(args={'op':'batch_upload_link'}) minted.",
    )
    pcb.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for the door's answer (default 60).",
    )
    pcb.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis door <kind>``."""
    if args.door_kind == "pcb":
        _door_pcb(args)


def _refuse(code: int, cause: str, next_step: str) -> NoReturn:
    print(f"precis door pcb: {cause}\nnext: {next_step}", file=sys.stderr)
    raise SystemExit(code)


def _post(url: str, data: bytes, sha: str, *, timeout: float) -> httpx.Response:
    """The one POST: raw body, the sha header, no redirect followed (it
    would carry the token and the body elsewhere) and no retry (the link
    is single-use)."""
    import httpx

    return httpx.post(
        url,
        content=data,
        headers={
            "Content-Type": "application/json",
            "X-Content-SHA256": sha,
            "User-Agent": "precis-door/1",
        },
        timeout=timeout,
        follow_redirects=False,
    )


def _detail(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200].strip() or "(no body)"
    if isinstance(body, dict) and body.get("detail") is not None:
        return str(body["detail"])
    return str(body)[:200]


def _door_pcb(args: argparse.Namespace) -> None:
    from precis import batch_door

    path = Path(args.path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        _refuse(
            EXIT_LOCAL,
            f"cannot read {args.path!r}: {exc.strerror or exc}",
            "pass the path of the batch JSON file",
        )
    cap = batch_door.MAX_BATCH_BYTES
    if len(data) > cap:
        _refuse(
            EXIT_LOCAL,
            f"{path.name} is {len(data)} bytes, over the door's {cap}-byte cap",
            _NEXT_BY_STATUS[413],
        )
    try:
        batch = batch_door.parse_batch_json(data)
    except ValueError as exc:
        _refuse(
            EXIT_LOCAL,
            f"{path.name} is {exc}",
            "write it as one JSON object — json.dump(board.to_batch(), f)",
        )
    refusal = batch_door.key_refusal(batch)
    if refusal is not None:
        cause, hint = refusal
        _refuse(EXIT_LOCAL, f"{path.name}: {cause}", hint)
    url = str(args.url).strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        _refuse(
            EXIT_LOCAL,
            "--url must be the absolute http(s) URL the link op minted",
            "prepend the precis_web origin to a path-only link, or " + _FRESH_LINK,
        )
    if "/files/upload_batch/" not in parts.path or "t=" not in parts.query:
        _refuse(
            EXIT_LOCAL,
            "--url is not a batch upload link (/files/upload_batch/<ref>?t=<token>)",
            _FRESH_LINK,
        )
    sha = hashlib.sha256(data).hexdigest()
    import httpx

    try:
        response = _post(url, data, sha, timeout=float(args.timeout))
    except httpx.HTTPError as exc:
        _refuse(
            EXIT_DOOR,
            f"could not reach the door ({type(exc).__name__}: {exc})",
            "check that this host reaches precis_web; if the upload may "
            "have landed, " + _FRESH_LINK,
        )
    if response.status_code != 200:
        _refuse(
            EXIT_DOOR,
            f"the door refused: HTTP {response.status_code} — {_detail(response)}",
            _NEXT_BY_STATUS.get(
                response.status_code,
                "check precis_web's logs; if the upload may have landed, "
                + _FRESH_LINK,
            ),
        )
    try:
        answer = response.json()
    except ValueError:
        answer = None
    staged = str(answer.get("sha256") or "") if isinstance(answer, dict) else ""
    if staged != sha:
        _refuse(
            EXIT_DOOR,
            f"the door staged sha256 {staged or '(none)'} but {path.name} "
            f"hashes to {sha}",
            "re-run with a fresh link — " + _FRESH_LINK,
        )
    assert isinstance(answer, dict)
    print(f"staged: {path.name} ({len(data)} bytes)")
    print(f"sha256: {sha}")
    print(f"next: {answer.get('next') or batch_door.put_call('<slug>', sha)}")
