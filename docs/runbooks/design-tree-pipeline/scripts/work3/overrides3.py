"""Historical design-tree pipeline script (gr516958); kept as provenance, inputs no longer exist."""

from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])
"""Hand overrides for run 3 merge: (node id, claim id) -> dict.

Keys: drop (reason), keep (keep an off_node claim), text, value, unit, conf, url, quality, eq, note, corrected.
"""

IM = "process/primary-shaping/injection-mould/"
PL_GUIDE = "https://www.protolabs.com/services/injection-molding/design-guidelines/"

MILL = "process/cutting/cnc-mill/"

FDM = "process/additive/desktop-fdm/"

AN = "process/coating/anodising"
HT = "process/property-change/heat-treatment"

LA = "process/cutting/lathe/"
ISO2768M = (
    "ISO 2768-1 class m linear: +-0.1 (0.5-3 and 3-6 mm), +-0.2 (6-30), +-0.3 (30-120), +-0.5 (120-400), "
    "+-0.8 (400-1000)"
)

NODE_ANN = "process/property-change/annealing-printed-parts"
ANN = NODE_ANN  # lint: the original used ANN before its later definition
TEX = (
    "Textured faces: add about 1.5 degrees of draft per 0.025 mm (0.001 in) of texture depth on top of base draft; "
    "light texture at least 3 degrees"
)

TF = "process/forming/thermoforming"
UC = "process/primary-shaping/urethane-casting"
SPAM = "only source was an unrelated non-engineering site; no primary found"

WJ = "process/cutting/waterjet"
EX = "process/forming/extrusion-profile"
FG = "process/forming/forging"

