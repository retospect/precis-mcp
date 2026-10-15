"""Poster branding profiles — validated data, resolved against a draft.

A poster draft (``meta.workspace.doc_type='poster'``) is branded by a
*profile*: a role→hex palette, an allowed font, ordered logo references
and a sponsor line. Profiles are data — slice 6 of
``docs/backlog/draft-poster-genre-and-themes.md`` stores them as
owner-scoped ``kind='profile'`` refs and exposes ``put``/``edit``; this
module is the pure schema + resolution + validation the export path
consumes. No storage, no MCP surface.

Resolution order (per the spec):

1. the draft's own ``meta.workspace.poster`` override dict;
2. the owning author's profile default — **seam**: no per-user default
   exists yet, ``user_default`` below is the injection point slice 6
   wires to ``kind='profile'`` storage;
3. :data:`PRODUCT_PROFILE` — the neutral product palette, matching the
   packaged ``templates/draft/poster.tex`` role defaults.

Logos are figure ids (Reto 2026-10-15 ruling): the blob bytes live in the
figure store and are resolved by the caller — validation takes a
``logo_resolver`` seam so it stays pure and testable.

Validators never raise; they return :class:`PosterIssue` rows carrying
the named codes the spec fixes (``poster_role_missing``,
``poster_role_unknown``, ``poster_logo_missing``,
``poster_logo_resolution_low``, ``poster_contrast_insufficient``) plus
the size-limit codes. The exporter turns a non-empty list into
:class:`PosterValidationError`.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Declared colour roles — the ones ``templates/draft/poster.tex``
#: ``\definecolor``s and every chrome rule names. A profile may fill
#: exactly these; anything else is ``poster_role_unknown``.
ROLES: frozenset[str] = frozenset(
    {
        "accent",  # emphasis + MEASURED-highlight hue family
        "heading",  # block-title band background
        "band",  # banner / dark band background
        "bodytint",  # block body background tint
        "citation",  # citation brackets — meant to recede
        "measured",  # measured-value text
        "computed",  # computed/predicted-value text
        "bandtext",  # text on heading/band backgrounds
        "bodytext",  # text on the body tint / page
    }
)

#: The neutral product palette — hex equivalents of the template's
#: ``black!N`` defaults, so a profile-free export renders identically.
PRODUCT_PALETTE: dict[str, str] = {
    "accent": "#404040",
    "heading": "#262626",
    "band": "#262626",
    "bodytint": "#F5F5F5",
    "citation": "#4D4D4D",
    "measured": "#333333",
    "computed": "#595959",
    "bandtext": "#FFFFFF",
    "bodytext": "#000000",
}

#: Fonts the generic template knows how to load. A profile picks one of
#: these; the template owns the type scale — the choice only swaps the
#: sans family. ``lmodern`` is the template default (loaded
#: unconditionally); anything else is injected as a package line.
ALLOWED_FONTS: dict[str, str] = {
    "lmodern": "",  # template default — nothing to inject
    "helvet": "\\usepackage{helvet}",
}

#: Size ceilings (spec): 10 MiB per logo file, 40 MP raster decode, 64 KiB
#: profile JSON, 20 logos in the rendered strip.
LOGO_MAX_BYTES = 10 * 1024 * 1024
LOGO_MAX_PIXELS = 40 * 1000 * 1000
PROFILE_MAX_JSON_BYTES = 64 * 1024
LOGO_MAX_COUNT = 20

#: Raster extensions — these get the 300-ppi-at-rendered-size floor;
#: ``svg``/``pdf`` are vector and skip it (PDF additionally must be
#: single-page).
_RASTER_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "tiff", "bmp"})

#: Print floor: raster logos must reach 300 ppi in both dimensions at
#: their rendered size (product floor, not a WCAG criterion).
MIN_RASTER_PPI = 300.0

#: WCAG 2.2 SC 1.4.3 normal-text floor, adopted for all poster text.
MIN_CONTRAST = 4.5

#: Text/background role pairs checked at MIN_CONTRAST — band text sits on
#: both the banner band and the block-title band, so it is checked
#: against each; body text sits on the body tint.
_CONTRAST_PAIRS: tuple[tuple[str, str], ...] = (
    ("bandtext", "band"),
    ("bandtext", "heading"),
    ("bodytext", "bodytint"),
)


@dataclass(frozen=True, slots=True)
class PosterLogo:
    """One logo slot in the strip: a figure ref id (blob bytes resolved by
    the caller) + presentation hints. ``group`` keeps the spec's two
    ordered lists inside one ``logos`` field — institutions render first,
    then sponsors/funders; list order is the display order within a
    group."""

    figure: str  # kind='figure' ref — slug or numeric id, resolved by caller
    alt: str = ""
    height_cm: float = 3.2
    variant: str = "light"  # "light" | "dark" — strip background it suits
    group: str = "institution"  # "institution" | "sponsor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "figure": self.figure,
            "alt": self.alt,
            "height_cm": self.height_cm,
            "variant": self.variant,
            "group": self.group,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PosterLogo:
        return cls(
            figure=str(raw.get("figure", "")),
            alt=str(raw.get("alt", "")),
            height_cm=float(raw.get("height_cm", 3.2) or 3.2),
            variant=str(raw.get("variant", "light")),
            group=str(raw.get("group", "institution")),
        )


@dataclass(frozen=True, slots=True)
class PosterProfile:
    """A resolved branding profile. Field names mirror the slice-6
    ``kind='profile'`` surface (Reto 2026-10-15 ruling 4): ``name``,
    ``palette`` (role→hex), ``fonts``, ``logos`` (figure ids),
    ``sponsor_line``, ``owner``."""

    name: str = "default"
    owner: str = ""
    palette: dict[str, str] = field(default_factory=lambda: dict(PRODUCT_PALETTE))
    fonts: dict[str, str] = field(default_factory=lambda: {"body": "lmodern"})
    logos: tuple[PosterLogo, ...] = ()
    sponsor_line: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner,
            "palette": dict(self.palette),
            "fonts": dict(self.fonts),
            "logos": [logo.to_dict() for logo in self.logos],
            "sponsor_line": self.sponsor_line,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> PosterProfile:
        raw = raw or {}
        logos = raw.get("logos") or []
        return cls(
            name=str(raw.get("name", "default") or "default"),
            owner=str(raw.get("owner", "")),
            palette={str(k): str(v) for k, v in (raw.get("palette") or {}).items()},
            fonts={str(k): str(v) for k, v in (raw.get("fonts") or {}).items()},
            logos=tuple(
                PosterLogo.from_dict(lg) for lg in logos if isinstance(lg, Mapping)
            ),
            sponsor_line=str(raw.get("sponsor_line", "")),
        )

    def ordered_logos(self) -> list[PosterLogo]:
        """Strip order: institution logos first (list order), then
        sponsor/funder logos (list order)."""
        return [l for l in self.logos if l.group != "sponsor"] + [
            l for l in self.logos if l.group == "sponsor"
        ]


#: The neutral product profile — what a poster exports under when the
#: draft carries no ``meta.workspace.poster`` override and no user default.
PRODUCT_PROFILE = PosterProfile()


@dataclass(frozen=True, slots=True)
class PosterIssue:
    """One validation failure. ``code`` is the stable, greppable token
    (``poster_role_missing`` …); ``detail`` names the offending value."""

    code: str
    detail: str = ""


class PosterValidationError(ValueError):
    """A resolved profile failed validation — ``issues`` carries every
    :class:`PosterIssue`, the message lists their codes."""

    def __init__(self, issues: list[PosterIssue]) -> None:
        self.issues = issues
        super().__init__(
            "invalid poster profile: "
            + "; ".join(
                f"{i.code} ({i.detail})" if i.detail else i.code for i in issues
            )
        )


def _deep_merge(base: dict[str, Any], layer: Mapping[str, Any]) -> dict[str, Any]:
    """``layer`` over ``base``: ``palette``/``fonts`` merge per key;
    everything else (logos, sponsor_line, name, owner) replaces."""
    out = dict(base)
    for key, value in layer.items():
        if key in ("palette", "fonts") and isinstance(value, Mapping):
            merged = dict(out.get(key) or {})
            merged.update({str(k): str(v) for k, v in value.items()})
            out[key] = merged
        else:
            out[key] = value
    return out


def resolve_poster_profile(
    meta: dict[str, Any] | None,
    *,
    user_default: Mapping[str, Any] | None = None,
) -> PosterProfile:
    """The effective profile for a draft: ``meta.workspace.poster``
    override over ``user_default`` over the product default.

    ``user_default`` is the slice-6 seam — the owning author's stored
    ``kind='profile'`` default, looked up by the caller. ``None`` today.
    """
    override: Mapping[str, Any] = {}
    if isinstance(meta, dict):
        ws = meta.get("workspace")
        if isinstance(ws, Mapping):
            raw = ws.get("poster")
            if isinstance(raw, Mapping):
                override = raw
    merged = PRODUCT_PROFILE.to_dict()
    merged = _deep_merge(merged, user_default or {})
    merged = _deep_merge(merged, override)
    return PosterProfile.from_dict(merged)


# ── validation ──────────────────────────────────────────────────


def _rel_luminance(hex_color: str) -> float:
    """WCAG 2.2 relative luminance of a ``#RRGGBB`` sRGB colour."""
    s = hex_color.lstrip("#")
    r, g, b = (int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _is_hex(value: str) -> bool:
    s = value.lstrip("#")
    return len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s)


