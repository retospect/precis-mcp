# Stateless time/date kind

No time/date/clock kind exists (`src/precis/handlers/calc.py` is the only
stateless kind). Mirror calc's shape (KindSpec + a get verb, no DB/embedder):
get(kind='time') → now UTC+local; get(kind='time', id=<ts>) →
parse/format/convert. `units` (conversions) and `regex` (test/match/extract)
are sibling candidates from the same template. Mechanical.
