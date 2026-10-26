"""Merge S2 drafter outputs + verifier verdicts + DESIGN overrides into <drafts>/S2-corrections.yaml."""

import collections
import datetime
import os
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

S = DRAFTS + "/s2/"
STRAT_FDM = "https://www.stratasys.com/siteassets/sdm/content---website-storage/design-guides/dg_sdm_fdm_0725a.pdf?v=490918"
PRUSA_MOD = "https://help.prusa3d.com/article/modeling-with-3d-printing-in-mind_164135"
TABLE_ROW = "More than one perimeter, but less than two perimeters [table: Is it printable? -> cross icon]"
P = "processes.yaml#process/additive/desktop-fdm/"

# DESIGN overrides, applied after the verifier. Key: ref (or ref/i for a split sub-record).
OV = {
    P + "walls-and-min-features#c25": dict(
        action="revalue",
        equipment="industrial-fdm",
        value="24",
        unit="pt",
        text="Industrial FDM embossed or engraved text: bold font, 24 pt or larger",
        source_url=STRAT_FDM,
        quote="bold font with 24+ point styling",
        source_quality="primary",
        confidence="handbook",
        min=24,
        max=None,
        note="Old 16 pt / 10 pt figures came from a dead Stratasys Direct PDF (404, no archive); the current Stratasys guide says 24+ pt bold.",
        verified="corrected-by-design",
    ),
    P + "walls-and-min-features#c26": dict(
        action="drop",
        note="Duplicate of c25 after the 24 pt correction; its 10 pt figure is not in the current Stratasys guide and the old PDF is gone.",
        verified="corrected-by-design",
    ),
    P + "dimensional-accuracy#c7": dict(
        quote=TABLE_ROW,
        note="Prusa printability table: below one perimeter cross, one perimeter tick, between one and two cross, two or more tick. Icons are images; read from page HTML (xmark.png / checkmark.png) by DESIGN.",
        verified="confirmed-by-design",
    ),
    P + "material-selection#c3": dict(
        text="On a 0.4 mm nozzle a wall prints as exactly one perimeter (0.45 mm) or two or more (0.9 mm and up); below one perimeter, or between one and two, it does not print cleanly",
        value="0.45 or >=0.9",
        unit="mm",
        min=0.9,
        max=None,
        quote=TABLE_ROW,
        note="Rewritten to Prusa's printability table; the drafted 'under 2 extrusion widths is unreliable' wording is not on the page.",
        verified="corrected-by-design",
    ),
    P + "overhangs-bridges-supports#c9": dict(
        source_quality="secondary",
        confidence="shop-rule",
        promote=False,
        note="Protolabs service-bureau FDM column, not a desktop vendor figure; Prusa table gives 0.45 / 0.9 mm walls. Kept as guidance only.",
        verified="scope-mismatch-accepted",
    ),
    P + "threads-and-inserts#c3": dict(
        source_quality="secondary",
        confidence="shop-rule",
        promote=False,
        note="Protolabs service-bureau FDM column; no desktop vendor figure exists. Guidance only.",
        verified="scope-mismatch-accepted",
    ),
}

POWDER = "powder-bed"

# DESIGN overrides on CAP records, keyed by idx (filled after verify-cap).
AXIS = (
    "Axis resolved by DESIGN: from vertical, the se field's convention. 75 deg from horizontal would be a near-vertical "
    "wall any printer manages, so Prusa's 'up to 75 deg without supports' only means something from vertical. "
    "PrusaSlicer's Overhang threshold setting is the complement (from horizontal)."
)
CAP_OV = {3: {"note": AXIS}, 4: {"note": AXIS}, 5: {"note": AXIS}}
for i in (111, 113, 114, 116, 118, 119, 122, 124, 125, 126):
    CAP_OV[i] = {"mode": "fdm/pa-cf", "verified": "corrected-by-design"}
for i in (132, 144, 152, 153):
    CAP_OV[i] = {"mode": "fdm/pc-cf", "verified": "corrected-by-design"}
