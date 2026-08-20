---
id: precis-notation-canon
title: precis — notation canon for authored claim sentences
summary: unit/exponent/dash/approximation spelling rules for a taproot claim sentence or nanopub title — notation is hashed into pub_id, so drift mints a duplicate hub
applies-to: put/edit(kind='finding') claim-hub title text; taproot/notation.py::lint_notation; nanopub mint-gate title checks
status: active
---

# precis-notation-canon — one spelling per quantity

Claim sentences are hashed to derive `pub_id`
(`identity.py::normalize_text_for_hash` → NFKD-fold, lowercase,
whitespace-collapse). So notation is **load-bearing, not cosmetic**:
NFKD already forgives some spelling differences, and splits identity on
the rest.

**The second reason is reuse, and it settles the judgment calls.** A claim
sentence is read far more often by a machine than by a person: the first
read establishes that the claim and its citation are correct, every read
after that is an agent citing an already-verified hub. Normalize for *that*
reader — one spelling per quantity, no ambiguity to resolve, fewer tokens
to carry. `µS` beats `microsiemens` on both counts at once. This is the
test to apply when a rule is arguable: does it remove work for the second
reader? Kerning does not (`C₆₀` renders tightly and is still correct);
a bare `450C` does, because it could be Celsius, coulombs or carbon.

Forgiven (these converge for free): `cm²`≡`cm2`, `C₆₀`≡`C60`, `10⁵`≡`105`,
`µ` U+00B5 ≡ `μ` U+03BC.

**Not** forgiven — each of these mints a *second hub for the same claim*:
unit-internal spacing (`cm²V⁻¹s⁻¹` vs `cm² V⁻¹ s⁻¹`); solidus vs negative
exponent (`cm²/Vs` vs `cm² V⁻¹ s⁻¹`); superscript minus (`⁻` U+207B folds
to `−` U+2212, **not** to ASCII `-`); digit grouping (`4,600` vs `4600`).

| question | rule |
|---|---|
| exponents | UTF-8 superscript — `cm²`, `10⁻¹⁰`. Never `cm^2`, never TeX `$\mathrm{cm}^2$`. |
| compound units | negative exponents, space-separated: `cm² V⁻¹ s⁻¹`. |
| single denominator | solidus is fine and preferred: `mA/µm`, `mV/dec`, `S/cm`, `dI/dV`. Never a solidus with two denominators (`cm²/Vs` is ambiguous). |
| minus | `⁻` U+207B inside superscripts, `−` U+2212 standalone. Never ASCII hyphen as a minus. |
| digit grouping | **none** — `4600`, not `4,600`. Grouping is locale-dependent and is the single largest hash-splitter. |
| order of magnitude | SI prefix when one exists and the mantissa lands in 0.1–1000 (`4.6 nm`, `250 µm`); power of ten otherwise (`10¹¹ cm⁻²`, `1.0×10⁻⁶ S`). Never both (`10⁻⁶ µm`). |
| unit system | SI, except a field's own conventional unit where it is unambiguous (eV, Å, bar, °C, ppm, mol%, wt%). |
| multiplication | `×` U+00D7. Never `x`, never `*`. Spaced for arithmetic and dimensions (`3 × 10⁸`, `2 × 2 supercell`), unspaced for magnification and fold-change (`100×`, `3500×`). Every sense of `×` is multiplicative, so it never needs disambiguating — but see carve-out 3: a lowercase `x` in `Li_xCoO₂` or `Cu₂₋ₓS` is a composition variable, not a multiplier. |
| dashes | en dash `–`, unspaced, only for numeric ranges (`19–39°`) and compound method/element names (`DFT–NEGF`, `Cu–Zn`) — never as a clause separator. Em dash `—` (and ASCII stand-ins ` -- ` / spaced ` - `) is banned outright — see "Em-dash is never a claim separator" below. |
| negative-endpoint ranges | if either endpoint is negative, write the word: `−5 to 10 °C`, never `−5–10 °C`. An en dash abutting a minus sign is unreadable and genuinely ambiguous. |
| approximation | `≈`, no space before a bare quantity (`≈1 Å`); space when it's a binary relation with a symbol on the left (`n ≈ 10²²`). Only for a numeral — `~` between two expressions is proportionality (`E_g ~ 1/W`), not approximation, and stays `~`. |
| percent / degrees | `50%`, `85°` unspaced; `300 K`, `25 °C` spaced. |
| temperature scale | **keep the scale the authors used** — never convert °C to K (carve-out 2). `°K` is banned outright (abolished 1967); a bare `C` after a number becomes `°C`, since `450C` is ambiguous between Celsius, coulombs and carbon. |

**Letter sub/superscripts stay ASCII.** `K_d`, `E_g`, `ΔG_aq`, `R_Q`, `2^N`
keep underscore/caret form — don't hunt for a subscript letter. Unicode has
no subscript glyph for most letters (`d`, `g`, …), and the modifier
superscripts that do exist (`ᴺ`, U+1D3A) are a different character class
that renders inconsistently across surfaces. Only digits and `+`/`−`
actually sub/superscript (`cm²`, `10⁻¹⁰`); a letter stays ASCII
underscore/caret. All three normalization agents on the 2026-08-19 pass
improvised this rule independently — the signal it needed writing down,
not that it was obvious.

**ASCII → UTF-8 fallback — closed list.** Apply these spelling
substitutions wherever ASCII notation reaches an extraction:

