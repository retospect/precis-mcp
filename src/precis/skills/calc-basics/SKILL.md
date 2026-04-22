---
name: calc-basics
description: >
  Everyday calculator — arithmetic, exact fractions, square roots, trig,
  constants, and number-base conversion (hex ↔ bin ↔ oct ↔ dec).  Use for
  any numeric "what is X" question that doesn't need calculus, matrices,
  or unit conversion.  Free, local, deterministic.
user-invocable: true
argument-hint: [expression]
allowed-tools: [get]
applies-to: [calc]
kind-onboarding: calc
tags: [math, calculator, arithmetic, numbers]
---

## When to use

- User asks "what's 2^10 + 3*(4+5)"
- User wants a clean exact fraction (``1/3 + 1/6``)
- Quick hex/bin/oct conversion
- Trig / sqrt / factorial / log lookup
- Anything where you'd reach for a scientific calculator

For anything with an unknown variable (``solve``, ``integrate``, matrices,
unit conversion) see `skill:calc-advanced`.

For natural-language questions (``"population of Ireland"``, ``"boiling
point of water"``) use the paid `math:` (Wolfram) kind instead — it
charges per call but handles fuzzy input and real-world data lookups.

## Notation

Write math the way a scientific calculator expects.  Both styles work:

| You type           | Meaning                 |
|--------------------|-------------------------|
| `2 + 3 * 4`        | arithmetic              |
| `2^10` or `2**10`  | power                   |
| `2x + 3`           | implicit multiplication |
| `1/2`              | exact rational (½)      |
| `0.1 + 0.2`        | 3/10 — decimals become rationals |
| `sqrt(2)`          | √2 (exact + numeric)    |
| `sin(pi/4)`        | trig, radians           |
| `(3+4I)*(5-2I)`    | complex arithmetic, `I` = √−1 |

Constants available: `pi`, `E`, `I`, `oo` (infinity), `GoldenRatio`,
`EulerGamma`, `Catalan`.

Functions: `sin cos tan asin acos atan sinh cosh tanh exp log ln sqrt
cbrt Abs factorial gamma gcd lcm floor ceiling binomial`.

## Arithmetic

```
get(id='calc:2+3*4')             → 14
get(id='calc:2^10 + 3(4+5)')     → 1051
get(id='calc:7!')                → 5040     # use factorial(7) if ! confuses
get(id='calc:factorial(7)')      → 5040
get(id='calc:binomial(52, 5)')   → 2598960
```

## Exact fractions

SymPy keeps results exact — no float drift.

```
get(id='calc:1/3 + 1/6')         → 1/2
get(id='calc:0.1 + 0.2')         → 3/10       (exact!)
get(id='calc:22/7 - pi')         → -pi + 22/7
```

Force a decimal with `/numeric`:

```
get(id='calc:sqrt(2)/numeric')   → 1.41421356237309...
```

## Roots and powers

```
get(id='calc:sqrt(2)')           → sqrt(2)         + numeric: 1.41421...
get(id='calc:sqrt(50)')          → 5*sqrt(2)       (auto-simplified)
get(id='calc:cbrt(27)')          → 3
get(id='calc:2^(1/2)')           → sqrt(2)
get(id='calc:root(16, 4)')       → 2
```

## Trigonometry — radians

```
get(id='calc:sin(pi/2)')         → 1
get(id='calc:cos(pi/6)')         → sqrt(3)/2
get(id='calc:sin(pi/6)')         → 1/2
get(id='calc:atan(1)')           → pi/4
```

Degrees: multiply by `pi/180`, or use `convert_to` (see `skill:calc-advanced`).

## Number-base conversion

The calculator accepts Python-style integer literals AND the `hex`,
`bin`, `oct`, `int` builtins.  When the input mentions any of these, the
result is reported in all four bases.

```
get(id='calc:0xff')              → 255    +Hex 0xff  Bin 0b11111111  Oct 0o377
get(id='calc:0b1010 + 0x1')      → 11
get(id='calc:hex(255)')          → '0xff' (a string)
get(id='calc:bin(42)')           → '0b101010'
get(id='calc:int("ff", 16)')     → 255
get(id='calc:0o17 * 2')          → 30
```

## Complex numbers

```
get(id='calc:(3+4I)*(5-2I)')     → (3 + 4*I)*(5 - 2*I)
                                   numeric: 23.0 + 14.0*I
get(id='calc:Abs(3+4I)')         → 5
get(id='calc:arg(1+I)')          → pi/4
```

Wrap in `expand(...)` to get the simplified polynomial form:

```
get(id='calc:expand((3+4I)*(5-2I))')  → 23 + 14*I
```

## Views — alternate output formats

Append a known view name to the expression as a trailing path segment:

| View        | Output                                    |
|-------------|-------------------------------------------|
| *default*   | Input, Exact, Numeric, (bases if relevant)|
| `/pretty`   | Unicode pretty-print (good for matrices)  |
| `/latex`    | LaTeX source string                       |
| `/numeric`  | Decimal approximation only (no header)    |
| `/help`     | This onboarding, inline                   |

```
get(id='calc:pi/4/latex')        → \frac{\pi}{4}
get(id='calc:pi/4/numeric')      → 0.785398163397448
```

The URI parser treats `calc:` as opaque so ordinary division (`/`) stays
inside the expression.  Only the *trailing* slash immediately before a
known view name is special.

## Common pitfalls

- **`^` is power, not XOR.** The parser converts it.
- **`pi` not `π`** — unicode math symbols aren't in the namespace.
- **Single-letter unit abbreviations are not defined** (`m`, `c`, `J`…).
  Use full names (`meter`, `speed_of_light`, `joule`) — see
  `skill:calc-advanced`.
- **Dunder / underscore access is blocked.** `x.__class__` won't evaluate
  — by design.
- **No variable assignment.** The calculator is stateless; you can't do
  `a = 2; a + 3`. Use nested expressions.

## See also

- `skill:calc-advanced` — calculus, matrices, `solve`, units, symbolic
  manipulation.
- `get(id='math:...')` — natural-language math + real-world lookups
  (**paid**, requires `WOLFRAM_APP_ID`).
