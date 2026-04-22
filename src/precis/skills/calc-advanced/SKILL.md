---
name: calc-advanced
description: >
  Power-user calculator — symbolic manipulation, calculus (integrate /
  diff / limit), equation solving, matrix / linear algebra, and unit
  conversion (via sympy.physics.units).  Free, local, deterministic.
  Read `skill:calc-basics` first if you just need arithmetic or number
  bases.
user-invocable: true
argument-hint: [expression]
allowed-tools: [get]
applies-to: [calc]
tags: [math, calculus, algebra, matrix, units]
---

## When to use

- Indefinite or definite integrals, derivatives, limits, series
- Solve polynomials or systems of equations
- Matrix ops — determinant, inverse, eigenvalues, rank
- Expand / factor / simplify / collect a symbolic expression
- Convert between units (feet ↔ meters, watts ↔ calories/s, …)

For everyday arithmetic, roots, and number bases, see `skill:calc-basics`.

For fuzzy natural-language questions ("boiling point of nitrogen",
"compare momentum of X and Y") use the paid `math:` (Wolfram) kind.

## Symbols

These are pre-declared and ready to use with no setup:

``x y z t n k a b r theta phi alpha omega``

Need more?  Use `symbols('p q s')` inline, or use any lowercase single
letter — the parser treats it as a symbol when it's not already defined.

## Calculus

### Indefinite integrals

```
get(id='calc:integrate(sin(x)*cos(x), x)')      → sin(x)**2/2
get(id='calc:integrate(1/(1+x^2), x)')          → atan(x)
get(id='calc:integrate(exp(x)*sin(x), x)')      → exp(x)*sin(x)/2 - exp(x)*cos(x)/2
```

### Definite integrals

Pass a tuple `(var, lower, upper)`.  `oo` is infinity.

```
get(id='calc:integrate(exp(-x^2), (x, -oo, oo))')  → sqrt(pi)
get(id='calc:integrate(1/x, (x, 1, E))')           → 1
get(id='calc:integrate(x^2, (x, 0, 1))')           → 1/3
```

### Derivatives

```
get(id='calc:diff(x^3 + 2x, x)')                → 3*x**2 + 2
get(id='calc:diff(sin(x)*cos(x), x)')           → -sin(x)**2 + cos(x)**2
get(id='calc:diff(x^3, x, 2)')                  → 6*x        # second derivative
```

### Limits

```
get(id='calc:limit(sin(x)/x, x, 0)')            → 1
get(id='calc:limit((1+1/n)^n, n, oo)')          → E
get(id='calc:limit(1/x, x, 0, "+")')            → oo         # one-sided
```

### Series expansion

```
get(id='calc:series(exp(x), x, 0, 6)')          → 1 + x + x**2/2 + x**3/6 + x**4/24 + x**5/120 + O(x**6)
```

### Sums and products

```
get(id='calc:Sum(1/k^2, (k, 1, oo)).doit()')    → pi**2/6
get(id='calc:Sum(k, (k, 1, n)).doit()')         → n*(n+1)/2
```

## Equation solving

### Polynomials and single equations

Pass the expression equated to zero.

```
get(id='calc:solve(x^2 - 4, x)')                → [-2, 2]
get(id='calc:solve(x^3 - 1, x)')                → [1, -1/2 - sqrt(3)*I/2, -1/2 + sqrt(3)*I/2]
get(id='calc:solve(sin(x), x)')                 → [0, pi]
```

### Systems

Pass a list of expressions and a list of unknowns.

```
get(id='calc:solve([x + y - 3, x - y - 1], [x, y])')   → {x: 2, y: 1}
```

### Numerical roots

When exact roots are unavailable or unhelpful:

```
get(id='calc:nroots(x^5 - x - 1)')              → [1.16730..., -0.76488... +/- 0.35247*I, ...]
```

## Symbolic manipulation

| Function    | What it does                                  |
|-------------|-----------------------------------------------|
| `simplify`  | general simplification (slow)                 |
| `expand`    | expand products / powers                      |
| `factor`    | factor a polynomial                           |
| `collect`   | group terms by a variable                     |
| `cancel`    | cancel common factors in a rational function  |
| `apart`     | partial-fraction decomposition                |
| `together` | combine over a common denominator              |
| `trigsimp`  | apply trig identities                         |
| `powsimp`   | combine powers                                |
| `radsimp`   | rationalise denominators                      |

```
get(id='calc:expand((x+1)^3)')                  → x**3 + 3*x**2 + 3*x + 1
get(id='calc:factor(x^2 - 4)')                  → (x - 2)*(x + 2)
get(id='calc:collect(x*y + x - 3*y + 3, x)')    → x*(y + 1) - 3*y + 3
get(id='calc:apart(1/((x-1)*(x-2)))')           → -1/(x - 1) + 1/(x - 2)
get(id='calc:trigsimp(sin(x)^2 + cos(x)^2)')    → 1
```

