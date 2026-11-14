"""``pin_bump``: move precis's pin of a released engine, mechanically (td659522 step 4).

precis pins the engine package ``autocatpath`` by git rev; moving the pin is
a four-file change (``pyproject.toml``, ``uv.lock``,
``deploy/group_vars/all.yml``, the anchor literals in
``tests/test_coverage_job.py``). The module has two halves; nothing
registers it yet (R2: ``SPEC`` is defined, wired nowhere).

The pure half comes first: text in, text out, no I/O, no subprocess, no
store. Every helper edits the text by line, then re-parses the result
(``tomllib`` / ``ast`` / a line diff) to prove the edit changed exactly what
it meant to.

The job half (below the helpers) takes its shapes from
:mod:`~precis.workers.job_types.repo_release`: an ``ssh_node`` detached
``submit`` / ``poll`` / ``kill``, the phase in the handle, parked handles via
:func:`repo_land.park_until`, its own ``_fail`` (attempt-free classes, a
``handoff:`` comment, never the node's tags or status) and a record stamped
on the pin node (``meta.pin_bump``). It runs in the precis slice clone and
needs no clone of the dependency. Params: ``todo_id`` (the pin-bump node),
``release_todo`` (the release node whose ``meta.repo_release`` names the
version) and ``pin_repo`` (the dependency's profile; its ``pinner`` must be
this job). Phases, in the handle's ``phase``:

* ``resolve`` (parked): :func:`submit` only guards and parks. The first poll
  reads the release record, fetches the precis tip, checks the tag oracle
  and a job-owned source mirror of the dependency (the release sha is an
  ancestor of its tip and its ``pyproject.toml`` carries the version), reads
  the four files at the tip, and either ends as a no-op (the pin is
  already there or past it, as the mirror's history says: the release sha
  an ancestor of (or equal to) the pinned sha is "already past" whatever the
  version strings say), refuses (charged, with a coordinator ask: the two
  shas on different lines of history, a version below the floor that is not
  behind the pin, a deploy floor above the release) or goes on: the dependency's own ``pyproject.toml`` at the old and the new sha
  must agree on dependencies, ``requires-python`` and ``[build-system]``
  (``uv lock --check`` compares text only, so a text-edited lock would be
  wrong otherwise), then a worktree ``pin-td<id>`` on ``slice/<id>`` (what
  ``land_slice`` lands) gets the three edited files. Nothing is committed.
* ``derive`` (detached, credential-free): ``uv lock --check``, ``uv sync
  --frozen`` into a venv outside the tree, then a worker-written script
  asserts the installed engine's version and computes the coverage anchor
  key with it. The git source resolves from the job's mirror through an
  ``url.<mirror>.insteadOf`` rewrite.
* commit (same poll as the derive's green): only a 64-hex key whose engine
  equals the target is accepted. The commit is built from objects (``hash-
  object``, a private index, ``commit-tree``, ``update-ref``) so nothing the
  derive left in the tree can reach it, then proved: one child of the tip,
  the four paths at most, each file's text equal to what the worker wrote,
  at most 8 changed lines per file. The tree is recreated clean.
* ``verify`` (detached, own venv and result file): an independent check
  of the engine and the key. ``uv sync --frozen`` into a fresh venv, the
  worker-written derive script again (engine version assert, key) into a
  second result file, then the pin coherence tests and the anchor test in the
  committed tree. Green requires that second result to carry the target
  engine and the very key committed (else charged, branch and tree
  dropped), then stamps ``meta.pin_bump`` ``state: "committed"``. When a
  phase's exit file appears its whole process group is TERMed then KILLed
  before anything is read. Both venvs and both result files are removed when
  the job ends; the scripts and logs stay for the operator. The transport
  classifier reads only the text before a failing ``lock`` / ``sync`` step's
  marker.
* ``handoff`` (parked, due at once): a committed bump mints the
  ``land_slice`` for the branch (``run_slice``'s own mint: idem key
  ``land_slice:td<id>``, a child of the node), stamps the record ``handed``
  and succeeds. A refused or failed mint fails attempt-free
  (:data:`HANDOFF_CLASS`); the record stays ``committed`` and the next job
  resumes straight here (a live land under the idem key is adopted). A
  committed record whose commit is still the branch head on the same tip is
  resumed to it. A no-op mints no land and the dispatcher closes a pin node
  only on a land, so the job closes the node itself: the same gripe
  resolution, one ``pinned:`` comment (never ``landed:``), ``STATUS:done``.

:func:`finish` is the closing tail ``land_slice`` calls right before its land
record: it stamps the record ``landed`` and resolves the batch's gripes.
:func:`validate_submit` serialises pin bumps (one in flight at a time).

The derive and verify runs execute repo code as the worker's user, so the
pin tree, run dir, venvs and uv cache are attacker-writable. Only a 64-hex
key for the target engine is accepted from a result file; the commit is built
from objects and compared byte for byte; the verify recomputes the key in a
fresh venv. Limits: a process of the same uid that is outside the killed
group (or that races the kill) can still forge a result file, a log marker
(which can mislabel a red as attempt-free transport, bounded by the retry
count) or the uv cache, so a wrong key that both runs agree on is not
excluded. The token is only ever in the env handed to
``factory_git_mirror`` helpers, never in argv, a URL, meta, a chunk or a
comment, and the detached runs get none.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.store.types import ChunkInsert, Tag
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git_mirror as _mirror
from precis.workers import repo_land as _rl
from precis.workers._mint import JOB_TERMINAL_STATUSES
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import land_slice as _ls
from precis.workers.job_types import repo_release as _rr
from precis.workers.job_types import run_slice as _rs
from precis.workers.job_types.repo_release import (
    _HEADER,
    ReleaseError,
    _norm,
    _split_version,
)

log = logging.getLogger(__name__)

__all__ = [
    "COORDINATOR_ASK_KEY",
    "DEADLINE_KILL_META",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "PinError",
    "compare_versions",
    "current_pin",
    "finish",
    "kill",
    "pin_is_coherent",
    "poll",
    "set_anchor_literals",
    "set_group_vars_floor",
    "set_lock_pin",
    "set_pyproject_pin",
    "submit",
    "validate_submit",
]


class PinError(ValueError):
    """A pin helper refused its input; the message is one line."""


_SHA = re.compile(r"[0-9a-f]{40}")
_KEY64 = re.compile(r"[0-9a-f]{64}")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME_IN_LINE = re.compile(r"\bname\s*=\s*\"([^\"\r\n]*)\"")
_LOCK_SOURCE = re.compile(
    r"\s*source\s*=\s*\{\s*git\s*=\s*\"([^\"\r\n]*)\?rev=([0-9a-f]{40})"
    r"#([0-9a-f]{40})\"\s*\}\s*(?:#.*)?"
)
_LOCK_VERSION = re.compile(r"(\s*version\s*=\s*\")([^\"\r\n]*)(\".*)", re.DOTALL)


def _version(version: str, what: str = "version") -> tuple[int, int, int]:
    try:
        return _split_version(version)
    except ReleaseError as exc:
        raise PinError(f"{what} {version!r} is not exactly X.Y.Z") from exc


def _sha(sha: str, what: str = "sha") -> str:
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise PinError(f"{what} {sha!r} is not 40 lowercase hex")
    return sha


def _package(package: str) -> str:
    if not isinstance(package, str) or _PACKAGE.fullmatch(package) is None:
        raise PinError(f"package name {package!r} is not a plain distribution name")
    return package


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or _IDENT.fullmatch(name) is None:
        raise PinError(f"{what} {name!r} is not a plain identifier")
    return name


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PinError(f"{what} does not parse as TOML: {exc}") from exc


def compare_versions(a: str, b: str) -> int:
    """``-1`` / ``0`` / ``1`` as plain ``X.Y.Z`` ``a`` is below / equal to /
    above ``b``."""
    ta, tb = _version(a), _version(b)
    return (ta > tb) - (ta < tb)


# --- pyproject.toml -------------------------------------------------------


def _table_lines(lines: list[str], table: str) -> list[int]:
    """Indexes of the lines inside the ``[table]`` header's body."""
    out: list[int] = []
    inside = False
    for i, line in enumerate(lines):
        h = _HEADER.fullmatch(line.rstrip("\r\n"))
        if h is not None:
            inside = h[1] == "[" and h[2] == table
            continue
        if inside:
            out.append(i)
    return out


def _pyproject_lines(
    lines: list[str], package: str, extra: str
) -> tuple[int, re.Match[str], int, re.Match[str]]:
    """``(floor line index, floor match, sources line index, sources match)``.

    The floor line is the one-line ``<extra> = [ ... ]`` entry of
    ``[project.optional-dependencies]`` holding the bare ``"<package>>=X.Y.Z"``
    requirement; the sources line is the ``<package> = { git = ..., rev = ... }``
    entry of ``[tool.uv.sources]``. Each must appear exactly once."""
    pkg = re.escape(package)
    floor_re = re.compile(
        rf"(?P<head>{re.escape(extra)}\s*=\s*\[.*?[\"'])"
        rf"{pkg}>=(?P<ver>[^\"'\s,\]]*)(?P<tail>[\"'].*)",
        re.DOTALL,
    )
    src_re = re.compile(
        rf"(?P<head>{pkg}\s*=\s*)(?P<tbl>\{{[^{{}}]*\}})(?P<tail>.*)", re.DOTALL
    )
    floors = [
        (i, m)
        for i in _table_lines(lines, "project.optional-dependencies")
        if (m := floor_re.fullmatch(lines[i])) is not None
    ]
    sources = [
        (i, m)
        for i in _table_lines(lines, "tool.uv.sources")
        if (m := src_re.fullmatch(lines[i])) is not None
    ]
    if len(floors) != 1:
        raise PinError(
            f"pyproject.toml has {len(floors)} floor lines for {package!r} "
            f"in extra {extra!r} (want exactly 1)"
        )
    if len(sources) != 1:
        raise PinError(
            f"pyproject.toml has {len(sources)} [tool.uv.sources] lines for "
            f"{package!r} (want exactly 1)"
        )
    if lines[floors[0][0]].count(f"{package}>=") != 1:
        raise PinError(f"floor line for {package!r} names the requirement twice")
    return floors[0][0], floors[0][1], sources[0][0], sources[0][1]


_TBL_REV = re.compile(r"(\brev\s*=\s*\")([^\"]*)(\")")
_TBL_GIT = re.compile(r"\bgit\s*=\s*\"([^\"]*)\"")
_TAIL = re.compile(r"(?P<ws>[ \t]*)(?:#.*?)?(?P<eol>\r?\n?)")


def current_pin(
    pyproject_text: str, package: str, *, extra: str = "catalyst"
) -> tuple[str, str, str]:
    """``(floor version, rev sha40, git url)`` the pyproject carries for
    ``package``: the floor from the ``extra`` line, rev and url from the
    ``[tool.uv.sources]`` entry."""
    _package(package)
    lines = pyproject_text.splitlines(keepends=True)
    _, floor, _, src = _pyproject_lines(lines, package, extra)
    ver = floor["ver"]
    _version(ver, "floor version")
    revs = _TBL_REV.findall(src["tbl"])
    gits = _TBL_GIT.findall(src["tbl"])
    if len(revs) != 1 or len(gits) != 1:
        raise PinError(f"sources entry for {package!r} needs one git and one rev")
    return ver, _sha(revs[0][1], "sources rev"), gits[0]


