"""Chemical name / CAS / abbreviation → canonical SMILES + InChIKey.

The pure resolver behind ``get(kind='chem', id=...)``. Network access is
injected as a :data:`Fetch` callable so tests replay recorded responses;
:func:`http_fetch` is the production fetcher (SSRF-pinned ``safe_get``).

Tiers, first answer wins:

1. **abbreviation table** — curated lab shorthand (DCM, THF, Boc2O, …).
   Known-ambiguous shorthand (TEA, DCE, DMA, DMP) carries every reading
   and refuses.
2. **OPSIN** (EBI web service) — systematic IUPAC names. A parse is
   deterministic, so an OPSIN hit is never ambiguous. Skipped for CAS
   numbers. Runs remote because the image has no JVM.
3. **PubChem PUG REST** — trivial names, trade names, CAS numbers.
   PubChem silently picks one record for an underspecified name
   ("dichloroethane" → the 1,1-isomer), so a PubChem hit is
   cross-checked against tier 4.
4. **NCI/CADD CIR** — fallback when PubChem misses, corroboration when
   it hits. Connectivity disagreement (InChIKey first block) between
   PubChem and CIR is refused as ambiguous with both readings listed.

A tier that faults (transport error, 5xx) is recorded and skipped. When
nothing answered and something faulted, :func:`resolve` returns status
``fault`` so the caller does not cache a miss caused by an outage.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

#: ``fetch(url) -> (http_status, body_text)``. Raises on transport failure.
Fetch = Callable[[str], tuple[int, str]]

OPSIN_URL = "https://www.ebi.ac.uk/opsin/ws/{name}.json"
PUBCHEM_CIDS_URL = (
    "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{name}/cids/JSON"
)
PUBCHEM_PROPS_URL = (
    "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cids}"
    "/property/SMILES,InChIKey,Title/JSON"
)
PUBCHEM_COMPOUND_URL = "https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
CIR_URL = "https://cactus.nci.nih.gov/chemical/structure/{name}/{rep}"

#: StructureReading cap when PubChem answers several CIDs for one name.
MAX_CANDIDATES = 5

_CAS_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")

# name, SMILES. One entry = unambiguous; several = refuse and list them.
_ABBREVIATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "DCM": (("dichloromethane", "ClCCl"),),
    "THF": (("tetrahydrofuran", "C1CCOC1"),),
    "DMF": (("N,N-dimethylformamide", "CN(C)C=O"),),
    "DMSO": (("dimethyl sulfoxide", "CS(C)=O"),),
    "DMAc": (("N,N-dimethylacetamide", "CC(=O)N(C)C"),),
    "MeCN": (("acetonitrile", "CC#N"),),
    "ACN": (("acetonitrile", "CC#N"),),
    "MeOH": (("methanol", "CO"),),
    "EtOH": (("ethanol", "CCO"),),
    "IPA": (("propan-2-ol", "CC(C)O"),),
    "iPrOH": (("propan-2-ol", "CC(C)O"),),
    "EtOAc": (("ethyl acetate", "CCOC(C)=O"),),
    "Et2O": (("diethyl ether", "CCOCC"),),
    "MTBE": (("methyl tert-butyl ether", "COC(C)(C)C"),),
    "TBME": (("methyl tert-butyl ether", "COC(C)(C)C"),),
    "DME": (("1,2-dimethoxyethane", "COCCOC"),),
    "NMP": (("N-methyl-2-pyrrolidone", "CN1CCCC1=O"),),
    "HMPA": (("hexamethylphosphoramide", "CN(C)P(=O)(N(C)C)N(C)C"),),
    "AcOH": (("acetic acid", "CC(=O)O"),),
    "TFA": (("trifluoroacetic acid", "OC(=O)C(F)(F)F"),),
    "TsOH": (("p-toluenesulfonic acid", "Cc1ccc(cc1)S(=O)(=O)O"),),
    "PTSA": (("p-toluenesulfonic acid", "Cc1ccc(cc1)S(=O)(=O)O"),),
    "Et3N": (("triethylamine", "CCN(CC)CC"),),
    "DIPEA": (("N,N-diisopropylethylamine", "CCN(C(C)C)C(C)C"),),
    "DIEA": (("N,N-diisopropylethylamine", "CCN(C(C)C)C(C)C"),),
    "NMM": (("N-methylmorpholine", "CN1CCOCC1"),),
    "DMAP": (("4-(dimethylamino)pyridine", "CN(C)c1ccncc1"),),
    "DBU": (("1,8-diazabicyclo[5.4.0]undec-7-ene", "C1CCC2=NCCCN2CC1"),),
    "Boc2O": (("di-tert-butyl dicarbonate", "CC(C)(C)OC(=O)OC(=O)OC(C)(C)C"),),
    "DCC": (("N,N'-dicyclohexylcarbodiimide", "C(=NC1CCCCC1)=NC1CCCCC1"),),
    "EDC": (("1-ethyl-3-(3-dimethylaminopropyl)carbodiimide", "CCN=C=NCCCN(C)C"),),
    "NBS": (("N-bromosuccinimide", "BrN1C(=O)CCC1=O"),),
    "NCS": (("N-chlorosuccinimide", "ClN1C(=O)CCC1=O"),),
    "mCPBA": (("3-chloroperoxybenzoic acid", "OOC(=O)c1cccc(Cl)c1"),),
    "LDA": (("lithium diisopropylamide", "[Li+].CC(C)[N-]C(C)C"),),
    "TBAF": (("tetrabutylammonium fluoride", "CCCC[N+](CCCC)(CCCC)CCCC.[F-]"),),
    "DEAD": (("diethyl azodicarboxylate", "CCOC(=O)/N=N/C(=O)OCC"),),
    "DIAD": (("diisopropyl azodicarboxylate", "CC(C)OC(=O)/N=N/C(=O)OC(C)C"),),
    "PPh3": (("triphenylphosphine", "c1ccc(cc1)P(c1ccccc1)c1ccccc1"),),
    "TEMPO": (("TEMPO", "CC1(C)CCCC(C)(C)N1[O]"),),
    "TEA": (
        ("triethylamine", "CCN(CC)CC"),
        ("triethanolamine", "OCCN(CCO)CCO"),
    ),
    "DCE": (
        ("1,2-dichloroethane", "ClCCCl"),
        ("1,1-dichloroethene", "C=C(Cl)Cl"),
    ),
    "DMA": (
        ("N,N-dimethylacetamide", "CC(=O)N(C)C"),
        ("dimethylamine", "CNC"),
    ),
    "DMP": (
        ("Dess-Martin periodinane", "CC(=O)OI1(OC(C)=O)(OC(C)=O)OC(=O)c2ccccc21"),
        ("2,2-dimethoxypropane", "COC(C)(C)OC"),
    ),
}

_ABBREV_FOLDED: dict[str, str] = {k.casefold(): k for k in _ABBREVIATIONS}


@dataclass(frozen=True, slots=True)
class StructureReading:
    """One structure reading of the query, with where it came from."""

    smiles: str
    inchikey: str
    name: str
    source: str
    source_ref: str = ""
    """Provider-side id / URL (PubChem CID page, OPSIN request URL, …)."""
    source_smiles: str = ""
    """The SMILES as the source returned it, before rdkit canonicalisation."""


@dataclass(slots=True)
class Resolution:
    """Outcome of :func:`resolve`. ``status`` is one of ``resolved``,
    ``ambiguous``, ``not_found`` or ``fault``."""

    query: str
    query_type: str
    status: str
    candidates: list[StructureReading] = field(default_factory=list)
    tiers: list[tuple[str, str]] = field(default_factory=list)
    """``(tier, outcome)`` in the order tried — hit / miss / fault: … / skipped."""
    notes: list[str] = field(default_factory=list)
    canonicaliser: str = ""

    @property
    def answer(self) -> StructureReading | None:
        return self.candidates[0] if self.status == "resolved" else None

    def to_meta(self) -> dict[str, Any]:
        ans = self.answer
        return {
            "query": self.query,
            "query_type": self.query_type,
            "status": self.status,
            "smiles": ans.smiles if ans else None,
            "inchikey": ans.inchikey if ans else None,
            "source": ans.source if ans else None,
            "candidates": [
                {
                    "name": c.name,
                    "smiles": c.smiles,
                    "inchikey": c.inchikey,
                    "source": c.source,
                    "source_ref": c.source_ref,
                }
                for c in self.candidates
            ],
            "tiers": [list(t) for t in self.tiers],
            "notes": list(self.notes),
            "canonicaliser": self.canonicaliser,
        }


# ── query classification ─────────────────────────────────────────────────


def normalise_query(raw: str) -> str:
    """Collapse whitespace; case is kept (``CO`` and ``Co`` differ)."""
    return " ".join((raw or "").split())


def cas_checksum_ok(cas: str) -> bool:
    """CAS check digit: weighted sum of the other digits, mod 10."""
    m = _CAS_RE.match(cas)
    if m is None:
        return False
    digits = (m.group(1) + m.group(2))[::-1]
    total = sum((i + 1) * int(d) for i, d in enumerate(digits))
    return total % 10 == int(m.group(3))


def is_cas(query: str) -> bool:
    return _CAS_RE.match(query) is not None


def classify_query(query: str) -> str:
    if is_cas(query):
        return "cas"
    if lookup_abbreviation(query) is not None:
        return "abbreviation"
    return "name"


def lookup_abbreviation(query: str) -> tuple[tuple[str, str], ...] | None:
    if query in _ABBREVIATIONS:
        return _ABBREVIATIONS[query]
    key = _ABBREV_FOLDED.get(query.casefold())
    return _ABBREVIATIONS[key] if key is not None else None


def abbreviation_table() -> dict[str, tuple[tuple[str, str], ...]]:
    """Read-only copy of the curated table (for tests and the skill)."""
    return dict(_ABBREVIATIONS)


# ── canonicalisation ─────────────────────────────────────────────────────


def _rdkit_canonical(smiles: str) -> tuple[str, str] | None:
    """``(canonical_smiles, inchikey)`` via rdkit, or None if rdkit is
    absent or cannot parse ``smiles``."""
    try:
        from rdkit import Chem, rdBase
    except ImportError:
        return None
    rdBase.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    key = Chem.MolToInchiKey(mol) or ""
    return Chem.MolToSmiles(mol), key


def rdkit_version() -> str:
    try:
        from rdkit import rdBase
    except ImportError:
        return ""
    return f"rdkit {rdBase.rdkitVersion}"


def _candidate(
    *,
    smiles: str,
    inchikey: str,
    name: str,
    source: str,
    source_ref: str = "",
    notes: list[str],
) -> StructureReading:
    """Canonicalise a source answer. rdkit's InChIKey wins; a
    disagreement with the source's own key is noted, not hidden."""
    canon = _rdkit_canonical(smiles)
    if canon is None:
        return StructureReading(smiles, inchikey, name, source, source_ref, smiles)
    can_smiles, can_key = canon
    if inchikey and can_key and inchikey != can_key:
        notes.append(
            f"{source} InChIKey {inchikey} differs from rdkit's {can_key} "
            "for the same SMILES; rdkit's is reported"
        )
    return StructureReading(
        can_smiles, can_key or inchikey, name, source, source_ref, smiles
    )