## Linear algebra

Build a matrix with `Matrix([[row1], [row2], …])`.

```
get(id='calc:Matrix([[1,2],[3,4]]).det()')      → -2
get(id='calc:Matrix([[1,2],[3,4]]).inv()')      → [[-2, 1], [3/2, -1/2]]
get(id='calc:Matrix([[1,2],[3,4]]).T')          → transpose
get(id='calc:Matrix([[0,1],[-2,-3]]).eigenvals()')  → {-1: 1, -2: 1}
get(id='calc:Matrix([[1,2],[3,4]]).rank()')     → 2
get(id='calc:eye(3)')                           → 3×3 identity
get(id='calc:zeros(2, 3)')                      → 2×3 zero matrix
get(id='calc:diag(1, 2, 3)')                    → diag([1, 2, 3])
```

Linear systems: `linsolve(equations, vars)` or `solve(...)`:

```
get(id='calc:linsolve([x + y - 3, x - y - 1], [x, y])')   → {(2, 1)}
```

Use `/pretty` to render matrices as boxes:

```
get(id='calc:Matrix([[1,2],[3,4]]).inv()/pretty')
⎡-2    1  ⎤
⎢         ⎥
⎣3/2  -1/2⎦
```

## Units

SymPy's `sympy.physics.units` provides a built-in unit system.  Use
`convert_to(quantity, target)` to get the equivalent in another unit.

### Available units (most common)

- **Length**: `meter`, `kilometer`, `centimeter`, `millimeter`,
  `micrometer`, `inch`, `foot`, `feet`, `yard`, `mile`
- **Mass**: `kilogram`, `gram`, `milligram`, `microgram`, `pound`,
  `tonne`, `amu`, `dalton`
- **Time**: `second`, `minute`, `hour`, `day`, `year`, `millisecond`,
  `microsecond`
- **Energy/Power**: `joule`, `electronvolt`, `watt`
- **Force/Pressure**: `newton`, `pascal`, `bar`, `atmosphere`, `psi`
- **Temperature**: `kelvin` (offset-based units like Celsius /
  Fahrenheit aren't exposed — use Wolfram `math:` for those)
- **Electric**: `volt`, `ohm`, `coulomb`, `farad`, `ampere`
- **Frequency**: `hertz`
- **Angle**: `degree`, `radian`
- **Constants**: `speed_of_light`, `gravitational_constant`, `planck`,
  `hbar`, `avogadro`, `elementary_charge`

Single-letter abbreviations (`m`, `g`, `s`, `c`, `J`, `W`…) are NOT
defined — they collide with symbol names.  Use the full word.

### Examples

```
get(id='calc:convert_to(5*foot, meter)')
  → 381*meter/250         (numeric: 1.524*meter)

get(id='calc:convert_to(100*mile/hour, meter/second)')
  → 5588*meter/(125*second)   (numeric: 44.704*meter/second)

get(id='calc:convert_to(1*kilogram * speed_of_light^2, electronvolt)')
  → energy equivalent of 1 kg in eV

get(id='calc:convert_to(360*degree, radian)')
  → 2*pi*radian
```

### What's missing?

SymPy 1.14 does not ship `horsepower`, `calorie`, `kilocalorie`, or
`kilojoule`.  Compose them inline:

```
get(id='calc:convert_to(100*watt, 1000*joule/second)')   # "kilojoule/s"
```

Or reach for the paid `math:` kind for anything exotic.

## Output views

Same as `skill:calc-basics`:

- `/pretty` — Unicode box drawings (matrices shine here)
- `/latex` — LaTeX string, ready to paste
- `/numeric` — `.evalf()` only
- `/help` — inline onboarding

```
get(id='calc:integrate(x^2, x)/latex')          → \frac{x^{3}}{3}
```

## Notation recap

- `^` is power, `^^` isn't anything
- `2x`, `3sin(x)`, `4(x+1)` — implicit multiplication works
- `1/2` is `Rational(1,2)`, not `0.5`
- `0xff`, `0b1010`, `0o17` are Python-style integer literals
- `oo` is infinity, `-oo` is negative infinity, `zoo` is complex infinity
- Commas separate function arguments; tuples in definite integrals use
  `(var, lower, upper)`

## Safety note

Dunder access (`.__class__`, `__import__`, …), comprehensions,
lambdas, walrus, f-strings, and `yield`/`await` are blocked at parse
time.  This is a calculator, not an eval sandbox.  Don't feed it
untrusted input — the safety layers are defence-in-depth, not a formal
guarantee.
