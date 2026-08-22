---
id: precis-calc-help
title: precis — the calc kind (local SymPy CAS)
summary: an exact, free, local computer-algebra system — arithmetic, calculus (integrals/derivatives/limits/sums/ODEs), equation solving, algebra, linear algebra, number theory; trig in degrees by default with a view='rad' switch; plus local unit conversion (3 ft to m; 1 ton to kg; 100 degC to degF) via pint
answers:
  - how do I do exact arithmetic or calculus without paying for Wolfram?
  - how do I convert units locally, like feet to meters?
  - does calc use degrees or radians by default?
  - what should I do if calc returns an error?
applies-to: get (kind='calc')
status: active
---

# precis-calc-help — exact local math via SymPy

`calc` is a **local, free, exact** computer-algebra system (SymPy). Pass
an expression as `q=` (or `id=`) and the value comes back — no rounding,
no API, no cost. It is *not* a four-function calculator: it integrates,
solves, factors, and does linear algebra.

```python
get(kind="calc", q="2+3*4")  # → 14
get(kind="calc", q="sqrt(2)")  # → sqrt(2)   (exact, not 1.4142…)
get(kind="calc", q="Rational(1,3)+Rational(1,6)")  # → 1/2
```

`calc` vs `math`: `calc` is **local SymPy** — symbolic, exact, free. The
`math` kind is **Wolfram Alpha** — natural-language facts + world data,
*paid*. Reach for `math` for "population of Ireland"; reach for `calc`
for "integrate this".

## What it can do

| area | examples |
|------|----------|
| arithmetic & numerics | `2**64`, `(2+3*I)*(1-I)` (complex), `N(pi, 30)` (30-digit), exact fractions |
| roots & powers | `sqrt(2)`, `2**10`, `cbrt(27)` |
| **trigonometry** | `sin cos tan asin acos atan atan2`, `pi` — **degrees by default** (see below) |
| **calculus** | **integrals** — indefinite `integrate(x**2, x)` → `x**3/3`, definite `integrate(exp(-x**2), (x, -oo, oo))` → `sqrt(pi)`; derivatives `diff(sin(x)*x**2, x)`; limits `limit(sin(x)/x, x, 0)` → `1`; Taylor series `series(cos(x), x, 0, 6)`; summations `Sum(1/n**2, (n, 1, oo)).doit()` → `pi**2/6`; ODEs `dsolve(Eq(f(x).diff(x), f(x)), f(x))` |
| equation solving | `solve(Eq(x**2-2, 0), x)` → `[-sqrt(2), sqrt(2)]`; `solveset(...)` for sets |
| algebra | `simplify(sin(x)**2 + cos(x)**2)` → `1`, `factor(x**3-1)`, `expand((x+1)**3)` |
| linear algebra | `Matrix([[1,2],[3,4]]).inv()`, `.det()`, `.eigenvals()` |
| number theory | `factorint(360)` → `{2:3, 3:2, 5:1}`, `gcd(48,36)`, `isprime(97)` |

Use `oo` for ∞, `I` for the imaginary unit, `pi`/`E` for the constants.
Wrap any exact result in `N(...)` for a decimal (`N(pi, 30)` sets the
precision). Symbols are free — `x`, `n`, `f` need no declaration.

## Angles: degrees by default

`calc` is engineering-leaning, so **trig reads/returns degrees** unless
you say otherwise — and a result that actually used trig is stamped with
a note:

```python
get(kind="calc", q="sin(30)")  # → 1/2
#   (trig evaluated in degrees — pass view='rad' for radians)
get(kind="calc", q="tan(45)")  # → 1
get(kind="calc", q="N(atan2(1,1))")  # → 45.0   (inverse trig returns degrees too)
```

Pass `view='rad'` for SymPy's native **radians** — what you want for
symbolic calculus, where a degree wrapper would turn the integrand into
`sin(pi*x/180)`:

```python
get(kind="calc", q="sin(pi/6)", view="rad")  # → 1/2
get(kind="calc", q="integrate(sin(x), x)", view="rad")  # → -cos(x)
```

`view='deg'` is an explicit synonym for the default. No note appears in
radian mode or when an expression uses no trig.

## Unit conversion (local, via pint)

A query with an explicit **`to`**, **`in`**, or **`->`** clause is a unit
conversion — handled locally by `pint`, exact and offline, before SymPy
ever sees it. No Wolfram, no API, no cost.

```python
get(kind="calc", q="3 ft to m")  # → 3 ft = 0.9144 m
get(kind="calc", q="100 km -> mi")  # → 100 km = 62.1371 mi
get(kind="calc", q="5 km in miles")  # → 5 km = 3.10686 mi
get(kind="calc", q="ft to m")  # → ft = 0.3048 m   (bare factor, no magnitude)
get(kind="calc", q="3 ft + 2 in to cm")  # → 3 ft + 2 in = 96.52 cm   (unit arithmetic)
get(kind="calc", q="100 degC to degF")  # → 100 degC = 212 °F   (temperature)
```

**Disambiguation is the point.** Bare names resolve to a documented
default; use the explicit name when you mean the other one. `pint`
*raises* on an unknown unit rather than guessing — so a conversion is
never silently wrong.

| you write | you get | the other one |
|-----------|---------|---------------|
| `1 ton to kg` | `907.185 kg` (US short ton) | `1 metric_ton`/`1 tonne` → `1000 kg`; `1 long_ton` → `1016.05 kg` |
| `1 gallon to L` | `3.78541 l` (US) | `1 imperial_gallon to L` → `4.54609 l` |
| `1 oz to g` | `28.3495 g` (mass) | `1 fluid_ounce to mL` → `29.5735 ml` |

(Units render with pint's abbreviated symbols — liter as `l`/`ml`,
temperature as `°C`/`°F`. You may *write* `L` either way; pint accepts it.)

Errors are actionable: `3 ft to kg` (incompatible dimensions) and
`3 blorp to m` (unknown unit) both refuse with a copy-pasteable `next=`.
For a **conversion factor** with math attached, both sides can carry
units and arithmetic (`3 ft + 2 in to cm`); for pure symbolic math with
no units, it's the plain SymPy path above.

> `calc` conversions vs `math` (Wolfram): reach for `calc` for any
> ordinary unit conversion — it's local, exact, free, and instant. `math`
> stays for the natural-language / world-data long tail.

## Errors are actionable

`calc` does math, not I/O — Python builtins (`random()`, `os.system`) and
bare prose ("one plus two") are refused with a copy-pasteable `next=`
example. If an expression "simplifies to itself", give SymPy more
structure: wrap it in `solve(Eq(lhs, rhs), var)`.
