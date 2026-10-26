"""Merge run 3 verified batches into <drafts>/processes.yaml (SPEC 2.2 field names).

Usage: uv run --with pyyaml python3 <drafts>/work3/build3.py
Writes processes.yaml and work3/build3.json (dropped, contradicted, red list inputs).
"""

import collections
import json
import pathlib
import re
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

sys.path.insert(0, DRAFTS + "/work3")
from overrides3 import (  # noqa: E402  (node_key, claim_id) -> dict(drop|text|value|unit|conf|url|note|eq|corrected)
    CONFLICT_DROP,
    CONFLICT_RULES,
    EXTRA_CONFLICTS,
    NODE_OV,
    OV,
)

RUN = pathlib.Path(DRAFTS + "/run3")
OUT = pathlib.Path(DRAFTS)
BATCHES = "LMNOPQRSTUV"
EQ = {
    "desktop-fdm",
    "industrial-fdm",
    "sla",
    "cnc-mill",
    "lathe",
    "injection-mould",
    "sheet",
    "cast",
    "bought",
    "any",
}
EQ_FIX = {"all": "any", "other": "any", "powder-bed": "industrial-fdm"}
CF = {"standard", "handbook", "secondary", "excerpt-only", "shop-rule", "unverified"}
STD_RE = re.compile(
    r"(ISO\s?\d[\d\-]*|DIN\s?(?:EN\s)?\d[\d\-]*|EN\s?\d{3,5}[\d\-]*|ASTM\s?[A-Z]\d+|SAE\s?AMS\s?\d+[\w/]*|AMS\s?\d+[\w/]*|"
    r"MIL-(?:PRF|A|STD)-\d+[A-Z]?|VDI\s?\d+|NADCA|FEFCO|ANSI\s?[A-Z]?\d[\d\.]*)"
)
BAD_VALUES = {
    "n/a",
    "null",
    "rule",
    "qualitative",
    "",
    "formula",
    "mixed",
    "none",
    "various",
}
PROC_FM = {
    "anisotropy",
    "residual-stress",
    "porosity",
    "sink",
    "warpage",
    "notch",
    "burr",
    "dimensional-error",
}


def load():
    nodes = {}
    for b in list(BATCHES) + ["T2"]:
        p = RUN / "verified" / f"{b}.yaml"
        if not p.exists():
            print("MISSING", p)
            continue
        for n in yaml.safe_load(p.read_text(encoding="utf-8")):
            if not isinstance(n, dict) or "claims" not in n:
                continue
            nid = n["id"]
            if b == "T" and nid in (
                "process/coating/anodising",
                "process/property-change/heat-treatment",
            ):
                continue  # superseded by T2
            n["_b"] = b
            nodes[nid] = n
    return nodes


