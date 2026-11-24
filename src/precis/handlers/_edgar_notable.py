"""Editorial allow-list for EDGAR watch hits entering the morning briefing."""

from collections.abc import Iterable

_NOTABLE_FORMS = frozenset({"10-K", "10-Q", "20-F", "S-1", "F-1"})
_NOTABLE_8K_ITEMS = frozenset(
    {
        "1.01",
        "1.02",
        "1.03",
        "1.05",
        "2.01",
        "2.02",
        "2.03",
        "2.04",
        "2.05",
        "2.06",
        "3.01",
        "4.01",
        "4.02",
        "5.01",
        "5.02",
        "8.01",
    }
)


def is_notable(form: str, items: Iterable[str]) -> bool:
    """Include periodic reports, registrations and listed material 8-K events."""
    base = form.strip().upper().removesuffix("/A")
    return base in _NOTABLE_FORMS or (
        base == "8-K" and not _NOTABLE_8K_ITEMS.isdisjoint(items)
    )
