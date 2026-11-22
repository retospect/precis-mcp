"""The factory git mirror — the land's credential boundary (gr659929, review td660513).

``scripts/ship``'s host-side steps run the branch's own code — the
checked-lane selector, ``uv run`` of the branch's environment — and no
process-table rule can tell that code's git calls from ship's own: the same
depth below the actual ship, the same inherited environment, forgeable
argv. The credential broker's depth-based ask authorization was therefore
unsound, and so is every file the host-side code can write: the mirror's
``config``, its ``hooks/``, the worktree gitdir's config, the main
checkout's — anything reachable by a same-uid adversary must be treated as
attacker input.

So three boundaries, not one:

1. **Ship holds no credential.** The lander keeps a *per-launch* bare
   mirror of the land refs — ``main``, ``prod`` and, for a release fix,
   the release target — under the main checkout's
   ``scratch/factory-mirror/<launch>.git``, refreshed from GitHub at each
   launch and handed to ship through env-config url-rewrite entries
   (:func:`mirror_ship_env`). Ship fetches, squashes and CAS-pushes
   exactly as before, against the mirror; a gate child's
   ``git credential fill`` has nothing to answer. Per-launch, never one
   shared mirror: a second land's populate must not force-refresh away
   the first's unsynced squash and leave its sync reporting "no land refs
   moved" over a sha origin never held (the review's third finding). A
   shared ``cache.git`` keeps the object store warm so each launch's
   GitHub fetch stays a delta. The launch tips that CAS the sync
   persist beside each mirror (``<launch>.tips.json``) — never in the
   branch-writable slice tree — so a worker restart's adopted handle
   still fails closed when the record is gone. But both it and the
   mirror stay same-uid-writable, so mirror mode itself persists
   outside this directory entirely: ``meta.factory_mirror`` on the
   todo, stamped and cleared by the lander (second pass — a deleted
   pair once read as "unmirrored" and the adopted land recorded a
   mirror-only squash as landed).

2. **Credential-bearing git reads nothing the adversary can write**
   (:func:`trusted_env`, :func:`_ops_repo`). Every call that touches the
   real origin under the token — the populate fetch, the sync push, the
   oracle ``ls-remote``, the branch push and the origin-state repair —
   runs in a fresh, lander-created gitdir with a random name:
   ``-c core.hooksPath=/dev/null`` (a planted ``hooks/pre-push`` in the
   mirror or the worktree gitdir never runs), plus
   ``GIT_CONFIG_NOSYSTEM`` and ``GIT_CONFIG_GLOBAL=/dev/null`` so a
   rewritten ``~/.gitconfig`` adds no ``url.insteadOf``, ``http.proxy``
   or helper — repo config would let a redirect fetch or a weakened TLS
   check move the token sideways. And no trusted read honors
   ``refs/replace/*`` or ``info/grafts``: ``--no-replace-objects`` rides
   every git argv (:data:`TRUSTED_ARGS`) and
   ``GIT_NO_REPLACE_OBJECTS`` every trusted env and every land-spawned
   child, because the mirror, the cache and the shared dir are all
   same-uid writable and a replacement planted for an oracle-verified
   sha would otherwise substitute the object a ``show`` or
   ``merge-base`` resolves — the drift helper's mirror ``git show``
   into code run under the token was the reported hole (review
   td660513, pre-land pass). The mirror itself is only ever
   operated on with unauthenticated *local* git (ref reads, the
   tmp→mirror fetch), because its config is adversary-writable; whatever
   the mirror then claims is verified against the **oracle** — an
   ``ls-remote`` run from a non-repo cwd with system/global config off,
   which sees only env-scoped config. Populate refuses a launch whose
   fetched tips do not match the oracle, and sync verifies the pushed
   sha is what ship reported, descends from the launch tip, and is what
   GitHub holds afterwards — a land record may never name a sha origin
   does not hold.

3. **Local ops never carry the token** (:func:`local_env`). Every op
   that touches only local state — the cache/common-dir fetches whose
   spawned upload-pack reads the peer's writable config, the fetches
   that copy verified refs out of an ops repo, and the primary checkout's
   ``status``/``reset``/``merge`` — runs credential-free, because a planted ``core.fsmonitor``, filter
   or ``packObjectsHook`` in the repo being repaired would otherwise
   execute with the token in its env (review td660513, second pass).

Ship's one non-transport shared-state write gets the same deferral as
its notify marks: a PATH-leading ``git`` shim (:func:`ship_git_shim`)
refuses its ``_sync_local_main`` shapes, and the lander replays the
local-main advance with :func:`advance_local_main` only after the sync
verifies — a phantom main survives a crashed repair, a refused call
does not (finding 4, second pass).

A worker that dies mid-write leaves 0-byte files in a mirror or its tips
record (gr762423). Every git call on a mirror therefore names it with
``--git-dir`` — never ``-C``, which for a mirror git no longer accepts
(a 0-byte ``HEAD``) walks up into the enclosing main checkout and
operates on that. Before use :func:`_init_bare` vets the mirror (non-empty
``HEAD``, ``rev-parse --git-dir`` accepts it, each present land ref
resolves to a commit) and renames a failing one aside to
``<name>.broken-<UTC stamp>`` — never deletes it — before initialising
afresh; :func:`read_launch_tips` treats an empty or unparseable record
as absent and sets it aside the same way. Each is logged as a warning
naming the path and the reason.

A crashed or raced land's unsynced squash lives only in that launch's
mirror: the next populate of the same launch force-updates the refs from
GitHub again — the objects stay (``gc.auto`` is off), the ref moves on,
because the slice branch is the source of truth and a relaunch
re-squashes it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git

log = logging.getLogger(__name__)

#: The mirror root under the main checkout's ``scratch/``: one
#: ``<launch>.git`` per launch plus the shared ``cache.git`` object store.
MIRROR_PARENT_NAME = "factory-mirror"
CACHE_DIR_NAME = "cache.git"

#: Refs every land mirrors: ship fetches both (main throughout, prod in its
#: release/drift bookkeeping) and pushes neither but the land target.
MIRROR_REFS = ("main", "prod")

#: Bounds for the populate fetch (~130 MiB of objects on first contact on
#: a fresh host, incremental after) and for one leased sync push.
POPULATE_TIMEOUT_S = 300.0
SYNC_TIMEOUT_S = 120.0

#: How long a launch waits for a sibling's vet/init/set-aside of the shared
#: cache (a few local git calls of at most 30 s each) before refusing.
CACHE_LOCK_TIMEOUT_S = 120.0

#: Prepended to every mirror/op git argv. The hooksPath override applies
#: to the invoking Git process. Local transports clear command-scope
#: config before spawning receive-pack, so its hooks still run on a push.
#: Internal mirror transfers fetch into the destination instead.
#:
#: ``--no-replace-objects`` blinds ``refs/replace/*`` and ``info/grafts``:
#: the mirror, the shared dir and the worktree gitdir are all same-uid
#: writable, so a replacement planted for an oracle-verified sha must
#: never substitute the object a trusted ``show``, ``merge-base`` or ref
#: read resolves (review td660513, pre-land pass — the drift helper's
#: mirror ``git show`` under the token was the reported hole). Git also
#: exports ``GIT_NO_REPLACE_OBJECTS=1`` to the children it spawns.
TRUSTED_ARGS: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "--no-replace-objects",
)

#: The origin URL may be pinned by the operator: the env wins over the
#: (adversary-writable) checkout config so a redirect cannot be planted.
ORIGIN_URL_ENV = "PRECIS_FACTORY_ORIGIN_URL"

#: The token is https/github.com-scoped; any other scheme or host is a
#: redirect (an ``ssh`` URL would reach for ``SSH_AUTH_SOCK`` instead).
_ORIGIN_URL_RE = re.compile(r"https://github\.com/\S+")

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def mirror_root(root: Path) -> Path:
    """``<main checkout>/scratch/factory-mirror/`` — outside every slice
    tree; its contents are adversary-writable by design (same-uid gate
    code), so no credential op ever trusts config read from it."""
    return root / "scratch" / MIRROR_PARENT_NAME


def cache_dir(root: Path) -> Path:
    """The shared object/ref cache the launch mirrors seed from."""
    return mirror_root(root) / CACHE_DIR_NAME


def mirror_dir(root: Path, launch_id: str) -> Path:
    """This launch's private mirror — per-launch state, so a sibling
    land's populate can never refresh away an unsynced squash."""
    return mirror_root(root) / f"{launch_id}.git"