def sval(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in BAD_VALUES else s


def base_conf(c):
    q = c.get("source_quality") or "weak"
    if q == "primary":
        return "standard" if c.get("source_kind") == "standard" else "handbook"
    if q == "weak":
        return "shop-rule"
    return "secondary"


DROPPED, CONTRA, OFFNODE, UNKNOWN_KEYS = [], [], [], collections.Counter()
KNOWN = {
    "id",
    "text",
    "value",
    "unit",
    "source_n",
    "also_n",
    "source_url",
    "source_kind",
    "equipment",
    "verdict",
    "quote",
    "source_quality",
    "alt_url",
    "note",
    "machine_specific",
    "measured_on",
    "off_node",
    "remapped",
    "added_in_stage2",
    "standard",
    "alt_source_quality",
    "source_url_draft",
    "value_corrected",
    "confidence",
    "suggested_text",
    "note_s2",
    "remapped_to",
    "source_url_corrected",
    "partial",
    "approximate",
    "correct_value",
    "remapped_from",
    "text_draft",
    "original_source_url",
    "original_source_n",
    "equipment_draft",
    "equipment_basis",
    "text_fixed",
    "value_fixed",
    "unit_fixed",
    "corrected",
    "corrected_value",
}


def build_rule(nid, c):
    for k in c:
        if k not in KNOWN:
            UNKNOWN_KEYS[k] += 1
    o = OV.get((nid, c["id"]), {})
    verdict = str(c.get("verdict") or "").strip()
    if o.get("drop"):
        DROPPED.append((nid, c["id"], o["drop"]))
        return None
    if verdict == "dropped":
        DROPPED.append((nid, c["id"], c.get("note") or "dropped by verifier"))
        return None
    if c.get("off_node") and not o.get("keep"):
        OFFNODE.append((nid, c["id"], c.get("equipment"), c.get("text")))
        return None
    fixed_value = (
        c.get("value_fixed") or c.get("corrected_value") or c.get("value_corrected")
    )
    if verdict == "contradicted" and not (fixed_value or c.get("text_fixed") or o):
        CONTRA.append(
            (nid, c["id"], c.get("text"), c.get("value"), c.get("quote"), c.get("note"))
        )
        return None
    d = {"id": c["id"]}
    suggested = c.get("suggested_text") if verdict != "contradicted" else None
    d["text"] = str(
        o.get("text") or c.get("text_fixed") or suggested or c["text"]
    ).strip()
    d["value"] = (
        sval(o["value"])
        if "value" in o
        else sval(
            fixed_value if verdict == "contradicted" and fixed_value else c.get("value")
        )
    )
    if suggested and "text" not in o and "value" not in o and d["value"]:
        nums = re.findall(r"\d+(?:\.\d+)?", d["value"])
        if not all(x in d["text"] for x in nums):
            d["value"] = (
                None  # verifier rewrote the text; the old value no longer matches it
            )
    unit = o["unit"] if "unit" in o else (c.get("unit_fixed") or c.get("unit"))
    if isinstance(unit, str) and unit.strip().lower() in (
        "n/a",
        "",
        "null",
        "rule",
        "formula",
        "mixed",
    ):
        unit = None
    d["unit"] = unit
    url = (
        o.get("url")
        or c.get("source_url_corrected")
        or c.get("remapped_to")
        or (
            c.get("alt_url")
            if verdict == "confirmed-alt" and c.get("alt_url")
            else c.get("source_url")
        )
    )
    d["source_url"] = None if url in ("", "None", "null") else url
    d["source_quality"] = o.get("quality") or c.get("source_quality") or "none"
    eq = o.get("eq") or c.get("equipment") or "any"
    eq = EQ_FIX.get(eq, eq)
    if eq not in EQ:
        raise SystemExit(f"bad equipment {eq} in {nid} {c['id']}")
    d["equipment"] = eq
    if "conf" in o:
        conf = o["conf"]
    elif c.get("confidence") in CF and verdict in ("confirmed", "confirmed-alt"):
        conf = c["confidence"]
    elif verdict == "unreachable":
        conf = "unverified"
    elif verdict == "excerpt-only":
        conf = "excerpt-only"
    elif verdict == "unsupported":
        conf = "shop-rule"
    else:
        conf = base_conf(c)
    d["confidence"] = conf
    std = c.get("standard")
    if not std and conf in ("secondary", "excerpt-only", "handbook"):
        m = STD_RE.search(d["text"])
        std = m.group(1).strip() if m else None
    if std:
        d["standard"] = std
    if c.get("machine_specific"):
        d["machine_specific"] = True
        d["table_ref"] = "se_capabilities.json (S1 decides the row)"
        if c.get("measured_on"):
            d["measured_on"] = c["measured_on"]
    if verdict == "contradicted" or o.get("corrected") or c.get("corrected"):
        d["corrected"] = True
    note = o.get("note")
    if not note and conf == "excerpt-only":
        note = "confirmed only from a search excerpt, page not read"
    if not note and conf == "unverified":
        note = "source could not be read; value kept for review"
    if not note and conf == "shop-rule":
        note = (
            "no primary source found or source weak; guidance only, not DRC-enforceable"
        )
    if note:
        d["note"] = note
    return d


def issues(n):
    out = []
    for i in n.get("issues") or []:
        fm = i.get("failure_mode")
        out.append({"issue": i.get("issue"), "fix": i.get("fix"), "failure_mode": fm})
    return out


def sort_key(r):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r["id"])]


