"""Configuration from ~/.config/precis/config.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "precis"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"


@dataclass
class PrecisConfig:
    """Precis configuration."""

    author: str = "precis"
    citation_style: str = "acs"

    # Embedding settings
    embed_provider: str = "none"  # ollama | openai | local | none
    embed_model: str = "nomic-embed-text"

    @classmethod
    def load(cls, path: Path | None = None) -> PrecisConfig:
        """Load config from TOML file, falling back to defaults."""
        path = path or DEFAULT_CONFIG_FILE
        if not path.exists():
            return cls()
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            precis = data.get("precis", {})
            paper = data.get("paper", {})
            embed = data.get("embed", {})
            return cls(
                author=precis.get("author", cls.author),
                citation_style=paper.get("citation_style", cls.citation_style),
                embed_provider=embed.get("provider", cls.embed_provider),
                embed_model=embed.get("model", cls.embed_model),
            )
        except Exception:
            return cls()
