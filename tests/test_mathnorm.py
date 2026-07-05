"""Gold-set for :func:`precis.draftimport.mathnorm.normalize_math`.

Cases are drawn from the real prod ``equation``-chunk corpus (drafts): bare
LaTeX, ``\\label``-bearing bodies, nested environments, and align/gather forms.
The contract: output is a single-line ``$$ … $$`` KaTeX renders — no
``\\label``, no outer ``equation``/``align``/``gather`` env, alignment tokens
always inside an ``aligned``/``gathered`` (or pre-existing) environment.
"""

from __future__ import annotations

import re

import pytest

from precis.draftimport.mathnorm import normalize_math

# Every non-empty result is a single-line $$ … $$ with no stray label.
GOLD: list[tuple[str, str]] = [
    # 1. bare LaTeX (the ~94% case) → just wrapped
    (r"\chi = V - E + F.", r"$$ \chi = V - E + F. $$"),
    # 2. leading \label stripped (KaTeX chokes on it)
    ("\\label{eq:euler}\n\\chi = V - E + F.", r"$$ \chi = V - E + F. $$"),
    # 3. \label mid-body + boxed
    (
        r"\label{eq:burnside} \boxed{|X/O| = \frac{c^6 + 3c^4}{24}}",
        r"$$ \boxed{|X/O| = \frac{c^6 + 3c^4}{24}} $$",
    ),
    # 4. explicit $$…$$ delimiters already present → normalised, not doubled
    ("$$ E = mc^2 $$", r"$$ E = mc^2 $$"),
    # 5. \[ … \] display delimiters → $$
    (r"\[ a^2 + b^2 = c^2 \]", r"$$ a^2 + b^2 = c^2 $$"),
    # 6. outer equation env unwrapped
    (
        r"\begin{equation} F = ma \end{equation}",
        r"$$ F = ma $$",
    ),
    # 7. align* env → aligned
    (
        "\\begin{align*}\na &= b \\\\\nc &= d\n\\end{align*}",
        r"$$ \begin{aligned} a &= b \\ c &= d \end{aligned} $$",
    ),
    # 8. gather env → gathered
    (
        "\\begin{gather}\nx = 1 \\\\\ny = 2\n\\end{gather}",
        r"$$ \begin{gathered} x = 1 \\ y = 2 \end{gathered} $$",
    ),
    # 9. bare aligned rows (importer stored inner-only) → wrapped in aligned
    (
        "a &= b \\\\ c &= d",
        r"$$ \begin{aligned} a &= b \\ c &= d \end{aligned} $$",
    ),
    # 10. \label + nested \begin{aligned} → label gone, aligned kept, not double-wrapped
    (
        "\\label{eq:poly}\n\\begin{aligned}\n\\sum_n P_n &= 6 \\\\ 2P_4 &= 0\n\\end{aligned}",
        r"$$ \begin{aligned} \sum_n P_n &= 6 \\ 2P_4 &= 0 \end{aligned} $$",
    ),
    # 11. multline → aligned
    (
        r"\begin{multline} A + B \\ + C \end{multline}",
        r"$$ \begin{aligned} A + B \\ + C \end{aligned} $$",
    ),
    # 12. single-row, no alignment tokens → bare, no aligned wrapper
    (r"\Delta H = -801.7\,\text{kJ}", r"$$ \Delta H = -801.7\,\text{kJ} $$"),
]


@pytest.mark.parametrize("raw,expected", GOLD)
def test_normalize_math_gold(raw: str, expected: str) -> None:
    assert normalize_math(raw) == expected


def test_label_only_body_reduces_to_empty() -> None:
    # A \label-only block carries no math → empty; caller drops it.
    assert normalize_math(r"\label{eq:orphan}") == ""
    assert normalize_math("   \\label{eq:x}  \n ") == ""


def test_empty_input() -> None:
    assert normalize_math("") == ""
    assert normalize_math("   ") == ""


@pytest.mark.parametrize("raw,_expected", GOLD)
def test_no_label_survives(raw: str, _expected: str) -> None:
    assert "\\label" not in normalize_math(raw)


@pytest.mark.parametrize("raw,_expected", GOLD)
def test_no_outer_display_env_survives(raw: str, _expected: str) -> None:
    out = normalize_math(raw)
    for env in ("equation", "align", "gather", "multline", "displaymath", "eqnarray"):
        assert f"\\begin{{{env}}}" not in out
        assert f"\\begin{{{env}}}" not in out.replace("*", "")


@pytest.mark.parametrize("raw,_expected", GOLD)
def test_single_line_dollar_wrapped(raw: str, _expected: str) -> None:
    out = normalize_math(raw)
    assert "\n" not in out
    assert out.startswith("$$ ") and out.endswith(" $$")
    # Balanced \begin/\end after normalisation.
    assert len(re.findall(r"\\begin\{", out)) == len(re.findall(r"\\end\{", out))
