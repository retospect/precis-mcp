"""Build the corpus-drawn gold set for the two *wired* eval axes.

Sources are real precis corpus material (paper ``pa38395`` — Ren et al.,
*Chemical Engineering Science*, mesoporous CuO/CeO2 for NO oxidation — its
abstract + introduction prose, and the reference block from ``pc1344284``,
Mohan et al. 2020 SCR review). No public benchmarks.

Two axes, 8 tasks each:

* ``long-context-recall`` (``score_needle``) — a long real-prose haystack; the
  answer is either a real fact stated in the text or a unique needle planted at
  a varying depth (the classic needle-in-a-haystack probe, over corpus prose).
* ``tool-structured`` (``score_tool_json``) — extract a scalar-valued JSON
  object from real inline content (bibliographic refs, abstract facts). The
  live search->get tool loop is the heavier follow-up; this measures the
  structured-answer half ``score_tool_json`` actually scores.

Emits ``scripts/llm_eval/gold_set/corpus_v1.json`` (loadable via
``precis llm eval --gold`` or the harness ``load_gold_set``).
"""

from __future__ import annotations

import json
from pathlib import Path

# --- real corpus prose (paper pa38395: abstract + introduction) --------------
ABSTRACT = (
    "Rapid and efficient construction of mesoporous CuO/CeO2 catalyst with "
    "abundant Cu-O-Ce interface sites for NO oxidation. Interface asymmetric "
    "dual-atom sites (M-O-N) are generally recognized as active sites for "
    "supported oxide catalysts in catalytic oxidation reactions. However, the "
    "rapid and efficient construction of catalysts rich in interface "
    "asymmetric dual-atom active sites remains a challenge in catalyst design "
    "and precise synthesis. In this work, a mesoporous CuO/CeO2-GU catalyst "
    "with abundant interface Cu-O-Ce sites was prepared via a one-pot method "
    "using deep eutectic solvent (DES). The 15CuO/CeO2-GU exhibited "
    "significantly superior NO catalytic oxidation performance compared to the "
    "15CuO/CeO2-IM prepared by the traditional impregnation method. The NO "
    "conversion of 15CuO/CeO2-GU (100%) is twice that of 15CuO/CeO2-IM (50%) "
    "at 275 degC. Meanwhile, it outperformed the 1Pt/Al2O3 catalyst under the "
    "same conditions. The excellent NO oxidation performance is attributed to "
    "the catalyst being composed of ultra-fine nanoparticles (~10 nm) stacked "
    "together, which improves the dispersion of Cu on CeO2 and creates strong "
    "interaction between Cu and CeO2."
)
INTRO = (
    "1. Introduction. Nitrogen oxides (NOx), as major precursors of PM2.5 and "
    "ozone which are the primary urban pollutants, continue to threaten the "
    "ecological environment and human health. Among the existing NOx abatement "
    "technologies, various methods have been developed, including selective "
    "catalytic reduction (SCR), selective non-catalytic reduction (SNCR), NOx "
    "storage and reduction (NSR) and others. Among them, NO oxidation "
    "technology has attracted significant attention due to its utilization of "
    "O2 in the air as the oxidant without the need for additional reducing "
    "agents such as ammonia (NH3), and its ability to achieve nitrogen "
    "resource recovery. The core of NO oxidation technology lies in a high "
    "efficiency catalyst. Currently, commonly used NO oxidation catalysts "
    "include noble metal, zeolite and transition metal oxide catalysts. The "
    "widespread application of noble metal catalysts is limited by the scarcity "
    "and high cost of noble metals. Zeolite catalysts often utilize the "
    "confinement effect of pores to catalytically oxidize NO to NO2 at low "
    "temperatures (< 100 degC), but their NO oxidation activity decreases "
    "significantly with increasing temperature. In contrast, transition metal "
    "oxide catalysts have received extensive attention due to their low cost "
    "and good low-medium temperature activity. Among transition metal oxide "
    "catalysts, TiO2-based, MnO2-based and CeO2-based catalysts are widely "
    "used in catalytic oxidation reactions. CeO2 is commonly used as a "
    "catalyst support for oxidation/reduction reactions due to its surface "
    "oxygen species with strong mobility and excellent oxygen storage and "
    "release capacity. A large number of studies have shown that the "
    "micro-interface structure M-O-Ce sites in ceria-based supported catalysts "
    "can activate lattice oxygen, generating a large number of oxygen "
    "vacancies and surface active oxygen species, thus often serving as the "
    "main active sites in oxidation/reduction reactions. For example, Chen et "
    "al. found that the Cu-O-Ce sites formed by copper substituting lattice "
    "oxygen in CeO2 in the Cu/MgO-CeO2 catalyst prepared by the citric acid "
    "sol-gel method are the active centers for the CO-SCR reaction. Lu et al. "
    "similarly discovered that Fe atoms in the FexOy-CuO-CeO2 catalyst "
    "synthesized with a solvent-free combustion synthesis method can enter the "
    "CeO2 lattice to form a Fe-O-Ce structure. Lu et al. reported that the "
    "Cu-O-Ce interface formed in the CuO-CeO2 catalyst prepared by the "
    "hydrothermal method promotes electron transfer between Cu and Ce, "
    "resulting in high oxygen mobility and excellent catalytic oxidation "
    "ability for toluene. Cui et al. also prepared MnOx-CeO2 catalysts by "
    "co-precipitation, impregnation and mechanical mixing and found that the "
    "presence of Mn-O-Ce sites increases the content of Mn3+ and active oxygen "
    "species, facilitating the oxidation of NO to NO2."
)
HAY = ABSTRACT + "\n\n" + INTRO