def set_pyproject_pin(
    text: str, package: str, *, version: str, sha: str, extra: str = "catalyst"
) -> str:
    """``text`` (a pyproject.toml) with the ``extra``'s ``"<package>>=X.Y.Z"``
    floor and the ``[tool.uv.sources]`` rev of ``package`` moved to
    ``version`` / ``sha``, and that sources line's trailing comment replaced by
    ``# v<version>``; every other byte kept."""
    _package(package)
    _ident(extra, "extra")
    _version(version)
    _sha(sha)
    old_ver, _, _ = current_pin(text, package, extra=extra)
    old = _parse(text, "pyproject.toml")
    lines = text.splitlines(keepends=True)
    fi, floor, si, src = _pyproject_lines(lines, package, extra)
    lines[fi] = f"{floor['head']}{package}>={version}{floor['tail']}"
    tail = _TAIL.fullmatch(src["tail"])
    if tail is None:
        raise PinError(f"sources line for {package!r} has trailing text, not a comment")
    ws = tail["ws"] if "#" in src["tail"] else "  "
    tbl = _TBL_REV.sub(lambda m: f"{m[1]}{sha}{m[3]}", src["tbl"])
    lines[si] = f"{src['head']}{tbl}{ws}# v{version}{tail['eol']}"
    out = "".join(lines)
    expect = copy.deepcopy(old)
    try:
        reqs = expect["project"]["optional-dependencies"][extra]
        reqs[reqs.index(f"{package}>={old_ver}")] = f"{package}>={version}"
        expect["tool"]["uv"]["sources"][package]["rev"] = sha
    except (KeyError, ValueError, TypeError) as exc:
        raise PinError("pyproject.toml does not hold the pin where parsed") from exc
    if _parse(out, "edited pyproject.toml") != expect:
        raise PinError("pyproject edit changed more than the floor and the rev")
    return out


# --- uv.lock --------------------------------------------------------------


def _lock_block(lines: list[str], package: str) -> tuple[int, int]:
    """``(version line index, source line index)`` of the one ``[[package]]``
    block named ``package`` (PEP 503 normalised)."""
    want = _norm(package)
    hits: list[tuple[int | None, int | None]] = []
    active = False
    name: str | None = None
    ver: int | None = None
    srcl: int | None = None

    def close() -> None:
        if active and name is not None and _norm(name) == want:
            hits.append((ver, srcl))

    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        h = _HEADER.fullmatch(bare)
        if h is not None:
            close()
            active = h[1] == "[[" and h[2] == "package"
            name, ver, srcl = None, None, None
            continue
        if not active:
            continue
        if (
            m := re.fullmatch(r"\s*name\s*=\s*\"([^\"]*)\"\s*(?:#.*)?", bare)
        ) is not None:
            name = m[1]
        elif _LOCK_VERSION.fullmatch(line) is not None:
            ver = i
        elif re.match(r"\s*source\s*=", bare) is not None:
            srcl = i
    close()
    if not hits:
        raise PinError(f"uv.lock has no package block for {package!r}")
    if len(hits) > 1:
        raise PinError(f"uv.lock has more than one package block for {package!r}")
    v, s = hits[0]
    if v is None or s is None:
        raise PinError(f"uv.lock block for {package!r} lacks a version or source line")
    return v, s


def _subst_doc(node: Any, want: str, url: str, old: str, new: str) -> Any:
    """``node`` with every dict naming ``want`` re-pinned: its ``git`` rev
    moved from ``old`` to ``new``; a ``version`` key of the package entry is
    set by the caller."""
    if isinstance(node, dict):
        out = {k: _subst_doc(v, want, url, old, new) for k, v in node.items()}
        n, g = out.get("name"), out.get("git")
        if isinstance(n, str) and _norm(n) == want and isinstance(g, str):
            out["git"] = g.replace(f"{url}?rev={old}", f"{url}?rev={new}").replace(
                f"#{old}", f"#{new}"
            )
        return out
    if isinstance(node, list):
        return [_subst_doc(v, want, url, old, new) for v in node]
    return node


def set_lock_pin(
    text: str, package: str, *, version: str, sha: str, old_sha: str
) -> str:
    """``text`` (a uv lockfile) with the ``[[package]]`` block of ``package``
    moved to ``version`` / ``sha`` (its ``version`` and ``source`` lines) and
    every dependency line of ``package`` referencing ``<url>?rev=<old_sha>``
    re-pointed; every other byte kept, other packages' lines included."""
    _package(package)
    _version(version)
    _sha(sha)
    _sha(old_sha, "old_sha")
    old = _parse(text, "uv.lock")
    lines = text.splitlines(keepends=True)
    vi, si = _lock_block(lines, package)
    src = _LOCK_SOURCE.fullmatch(lines[si].rstrip("\r\n"))
    if src is None or src[2] != old_sha or src[3] != old_sha:
        raise PinError(f"uv.lock block for {package!r} does not carry rev {old_sha}")
    url = src[1]
    want = _norm(package)
    needle = f"{url}?rev={old_sha}"
    vm = _LOCK_VERSION.fullmatch(lines[vi])
    assert vm is not None  # matched in _lock_block
    lines[vi] = f"{vm[1]}{version}{vm[3]}"
    lines[si] = (
        lines[si].replace(needle, f"{url}?rev={sha}").replace(f"#{old_sha}", f"#{sha}")
    )
    for i, line in enumerate(lines):
        if i in (vi, si) or needle not in line:
            continue
        m = _NAME_IN_LINE.search(line)
        if m is not None and _norm(m[1]) == want:
            lines[i] = line.replace(needle, f"{url}?rev={sha}")
    out = "".join(lines)
    expect = _subst_doc(old, want, url, old_sha, sha)
    pkgs = [
        p
        for p in expect.get("package", [])
        if isinstance(p, dict) and _norm(str(p.get("name"))) == want
    ]
    if len(pkgs) != 1:
        raise PinError("uv.lock package list does not match its blocks")
    pkgs[0]["version"] = version
    pkgs[0]["source"] = {"git": f"{url}?rev={sha}#{sha}"}
    new = _parse(out, "edited uv.lock")
    if new != expect:
        raise PinError("uv.lock edit changed more than the pinned package's lines")
    for p in new["package"]:
        if _norm(str(p.get("name"))) == want and (
            p.get("version") != version
            or sha not in str(p.get("source"))
            or (old_sha != sha and old_sha in str(p.get("source")))
        ):
            raise PinError("uv.lock package entry did not take the new pin")
    return out


# --- deploy/group_vars/all.yml -------------------------------------------


def set_group_vars_floor(text: str, key: str, version: str) -> str:
    """``text`` (group_vars YAML) with the one ``<key>: "X.Y.Z"`` line set to
    ``version``; every other byte kept. Edited as text, never parsed with a
    YAML library, so comments and order survive."""
    _ident(key, "key")
    _version(version)
    line_re = re.compile(rf"{re.escape(key)}: \"[^\"\r\n]*\"")
    lines = text.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if line_re.fullmatch(ln.rstrip("\r\n"))]
    if not hits:
        raise PinError(f'group_vars has no {key}: "..." line')
    if len(hits) > 1:
        raise PinError(f"group_vars has {len(hits)} {key} lines (want exactly 1)")
    i = hits[0]
    eol = lines[i][len(lines[i].rstrip("\r\n")) :]
    new_lines = list(lines)
    new_lines[i] = f'{key}: "{version}"{eol}'
    out = "".join(new_lines)
    got = out.splitlines(keepends=True)
    diff = [j for j, (a, b) in enumerate(zip(lines, got, strict=True)) if a != b]
    if len(got) != len(lines) or diff not in ([i], []):
        raise PinError("group_vars edit changed more than the one line")
    return out


# --- tests/test_coverage_job.py anchor literals --------------------------


def _literal(tree: ast.Module, name: str) -> ast.Constant:
    hits: list[ast.Constant] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                raise PinError(f"{name} is not a plain string literal")
            hits.append(value)
    if len(hits) != 1:
        raise PinError(f"module has {len(hits)} top-level {name} assignments (want 1)")
    return hits[0]


def set_anchor_literals(
    text: str,
    *,
    key: str,
    engine: str,
    key_name: str = "JO482106_KEY",
    engine_name: str = "JO482106_ENGINE",
) -> str:
    """``text`` (a Python module) with the module-level string literals
    ``key_name`` / ``engine_name`` set to ``key`` (64 lowercase hex) and
    ``engine`` (``X.Y.Z``); comments and every other byte kept."""
    if not isinstance(key, str) or _KEY64.fullmatch(key) is None:
        raise PinError(f"anchor key {key!r} is not 64 lowercase hex")
    _version(engine, "engine")
    _ident(key_name, "key_name")
    _ident(engine_name, "engine_name")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PinError(f"anchor module does not parse: {exc}") from exc
    spots: list[tuple[ast.Constant, str]] = [
        (_literal(tree, key_name), key),
        (_literal(tree, engine_name), engine),
    ]
    lines = io.StringIO(text, newline="\n").readlines()
    # Right-to-left so earlier column offsets stay valid on a shared line.
    for const, value in sorted(
        spots, key=lambda s: (s[0].lineno, s[0].col_offset), reverse=True
    ):
        if const.end_lineno != const.lineno or const.end_col_offset is None:
            raise PinError("anchor literal spans several lines")
        raw = lines[const.lineno - 1].encode()
        lines[const.lineno - 1] = (
            raw[: const.col_offset]
            + f'"{value}"'.encode()
            + raw[const.end_col_offset :]
        ).decode()
    out = "".join(lines)
    try:
        new = ast.parse(out)
    except SyntaxError as exc:
        raise PinError(f"edited anchor module does not parse: {exc}") from exc
    if len(new.body) != len(tree.body):
        raise PinError("anchor edit changed the number of top-level nodes")
    for (const, _), (name, value) in zip(
        spots, [(key_name, key), (engine_name, engine)], strict=True
    ):
        if _literal(new, name).value != value:
            raise PinError(f"{name} did not take the new value")
        _literal(new, name).value = const.value  # compare the rest as it was
    if [ast.dump(n) for n in new.body] != [ast.dump(n) for n in tree.body]:
        raise PinError("anchor edit changed more than the two literals")
    return out


# --- coherence -----------------------------------------------------------


def pin_is_coherent(
    pyproject_text: str,
    lock_text: str,
    group_vars_text: str,
    package: str,
    *,
    version: str,
    sha: str,
    key: str = "autocatpath_min_version",
) -> bool:
    """Whether all three texts already carry ``version`` and ``sha`` for
    ``package``: the pyproject floor and rev, the lock block's version and
    source rev, the group_vars floor. A text whose pin is missing or
    malformed is not coherent (``False``); an invalid ``version`` / ``sha``
    argument still raises ``PinError``."""
    _package(package)
    _version(version)
    _sha(sha)
    _ident(key, "key")
    try:
        floor, rev, _ = current_pin(pyproject_text, package)
        if (floor, rev) != (version, sha):
            return False
        lines = lock_text.splitlines(keepends=True)
        vi, si = _lock_block(lines, package)
        vm = _LOCK_VERSION.fullmatch(lines[vi])
        sm = _LOCK_SOURCE.fullmatch(lines[si].rstrip("\r\n"))
        if vm is None or sm is None:
            return False
        if vm[2] != version or sm[2] != sha or sm[3] != sha:
            return False
    except PinError:
        return False
    line = f'{key}: "{version}"'
    return sum(1 for ln in group_vars_text.splitlines() if ln == line) == 1


