# Draft section cross-references cannot render page numbers

**what**: Draft section cross-references (e.g., `\ref{sec:methods}`) in LaTeX export render as section labels only. A `\cpageref`-style marker is needed so LaTeX drafts can render page numbers in the PDF export, with a fallback to section names in docx (since docx does not support page-number fields in cross-references).

**why**: Reto asked 2026-09-27 for page-number cross-references in section citations so draft readers can find cited sections by page. Currently LaTeX renders the section label (e.g., `§2.1`) but not the page number.

**owner anchor**: `src/precis/export/latex.py` · `BARE_BRACKET_REF_PATTERN` (cross-reference pattern) · `src/precis/export/docx.py` (fallback)

**test**: n/a — requires manual inspection of LaTeX export with cross-references and docx export fallback, no automated regression test yet.
