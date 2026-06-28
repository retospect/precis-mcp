---
id: precis-calc-help
title: precis — the calc kind (local SymPy CAS)
summary: an exact, free, local computer-algebra system — arithmetic, calculus (integrals/derivatives/limits/sums/ODEs), equation solving, algebra, linear algebra, number theory; trig in degrees by default with a view='rad' switch
applies-to: get (kind='calc')
status: active
---

# precis-calc-help — exact local math via SymPy

`calc` is a **local, free, exact** computer-algebra system (SymPy). Pass
an expression as `q=` (or `id=`) and the value comes back — no rounding,
no API, no cost. It is *not* a four-function calculator: it integrates,
solves, factors, and does linear algebra.

```python
get(kind='calc', q='2+3*4')            # → 14
get(kind='calc', q='sqrt(2)')          # → sqrt(2)   (exact, not 1.4142…)
get(kind='calc', q='Rational(1,3)+Rational(1,6)')   # → 1/2
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
get(kind='calc', q='sin(30)')          # → 1/2
                                       #   (trig evaluated in degrees — pass view='rad' for radians)
get(kind='calc', q='tan(45)')          # → 1
get(kind='calc', q='N(atan2(1,1))')    # → 45.0   (inverse trig returns degrees too)
```

Pass `view='rad'` for SymPy's native **radians** — what you want for
symbolic calculus, where a degree wrapper would turn the integrand into
`sin(pi*x/180)`:

```python
get(kind='calc', q='sin(pi/6)',          view='rad')   # → 1/2
get(kind='calc', q='integrate(sin(x), x)', view='rad') # → -cos(x)
```

`view='deg'` is an explicit synonym for the default. No note appears in
radian mode or when an expression uses no trig.

## Errors are actionable

`calc` does math, not I/O — Python builtins (`random()`, `os.system`) and
bare prose ("one plus two") are refused with a copy-pasteable `next=`
example. If an expression "simplifies to itself", give SymPy more
structure: wrap it in `solve(Eq(lhs, rhs), var)`.