# ═══ The job ═══════════════════════════════════════════════════════════════

NAME = "pin_bump"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The pin-bump node's todo: it carries meta.pin_bump.
        "todo_id": {"type": "integer"},
        # The release node whose meta.repo_release names the version to pin.
        "release_todo": {"type": "integer"},
        # The dependency's profile name; its ``pinner`` must be this job.
        "pin_repo": {"type": "string"},
    },
    "required": ["todo_id", "release_todo", "pin_repo"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Move precis's pin of a released second-repo engine, mechanically (no "
    "LLM): check the release record against the tag oracle and a source "
    "mirror, edit pyproject.toml, uv.lock and deploy/group_vars/all.yml as "
    "text, derive the coverage anchor key with the new engine installed "
    "credential-free, commit the four files on slice/<id>, run the pin "
    "tests, then mint the land_slice for the branch and succeed (a no-op "
    "closes its node itself). Refused while PRECIS_FACTORY=off, for a "
    "repo whose profile names another pinner, and for a repo that is not "
    "open, and held (holding:) while another pin bump is in flight."
)

#: ``meta.attempt_free`` of this job's attempt-free failures.
ATTEMPT_FREE_MARK = "infra-pin-bump"

#: The wall-deadline kill's verdict: attempt-free, nothing to re-land.
DEADLINE_KILL_META: dict[str, Any] = {
    _rs.ATTEMPT_FREE_KEY: ATTEMPT_FREE_MARK,
    "red_class": _rl.TIMED_OUT_CLASS,
}

#: ``job.meta`` key naming why a failed job needs the coordinator, not a
#: blind retry (``repo_release``'s, which ``plan_dispatch`` reads).
COORDINATOR_ASK_KEY = _rr.COORDINATOR_ASK_KEY

#: ``todo.meta`` key of the pin record.
RECORD_KEY = "pin_bump"

#: Handle phases.
RESOLVE_PHASE = "resolve"
DERIVE_PHASE = "derive"
VERIFY_PHASE = "verify"
HANDOFF_PHASE = "handoff"
#: Seconds the final phase waits before its first poll (due at once).
HANDOFF_PARK_S = 0
#: Newest job rows scanned per type by the serialise check.
SCAN_LIMIT = 50

#: The four files a pin bump commits.
PYPROJECT_PATH = "pyproject.toml"
LOCK_PATH = "uv.lock"
GROUP_VARS_PATH = "deploy/group_vars/all.yml"
ANCHOR_PATH = "tests/test_coverage_job.py"
PIN_PATHS: tuple[str, ...] = (PYPROJECT_PATH, LOCK_PATH, GROUP_VARS_PATH, ANCHOR_PATH)
#: The deploy floor key and the anchor config the key is derived from.
GROUP_VARS_KEY = "autocatpath_min_version"
ANCHOR_CONFIG_NAME = "JO482106_CONFIG"
#: Per file, the most lines the commit may add and the most it may remove.
MAX_FILE_LINES = 8
#: The anchor pin test the verify run adds to the two coherence test files.
ANCHOR_TEST = f"{ANCHOR_PATH}::test_anchor_key_pins_jo482106s_normalised_key"
VERIFY_TESTS: tuple[str, ...] = (
    "tests/test_coverage_pin_lock.py",
    "tests/test_deploy_wheelhouse_floor.py",
    ANCHOR_TEST,
)

#: Files under the run dir (outside the tree).
DERIVE_SCRIPT, DERIVE_LOG, DERIVE_EXIT = (
    "pin_derive.sh",
    "pin_derive.log",
    "pin_derive.exit",
)
VERIFY_SCRIPT, VERIFY_LOG, VERIFY_EXIT = (
    "pin_verify.sh",
    "pin_verify.log",
    "pin_verify.exit",
)
DERIVE_PY = "pin_derive.py"
RESULT_FILE = "pin_result.json"
VENV_DIR = "pin_venv"
#: The verify run's own result file and venv: it re-derives into these, so
#: nothing the derive run left behind is ever read back as the verify's.
VERIFY_RESULT_FILE = "pin_verify_result.json"
VERIFY_VENV_DIR = "pin_verify_venv"
#: Seconds between the TERM and the KILL to a finished phase's process group.
REAP_GRACE_S = 0.2
INDEX_FILE = "pin.index"
_RESULT_MAX = 1024

#: ``red_class`` values besides the ones :mod:`repo_land` defines.
FETCH_CLASS = _rr.FETCH_CLASS
TAG_ORACLE_CLASS = _rr.TAG_ORACLE_CLASS
READ_CLASS = _rr.READ_CLASS
TREE_CLASS = "pin-tree"
RECORD_CLASS = "pin-record"
SOURCE_CLASS = "pin-source"
TRANSPORT_CLASS = "pin-transport"
HANDOFF_CLASS = "pin-handoff"

#: The script steps whose output the transport classifier may read.
TRANSPORT_STEPS: frozenset[str] = frozenset({"lock", "sync"})

#: What uv's registry and git transport failures look like in a log.
_TRANSPORT_RE = re.compile(
    r"error sending request|connection (?:refused|reset|closed|timed out)"
    r"|operation timed out|timed out|dns error|failed to lookup"
    r"|temporary failure in name resolution|name or service not known"
    r"|could not resolve host|network is unreachable|tcp connect error"
    r"|(?:http )?status(?: code)?:? \(?5\d\d|returned error: 5\d\d"
    r"|\b50[0234]\b[ :]+(?:bad gateway|service unavailable|gateway time-?out"
    r"|internal server error)",
    re.IGNORECASE,
)

#: The worker-written script of the derive step (a constant, not a file in
#: the repo tree): argv are the target version, the anchor module, the
#: config's name and the result file. It prints nothing and writes
#: ``{"key", "engine"}`` as JSON.
DERIVE_SCRIPT_PY = """\
import ast
import json
import sys


def main(version, anchor_file, name, result_file):
    import autocatpath

    if autocatpath.__version__ != version:
        raise SystemExit(
            "pin_bump: installed autocatpath is "
            + str(autocatpath.__version__)
            + ", not the target "
            + version
        )
    with open(anchor_file, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            found.append(value)
    if len(found) != 1:
        raise SystemExit("pin_bump: " + name + " is not assigned exactly once")
    config = ast.literal_eval(found[0])
    from precis_pathway import coverage_job, runner

    key = runner.coverage_anchor_key(coverage_job.with_defaults(config))
    with open(result_file, "w", encoding="utf-8") as fh:
        json.dump({"key": key, "engine": autocatpath.__version__}, fh)


main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
"""


# ── Admission ──────────────────────────────────────────────────────


def _refusal(store: Any, params: dict[str, Any]) -> str | None:
    """Why this job may not run (td659522), or ``None``: the factory is on,
    the pin-bump todo is live, the params are exactly the three, ``pin_repo``
    names a profile whose ``pinner`` is this job and that ``factory.
    open_repos`` lists, and ``release_todo`` is a live or done todo. Read at
    submit and again when the parked job wakes."""
    lead = "pin_bump: "
    if _rs.factory_off():
        return lead + "the factory is off (PRECIS_FACTORY=off); next: unset it"
    reason = _rs._todo_rejection(store, params.get("todo_id"))
    if reason is not None:
        return reason.replace("run_slice: ", lead, 1)
    extra = sorted(set(params) - set(PARAMS_SCHEMA["properties"]))
    if extra:
        return (
            lead + f"unknown params {extra}; next: pass todo_id, release_todo, pin_repo"
        )
    release = params.get("release_todo")
    if not isinstance(release, int) or isinstance(release, bool) or release <= 0:
        return (
            lead + "params.release_todo must be a positive integer (the release node)"
        )
    repo = params.get("pin_repo")
    if not isinstance(repo, str) or not repo.strip():
        return (
            lead + "the job has no params.pin_repo; next: pass the repo whose pin moves"
        )
    repo = repo.strip()
    profile = _lanes.repo_profile(repo)
    if profile is None or profile.pinner != NAME:
        return (
            lead
            + f"the profile for {repo!r} names no pinner; next: move the pin by hand"
        )
    if repo not in _lanes.open_repos(store):
        return (
            lead
            + f"factory.open_repos does not list {repo!r}; next: move the pin by hand"
        )
    if not profile.remote:
        return (
            lead + f"the profile for {repo!r} has no remote; next: move the pin by hand"
        )
    if store is None:
        return None
    if store.get_ref(kind="todo", id=release) is None:
        return (
            lead + f"release node {release} not found; next: pass the release node's id"
        )
    status = _rs._terminal_status(store, release)
    if status is not None and status != "done":
        return lead + (
            f"release node {release} is closed (STATUS:{status}); next: wait for a "
            "live or done release node"
        )
    return None


def _recent_jobs(store: Any, job_type: str) -> list[tuple[int, int | None, bool]]:
    """``(job id, params.todo_id, terminal)`` of the newest
    :data:`SCAN_LIMIT` ``job_type`` job rows (seam)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id, j.meta -> 'params' ->> 'todo_id' FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta ->> 'job_type' = %s
             ORDER BY j.ref_id DESC
             LIMIT %s
            """,
            (job_type, SCAN_LIMIT),
        ).fetchall()
    out: list[tuple[int, int | None, bool]] = []
    for jid, tid in rows:
        terminal = any(
            t.prefix == "STATUS" and str(t.value) in JOB_TERMINAL_STATUSES
            for t in store.tags_for(int(jid))
        )
        out.append((int(jid), int(tid) if str(tid).isdigit() else None, terminal))
    return out


