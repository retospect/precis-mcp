# Secrets entry feedback — gr467559 / gr467562

Scope: current-input character/line feedback beside each write-only add/replace control. Base 1b4d8b5 already includes shipped R10 multiline writer (516dc91 /8.35.9); do not reapply it. Native Python navigation indexes /app, not this source tree; local targeted reads confirm the current macro/routes/tests.

Contract: show Entered: N chars · M lines. Characters count Unicode code points (including whitespace and newline characters; CRLF is two characters), not UTF-16 units, bytes or graphemes. Empty input is 0 chars/0 lines. Nonempty lines are newline-delimited segments; CRLF is one delimiter, LF/CR one each; blank/trailing lines count. Browser textarea normalizes pasted CRLF to LF, so counts describe the actual current control value.

Feedback is entered text only, NOT saved-state evidence, key validity, storage or authentication proof. Saved masked hints remain unchanged; inventory exposes only name/hint/time and no plaintext or persisted lengths. Saved-state counts would require a separately agreed metadata/API contract and are outside this approved current-input slice. gr467562 multiline premise is already shipped; remaining count request is addressed only for current entry. Do not close either gripe before deployed browser/native synthetic dogfood.

Preserve: empty replacements no-op; exact route multiline contents; one enabled submitted value control; newline mode-switch guard; stored values never repopulate. No endpoints/schema/resolver/crypto/probes/dependencies. Add concise counting explanation and owning rationale.

Validation: existing focused route tests (including LF/CRLF write-only and blank no-op), rendered feedback smoke assertion, scoped container types, Ruff/format/diff check. Bounded real browser/Alpine synthetic fixtures: empty/ASCII/astral/combining/blank/trailing newline counts, paste, clear/reset, mode switching/guard, single enabled value and preserved masked inventory. Coordinator owns version/full gate/deploy; postdeploy replay same synthetic cases plus isolated roundtrip before closure.

## Local result
10 focused route tests PASS; scoped container types3 PASS; scoped Ruff/format/diff checks PASS. Actual Jinja controls with bundled Alpine, offline Chromium151.0.7922.34:8 checks PASS,0 JS errors,0 production requests. Evidence .scratch/browser-feedback.json and replay .scratch/check_feedback_cdp.py. Browser native insertion retains blank/trailing lines and mode guard; Unicode astral/combining code-point count verified. Initial cached WebKit launch stalled and stopped; browser download timed out and stopped; fixture shell needed UTF-8 declaration and a visible textarea wait, corrected before passing. No product behavior fix beyond count feedback was needed.
