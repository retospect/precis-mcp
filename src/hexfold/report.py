"""Diagnostics: Severity, Finding, Report, Profile.

Codes are the API, messages are not.  A Report is a value object: it is
sorted, serialisable, and has no __bool__ on purpose (SPEC section 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar


class Severity(IntEnum):
    INFO = 0
    WARN = 1
    ERROR = 2


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str
    where: str = ""
    span: tuple[int, int] | None = None  # (line, col), 1-based
    data: tuple[tuple[str, Any], ...] = ()
    fix: str = ""

    def _key(self) -> tuple:
        return (
            -int(self.severity),
            self.span or (-1, -1),
            self.code,
            self.where,
            self.message,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.name,
        }
        if self.where:
            d["where"] = self.where
        if self.span is not None:
            d["span"] = list(self.span)
        if self.data:
            d["data"] = {k: v for k, v in self.data}
        if self.fix:
            d["fix"] = self.fix
        return dict(sorted(d.items()))


@dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.severity == Severity.ERROR for f in self.findings)

    def at(self, level: Severity) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity >= level)

    def errors(self) -> tuple[Finding, ...]:
        return self.at(Severity.ERROR)

    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == Severity.WARN)

    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == Severity.INFO)

    def sorted(self) -> Report:
        return Report(tuple(sorted(self.findings, key=lambda f: f._key())))

    def merge(self, other: Report) -> Report:
        return Report(self.findings + other.findings).sorted()

    def render(self, verbose: bool = False) -> str:
        lines = []
        for f in self.sorted().findings:
            loc = f"{f.where} " if f.where else ""
            span = f" @{f.span[0]}:{f.span[1]}" if f.span else ""
            lines.append(f"{f.severity.name:5} {f.code} {loc}{f.message}{span}")
            if verbose and f.fix:
                lines.append(f"      fix: {f.fix}")
        if not lines:
            return "ok"
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.sorted().findings],
            "ok": self.ok,
        }


@dataclass(frozen=True)
class Profile:
    """Severity policy.  Passed to check/build, never module-global."""

    promote: frozenset[str] = frozenset()
    demote: frozenset[str] = frozenset()
    ignore: frozenset[str] = frozenset()
    bond_tol_A: float = 0.10
    angle_tol_deg: float = 10.0

    DEFAULT: ClassVar[Profile]
    STRICT: ClassVar[Profile]

    def apply(self, f: Finding) -> Finding | None:
        if f.code in self.ignore or any(
            f.code.startswith(p) for p in self.ignore if p.endswith("*")
        ):
            return None
        if f.code in self.promote and f.severity < Severity.ERROR:
            return Finding(
                f.code, Severity.ERROR, f.message, f.where, f.span, f.data, f.fix
            )
        if f.code in self.demote and f.severity > Severity.INFO:
            return Finding(
                f.code, Severity.INFO, f.message, f.where, f.span, f.data, f.fix
            )
        return f

    def apply_all(self, findings: list[Finding]) -> Report:
        out = [g for f in findings if (g := self.apply(f)) is not None]
        return Report(tuple(out)).sorted()


_PROFILE_DEFAULT = Profile()
_PROFILE_STRICT = Profile(
    promote=frozenset(
        {
            "euler.residual",
            "ring.size.unusual",
            "geom.bond.long",
            "geom.bond.short",
            "geom.angle.dev",
            "registry.closure",
        }
    )
)
Profile.DEFAULT = _PROFILE_DEFAULT
Profile.STRICT = _PROFILE_STRICT


class HexfoldError(Exception):
    """Base class for hexfold errors."""


class ParseError(HexfoldError):
    def __init__(self, message: str, span: tuple[int, int] | None = None):
        super().__init__(message)
        self.message = message
        self.span = span

    def __str__(self) -> str:
        if self.span:
            return f"{self.span[0]}:{self.span[1]}: {self.message}"
        return self.message


class BuildError(HexfoldError):
    def __init__(self, report: Report):
        super().__init__(report.render())
        self.report = report