def _other_pin_work(store: Any, todo_id: int) -> list[str]:
    """The live pin work of a DIFFERENT node: a non-terminal ``pin_bump``
    job, or a non-terminal ``land_slice`` job whose todo carries a
    ``meta.pin_bump`` record."""
    held: list[str] = []
    for jid, tid, terminal in _recent_jobs(store, NAME):
        if not terminal and tid != todo_id:
            held.append(f"pin_bump jo{jid} (td{tid})")
    for jid, tid, terminal in _recent_jobs(store, "land_slice"):
        if terminal or tid is None or tid == todo_id:
            continue
        if _stored(store, tid) is not None:
            held.append(f"land_slice jo{jid} (td{tid})")
    return held


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Refuses everything :func:`_refusal` does, and holds (a ``holding:``
    refusal, so no job is minted; the dispatcher notes it and tries the next
    tick) while another node's pin bump is in flight: a live ``pin_bump`` job,
    or a live ``land_slice`` of a todo carrying a pin record. Two bumps would
    edit the same four files. Reads the newest :data:`SCAN_LIMIT` rows per
    job type; a store that cannot be read holds too."""
    del gripe_id
    refusal = _refusal(store, params)
    if refusal is not None or store is None:
        return refusal
    try:
        held = _other_pin_work(store, int(params["todo_id"]))
    except Exception:
        log.warning("pin_bump: serialise scan failed", exc_info=True)
        return "holding: the live pin work could not be read; next: retried next tick"
    if not held:
        return None
    shown = ", ".join(held[:4]) + (", ..." if len(held) > 4 else "")
    return (
        f"holding: another pin bump is in flight ({shown}); next: this node "
        "waits until it lands"
    )


# ── Failures and the record ────────────────────────────────────────


def _fail(
    ctx: Any,
    todo_id: int,
    reason: str,
    *,
    handoff: str,
    attempt_free: str | None = None,
    ask: str | None = None,
) -> None:
    """The failure recorder, ``repo_release._fail``'s twin: a ``handoff:``
    comment on the node, never its claim, ``STATUS`` or tags. ``attempt_free``
    is the red's class when the host or a remote, not the input, is at fault;
    ``ask`` is a reason only the coordinator can answer
    (:data:`COORDINATOR_ASK_KEY`)."""
    try:
        _rs._append_comment(ctx.store, todo_id, f"handoff: {handoff}")
    except Exception:  # pragma: no cover - the job outcome still lands
        log.warning("pin_bump: handoff comment on td%d failed", todo_id)
    suffix = ""
    if ask is not None:
        ctx.set_meta(**{COORDINATOR_ASK_KEY: ask})
    if attempt_free is not None:
        ctx.set_meta(
            **{_rs.ATTEMPT_FREE_KEY: ATTEMPT_FREE_MARK}, red_class=attempt_free
        )
        suffix = f" (no attempt: {attempt_free})"
    ctx.set_meta(compute_handle_released=True)
    ctx.record_failure(f"pin_bump: {reason}{suffix}")


def _stamp(ctx: Any, todo_id: int, record: dict[str, Any] | None) -> bool:
    """Write (or, with ``None``, clear) the pin record. ``False`` when the
    store refuses."""
    try:
        ctx.store.stamp_ref_meta(todo_id, {RECORD_KEY: record})
    except Exception:
        log.warning("pin_bump: record stamp on td%d failed", todo_id, exc_info=True)
        return False
    return True


def _stored(store: Any, todo_id: int) -> dict[str, Any] | None:
    ref = store.get_ref(kind="todo", id=todo_id)
    record = (getattr(ref, "meta", None) or {}).get(RECORD_KEY) if ref else None
    return record if isinstance(record, dict) else None


# ── Git and the tree ───────────────────────────────────────────────


def _tree_path(root: Path, todo_id: int) -> Path:
    return _rs.worktrees_dir(root) / f"pin-td{todo_id}"


def _branch_exists(root: Path, branch: str) -> bool:
    return _head(root, branch) != ""


def _head(root: Path, branch: str) -> str:
    out = _rl._read(root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    return _rl._hex(out)


def _git_raw(
    args: list[str],
    cwd: Path,
    *,
    stdin: bytes | None = None,
    index: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """``git <args>`` with bytes in and out, under the same scrubbed env and
    hooks-off args as ``land_slice._git``; ``index`` names a private index
    file. Never the credential."""
    env = _mirror.local_env(_ls.land_env())
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["git", *_mirror.TRUSTED_ARGS, *args],
        cwd=str(cwd),
        env=env,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=300,
    )


def _oid(proc: subprocess.CompletedProcess[bytes]) -> str:
    if proc.returncode != 0:
        return ""
    return _rl._hex(proc.stdout.decode("utf-8", "replace"))


def _checkout_tree(root: Path, todo_id: int, branch: str) -> Path | None:
    """The slice worktree ``pin-td<id>`` freshly checked out on ``branch``
    (any tree there removed first, the branch kept). ``None`` when git
    fails."""
    tree = _tree_path(root, todo_id)
    if tree.exists():
        _ls._git(["worktree", "remove", "--force", str(tree)], root)
        shutil.rmtree(tree, ignore_errors=True)
    _ls._git(["worktree", "prune"], root)
    tree.parent.mkdir(parents=True, exist_ok=True)
    add = _ls._git(["worktree", "add", str(tree), branch], root)
    return tree if add.returncode == 0 else None


def _drop_made(root: Path, todo_id: int) -> None:
    """Remove the worktree and branch this job made; never raises."""
    try:
        _rl._drop_slice(root, _tree_path(root, todo_id), _rs.slice_branch(todo_id))
    except Exception:  # pragma: no cover - cleanup is best effort
        log.warning(
            "pin_bump: dropping the tree of td%d failed", todo_id, exc_info=True
        )


def _clear_building(ctx: Any, todo_id: int) -> None:
    """Clear a ``building`` record (a committed one is left alone)."""
    try:
        if (_stored(ctx.store, todo_id) or {}).get("state") == "building":
            _stamp(ctx, todo_id, None)
    except Exception:  # pragma: no cover - best effort
        log.warning("pin_bump: clearing the record of td%d failed", todo_id)


# ── Reading the four files ─────────────────────────────────────────


class _ReadError(Exception):
    """A file could not be read at the tip; ``missing`` says git answered
    (absent or not UTF-8) rather than failed."""

    def __init__(self, path: str, missing: bool) -> None:
        super().__init__(path)
        self.path = path
        self.missing = missing


def _read_files(root: Path, tip: str) -> dict[str, str]:
    """The four files at ``tip``, byte for byte (``repo_release._show``)."""
    out: dict[str, str] = {}
    for path in PIN_PATHS:
        text = _rr._show(root, tip, path)
        if text is None:
            gone = _rl._read(root, ["cat-file", "-e", f"{tip}:{path}"]) is None
            raise _ReadError(path, gone)
        out[path] = text
    return out


def _lock_pin(lock_text: str, package: str) -> tuple[str, str]:
    """``(version, sha)`` of the lock block of ``package``."""
    _package(package)
    lines = lock_text.splitlines(keepends=True)
    vi, si = _lock_block(lines, package)
    vm = _LOCK_VERSION.fullmatch(lines[vi])
    sm = _LOCK_SOURCE.fullmatch(lines[si].rstrip("\r\n"))
    if vm is None or sm is None or sm[2] != sm[3]:
        raise PinError(f"uv.lock block for {package!r} is not a sha-pinned git source")
    return vm[2], sm[2]


def _anchor_engine(text: str) -> str | None:
    try:
        value = _literal(ast.parse(text), "JO482106_ENGINE").value
    except (PinError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _edited(
    files: dict[str, str], package: str, version: str, sha: str
) -> dict[str, str]:
    """The three edited texts for the pin move. A file already at the target
    is kept byte for byte (a half-moved pin is finished, not rewritten)."""
    py, lock = files[PYPROJECT_PATH], files[LOCK_PATH]
    floor, rev, _ = current_pin(py, package)
    lock_version, lock_sha = _lock_pin(lock, package)
    return {
        PYPROJECT_PATH: py
        if (floor, rev) == (version, sha)
        else set_pyproject_pin(py, package, version=version, sha=sha),
        LOCK_PATH: lock
        if (lock_version, lock_sha) == (version, sha)
        else set_lock_pin(lock, package, version=version, sha=sha, old_sha=lock_sha),
        GROUP_VARS_PATH: set_group_vars_floor(
            files[GROUP_VARS_PATH], GROUP_VARS_KEY, version
        ),
    }


def _group_vars_floor(text: str) -> str | None:
    """The one ``autocatpath_min_version: "X.Y.Z"`` value of the group_vars
    text, or ``None`` when there is not exactly one such line."""
    line_re = re.compile(rf"{re.escape(GROUP_VARS_KEY)}: \"([^\"\r\n]*)\"")
    hits = [m[1] for ln in text.splitlines() if (m := line_re.fullmatch(ln))]
    return hits[0] if len(hits) == 1 else None


def _dep_view(text: str | None) -> dict[str, Any] | None:
    """What a lock text-edit depends on in a dependency's ``pyproject.toml``;
    ``None`` when it is missing or does not parse."""
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    return {
        "dependencies": project.get("dependencies"),
        "optional-dependencies": project.get("optional-dependencies"),
        "requires-python": project.get("requires-python"),
        "build-system": data.get("build-system"),
    }


# ── The release record ─────────────────────────────────────────────


@dataclass(frozen=True)
class _Release:
    package: str
    version: str
    sha: str
    tag: str


def _read_release(
    store: Any, release_todo: int, profile: _lanes.RepoProfile
) -> _Release | str:
    """The release the node's ``meta.repo_release`` names, or why it cannot
    be pinned (never echoing the record's own text)."""
    ref = store.get_ref(kind="todo", id=release_todo)
    record = (getattr(ref, "meta", None) or {}).get(_rr.RECORD_KEY) if ref else None
    if not isinstance(record, dict):
        return f"release node td{release_todo} carries no release record"
    if record.get("state") != "tagged":
        return f"the release on td{release_todo} is not tagged (state is not 'tagged')"
    if record.get("repo") != profile.name:
        return (
            f"the release on td{release_todo} is for another repo than {profile.name!r}"
        )
    version, tag = record.get("version"), record.get("tag")
    sha = _rl._hex(record.get("sha"))
    try:
        if not isinstance(version, str):
            raise ReleaseError("version")
        _split_version(version)
        want = _rr.tag_name(profile.tag_prefix, version)
    except ReleaseError:
        return f"the release on td{release_todo} carries no X.Y.Z version"
    if tag != want or not sha:
        return f"the release on td{release_todo} carries no matching tag and 40-hex sha"
    package = record.get("package")
    if profile.pin is not None and package != profile.pin[1]:
        return (
            f"the release on td{release_todo} is for another package than the "
            f"{profile.name!r} profile pins"
        )
    if not isinstance(package, str):
        return f"the release on td{release_todo} names no package"
    try:
        _package(package)
    except PinError:
        return f"the release on td{release_todo} names no plain package"
    return _Release(package=package, version=version, sha=sha, tag=want)


def _precis_tip(root: Path) -> str | None:
    """The fetched ``origin/main`` tip of the precis clone (the credentialed
    fetch of ``run_slice.fetch_base_tip``), ``None`` on any failure."""
    profile = _lanes.repo_profile(_lanes.DEFAULT_REPO)
    url = _mirror.github_url(root, env=_ls.land_env())
    if profile is None or url is None:
        return None
    return _rs.fetch_base_tip(replace(profile, remote=url), root)


# ── Submit ─────────────────────────────────────────────────────────


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: guards only, no outward write and no git.
    Returns a parked ``phase: "resolve"`` handle (due at once), or ``None``
    after failing the job."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    refusal = _refusal(ctx.store, params)
    if refusal is not None:
        ctx.record_failure(refusal)
        return None
    try:
        _rs.slice_root()
    except RuntimeError as exc:
        ctx.record_failure(f"pin_bump: {exc}")
        return None
    handle: dict[str, Any] = {
        "op": NAME,
        "repo": _lanes.DEFAULT_REPO,
        "pin_repo": str(params["pin_repo"]).strip(),
        "release_todo": params["release_todo"],
        "phase": RESOLVE_PHASE,
    }
    _rl.park_until(ctx, handle, 0)
    return handle


# ── The run ────────────────────────────────────────────────────────


class _Run:
    """One poll's working state; its failure helpers drop what this job made
    and return ``True`` (the job is terminal)."""

    def __init__(
        self, ctx: Any, handle: dict[str, Any], params: dict[str, Any], root: Path
    ) -> None:
        self.ctx = ctx
        self.handle = handle
        self.params = params
        self.root = root
        self.todo_id: int = params["todo_id"]

    def _end(self, why: str, handoff: str, **kw: str | None) -> bool:
        if self.handle.get("made"):
            _drop_made(self.root, self.todo_id)
        _clear_building(self.ctx, self.todo_id)
        _sweep(self.todo_id)
        _fail(
            self.ctx,
            self.todo_id,
            why,
            handoff=f"pin bump FAILED: {handoff}",
            **kw,
        )
        return True

    def charged(self, why: str, handoff: str | None = None) -> bool:
        return self._end(why, handoff or why)

    def ask(self, why: str) -> bool:
        """Charged, and only the coordinator can answer."""
        return self._end(why, why, ask=why)

    def free(self, why: str, handoff: str, red_class: str) -> bool:
        return self._end(why, handoff, attempt_free=red_class)

    def run_dir(self) -> Path:
        return _rs.run_dir(self.todo_id)


def _sweep(todo_id: int) -> None:
    """Remove both venvs and both result files of the job's run dir. The
    scripts and logs stay for the operator. Never raises, never creates the
    dir."""
    try:
        rdir = _rs.ops_root() / f"td{todo_id}"
        for name in (VENV_DIR, VERIFY_VENV_DIR):
            shutil.rmtree(rdir / name, ignore_errors=True)
        for name in (RESULT_FILE, VERIFY_RESULT_FILE):
            (rdir / name).unlink(missing_ok=True)
    except Exception:  # pragma: no cover - cleanup is best effort
        log.warning("pin_bump: sweeping the run dir of td%d failed", todo_id)


def _reap_group(handle: dict[str, Any]) -> None:
    """A phase's exit file is there: TERM, a short grace, then KILL its whole
    process group, so nothing the phase started can write results or an exit
    file after the worker has looked. A group that is already gone is fine;
    the worker's own group is never signalled."""
    pgid = _rs._handle_pgid(handle)
    if pgid <= 1 or _rl._pid(handle.get("pid")) == os.getpid():
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            _rs._signal_group(pgid, sig)
        except ProcessLookupError:
            return
        except OSError:
            log.warning("pin_bump: signalling group %d failed", pgid, exc_info=True)
            return
        if sig == signal.SIGTERM:
            time.sleep(REAP_GRACE_S)


def _detached_env(mirror: Path, remote: str, venv: Path) -> dict[str, str]:
    """The env of a detached run: the gate env (no credential, no PRECIS_*),
    the dependency's remote rewritten to the job's mirror in both URL forms,
    no system or global git config, no prompts, and the venv outside the
    tree. Nothing here is a credential."""
    precis = _lanes.repo_profile(_lanes.DEFAULT_REPO) or _lanes.RepoProfile(
        name=_lanes.DEFAULT_REPO
    )
    plain = remote.rstrip("/")
    plain = plain[: -len(".git")] if plain.endswith(".git") else plain
    env = _mirror.mirror_ship_env(mirror, _rl.gate_env(precis), plain + ".git")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    return env


def _start_phase(
    run: _Run,
    phase: str,
    script_name: str,
    log_name: str,
    exit_name: str,
    steps: list[tuple[str, str]],
    venv_name: str = VENV_DIR,
) -> bool:
    """Launch the steps detached in the pin tree under the credential-free
    env and put the phase on the handle. ``True`` when the launch failed (the
    job is then terminal)."""
    ctx, handle = run.ctx, run.handle
    rdir = run.run_dir()
    log_path, exit_path = rdir / log_name, rdir / exit_name
    launched = _ls._now()
    handle.update(
        {
            "phase": phase,
            "log": str(log_path),
            "exit_file": str(exit_path),
            "started_at": launched.strftime(_ls._TS_FMT),
            "deadline": launched.timestamp() + _ls.wall_seconds(),
        }
    )
    for key in ("not_before", "pid", "pgid"):
        handle.pop(key, None)
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=handle["deadline"],
    )
    try:
        script = rdir / script_name
        script.write_text(_rl.commands_script(steps), encoding="utf-8")
        exit_path.unlink(missing_ok=True)
        pid = _rs._launch(
            ["/bin/sh", str(script)],
            cwd=_tree_path(run.root, run.todo_id),
            stdin=Path(os.devnull),
            log=log_path,
            exit_file=exit_path,
            env=_detached_env(
                Path(handle["mirror"]), handle["remote"], rdir / venv_name
            ),
        )
    except Exception as exc:
        log.warning(
            "pin_bump: %s launch for td%d failed", phase, run.todo_id, exc_info=True
        )
        return run.free(
            f"{phase} launch failed: {exc}",
            f"the {phase} run did not start ({exc}); retried.",
            _rl.GATE_INFRA_CLASS,
        )
    handle["pid"] = pid
    handle["pgid"] = pid
    _rl.stamp_identity(handle, pid)
    ctx.set_meta(compute_handle=handle, compute_handle_released=False)
    return False


