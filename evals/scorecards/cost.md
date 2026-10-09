# What the recorded runs cost

All figures are in US dollars at agent-core's packaged prices (the small tier is Haiku 4.5, the mid
tier Sonnet 5.5). The first table is read from the `llm_call` ledger after replaying day 1 and day 2
from the committed recordings on a fresh database; a replayed call carries the cost of the call
that was recorded. The second is the paid recording session.

## A day's run, by stage (replayed from the committed recordings)

| Day | Stage | Calls | Input tokens | Output tokens | Cost |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-10-01 | signals | 10 | 21,723 | 705 | $0.025248 |
| 2026-10-01 | narratives (5 computed candidates, one repair) | 6 | 14,140 | 6,005 | $0.088330 |
| 2026-10-02 | signals | 6 | 13,022 | 397 | $0.015007 |
| 2026-10-02 | narratives | 2 | 4,761 | 2,560 | $0.035122 |
| | **Both days** | 24 | 53,646 | 9,667 | $0.163707 |

Day 2 calls only for what changed: six remarks day 1 never sent, and the narratives of the two
candidates whose inputs changed. A same-day re-run makes no call.

## The recording session

| Step | Calls | Cost |
| --- | ---: | ---: |
| Extraction eval, dev and holdout | 56 | $0.139176 |
| Day 1 (10 signals, 5 narratives) | 15 | $0.103806 |
| Day 2 (6 signals, 2 narratives) | 8 | $0.050129 |
| Narrative eval, 13 cases (9 scored, 4 cut off) and one repair | 14 (cost known for 10) | $0.153672 |
| Ledger total, calls with a known cost | 89 | $0.446783 |
| Four narrative calls cut off at `max_tokens` (cost unknown to the ledger; it counts the $0.05 reservation each) | 4 | $0.200000 as counted |

The four cut-off calls each ended at 1,500 output tokens, the mid tier's `max_tokens`. Their
recorded usage prices them at about $0.0789 together, so the session's true cost is about $0.5257
and the ledger, which never under-counts, shows $0.6468.

## A live day, narratives only (projection)

Live data has no remarks, so a live day makes narrative calls only, for computed candidates whose
inputs changed. The measured average is $0.0154 a narrative call (8 calls, $0.123452, one of them a
repair). A first day with 5 computed candidates is about $0.08; a day with 2 changed candidates is
about $0.03. These are projections from two snapshot days, not measurements of live data.