def _skeleton(inchikey: str) -> str:
    return inchikey.split("-", 1)[0]


# ── tiers ────────────────────────────────────────────────────────────────


class _Fault(Exception):
    """A tier failed to answer (transport error or unexpected status)."""


def _call(fetch: Fetch, url: str) -> tuple[int, str]:
    try:
        status, body = fetch(url)
    except Exception as exc:
        raise _Fault(f"{type(exc).__name__}: {exc}") from exc
    if status >= 500 or status in (429, 403):
        raise _Fault(f"HTTP {status}")
    return status, body


def _q(name: str) -> str:
    return quote(name, safe="")


def _tier_opsin(query: str, fetch: Fetch, notes: list[str]) -> StructureReading | None:
    url = OPSIN_URL.format(name=_q(query))
    status, body = _call(fetch, url)
    if status in (400, 404):
        return None
    if status != 200:
        raise _Fault(f"HTTP {status}")
    data = json.loads(body)
    if data.get("status") != "SUCCESS" or not data.get("smiles"):
        return None
    return _candidate(
        smiles=str(data["smiles"]),
        inchikey=str(data.get("stdinchikey") or ""),
        name=query,
        source="OPSIN",
        source_ref=url,
        notes=notes,
    )


def _pubchem_props(cids: list[int], fetch: Fetch) -> list[dict[str, Any]]:
    url = PUBCHEM_PROPS_URL.format(cids=",".join(str(c) for c in cids))
    status, body = _call(fetch, url)
    if status != 200:
        raise _Fault(f"PubChem properties HTTP {status}")
    rows = (json.loads(body).get("PropertyTable") or {}).get("Properties") or []
    return [r for r in rows if isinstance(r, dict)]