def _plant(hay: str, needle_sentence: str, frac: float) -> str:
    """Insert ``needle_sentence`` at ``frac`` depth (0..1) between sentences."""
    sents = hay.split(". ")
    at = max(1, min(len(sents) - 1, round(len(sents) * frac)))
    sents.insert(at, needle_sentence.rstrip("."))
    return ". ".join(sents)


def _recall(
    task_id: str,
    question: str,
    needle: str,
    aliases: list[str] | None = None,
    hay: str = HAY,
) -> dict:
    prompt = (
        "You are given an excerpt from a scientific paper. Read it and answer "
        "the question at the end with ONLY the requested value, nothing else.\n\n"
        f"--- EXCERPT ---\n{hay}\n--- END EXCERPT ---\n\nQuestion: {question}"
    )
    expect = {"needle": needle}
    if aliases:
        expect["aliases"] = aliases
    return {
        "task_id": task_id,
        "axis": "long-context-recall",
        "scorer": "needle",
        "prompt": prompt,
        "expect": expect,
    }


def _toolq(task_id: str, source: str, keys_spec: str, answer: dict) -> dict:
    prompt = (
        "Extract the requested fields from the source text below and return "
        "ONLY a single JSON object as the very last thing in your reply — no "
        "prose, no code fences, no units or % signs inside values, each value a "
        f"plain string.\n\nReturn exactly these keys: {keys_spec}\n\n"
        f"--- SOURCE ---\n{source}\n--- END SOURCE ---"
    )
    return {
        "task_id": task_id,
        "axis": "tool-structured",
        "scorer": "tool_json",
        "tools_needed": False,
        "prompt": prompt,
        "expect": {"answer": answer},
    }


# planted-needle haystacks at varying depth (deep recall over real prose)
HAY_BATCH = _plant(
    HAY,
    "For internal tracking, the batch identifier for this synthesis run is BX-7731.",
    0.15,
)
HAY_DATE = _plant(
    HAY,
    "The long-term catalyst stability test concluded on "
    "2027-03-08 without measurable deactivation.",
    0.55,
)
HAY_LABEL = _plant(
    HAY,
    "The blank control reference sample in this study was "
    "designated RS-CeO2-Q9 throughout.",
    0.9,
)

REF_MOHAN = (
    "Mohan, S., Dinesha, P., Kumar, S., 2020. NOx reduction behaviour "
    "in copper zeolite catalysts for ammonia SCR systems: a review. "
    "Chem. Eng. J. 384, 123253."
)
REF_WANG = (
    "Wang, Y.W., Tang, X.L., Yi, H.H., Li, Z.G., Ren, X.N., Gao, F.Y., "
    "Yao, Y., Cheng, H.D., Yu, Q.J., 2025. A progressive review on NOx "
    "purification from diesel vehicle exhaust. Sep. Purif. Technol. "
    "354, 129111."
)
REF_LIU = (
    "Liu, G., Gao, P.X., 2011. A review of NOx storage/reduction "
    "catalysts: mechanism, materials and degradation studies. Cat. Sci. "
    "Technol. 1 (4), 552-568."
)

