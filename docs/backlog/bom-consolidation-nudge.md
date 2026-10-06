---
status: idea
title: BOM consolidation nudge — fewer distinct parts, one standard set
pillar: 3d-design
prio: normal
---

# BOM consolidation nudge — fewer distinct parts, one standard set

Proposed, not ruled (design sketched by the orchestrator from Reto's 2026-10-06 intent; the attribute-role split and slice order are open to change).

## Motivation / why

Reto, 2026-10-06: minimise the number of discrete parts. Five screw types — can it be one, and if not, why not. A 10k, three 12k and an 8k resistor — can all be 10k. Ideally the designs converge on a standard set used across projects, so inventory carries over. Fewer distinct parts means fewer line items to buy, stock, pick and load, and a driver or tool set that covers every fastener.

Shape of the answer: not a skill and not an automatic rewrite, a nudge. The system says "x and y are similar, can they be combined?" and the human decides. That requires the system to know what a part is: a resistor has value and power rating, a screw has thread designation, length and head. Today a BOM lists lines; nothing compares them.

## In scope

Proposed design, four parts.

**Attribute roles per part class.** Each component category and each PCB part class declares which specs are identity (must match for two parts to be one part: thread designation, package or footprint, pin count), envelope (one part can stand in when it covers the other: power rating at least, voltage rating at least, tolerance at most, screw length within a stated margin), free (a value the design may be able to change: resistance to an E-series neighbour, screw length by a couple of millimetres, head style) and tool (attributes that imply a tool or process rather than a part: drive type implies a driver, thread size a tap, package a reflow or hand-solder step, wire gauge a crimp die). Mechanical parts take their specs from the component spec registry (`src/precis/handlers/component.py` and the families in `src/precis/data/component_series.json`). PCB parts take them from `parts.params` (the jlcparts parametrics), joined through `pcb_components` and `pcb_instances`.

**The nudge.** A "Consolidation" footer on `get(kind='component', view='bom')` (`ComponentHandler._render_bom`) and on the pcb bom export (`view == "bom"` branch of `PcbHandler` in `src/precis/handlers/pcb.py`). One line per group of parts that share identity and differ only in free or envelope attributes, for example "resistors 0603: 10k x4, 12k x3, 8k x1 — one value? power and package identical; 12k to 10k is -17 %, 8k to 10k is +25 %" and "screws M4: 10 mm x6, 12 mm x2, 16 mm x1 — one length? the 16 mm position has 14 mm of stack". One tool line per tool attribute: "screw drives: hex 3 mm x6, Torx T20 x2, Phillips PH2 x1 — all hex 3 mm? (one driver)". A line names the attribute that blocks a merge when the data shows one (envelope violated), otherwise it asks. It never mutates anything. On the pcb side the exported BOM file keeps JLCPCB's fixed upload headers byte-for-byte; the footer goes in the response head beside the existing datasheet-status block (`PcbHandler._datasheet_status_block`), not in the file.

**Why-not memory.** The human answers once: `link(..., rel='kept-distinct', meta={'reason': ...})` between the two parts, or a tag on the BOM root, suppresses that group's line and prints the reason instead ("kept distinct: 8k sets the LED current"). An accepted merge is an ordinary edit of the design; the nudge disappears because the group does.

**Standard parts.** A curated `src/precis/data/standard_parts.json` beside `component_series.json`: the house set (an E-series subset such as E6 values in 0603 and 0805, ISO 4762 in a few sizes, hex drive, a few connector families). When a group has a candidate in the house set the nudge proposes that one. Cross-project usage ("10k 0603 is used in 4 other designs") is a count over BOM roots, used as a ranking signal. Live supplier stock comes from `precis.supply.base.quote` where a supplier is configured, following the stock-as-selection-signal decision in `se-off-the-shelf-fabrication.md` (section "Stock as a selection signal"): fit gates, then stock, then tier, then standard-set preference, then price.

Slices, in order: (1) attribute roles for resistors, capacitors and screws, grouping, and nudge lines in both bom views including the tool line; (2) kept-distinct memory; (3) `standard_parts.json` and the cross-design usage count.

## Explicitly NOT in scope

- Automatic substitution; the nudge only asks.
- Writing to designs; merging is the human's edit.
- Supplier data stored in the DB. Digi-Key, Farnell and Mouser data stays link plus identifiers only (repo rule); stock is fetched live.
- A real inventory count (stock on hand per part); slice 3 counts design usage only. Inventory is a later slice.
- Semantic matching of free-text descriptions; grouping runs on declared attributes only.

## Acceptance criteria

- A fixture BOM with 4x 10k, 3x 12k and 1x 8k, all 0603, produces exactly one resistor consolidation line, and it names 10k.
- A fixture with M4x10, M4x12 and M4x16 where one stack is 14 mm names the 16 mm position as the blocker.
- A fixture with three drive types (hex, Torx, Phillips) produces exactly one tool line.
- A `kept-distinct` link between two parts suppresses their group's line and prints the link's reason.
- Both bom views are byte-identical to today's output above the footer; the exported pcb BOM file is unchanged.
- Nothing in the design, links or DB changes as a result of rendering a bom view.

## Target + blast radius

`src/precis/handlers/component.py` (`_render_bom`, spec registry), `src/precis/handlers/pcb.py` (bom export response), `src/precis/data/component_series.json` (role annotations), new `src/precis/data/standard_parts.json`, reads of `parts.params`, `pcb_components`, `pcb_instances`, the `link` verb for `kept-distinct`, `precis.supply.base` for optional stock. Skills `precis-overview` and any bom-describing skill change if the footer ships. Read-only on designs; blast radius is the bom response text.

## Open questions / decisions log

- Which attribute roles are per-category data (in `component_series.json`) and which are code; PCB part classes have no equivalent registry today.
- Whether the tolerance of the circuit (as opposed to the part) can ever be known without asking a human; until then every PCB value line asks rather than asserts.
- Whether the tool line belongs in the se `print` and assembly skill as well as the bom views.
