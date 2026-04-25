#!/usr/bin/env python3
"""One-shot migration from v1 wisdom + iching data to oracle papers.

Reads:
  - data/iching.yaml          (the bespoke iching kind's data file)
  - data/wisdom-starter.yaml  (the v1 wisdom kind's seed entries)

Writes one YAML per tradition into data/oracle/:
  - oracle/iching.yaml         — 64 hexagrams, three-layer body baked
  - oracle/chengyu.yaml        — Chinese 4-character idioms
  - oracle/proverbs-euro.yaml  — European proverbs
  - oracle/proverbs-irish.yaml — Irish proverbs (Gaeilge + English)
  - oracle/stoic.yaml          — Stoic + classical aphorisms
  - oracle/engineering.yaml    — Knuth, Chesterton, Hyrum, Gall, Postel, …
  - oracle/talmudic.yaml       — Pirkei Avot etc.
  - oracle/zen.yaml            — Koan-flavoured aphorisms

Output schema (consumed by ``precis-ingest-oracle``)::

    slug: <tradition>           # → ref slug oracle:<tradition>
    title: <human title>
    description: <paragraph>
    tags: [<tradition>, ...]    # 'oracle' and 'built-in' added at ingest
    entries:
      - title: <chunk label>    # → section_path[0]
        body: |                 # → chunk text
          <pre-rendered markdown>
        original: ...           # → tail line `_original_: ...`
        pinyin: ...
        lang: ...
        source: ...

Run once::

    python scripts/migrate_to_oracle.py

The legacy data files (iching.yaml, wisdom-starter.yaml) can be
deleted after this script's output is committed.
"""

from __future__ import annotations

from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
DATA_DIR = PKG_ROOT / "data"
OUT_DIR = DATA_DIR / "oracle"


# ---------------------------------------------------------------------------
# I-Ching: render three-layer markdown bodies + section paths
# ---------------------------------------------------------------------------


def _render_iching_body(hx: dict) -> str:
    """Render a hexagram's three layers as markdown."""
    heritage = hx.get("iching", {}) or {}
    modern = hx.get("modern", {}) or {}
    cog = hx.get("cognitive", {}) or {}
    parts: list[str] = []

    # Heritage layer.
    parts.append("**Heritage**")
    h_idea = (heritage.get("idea") or "").strip()
    h_text = (heritage.get("text") or "").strip()
    if h_idea:
        parts.append(h_idea)
    if h_text:
        parts.append(f"_{h_text}_")
    parts.append("")

    # Modern (systems) layer.
    m_name = (modern.get("name") or "").strip()
    parts.append(
        f"**Modern (systems): {m_name}**" if m_name else "**Modern (systems)**"
    )
    m_idea = (modern.get("idea") or "").strip()
    m_text = (modern.get("text") or "").strip()
    if m_idea:
        parts.append(m_idea)
    if m_text:
        parts.append(f"_{m_text}_")
    parts.append("")

    # Cognitive layer.
    c_name = (cog.get("name") or "").strip()
    c_type = (cog.get("type") or "").strip()
    if c_name and c_type:
        parts.append(f"**Cognitive lens ({c_type}): {c_name}**")
    elif c_name:
        parts.append(f"**Cognitive lens: {c_name}**")
    else:
        parts.append("**Cognitive lens**")
    c_idea = (cog.get("idea") or "").strip()
    c_text = (cog.get("text") or "").strip()
    if c_idea:
        parts.append(c_idea)
    if c_text:
        parts.append(f"_{c_text}_")

    return "\n".join(parts).strip() + "\n"