def node_out(nid, role, coverage, n, equipment):
    o = {
        "id": nid,
        "role": role,
        "domain": "mech",
        "scale": "macro",
        "coverage": coverage,
    }
    if equipment:
        o["equipment"] = equipment
    n = {**n, **NODE_OV.get(nid, {})}
    o["summary"] = n.get("summary")
    for f in ("when", "avoid_when", "variants", "tradeoffs", "howto"):
        if n.get(f):
            o[f] = n[f]
    o["issues"] = issues(n)
    confl = []
    for cfl in list(n.get("conflicts") or []) + EXTRA_CONFLICTS.get(nid, []):
        key = (nid, cfl["topic"])
        if key in CONFLICT_DROP:
            continue
        if key not in CONFLICT_RULES:
            raise SystemExit(f"conflict without rules mapping: {key}")
        confl.append({**cfl, "rules": CONFLICT_RULES[key]})
    rules = [r for r in (build_rule(nid, c) for c in n["claims"]) if r]
    o["rules"] = sorted(rules, key=sort_key)
    have = {r["id"] for r in o["rules"]}
    for cfl in confl:
        missing = [x for x in cfl["rules"] if x not in have]
        if missing:
            raise SystemExit(
                f"conflict {nid} {cfl['topic']} names unknown rules {missing}"
            )
    if confl:
        o["conflicts"] = confl
    urls = []
    for r in o["rules"]:
        if r.get("source_url") and r["source_url"] not in urls:
            urls.append(r["source_url"])
    o["sources"] = [n.get("pplx")] + urls
    return o


def main():
    nodes = load()
    skel = yaml.safe_load((RUN / "skeleton.yaml").read_text(encoding="utf-8"))[
        "process"
    ]
    root = {
        "id": "process",
        "role": "goal",
        "domain": "mech",
        "scale": "macro",
        "coverage": "shallow",
        "def": skel["def"],
        "asks": skel["asks"],
        "children": {},
    }
    missing = []
    for mname, m in skel["children"].items():
        mid = f"process/{mname}"
        mo = {"id": mid, "role": "method", "def": m["def"], "children": {}}
        for pname, p in m["children"].items():
            pid = f"{mid}/{pname}"
            if "topics" in p:
                po = {
                    "id": pid,
                    "role": "process",
                    "domain": "mech",
                    "scale": "macro",
                    "coverage": "deep",
                    "equipment": p["equipment"],
                    "def": p["def"],
                    "children": {},
                }
                for tname in p["topics"]:
                    tid = f"{pid}/{tname}"
                    if tid not in nodes:
                        missing.append(tid)
                        continue
                    po["children"][tname] = node_out(
                        tid, "leaf", "deep", nodes[tid], None
                    )
            else:
                if pid not in nodes:
                    missing.append(pid)
                    continue
                po = node_out(pid, "process", "shallow", nodes[pid], p["equipment"])
                po["def"] = p["ctx"].split(":")[0]
            mo["children"][pname] = po
        root["children"][mname] = mo

    ai_use = (
        "Pre-filled with Perplexity (perplexity-reasoning) to find sources, then each rule was checked against its cited source "
        "by a separate agent. Prose and rule texts are our own wording; values trace to the listed source_url. Confidence levels: "
        "standard, handbook, secondary, excerpt-only, shop-rule (not DRC-enforceable), unverified. Rules marked machine_specific "
        "hold a number that depends on the machine, tool or material; S1 moves them to se_capabilities.json rows via table_ref."
    )
    doc = {
        "design_tree_manifest": "run3",
        "status": "run 3 (S6), verified drafts merged; for review, not yet linked or deduped",
        "ai_use": ai_use,
        "process": root,
    }

    class D(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    (OUT / "processes.yaml").write_text(
        yaml.dump(
            doc,
            Dumper=D,
            sort_keys=False,
            allow_unicode=True,
            width=120,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    cf = collections.Counter()
    ms = 0
    nn = 0

    def walk(x):
        nonlocal ms, nn
        if isinstance(x, dict):
            if "rules" in x:
                nn += 1
                for r in x["rules"]:
                    cf[r["confidence"]] += 1
                    ms += bool(r.get("machine_specific"))
                    assert r["confidence"] in CF and r["equipment"] in EQ, r
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(root)
    json.dump(
        {
            "dropped": DROPPED,
            "contradicted": CONTRA,
            "off_node": OFFNODE,
            "missing": missing,
        },
        open(DRAFTS + "/work3/build3.json", "w", encoding="utf-8"),  # noqa: SIM115
        indent=1,
    )
    print("nodes", nn, "rules", sum(cf.values()), dict(cf), "machine_specific", ms)
    print(
        "dropped",
        len(DROPPED),
        "contradicted-dropped",
        len(CONTRA),
        "off-node",
        len(OFFNODE),
        "missing",
        missing,
    )
    if UNKNOWN_KEYS:
        print("unknown claim keys", dict(UNKNOWN_KEYS))


main()
