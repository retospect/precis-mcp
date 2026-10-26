"""Write <drafts>/implies-corrections.yaml: implies {class, mechanism} per join/rotate/close leaf."""

import datetime
import subprocess
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

CLASSES = {
    "rigid",
    "revolute",
    "prismatic",
    "cylindrical",
    "screw",
    "planar",
    "ball",
    "compliant",
    "captive",
    "axial",
}
MECHS = {
    "snap",
    "screw",
    "press",
    "clamp",
    "key",
    "magnet",
    "bearing",
    "bond",
    "integral",
    "cable",
    "tensioner",
}

# leaf -> (class, mechanism or None, note). None class = no implies (not a connect choice).
M = {
    "join/integrate/one-body-print-mould": ("rigid", "integral", None),
    "join/integrate/machine-from-solid": ("rigid", "integral", None),
    "join/integrate/cast-as-one": ("rigid", "integral", None),
    "join/permanent/material-bond/weld": (
        "rigid",
        None,
        "no fusion mechanism token; 'bond' is atomic-scale",
    ),
    "join/permanent/material-bond/braze-solder": (
        "rigid",
        None,
        "no fusion mechanism token",
    ),
    "join/permanent/material-bond/adhesive": (
        "rigid",
        None,
        "no adhesive mechanism token",
    ),
    "join/permanent/material-bond/solvent-weld": (
        "rigid",
        None,
        "no fusion mechanism token",
    ),
    "join/permanent/permanent-mechanical/rivet": (
        "rigid",
        None,
        "no rivet token; 'screw' would demand a threaded fastener",
    ),
    "join/permanent/permanent-mechanical/heat-stake": (
        "rigid",
        None,
        "formed post, no token fits",
    ),
    "join/permanent/permanent-mechanical/clinch": (
        "rigid",
        None,
        "formed interlock, no token fits",
    ),
    "join/permanent/permanent-mechanical/self-clinching-fastener": (
        "rigid",
        "screw",
        "the leaf supplies the thread a screw clamps into",
    ),
    "join/permanent/permanent-mechanical/rivet-nut": (
        "rigid",
        "screw",
        "the leaf supplies the thread a screw clamps into",
    ),
    "join/permanent/permanent-mechanical/tube-swage-crimp": (
        "rigid",
        None,
        "plastic deformation, not a toleranced interference; 'press' would demand an interference relation",
    ),
    "join/permanent/permanent-mechanical/press-shrink-fit": ("rigid", "press", None),
    "join/permanent/permanent-mechanical/permanent-snap": ("rigid", "snap", None),
    "join/service/threaded/clearance-tapped": ("rigid", "screw", None),
    "join/service/threaded/through-bolt-nut": ("rigid", "screw", None),
    "join/service/threaded/heat-set-insert": ("rigid", "screw", None),
    "join/service/threaded/nut-trap": ("rigid", "screw", None),
    "join/service/threaded/thread-forming": ("rigid", "screw", None),
    "join/service/threaded/shoulder-screw": (
        "revolute",
        "screw",
        "the shoulder is the pivot; the screw is bought",
    ),
    "join/service/pinned-retained/pin-and-clip": (
        "revolute",
        None,
        "pivot or link; no pin token",
    ),
    "join/service/pinned-retained/retaining-ring": (
        "rigid",
        None,
        "axial retention; the rotation, if any, comes from a rotate leaf",
    ),
    "join/service/clamp/split-boss-pinch": ("rigid", "clamp", None),
    "join/service/releasable-form-lock/releasable-snap": ("rigid", "snap", None),
    "join/service/releasable-form-lock/dovetail-slide": (
        "prismatic",
        None,
        "form-locked slide; no token fits",
    ),
    "join/service/releasable-form-lock/twist-lock": (
        "rigid",
        None,
        "bayonet-type form lock; no token fits",
    ),
    "locate/dowel-pin-and-slot": (
        "rigid",
        None,
        "location only; a separate leaf holds",
    ),
    "locate/nest-pocket-step": ("rigid", None, "location only; a separate leaf holds"),
    "locate/second-fastener": ("rigid", "screw", None),
    "locate/key-flat-spline": ("rigid", "key", None),
    "rotate/plain-bearing/pin-in-hole": (
        "revolute",
        None,
        "no bought part, so not 'bearing' (which demands a BOM bearing)",
    ),
    "rotate/plain-bearing/polymer-bushing": ("revolute", "bearing", None),
    "rotate/plain-bearing/sintered-bronze-bushing": ("revolute", "bearing", None),
    "rotate/rolling-bearing/deep-groove-ball": ("revolute", "bearing", None),
    "rotate/rolling-bearing/bearing-in-printed-part": ("revolute", "bearing", None),
    "rotate/rolling-bearing/paired-preloaded": ("revolute", "bearing", None),
    "rotate/seat-and-retain/housing-and-shaft-fits": (
        None,
        None,
        "follow-on decision after a bearing leaf, reached by link; would crowd the 3-slot revolute+bearing tier",
    ),
    "rotate/seat-and-retain/locating-floating-arrangement": (
        None,
        None,
        "follow-on decision after a bearing leaf, reached by link",
    ),
    "rotate/seat-and-retain/axial-retention": (
        None,
        None,
        "follow-on decision after a bearing leaf, reached by link",
    ),
    "rotate/flexure-pivot/cross-spring-and-notch": (
        "compliant",
        "integral",
        "notch hinge is one body; a cross-spring with separate leaves would carry no mechanism",
    ),
    "rotate/spherical/rod-end-ball-joint": (
        "ball",
        "bearing",
        "rod ends and spherical plain bearings are bought",
    ),
    "rotate/shaft-hub/set-screw-on-flat": ("rigid", "screw", None),
    "rotate/shaft-hub/key-and-keyway": ("rigid", "key", None),
    "rotate/shaft-hub/spline-and-polygon": (
        "rigid",
        "key",
        "form-locked torque transfer",
    ),
    "rotate/shaft-hub/clamp-hub": ("rigid", "clamp", None),
    "rotate/shaft-hub/cross-pin": (
        "rigid",
        "key",
        "a transverse pin is a form-locked torque transfer; no pin token",
    ),
    "rotate/shaft-basics/shaft-shoulders-fillets": (
        None,
        None,
        "shaft geometry guidance, not a connection",
    ),
    "rotate/lever-crank-arm/offset-arm": (
        None,
        None,
        "arm geometry guidance; the arm-to-shaft connection is a shaft-hub leaf",
    ),
    "close/snap-and-friction/cantilever-snap-lid": ("rigid", "snap", None),
    "close/snap-and-friction/annular-snap-cap": ("rigid", "snap", None),
    "close/snap-and-friction/friction-press-lid": ("rigid", "press", None),
    "close/threaded-and-twist/screw-cap": (
        "rigid",
        None,
        "moulded thread, no bought fastener, so not 'screw' (which demands one)",
    ),
    "close/threaded-and-twist/bayonet": ("rigid", None, "form lock; no token fits"),
    "close/threaded-and-twist/quarter-turn-fastener": (
        "rigid",
        "screw",
        "bought quarter-turn or captive panel screw; the BOM demand fits",
    ),
    "close/hinged/pin-hinge": ("revolute", None, "no pin token"),
    "close/hinged/living-hinge": ("compliant", "integral", None),
    "close/hinged/stay-detent-stop": (
        None,
        None,
        "accessory to a hinge leaf, not a connection",
    ),
    "close/latches/draw-latch": (
        "rigid",
        "clamp",
        "over-centre clamp: releasable normal force",
    ),
    "close/latches/magnetic-catch": ("rigid", "magnet", None),
    "close/latches/push-push-and-slide": ("prismatic", None, "no token fits"),
    "close/seal-interface/o-ring-face-seal": (
        None,
        None,
        "seal interface on a closure, not a connection",
    ),
    "close/seal-interface/gasket-and-lip": (
        None,
        None,
        "seal interface on a closure, not a connection",
    ),
}

