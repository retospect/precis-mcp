---
id: precis-glossary-help
family: memory
title: precis — glossary terms (synonyms, mishearings, say-as, misconceptions)
summary: convention for a term lookup — one memory per term tagged 'glossary', carrying synonyms (MOF = metal-organic framework), speech-to-text mishearings (Moth for MOF), text-to-speech say-as spellings and common misconceptions; look there when a transcript reads oddly
answers:
  - what does this acronym or term mean, and what else is it called?
  - this voice transcript has a strange word — what did the speaker probably say?
  - how do I stop the speech engine mispronouncing a word with unusual spelling?
  - how do I record that a term is often misunderstood?
  - is there a glossary of terms in precis?
applies-to: put/search (kind='memory', tags=['glossary']); voice transcripts; draft narration
tags: [workflow, drafting]
kinds: [memory, alias, taxon]
status: active
---

# precis-glossary-help — look terms up before guessing

There is no dedicated glossary kind. A glossary entry is a **memory tagged
`glossary`**, one term per memory, so ordinary memory search finds it.
`kind='alias'` is for id and URL crosswalks, not vocabulary, and the taxon
`tn559460` ('repo-dev vocabulary') names repo-internal terms. Use a glossary
memory for domain terms heard or read in prose.

## Look a term up

```python
search(kind='memory', tags=['glossary'], q='MOF')
search(kind='memory', tags=['glossary'], q='Moth', mode='lexical')   # a suspected mishearing
```

Search lexically as well as semantically: a mishearing is a rare literal
string that embeddings handle poorly. An empty result is not proof the term
is unknown while the embedder is a mock; say which mode you ran.

## When a transcript looks odd

If a voice transcript contains a word that makes no sense in context
("Moth" in a materials discussion), search the glossary for the odd word, then
for the field's nearby terms. If an entry lists it under `heard as`, use the
canonical term and tell the user you substituted it. If nothing matches,
keep the original word and flag it instead of silently correcting it.

## Write an entry

```python
put(kind='memory', tags=['glossary'],
    title='MOF — metal-organic framework',
    text="""MOF = metal-organic framework (porous crystalline solid).
synonyms: metal-organic framework, coordination polymer (looser)
heard as: "Moth", "Mof", "M.O.F."   (speech-to-text)
say as: "em oh eff"                  (text-to-speech)
misconception: not the same as a zeolite; the pores are tunable by linker.""")
```

Keep these labelled lines, each optional, so a lexical search hits them:

- `synonyms:` other names and expansions of the term.
- `heard as:` what speech-to-text writes for it.
- `say as:` a respelling that makes the text-to-speech engine pronounce it
  right, for words whose spelling and sound disagree. For a narrated draft
  put the same respelling in the draft's pronunciation lexicon
  (`precis-audio-help`); the glossary entry is where you record it first.
- `misconception:` the usual wrong belief and the correction.

Search before writing: update an existing entry for the same term instead of
adding a second (`precis-memory-help`, duplicates section).

## See also

- [[precis-voice]] — writing drafts for the ear
- [[precis-audio-help]] — the per-draft and personal pronunciation lexicon
- [[precis-memory-help]] — memory capture, dedup and linking
- [[precis-alias-help]] — id and URL crosswalks
