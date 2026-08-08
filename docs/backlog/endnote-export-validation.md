# docx / EndNote export — validation pending

Round-trip correctness needs real Word + EndNote + "Update Citations and
Bibliography" — Reto is testing. Open notes: EN.Layout hardcoded to
"Annotated" (make a param if requested); docx [dc<id>] cross-refs render as
plain text, not Word REF fields (pre-existing, low-pri); [pc<id>]
cited-passage embedding round-trip unverified (EndNote drops Research-Notes
on library import; retry <custom1> if persistence wanted). Owner
`src/precis/export/endnote.py`.