OV = {
    # --- batch R
    (WJ, "c3"): {
        "text": "Abrasive waterjet minimum hole: about 1 mm (maker); 1.5 mm is a conservative shop value with a 1 mm "
        "kerf",
        "value": "1 (1.5 conservative)",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
    },
    (WJ, "c7"): {
        "text": "Waterjet minimum tab: the greater of 1.6 mm (0.063 in) or 1 x material thickness",
        "value": "max(1.6 mm, 1 x T)",
        "unit": None,
        "conf": "secondary",
        "corrected": True,
    },
    (WJ, "c17"): {
        "text": "Waterjet thickness: most shops cut up to 100-150 mm; 200-300 mm possible at the slowest setting",
        "value": "100-150 (200-300 slow)",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
    },
    ("process/cutting/grinding", "c3"): {
        "text": "Choose grinding when Ra finer than about 0.4 um is required; finish milling "
        "reaches about Ra 0.8-1.6 um",
        "value": "0.4",
        "unit": "um Ra",
        "conf": "secondary",
        "corrected": True,
    },
    ("process/forming/sheet-metal-bending", "c5"): {
        "text": "Minimum flange length 4 x thickness (Protolabs); hems: Protolabs "
        "hem return 6 x T, Xometry flat hem 4 x T",
        "value": "4; hem 4-6",
        "unit": "x T",
        "conf": "secondary",
        "corrected": True,
    },
    (EX, "c14"): {
        "drop": "casting wall figures, off-topic in the extrusion node; casting nodes hold them"
    },
    (EX, "c15"): {
        "text": "Extrusion die cost (one supplier): solid 250-600 USD, fine-featured solid 450-1000, single-void hollow "
        "600-1400, multi-void hollow 1200-4000+; quotes vary widely",
        "value": "250-4000+",
        "unit": "USD",
        "conf": "secondary",
        "corrected": True,
    },
    (FG, "c2"): {
        "text": "Forging draft: internal faces need more than external; steel about 5-7 deg external, 7-10 deg "
        "internal (FIA: 3-7 deg for finished forgings)",
        "value": "5-7 / 7-10",
        "unit": "deg",
        "conf": "secondary",
        "corrected": True,
    },
    (FG, "c16"): {
        "text": "Die-closure tolerance (FIA Table II): carbon steel 1.1-6.4 mm, stainless 1.6-7.9 mm, rising with "
        "projected area at the trim line from 0 to over 650 x 1000 mm2",
        "corrected": True,
    },
    # --- batch S
    (TF, "c3"): {
        "text": "Draw ratio (depth over narrowest opening): up to about 3:1 plain vacuum, 4:1 with plug assist; "
        "design near 1:1 for uniform walls",
        "value": "3 / 4",
        "unit": "ratio",
        "conf": "secondary",
        "corrected": True,
    },
    (TF, "c23"): {
        "text": "Forming temperature windows: ABS 150-175 C; PETG 138-160 C; HIPS 127-182 C (about 150 C normal)",
        "value": "150-175 / 138-160 / 127-182",
        "unit": "C",
        "conf": "secondary",
        "corrected": True,
    },
    ("process/forming/corrugated-board", "c16"): {
        "text": "FEFCO worked example (B flute): blank 653 +-4 x 403 +-4 x 256 +-3 mm",
        "corrected": True,
    },
    (UC, "c17"): {
        "text": "Urethane casting tooling (master plus silicone mould) often about 200-1000 USD",
        "value": "200-1000",
        "unit": "USD",
        "conf": "secondary",
        "corrected": True,
    },
    (UC, "c4"): {"drop": SPAM},
    (UC, "c8"): {"drop": SPAM},
    (UC, "c9"): {"drop": SPAM},
    (UC, "c10"): {
        "drop": SPAM + "; Xometry gives a 3.2 mm fillet on inside corners instead"
    },
    # --- batch L
    (FDM + "overhangs-bridges-supports", "c18"): {
        "drop": "rib thickness cited to a resin (SL1) page with no rib figure; wrong "
        "process"
    },
    # --- batch T (plating, powder coat, annealing)
    (ANN, "c2"): {
        "drop": "PLA 50-60 C / 6-12 h is on no source and sits at or below PLA's glass transition; c5 holds the "
        "measured schedules (90 C / 60 min, 100 C / 90 min)"
    },
    (ANN, "c3"): {
        "drop": "PETG schedule not on the cited Bambu page, which lists PETG as benefiting; c6 holds the measured "
        "schedules (60-70 C / 90 min)"
    },
    (ANN, "c15"): {"drop": "4-12 h cycle not on the cited Bambu page"},
    ("process/coating/plating-and-passivation", "c9"): {
        "drop": "NASA PRC-5007 has no corner-radius rule; not found elsewhere"
    },
    # --- batch V (injection mould)
    (IM + "undercuts-and-actions", "c9"): {
        "text": TEX,
        "value": "1.5",
        "unit": "deg per 0.025 mm",
        "conf": "secondary",
        "url": "https://www.efunda.com/designstandards/plastic_design/draft.cfm",
        "corrected": True,
        "note": "Hubs' 0.4 deg per 0.1 mm (about 15x lower) is an outlier and under-drafts",
    },
    (IM + "tooling-cost-quantity", "c4"): {
        "text": TEX,
        "value": "1.5; 3",
        "unit": "deg",
        "conf": "secondary",
        "url": "https://www.efunda.com/designstandards/plastic_design/draft.cfm",
        "corrected": True,
        "note": "Hubs' 0.4 deg per 0.1 mm (about 15x lower) is an outlier and under-drafts",
    },
    (IM + "shrinkage-and-tolerances", "c12"): {
        "value": "ABS 0.4-0.7; PC 0.65-0.70",
        "unit": "percent",
        "conf": "handbook",
        "corrected": True,
    },
    (IM + "shrinkage-and-tolerances", "c14"): {
        "value": "0.4-0.5 flow; 1.0-1.2 cross",
        "unit": "percent",
        "conf": "handbook",
        "corrected": True,
    },
    (IM + "gates-weld-lines-warpage", "c20"): {
        "value": "0.1 flow; 0.5 transverse",
        "unit": "percent",
    },
    (IM + "resin-selection", "c24"): {
        "value": "50-70 percent; 3000 to >100000 USD",
        "unit": None,
        "corrected": True,
    },
    "process/cutting/edm-sinker": {
        "avoid_when": [
            "geometry millable cheaply (rounded ribs instead of knife edges)",
            "through profiles (wire EDM) or nonconductive stock",
            "mirror finish needed in production: fine EDM finishing reaches Ra 0.1-0.2 um but is slow; polishing is usual",
        ]
    },
}

