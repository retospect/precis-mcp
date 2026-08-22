---
id: precis-draft-section-insertion
title: precis — inserting a new section into a living draft
summary: before creating a section — read the toc, prefer expanding an existing section, insert at the lowest outline level that fits, propose a new top-level section in the logbook first
answers:
  - should I create a new section or expand an existing one?
  - what should I check before inserting a new section into a draft?
applies-to: put/edit (kind='draft')
status: active
---
A protocol, not a ban — new sections are allowed, but earn their place.

1. **Read the TOC first, always.** `get(kind='draft', id='<slug>', view='toc')`
   before proposing anything new — you cannot judge "does this already exist"
   without seeing the skeleton.
2. **Prefer expanding an existing section.** If the new material fits an
   existing heading's scope, add paragraphs there (`put(kind='draft',
   id='<slug>', chunk_kind='paragraph', text='…', at={'into': 'dc<id>'})`)
   instead of minting a heading.
3. **If a new heading is genuinely needed, insert at the LOWEST outline
   level that fits.** A subsection under the closest-matching existing
   section beats a new sibling at the top — narrower scope, less
   restructuring, easier to fold back in later.
4. **A new TOP-LEVEL section is a structural change to the document** — do
   not create it directly. Propose it first as a logbook entry on the
   draft's owning quest (`put(kind='quest', id=<quest-id>, text='propose:
   new top-level section — …', entry='decision')`) and let the operator or
   a later pass confirm before it lands.
5. **Never duplicate.** Search the draft for existing coverage before
   inserting: `search(kind='draft', scope='<slug>', q='…')`.

A questless draft has no logbook to propose into — treat step 4 as "ask the
operator" instead of skipping the check.
