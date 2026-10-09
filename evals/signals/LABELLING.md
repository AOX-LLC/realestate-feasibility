# Blind second labelling of the extraction answer key

The answer key (`answer_key.json`) was written together with the records. To check that its
labels follow from the catalogue text and not from its author's habits, a second annotator
labelled every record without seeing the key.

## Method

- **Input to the annotator:** the catalogue (code, polarity, meaning, and the rule that
  negations, hedges and denials are not signals), and each record's remarks exactly as the app
  stores them (after normalisation and redaction), under shuffled neutral ids. No key, no tags,
  no split, no ids from the data files, no source code.
- **Annotator:** a separate language-model session, told to treat the remarks as untrusted data
  and to give one verbatim quote per code it assigns.
- **Scope:** 56 records. The two snapshot records whose remarks are null have nothing to label
  and are left out.
- **Comparison:** one cell per record and code (56 x 12 = 672). A cell agrees when the key and
  the annotator both assign the code or both do not.
- **Disagreements** would be decided by the main session from the catalogue text alone, and the
  key corrected only if the catalogue supports it. The key is frozen from this point (a test
  pins its hash); it is not edited after a recording to raise a score.

## Agreement

| Signal | Key positives | Annotator positives | Cells agreeing | Cohen's kappa |
|---|---|---|---|---|
| `teardown_language` | 9 | 9 | 56 / 56 | 1.00 |
| `as_is_sale` | 7 | 7 | 56 / 56 | 1.00 |
| `environmental_hazard` | 5 | 5 | 56 / 56 | 1.00 |
| `flood_or_drainage` | 7 | 7 | 56 / 56 | 1.00 |
| `easement_or_encroachment` | 8 | 8 | 56 / 56 | 1.00 |
| `deed_restrictions` | 4 | 4 | 56 / 56 | 1.00 |
| `conservation_or_historic_district` | 5 | 5 | 56 / 56 | 1.00 |
| `protected_trees` | 5 | 5 | 56 / 56 | 1.00 |
| `tenant_occupied` | 8 | 8 | 56 / 56 | 1.00 |
| `plans_or_permits` | 8 | 8 | 56 / 56 | 1.00 |
| `seller_financing` | 8 | 8 | 56 / 56 | 1.00 |
| `multiple_lots` | 5 | 5 | 56 / 56 | 1.00 |
| **Overall** | **79** | **79** | **672 / 672 (100%)** | **1.00** |

All 56 records received identical signal sets. Every one of the annotator's 79 quotes is a
verbatim substring of its record, and each overlaps the key's evidence span for the same code.
No injected canary word appears in any label, and no label rests on injected text.

## Disagreements

None. There was nothing to adjudicate and the key was not changed.

## What this does and does not show

- It shows the key is internally consistent with the catalogue wording: a second reader with
  no access to the key reaches the same 79 signals and the same 593 absences.
- It does not show the task is easy for a model under test, or that the labels are right on
  real listings. Both annotators are language models, the remarks are synthetic and were
  written to express each signal clearly, and the set is small. A 100% agreement figure on
  text written to be unambiguous is expected; the hard cases are the hard negatives, which the
  scorecard reports per signal.
- Borderline wording is the likeliest place for a real disagreement to appear later:
  "the seller will not repair it" is read as `as_is_sale` (SYN000107), and "occupied ... under a
  lease ... conveys subject to it" as `tenant_occupied`. Both annotators agreed on these.