def _logo_dims(data: bytes) -> tuple[int, int] | None:
    """Pixel ``(w, h)`` of a raster logo from its header — lazy open, no
    full decode (the 40 MP ceiling is enforced on the declared size, not
    on decompressed pixels)."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover — pillow is a pinned dep
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _pdf_pages(data: bytes) -> int | None:
    """Page count of a PDF logo (spec: PDFs are single-page)."""
    try:
        import fitz
    except ImportError:  # pragma: no cover — pymupdf is a pinned dep
        return None
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            return len(doc)
    except Exception:
        return None


def _validate_logo(
    logo: PosterLogo,
    resolver: Callable[[str], tuple[bytes, str] | None],
) -> list[PosterIssue]:
    issues: list[PosterIssue] = []
    try:
        asset = resolver(logo.figure)
    except Exception as exc:
        log.warning("poster_profile: logo resolver failed for %r: %s", logo.figure, exc)
        asset = None
    if asset is None:
        return [PosterIssue("poster_logo_missing", logo.figure)]
    data, ext = asset
    ext = (ext or "").lower().lstrip(".")
    if len(data) > LOGO_MAX_BYTES:
        issues.append(
            PosterIssue(
                "poster_logo_too_large",
                f"{logo.figure}: {len(data)} bytes > {LOGO_MAX_BYTES}",
            )
        )
    if ext in _RASTER_EXTS:
        dims = _logo_dims(data)
        if dims is not None:
            w_px, h_px = dims
            if w_px * h_px > LOGO_MAX_PIXELS:
                issues.append(
                    PosterIssue(
                        "poster_logo_pixels_exceeded",
                        f"{logo.figure}: {w_px}x{h_px} > {LOGO_MAX_PIXELS} px",
                    )
                )
            h_in = max(logo.height_cm, 0.01) / 2.54
            w_in = h_in * (w_px / max(h_px, 1))
            ppi = min(w_px / w_in, h_px / h_in)
            if ppi < MIN_RASTER_PPI:
                issues.append(
                    PosterIssue(
                        "poster_logo_resolution_low",
                        f"{logo.figure}: {ppi:.0f} ppi < {MIN_RASTER_PPI:.0f} "
                        f"at {logo.height_cm}cm rendered",
                    )
                )
    elif ext == "pdf":
        pages = _pdf_pages(data)
        if pages is not None and pages != 1:
            issues.append(
                PosterIssue("poster_logo_multipage", f"{logo.figure}: {pages} pages")
            )
    return issues


def validate_poster_profile(
    profile: PosterProfile,
    *,
    logo_resolver: Callable[[str], tuple[bytes, str] | None] | None = None,
) -> list[PosterIssue]:
    """All validation failures for a resolved profile. ``logo_resolver``
    maps a figure id to ``(bytes, ext)`` or ``None`` — ``None`` (or an
    unset resolver) skips the byte-level logo checks; presence is still
    checked when a resolver is given.
    """
    issues: list[PosterIssue] = []
    encoded = json.dumps(profile.to_dict(), separators=(",", ":")).encode("utf-8")
    if len(encoded) > PROFILE_MAX_JSON_BYTES:
        issues.append(
            PosterIssue(
                "poster_profile_too_large",
                f"{len(encoded)} bytes > {PROFILE_MAX_JSON_BYTES}",
            )
        )
    for role in sorted(ROLES):
        value = profile.palette.get(role)
        if not value or not str(value).strip():
            issues.append(PosterIssue("poster_role_missing", role))
        elif not _is_hex(str(value).strip()):
            issues.append(
                PosterIssue("poster_role_invalid", f"{role}={value!r} (want #RRGGBB)")
            )
    for key in sorted(profile.palette):
        if key not in ROLES:
            issues.append(PosterIssue("poster_role_unknown", key))
    for key, value in sorted(profile.fonts.items()):
        if key != "body" or value not in ALLOWED_FONTS:
            issues.append(
                PosterIssue(
                    "poster_font_unknown",
                    f"{key}={value!r} (want body ∈ {sorted(ALLOWED_FONTS)})",
                )
            )
    if len(profile.logos) > LOGO_MAX_COUNT:
        issues.append(
            PosterIssue("poster_logo_count", f"{len(profile.logos)} > {LOGO_MAX_COUNT}")
        )
    if logo_resolver is not None:
        for logo in profile.ordered_logos():
            issues.extend(_validate_logo(logo, logo_resolver))
    # Contrast only when both sides carry a valid hex — an invalid role is
    # already reported; piling a bogus ratio on top adds noise.
    for text_role, bg_role in _CONTRAST_PAIRS:
        fg, bg = profile.palette.get(text_role, ""), profile.palette.get(bg_role, "")
        if not (_is_hex(fg or "") and _is_hex(bg or "")):
            continue
        ratio = _contrast(fg, bg)
        if ratio < MIN_CONTRAST:
            issues.append(
                PosterIssue(
                    "poster_contrast_insufficient",
                    f"{text_role} on {bg_role}: {ratio:.2f}:1 < {MIN_CONTRAST}:1",
                )
            )
    return issues


def resolve_and_validate(
    meta: dict[str, Any] | None,
    *,
    user_default: Mapping[str, Any] | None = None,
    logo_resolver: Callable[[str], tuple[bytes, str] | None] | None = None,
) -> PosterProfile:
    """Resolve then validate — raises :class:`PosterValidationError` on any
    issue. The export path's one call."""
    profile = resolve_poster_profile(meta, user_default=user_default)
    issues = validate_poster_profile(profile, logo_resolver=logo_resolver)
    if issues:
        raise PosterValidationError(issues)
    return profile


__all__ = [
    "ALLOWED_FONTS",
    "LOGO_MAX_BYTES",
    "LOGO_MAX_COUNT",
    "LOGO_MAX_PIXELS",
    "MIN_CONTRAST",
    "MIN_RASTER_PPI",
    "PRODUCT_PALETTE",
    "PRODUCT_PROFILE",
    "PROFILE_MAX_JSON_BYTES",
    "ROLES",
    "PosterIssue",
    "PosterLogo",
    "PosterProfile",
    "PosterValidationError",
    "resolve_and_validate",
    "resolve_poster_profile",
    "validate_poster_profile",
]