NO_PROMOTE = {
    38: "ratio computed from Polymaker Z/XY pairs, not stated by the source",
    58: "ratio computed from Polymaker Z/XY pairs, not stated by the source",
    74: "ratio computed from Polymaker Z/XY pairs, not stated by the source",
    93: "ratio computed from Polymaker Z/XY pairs, not stated by the source",
    151: "ratio computed from vendor Z/XY pairs, not stated by the source",
    39: "CNC Kitchen hook test, not a standard tensile specimen",
    59: "CNC Kitchen hook test, not a standard tensile specimen",
    94: "CNC Kitchen hook test, not a standard tensile specimen",
    12: "single worked M6 example on the Bambu wiki, no material named",
    15: "printer accuracy (0.2 mm), not a clearance recommendation",
    11: "soft 'challenging' limit, not a floor",
}
for i, why in NO_PROMOTE.items():
    CAP_OV.setdefault(i, {}).update({"promote": False, "note_design": why})
HDT_18 = {33, 53, 71, 88, 106}


def load(name):
    p = f"{S}out/{name}.yaml"
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


ver = {v["id"]: v for v in load("verify") or []}
with open(f"{S}in-M.yaml", encoding="utf-8") as fh:
    M = yaml.safe_load(fh)

changes, kept = [], []
counts = collections.Counter()
FIELDS = (
    "action",
    "equipment",
    "material",
    "text",
    "value",
    "unit",
    "min",
    "max",
    "source_url",
    "quote",
    "source_quality",
    "confidence",
    "verified",
    "promote",
    "note",
)


def clean(r):
    return (
        {k: r[k] for k in FIELDS if k in r and r[k] not in (None, "")}
        | ({"min": r["min"]} if r.get("min") is not None else {})
        | ({"max": r["max"]} if r.get("max") is not None else {})
    )


def finish(ref, r):
    """Attach verify verdict, apply override, decide promote."""
    v = ver.get(ref)
    if v:
        r["verified"] = v["verdict"]
        if v["verdict"] == "wrong-number" and v.get("correct_value") and ref not in OV:
            r["note"] = (
                r.get("note") or ""
            ) + f" | VERIFIER: page says {v['correct_value']}"
        if v.get("note") and "not on the page" in v["note"]:
            r["confidence"] = "shop-rule"
            r["note"] = (r.get("note") or "") + " | VERIFIER: " + v["note"]
    elif r.get("quote"):
        r["verified"] = "unverified"
    else:
        r["verified"] = "n/a (tag change, source unchanged)"
    r.update(OV.get(ref, {}))
    if "promote" not in r:
        r["promote"] = (
            r.get("equipment") == "desktop-fdm"
            and r.get("source_quality") == "primary"
            and r.get("confidence") == "handbook"
            and r["verified"]
            in ("confirmed", "confirmed-by-design", "corrected-by-design")
            and r.get("action") != "drop"
        )
    return r


for name in ("A1", "A2", "B"):
    for r in load(name):
        ref = r["ref"]
        if r.get("action") == "keep":
            kept.append(ref)
            counts["keep"] += 1
            continue
        counts[r["action"]] += 1
        rec = {"ref": ref, "slice": name} | clean(r)
        if r.get("split_into"):
            rec["split_into"] = [
                finish(f"{ref}/{i}", clean(s)) for i, s in enumerate(r["split_into"])
            ]
            rec.pop("verified", None)
        else:
            rec = {"ref": ref, "slice": name} | finish(ref, clean(r))
        changes.append(rec)

for r in M:
    if r["node"] == "process/additive/powder-bed":
        counts["retag"] += 1
        changes.append(
            {
                "ref": r["ref"],
                "slice": "M",
                "action": "retag",
                "equipment": POWDER,
                "verified": "n/a (tag change, source unchanged)",
                "promote": False,
                "note": "SLS/MJF rule; S6 merge mapped powder-bed to industrial-fdm for want of a vocabulary value. Needs 'powder-bed' added to the equipment vocabulary.",
            }
        )
    else:
        counts["keep"] += 1
        kept.append(r["ref"])