def _tier_pubchem(query: str, fetch: Fetch, notes: list[str]) -> list[StructureReading]:
    status, body = _call(fetch, PUBCHEM_CIDS_URL.format(name=_q(query)))
    if status in (400, 404):
        return []
    if status != 200:
        raise _Fault(f"HTTP {status}")
    cids = [
        int(c)
        for c in ((json.loads(body).get("IdentifierList") or {}).get("CID") or [])
        if int(c) > 0
    ]
    if not cids:
        return []
    out: list[StructureReading] = []
    for row in _pubchem_props(cids[:MAX_CANDIDATES], fetch):
        smiles = row.get("SMILES") or row.get("IsomericSMILES") or ""
        if not smiles:
            continue
        cid = row.get("CID")
        out.append(
            _candidate(
                smiles=str(smiles),
                inchikey=str(row.get("InChIKey") or ""),
                name=str(row.get("Title") or query),
                source=f"PubChem CID {cid}",
                source_ref=PUBCHEM_COMPOUND_URL.format(cid=cid),
                notes=notes,
            )
        )
    if len(cids) > MAX_CANDIDATES:
        notes.append(
            f"PubChem returned {len(cids)} records; first {MAX_CANDIDATES} shown"
        )
    return out


def _tier_cir(query: str, fetch: Fetch, notes: list[str]) -> list[StructureReading]:
    url = CIR_URL.format(name=_q(query), rep="smiles")
    status, body = _call(fetch, url)
    if status in (400, 404):
        return []
    if status != 200:
        raise _Fault(f"HTTP {status}")
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    out: list[StructureReading] = []
    for smiles in lines[:MAX_CANDIDATES]:
        inchikey = ""
        if _rdkit_canonical(smiles) is None:
            kstatus, kbody = _call(
                fetch, CIR_URL.format(name=_q(smiles), rep="stdinchikey")
            )
            if kstatus == 200:
                inchikey = kbody.strip().removeprefix("InChIKey=")
        out.append(
            _candidate(
                smiles=smiles,
                inchikey=inchikey,
                name=query,
                source="NCI/CADD CIR",
                source_ref=url,
                notes=notes,
            )
        )
    return out


