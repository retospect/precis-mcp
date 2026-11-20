"""Shared audience policy for record references in document exports.

Bibliographic citations and intra-draft cross-references retain their existing
semantics. Other record handles link to the configured web application for
collaborators, or receive stable supplement numbers for external readers.
Numbering and evidence collection live here rather than in either renderer.
The URL guard is lexical and offline: it checks the configured application
host, private address literals and private DNS suffixes without resolving DNS.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zipfile import ZipFile

from precis.errors import BadInput
from precis.export._provenance_strip import (
    _AUTHORING,
    _BRACKET_HANDLE,
    _BRACKET_SEL,
    _HANDLE_LIST,
    _OPAQUE,
    _explode,
    _sel_norm,
    record_nav_sel,
)
from precis.export.supplement import SupplementEntry, collect_entry
from precis.utils import handle_registry

_SCHEME_SEPARATOR = re.compile(r"://|//")
_AUTHORITY = re.compile(r"[^/?#\s<>\"'`(){}|,;\\]+")
_HOST = re.compile(r"\[[0-9a-fA-F:.%]+\]|(?:[^\W_]|[.-])+")
_URL_TOKEN = re.compile(r"[^\s<>\"'`(){}|,;]+")


def web_base_url(store: Any) -> str:
    """Read the existing application base URL setting (vault/env)."""
    from precis import secrets
    from precis.file_links import WEB_BASE_URL_SECRET

    return (secrets.get_secret(WEB_BASE_URL_SECRET, store=store) or "").rstrip("/")


#: The exact remediation every unset-origin warning names (gr592892): one
#: operator-set application origin per installation — a vault secret or
#: env var, never a per-export param (there is no ``base_url`` override).
#: ``precis secret set`` reads the value from stdin, never argv, so the
#: origin is piped in (or typed at the ``--prompt`` no-echo prompt).
SET_ORIGIN_COMMAND = (
    "echo https://<web-app-origin> | precis secret set PRECIS_WEB_BASE_URL"
)


def link_origin_warnings(base_url: str) -> list[str]:
    """The loud warning a dev/collaborator export emits when the
    application origin is unset (gr592892): every record-handle link it
    writes is path-only, which a PDF viewer cannot open. ``[]`` when the
    origin is configured; one shared text feeds the job event, the job
    summary and the draft hygiene report."""
    if base_url:
        return []
    return [
        "PRECIS_WEB_BASE_URL is unset — every record-handle link in this "
        "export is path-only (no origin to join onto), which a PDF viewer "
        "cannot open. Set the application origin once per installation: "
        f"{SET_ORIGIN_COMMAND} (or the same name on /secrets)."
    ]


def validate_audience(audience: str) -> None:
    if audience not in {"dev", "external"}:
        raise BadInput("unknown export audience", next="choose dev or external")


def private_url(url: str, base_url: str) -> bool:
    """Whether an absolute URL points at this installation or private space."""
    url = unquote(url)
    while True:
        trimmed = url.rstrip(".,;:!?)'`*_~>")
        if trimmed.endswith("]") and "[" not in trimmed:
            trimmed = trimmed[:-1]
        if trimmed == url:
            break
        url = trimmed
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        own = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host:
        return False
    if own and host == own:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return "." not in host or host.endswith(
            (".local", ".internal", ".localhost", ".ts.net", ".home.arpa")
        )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.items() for v in item for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _private_url_match(text: str, base_url: str) -> re.Match[str] | None:
    """Check every authority independently, including URLs embedded in URLs.

    Protocol-relative URLs must start a token, outside another URL. Bound the
    authority before removing userinfo, then read the hostname without its port
    (preserving IPv6 brackets and Unicode letters).
    """
    url_end = 0
    for match in _SCHEME_SEPARATOR.finditer(text):
        relative = match.group() == "//"
        if relative and (
            match.start() < url_end
            or (
                match.start() > 0
                and not text[match.start() - 1].isspace()
                and text[match.start() - 1] not in "([<\"'="
            )
        ):
            continue
        authority = _AUTHORITY.match(unquote(text[match.end() :]))
        if authority is None:
            continue
        host = _HOST.match(authority.group().rsplit("@", 1)[-1])
        if host is None:
            continue
        if relative and "." not in authority.group():
            own = (urlsplit(base_url).hostname or "").lower().rstrip(".")
            if host.group().lower().rstrip(".") != own:
                continue
        token = _URL_TOKEN.match(text, match.end())
        if token is not None:
            url_end = max(url_end, token.end())
        if private_url("//" + host.group(), base_url):
            return match
    return None


def guard_text(text: str, label: str, base_url: str) -> None:
    if _private_url_match(text, base_url) is not None:
        raise BadInput(
            f"external export contains a private URL in {label}",
            next="remove the private URL from the named chunk or use audience=dev",
        )


def guard_audience(store: Any, ref: Any, audience: str = "dev") -> None:
    """Refuse private URLs before writing anything, including on cache hits."""
    validate_audience(audience)
    if audience != "external":
        return
    base = web_base_url(store)
    fields = [("draft title", getattr(ref, "title", "") or "")]
    fields.extend(("draft byline", s) for s in _strings(getattr(ref, "authors", None)))
    meta = getattr(ref, "meta", None) or {}
    fields.extend(("draft byline", s) for s in _strings(meta.get("author")))
    for chunk in store.drafts.reading_order(ref.id):
        label = f"chunk {getattr(chunk, 'dc', None) or getattr(chunk, 'handle', '?')}"
        fields.append((label, chunk.text or ""))
        fields.extend((label, s) for s in _strings(getattr(chunk, "meta", None)))
    bad = [
        label for label, text in fields if _private_url_match(text, base) is not None
    ]
    if bad:
        raise BadInput(
            "external export contains private URLs in " + ", ".join(dict.fromkeys(bad)),
            next="remove those URLs from the named chunks or use audience=dev",
        )


def guard_artifacts(
    store: Any,
    ref: Any,
    paths: list[Path],
    *,
    cited: list[str] | None = None,
    entries: list[SupplementEntry] | None = None,
) -> None:
    """Check rendered text independently of URL parsing and input markup.

    XML relationships are text too: a hidden hyperlink can leak the host even
    when the visible Word paragraph contains only a public URL.
    """
    base = web_base_url(store)
    host = (urlsplit(base).hostname or "").rstrip(".")
    pattern = (
        re.compile(
            r"(?<![\w.-])" + re.escape(host) + r"(?![\w-]|\.[\w-])", re.IGNORECASE
        )
        if host
        else None
    )

    def private_match(text: str) -> re.Match[str] | None:
        match = _private_url_match(text, base)
        if match is not None:
            return match
        return pattern.search(text) if pattern is not None else None

    def check(text: str, location: str, *, bibliography: bool = False) -> None:
        from precis.utils.mentions import _universal_handle_tokens

        hit = private_match(text)
        if hit is None:
            return
        labels = []
        if bibliography:
            keys = re.findall(r"(?m)^@\w+\{([^,]+),", text[: hit.start()])
            if keys:
                labels.append(f"bibliography key {keys[-1]}")
        records = {
            entry.handle
            for entry in entries or []
            if private_match(entry.description)
            or any(
                private_match(data.decode("utf-8", errors="ignore"))
                for data in entry.files.values()
            )
        }
        for chunk in store.drafts.reading_order(ref.id):
            if any(
                private_match(value)
                or records.intersection(
                    _universal_handle_tokens(preserve_records(value))
                )
                for value in [chunk.text or "", *_strings(getattr(chunk, "meta", None))]
            ):
                labels.append(f"chunk {chunk.dc}")
        if not bibliography:
            for slug in cited or []:
                source = next(
                    (
                        source
                        for kind in ("paper", "patent", "datasheet")
                        if (source := store.get_ref(kind=kind, id=slug)) is not None
                    ),
                    None,
                )
                if source is not None and any(
                    private_match(value)
                    for value in [
                        getattr(source, "title", "") or "",
                        *_strings(getattr(source, "authors", None)),
                        *_strings(getattr(source, "meta", None)),
                    ]
                ):
                    labels.append(f"bibliography key {slug}")
        detail = f" ({', '.join(labels)})" if labels else ""
        raise BadInput(
            f"external export contains a private URL or host in {location}{detail}",
            next="remove the private URL or host from the named source or use audience=dev",
        )

    for path in paths:
        if path.suffix in {".docx", ".zip"}:
            with ZipFile(path) as archive:
                for name in archive.namelist():
                    try:
                        text = archive.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    check(text, f"{path.name}:{name}")
        else:
            check(
                path.read_text(encoding="utf-8"),
                path.name,
                bibliography=path.suffix == ".bib",
            )


def record_handle(handle: str, store: Any = None) -> tuple[str, bool, int] | None:
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return None
    kind, is_chunk, _pk = parsed
    if kind == "paper" and store is not None:
        if is_chunk:
            resolved = store.resolve_handle(handle)
            if resolved is None or resolved.kind != "paper":
                return None
            _pk = resolved.ref_id
        ref = store.get_ref(kind="paper", id=_pk)
        if (getattr(ref, "meta", None) or {}).get("record_type") == "dataset":
            return "dataset", False, _pk
    if kind in {"paper", "patent", "finding"} or (kind == "draft" and is_chunk):
        return None
    return parsed


def preserve_records(text: str) -> str:
    """Keep record handles through the legacy provenance stripping pass."""

    def prose(part: str) -> str:
        part = _BRACKET_SEL.sub(_sel_norm, part)
        part = _AUTHORING.sub(
            lambda m: f"[{m[1]}]" if record_handle(m[1]) else m[0], part
        )
        part = _HANDLE_LIST.sub(_explode, part)
        return _BRACKET_HANDLE.sub(
            lambda m: f"[{m[1]}]({m[1]})" if record_handle(m[1]) else m[0], part
        )

    return "".join(
        part if i % 2 else prose(part) for i, part in enumerate(_OPAQUE.split(text))
    )


def has_record_handles(text: str, store: Any = None) -> bool:
    """Whether a literal title needs the audience-aware inline renderer."""
    from precis.utils.mentions import _universal_handle_tokens

    return any(
        record_handle(h, store)
        for h in _universal_handle_tokens(preserve_records(text))
    )


@dataclass
class RecordReference:
    text: str
    url: str = ""
    footnote: str = ""
    citation: bool = False


_NAV_SEL_RE = re.compile(handle_registry.NAV_SEL_PATTERN)

#: The chunk noun a ``~<sel>`` selector prints with per kind — a pres deck's
#: chunks are slides, so ``pr592588~124`` renders "pr592588 slide 124".
_SEL_NOUN: dict[str, str] = {"pres": "slide"}


def nav_sel_text(kind: str, sel: str) -> str:
    """The reader-facing phrase a ``~<sel>`` chunk selector prints as:
    "slide 124" on a pres deck, "page 4" for a ``~pN`` page jump,
    "chunks 1..3" for an ord range, "chunk N" anywhere else (gr593143)."""
    if sel.startswith("p"):
        return f"page {sel[1:]}"
    noun = _SEL_NOUN.get(kind, "chunk")
    if ".." in sel:
        noun += "s"
    return f"{noun} {sel}"


@dataclass
class Audience:
    store: Any
    name: str = "dev"
    base_url: str = ""
    entries: list[SupplementEntry] = field(default_factory=list)
    source_chunks: dict[str, list[str]] = field(default_factory=dict)
    #: Export-level advisories the renderer folds into its own warnings
    #: (gr592892: an unset ``PRECIS_WEB_BASE_URL`` leaves every dev link
    #: path-only — :func:`link_origin_warnings`).
    warnings: list[str] = field(default_factory=list)
    _by_record: dict[tuple[str, bool, int], SupplementEntry] = field(
        default_factory=dict
    )
    _groups: dict[str, int] = field(
        default_factory=lambda: {
            "pathway": 1,
            "structure": 2,
            "dataset": 3,
            "job": 3,
        }
    )
    _counts: dict[int, int] = field(default_factory=dict)

    def reference(self, handle: str, surface: str | None) -> RecordReference | None:
        store = self.store if self.name == "external" else None
        parsed = record_handle(handle, store)
        # ``<record-handle>~<sel>`` — a chunk selector on a record of a
        # chunked kind (``pr592588~124``): ``handle_registry.parse`` refuses
        # it (a record takes no selector), so resolve the base and carry the
        # selector into the link — ``/r/<kind>/<pk>?chunk=<sel>`` — and into
        # the printed text ("pr592588 slide 124") instead of dropping it
        # (gr593143). A table selector on a chunk handle (``gc13~abc1``) is
        # not navigation and is not this case.
        base, sel = handle_registry.split_selector(handle)
        nav_sel: str | None = None
        if parsed is None and sel is not None and _NAV_SEL_RE.fullmatch(sel):
            candidate = record_handle(base, store)
            if candidate is not None and not candidate[1] and record_nav_sel(base):
                parsed, nav_sel = candidate, sel
            elif candidate is None and record_nav_sel(base):
                # A citation kind's base (``pa5~3``): ``record_handle``
                # refuses papers so the *bare* handle cites, but a
                # ``~<sel>`` names a passage the ``/r`` resolver lands on
                # — link it like any other navigable selector.
                parsed, nav_sel = handle_registry.parse(base), sel
        if parsed is None:
            return None
        kind, is_chunk, pk = parsed
        if self.name == "dev":
            path = f"/c/{base}" if is_chunk else f"/r/{kind}/{pk}"
            if nav_sel is not None:
                path += f"?chunk={quote(nav_sel, safe='')}"
                text = surface or f"{base} {nav_sel_text(kind, nav_sel)}"
            else:
                text = surface or handle
            return RecordReference(text, self.base_url + path)
        record = parsed
        if is_chunk:
            resolved = self.store.resolve_handle(handle)
            if resolved is not None:
                record = (resolved.kind, False, resolved.ref_id)
        if record not in self._by_record:
            group = self._groups.setdefault(kind, max(self._groups.values()) + 1)
            self._counts[group] = self._counts.get(group, 0) + 1
            number = f"S{group}.{self._counts[group]}"
            entry = collect_entry(
                self.store,
                base if nav_sel is not None else handle,
                number,
                base_url=self.base_url,
                source_chunks=self.source_chunks.get(handle),
            )
            self._by_record[record] = entry
            self.entries.append(entry)
        entry = self._by_record[record]
        text = entry.number
        if nav_sel is not None:
            text = f"{entry.number} ({nav_sel_text(kind, nav_sel)})"
        return RecordReference(
            text, footnote=entry.description, citation=kind in ("dataset", "paper")
        )


def for_export(
    store: Any, audience: str, *, chunks: list[Any] | None = None
) -> Audience:
    validate_audience(audience)
    state = Audience(store, audience, web_base_url(store))
    if audience == "dev":
        state.warnings.extend(link_origin_warnings(state.base_url))
    if audience == "external":
        for chunk in chunks or []:
            for text in [chunk.text or "", *_strings(getattr(chunk, "meta", None))]:
                for handle in re.findall(r"\b[a-z]{2}\d+\b", text):
                    labels = state.source_chunks.setdefault(handle, [])
                    if chunk.dc not in labels:
                        labels.append(chunk.dc)
    return state