counts = collections.Counter(r["action"] for r in changes) + collections.Counter(
    keep=len(kept)
)
cap = load("CAP")
cap_ver = {v["idx"]: v for v in load("verify-cap") or []}
if cap:
    for c in cap:
        v = cap_ver.get(c["idx"])
        c["verified"] = (
            v["verdict"]
            if v
            else (
                "n/a (no figure)"
                if c.get("min") is None and c.get("max") is None
                else "unverified"
            )
        )
        if v and v.get("correct_min") is not None:
            c["note"] = (
                c.get("note") or ""
            ) + f" | VERIFIER: page says {v.get('correct_min')}-{v.get('correct_max')}"
        c.update(CAP_OV.get(c["idx"], {}))
        if c["field"] == "hdt":
            c["field"] = "hdt_1.8mpa" if c["idx"] in HDT_18 else "hdt_0.45mpa"
        c["promote"] = c.get(
            "promote",
            c["verified"] in ("confirmed", "corrected-by-design")
            and c.get("source_quality") == "primary",
        )

out = {
    "meta": {
        "generated": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base": "src/precis/data/design-tree at 03589268e (S1)",
        "by": "DESIGN S2 pass: Sonnet drafters A1/A2 (Stratasys-cited desktop-FDM rules), B (join.yaml split), CAP (per-material capabilities); separate Sonnet verifier; DESIGN overrides",
        "ref_format": "<file>#<node id>#<rule id>; split sub-records replace the one rule with one rule per record",
        "apply": [
            "action keep: already-checked, listed under kept_refs only.",
            "action retag: change equipment only.",
            "action recite / revalue: replace the listed fields (text/value/unit/source_url/source_quality/confidence); add quote as evidence if the schema has a place for it, else drop it.",
            "action split: replace the rule with the split_into records; new rule ids are the builder's (suggest <rid>a, <rid>b).",
            "action drop: remove the rule and any conflict-record reference to it.",
            "action no-source: keep the rule, set confidence shop-rule unless already lower.",
            "promote: true marks the only figures vetted enough for se_capabilities rows (desktop-fdm, primary vendor source, handbook, verifier-confirmed). Everything else stays a rule, not a capability.",
            "Vocabulary: add equipment value 'powder-bed' (SLS + MJF) before applying the slice-M retags.",
            "capabilities: one record per (mode, field, source[, machine]); several vendors per field are kept side by side - the builder takes min of mins / max of maxes or picks one vendor, and says which in the row's source. promote: true = verifier-confirmed primary vendor figure.",
            "capabilities modes: fdm/* is process-level (all materials); fdm/pa-cf and fdm/pc-cf are new (carbon-filled, distinct from fdm/pa, fdm/pc). max_build records carry machine:.",
            "capabilities fields beyond se_capabilities today: nozzle_temp, bed_temp, drying, enclosure, shrinkage, hdt_0.45mpa, hdt_1.8mpa, tensile_xy, tensile_z (MPa). HDT is split by load because vendors mix 0.45 and 1.8 MPa.",
            "max_overhang is from vertical (se convention); PrusaSlicer's overhang threshold is the complement.",
            "Records with min/max null are honest gaps (no published figure); never fill them from a sibling material.",
        ],
        "counts": dict(counts),
        "verifier": dict(collections.Counter(v["verdict"] for v in ver.values())),
    },
    "rules": changes,
    "kept_refs": kept,
}
if cap:
    out["capabilities"] = cap
with open(DRAFTS + "/S2-corrections.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(out, fh, sort_keys=False, allow_unicode=True, width=140)
print(out["meta"]["counts"], out["meta"]["verifier"])
print(
    "rules",
    len(changes),
    "kept",
    len(kept),
    "promote",
    sum(1 for r in changes if r.get("promote"))
    + sum(1 for r in changes for s in r.get("split_into", []) if s.get("promote")),
    "cap",
    len(cap or []),
    "cap promote",
    sum(1 for c in cap or [] if c["promote"]),
)
print("unapplied OV", [k for k in OV if not any(r["ref"] == k for r in changes)])
