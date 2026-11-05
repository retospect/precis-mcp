# Printable vendor ink before outline selection

The November 5 TAZ route has one full-DRC error: U2's retained vendor outline
uses 0.1016 mm strokes below its fabrication profile's 0.15 mm minimum.
The existing half-ink selection sees 2.208769 mm of 3.706251 mm surviving
geometric cuts (59.6%) and selects it, but only 0.060251 mm of the original
ink is printable. Root cause and finite saved-state controls are in
`scratch/review/silk-printability-1105/`.

Reuse the fabrication minimum already checked by silk DRC. Treat a vendor
stroke below that minimum as a non-survivor, after calculating the original
submitted-ink length. Do not remove thin strokes from that denominator:
otherwise a tiny legal arc would become 100% of the artwork and incorrectly
suppress the existing minimal-ring fallback. Keep printable vendor strokes
at their original width, retain all geometric clipping, and select the
existing fallback when less than half the original ink survives. Do not
thicken or redraw vendor artwork, lower the fabrication minimum, enlarge
the fallback offset, or change copper. Preserve the no-capability caller
contract where there is no authoritative width floor.

Write real geometry regressions before the fix: all-thin vendor outline;
mostly thin with a small printable tail; legal and exact-floor artwork;
exactly half printable artwork; existing clipping and no-capability behavior.
Verify meaningful positive and negative outcomes through the shared silk
printability checker and generated outline geometry. Preserve original
vendor widths where selected. Add the non-obvious denominator rationale to
the owning package documentation.

After review, regenerate silk only from the saved route's exact candidate,
catalog and copper. Require every non-silk model field and copper hash to
remain identical, correct the U2 printability error, re-export, and repeat
the actual pad/15-hard-distance readback. Do not run routing again for this
change. No application deployment, production mutation or manufacturing
approval is part of this fix.