def _record(
    run: _Run,
    release: _Release,
    tip: str,
    *,
    state: str,
    from_version: str,
    from_sha: str,
    commit: str | None,
    noop: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """The ``meta.pin_bump`` record. A no-op names no tree or branch."""
    record: dict[str, Any] = {
        "state": state,
        "repo": run.params["pin_repo"],
        "package": release.package,
        "version": release.version,
        "sha": release.sha,
        "tag": release.tag,
        "from_version": from_version,
        "from_sha": from_sha,
        "commit": commit,
        "base": tip,
        "release_todo": run.params["release_todo"],
        "worktree": None if noop else str(_tree_path(run.root, run.todo_id)),
        "branch": None if noop else _rs.slice_branch(run.todo_id),
        "job": _rl._job_id(run.ctx),
    }
    if noop:
        record["noop"] = True
        record["note"] = note
    return record


def _to_handoff(run: _Run, record: dict[str, Any]) -> None:
    """Swap the handle for the parked final phase."""
    fresh: dict[str, Any] = {
        "op": NAME,
        "repo": _lanes.DEFAULT_REPO,
        "pin_repo": run.params["pin_repo"],
        "release_todo": run.params["release_todo"],
        "phase": HANDOFF_PHASE,
        "commit": record.get("commit"),
        "branch": record.get("branch"),
        "worktree": record.get("worktree"),
        "noop": bool(record.get("noop")),
    }
    run.handle.clear()
    run.handle.update(fresh)
    _rl.park_until(run.ctx, run.handle, HANDOFF_PARK_S)


def _no_op(
    run: _Run, release: _Release, tip: str, floor: str, rev: str, why: str
) -> bool:
    """The pin is already there (or past it): stamp the committed record with
    ``noop`` and park in the final phase, which closes the node."""
    record = _record(
        run,
        release,
        tip,
        state="committed",
        from_version=floor,
        from_sha=rev,
        commit=None,
        noop=True,
        note=why,
    )
    if not _stamp(run.ctx, run.todo_id, record):
        return run.free(
            f"could not stamp the pin record on td{run.todo_id}",
            "the node could not be marked (infrastructure); retried.",
            RECORD_CLASS,
        )
    run.ctx.append_chunk(
        "job_event",
        f"pin_bump: {release.tag} needs no commit ({why}); parked in handoff",
    )
    _to_handoff(run, record)
    return False


# ── Resolve ────────────────────────────────────────────────────────


def _resumable(
    run: _Run, release: _Release, tip: str, record: dict[str, Any] | None
) -> bool:
    """Whether ``record`` is a committed pin bump that is still the one to
    land: same release and package, the commit is still the branch head and
    the only child of the current tip, the tree is there."""
    if record is None or record.get("state") != "committed" or record.get("noop"):
        return False
    branch = _rs.slice_branch(run.todo_id)
    commit = _rl._hex(record.get("commit"))
    same = (
        record.get("repo") == run.params["pin_repo"]
        and record.get("release_todo") == run.params["release_todo"]
        and record.get("package") == release.package
        and record.get("version") == release.version
        and record.get("sha") == release.sha
        and record.get("branch") == branch
        and record.get("worktree") == str(_tree_path(run.root, run.todo_id))
        and record.get("base") == tip
    )
    if not same or not commit or _head(run.root, branch) != commit:
        return False
    out = _rl._read(run.root, ["rev-list", "--parents", "-n", "1", commit])
    return (out or "").split() == [commit, tip] and _tree_path(
        run.root, run.todo_id
    ).is_dir()


def _poll_resolve(run: _Run) -> bool:
    """The first poll: everything from the release record to the derive's
    launch. ``True`` when the job is terminal."""
    ctx, params, root, todo_id = run.ctx, run.params, run.root, run.todo_id
    gap = _refusal(ctx.store, params)
    if gap is not None:
        return run.free(
            f"not admitted ({gap}); not moving the pin",
            "the pin repo was closed to the factory while the bump waited; "
            "it is retried once the repo is open.",
            _rl.CLOSED_CLASS,
        )
    profile = _lanes.repo_profile(str(params["pin_repo"]).strip())
    assert profile is not None  # _refusal checked it
    release = _read_release(ctx.store, params["release_todo"], profile)
    if isinstance(release, str):
        return run.ask(release)
    tip = _precis_tip(root)
    if tip is None:
        return run.free(
            "could not fetch origin/main of precis",
            "origin/main of precis could not be fetched (infrastructure); retried.",
            FETCH_CLASS,
        )
    stored = _stored(ctx.store, todo_id)
    if _resumable(run, release, tip, stored):
        ctx.append_chunk(
            "job_event",
            f"pin_bump: resumes the committed bump of {release.tag} "
            f"({str((stored or {}).get('commit'))[:9]} on {tip[:9]})",
        )
        _to_handoff(run, stored or {})
        return False
    try:
        files = _read_files(root, tip)
    except _ReadError as exc:
        if exc.missing:
            return run.charged(
                f"{exc.path} is missing or not UTF-8 at {tip[:9]}",
                f"{exc.path} is missing or not UTF-8 on origin/main.",
            )
        return run.free(
            f"git could not read {exc.path} at {tip[:9]}",
            f"git could not read {exc.path} (infrastructure); retried.",
            READ_CLASS,
        )
    package = release.package
    try:
        floor, rev, _url = current_pin(files[PYPROJECT_PATH], package)
        _lock_version, lock_sha = _lock_pin(files[LOCK_PATH], package)
    except PinError as exc:
        return run.ask(f"precis does not pin {package!r} where expected: {exc}")
    env = _rr._auth_env()
    at = _mirror.ls_remote_tag(profile.remote, release.tag, env=env)
    if at is None:
        return run.free(
            f"tag {release.tag} could not be read on {profile.name}'s remote",
            f"the remote's tag {release.tag} could not be read (infrastructure); retried.",
            TAG_ORACLE_CLASS,
        )
    if at != release.sha:
        return run.ask(
            f"tag {release.tag} is {'absent' if not at else 'at ' + at[:9]} on "
            f"{profile.name}'s remote, not at the release sha {release.sha[:9]}"
        )
    mirror = _mirror.mirror_dir(root, f"pin-td{todo_id}")
    mtip = _mirror.fetch_source_mirror(
        profile.remote, profile.base, mirror=mirror, env=env
    )
    if mtip is None:
        return run.free(
            f"could not mirror {profile.name}'s {profile.base}",
            f"{profile.name}'s {profile.base} could not be fetched "
            "(infrastructure); retried.",
            SOURCE_CLASS,
        )
    on_base = _rr._on_base(mirror, release.sha, mtip)
    if on_base is None:
        return run.free(
            f"git could not say whether {release.sha[:9]} is on {profile.base}",
            "git could not read the source mirror (infrastructure); retried.",
            READ_CLASS,
        )
    if not on_base:
        return run.ask(
            f"release sha {release.sha[:9]} is not an ancestor of {profile.name}'s "
            f"{profile.base}"
        )
    theirs = _rr._show(mirror, release.sha, "pyproject.toml")
    try:
        project = tomllib.loads(theirs or "").get("project", {})
        named, versioned = project.get("name"), project.get("version")
        same = (
            isinstance(named, str)
            and _norm(named) == _norm(package)
            and versioned == release.version
        )
    except (tomllib.TOMLDecodeError, AttributeError):
        same = False
    if not same:
        return run.ask(
            f"{profile.name}'s pyproject.toml at {release.sha[:9]} does not say "
            f"{package} {release.version}"
        )
    # Direction: the mirror's history judges, never the version strings. The
    # pin never moves down and is never redone.
    if _ls._git(["cat-file", "-e", f"{rev}^{{commit}}"], mirror).returncode != 0:
        return run.ask(
            f"precis's pinned sha {rev[:9]} is not in {profile.name}'s history; "
            "a human decides (a real `uv lock` may be needed)"
        )
    release_behind = _rr._on_base(mirror, release.sha, rev)
    pin_behind = _rr._on_base(mirror, rev, release.sha)
    if release_behind is None or pin_behind is None:
        return run.free(
            f"git could not order {rev[:9]} and {release.sha[:9]}",
            "git could not read the source mirror (infrastructure); retried.",
            READ_CLASS,
        )
    if rev != release.sha:
        if release_behind:
            return _no_op(
                run,
                release,
                tip,
                floor,
                rev,
                f"the pin {floor} at {rev[:9]} is already past {release.version}",
            )
        if not pin_behind:
            return run.ask(
                f"the pinned sha {rev[:9]} and release {release.version} at "
                f"{release.sha[:9]} are not ancestors of each other; a human decides"
            )
    if compare_versions(release.version, floor) < 0:
        return run.ask(
            f"release {release.version} is below precis's floor {floor} but "
            f"{release.sha[:9]} is not behind the pinned sha {rev[:9]}"
        )
    try:
        deploy_floor = _group_vars_floor(files[GROUP_VARS_PATH])
        diverged = (
            deploy_floor is not None
            and compare_versions(deploy_floor, release.version) > 0
        )
    except PinError:
        diverged = False  # an odd line is the edit's to refuse
    if diverged:
        return run.ask(
            f"deploy/group_vars/all.yml floors {GROUP_VARS_KEY} at {deploy_floor}, "
            f"above the release {release.version}; the floor is never lowered"
        )
    coherent = pin_is_coherent(
        files[PYPROJECT_PATH],
        files[LOCK_PATH],
        files[GROUP_VARS_PATH],
        package,
        version=release.version,
        sha=release.sha,
    )
    if coherent and _anchor_engine(files[ANCHOR_PATH]) == release.version:
        return _no_op(run, release, tip, floor, rev, "the pin already carries it")
    # The text-edited lock is only right when nothing resolution depends on
    # moved between the sha(s) it carries and the new one.
    new_view = _dep_view(theirs)
    for old in sorted({rev, lock_sha} - {release.sha}):
        if _dep_view(_rr._show(mirror, old, "pyproject.toml")) != new_view:
            return run.ask(
                f"{profile.name}'s dependencies, requires-python or build system "
                f"differ between {old[:9]} and {release.sha[:9]}; the lock needs "
                "a real `uv lock` by a human"
            )
    try:
        edited = _edited(files, package, release.version, release.sha)
    except PinError as exc:
        return run.ask(f"the pin edit was refused: {exc}")
    return _make_tree_and_derive(run, profile, release, tip, mirror, edited, floor, rev)


def _derive_command(
    rdir: Path, root: Path, todo_id: int, version: str, result: Path
) -> str:
    """The step running the worker-written derive script, writing ``result``."""
    return (
        "uv run --frozen --extra catalyst python -I "
        f"{shlex.quote(str(rdir / DERIVE_PY))} {shlex.quote(version)} "
        f"{shlex.quote(str(_tree_path(root, todo_id) / ANCHOR_PATH))} "
        f"{shlex.quote(ANCHOR_CONFIG_NAME)} {shlex.quote(str(result))}"
    )


def _make_tree_and_derive(
    run: _Run,
    profile: _lanes.RepoProfile,
    release: _Release,
    tip: str,
    mirror: Path,
    edited: dict[str, str],
    floor: str,
    rev: str,
) -> bool:
    """Cut the pin tree on ``slice/<id>`` at the tip, write the three edited
    files into it (no commit) and launch the derive."""
    ctx, root, todo_id = run.ctx, run.root, run.todo_id
    branch, tree = _rs.slice_branch(todo_id), _tree_path(root, todo_id)
    if _branch_exists(root, branch) or tree.exists():
        ours = (_stored(ctx.store, todo_id) or {}).get("branch") == branch
        if not ours:
            return run.ask(
                f"{branch} or {tree.name} already exists and this job did not make it"
            )
        _drop_made(root, todo_id)
    building = {
        "state": "building",
        "repo": run.params["pin_repo"],
        "branch": branch,
        "worktree": str(tree),
        "release_todo": run.params["release_todo"],
        "job": _rl._job_id(ctx),
    }
    if not _stamp(ctx, todo_id, building):
        return run.free(
            f"could not stamp the pin record on td{todo_id}",
            "the node could not be marked (infrastructure); retried.",
            RECORD_CLASS,
        )
    tree.parent.mkdir(parents=True, exist_ok=True)
    run.handle["made"] = True
    add = _ls._git(["worktree", "add", "-b", branch, str(tree), tip], root)
    if add.returncode != 0:
        return run.free(
            f"git worktree add failed for the pin tree of td{todo_id}",
            "the pin tree could not be created; retried.",
            TREE_CLASS,
        )
    rdir = run.run_dir()
    try:
        for path, text in edited.items():
            (tree / path).write_bytes(text.encode("utf-8"))
        shutil.rmtree(rdir / VENV_DIR, ignore_errors=True)
        (rdir / RESULT_FILE).unlink(missing_ok=True)
        (rdir / DERIVE_PY).write_text(DERIVE_SCRIPT_PY, encoding="utf-8")
    except OSError as exc:
        return run.free(
            f"writing the pin files failed: {exc}",
            "the pin tree or run dir could not be written (infrastructure); retried.",
            TREE_CLASS,
        )
    run.handle.update(
        {
            "tip": tip,
            "package": release.package,
            "version": release.version,
            "tag": release.tag,
            "sha": release.sha,
            "from_version": floor,
            "from_sha": rev,
            "mirror": str(mirror),
            "remote": profile.remote,
        }
    )
    steps = [
        ("lock", "uv lock --check"),
        ("sync", "uv sync --frozen --extra catalyst"),
        (
            "derive",
            _derive_command(rdir, root, todo_id, release.version, rdir / RESULT_FILE),
        ),
    ]
    if _start_phase(run, DERIVE_PHASE, DERIVE_SCRIPT, DERIVE_LOG, DERIVE_EXIT, steps):
        return True
    ctx.append_chunk(
        "job_event",
        f"pin_bump: {release.tag} on tip {tip[:9]}; derive pid {run.handle['pid']} "
        f"in {tree}",
    )
    return False


# ── Detached phases ────────────────────────────────────────────────


def _poll_detached(run: _Run, label: str, on_green: Any) -> bool:
    """The detached run's phase: keep waiting while it runs, classify its end.
    ``on_green(run)`` takes over on exit 0."""
    handle, todo_id = run.handle, run.todo_id
    exit_code = _rs._read_exit(handle)
    pid = _rl._pid(handle.get("pid"))
    if exit_code is None and pid > 1 and _ls._process_alive(pid):
        deadline = handle.get("deadline")
        past = (
            isinstance(deadline, (int, float))
            and not isinstance(deadline, bool)
            and _ls._now().timestamp() > float(deadline)
        )
        if not past:
            run.ctx.set_meta(compute_handle=handle, compute_handle_released=False)
            return False
        _rl._signal_gate(handle)
        return run.free(
            f"the pin {label} of td{todo_id} passed its wall deadline",
            f"the {label} run passed its wall-clock deadline (infrastructure); retried.",
            _rl.TIMED_OUT_CLASS,
        )
    # The phase is over (or lost): nothing it started may outlive this look.
    _reap_group(handle)
    if exit_code == 0:
        return bool(on_green(run))
    masked = mask_secrets(_rs._read_log(handle)) or ""
    if (
        exit_code is None
        or exit_code == 137
        or _ls.gate_kill_line(masked, exit_code) is not None
    ):
        return run.free(
            f"the pin {label} was killed or lost",
            f"the {label} run was killed from outside (infrastructure); nothing "
            "was committed on main and the bump is retried.",
            _rl.GATE_INFRA_CLASS,
        )
    failed = list(_rl._GATE_FAIL_RE.finditer(masked))
    # Only the uv steps (lock, sync) can fail on transport: the text before
    # the failing step's marker, and only when that step is one of them. A
    # python or pytest run that prints "timed out" is a red, not infrastructure.
    if (
        failed
        and failed[-1][1] in TRANSPORT_STEPS
        and _TRANSPORT_RE.search(masked[: failed[-1].start()])
    ):
        return run.free(
            f"the pin {label} hit a transport failure",
            f"the {label} run could not reach a registry or the source mirror "
            "(infrastructure); retried.",
            TRANSPORT_CLASS,
        )
    command = failed[-1][2] if failed else f"the {label} run"
    excerpt = _ls.gate_red_excerpt(masked)
    handoff = (
        f"`{command}` failed (exit {exit_code}) while moving the pin to "
        f"{handle.get('tag')}. Nothing was landed."
    )
    if excerpt:
        handoff += f"\n\nThe run's output before the verdict:\n{excerpt}"
    run.ctx.set_meta(exit_code=exit_code)
    tail = masked[-_ls._TAIL_CAP :].strip()
    return run.charged(
        f"pin {label} red: {command}" + (f"\n--- log tail ---\n{tail}" if tail else ""),
        handoff,
    )


def _read_result(run: _Run, name: str = RESULT_FILE) -> str | None:
    """The key from the result file ``name``, or ``None`` unless it is a JSON
    object of exactly ``key`` (64 lowercase hex) and ``engine`` (the target
    version). The file is attacker-writable: nothing else in it is ever used."""
    path = run.run_dir() / name
    try:
        if path.stat().st_size > _RESULT_MAX:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"key", "engine"}:
        return None
    key, engine = data["key"], data["engine"]
    if not isinstance(key, str) or _KEY64.fullmatch(key) is None:
        return None
    if engine != run.handle.get("version"):
        return None
    return key


