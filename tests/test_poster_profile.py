"""Poster branding profiles: resolution order + the named validators.

Pure-module tests — no DB. Placeholder logos are generated in-test: real
small PNGs via Pillow for the ok/low-res paths, header-only PNGs for the
megapixel ceiling (``Image.open`` reports ``size`` without decoding), a
two-page PDF via fitz for the single-page rule.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from precis.export import poster_profile as pp


def _png(w: int, h: int, *, real: bool = True) -> bytes:
    """A PNG placeholder. ``real=False`` writes a valid header + empty IDAT
    so huge declared dimensions cost no memory (PIL reads ``size`` lazily)."""
    if real:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (w, h), (200, 200, 200)).save(buf, "PNG")
        return buf.getvalue()
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _resolver(assets: dict[str, tuple[bytes, str]]):
    return lambda figure: assets.get(figure)


def _codes(issues: list[pp.PosterIssue]) -> list[str]:
    return [i.code for i in issues]


class TestResolution:
    def test_product_default_without_override(self) -> None:
        profile = pp.resolve_poster_profile(None)
        assert profile == pp.PRODUCT_PROFILE
        assert set(profile.palette) == set(pp.ROLES)

    def test_workspace_override_merges_palette_and_replaces_logos(self) -> None:
        meta = {
            "workspace": {
                "poster": {
                    "palette": {"accent": "#005335"},
                    "logos": [{"figure": "ul-logo", "kind": "institution"}],
                    "sponsor_line": "Funded by Example",
                    "name": "lab",
                }
            }
        }
        profile = pp.resolve_poster_profile(meta)
        assert profile.palette["accent"] == "#005335"
        # untouched roles stay at product defaults
        assert profile.palette["band"] == pp.PRODUCT_PALETTE["band"]
        assert [l.figure for l in profile.logos] == ["ul-logo"]
        assert profile.sponsor_line == "Funded by Example"
        assert profile.name == "lab"

    def test_resolution_order_user_then_workspace(self) -> None:
        """user_default is the slice-6 seam: override wins over it, it wins
        over the product default."""
        user = {"palette": {"accent": "#111111"}, "sponsor_line": "user line"}
        meta = {"workspace": {"poster": {"palette": {"accent": "#222222"}}}}
        profile = pp.resolve_poster_profile(meta, user_default=user)
        assert profile.palette["accent"] == "#222222"
        assert profile.sponsor_line == "user line"
        # workspace without a poster key → the user default stands
        meta2 = {"workspace": {"doc_type": "poster"}}
        assert (
            pp.resolve_poster_profile(meta2, user_default=user).palette["accent"]
            == "#111111"
        )

    def test_strip_order_institutions_then_sponsors(self) -> None:
        profile = pp.PosterProfile(
            logos=(
                pp.PosterLogo(figure="s1", group="sponsor"),
                pp.PosterLogo(figure="i1"),
                pp.PosterLogo(figure="i2"),
                pp.PosterLogo(figure="s2", group="sponsor"),
            )
        )
        assert [l.figure for l in profile.ordered_logos()] == [
            "i1",
            "i2",
            "s1",
            "s2",
        ]


class TestValidation:
    def test_product_profile_is_valid(self) -> None:
        assert pp.validate_poster_profile(pp.PRODUCT_PROFILE) == []

    def test_missing_role(self) -> None:
        profile = pp.PosterProfile(palette={"accent": "#005335"})
        issues = pp.validate_poster_profile(profile)
        assert "poster_role_missing" in _codes(issues)
        assert (
            sum(c == "poster_role_missing" for c in _codes(issues)) == len(pp.ROLES) - 1
        )

    def test_unknown_role(self) -> None:
        profile = pp.PosterProfile(
            palette={**pp.PRODUCT_PALETTE, "chartreuse": "#7FFF00"}
        )
        assert _codes(pp.validate_poster_profile(profile)) == ["poster_role_unknown"]

    def test_invalid_hex(self) -> None:
        profile = pp.PosterProfile(palette={"accent": "green"})
        assert "poster_role_invalid" in _codes(pp.validate_poster_profile(profile))

    def test_unknown_font(self) -> None:
        profile = pp.PosterProfile(fonts={"body": "papyrus"})
        assert "poster_font_unknown" in _codes(pp.validate_poster_profile(profile))

    def test_contrast_floor(self) -> None:
        # bandtext on band far below 4.5:1
        profile = pp.PosterProfile(
            palette={**pp.PRODUCT_PALETTE, "bandtext": "#000000", "band": "#111111"}
        )
        codes = _codes(pp.validate_poster_profile(profile))
        assert "poster_contrast_insufficient" in codes
        detail = next(
            i.detail
            for i in pp.validate_poster_profile(profile)
            if i.code == "poster_contrast_insufficient"
        )
        assert "bandtext on band" in detail
        # a compliant pair passes
        ok = pp.PosterProfile(
            palette={**pp.PRODUCT_PALETTE, "band": "#005335", "bandtext": "#FFFFFF"}
        )
        assert "poster_contrast_insufficient" not in _codes(
            pp.validate_poster_profile(ok)
        )

    def test_logo_missing(self) -> None:
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="gone"),))
        issues = pp.validate_poster_profile(profile, logo_resolver=_resolver({}))
        assert _codes(issues) == ["poster_logo_missing"]

    def test_logo_resolution_low(self) -> None:
        # 100px tall at 4cm ≈ 63 ppi — under the 300 floor
        assets = {"low": (_png(100, 100), "png")}
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="low", height_cm=4.0),))
        assert _codes(
            pp.validate_poster_profile(profile, logo_resolver=_resolver(assets))
        ) == ["poster_logo_resolution_low"]

    def test_logo_resolution_ok(self) -> None:
        assets = {"hi": (_png(1200, 1200), "png")}
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="hi", height_cm=4.0),))
        assert (
            pp.validate_poster_profile(profile, logo_resolver=_resolver(assets)) == []
        )

    def test_logo_pixel_ceiling(self) -> None:
        assets = {"big": (_png(9000, 6000, real=False), "png")}
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="big"),))
        assert "poster_logo_pixels_exceeded" in _codes(
            pp.validate_poster_profile(profile, logo_resolver=_resolver(assets))
        )

    def test_logo_byte_ceiling(self) -> None:
        assets = {"fat": (b"\0" * (pp.LOGO_MAX_BYTES + 1), "png")}
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="fat"),))
        assert "poster_logo_too_large" in _codes(
            pp.validate_poster_profile(profile, logo_resolver=_resolver(assets))
        )

    def test_logo_count_ceiling(self) -> None:
        profile = pp.PosterProfile(
            logos=tuple(pp.PosterLogo(figure=f"l{i}") for i in range(21))
        )
        assert "poster_logo_count" in _codes(pp.validate_poster_profile(profile))

    def test_profile_json_ceiling(self) -> None:
        profile = pp.PosterProfile(sponsor_line="x" * (pp.PROFILE_MAX_JSON_BYTES))
        assert "poster_profile_too_large" in _codes(pp.validate_poster_profile(profile))

    def test_multipage_pdf_logo(self) -> None:
        fitz = pytest.importorskip("fitz")
        doc = fitz.open()
        doc.new_page()
        doc.new_page()
        pdf = doc.tobytes()
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="twop"),))
        issues = pp.validate_poster_profile(
            profile, logo_resolver=_resolver({"twop": (pdf, "pdf")})
        )
        assert _codes(issues) == ["poster_logo_multipage"]

    def test_vector_logo_skips_ppi(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
        profile = pp.PosterProfile(logos=(pp.PosterLogo(figure="vec"),))
        assert (
            pp.validate_poster_profile(
                profile, logo_resolver=_resolver({"vec": (svg, "svg")})
            )
            == []
        )

    def test_resolve_and_validate_raises(self) -> None:
        meta = {"workspace": {"poster": {"palette": {"accent": "#005335"}}}}
        assert pp.resolve_and_validate(meta).palette["accent"] == "#005335"
        # an emptied role is unfilled → poster_role_missing
        empty = {"workspace": {"poster": {"palette": {"accent": ""}}}}
        with pytest.raises(pp.PosterValidationError) as e0:
            pp.resolve_and_validate(empty)
        assert "poster_role_missing" in str(e0.value)
        bad = {"workspace": {"poster": {"fonts": {"body": "papyrus"}}}}
        with pytest.raises(pp.PosterValidationError) as ei:
            pp.resolve_and_validate(bad)
        assert "poster_font_unknown" in str(ei.value)
        assert ei.value.issues[0].code == "poster_font_unknown"
