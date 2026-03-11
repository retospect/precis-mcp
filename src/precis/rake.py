"""RAKE keyword extraction — re-exported from precis-summary.

This module re-exports the RAKE implementation from the precis-summary
package for backward compatibility.
"""

from precis_summary.rake import (  # noqa: F401
    STOPWORDS,
    _score_phrases,
    _split_to_phrases,
    telegram_precis,
)
