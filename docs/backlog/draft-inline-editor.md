# draft-inline-editor

## Residuals (from OPEN-ITEMS)

- Headless-browser verification in CI (high-value): the interactive editor +
  virtual-scroller JS has no gate coverage and several browser-only bugs
  reached prod. Wire a slim Playwright pass into scripts/ship: boot the web
  app on the test DB with a seeded draft, assert a clean console + a couple
  of core interactions.
- Optional extensions (none block use): [-autocomplete over non-paper kinds
  (chunks/findings); resolved-title chips; slash-menu structured blocks;
  per-draft spellcheck language selector.
