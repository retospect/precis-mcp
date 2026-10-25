// abstract-cite.js — strip OCR'd citation superscripts from an abstract
// (gr515240). Journals' superscript reference markers come through PDF
// text extraction as inline digit runs — "surfaces1±3 .", "forces 8,9",
// "description 2,4±6 ,". The paper Meta tab's "✂ Strip citations" button
// (templates/papers/_meta_forms.html.j2) runs this on the abstract
// textarea — client-side only like ⤒ Unwrap: the operator reviews the
// result and Saves through the normal POST /papers/<id>/edit. Behaviour
// is pinned under node by scripts/abstract_cite_smoke.mjs.
//
// A citation group is 1–3 digit numbers, optionally a range joined by
// ±, -, – or — ("1±3", "4-6"), optionally comma-listed ("2,4±6"). It is
// stripped only when it is
//   (a) right before sentence punctuation — glued or spaced
//       ("surfaces1±3 .", "forces 8,9\n."), or
//   (b) glued to a lowercase word char / ")" / "]" AND structured — at
//       least one range or comma join inside ("surfaces1±3",
//       "structures2,4" mid-sentence).
//
// That anchoring is what keeps real numbers alive:
//   "30±130 mm", "0.2±0.5 mm" — a space before and a unit after;
//   "600-fold"                  — "-" followed by a letter, not a digit;
//   "1,000", "12,500"           — comma items cap at two digits and a
//                                 group can't start right after ",";
//   "3.14", "2.0", "2020"       — a group can't start inside a number
//                                 (digit/"." lookbehind) nor end at a "."
//                                 a digit follows;
//   "CO2", "(2).", "[3]."       — uppercase / "(" / "[" glue doesn't
//                                 count, so element symbols, equation
//                                 numbers and bracketed refs survive;
//   "sensor 7", "peeling 2"     — a bare spaced digit mid-sentence is
//                                 indistinguishable from a real count:
//                                 it stays.
// The removal also folds the gap the superscript leaves before its
// punctuation ("surfaces1±3 ." → "surfaces."), and Æ — the OCR'd fi
// ligature in "Æve"/"Ærst" — folds to "fi" before a lowercase letter,
// the same mapping precis.ingest.marker._LIGATURES gives \ufb01.

const _RANGE = '[ \\t]*\\n?[ \\t]*[±\\-–—][ \\t]*\\n?[ \\t]*\\d{1,3}';
const _COMMA = '[ \\t]*\\n?[ \\t]*,[ \\t]*\\n?[ \\t]*';
// Comma-joined items cap at two digits so "1,000" / "12,500" can never
// parse as a citation list.
const _ITEM = `\\d{1,3}(?:${_RANGE})?`;
const _COMMA_ITEM = `\\d{1,2}(?:${_RANGE})?`;
const _LIST = `${_ITEM}(?:${_COMMA}${_COMMA_ITEM})*`;
const _PUNCT = '[.,;:!?]';

const _BEFORE_PUNCT = new RegExp(
  `[ \\t]?(?<![\\d.,\\nA-Z(\\[])${_LIST}[ \\t]*\\n?[ \\t]*(?=${_PUNCT}(?!\\d))`,
  'g',
);
const _GLUED_STRUCTURED = new RegExp(
  `(?<=[a-z)\\]])\\d{1,3}(?:${_RANGE}|${_COMMA}${_COMMA_ITEM})` +
    `(?:${_COMMA}${_COMMA_ITEM})*(?![a-zA-Z\\d])`,
  'g',
);

export function stripCitationMarks(text) {
  return (
    text
      .replace(_BEFORE_PUNCT, '')
      .replace(_GLUED_STRUCTURED, '')
      // A superscript's gap before its stranded punctuation goes too.
      .replace(/[ \t]*\n[ \t]*(?=[.,;:!?])/g, '')
      .replace(/[ \t]+(?=[.,;:!?])/g, '')
      .replace(/Æ(?=[a-z])/g, 'fi')
  );
}