def _build_iching_doc():
    # Phase D shipped iching.yaml under src/precis/data/ (importlib
    # resources path); the migration reads from there.
    src = PKG_ROOT / "src" / "precis" / "data" / "iching.yaml"
    with open(src) as f:
        data = yaml.safe_load(f)
    hexagrams = data["unified_64_system"]
    assert len(hexagrams) == 64

    entries = []
    for hx in hexagrams:
        hid = hx["id"]
        hexagram = hx.get("hexagram", {}) or {}
        chinese = hexagram.get("chinese", "")
        h_name = hexagram.get("name", "")
        cog = hx.get("cognitive", {}) or {}
        cog_name = cog.get("name", "")
        cog_type = cog.get("type", "lens")
        title = f"Hexagram {hid:>2} · {chinese} {h_name}".strip()
        body = _render_iching_body(hx)
        entry = {
            "title": title,
            "body": body,
            "extra_section_path": [
                f"Cognitive: {cog_name} ({cog_type})" if cog_name else "—"
            ],
            "original": chinese,
            "lang": "zh",
            "source": "I-Ching, unified three-layer system",
        }
        # Trigram pair as a parenthetical helps grep/embedding pick up
        # structural keywords ("Heaven over Earth").
        trigrams = hx.get("trigrams", {}) or {}
        upper = (trigrams.get("upper") or {}).get("name", "")
        lower = (trigrams.get("lower") or {}).get("name", "")
        if upper and lower:
            entry["trigrams"] = f"{upper} over {lower}"
        binary = hx.get("binary", "")
        if binary:
            entry["binary"] = binary
        entries.append(entry)

    return {
        "slug": "iching",
        "title": "I-Ching",
        "description": (
            "The Book of Changes.  64 archetypes for re-framing "
            "situations, each with three layers (heritage Yi-Jing / "
            "modern systems / cognitive lens).  Use as a re-framing "
            "prompt, not divination."
        ),
        "tags": ["i-ching", "divination"],
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Wisdom-starter → traditions
# ---------------------------------------------------------------------------


_TRADITION_TO_SLUG = {
    "chengyu": "chengyu",
    "proverb": None,                     # Special-cased: split euro / irish
    "stoic": "stoic",
    "engineering": "engineering",
    "talmudic": "talmudic",
    "koan": "zen",
}

_TRADITION_TITLES = {
    "chengyu": "Chengyu",
    "stoic": "Stoic",
    "engineering": "Engineering",
    "talmudic": "Talmudic",
    "zen": "Zen",
    "proverbs-euro": "European Proverbs",
    "proverbs-irish": "Irish Proverbs",
}

_TRADITION_DESCRIPTIONS = {
    "chengyu": (
        "Chinese four-character idioms.  Compact archetypes with deep "
        "embedded narratives — each idiom names a recurring strategic "
        "situation."
    ),
    "stoic": (
        "Stoic and classical aphorisms.  Marcus Aurelius, Epictetus, "
        "and the Latin tradition — pithy frames for control, finitude, "
        "and equanimity."
    ),
    "engineering": (
        "Engineering and decision-theoretic principles.  Knuth, "
        "Chesterton, Hyrum, Gall, Postel, Dunning-Kruger — named "
        "patterns and counter-patterns for software-shaped work."
    ),
    "talmudic": (
        "Talmudic and rabbinic aphorisms.  Pirkei Avot and "
        "post-biblical Jewish wisdom — frames for contribution, "
        "agency, and the bounds of obligation."
    ),
    "zen": (
        "Zen and koan-flavoured aphorisms.  Practice over insight; "
        "map versus territory; the discipline of beginner's mind."
    ),
    "proverbs-euro": (
        "European proverbs.  English and continental folk-wisdom — "
        "compounding, prevention, signal-from-failure, and the "
        "cost of premature optimisation by another name."
    ),
    "proverbs-irish": (
        "Irish proverbs (seanfhocail).  Gaeilge with English "
        "rendering — quality over quantity, fit-to-environment, "
        "strategic retreat."
    ),
}


def _entry_from_starter(item: dict) -> dict:
    """Convert one wisdom-starter entry into the oracle entry schema."""
    body = (item.get("text") or "").strip()
    title = (item.get("title") or "").strip()
    out = {"title": title, "body": body}
    for k in ("original", "pinyin", "source", "lang"):
        v = item.get(k)
        if v:
            out[k] = v
    return out


def _build_wisdom_docs():
    src = DATA_DIR / "wisdom-starter.yaml"
    with open(src) as f:
        data = yaml.safe_load(f)
    entries = data["entries"]

    # Bucket by tradition slug.
    buckets: dict[str, list[dict]] = {}
    for item in entries:
        tradition = (item.get("tradition") or "").strip()
        # Proverbs split by language.
        if tradition == "proverb":
            lang = (item.get("lang") or "en").strip()
            slug = "proverbs-irish" if lang == "ga" else "proverbs-euro"
        else:
            slug = _TRADITION_TO_SLUG.get(tradition)
        if slug is None:
            print(
                f"  warn: unknown tradition {tradition!r} for slug "
                f"{item.get('slug', '?')!r}; skipping"
            )
            continue
        buckets.setdefault(slug, []).append(_entry_from_starter(item))

    docs: list[dict] = []
    for slug, items in buckets.items():
        docs.append({
            "slug": slug,
            "title": _TRADITION_TITLES[slug],
            "description": _TRADITION_DESCRIPTIONS[slug],
            "tags": [slug.split("-")[0]],
            "entries": items,
        })
    return docs


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    iching_doc = _build_iching_doc()
    wisdom_docs = _build_wisdom_docs()
    all_docs = [iching_doc, *wisdom_docs]

    for doc in all_docs:
        out = OUT_DIR / f"{doc['slug']}.yaml"
        with open(out, "w") as f:
            yaml.safe_dump(
                doc, f, sort_keys=False, default_flow_style=False,
                width=78, allow_unicode=True,
            )
        print(f"wrote {out}: {len(doc['entries'])} entries")


if __name__ == "__main__":
    main()
