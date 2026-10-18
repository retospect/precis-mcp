"""Hover/placeholder hint lint over the web templates (batch W, S4).

The rule, for every ``templates/**/*.j2``:

* every text-like ``<input>`` (type text, search, url, email, or no type)
  and every ``<textarea>`` carries a ``placeholder`` (literal, ``:placeholder``
  or ``x-bind:placeholder``); ``type="hidden"`` inputs are exempt;
* an icon-only ``<button>`` (no letters or digits in its text, so ✎ ⋯ × ▾)
  carries ``title``, ``:title`` or ``aria-label``;
* a ``data-shortcut`` value is not repeated within one template;
* a control bound with a literal ``data-shortcut`` names its key in the hint:
  ``placeholder="Search everything (/)"`` for fields, ``title="Name (key)"``
  for buttons and links.

Jinja is stripped before parsing: ``{# #}`` and ``{% %}`` vanish (newlines
kept so line numbers stay true) and ``{{ … }}`` becomes the sentinel ``X``, so
a computed placeholder or title counts as present.

Offenders that predate the rule sit in ``template_hints_allowlist.json`` as
per-file counts. The allowlist may only shrink: a file over its count fails,
and a file under it fails too until the number is lowered (S5 work-down).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "src/precis_web/templates"
ALLOWLIST = Path(__file__).with_name("template_hints_allowlist.json")

TEXT_INPUT_TYPES = {"", "text", "search", "url", "email"}
PLACEHOLDER_ATTRS = ("placeholder", ":placeholder", "x-bind:placeholder")
TITLE_ATTRS = ("title", ":title", "x-bind:title", "aria-label", ":aria-label")
SENTINEL = "X"

_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_STMT = re.compile(r"\{%.*?%\}", re.DOTALL)
_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def strip_jinja(src: str) -> str:
    def blank(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    src = _COMMENT.sub(blank, src)
    src = _STMT.sub(blank, src)
    return _EXPR.sub(lambda m: SENTINEL + "\n" * m.group(0).count("\n"), src)


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.offenders: list[tuple[str, int, str]] = []  # (rule, line, detail)
        self.shortcuts: dict[str, list[int]] = {}
        self._button: tuple[int, dict[str, str | None], list[str]] | None = None
        self._button_has_media = False

    def handle_starttag(self, tag, attrs):
        a = {k: v for k, v in attrs}
        line = self.getpos()[0]
        if (
            tag == "input" and (a.get("type") or "").strip().lower() in TEXT_INPUT_TYPES
        ) or tag == "textarea":
            self._need_placeholder(a, line, tag)
        if tag == "button":
            self._button = (line, a, [])
            self._button_has_media = False
        elif self._button is not None and tag in ("svg", "img"):
            self._button_has_media = True
        sc = a.get("data-shortcut")
        if sc and SENTINEL not in sc:
            self.shortcuts.setdefault(sc, []).append(line)
            self._check_hint(tag, a, sc, line)

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._button is not None:
            self._button[2].append(data)

    def handle_endtag(self, tag):
        if tag != "button" or self._button is None:
            return
        line, a, text = self._button
        self._button = None
        letters = "".join(text)
        if not any(ch.isalnum() for ch in letters) and not any(
            k in a for k in TITLE_ATTRS
        ):
            self.offenders.append(("icon-button-title", line, letters.strip()))

    def _need_placeholder(self, a, line, tag):
        if not any(k in a for k in PLACEHOLDER_ATTRS):
            self.offenders.append(
                ("placeholder", line, f"<{tag} name={a.get('name')}>")
            )

    def _check_hint(self, tag, a, sc, line):
        if tag in ("input", "textarea"):
            hint = next((a[k] for k in PLACEHOLDER_ATTRS if a.get(k)), None)
        else:
            hint = next((a[k] for k in TITLE_ATTRS if a.get(k)), None)
        if hint is None or (f"({sc})" not in hint and f"({SENTINEL})" not in hint):
            self.offenders.append(("shortcut-hint", line, f"{sc!r} not named in hint"))


def scan(path: Path) -> list[tuple[str, int, str]]:
    scanner = _Scanner()
    scanner.feed(strip_jinja(path.read_text(encoding="utf-8")))
    scanner.close()
    out = list(scanner.offenders)
    for keys, lines in scanner.shortcuts.items():
        if len(lines) > 1:
            out.append(("shortcut-dup", lines[1], f"{keys!r} on lines {lines}"))
    return sorted(out, key=lambda o: (o[0], o[1]))


def scan_all() -> dict[str, list[tuple[str, int, str]]]:
    found = {}
    for p in sorted(TEMPLATES.rglob("*.j2")):
        hits = scan(p)
        if hits:
            found[p.relative_to(TEMPLATES).as_posix()] = hits
    return found


def counts() -> dict[str, dict[str, int]]:
    """{rule: {template: n}} — the shape ``template_hints_allowlist.json`` stores."""
    out: dict[str, Counter[str]] = {}
    for rel, hits in scan_all().items():
        for rule, _line, _detail in hits:
            out.setdefault(rule, Counter())[rel] += 1
    return {r: dict(sorted(c.items())) for r, c in sorted(out.items())}


# ── the lint ──────────────────────────────────────────────────────────


def test_template_hints_never_grow_and_allowlist_only_shrinks():
    allowed = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    now = counts()
    detail = scan_all()
    problems: list[str] = []
    for rule in sorted(set(allowed) | set(now)):
        a, n = allowed.get(rule, {}), now.get(rule, {})
        for rel in sorted(set(a) | set(n)):
            have, cap = n.get(rel, 0), a.get(rel, 0)
            if have > cap:
                lines = [f"{ln}:{d}" for r, ln, d in detail[rel] if r == rule]
                problems.append(
                    f"{rel}: {have} x {rule} (allowed {cap}) — add the hint; "
                    f"at {'; '.join(lines)}"
                )
            elif have < cap:
                problems.append(
                    f"{rel}: {have} x {rule} but the allowlist says {cap} — "
                    f"lower it to {have} in {ALLOWLIST.name}"
                )
    assert not problems, "\n".join(problems)


# ── the scanner itself ────────────────────────────────────────────────


def _scan_text(tmp_path: Path, html: str) -> list[tuple[str, int, str]]:
    p = tmp_path / "t.j2"
    p.write_text(html, encoding="utf-8")
    return scan(p)


def test_scanner_flags_missing_placeholder_but_not_hidden(tmp_path):
    hits = _scan_text(
        tmp_path,
        '<input name="a"><input type="hidden" name="b">'
        '<input type="checkbox" name="c"><textarea name="d"></textarea>'
        '<input type="search" :placeholder="p"><input placeholder="{{ p }}">',
    )
    assert [(r, d) for r, _l, d in hits] == [
        ("placeholder", "<input name=a>"),
        ("placeholder", "<textarea name=d>"),
    ]


def test_scanner_flags_icon_only_button_without_title(tmp_path):
    hits = _scan_text(
        tmp_path,
        "<button>&times;</button>\n"
        '<button title="Close">&times;</button>\n'
        '<button aria-label="More">⋯</button>\n'
        "<button>Save</button>\n"
        "<button>{{ label }}</button>\n",
    )
    assert [(r, ln) for r, ln, _d in hits] == [("icon-button-title", 1)]


def test_scanner_flags_duplicate_shortcut_and_unnamed_key(tmp_path):
    hits = _scan_text(
        tmp_path,
        '<a href="/" data-shortcut="g d" title="Drive (g d)">x</a>\n'
        '<a href="/" data-shortcut="g d" title="Drive">x</a>\n'
        '<input data-shortcut="/" placeholder="Search (/)">\n',
    )
    assert [r for r, _l, _d in hits] == ["shortcut-dup", "shortcut-hint"]


def test_jinja_is_stripped_with_line_numbers_kept():
    out = strip_jinja("a{# x\ny #}b{% if q %}c{{ v }}\n<i>")
    assert out == "a\nbcX\n<i>"