def _dedupe(cands: list[StructureReading]) -> list[StructureReading]:
    seen: set[str] = set()
    out: list[StructureReading] = []
    for c in cands:
        key = c.inchikey or c.smiles
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ── entry point ──────────────────────────────────────────────────────────


def resolve(raw_query: str, fetch: Fetch, cir_fetch: Fetch | None = None) -> Resolution:
    """Resolve ``raw_query`` through the tiers. Never raises on upstream
    trouble: faults land in ``tiers``, and ``status='fault'`` only when
    PubChem itself failed to answer. ``cir_fetch`` lets the caller give
    the corroborating CIR leg a shorter timeout."""
    query = normalise_query(raw_query)
    qtype = classify_query(query)
    res = Resolution(query=query, query_type=qtype, status="not_found")
    res.canonicaliser = rdkit_version() or "none (rdkit absent; source SMILES kept)"
    notes = res.notes

    if qtype == "cas" and not cas_checksum_ok(query):
        res.status = "not_found"
        res.tiers.append(("cas-checksum", "fail"))
        notes.append(f"{query!r} fails the CAS check digit; not looked up")
        return res

    abbrev = lookup_abbreviation(query) if qtype == "abbreviation" else None
    if abbrev is not None:
        cands = [
            _candidate(
                smiles=smi,
                inchikey="",
                name=name,
                source="precis abbreviation table",
                notes=notes,
            )
            for name, smi in abbrev
        ]
        res.tiers.append(("abbreviations", "hit"))
        res.candidates = cands
        res.status = "resolved" if len(cands) == 1 else "ambiguous"
        return res

    faulted = False

    def run(tier: str, fn: Callable[[], Any]) -> Any:
        nonlocal faulted
        try:
            got = fn()
        except _Fault as exc:
            faulted = True
            res.tiers.append((tier, f"fault: {exc}"))
            return None
        res.tiers.append((tier, "hit" if got else "miss"))
        return got

    if qtype == "name":
        opsin = run("OPSIN", lambda: _tier_opsin(query, fetch, notes))
        if opsin is not None:
            res.candidates = [opsin]
            res.status = "resolved"
            return res
    else:
        res.tiers.append(("OPSIN", "skipped (CAS number)"))

    pubchem = _dedupe(run("PubChem", lambda: _tier_pubchem(query, fetch, notes)) or [])
    pubchem_faulted = res.tiers[-1][1].startswith("fault")
    cir = _dedupe(run("CIR", lambda: _tier_cir(query, cir_fetch or fetch, notes)) or [])

    if pubchem:
        if len(pubchem) > 1:
            res.candidates, res.status = pubchem, "ambiguous"
            return res
        best = pubchem[0]
        conflicts = [
            c for c in cir if _skeleton(c.inchikey) != _skeleton(best.inchikey)
        ]
        if conflicts:
            res.candidates = _dedupe([best, *conflicts])
            res.status = "ambiguous"
            notes.append("PubChem and CIR read this name as different structures")
            return res
        if qtype == "name" and underspecified_locants(query, best.name):
            res.candidates, res.status = [best], "ambiguous"
            notes.append(
                f"the name does not fix substituent positions; PubChem picked "
                f"{best.name}. Other positional isomers exist — ask with the "
                "locants (e.g. the full name above) or the CAS number"
            )
            return res
        stereo = [c for c in cir if c.inchikey != best.inchikey]
        if stereo:
            notes.append(
                "CIR agrees on connectivity but differs in stereo/charge layer "
                f"({stereo[0].inchikey}); PubChem's reading is reported"
            )
        elif cir:
            notes.append("CIR agrees")
        else:
            notes.append("not corroborated: CIR gave no answer")
        res.candidates, res.status = [best], "resolved"
        return res

    if cir:
        res.candidates = cir
        res.status = "resolved" if len(cir) == 1 else "ambiguous"
        return res

    # PubChem is the authoritative tier: a PubChem miss is a real "not
    # found" even when the corroborating CIR leg is unreachable.
    if pubchem_faulted:
        res.status = "fault"
    else:
        res.status = "not_found"
        if faulted:
            notes.append("CIR was unreachable; OPSIN and PubChem found nothing")
    return res