# Within-tier order for tiers with more than 3 leaves: most general default first.
RANK = {
    "join/service/threaded/clearance-tapped": 1,
    "join/service/threaded/through-bolt-nut": 2,
    "join/service/threaded/heat-set-insert": 3,
    "join/service/threaded/nut-trap": 4,
    "join/service/threaded/thread-forming": 5,
    "locate/second-fastener": 6,
    "rotate/shaft-hub/set-screw-on-flat": 7,
    "close/threaded-and-twist/quarter-turn-fastener": 8,
    "join/permanent/permanent-mechanical/rivet-nut": 9,
    "join/permanent/permanent-mechanical/self-clinching-fastener": 10,
    "join/service/releasable-form-lock/releasable-snap": 1,
    "close/snap-and-friction/cantilever-snap-lid": 2,
    "close/snap-and-friction/annular-snap-cap": 3,
    "join/permanent/permanent-mechanical/permanent-snap": 4,
    "rotate/shaft-hub/key-and-keyway": 1,
    "locate/key-flat-spline": 2,
    "rotate/shaft-hub/spline-and-polygon": 3,
    "rotate/shaft-hub/cross-pin": 4,
    "rotate/rolling-bearing/deep-groove-ball": 1,
    "rotate/rolling-bearing/bearing-in-printed-part": 2,
    "rotate/plain-bearing/polymer-bushing": 3,
    "rotate/plain-bearing/sintered-bronze-bushing": 4,
    "rotate/rolling-bearing/paired-preloaded": 5,
}