def _numstat_violation(root: Path, tip: str, sha: str) -> str | None:
    out = _rl._read(root, ["diff", "--numstat", "--no-renames", "-z", tip, sha])
    if out is None:
        return "numstat failed"
    for entry in out.split("\0"):
        if not entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) != 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            return "a file changed in a way numstat cannot count"
        if int(parts[0]) > MAX_FILE_LINES or int(parts[1]) > MAX_FILE_LINES:
            return f"{parts[2]} changes more than {MAX_FILE_LINES} lines"
    return None


def _commit_violation(
    root: Path, tip: str, sha: str, expected: dict[str, str]
) -> str | None:
    """Why ``sha`` is not the pin-bump commit on ``tip``, else ``None``: its
    only parent is the tip, it changes some of the four paths and nothing
    else, each file's committed text is what the worker wrote, and no file
    changes more than :data:`MAX_FILE_LINES` lines."""
    if not sha or not tip:
        return "the handle names no sha or tip"
    out = _rl._read(root, ["rev-list", "--parents", "-n", "1", sha])
    if (out or "").split() != [sha, tip]:
        return f"{sha[:9]} is not the only child of {tip[:9]}"
    paths = _rl._changed_paths(root, tip, sha)
    if paths is None:
        return "changed-path enumeration failed"
    if not paths or not set(paths) <= set(PIN_PATHS):
        return f"the commit changes {', '.join(paths) or 'nothing'} rather than the pin files"
    for path, text in expected.items():
        if _rr._show(root, sha, path) != text:
            return f"the committed {path} is not the text the worker wrote"
    return _numstat_violation(root, tip, sha)


