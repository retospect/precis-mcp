"""Pure text helpers of the engine pin-bump job (td659522 step 4, R1a).

precis pins the engine package ``autocatpath`` by git rev; moving the pin is
a four-file change (``pyproject.toml``, ``uv.lock``,
``deploy/group_vars/all.yml``, the anchor literals in
``tests/test_coverage_job.py``). This module is the pure half: text in, text
out, no I/O, no subprocess, no store. Every helper edits the text by line,
then re-parses the result (``tomllib`` / ``ast`` / a line diff) to prove the
edit changed exactly what it meant to. The job half comes in a later round;
nothing registers this module.
"""

from __future__ import annotations

import ast
import copy
import io
import re
import tomllib
from typing import Any

from precis.workers.job_types.repo_release import (
    _HEADER,
    ReleaseError,
    _norm,
    _split_version,
)

__all__ = [
    "PinError",
    "compare_versions",
    "current_pin",
    "pin_is_coherent",
    "set_anchor_literals",
    "set_group_vars_floor",
    "set_lock_pin",
    "set_pyproject_pin",
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
