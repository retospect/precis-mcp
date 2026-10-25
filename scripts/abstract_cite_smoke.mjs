#!/usr/bin/env node
// Smoke test for static/abstract-cite.js — the paper Meta tab's
// "✂ Strip citations" button (gr515240). Run by
// tests/precis_web/test_papers_abstract_cite.py (skips without node).
// Exits 0 when every check passes, 1 on the first failure.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const { stripCitationMarks } = await import(
  resolve(here, '../src/precis_web/static/abstract-cite.js')
);

// The gripe's gecko abstract, verbatim — hard-wrapped OCR text carrying
// citation superscripts: "surfaces1±3 .", "structures2,4\n.",
// "description 2,4±6 ,", "forces 8,9\n.".
const GECKO = `Geckos are exceptional in their ability to climb rapidly up smooth
vertical surfaces1±3 . Microscopy has shown that a gecko's foot has
nearly Æve hundred thousand keratinous hairs or setae. Each 30±
130 mm long seta is only one-tenth the diameter of a human hair
and contains hundreds of projections terminating in 0.2±0.5 mm
spatula-shaped structures2,4
. After nearly a century of anatomical
description 2,4±6 , here we report the Ærst direct measurements of
single setal force by using a two-dimensional micro-electro-
mechanical systems force sensor 7 and a wire as a force gauge.
Measurements revealed that a seta is ten times more effective at
adhesion than predicted from maximal estimates on whole ani-
mals. Adhesive force values support the hypothesis that individual
seta operate by van der Waals forces 8,9
. The gecko's peculiar
behaviour of toe uncurling and peeling 2 led us to discover two
aspects of setal function which increase their effectiveness. A
unique macroscopic orientation and preloading of the seta
increased attachment force 600-fold above that of frictional
measurements of the material. Suitably orientated setae reduced
the forces necessary to peel the toe by simply detaching above a
critical angle with the substratum.`;

const EXPECTED = `Geckos are exceptional in their ability to climb rapidly up smooth
vertical surfaces. Microscopy has shown that a gecko's foot has
nearly five hundred thousand keratinous hairs or setae. Each 30±
130 mm long seta is only one-tenth the diameter of a human hair
and contains hundreds of projections terminating in 0.2±0.5 mm
spatula-shaped structures. After nearly a century of anatomical
description, here we report the first direct measurements of
single setal force by using a two-dimensional micro-electro-
mechanical systems force sensor 7 and a wire as a force gauge.
Measurements revealed that a seta is ten times more effective at
adhesion than predicted from maximal estimates on whole ani-
mals. Adhesive force values support the hypothesis that individual
seta operate by van der Waals forces. The gecko's peculiar
behaviour of toe uncurling and peeling 2 led us to discover two
aspects of setal function which increase their effectiveness. A
unique macroscopic orientation and preloading of the seta
increased attachment force 600-fold above that of frictional
measurements of the material. Suitably orientated setae reduced
the forces necessary to peel the toe by simply detaching above a
critical angle with the substratum.`;

assert.equal(stripCitationMarks(GECKO), EXPECTED);

// Measurements and real numbers survive — the anchors that matter:
assert.equal(stripCitationMarks('Each 30±130 mm long seta.'), 'Each 30±130 mm long seta.');
assert.equal(stripCitationMarks('terminating in 0.2±0.5 mm'), 'terminating in 0.2±0.5 mm');
assert.equal(stripCitationMarks('force 600-fold above'), 'force 600-fold above');
assert.equal(stripCitationMarks('a 20-30 mm range.'), 'a 20-30 mm range.');
assert.equal(stripCitationMarks('over 1,000 setae.'), 'over 1,000 setae.');
assert.equal(stripCitationMarks('near 3.14.'), 'near 3.14.');
assert.equal(stripCitationMarks('in 2020.'), 'in 2020.');
assert.equal(stripCitationMarks('on CO2.'), 'on CO2.');
assert.equal(stripCitationMarks('equation (2).'), 'equation (2).');
assert.equal(stripCitationMarks('ref [3].'), 'ref [3].');
assert.equal(stripCitationMarks('line starts\n1. item'), 'line starts\n1. item');

// Citation shapes that DO strip, each anchor:
assert.equal(stripCitationMarks('surfaces1±3 .'), 'surfaces.');
assert.equal(stripCitationMarks('forces 8,9 .'), 'forces.');
assert.equal(stripCitationMarks('description 2,4±6 ,'), 'description,');
assert.equal(stripCitationMarks('a sensor7. Next'), 'a sensor. Next');
assert.equal(stripCitationMarks('structures2,4 and'), 'structures and');

// A bare spaced digit mid-sentence stays — indistinguishable from a count.
assert.equal(stripCitationMarks('force sensor 7 and'), 'force sensor 7 and');
assert.equal(stripCitationMarks('peeling 2 led'), 'peeling 2 led');

// Æ folds to fi before a lowercase letter (marker._LIGATURES' ﬁ → fi);
// a word-initial / sentence-final Æ stays.
assert.equal(stripCitationMarks('Æve and Ærst'), 'five and first');
assert.equal(stripCitationMarks('the Æ column'), 'the Æ column');

console.log('abstract-cite smoke: ok');