ANN = "process/property-change/annealing-printed-parts"
TEX = (
    "Textured faces: add about 1.5 degrees of draft per 0.025 mm (0.001 in) of texture depth on top of base draft; "
    "light texture at least 3 degrees"
)

OV = {
    # --- batch T (plating, powder coat, annealing)
    (ANN, "c2"): {
        "drop": "PLA 50-60 C / 6-12 h is on no source and sits at or below PLA's glass transition; c5 holds the "
        "measured schedules (90 C / 60 min, 100 C / 90 min)"
    },
    (ANN, "c3"): {
        "drop": "PETG schedule not on the cited Bambu page, which lists PETG as benefiting; c6 holds the measured "
        "schedules (60-70 C / 90 min)"
    },
    (ANN, "c15"): {"drop": "4-12 h cycle not on the cited Bambu page"},
    ("process/coating/plating-and-passivation", "c9"): {
        "drop": "NASA PRC-5007 has no corner-radius rule; not found elsewhere"
    },
    # --- batch V (injection mould)
    (IM + "undercuts-and-actions", "c9"): {
        "text": TEX,
        "value": "1.5",
        "unit": "deg per 0.025 mm",
        "conf": "secondary",
        "url": "https://www.efunda.com/designstandards/plastic_design/draft.cfm",
        "corrected": True,
        "note": "Hubs' 0.4 deg per 0.1 mm (about 15x lower) is an outlier and under-drafts",
    },
    (IM + "tooling-cost-quantity", "c4"): {
        "text": TEX,
        "value": "1.5; 3",
        "unit": "deg",
        "conf": "secondary",
        "url": "https://www.efunda.com/designstandards/plastic_design/draft.cfm",
        "corrected": True,
        "note": "Hubs' 0.4 deg per 0.1 mm (about 15x lower) is an outlier and under-drafts",
    },
    (IM + "shrinkage-and-tolerances", "c12"): {
        "value": "ABS 0.4-0.7; PC 0.65-0.70",
        "unit": "percent",
        "conf": "handbook",
        "corrected": True,
    },
    (IM + "shrinkage-and-tolerances", "c14"): {
        "value": "0.4-0.5 flow; 1.0-1.2 cross",
        "unit": "percent",
        "conf": "handbook",
        "corrected": True,
    },
    (IM + "gates-weld-lines-warpage", "c20"): {
        "value": "0.1 flow; 0.5 transverse",
        "unit": "percent",
    },
    (IM + "resin-selection", "c24"): {
        "value": "50-70 percent; 3000 to >100000 USD",
        "unit": None,
        "corrected": True,
    },
    # --- batch Q
    ("process/additive/sla", "c19"): {
        "text": "Xometry SLA standard resolution: linear features below 0.508 mm at risk, below "
        "0.254 mm will not build",
        "value": "0.508 / 0.254",
        "unit": "mm",
        "corrected": True,
        "conf": "secondary",
    },
    ("process/additive/sla", "c20"): {
        "text": "Xometry SLA recommended minimum wall",
        "value": "1.0",
        "unit": "mm",
        "corrected": True,
        "conf": "secondary",
    },
    ("process/cutting/edm-sinker", "c1"): {
        "text": "Sinker EDM tolerance: about +-0.025 mm standard (Noramark); +-0.004 mm is a "
        "best case (Xometry)",
        "value": "0.025 (best 0.004)",
        "unit": "mm",
    },
    ("process/cutting/edm-sinker", "c16"): {
        "drop": "unsupported and contradicted by c11 (fine finishing Ra 0.1-0.2 um)"
    },
    ("process/cutting/laser-cutting", "c24"): {
        "text": "Typical metal laser kerf (Xometry)",
        "value": "0.008-0.012 in (0.2-0.3 mm)",
        "unit": "in",
    },
    ("process/cutting/laser-cutting", "c32"): {
        "text": "ISO 9013 classifies thermal-cut quality (perpendicularity and Rz5 "
        "ranges) for laser cuts 0.5-32 mm; class values not reproduced here",
        "value": None,
        "unit": None,
        "conf": "excerpt-only",
    },
    # --- batch P (lathe)
    (LA + "grooves-and-reliefs", "c5"): {
        "text": ISO2768M,
        "value": "0.1/0.1/0.2/0.3/0.5/0.8",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
        "note": "draft had +-0.5 for 30-120 mm (that is the 120-400 row)",
    },
    (LA + "grooves-and-reliefs", "c6"): {
        "text": "ISO 2768-2 class K straightness/flatness: 0.05 (up to 10 mm), 0.1 (10-30), "
        "0.2 (30-100), 0.4 (100-300); class H is half these. ISO 2768-2 is replaced "
        "by ISO 22081:2021",
        "value": "0.05/0.1/0.2/0.4",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
        "note": "draft gave the class H values as K",
    },
    (LA + "grooves-and-reliefs", "c9"): {
        "drop": "DIN 509 Form E size bands disagree between the cited shop article and a DIN "
        "509:2006 copy; no maker table found. S1: source DIN 509 rows from a "
        "catalogue before adding"
    },
    (LA + "grooves-and-reliefs", "c10"): {
        "text": "DIN 509 undercut radius tolerance +-0.1 mm",
        "value": "0.1",
        "unit": "mm",
    },
    (LA + "grooves-and-reliefs", "c11"): {
        "text": "DIN 471 covers shaft rings 3-300 mm; DIN 472 bore rings 8-300 mm (normal type)",
        "value": "3-300 / 8-300",
        "unit": "mm",
        "corrected": True,
    },
    (LA + "grooves-and-reliefs", "c13"): {
        "text": "DIN 472 groove examples: 20 mm bore, groove 21.0 mm dia x 1.1 mm wide; 50 mm "
        "bore, 53.0 x 2.15 mm. Take groove sizes from the ring maker's table, not "
        "from the ring's free diameter",
        "value": "21.0 x 1.1 / 53.0 x 2.15",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
    },
    (LA + "bores-and-internal", "c3"): {
        "text": ISO2768M,
        "value": "0.1/0.1/0.2/0.3/0.5/0.8",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
        "url": "https://brassland.com/resources/standards/iso-2768/",
        "note": "the cited guide's reproduction (+-0.10 for 3-30, +-0.15, +-0.20) is wrong",
    },
    # --- batch T2 (anodising, heat treatment)
    (AN, "c22"): {
        "text": "Hard anodising on threads: NASA PRC-5006 advises against it; ISO 10074 allows thin hardcoat (up to "
        "25 um) on threads. Mask threads or allow for growth on both flanks"
    },
    (AN, "c26"): {
        "text": "Hard anodising (ISO 10074): edge radius at least 10 times the coating thickness (25 um coat 0.25 mm, "
        "50 um 0.5 mm, 100 um 1.0 mm); not a rule for thin decorative Type II"
    },
    (AN, "c29"): {
        "text": "Anodic oxide is electrically insulating; grounding and bonding areas are commonly masked or machined "
        "bare after anodising"
    },
    (HT, "c8"): {
        "text": "Effective case depth: depth to 550 HV in ISO practice; US practice (SAE J423) often uses 50 HRC "
        "(about 513 HV). State which criterion on the drawing",
        "conf": "secondary",
        "corrected": True,
        "note": "verifier found the 50 HRC criterion at the cited source and 550 HV only at a secondary page; "
        "the ISO standard (ISO 2639) was not read",
    },
    # --- batch M (desktop-fdm)
    (FDM + "dimensional-accuracy", "c7"): {
        "text": "Supporting walls 1.2-1.5 mm; features thinner than twice the extrusion road "
        "usually fail (Xometry, industrial 0.254 mm road)",
        "value": "1.2-1.5",
        "unit": "mm",
        "eq": "industrial-fdm",
        "conf": "secondary",
        "corrected": True,
        "note": "the draft's 2 x 0.4 mm nozzle = 0.8 mm desktop rule is not in the source",
    },
    (FDM + "dimensional-accuracy", "c20"): {
        "text": "Bambu, hard or abrasive filaments: 0.4 mm hardened nozzle with 0.15-0.30 mm "
        "layers (second choice; 0.6 mm hardened nozzle is first choice)",
        "value": "0.15-0.30",
        "unit": "mm",
        "conf": "handbook",
        "corrected": True,
    },
    (FDM + "material-selection", "c31"): {
        "text": "Bambu oven drying: PETG 60-65 C 8 h; ABS/ASA 75-85 C 8 h; PC 75-85 C 8 h; "
        "PA6-CF/PAHT-CF 75-85 C 8-12 h",
        "value": "60-65/8; 75-85/8; 75-85/8-12",
        "unit": "C/h",
        "conf": "handbook",
        "corrected": True,
        "note": "table columns read from stripped HTML; oven column inferred",
    },
    # --- batch N (cnc-mill)
    (MILL + "holes-and-threads", "c4"): {
        "text": "Drilled hole depth: design to 4-6 times diameter; up to about 10 times is possible "
        "but costs more",
        "value": "6 (10 at extra cost)",
        "unit": "x D",
        "url": "https://www.hubs.com/knowledge-base/reducing-cnc-machining-costs-design-tips/",
        "conf": "secondary",
        "corrected": True,
    },
    (MILL + "holes-and-threads", "c15"): {
        "text": "Blind tapped hole: leave at least 0.5 times diameter unthreaded below the thread",
        "value": "0.5",
        "unit": "x D",
        "note": "the draft's 1.5x half was not on either page; dropped",
    },
    (MILL + "holes-and-threads", "c7"): {
        "text": "Cavity depth about 3-5 times its width (Fictiv lists max cavity depth 4 times "
        "tool diameter)",
        "value": "3-5",
        "unit": "ratio",
    },
    # --- batch U (injection mould)
    (IM + "wall-thickness", "c3"): {
        "text": "Nominal wall for PP",
        "value": "0.64-3.81",
        "unit": "mm",
        "conf": "secondary",
        "corrected": True,
        "note": "draft copied the PC row; Protolabs and Fictiv give PP 0.64-3.81 mm",
    },
    (IM + "draft-and-texture", "c6"): {
        "text": "Protolabs nominal wall range for PC",
        "value": "1.02-3.81",
        "unit": "mm",
        "conf": "secondary",
        "url": PL_GUIDE,
        "corrected": True,
        "note": "one Protolabs page prints 0.11 mm (typo); the design-guidelines page gives 0.040-0.150 in",
    },
    (IM + "ribs-and-bosses", "c4"): {
        "text": "Protolabs nominal wall range for PC",
        "value": "1.02-3.81",
        "unit": "mm",
        "conf": "secondary",
        "url": PL_GUIDE,
        "corrected": True,
        "note": "one Protolabs page prints 0.11 mm (typo); the design-guidelines page gives 0.040-0.150 in",
    },
    (IM + "draft-and-texture", "c13"): {
        "text": "Draft rule of thumb: about 1 degree per 25 mm (1 in) of draw depth; one Protolabs page "
        "says 1 degree for each of the first 2 in",
        "value": "1",
        "unit": "deg per 25 mm",
        "conf": "secondary",
        "corrected": True,
    },
    (IM + "draft-and-texture", "c21"): {
        "text": "Light textures (Mold-Tech MT-11010 and MT-11020 class): at least 3 degrees draft "
        "per the texture chart; the 1.5 deg per 0.001 in depth rule gives less and is not "
        "the published minimum for these codes",
        "value": "3",
        "unit": "deg",
        "conf": "secondary",
        "corrected": True,
    },
    (IM + "draft-and-texture", "c22"): {
        "drop": "arithmetic (1.5 x 1.5 deg), not a published MT-11020 value; merged into c21"
    },
    (IM + "radii-and-corners", "c21"): {
        "text": "Light texture (Protolabs PM-T2): at least 3 degrees draft; heavier textures need "
        "5 degrees or more",
        "value": "3",
        "unit": "deg",
        "conf": "secondary",
        "corrected": True,
        "note": "the 5 deg for PM-T2 in the draft was a table misread; 5+ applies to heavier textures",
    },
}
