# S2 drafter brief (shared by every S2 research agent)

You are researching design-for-manufacture rules for a knowledge graph an LLM
walks while designing parts. Your slice says what to do; this file says how.

## Hard limits
- Do NOT call any `mcp__precis__*` tool. Do not write anywhere except your one
  output file under <drafts>/s2/out/. No git.
- Web only: WebSearch, WebFetch, or `curl -sL <url>` (PDFs: save under
  <drafts>/s2/dl/<name>.pdf, then `pdftotext` or `gs -sDEVICE=txtwrite`).
- Never invent a number, URL or quote. If you cannot find a source, say so in
  the record (`action: no-source`). An honest gap beats a plausible figure.

## Equipment vocabulary (closed)
desktop-fdm (Prusa, Bambu, Creality, Voron class: 0.4 mm nozzle, open or
hobby-enclosed, slicer defaults), industrial-fdm (Stratasys Fortus/F-series,
heated-chamber, 0.178-0.33 mm slices, soluble support), sla, injection-mould,
cnc-mill, lathe, sheet, cast, bought, any. `any` means the rule holds for
every process.

## Source preference for desktop-fdm
1. Prusa Knowledge Base (help.prusa3d.com), incl. the material guide
   pages (help.prusa3d.com/materials and each material's article).
2. Bambu Lab wiki (wiki.bambulab.com), incl. filament guides and the
   per-filament technical data sheets Bambu publishes.
3. Manufacturer TDS for desktop filaments (Prusament, Bambu, Polymaker).
4. Slicer documentation (PrusaSlicer / OrcaSlicer / Bambu Studio docs).
5. Only if 1-4 are silent: secondary desktop sources (Ultimaker, Formlabs
   FDM pages, CNC Kitchen measured tests, Protolabs/Xometry/Hubs FDM guides)
   — mark these `source_quality: secondary`.
Stratasys is an industrial-fdm source. It is never a desktop-fdm citation.

## Output (YAML list, one record per input rule, same order)
```yaml
- ref: <copy the input ref verbatim>
  action: keep | recite | revalue | retag | split | drop | no-source
  equipment: <final equipment tag>
  text: <only if changed: one sentence, plain, units in it>
  value: <only if changed; string; ranges as "a-b">
  unit: <only if changed>
  source_url: <the URL that states it; deep link, not a home page>
  quote: <verbatim from that page, <= 30 words, the words that carry the number>
  source_quality: primary | secondary
  confidence: handbook | secondary | shop-rule
  min: <number or null>   # numeric bounds when the value is a range or limit
  max: <number or null>
  material: <pla|petg|abs|asa|tpu|pa|pc|any — only when the figure is material-specific>
  split_into: [...]      # only for action split: a list of records in this same shape (no ref; add equipment)
  note: <one line: why, and what differed from the old record>
```
Actions:
- keep: rule is right as it stands (equipment and source fit).
- recite: same value, better (desktop) source.
- revalue: desktop sources give a different number; give it.
- retag: only the equipment tag is wrong.
- split: the rule mixes two processes (e.g. a Stratasys figure in a desktop
  node): give one record per equipment.
- drop: wrong or not a design rule.
- no-source: no desktop source found after a real try (say what you searched).

`confidence: handbook` needs a primary vendor/KB page that states the figure;
a forum or blog figure is `shop-rule`. Prefer one exact page per record.

When done, reply with: counts per action, and the 5 records you are least
sure of. Keep the reply under 250 words.