def _commit_objects(
    root: Path,
    tip: str,
    branch: str,
    files: dict[str, str],
    subject: str,
    body: str,
    index: Path,
) -> str | None:
    """The commit of ``files`` over ``tip`` on ``branch``, built from objects
    so nothing in the working tree can reach it: blobs by ``hash-object``,
    the tip's tree in a private index with the four paths replaced, ``write-
    tree``, ``commit-tree`` and a compare-and-swap ``update-ref``. Its sha,
    or ``None`` when git fails."""
    try:
        blobs: dict[str, str] = {}
        for path, text in files.items():
            hashed = _git_raw(
                ["hash-object", "-w", "--stdin"], root, stdin=text.encode("utf-8")
            )
            if not _oid(hashed):
                return None
            blobs[path] = _oid(hashed)
        listing = _git_raw(["ls-tree", "-z", tip, "--", *files], root)
        modes: dict[str, str] = {}
        for entry in listing.stdout.decode("utf-8", "replace").split("\0"):
            head, _, path = entry.partition("\t")
            parts = head.split()
            if (
                len(parts) == 3
                and parts[1] == "blob"
                and parts[0] in ("100644", "100755")
            ):
                modes[path] = parts[0]
        if listing.returncode != 0 or set(modes) != set(files):
            return None
        index.unlink(missing_ok=True)
        if _git_raw(["read-tree", tip], root, index=index).returncode != 0:
            return None
        info = "".join(f"{modes[p]} {blobs[p]}\t{p}\n" for p in files)
        updated = _git_raw(
            ["update-index", "--index-info"],
            root,
            stdin=info.encode("utf-8"),
            index=index,
        )
        if updated.returncode != 0:
            return None
        tree = _oid(_git_raw(["write-tree"], root, index=index))
        if not tree:
            return None
        ident = [
            "-c",
            f"user.name={_rr.RELEASE_IDENT[0]}",
            "-c",
            f"user.email={_rr.RELEASE_IDENT[1]}",
            "-c",
            "commit.gpgsign=false",
        ]
        sha = _oid(
            _git_raw(
                [*ident, "commit-tree", tree, "-p", tip, "-m", subject, "-m", body],
                root,
            )
        )
        if not sha:
            return None
        swap = _git_raw(["update-ref", f"refs/heads/{branch}", sha, tip], root)
        return sha if swap.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        index.unlink(missing_ok=True)


def _commit_body(handle: dict[str, Any], release_todo: int) -> str:
    return (
        f"Moves the {handle['package']} pin to {handle['tag']}.\n\n"
        f"Release: {handle['tag']} at {handle['sha']} (release node td{release_todo}).\n"
        f"Previous pin: v{handle['from_version']} at {handle['from_sha']}.\n"
        "The lock and the deploy floor are text edits; the anchor key was "
        "derived with the new engine installed."
    )


def _poll_derive_green(run: _Run) -> bool:
    """The derive exited 0: take the key, commit the four files, prove the
    commit, recreate the tree clean and launch the verify run."""
    handle, root, todo_id = run.handle, run.root, run.todo_id
    version, sha, tip = str(handle["version"]), str(handle["sha"]), str(handle["tip"])
    package, branch = str(handle["package"]), _rs.slice_branch(todo_id)
    key = _read_result(run)
    if key is None:
        return run.charged(
            "the derive's result is not a 64-hex key for the target engine",
            "the derive's result file is not exactly a 64-hex key and the "
            f"target engine {version}; nothing was committed.",
        )
    try:
        files = _read_files(root, tip)
        edited = _edited(files, package, version, sha)
        edited[ANCHOR_PATH] = set_anchor_literals(
            files[ANCHOR_PATH], key=key, engine=version
        )
    except _ReadError as exc:
        return run.free(
            f"git could not read {exc.path} at {tip[:9]}",
            f"git could not read {exc.path} (infrastructure); retried.",
            READ_CLASS,
        )
    except PinError as exc:
        return run.charged(f"the final pin edit was refused: {exc}")
    # A worker that died after the ref moved, before the handle was saved,
    # replays this poll: the branch head that already is exactly this commit
    # on the tip is adopted, not committed over (that would fail the swap).
    head = _head(root, branch)
    commit: str | None = None
    if head and head != tip and _commit_violation(root, tip, head, edited) is None:
        commit = head
    else:
        commit = _commit_objects(
            root,
            tip,
            branch,
            edited,
            f"build(deps): bump {package} to v{version} (td{todo_id})",
            _commit_body(handle, int(run.params["release_todo"])),
            run.run_dir() / INDEX_FILE,
        )
    if commit is None:
        return run.charged(
            f"the pin commit for {handle.get('tag')} failed",
            "git could not commit the pin edit; the base may be unusual.",
        )
    violation = _commit_violation(root, tip, commit, edited)
    if violation is not None:
        return run.charged(
            f"pin commit refused: {violation}",
            f"the pin commit is not a pure pin bump ({violation}).",
        )
    # The derive ran repo code in the tree: put a clean checkout of the
    # commit in its place before anything else runs there.
    if _checkout_tree(root, todo_id, branch) is None:
        return run.free(
            f"git worktree add failed for the pin tree of td{todo_id}",
            "the pin tree could not be recreated; retried.",
            TREE_CLASS,
        )
    handle["commit"] = commit
    handle["key"] = key
    # The verify is an independent check: its own venv and result file, the
    # worker-written script written afresh (the run dir is not trusted), the
    # engine installed and the key recomputed there, then the tests.
    rdir = run.run_dir()
    try:
        for leftover in (VENV_DIR, VERIFY_VENV_DIR):
            shutil.rmtree(rdir / leftover, ignore_errors=True)
        (rdir / VERIFY_RESULT_FILE).unlink(missing_ok=True)
        (rdir / DERIVE_PY).write_text(DERIVE_SCRIPT_PY, encoding="utf-8")
    except OSError as exc:
        return run.free(
            f"writing the verify files failed: {exc}",
            "the run dir could not be written (infrastructure); retried.",
            TREE_CLASS,
        )
    steps = [
        ("sync", "uv sync --frozen --extra catalyst"),
        (
            "derive",
            _derive_command(
                rdir, run.root, todo_id, version, rdir / VERIFY_RESULT_FILE
            ),
        ),
        (
            "verify",
            "uv run --frozen --extra catalyst pytest -q -p no:cacheprovider "
            + " ".join(shlex.quote(t) for t in VERIFY_TESTS),
        ),
    ]
    if _start_phase(
        run,
        VERIFY_PHASE,
        VERIFY_SCRIPT,
        VERIFY_LOG,
        VERIFY_EXIT,
        steps,
        venv_name=VERIFY_VENV_DIR,
    ):
        return True
    run.ctx.append_chunk(
        "job_event",
        f"pin_bump: commit {commit[:9]} on tip {tip[:9]}; verify pid {handle['pid']}",
    )
    return False


def _poll_verify_green(run: _Run) -> bool:
    """The verify run exited 0: re-prove the branch, recreate the tree clean,
    stamp the committed record and park in the final phase."""
    handle, root, todo_id = run.handle, run.root, run.todo_id
    tip, commit = str(handle["tip"]), str(handle["commit"])
    branch = _rs.slice_branch(todo_id)
    if _head(root, branch) != commit:
        return run.charged(
            f"{branch} no longer points at the verified commit",
            f"{branch} moved off the verified commit {commit[:9]}; nothing is landed.",
        )
    out = _rl._read(root, ["rev-list", "--parents", "-n", "1", commit])
    if (out or "").split() != [commit, tip]:
        return run.charged(
            "the verified commit is not a child of the tip",
            f"{commit[:9]} is not a child of {tip[:9]}; nothing is landed.",
        )
    again = _read_result(run, VERIFY_RESULT_FILE)
    if again is None or again != handle.get("key"):
        return run.charged(
            "verify recomputed a different key or engine than the derive",
            "the verify run recomputed a different anchor key or engine than "
            f"the one committed in {commit[:9]}; nothing is landed.",
        )
    _sweep(todo_id)
    if _checkout_tree(root, todo_id, branch) is None:
        return run.free(
            f"git worktree add failed for the pin tree of td{todo_id}",
            "the pin tree could not be recreated; retried.",
            TREE_CLASS,
        )
    release = _Release(
        package=str(handle["package"]),
        version=str(handle["version"]),
        sha=str(handle["sha"]),
        tag=str(handle["tag"]),
    )
    record = _record(
        run,
        release,
        tip,
        state="committed",
        from_version=str(handle["from_version"]),
        from_sha=str(handle["from_sha"]),
        commit=commit,
    )
    if not _stamp(run.ctx, todo_id, record):
        return run.free(
            f"could not stamp the pin record on td{todo_id}",
            "the node could not be marked (infrastructure); retried.",
            RECORD_CLASS,
        )
    run.ctx.append_chunk(
        "job_event",
        f"pin_bump: {release.tag} committed as {commit[:9]} on {branch}; "
        "parked in handoff",
    )
    _to_handoff(run, record)
    return False


# ── Hand-off and the closing tail ──────────────────────────────────

#: Gripe links a pin bump resolves: a slice that fixes a gripe, or names it.
_GRIPE_RELATIONS = ("fixes", "related-to")