def launch_tips_path(root: Path, launch_id: str) -> Path:
    """This launch's mirror-mode record — the ``{ref: sha}`` launch tips
    that are the sync's CAS basis, persisted NEXT TO the launch mirror
    under the main checkout's ``scratch/``. Never the slice tree's own
    ``scratch/``: that is branch-writable, and a record disappearing
    there let an adopted handle read "no mirror" and poll record a
    mirror-only squash as landed (review td660513)."""
    return mirror_root(root) / f"{launch_id}.tips.json"


def write_launch_tips(root: Path, launch_id: str, tips: Mapping[str, str]) -> bool:
    """Atomically persist the launch tips beside the launch mirror — the
    record an adopted handle's sync reads back, its rebuilt handle
    carrying none. ``False`` when it cannot be written; the caller fails
    the launch rather than risk a ship whose land syncs blind."""
    path = launch_tips_path(root, launch_id)
    tmp = path.parent / (path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(dict(tips), sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def read_launch_tips(root: Path, launch_id: str) -> dict[str, str] | None:
    """The persisted launch tips. ``None`` when no record exists — the
    launch was not mirrored — or when the file is empty or not a JSON
    object: a record a dying worker left half-written is no record, and
    is renamed aside (``<name>.broken-<UTC stamp>``, never deleted) so it
    cannot keep reading as one (gr762423). A dict — possibly empty — when
    a record parses, and ``{}`` when the file cannot be read at all, so
    an unreadable record fails the caller closed."""
    path = launch_tips_path(root, launch_id)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    raw: object = None
    reason = "empty record"
    if text.strip():
        try:
            raw = json.loads(text)
        except ValueError:
            reason = "unparseable record"
        else:
            reason = "record is not a JSON object"
    if not isinstance(raw, dict):
        try:
            _set_aside(path, reason)
        except OSError as exc:
            log.warning("factory tips record %s is corrupt (%s): %s", path, reason, exc)
        return None
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and _SHA_RE.fullmatch(v)
    }


def has_broken_tips(root: Path, launch_id: str) -> bool:
    """Whether a corrupt launch record was set aside for this launch
    (``<launch>.tips.json.broken-*``, see :func:`read_launch_tips`) — the
    evidence that the launch ran mirrored though its record now reads as
    absent."""
    prefix = launch_tips_path(root, launch_id).name + ".broken-"
    try:
        return any(p.name.startswith(prefix) for p in mirror_root(root).iterdir())
    except OSError:
        return False


def drop_launch(root: Path, launch_id: str) -> None:
    """Forget this launch's mirror-mode record and mirror — after a
    verified sync, and before a token-less launch that must not read a
    predecessor's state as its own. Best-effort.

    A ``<launch>.tips.json.broken-*`` sibling (:func:`has_broken_tips`) is
    evidence of a mirrored launch, so it is retired with the rest: renamed
    to ``<launch>.tips.json.dropped-<UTC stamp>`` — which
    :func:`has_broken_tips` does not match — never deleted."""
    launch_tips_path(root, launch_id).unlink(missing_ok=True)
    shutil.rmtree(mirror_dir(root, launch_id), ignore_errors=True)
    prefix = launch_tips_path(root, launch_id).name + ".broken-"
    try:
        broken = [p for p in mirror_root(root).iterdir() if p.name.startswith(prefix)]
    except OSError:
        return
    for path in broken:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = path.name.replace(".broken-", ".dropped-", 1)
        target = path.with_name(base)
        n = 1
        while target.exists():
            target = path.with_name(f"{base}-{stamp}-{n}")
            n += 1
        try:
            path.rename(target)
        except OSError as exc:
            log.warning("factory tips record %s not retired: %s", path, exc)


def land_refs(onto: str | None) -> tuple[str, ...]:
    """The refs a launch mirrors: the standing set plus the release
    target when this land ships ``--onto release/r<N>``."""
    refs = list(MIRROR_REFS)
    if onto and onto not in refs:
        refs.append(onto)
    return tuple(refs)


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """The git seam — tests monkeypatch this; never raises on non-zero."""
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


class _RefreshFailure(OSError):
    """An already sanitized git step failure; preserve its step when wrapping."""


def refresh_error(exc: OSError | subprocess.TimeoutExpired) -> str:
    """Sanitized diagnostic for a refresh syscall, including timeout stderr."""
    if isinstance(exc, _RefreshFailure):
        return str(exc)
    stderr = getattr(exc, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    detail = stderr or (
        f"timed out after {exc.timeout}s"
        if isinstance(exc, subprocess.TimeoutExpired)
        else str(exc)
    )
    return f"{type(exc).__name__}: {mask_secrets(detail)[-300:]}".replace("\n", " | ")


def _refresh_run(
    args: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Retain the git step when a refresh process cannot complete."""
    try:
        return _run(args, cwd=cwd, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Never include argv: transport URLs may contain credentials.
        step = next(
            (s for s in ("init", "config", "fetch", "push", "rev-parse") if s in args),
            "git",
        )
        raise _RefreshFailure(f"{step}: {refresh_error(exc)}") from None


def trusted_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` plus the file-config kill set for credential-bearing git:
    ``GIT_CONFIG_NOSYSTEM`` and ``GIT_CONFIG_GLOBAL=/dev/null`` — a
    rewritten ``~/.gitconfig`` or ``/etc/gitconfig`` adds nothing
    (``url.insteadOf`` redirects, ``http.*`` TLS weakening, helpers).
    Command-scope ``GIT_CONFIG_*`` entries — the credential helper —
    are unaffected, and local repo config is avoided outright by
    running those calls in a fresh :func:`_ops_repo` gitdir.

    ``GIT_NO_REPLACE_OBJECTS=1`` joins the kill set too — the env half of
    :data:`TRUSTED_ARGS`' ``--no-replace-objects``, kept here for spawned
    children (a local-path fetch's ``upload-pack``) and any caller that
    forgets the argv flag: a planted ``refs/replace/*`` must never
    substitute the object a trusted read resolves."""
    out = dict(env)
    out["GIT_CONFIG_NOSYSTEM"] = "1"
    out["GIT_CONFIG_GLOBAL"] = os.devnull
    out["GIT_NO_REPLACE_OBJECTS"] = "1"
    return out


#: Env names carrying credential material — the factory token itself, its
#: ``gh`` shadow, and every command-scope config entry (the credential
#: helper :func:`precis.workers.factory_git.git_auth_env` wires is an
#: env-config pair; a repair op needs none of it). LOCAL repair ops drop
#: them (review td660513, second pass): ``git status`` or ``reset --hard``
#: in the shared checkout, or a fetch from a local path repo, spawns
#: whatever a planted ``core.fsmonitor``, clean/smudge filter or
#: ``uploadpack.packObjectsHook`` the LOCAL config names — config the
#: kill set cannot blind, because it belongs to the repo being repaired —
#: and every spawned child inherits the env, so an env holding the token
#: hands it to branch code.
_CREDENTIAL_ENV_RE = re.compile(
    rf"^(?:{re.escape(_factory_git.SECRET_NAME)}|GH_TOKEN|GIT_CONFIG_.*)$"
)


def local_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` minus every credential-carrying name, under the same
    file-config kill set as :func:`trusted_env`: the env for ops that
    touch only local state — the shared dir's refs, the primary
    checkout's status/reset, a local-path fetch's spawned upload-pack.
    Their spawned children read adversary-planted repo config but
    inherit nothing to steal. Idempotent."""
    out = {k: v for k, v in env.items() if not _CREDENTIAL_ENV_RE.fullmatch(k)}
    return trusted_env(out)


def _local_fetch_args(
    dest: Path, ops: Path, *refspecs: str, bare: bool = False
) -> list[str]:
    """Copy explicit refs from a fresh ops repo without invoking receive-pack.
    ``bare`` marks ``dest`` as a bare gitdir (a mirror) addressed with
    ``--git-dir`` — never ``-C``, which would walk up out of a broken one
    into the enclosing checkout (gr762423)."""
    return [
        "git",
        *TRUSTED_ARGS,
        *(("--git-dir", str(dest)) if bare else ("-C", str(dest))),
        "fetch",
        "--quiet",
        "--atomic",
        "--no-tags",
        "--no-prune",
        "--no-prune-tags",
        "--refmap=",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        "--",
        str(ops),
        *refspecs,
    ]


def _stamped_aside(path: Path) -> Path:
    """``<name>.broken-<UTC stamp>`` beside ``path`` — a name not yet taken."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside = path.with_name(f"{path.name}.broken-{stamp}")
    n = 1
    while aside.exists() or aside.is_symlink():
        aside = path.with_name(f"{path.name}.broken-{stamp}-{n}")
        n += 1
    return aside


def _set_aside(path: Path, reason: str) -> Path:
    """Rename a corrupt mirror file or dir to ``<name>.broken-<UTC stamp>``
    — never deleted, so the wreckage stays for a post-mortem — and warn
    naming it and the reason. Raises :class:`OSError` when the rename fails."""
    aside = _stamped_aside(path)
    path.rename(aside)
    log.warning(
        "factory mirror %s is corrupt (%s); set aside as %s", path, reason, aside.name
    )
    return aside


def _ref_present(path: Path, ref: str) -> bool:
    """Whether the bare repo at ``path`` carries ``refs/heads/<ref>`` — as a
    loose file (a 0-byte one included: that is the corruption to catch) or
    a packed-refs row."""
    if (path / "refs" / "heads" / ref).exists():
        return True
    try:
        packed = (path / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(
        line.rpartition(" ")[2].strip() == f"refs/heads/{ref}"
        for line in packed.splitlines()
    )


def _bare_defect(
    path: Path, refs: tuple[str, ...], *, env: Mapping[str, str]
) -> str | None:
    """Why the bare repo at ``path`` cannot be used — an empty ``HEAD``, a
    ``git --git-dir`` that does not accept it, a land ref that is present
    but does not resolve to a commit — or ``None`` when it is sound. Every
    call names the gitdir with ``--git-dir``: a broken mirror under the
    main checkout must never resolve to the enclosing repo (gr762423).
    Only called for a path whose ``HEAD`` exists."""
    try:
        if (path / "HEAD").stat().st_size == 0:
            return "HEAD is empty"
    except OSError as exc:
        return f"HEAD unreadable: {refresh_error(exc)}"
    cp = _refresh_run(
        ["git", *TRUSTED_ARGS, "--git-dir", str(path), "rev-parse", "--git-dir"],
        cwd=path.parent,
        env=trusted_env(env),
        timeout=30.0,
    )
    if cp.returncode != 0:
        return f"not a git repository (rev-parse --git-dir rc={cp.returncode}: {_tail(cp)})"
    for ref in refs:
        if not _ref_present(path, ref):
            continue
        cp = _refresh_run(
            [
                "git",
                *TRUSTED_ARGS,
                "--git-dir",
                str(path),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{ref}^{{commit}}",
            ],
            cwd=path.parent,
            env=trusted_env(env),
            timeout=30.0,
        )
        if cp.returncode != 0:
            return f"refs/heads/{ref} does not resolve to a commit"
    return None


@contextlib.contextmanager
def _cache_lock(cache: Path) -> Iterator[None]:
    """Exclusive ``flock`` on ``<cache>.lock`` (a sibling file, created on
    first use and left in place) for the span of one vet/init/set-aside of
    the shared cache. The lock is polled with ``LOCK_NB`` for at most
    :data:`CACHE_LOCK_TIMEOUT_S`. Raises :class:`OSError` when the lock
    file cannot be opened, and :class:`TimeoutError` (an ``OSError``) when
    a sibling launch still holds it at the deadline."""
    lock = cache.with_name(cache.name + ".lock")
    with lock.open("a", encoding="utf-8") as fh:
        deadline = time.monotonic() + CACHE_LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{lock.name} still held by another launch after "
                        f"{CACHE_LOCK_TIMEOUT_S:.0f}s"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _init_bare(
    path: Path,
    *,
    env: Mapping[str, str],
    why: list[str] | None = None,
    refs: tuple[str, ...] = MIRROR_REFS,
) -> bool:
    """``git init --bare`` (idempotent) plus ``gc.auto 0``: objects a
    later force-refresh unrefs must stay readable for a recovering
    sync's explicit shas.

    An existing repo is vetted first (:func:`_bare_defect`): one that is
    unusable — a 0-byte ``HEAD`` left by a worker that died mid-write, a
    land ref that does not resolve — is renamed aside
    (:func:`_set_aside`) and initialised afresh. A ``HEAD`` merely
    existing is not "initialised" (gr762423)."""
    if (path / "HEAD").exists():
        defect = _bare_defect(path, refs, env=env)
        if defect is not None:
            try:
                _set_aside(path, defect)
            except OSError as exc:
                if why is not None:
                    why.append(f"corrupt mirror ({defect}) not set aside: {exc}")
                return False
    if not (path / "HEAD").exists():
        cp = _refresh_run(
            ["git", *TRUSTED_ARGS, "init", "--bare", str(path)],
            cwd=path.parent,
            env=trusted_env(env),
            timeout=30.0,
        )
        if cp.returncode != 0:
            if why is not None:
                why.append(f"bare init rc={cp.returncode}: {_tail(cp)}")
            return False
    cp = _refresh_run(
        ["git", *TRUSTED_ARGS, "--git-dir", str(path), "config", "gc.auto", "0"],
        cwd=path,
        env=trusted_env(env),
        timeout=30.0,
    )
    if cp.returncode != 0:
        if why is not None:
            why.append(f"bare config rc={cp.returncode}: {_tail(cp)}")
        return False
    return True


def _ops_repo(
    parent: Path,
    *object_dirs: Path,
    env: Mapping[str, str],
    why: list[str] | None = None,
) -> Path | None:
    """A fresh bare repo for credential-bearing git: a random-named
    gitdir the lander just created (own config, empty hooks, hooks off
    per-call via :data:`TRUSTED_ARGS` anyway), with ``object_dirs``
    seeded into ``objects/info/alternates`` so fetches/pushes see the
    shared objects without trusting a writable repo's context. The
    caller drops it with :func:`_drop_ops`. ``None`` when it cannot be
    created."""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        ops = Path(tempfile.mkdtemp(prefix="ops-", dir=str(parent)))
    except OSError as exc:
        if why is not None:
            why.append(f"ops mkdir: {refresh_error(exc)}")
        return None
    try:
        init = _refresh_run(
            ["git", *TRUSTED_ARGS, "init", "--bare", str(ops)],
            cwd=parent,
            env=trusted_env(env),
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        _drop_ops(ops)
        raise
    if init.returncode != 0:
        if why is not None:
            why.append(f"ops init rc={init.returncode}: {_tail(init)}")
        _drop_ops(ops)
        return None
    alternates = [str(d) for d in object_dirs if (d / "..").exists() or d.exists()]
    try:
        info = ops / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(
            "".join(f"{d}\n" for d in alternates), encoding="utf-8"
        )
    except OSError as exc:
        if why is not None:
            why.append(f"ops alternates: {refresh_error(exc)}")
        _drop_ops(ops)
        return None
    return ops


def _drop_ops(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def github_url(root: Path, *, env: Mapping[str, str]) -> str | None:
    """The real origin URL: the operator's ``PRECIS_FACTORY_ORIGIN_URL``
    pin when set, else the MAIN checkout's ``remote.origin.url`` (a
    same-uid adversary could point it elsewhere, so only an
    ``https://github.com/`` shape is accepted — the credential is
    github-scoped and anything else, an ``ssh://`` included, is a
    sideways read). ``None`` when nothing trustworthy answers."""
    pinned = os.environ.get(ORIGIN_URL_ENV, "").strip()
    if pinned:
        return pinned if _ORIGIN_URL_RE.fullmatch(pinned) else None
    cp = _run(
        ["git", *TRUSTED_ARGS, "-C", str(root), "remote", "get-url", "origin"],
        cwd=root,
        env=trusted_env(env),
        timeout=30.0,
    )
    url = (cp.stdout or "").strip()
    if cp.returncode != 0 or not _ORIGIN_URL_RE.fullmatch(url):
        return None
    return url


def _tip(
    repo: Path, ref: str, *, env: Mapping[str, str], why: list[str] | None = None
) -> str | None:
    cp = _refresh_run(
        [
            "git",
            *TRUSTED_ARGS,
            "--git-dir",
            str(repo),
            "rev-parse",
            f"refs/heads/{ref}",
        ],
        cwd=repo,
        env=trusted_env(env),
        timeout=30.0,
    )
    sha = (cp.stdout or "").strip()
    if cp.returncode == 0 and _SHA_RE.fullmatch(sha):
        return sha
    if why is not None:
        why.append(f"rev-parse {ref} rc={cp.returncode}: {_tail(cp)}")
    return None


def ls_remote(
    url: str,
    refs: tuple[str, ...] | list[str],
    *,
    env: Mapping[str, str],
    namespace: str = "refs/heads/",
    why: list[str] | None = None,
) -> dict[str, str] | None:
    """The oracle: ``git ls-remote`` from a non-repo cwd under
    :func:`trusted_env`, so no on-disk config can redirect it — the
    answer is what GitHub actually serves. ``{ref: sha}`` with ``ref``
    relative to ``namespace`` (branches by default; the factory's hidden
    slice refs pass ``refs/factory/``), ``None`` when GitHub cannot be
    read (a refused compare is always safe). A failure reason (token-free
    stderr tail) is appended to ``why`` when given."""
    try:
        cp = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "ls-remote",
                url,
                *[f"{namespace}{r}" for r in refs],
            ],
            cwd=Path(tempfile.gettempdir()),
            env=trusted_env(env),
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if why is not None:
            why.append(f"ls-remote {refresh_error(exc)}")
        return None
    if cp.returncode != 0:
        if why is not None:
            why.append(f"ls-remote rc={cp.returncode}: {_tail(cp)}")
        return None
    out: dict[str, str] = {}
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if (
            len(parts) == 2
            and parts[1].startswith(namespace)
            and _SHA_RE.fullmatch(parts[0])
        ):
            out[parts[1][len(namespace) :]] = parts[0]
    return out


#: Tries for the GitHub-facing steps of :func:`populate` (oracle read +
#: fetch + tip check), and the sleep before each retry — a transient
#: network blip must not fail a land.
POPULATE_TRIES = 3
POPULATE_BACKOFF_S: tuple[float, ...] = (2.0, 5.0)


def _tail(cp: subprocess.CompletedProcess[str]) -> str:
    """Token-free last bytes of a git call's stderr, for a failure reason."""
    text = mask_secrets((cp.stderr or "").strip())
    return text[-300:].replace("\n", " | ")


def _network_step(
    ops: Path,
    url: str,
    refs: tuple[str, ...],
    refspecs: list[str],
    *,
    env: Mapping[str, str],
    local: Mapping[str, str],
) -> tuple[dict[str, str] | None, str]:
    """One try of oracle read + GitHub fetch + tip check: ``(oracle,
    "")`` on success, else ``(None, reason)``."""
    why: list[str] = []
    oracle = ls_remote(url, refs, env=env, why=why)
    if oracle is None:
        return None, why[-1] if why else "ls-remote failed"
    if any(oracle.get(r) is None for r in refs):
        return None, "ls-remote oracle missing a land ref"
    try:
        cp = _run(
            ["git", *TRUSTED_ARGS, "-C", str(ops), "fetch", "--quiet", url, *refspecs],
            cwd=ops,
            env=trusted_env(env),
            timeout=POPULATE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"fetch {refresh_error(exc)}"
    if cp.returncode != 0:
        return None, f"fetch rc={cp.returncode}: {_tail(cp)}"
    for ref in refs:
        if _tip(ops, ref, env=local, why=why) != oracle[ref]:
            reason = (
                why[-1]
                if why
                else f"fetched tip of {ref} differs from the ls-remote oracle"
            )
            return None, reason
    return oracle, ""


def populate(
    mirror: Path,
    cache: Path,
    url: str,
    onto: str | None,
    *,
    env: Mapping[str, str],
    why: list[str] | None = None,
) -> dict[str, str] | None:
    """Refresh failures are infrastructure refusals, including raised local IO errors."""
    try:
        return _populate(mirror, cache, url, onto, env=env, why=why)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = f"mirror refresh: {refresh_error(exc)}"
        log.warning("factory mirror populate refused: %s", reason)
        if why is not None:
            why.append(reason)
        return None


def _populate(
    mirror: Path,
    cache: Path,
    url: str,
    onto: str | None,
    *,
    env: Mapping[str, str],
    why: list[str] | None = None,
) -> dict[str, str] | None:
    """Refresh THIS launch's mirror from GitHub under the factory
    credential and return the launch tips — ``{ref: sha}`` as GitHub
    held each ref, the sync's CAS basis.

    The fetch runs in a fresh :func:`_ops_repo` (primed from ``cache``
    so only the delta crosses the wire), the fetched tips are compared
    against the :func:`ls_remote` oracle — a config redirect or a
    tampered fetch source is refused, never launched on — then fetched
    into ``mirror`` over an unauthenticated local transport. The network
    steps (oracle + fetch + compare, the oracle re-read each try) are
    tried :data:`POPULATE_TRIES` times with a short backoff. ``None``
    when the fetch cannot be made to stick or does not verify; the
    failing step and a token-free git stderr tail are appended to
    ``why`` (when given) and logged."""

    def refuse(reason: str) -> dict[str, str] | None:
        log.warning("factory mirror populate refused: %s", reason)
        if why is not None:
            why.append(reason)
        return None

    refs = land_refs(onto)
    parent = mirror.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return refuse(f"mkdir: {refresh_error(exc)}")
    # ``env`` may carry the credential; only the GitHub-facing calls get
    # it. Everything local — the bare inits, the cache priming fetch
    # (whose spawned upload-pack reads the cache's writable config), the
    # tmp→mirror fetch, the tip reads — runs credential-free so no
    # planted hook/filter meets the token (review td660513).
    local = local_env(env)
    for repo, repo_refs in ((cache, ()), (mirror, refs)):
        details: list[str] = []
        # The cache is shared by every launch: a first-time ``git init``
        # writes HEAD before objects/, so an unserialised sibling's vet
        # could set aside a half-built healthy cache, and two launches
        # could rename one corrupt cache. The per-launch mirror is ours.
        with _cache_lock(repo) if repo == cache else contextlib.nullcontext():
            ok = _init_bare(repo, env=local, why=details, refs=repo_refs)
        if not ok:
            return refuse(
                details[-1] if details else f"bare init of {repo.name} failed"
            )
    details = []
    ops = _ops_repo(parent, cache / "objects", env=local, why=details)
    if ops is None:
        return refuse(details[-1] if details else "ops repo could not be created")
    try:
        # Prime haves from the cache (local, best-effort) so the GitHub
        # fetch negotiates only the delta.
        try:
            _refresh_run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "fetch",
                    "--quiet",
                    str(cache),
                    "+refs/heads/*:refs/heads/*",
                ],
                cwd=ops,
                env=local,
                timeout=60.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("factory mirror cache priming failed: %s", refresh_error(exc))
        refspecs = [f"+refs/heads/{r}:refs/heads/{r}" for r in refs]
        oracle: dict[str, str] | None = None
        reason = ""
        for attempt in range(POPULATE_TRIES):
            if attempt:
                time.sleep(
                    POPULATE_BACKOFF_S[min(attempt - 1, len(POPULATE_BACKOFF_S) - 1)]
                )
            oracle, reason = _network_step(
                ops, url, refs, refspecs, env=env, local=local
            )
            if oracle is not None:
                break
            log.warning(
                "factory mirror populate try %d/%d: %s",
                attempt + 1,
                POPULATE_TRIES,
                reason,
            )
        if oracle is None:
            return refuse(f"{reason} (after {POPULATE_TRIES} tries)")
        into = _refresh_run(
            _local_fetch_args(
                mirror,
                ops,
                *(f"+{oracle[r]}:refs/heads/{r}" for r in refs),
                bare=True,
            ),
            cwd=mirror,
            env=local,
            timeout=120.0,
        )
        if into.returncode != 0:
            return refuse(
                f"local fetch into the mirror rc={into.returncode}: {_tail(into)}"
            )
    finally:
        _drop_ops(ops)
    tips: dict[str, str] = {}
    for ref in refs:
        details = []
        sha = _tip(mirror, ref, env=local, why=details)
        if sha is None or sha != oracle[ref]:
            return refuse(
                details[-1]
                if details
                else f"mirror tip of {ref} does not match the oracle"
            )
        tips[ref] = sha
    return tips


def mirror_ship_env(
    mirror: Path, base: Mapping[str, str], origin_url: str
) -> dict[str, str]:
    """Ship's launch env: ``base`` plus ``GIT_CONFIG_*`` url-rewrite
    entries pointing every git transport contact with ``origin_url`` at
    the local bare mirror (fetch, push, ls-remote — the rewrite applies
    at the transport layer). Command-scope config is required because
    the worktrees share the main checkout's config, and it must be
    ``url.<mirror>.insteadOf`` rather than ``remote.origin.url``: git
    resolves a remote's URL without consulting command-scope config (a
    ``-c remote.origin.url=…`` / ``GIT_CONFIG_KEY_n`` override is read
    by ``git config --get`` but ignored by fetch and push — verified on
    git 2.56), while ``insteadOf`` is honored at connection time. The
    entries carry a local path and the repo's own public URL — no
    credential material of any kind. ``GIT_NO_REPLACE_OBJECTS=1`` rides
    the env too: ship's fetch spawns ``upload-pack`` in the adversary-
    writable mirror and its merges read the shared dir, so a planted
    ``refs/replace/*`` must not substitute the object a verified sha
    names (review td660513, pre-land pass)."""
    env = dict(base)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        n = 0
    variants = [origin_url]
    if origin_url.endswith(".git"):
        variants.append(origin_url[: -len(".git")])
    for url in variants:
        env[f"GIT_CONFIG_KEY_{n}"] = f"url.{mirror}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{n}"] = url
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


def changed_refs(
    mirror: Path, tips: Mapping[str, str], *, env: Mapping[str, str]
) -> dict[str, str] | None:
    """The sync set — refs whose mirror tip differs from the launch tip,
    i.e. the refs ship moved. ``None`` when a tip cannot be read: the
    caller must fail closed, never sync blind."""
    out: dict[str, str] = {}
    for ref, tip in tips.items():
        sha = _tip(mirror, ref, env=env)
        if sha is None:
            return None
        if sha != tip:
            out[ref] = sha
    return out


def _descends(repo: Path, ancestor: str, sha: str, *, env: Mapping[str, str]) -> bool:
    """``sha`` must descend from the launch tip — a mirror ref flipped to
    a foreign commit (same-uid sabotage, a shared-dir ref race) is never
    pushed to origin."""
    cp = _run(
        [
            "git",
            *TRUSTED_ARGS,
            "--git-dir",
            str(repo),
            "merge-base",
            "--is-ancestor",
            ancestor,
            sha,
        ],
        cwd=repo,
        env=trusted_env(env),
        timeout=30.0,
    )
    return cp.returncode == 0


def _push_ref(
    ops: Path,
    url: str,
    ref: str,
    sha: str,
    tip: str,
    *,
    env: Mapping[str, str],
) -> tuple[str, str]:
    """One leased push from the ops repo, reconciled like
    ``git_network``: a failure is not a verdict until the destination
    has been read, and a success is verified against the oracle — the
    pushed sha is what GitHub holds or the land defers."""
    for _attempt in range(2):
        try:
            cp = _run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "push",
                    url,
                    f"{sha}:refs/heads/{ref}",
                    f"--force-with-lease={ref}:{tip}",
                ],
                cwd=ops,
                env=trusted_env(env),
                timeout=SYNC_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ("error", f"{ref}: push did not answer ({type(exc).__name__})")
        remote = (ls_remote(url, (ref,), env=env) or {}).get(ref)
        if cp.returncode == 0:
            if remote == sha:
                return ("ok", ref)
            if remote is None:
                return ("error", f"{ref}: GitHub unreadable after the push")
            return (
                "raced",
                f"{ref} moved on origin after the push: expected {sha[:9]}, "
                f"found {remote[:9]}",
            )
        if remote is None:
            return ("error", f"{ref}: GitHub unreadable after the push failed")
        if remote == sha:
            # Another sync (a same-sha reland) already placed it.
            return ("ok", ref)
        if remote != tip:
            return (
                "raced",
                f"{ref} moved on origin: expected {tip[:9]}, found {remote[:9]}",
            )
        # The lease still names the launch tip: one straight retry, then
        # the race reading above decides on a fresh read.
    return ("raced", f"{ref}: lease held {tip[:9]} across a retry")


def fetch_ref_into(
    dest: Path,
    url: str,
    ref: str,
    dest_ref: str,
    *,
    ops_parent: Path,
    env: Mapping[str, str],
    object_dirs: tuple[Path, ...] = (),
    bare: bool = False,
) -> str | None:
    """Fetch branch ``ref`` of ``url`` under the factory credential into
    ``dest`` (``bare``: a bare mirror gitdir, addressed with ``--git-dir``)
    — a clone a builder session can write — without running one
    credential-bearing git inside it. The fetch lands in a fresh
    :func:`_ops_repo`, its tip is compared with the :func:`ls_remote`
    oracle, and only then does a credential-free fetch into the destination
    move the objects and force-update exactly ``dest_ref`` (say
    ``refs/remotes/origin/main``) in ``dest``. The tip sha, or ``None``
    on any failure — a git failure or timeout never raises."""
    local = local_env(env)
    ops = _ops_repo(ops_parent, *object_dirs, env=local)
    if ops is None:
        return None
    try:
        oracle = (ls_remote(url, (ref,), env=env) or {}).get(ref)
        if oracle is None:
            return None
        cp = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                str(ops),
                "fetch",
                "--quiet",
                url,
                f"+refs/heads/{ref}:refs/heads/{ref}",
            ],
            cwd=ops,
            env=trusted_env(env),
            timeout=POPULATE_TIMEOUT_S,
        )
        if cp.returncode != 0 or _tip(ops, ref, env=local) != oracle:
            return None
        into = _run(
            _local_fetch_args(dest, ops, f"+{oracle}:{dest_ref}", bare=bare),
            cwd=dest,
            env=local,
            timeout=120.0,
        )
        return oracle if into.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        _drop_ops(ops)


def fetch_source_mirror(
    url: str, ref: str, *, mirror: Path, env: Mapping[str, str]
) -> str | None:
    """A local copy of a dependency's source, for a pin-bump job to resolve
    its git source from with no credential. Make sure a bare repo exists at
    ``mirror`` (a job-owned directory under
    :func:`mirror_root`; the caller picks the path), fetch branch ``ref``
    of ``url`` into it under the factory credential through
    :func:`fetch_ref_into` (fresh ops gitdir, oracle-checked tip, then a
    credential-free fetch into the mirror), and
    return the tip sha, or ``None`` on any failure (the error text is
    never surfaced, so no token can leak through it).

    The mirror ends with ``refs/heads/<ref>`` at the returned tip and
    ``HEAD`` pointing at that branch; no tags, no other branches, and the
    objects of earlier calls stay (``gc.auto 0``). That is all a credential-
    free ``uv`` needs, run with an ``url.<mirror>.insteadOf`` rewrite of
    ``url``: for a sha-pinned git source it runs one plain
    ``git fetch <url> +<sha>:refs/commit/<sha>`` against the mirror (proved
    with uv 0.13.8), which upload-pack answers from the object store for
    any commit reachable from the branch. So the commit to pin must be an
    ancestor of ``ref``'s tip (a release tag is, once merged); a commit
    only on an unmerged branch is not in the mirror."""
    local = local_env(env)
    try:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        if not _init_bare(mirror, env=local, refs=(ref,)):
            return None
        tip = fetch_ref_into(
            mirror,
            url,
            ref,
            f"refs/heads/{ref}",
            ops_parent=mirror.parent,
            env=env,
            bare=True,
        )
        if tip is None:
            return None
        head = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "--git-dir",
                str(mirror),
                "symbolic-ref",
                "HEAD",
                f"refs/heads/{ref}",
            ],
            cwd=mirror,
            env=local,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return tip if head.returncode == 0 else None


def push_cas(
    url: str,
    ref: str,
    sha: str,
    tip: str,
    *,
    ops_parent: Path,
    env: Mapping[str, str],
    object_dirs: tuple[Path, ...],
) -> tuple[str, str]:
    """Push ``sha`` onto branch ``ref`` of ``url`` as a pure fast-forward
    compare-and-swap from a fresh ops gitdir whose alternates hold
    ``object_dirs`` (the clone's object store). A plain push — never a
    ``+`` refspec, ``--force`` or a lease: origin's own fast-forward rule
    is the compare, and no force push reaches this repo. ``sha`` must
    descend from ``tip`` before anything touches the network, and the
    outcome is reconciled against the oracle like :func:`_push_ref`:
    ``("ok", ref)``, ``("raced", detail)`` when origin moved under the
    push, ``("error", detail)`` when it cannot be verified."""
    local = local_env(env)
    ops = _ops_repo(ops_parent, *object_dirs, env=local)
    if ops is None:
        return ("error", f"{ref}: no scratch for the push ops repo")
    try:
        if not _descends(ops, tip, sha, env=local):
            return (
                "error",
                f"{ref}: {sha[:9]} does not descend from {tip[:9]} — refusing the push",
            )
        before = (ls_remote(url, (ref,), env=env) or {}).get(ref)
        if before is None:
            return ("error", f"{ref}: GitHub unreadable before the push")
        if before == sha:
            return ("ok", ref)
        try:
            cp = _run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "push",
                    url,
                    f"{sha}:refs/heads/{ref}",
                ],
                cwd=ops,
                env=trusted_env(env),
                timeout=SYNC_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ("error", f"{ref}: push did not answer ({type(exc).__name__})")
        remote = (ls_remote(url, (ref,), env=env) or {}).get(ref)
        if remote is None:
            return ("error", f"{ref}: GitHub unreadable after the push")
        if remote == sha:
            return ("ok", ref)
        if cp.returncode == 0:
            return (
                "raced",
                f"{ref} moved on origin after the push: expected {sha[:9]}, "
                f"found {remote[:9]}",
            )
        if remote != tip:
            return (
                "raced",
                f"{ref} moved on origin: expected {tip[:9]}, found {remote[:9]}",
            )
        out = (cp.stderr or "").lower()
        if "non-fast-forward" in out or "fetch first" in out:
            return ("raced", f"{ref}: push rejected as non-fast-forward")
        return ("error", f"{ref}: push failed with origin still at {tip[:9]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("error", f"{ref}: git did not answer ({type(exc).__name__})")
    finally:
        _drop_ops(ops)


_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _safe_tag(tag: str) -> bool:
    """A tag name that cannot be read as an option or a ref escape: no
    leading ``-``, no whitespace or ref metacharacters, no ``..``."""
    return bool(_TAG_RE.fullmatch(tag)) and ".." not in tag


def ls_remote_tag(url: str, tag: str, *, env: Mapping[str, str]) -> str | None:
    """The oracle for one tag: ``git ls-remote`` from a non-repo cwd under
    :func:`trusted_env`, asking for ``refs/tags/<tag>`` and its peeled
    ``^{}`` line. The commit the tag resolves to — the peeled sha of an
    annotated tag, the direct sha of a lightweight one — ``""`` when the
    remote has no such tag, ``None`` when it cannot be read (or ``tag`` is
    not a safe name, in which case git never runs)."""
    if not _safe_tag(tag):
        return None
    ref = f"refs/tags/{tag}"
    try:
        cp = _run(
            ["git", *TRUSTED_ARGS, "ls-remote", url, ref, f"{ref}^{{}}"],
            cwd=Path(tempfile.gettempdir()),
            env=trusted_env(env),
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    direct = ""
    peeled = ""
    for line in (cp.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not _SHA_RE.fullmatch(parts[0]):
            return None
        if parts[1] == ref:
            direct = parts[0]
        elif parts[1] == f"{ref}^{{}}":
            peeled = parts[0]
        else:
            return None
    return peeled or direct


def push_tag_new(
    url: str,
    tag: str,
    sha: str,
    *,
    message: str,
    tagger: tuple[str, str],
    ops_parent: Path,
    env: Mapping[str, str],
    object_dirs: tuple[Path, ...] = (),
) -> tuple[str, str]:
    """Create annotated tag ``tag`` at commit ``sha`` on ``url``,
    create-only, from a fresh ops gitdir whose alternates hold
    ``object_dirs`` (the clone's object store). The tag object is made
    under :func:`local_env` (no credential names) with ``tagger`` as the
    identity; only the push runs under :func:`trusted_env`. The push is a
    plain ``refs/tags/<tag>`` refspec — never ``+``, ``--force`` or
    ``--tags`` — so an existing tag is never moved. The :func:`ls_remote_tag`
    oracle brackets the push and the push's own exit status is not
    trusted: the answer is what the remote serves. ``("ok", detail)`` when
    the remote tag peels to ``sha`` (already there or created),
    ``("exists", sha)`` when it peels elsewhere, ``("error", detail)``
    otherwise. Never raises."""
    if not _safe_tag(tag):
        return ("error", f"{tag!r}: not a safe tag name")
    if not _SHA_RE.fullmatch(sha):
        return ("error", f"{tag}: {sha!r} is not a full lowercase sha")
    before = ls_remote_tag(url, tag, env=env)
    if before is None:
        return ("error", f"{tag}: remote unreadable before the push")
    if before == sha:
        return ("ok", f"{tag} already at {sha[:9]}")
    if before:
        return ("exists", before)
    local = local_env(env)
    ops = _ops_repo(ops_parent, *object_dirs, env=local)
    if ops is None:
        return ("error", f"{tag}: no scratch for the tag ops repo")
    try:
        have = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                str(ops),
                "cat-file",
                "-e",
                f"{sha}^{{commit}}",
            ],
            cwd=ops,
            env=local,
            timeout=30.0,
        )
        if have.returncode != 0:
            return ("error", f"{tag}: commit {sha[:9]} not found in the clone")
        name, email = tagger
        made = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                str(ops),
                "tag",
                "-a",
                "--no-sign",
                "-m",
                message,
                tag,
                sha,
            ],
            cwd=ops,
            env={**local, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email},
            timeout=30.0,
        )
        if made.returncode != 0:
            return ("error", f"{tag}: could not create the tag object")
        try:
            _run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "push",
                    url,
                    f"refs/tags/{tag}:refs/tags/{tag}",
                ],
                cwd=ops,
                env=trusted_env(env),
                timeout=SYNC_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # the oracle below is the verdict
        after = ls_remote_tag(url, tag, env=env)
        if after is None:
            return ("error", f"{tag}: remote unreadable after the push")
        if after == sha:
            return ("ok", f"{tag} at {sha[:9]}")
        if after:
            return ("exists", after)
        return ("error", f"{tag}: push left no tag on the remote")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("error", f"{tag}: git did not answer ({type(exc).__name__})")
    finally:
        _drop_ops(ops)


def sync(
    mirror: Path,
    cache: Path | None,
    url: str,
    tips: Mapping[str, str],
    *,
    expect: Mapping[str, str],
    env: Mapping[str, str],
) -> tuple[str, str]:
    """Push the refs ship moved to GitHub, each leased on its launch
    tip and each verified before and after:

    * ``expect`` is ``{land target: the sha ship reported landed}`` — a
      moved ref ship never claimed, a mirror tip that is not the claimed
      sha, or a claimed land whose ref never moved are all refused
      (the "no land refs moved" record of an unpushed sha);
    * the pushed commit must descend from the launch tip;
    * after the push the oracle must show exactly that sha.

    ``("ok", detail)`` when everything verifies; ``("raced", detail)``
    when origin moved under the land — the caller defers and ship
    re-runs on a fresh mirror; ``("error", detail)`` when GitHub, the
    mirror or the claimed sha cannot be verified — the caller defers
    too, never records a land origin does not hold."""
    # Mirror-side reads and the ops repo are local work — credential-free
    # (:func:`local_env`); only the leased pushes and their oracle
    # re-reads carry ``env``'s token.
    local = local_env(env)
    try:
        changed = changed_refs(mirror, tips, env=local)
    except OSError as exc:
        return ("error", f"mirror unreadable ({exc})")
    if changed is None:
        return ("error", "mirror ref unreadable")
    expected = dict(expect)
    if not changed:
        if expected:
            ref, sha = next(iter(expected.items()))
            return (
                "error",
                f"{ref}: ship reported landed {sha[:9]} but the mirror "
                "shows no land refs moved — refusing the record",
            )
        return ("ok", "no land refs moved")
    for ref, sha in changed.items():
        if ref not in expected:
            return (
                "error",
                f"{ref} moved in the mirror ({sha[:9]}) but ship reported "
                "no land there — refusing to push what ship did not claim",
            )
        want = expected[ref]
        if sha != want:
            return (
                "error",
                f"{ref}: mirror tip {sha[:9]} is not the sha ship reported "
                f"({want[:9]}) — refusing the push",
            )
        if not _descends(mirror, tips[ref], sha, env=local):
            return (
                "error",
                f"{ref}: mirror tip {sha[:9]} does not descend from the "
                f"launch tip {tips[ref][:9]} — refusing the push",
            )
    ops_dirs = [mirror / "objects"]
    if cache is not None:
        ops_dirs.append(cache / "objects")
    ops = _ops_repo(mirror.parent, *ops_dirs, env=local)
    if ops is None:
        return ("error", "no scratch for the sync ops repo")
    try:
        placed: list[str] = []
        for ref, sha in changed.items():
            outcome, detail = _push_ref(ops, url, ref, sha, tips[ref], env=env)
            if outcome != "ok":
                return (outcome, detail)
            placed.append(ref)
        return ("ok", ", ".join(placed) + " → origin")
    finally:
        _drop_ops(ops)


def repair_origin(
    common: Path,
    url: str,
    refs: tuple[str, ...] | list[str],
    *,
    phantom_main: str | None,
    env: Mapping[str, str],
) -> list[str]:
    """Undo ship's not-yet-published side effects in the shared git dir:
    ``refs/remotes/origin/*`` ship fetched or pushed from the mirror are
    reset to GitHub's real tips, and a local ``main`` fast-forwarded to
    the unpushed squash is rewound — both only after a sync failed or
    deferred, so the shared dir stops claiming a land origin does not
    hold. Best-effort notes; never raises.

    ``env`` may carry the factory credential — it reaches only the two
    GitHub-facing calls (the oracle, the fetch from ``url``). Every op
    touching ``common`` or the primary worktree runs under
    :func:`local_env` (review td660513, second pass): their spawned
    children — an upload-pack's ``packObjectsHook`` on the local-path
    fetches, the ``status``/``reset`` filter or fsmonitor calls in the
    worktree — run code the local, adversary-writable config names, and
    must inherit nothing to steal."""
    notes: list[str] = []
    local = local_env(env)
    oracle = ls_remote(url, refs, env=env)
    if oracle is None:
        return ["origin repair: GitHub unreadable — refs left for the relaunch"]
    ops = _ops_repo(common.parent, common / "objects", env=local)
    if ops is None:
        return ["origin repair: no scratch for the ops repo"]
    try:
        # Prime haves from the common dir so the GitHub fetch is a delta.
        _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                str(ops),
                "fetch",
                "--quiet",
                str(common),
                "+refs/heads/*:refs/heads/*",
            ],
            cwd=ops,
            env=local,
            timeout=60.0,
        )
        refspecs = [f"+refs/heads/{r}:refs/heads/{r}" for r in refs if oracle.get(r)]
        if refspecs:
            _run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "fetch",
                    "--quiet",
                    url,
                    *refspecs,
                ],
                cwd=ops,
                env=trusted_env(env),
                timeout=POPULATE_TIMEOUT_S,
            )
        for ref, tip in oracle.items():
            into = _run(
                _local_fetch_args(common, ops, f"{tip}:refs/remotes/origin/{ref}"),
                cwd=common,
                env=local,
                timeout=60.0,
            )
            notes.append(
                f"origin/{ref} → {tip[:9]}"
                if into.returncode == 0
                else f"origin/{ref} repair FAILED (fetch exit {into.returncode})"
            )
    finally:
        _drop_ops(ops)
    if phantom_main:
        real = oracle.get("main")
        cur = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                str(common),
                "rev-parse",
                "refs/heads/main",
            ],
            cwd=common,
            env=local,
            timeout=30.0,
        )
        if real and cur.returncode == 0 and (cur.stdout or "").strip() == phantom_main:
            notes.append(_rewind_local_main(common, real, phantom_main, env=local))
        elif cur.returncode == 0:
            notes.append("local main: not the unpushed squash — left alone")
    return notes


def _primary_worktree(common: Path, *, env: Mapping[str, str]) -> str | None:
    """The primary checkout's path — the first ``worktree list`` row —
    ``None`` when git cannot answer."""
    cp = _run(
        [
            "git",
            *TRUSTED_ARGS,
            "-C",
            str(common),
            "worktree",
            "list",
            "--porcelain",
        ],
        cwd=common,
        env=env,
        timeout=30.0,
    )
    return next(
        (
            line.partition(" ")[2]
            for line in (cp.stdout or "").splitlines()
            if line.startswith("worktree ")
        ),
        None,
    )


def _primary_branch(primary: str, *, env: Mapping[str, str]) -> str | None:
    """The branch the primary worktree checks out (``None`` detached or
    unreadable)."""
    cp = _run(
        [
            "git",
            *TRUSTED_ARGS,
            "-C",
            primary,
            "symbolic-ref",
            "-q",
            "--short",
            "HEAD",
        ],
        cwd=Path(primary),
        env=env,
        timeout=30.0,
    )
    branch = (cp.stdout or "").strip()
    return branch if cp.returncode == 0 and branch else None


def _rewind_local_main(
    common: Path, real: str, phantom: str, *, env: Mapping[str, str]
) -> str:
    """Rewind the shared local ``main`` off the unpushed squash: when the
    primary worktree checks it out and is clean (ship's ff left a
    consistent tree) a ``reset --hard`` restores ref, index and worktree
    in one move; otherwise the ref alone moves and the worktree keeps
    ship's checkout until a human resets it. Every op is local and runs
    under :func:`local_env` — a planted ``core.fsmonitor`` or checkout
    filter the ``status``/``reset`` spawn must not meet the token
    (review td660513, second pass)."""
    env = local_env(env)
    first = _primary_worktree(common, env=env)
    if first:
        status = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                first,
                "status",
                "--porcelain",
            ],
            cwd=Path(first),
            env=env,
            timeout=30.0,
        )
        if (
            _primary_branch(first, env=env) == "main"
            and status.returncode == 0
            and not (status.stdout or "").strip()
        ):
            cp = _run(
                [
                    "git",
                    *TRUSTED_ARGS,
                    "-C",
                    first,
                    "reset",
                    "--hard",
                    real,
                ],
                cwd=Path(first),
                env=env,
                timeout=60.0,
            )
            return (
                f"local main rewound {phantom[:9]} → {real[:9]} (worktree reset)"
                if cp.returncode == 0
                else f"local main rewind FAILED (reset exit {cp.returncode})"
            )
    cp = _run(
        [
            "git",
            *TRUSTED_ARGS,
            "-C",
            str(common),
            "update-ref",
            "refs/heads/main",
            real,
        ],
        cwd=common,
        env=env,
        timeout=30.0,
    )
    return (
        f"local main rewound {phantom[:9]} → {real[:9]} (ref only — the "
        "worktree keeps ship's checkout until a human resets it)"
        if cp.returncode == 0
        else f"local main rewind FAILED (update-ref exit {cp.returncode})"
    )


def advance_local_main(common: Path, sha: str, *, env: Mapping[str, str]) -> str:
    """The local-main advance ship's ``_sync_local_main`` would have run,
    deferred until the origin sync verified ``sha`` (review td660513
    finding 4, second pass): this launch's ship ran under
    :func:`ship_git_shim`, whose PATH entry refuses those calls — the
    shared main must never sit on a commit only the mirror holds, and a
    best-effort repair cannot cover a crashed worker or a blind GitHub.
    Ship's own moves, replayed here — ``merge --ff-only`` in the primary
    when it checks ``main`` out (ref + worktree together, safely refused
    when dirty or diverged), else the bare ref — under the
    credential-free :func:`local_env`. One note; never raises."""
    env = local_env(env)
    first = _primary_worktree(common, env=env)
    if first is None:
        return "local main advance: primary worktree unreadable — left alone"
    if _primary_branch(first, env=env) == "main":
        cp = _run(
            [
                "git",
                *TRUSTED_ARGS,
                "-C",
                first,
                "merge",
                "--ff-only",
                sha,
            ],
            cwd=Path(first),
            env=env,
            timeout=60.0,
        )
        return (
            f"local main → {sha[:9]} (advanced after the origin sync verified)"
            if cp.returncode == 0
            else "local main not fast-forwarded (primary dirty or "
            "diverged) — left at its tip"
        )
    cp = _run(
        [
            "git",
            *TRUSTED_ARGS,
            "-C",
            str(common),
            "update-ref",
            "refs/heads/main",
            sha,
        ],
        cwd=common,
        env=env,
        timeout=30.0,
    )
    return (
        f"local main ref → {sha[:9]} (advanced after the origin sync verified)"
        if cp.returncode == 0
        else f"local main ref not moved (update-ref exit {cp.returncode})"
    )


#: The shim's bin dir inside the launch mirror — removed with the mirror
#: on a verified sync.
SHIP_BIN_DIR_NAME = "ship-bin"

#: The ``git`` shim's body; ``__GIT__`` is replaced by the real binary's
#: resolved path. It refuses exactly the two call shapes ship's
#: ``_sync_local_main`` uses to move the shared local main — refused, not
#: answered, so ship notes its usual best-effort warning and moves on.
#: Everything else execs real git untouched.
_SHIP_GIT_SHIM = """#!/bin/sh
# precis-land git shim (gr659929, review td660513): ship pushed the
# lander's private mirror, not GitHub — so the shared local main must
# not be fast-forwarded to the unpublished squash yet. The lander
# replays the advance itself after the mirror→origin sync verifies;
# until then the two _sync_local_main shapes are refused here.
_seen_merge=""
_seen_ff=""
_seen_ur=""
_seen_main=""
for _a in "$@"; do
    case "$_a" in
        merge) _seen_merge=1 ;;
        --ff-only) _seen_ff=1 ;;
        update-ref) _seen_ur=1 ;;
        refs/heads/main) _seen_main=1 ;;
    esac
done
if [ "$_seen_merge$_seen_ff" = "11" ] || [ "$_seen_ur$_seen_main" = "11" ]; then
    printf '%s\\n' "precis-land git shim: the shared local-main advance is deferred to the lander until the origin sync verifies (gr659929)" >&2
    exit 1
fi
exec __GIT__ "$@"
"""


def ship_git_shim(mirror: Path) -> Path | None:
    """Write this launch's ``git`` shim into ``<mirror>/ship-bin/`` and
    return the dir for ship's PATH prepend — ``None`` when it cannot be
    written or no real git resolves on the worker's PATH.

    The env-config rewrite already points ship's *transports* at the
    mirror, but ``_sync_local_main`` is not a transport call: it
    fast-forwards the shared local ``main`` in the primary checkout to
    the squash ship just pushed — a ref and worktree state that claims
    a land origin does not yet hold (review td660513 finding 4, second
    pass: a sync that then cannot reach GitHub, or a worker that dies
    before the repair, leaves the phantom). The shim refuses those two
    call shapes; the lander replays the advance with
    :func:`advance_local_main` once the sync verifies. It is a
    *correctness* boundary for ship's own steps — the mirror tree is
    adversary-writable by design and a gate child can call real git
    itself; doing so wins it nothing origin lacks."""
    real = shutil.which("git")
    if real is None:
        return None
    try:
        bindir = mirror / SHIP_BIN_DIR_NAME
        bindir.mkdir(parents=True, exist_ok=True)
        shim = bindir / "git"
        shim.write_text(_SHIP_GIT_SHIM.replace("__GIT__", real), encoding="utf-8")
        shim.chmod(0o755)
    except OSError:
        return None
    return bindir


__all__ = [
    "CACHE_DIR_NAME",
    "MIRROR_PARENT_NAME",
    "MIRROR_REFS",
    "ORIGIN_URL_ENV",
    "SHIP_BIN_DIR_NAME",
    "TRUSTED_ARGS",
    "advance_local_main",
    "cache_dir",
    "changed_refs",
    "drop_launch",
    "github_url",
    "has_broken_tips",
    "land_refs",
    "launch_tips_path",
    "local_env",
    "ls_remote",
    "ls_remote_tag",
    "mirror_dir",
    "mirror_root",
    "mirror_ship_env",
    "populate",
    "push_tag_new",
    "read_launch_tips",
    "repair_origin",
    "ship_git_shim",
    "sync",
    "trusted_env",
    "write_launch_tips",
]
