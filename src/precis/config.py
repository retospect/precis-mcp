"""Configuration from ~/.config/precis/precis.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "precis"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "precis.toml"


@dataclass
class PrecisConfig:
    """Precis config — just track-changes author name."""

    author: str = "precis"

    @classmethod
    def load(cls, path: Path | None = None) -> PrecisConfig:
        """Load config from TOML file, falling back to defaults."""
        path = path or DEFAULT_CONFIG_FILE
        if not path.exists():
            return cls()
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            section = data.get("precis", {})
            return cls(
                author=section.get("author", cls.author),
            )
        except Exception:
            return cls()
