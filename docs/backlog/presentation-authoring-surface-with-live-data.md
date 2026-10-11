# Presentation/poster authoring surface with live data binding

**What:** Precis should provide an authoring surface for slides and posters as structured presentation objects — frames/panels as blocks that cite findings and pull live counts from prod at render time instead of hand-copied numbers. Today `pres` only stores finished decks for search; authoring happens in hand-maintained LaTeX or other external tools.

**Why:** The August status deck (pres/status-2026-08/, a hand-written LaTeX beamer deck) relied on a NOTES.md honesty ledger to manually track which numbers were current and which were stale. Structured authoring would eliminate that manual bookkeeping, allow live number re-verification at render time, and keep the deck in sync with the data it reports.

**Owner anchor:** Likely drafting pillar; unclear whether this is a new kind or a draft export mode. Open questions: new kind vs draft→slides/poster export, how live numbers are bound and re-verified, whether `pres` becomes the authoring kind or stays storage-only.

**Test:** Minimal: retire a hand-maintained LaTeX deck (pres/status-2026-08/) into a structured precis presentation object; render it with live number pulls; modify a finding and verify that the deck's rendered numbers update without manual re-entry.
