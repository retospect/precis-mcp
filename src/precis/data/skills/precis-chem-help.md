---
id: precis-chem-help
family: tools
title: precis — chemical name to SMILES
summary: Resolve a chemical name, CAS number or lab abbreviation to canonical SMILES + InChIKey, with the source named and ambiguity refused
answers:
  - how do I turn a chemical name into a SMILES string?
  - how do I look up a CAS number?
  - what does DCM / TEA / Boc2O mean as a structure?
  - why did my chemical name come back ambiguous?
applies-to: get/tag/link (kind='chem')
tags: [external-sources]
kinds: [chem, rxn, route]
status: active
---

# precis-chem-help — chemical name to SMILES

`get(kind='chem', id='<name | CAS | abbreviation>')` returns canonical
SMILES and the InChIKey for one compound. Use it before you write a
SMILES by hand into `put(kind='rxn', rxn_smiles=...)` or
`put(kind='route', target=...)`: a SMILES written from memory is the
most common silent error in a chemistry record.

```
get(kind='chem', id='aspirin')
get(kind='chem', id='50-78-2')            # CAS number, check digit verified
get(kind='chem', id='2-acetyloxybenzoic acid')
get(kind='chem', id='DCM')                # lab shorthand
```

A resolved answer looks like this:

```
aspirin (name) → resolved
smiles:   CC(=O)Oc1ccccc1C(=O)O
inchikey: BSYNRYMUTXBXSQ-UHFFFAOYSA-N
name:     Aspirin
source:   PubChem CID 2244 — https://pubchem.ncbi.nlm.nih.gov/compound/2244
tried:    OPSIN: miss · PubChem: hit · CIR: hit
note: CIR agrees
```

Copy the `smiles:` line as it is. Cite the `source:` line when the
structure matters to a claim.

## Where the answer comes from

The resolver tries these sources in order and stops at the first answer:

1. **Abbreviation table.** About 45 common solvents and reagents (DCM,
   THF, DMF, Et3N, DIPEA, Boc2O, mCPBA, …). It needs no network.
   Matching ignores case.
2. **OPSIN.** It parses systematic IUPAC names. A successful parse has
   only one meaning, so it is never ambiguous. CAS numbers skip it.
3. **PubChem.** It covers trivial names, trade names and CAS numbers.
4. **NCI/CADD CIR.** It is used when PubChem has no record. When PubChem
   does answer, CIR checks that answer.

When rdkit is installed, the SMILES is canonicalised with rdkit and the
InChIKey is computed from it. The `canonical:` line names the rdkit
version. If the source's own InChIKey disagrees, a `note:` line says so.

## Ambiguity is refused

The resolver gives no SMILES when an identifier has more than one reading.
Instead it lists the readings with their sources:

- shorthand with several meanings: `TEA` (triethylamine or
  triethanolamine), `DCE`, `DMA`, `DMP`;
- PubChem returning several records for one name;
- PubChem and CIR reading the same name as different structures. For
  example, PubChem maps the bare name "dichloroethane" to the
  1,1-isomer, while "1,2-dichloroethane" is the common solvent.

- a name that leaves substituent positions open, where PubChem's pick
  only adds numeric locants: "dichloroethane" gives 1,1-dichloroethane,
  and "propanol" gives 1-propanol. Here the body says `REFUSED:
  underspecified` and shows PubChem's pick. If that pick is what you
  mean, ask again with its full name.

To resolve it, ask again with the full name or the CAS number of the
reading you mean. Stereo is not checked this way, so "glucose" passes as
PubChem's D-glucose. Check the `name:` line.

## Caching and faults

Answers are cached for 30 days. This includes refusals and "not found".
To look again, pass `mode='refresh'`. If PubChem itself faulted (a
timeout or a 5xx error) and no other source answered, the call raises an
error and nothing is cached, so a retry runs the lookup again. CIR only
cross-checks: when it is unreachable, the answer stands and a note says
it was not cross-checked. A CAS number with a
bad check digit is refused before any lookup.

The structured answer is stored in the cache meta as `smiles`,
`inchikey`, `status`, `candidates` and `tiers`. Bare
`get(kind='chem')` lists recent lookups.

## Limits

- One compound per call. Mixtures and reaction SMILES are not parsed.
- Salts and hydrates come back as PubChem stores them (a
  dot-separated SMILES).
- OPSIN runs on EBI's public web service, because the image has no Java
  runtime.
