# LaTeX → speech for voice drafts

`speakable()` skips math (a spoken "equation" cue, drops inline $…$) — weak
for math-heavy drafts. Add math_speech ∈ {skip, brief, full}: v1 = a
pure-Python heuristic (^ → "to the power of", \frac → "over", greek,
operators); accessibility-grade = MathSpeak/ClearSpeak via the Speech Rule
Engine over MathML (latex2mathml in hand; MathML→speech is a node shell-out);
per-equation author override (pronunciation-lexicon pattern). Default stays
brief. Owner `src/precis/draft/narrate.py`.