leaves = []
with open(DRAFTS + "/s2/leaves.txt", encoding="utf-8") as fh:
    for line in fh:
        leaves.append(line.split(" | ")[0].strip())
missing = [x for x in leaves if x not in M]
extra = [x for x in M if x not in leaves]
assert not missing and not extra, (missing, extra)
for k, (c, m, _) in M.items():
    assert c is None or c in CLASSES, k
    assert m is None or m in MECHS, k

sha = subprocess.run(
    ["git", "rev-parse", "--short", "main"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=True,
    cwd=REPO,
).stdout.strip()
recs, none = [], []
for k, (c, m, note) in M.items():
    if c is None:
        none.append({"leaf": k, "why": note})
        continue
    imp = (
        {"class": c}
        | ({"mechanism": m} if m else {})
        | ({"rank": RANK[k]} if k in RANK else {})
    )
    recs.append({"leaf": k, "implies": imp} | ({"note": note} if note else {}))

tiers = {}
for r in recs:
    key = (r["implies"]["class"], r["implies"].get("mechanism"))
    tiers.setdefault(key, []).append(r["leaf"])
crowded = {
    f"{c}+{m}": sorted(v, key=lambda x: RANK.get(x, 99))
    for (c, m), v in tiers.items()
    if m and len(v) > 3
}
for v in crowded.values():
    assert all(x in RANK for x in v), v

out = {
    "meta": {
        "generated": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base": f"src/precis/data/design-tree at main {sha}",
        "by": "DESIGN, hand-mapped against src/precis_se/joints.py KINEMATIC_CLASSES / MECHANISMS",
        "apply": "Set node field implies: {class, mechanism?} on each listed leaf (join/rotate/close manifests). Leaves under no_implies get none. Process-topic leaves in processes.yaml are not connect realisations and get none.",
        "counts": {
            "with_implies": len(recs),
            "with_mechanism": sum(1 for r in recs if "mechanism" in r["implies"]),
            "no_implies": len(none),
        },
        "s4_notes": [
            "dtree_hints shows 3 leaves: class+mechanism matches first, then class-only, alphabetical within a tier. Four tiers hold more than 3 leaves (crowded_tiers); alphabetically, rigid+screw shows quarter-turn-fastener, rivet-nut and self-clinching-fastener and never clearance-tapped. Fix: implies carries an optional integer rank (lower = more general default) on every leaf in a crowded tier; S4 should sort inside a tier by (rank, id), rank missing = 99. One-line change in _candidate_leaves; crowded_tiers below is already in rank order.",
            "The class-only fallback for 'rigid' returns 3 alphabetically-first rigid leaves of 38 (close/... snap lids for a weld). Recommend S4 drops the class-only tier when the class has more than ~5 candidates, or when the connect has a mechanism that matched nothing.",
            "Vocabulary gaps (leaves left mechanism-less): fusion (weld, braze-solder, solvent-weld), adhesive, rivet, pin (pin-and-clip, pin-hinge, cross-pin mapped to key), formed (heat-stake, clinch, crimp), form-lock (bayonet, twist-lock, dovetail). Adding 'fuse', 'adhesive', 'rivet', 'pin' to MECHANISMS would let 9 more leaves match on mechanism; that is a joints.py change, not a manifest one.",
        ],
    },
    "implies": recs,
    "no_implies": none,
    "crowded_tiers": crowded,
}
with open(DRAFTS + "/implies-corrections.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(out, fh, sort_keys=False, allow_unicode=True, width=140)
print(out["meta"]["counts"])
for k, v in crowded.items():
    print(k, len(v), v[:3])
print("rigid total", sum(1 for r in recs if r["implies"]["class"] == "rigid"))