_LOCANT_RE = re.compile(r"\b\d+(?:,\d+)*-")


def underspecified_locants(query: str, title: str) -> bool:
    """True when ``title`` is ``query`` plus numeric locants only, e.g.
    "dichloroethane" → "1,1-Dichloroethane", "propanol" → "1-Propanol":
    the name left the substituent positions open and the registry picked
    one isomer. Stereo descriptors are not locants and do not count."""
    if not _LOCANT_RE.search(title):
        return False

    def flat(s: str) -> str:
        return re.sub(r"[\s\-]", "", s).casefold()

    return flat(_LOCANT_RE.sub("", title)) == flat(query) and flat(title) != flat(query)


def http_fetch(timeout_s: float = 20.0, attempts: int = 2) -> Fetch:
    """Production fetcher: SSRF-pinned ``safe_get`` per call. The query
    is agent-supplied and rides the URL path; the hosts are fixed.
    ``attempts`` counts connect-phase retries (``retry_transient``)."""
    from urllib.parse import urlsplit

    from precis.utils.http import http_client, retry_transient
    from precis.utils.safe_fetch import safe_get

    def fetch(url: str) -> tuple[int, str]:
        with http_client(timeout=timeout_s) as client:
            resp = retry_transient(
                lambda: safe_get(client, url),
                host=urlsplit(url).netloc,
                attempts=attempts,
            )
        return resp.status_code, resp.text

    return fetch


__all__ = [
    "Fetch",
    "Resolution",
    "StructureReading",
    "abbreviation_table",
    "cas_checksum_ok",
    "classify_query",
    "http_fetch",
    "normalise_query",
    "resolve",
]