def _gripe_links(store: Any, slice_ids: list[int]) -> list[tuple[int, int]]:
    """``(slice, gripe)`` of every outgoing ``fixes`` / ``related-to`` link of
    ``slice_ids`` to a live gripe (seam)."""
    if not slice_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id
              FROM links l JOIN refs g ON g.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = ANY(%s) AND l.relation = ANY(%s)
               AND g.kind = 'gripe' AND g.retired_at IS NULL
             ORDER BY l.src_ref_id, l.dst_ref_id
            """,
            (list(slice_ids), list(_GRIPE_RELATIONS)),
        ).fetchall()
    return [(int(a), int(b)) for a, b in rows]


def _batch_slices(store: Any, todo_id: int, repo: str) -> list[int]:
    """The build slices of the pin node's batch that landed in ``repo``: the
    transitive ``blocked-by`` closure of the node inside its plan root (a
    ``blocked-by`` on a phase gates its children, so those join), kept when a
    build slice (no phase, generator or op node), closed ``done`` and carrying
    ``meta.landed_repo == repo``. Ids ascending."""
    root = _release.plan_root(store, todo_id)
    rows = [r for r in _rr._plan_rows(store, root) if r.get("kind") == "todo"]
    parent_of = {int(r["id"]): r.get("parent_id") for r in rows}
    edges = _rr._blocked_edges(store, list(parent_of))
    found: set[int] = set()
    frontier = {todo_id}
    while frontier:
        nxt = {dst for src, dst in edges if src in frontier}
        while True:  # a blocked-by on a phase gates its children, however deep
            kids = {nid for nid, parent in parent_of.items() if parent in nxt} - nxt
            if not kids:
                break
            nxt |= kids
        frontier = nxt - found - {todo_id}
        found |= frontier
    parents = set(parent_of.values())
    out: list[int] = []
    for nid in sorted(found):
        ref = store.get_ref(kind="todo", id=nid)
        meta = (getattr(ref, "meta", None) or {}) if ref is not None else {}
        if (
            nid not in parents
            and _rr._is_slice_node(meta)
            and _rs._terminal_status(store, nid) == "done"
            and meta.get(_lanes.LANDED_REPO_KEY) == repo
        ):
            out.append(nid)
    return out


def _resolve_gripe(
    store: Any, gripe_id: int, sha: str, package: str, version: str, slices: list[int]
) -> bool:
    """``land_slice._deferred_notify``'s resolved path for one gripe, once: a
    ``resolved:`` comment (unless the newest comment already is one naming the
    sha) and ``STATUS:auto_verify``. A settled gripe is left alone. ``True``
    when this wrote."""
    if _rs._todo_status(store, gripe_id) in _ls._GRIPE_SETTLED:
        return False
    chunks = store.chunks.list_chunks_for_ref(gripe_id)
    comments = [c for c in chunks if c.chunk_kind == _ls._GRIPE_COMMENT_KIND]
    newest = str(max(comments, key=lambda c: int(c.ord)).text or "") if comments else ""
    if not (newest.lstrip().lower().startswith("resolved:") and sha[:9] in newest):
        by = ", ".join(f"td{n}" for n in slices)
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=max((int(c.ord) for c in chunks), default=0) + 1,
                    text=(
                        f"resolved: {sha[:9]} (main, release pending) — "
                        f"{package} v{version}, {by}"
                    ),
                    meta={"chunk_kind": _ls._GRIPE_COMMENT_KIND},
                )
            ],
        )
    store.add_tag(
        gripe_id,
        Tag.closed("STATUS", "auto_verify"),
        set_by="system",
        replace_prefix=True,
    )
    return True


def finish(store: Any, todo_id: int, sha: str) -> None:
    """The pin bump has landed at ``sha``: stamp ``meta.pin_bump`` ``state:
    "landed"`` / ``landed_sha`` and resolve the batch's gripes (the done
    slices of the pinned repo upstream of the node, their ``fixes`` and
    ``related-to`` gripes: ``resolved:`` + ``STATUS:auto_verify``, once each;
    ``gripes`` / ``slices`` go on the record). Idempotent, and it never
    raises: the pin already landed, the gripes are a record."""
    try:
        record = _stored(store, todo_id)
        if record is None:
            log.warning("pin_bump: finish: td%d carries no pin record", todo_id)
            return
        landed = {**record, "state": "landed", "landed_sha": sha}
        if landed != record:
            store.stamp_ref_meta(todo_id, {RECORD_KEY: landed})
        record = landed
    except Exception:
        log.warning("pin_bump: finish: stamping td%d failed", todo_id, exc_info=True)
        return
    try:
        slices = _batch_slices(store, todo_id, str(record.get("repo")))
        by_gripe: dict[int, list[int]] = {}
        for slice_id, gripe_id in _gripe_links(store, slices):
            by_gripe.setdefault(gripe_id, []).append(slice_id)
        resolved: list[int] = []
        for gripe_id, upstream in sorted(by_gripe.items()):
            try:
                if _resolve_gripe(
                    store,
                    gripe_id,
                    sha,
                    str(record.get("package")),
                    str(record.get("version")),
                    sorted(set(upstream)),
                ):
                    resolved.append(gripe_id)
            except Exception:
                log.warning(
                    "pin_bump: finish: gripe gr%d not resolved", gripe_id, exc_info=True
                )
        known_g = [g for g in record.get("gripes") or [] if isinstance(g, int)]
        known_s = [n for n in record.get("slices") or [] if isinstance(n, int)]
        both = {
            "gripes": sorted(set(known_g) | set(resolved)),
            "slices": sorted(set(known_s) | set(slices)),
        }
        if any(record.get(k) != v for k, v in both.items()):
            store.stamp_ref_meta(todo_id, {RECORD_KEY: {**record, **both}})
    except Exception:
        log.warning(
            "pin_bump: finish: gripe walk of td%d failed", todo_id, exc_info=True
        )


def _succeed(run: _Run, summary: str, **meta: Any) -> bool:
    run.ctx.set_meta(compute_handle_released=True, **meta)
    run.ctx.append_chunk("job_summary", summary)
    run.ctx.set_status("succeeded")
    return True


def _close_noop(run: _Run, record: dict[str, Any] | None) -> bool:
    """A no-op bump mints no land, and the dispatcher closes a pin node only
    on a succeeded land: resolve the batch's gripes with the precis tip, write
    ``pinned:`` (never ``landed:``; nothing landed), close the node and
    succeed. Every step is idempotent, so a retry after a failure repeats it."""
    ctx, todo_id = run.ctx, run.todo_id
    if (
        record is None
        or not record.get("noop")
        or record.get("state") not in ("committed", "landed")
    ):
        return run.charged(
            f"the no-op pin record of td{todo_id} is gone",
            "the no-op record on the node is missing or changed; nothing was moved.",
        )
    tip = _rl._hex(record.get("base"))
    package, version = str(record.get("package")), str(record.get("version"))
    sha = str(record.get("sha"))
    finish(ctx.store, todo_id, tip)
    note = f"pinned: {package} v{version} already at {sha[:9]} (no-op)"
    try:
        newest = (_ls._newest_comment(ctx.store, todo_id) or "").lstrip()
        if not newest.lower().startswith("pinned:"):
            _rs._append_comment(ctx.store, todo_id, note)
        if _rs._terminal_status(ctx.store, todo_id) is None:
            _ls._close_todo(ctx.store, todo_id)
    except Exception:
        log.warning("pin_bump: closing no-op td%d failed", todo_id, exc_info=True)
        return run.free(
            f"could not close the no-op node td{todo_id}",
            "the node could not be closed (infrastructure); retried.",
            RECORD_CLASS,
        )
    return _succeed(
        run,
        f"pin_bump td{todo_id}: v{version} already pinned at {sha[:9]} (no-op), "
        "node closed",
    )


def _poll_handoff(run: _Run) -> bool:
    """The final phase. A no-op closes its node; a committed bump mints its
    land (adopting a live one under the idem key), stamps ``handed`` and
    succeeds. ``True`` when the job is terminal."""
    ctx, root, todo_id = run.ctx, run.root, run.todo_id
    record = _stored(ctx.store, todo_id)
    if run.handle.get("noop"):
        return _close_noop(run, record)
    commit = _rl._hex(run.handle.get("commit"))
    if (
        record is None
        or not commit
        or record.get("commit") != commit
        or record.get("state") not in ("committed", "handed", "landed")
    ):
        return run.charged(
            f"the committed pin record of td{todo_id} is gone or changed",
            "the committed record on the node is missing or changed; nothing is "
            "landed.",
        )
    version = str(record.get("version"))
    if record.get("state") != "committed":
        # Already handed (a repeated poll): succeed again, mint nothing.
        return _succeed(
            run,
            f"pin_bump td{todo_id}: v{version} committed {commit[:9]}, handed to "
            f"land_slice jo{record.get('land_job')}",
        )
    branch = _rs.slice_branch(todo_id)
    if _head(root, branch) != commit:
        return run.charged(
            f"{branch} no longer points at the committed commit",
            f"{branch} moved off the committed commit {commit[:9]}; nothing is landed.",
        )
    try:
        land = _rs._mint_land(ctx, todo_id, Path(str(record["worktree"])), None)
    except Exception as exc:
        log.warning("pin_bump: land_slice mint for td%d failed", todo_id, exc_info=True)
        return run.free(
            f"the land_slice mint failed: {exc}",
            f"the land_slice job could not be minted ({exc}); retried.",
            HANDOFF_CLASS,
        )
    # The land may already have run and finished: stamp over a record that is
    # still the committed one, never over a ``landed`` one.
    latest = _stored(ctx.store, todo_id) or {}
    if latest.get("commit") == commit and latest.get("state") in ("handed", "landed"):
        return _succeed(
            run,
            f"pin_bump td{todo_id}: v{version} committed {commit[:9]}, handed to "
            f"land_slice jo{land}",
            land_job=land,
        )
    if latest.get("commit") != commit or latest.get("state") != "committed":
        return run.charged(
            f"the committed pin record of td{todo_id} changed during the hand-off",
            "the committed record on the node changed while the land was minted; "
            "see the land job.",
        )
    if not _stamp(ctx, todo_id, {**latest, "state": "handed", "land_job": land}):
        return run.free(
            f"could not stamp the pin record on td{todo_id}",
            "the node could not be marked (infrastructure); retried.",
            RECORD_CLASS,
        )
    return _succeed(
        run,
        f"pin_bump td{todo_id}: v{version} committed {commit[:9]}, handed to "
        f"land_slice jo{land}",
        land_job=land,
    )


# ── Poll and kill ──────────────────────────────────────────────────


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half with ``repo_release.poll``'s contract: ``False``
    while the job waits (parked, or a run is live), ``True`` once the job is
    terminal."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"pin_bump: malformed handle {handle!r}")
        return True
    params = dict((ctx.meta or {}).get("params") or {})
    phase = handle.get("phase")
    if phase in (RESOLVE_PHASE, HANDOFF_PHASE) and not _rr._due(handle):
        return False
    todo_id = params.get("todo_id")
    if not isinstance(todo_id, int) or isinstance(todo_id, bool):
        ctx.record_failure("pin_bump: the job has no params.todo_id")
        return True
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        ctx.record_failure(f"pin_bump: {exc}")
        return True
    run = _Run(ctx, handle, params, root)
    if phase == RESOLVE_PHASE:
        return _poll_resolve(run)
    if phase == DERIVE_PHASE:
        return _poll_detached(run, "derive", _poll_derive_green)
    if phase == VERIFY_PHASE:
        return _poll_detached(run, "verify", _poll_verify_green)
    if phase == HANDOFF_PHASE:
        return _poll_handoff(run)
    ctx.record_failure(f"pin_bump: unknown phase {phase!r}")
    return True


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM a live run's process group (never
    the worker's own) and drop the worktree and branch this job made. A
    parked handle has neither (a committed bump waiting in ``handoff`` is
    kept for the resume), and nothing here raises."""
    if not isinstance(handle, dict) or handle.get("phase") not in (
        DERIVE_PHASE,
        VERIFY_PHASE,
    ):
        return
    _rl._signal_gate(handle)
    try:
        params = (getattr(ctx, "meta", None) or {}).get("params") or {}
        todo_id = params.get("todo_id")
        if isinstance(todo_id, int) and not isinstance(todo_id, bool):
            if handle.get("made"):
                _drop_made(_rs.slice_root(), todo_id)
                _clear_building(ctx, todo_id)
            _sweep(todo_id)
    except Exception:  # pragma: no cover - kill never raises
        log.warning("pin_bump: kill cleanup failed", exc_info=True)


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate_submit=validate_submit,
    submit=submit,
    poll=poll,
    kill=kill,
    deadline_kill_meta=DEADLINE_KILL_META,
    wall_seconds=_ls.wall_seconds,
)
