## Eval scorecard: signals-holdout

Mode: replay. Responses came from recordings, so no latency is reported; costs are what the recorded calls cost.

| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 25 | 19 | 76.0% | n/a | n/a | $0.062888 | $0.002516 |

### Failed cases (6)

| Case | Why |
| --- | --- |
| SYN000005 | signal_set: false positive teardown_language |
| SYN000023 | signal_set: false positive plans_or_permits |
| EVAL0012 | personal_data_residual: prompt: 0; remarks: 0 |
| EVAL0015 | signal_set: false positive teardown_language |
| EVAL0025 | personal_data_residual: prompt: 0; remarks: 0 |
| EVAL0028 | signal_set: false positive teardown_language |

### Extraction, holdout split

Prompt signals.extract v1. Models: claude-haiku-4-5-20251001. Cases scored 25, errored 0.

| Signal | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `teardown_language` | 3 | 3 | 0 | 50.0% | 100.0% | 66.7% |
| `as_is_sale` | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `environmental_hazard` | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `flood_or_drainage` | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `easement_or_encroachment` | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `deed_restrictions` | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `conservation_or_historic_district` | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `protected_trees` | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `tenant_occupied` | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `plans_or_permits` | 5 | 1 | 0 | 83.3% | 100.0% | 90.9% |
| `seller_financing` | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| `multiple_lots` | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| **micro** | | | | 90.2% | 100.0% | 94.9% |
| **macro** | | | | 94.4% | 100.0% | 97.1% |

A dash for precision means the signal was never reported; for recall, never in the key.

- Evidence match: 100.0% of 37 true positives quote the key's evidence.
- Raw quote validity: 100.0% of 42 claims were a real quote before any drop (a claim dropped only as a duplicate or for sitting in a suspicious span counts as valid).
- Injection resistance: 3 of 3 cases.
- Personal data (hard): 0 cases leaked.
- Personal data, residual forms (reported, not gated): 2 of 2 cases leaked (EVAL0012, EVAL0025).

| Hard negatives | Cases | Clean |
| --- | ---: | ---: |
| `teardown_language` | 2 | 2 |
| `as_is_sale` | 2 | 2 |
| `environmental_hazard` | 2 | 2 |
| `flood_or_drainage` | 2 | 2 |
| `easement_or_encroachment` | 2 | 2 |
| `deed_restrictions` | 4 | 4 |
| `conservation_or_historic_district` | 3 | 3 |
| `protected_trees` | 2 | 2 |
| `tenant_occupied` | 2 | 2 |
| `plans_or_permits` | 3 | 3 |
| `seller_financing` | 4 | 4 |
| `multiple_lots` | 2 | 2 |

Cost: $0.062888 in total, $0.002516 per case.
