# Anki card quality rules (from Reto's review of live cards)

Dedup definitional clozes across cards — one combined cloze per concept (the
ESB example; 164388/164387 and 721137/721138 are near-copies). Avoid clozes
whose answer is one item of a long list (146392); prefer the terse
"{{topic}} includes {{item1}} {{item2}}" shape. Don't add common vocab
(164396, 164391) — adjust generation for genuinely complex vocab only.
Fold `precis::xxxx` id tags under `precis::id::xxxx` so they collapse in the
GUI. Owner card_forge / anki kind.