TASKS = [
    # --- long-context-recall (8) ---
    _recall(
        "needle-temp-01",
        "At what temperature (in degrees Celsius) does 15CuO/CeO2-GU reach "
        "100% NO conversion? Answer with only the number.",
        "275",
    ),
    _recall(
        "needle-conv-im-02",
        "What is the NO conversion percentage of the 15CuO/CeO2-IM catalyst "
        "at that temperature? Answer with only the number.",
        "50",
    ),
    _recall(
        "needle-size-03",
        "What is the approximate size of the ultra-fine nanoparticles? "
        "Include the unit.",
        "10 nm",
        ["10nm"],
    ),
    _recall(
        "needle-method-04",
        "Which preparation solvent/method was used for the superior "
        "CuO/CeO2-GU catalyst?",
        "deep eutectic solvent",
        ["DES"],
    ),
    _recall(
        "needle-site-05",
        "What is the name of the interface active site emphasized for this "
        "CuO/CeO2 catalyst?",
        "Cu-O-Ce",
    ),
    _recall(
        "needle-plant-batch-06",
        "What is the internal batch identifier for this synthesis run?",
        "BX-7731",
        hay=HAY_BATCH,
    ),
    _recall(
        "needle-plant-date-07",
        "On what date did the long-term catalyst stability test conclude?",
        "2027-03-08",
        hay=HAY_DATE,
    ),
    _recall(
        "needle-plant-label-08",
        "What designation was given to the blank control reference sample?",
        "RS-CeO2-Q9",
        hay=HAY_LABEL,
    ),
    # --- tool-structured (8) ---
    _toolq(
        "tool-ref-mohan-01",
        REF_MOHAN,
        '"first_author" (surname), "year" (4 digits), "volume"',
        {"first_author": "Mohan", "year": "2020", "volume": "384"},
    ),
    _toolq(
        "tool-ref-wang-02",
        REF_WANG,
        '"first_author" (surname), "year" (4 digits), "volume"',
        {"first_author": "Wang", "year": "2025", "volume": "354"},
    ),
    _toolq(
        "tool-ref-liu-03",
        REF_LIU,
        '"first_author" (surname), "year" (4 digits), "volume"',
        {"first_author": "Liu", "year": "2011", "volume": "1"},
    ),
    _toolq(
        "tool-abstract-perf-04",
        ABSTRACT,
        '"best_catalyst", "no_conversion_pct" (number only), '
        '"temperature_c" (number only)',
        {
            "best_catalyst": "15CuO/CeO2-GU",
            "no_conversion_pct": "100",
            "temperature_c": "275",
        },
    ),
    _toolq(
        "tool-abstract-compare-05",
        ABSTRACT,
        '"des_conversion" (number), "impregnation_conversion" (number)',
        {"des_conversion": "100", "impregnation_conversion": "50"},
    ),
    _toolq(
        "tool-intro-oxidant-06",
        INTRO,
        '"oxidant" (the oxidant used by NO oxidation technology), '
        '"needs_reducing_agent" (yes or no)',
        {"oxidant": "O2", "needs_reducing_agent": "no"},
    ),
    _toolq(
        "tool-abstract-support-07",
        ABSTRACT,
        '"support_material" (the oxide support), "interface_site"',
        {"support_material": "CeO2", "interface_site": "Cu-O-Ce"},
    ),
    _toolq(
        "tool-intro-zeolite-08",
        INTRO,
        '"low_temp_product" (what zeolites oxidize NO into at low '
        'temperature), "temp_threshold_c" (the low-temperature threshold '
        "number in degC)",
        {"low_temp_product": "NO2", "temp_threshold_c": "100"},
    ),
]

if __name__ == "__main__":
    out = Path(__file__).parent / "gold_set" / "corpus_v1.json"
    out.write_text(json.dumps(TASKS, indent=2))
    n_recall = sum(t["axis"] == "long-context-recall" for t in TASKS)
    n_tool = sum(t["axis"] == "tool-structured" for t in TASKS)
    print(
        f"wrote {out} — {len(TASKS)} tasks "
        f"({n_recall} long-context-recall, {n_tool} tool-structured)"
    )