| ASCII | UTF-8 | condition |
|---|---|---|
| `+/-` | `±` | a numeral on **both** sides |
| `ug` | `µg` | none — symbol → symbol |
| `micro`+unit word | `µ`+symbol | a numeral on the left |
| `degrees C`, `degree C` | ` °C` | a numeral on the left |
| `micrometer`, `micron` | `µm` | a numeral on the left |
| `x`, `*` (multiplication) | `×` | none |
| `Ohm` | `Ω` (so `kOhm` → `kΩ`) | none |
| `Angstrom` | `Å` | none |

**The numeral condition is load-bearing, not pedantry.** SI writes a unit
*symbol* with a numerical value and the unit *name* in words, so
`50 micrometres` → `50 µm` but `micron-scale particles` stays spelled out
— there is no value for the symbol to qualify. Without the `±` condition
the rule eats an oxidation state: `Zn2+-sensing` → `Zn2±sensing`, a
different and meaningless string that would have been signed into an
artifact. A rule that rewrites *some* occurrences of a unit must rewrite
all of them in the sentence — two spellings of one unit is worse than
either input. (Canon v3.1, `src/precis/taproot/notation.py`; the guard is
`_NUMERAL_LEFT`.)

Closed — don't extrapolate a spelling that isn't on this list without
adding a row. **Symbol respelling is not unit conversion.** Carve-out 2
below ("never convert the paper's unit") blocks changing a quantity's
*unit*; it does not block writing that same unit's UTF-8 symbol instead of
its ASCII spelling — `ug` → `µg` stays micrograms, it is not a conversion
to milligrams. Apply the fallback table wherever its condition column is
met; carve-out 2 does not gate it. (All three normalization agents on the
2026-08-19 pass stalled on this exact ambiguity — it must not be misread
again.)

**Em-dash is never a claim separator.** ` — ` (and its ASCII stand-ins
` -- ` and spaced ` - ` used the same way) splits a citation from a topic
— `Landauer 1957/1970 — conductance as transmission`, `Yoon & Guo 2007 —
NEGF upper bounds on GNR-FET performance, APL 91`. Measured across the
live corpus: 90 hubs contain an em-dash and **all 90** are this label
shape — none is a legitimate parenthetical. If the material really is
subordinate, replace the dash with a comma; far more often the two sides
are a citation and a topic, which means [[precis-taproot-mint-help]]'s "Claim
admissibility" was failing outright and the ref should never have carried
`TAPROOT:claim`. This is the single most reliable *syntactic* marker of
the bibliography-stub failure mode — that's why it earns its own rule
instead of living under terseness. En dash stays legal for ranges and
compound names (table above); do not let this rule bleed into those 102
correct uses, 42 of them numeric ranges.

**Terseness is a rule, not a preference.** Prefer the shortest sentence
that stays falsifiable and self-contained (see [[precis-taproot-mint-help]]'s
"Claim admissibility"). A sentence joining two assertions with "and" is
two atoms — split it; a 506-character claim is not an atomic claim.
Shorter atoms also close a known failure: the SMALL-tier extractor
collapses a multi-clause claim into one truncated atom, and an atom that
can't be multi-clause can't collapse that way.

Three carve-outs, all of which **outrank the table**:

1. **Quote-containment wins.** The nanopub mint gate requires a structured
   quantity to appear in the quoted passage. If the source writes
   `0.05 ps` and never writes `50 fs`, keep `0.05 ps` — normalizing the
   magnitude would strand the claim outside its own evidence.
2. **Never convert the paper's unit.** Report the quantity in the unit the
   authors used (psi, barg, mol%, wt%, Å). Converting a measurement into
   units nobody wrote is its own kind of misquotation; record the
   conversion in `scope` if it helps, never in the sentence. This does not
   cover symbol respelling — the ASCII→UTF-8 fallback above is spelling,
   not conversion, and always applies.
   Temperature is the case that tempts hardest, so state it plainly: a
   scale carries information the number does not. `77 K` means liquid
   nitrogen, `4.2 K` liquid helium, `25 °C` ambient, `298 K` the standard
   reference state — converting destroys the signal that the value was
   *chosen*. Converting would also strand the quantity outside its own
   quote and fail the mint gate, and it invites the classic slip that a
   temperature *difference* of 5 °C is 5 K while a temperature *of* 5 °C
   is 278.15 K. **The sentence keeps the authors' scale; `scope` may carry
   the SI equivalent** — scope is for machine filtering, the sentence is
   for fidelity.
3. **Nomenclature is not notation.** Hyphens inside a defect or phase name
   (`5-8-5`, `H₅,₆,₇`) are name separators, not minus signs — leave them.
   The same holds for digits after a letter: they are not always
   stoichiometry. Crown ethers and cryptands reuse `C` for "crown"
   (`DB18C6`, `15C5`), and `B3LYP`, `M06-2X`, `S22`, vitamin `B12`, point
   group `C4` and alkyl-chain labels (`C10-DNTT`) are all names. Measured
   over the live corpus, ~23% of naive element-plus-digit matches are
   nomenclature — which is why `formula-ascii-subscript` ships
   **detector-only** and is never auto-fixed. A lowercase `x` in a formula
   (`Li_xCoO₂`, `Cu₂₋ₓS`, `Ba₁₋ₓSrₓTiO₃`) is a composition variable and must
   never be rewritten to `×`. ASCII ionic charges (`Mg2+`, `Gd3+`) want a
   *superscript*, not a subscript.

**Quotes are verbatim and are never normalized.** The canon governs the
*authored claim sentence* only; rewriting a quote to match it fails the
mint gate.

## See also

```python
get(
    kind="skill", id="precis-taproot-mint-help"
)  # mint/reword doors, admissibility rubric
get(kind="skill", id="precis-nanopub-help")  # claim-sentence grammar, mint gates
```
